from __future__ import annotations

import sqlite3
import unittest

import storage_db
import withdrawal_service as withdrawals


NOW = "2026-06-28T00:00:00+00:00"
ACCOUNT = {"type": "bank", "accountName": "测试代理", "accountNoMasked": "6222****8888"}


class WithdrawalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage_db.init_db(self.conn)
        self._insert_agent("agent_1")

    def tearDown(self) -> None:
        self.conn.close()

    def test_schema_creates_withdrawal_table(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'agent_withdrawal_requests'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("agent_withdrawal_requests", storage_db.REQUIRED_TABLES)

    def test_create_requires_active_agent_and_minimum_amount(self) -> None:
        self._insert_settlement("set_paid", "agent_1", 20000, status="paid")
        with self.assertRaises(withdrawals.InvalidWithdrawalInput) as minimum:
            withdrawals.create_withdrawal_request(
                self.conn,
                agent_id="agent_1",
                amount_cents=9999,
                account_snapshot=ACCOUNT,
            )
        self.assertEqual(minimum.exception.details["minimumAmountCents"], 10000)

        self._insert_agent("agent_inactive", status="inactive")
        with self.assertRaises(withdrawals.WithdrawalConflict) as inactive:
            withdrawals.create_withdrawal_request(
                self.conn,
                agent_id="agent_inactive",
                amount_cents=10000,
                account_snapshot=ACCOUNT,
            )
        self.assertEqual(inactive.exception.details["status"], "inactive")

        with self.assertRaises(withdrawals.WithdrawalNotFound):
            withdrawals.create_withdrawal_request(
                self.conn,
                agent_id="missing_agent",
                amount_cents=10000,
                account_snapshot=ACCOUNT,
            )

    def test_create_uses_conservative_paid_settlement_balance_and_locks_pending_requests(self) -> None:
        self._insert_settlement("set_paid", "agent_1", 30000, status="paid")
        self._insert_settlement("set_pending", "agent_1", 90000, status="pending")
        self._insert_commission("co_eligible", "agent_1", 50000, status="eligible")

        balance = withdrawals.calculate_withdrawable_balance(self.conn, "agent_1")

        self.assertEqual(balance["paidSettlementCents"], 30000)
        self.assertEqual(balance["availableCents"], 30000)
        self.assertEqual(balance["excludedUnpaidSettlementCents"], 90000)
        self.assertEqual(balance["excludedEligibleCommissionCents"], 50000)
        self.assertEqual(balance["policy"], "paid_settlements_minus_pending_approved_paid_withdrawals")

        request = withdrawals.create_withdrawal_request(
            self.conn,
            agent_id="agent_1",
            amount_cents=20000,
            account_snapshot=ACCOUNT,
            metadata={"source": "unit-test"},
        )

        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["amountCents"], 20000)
        self.assertEqual(request["accountSnapshot"], ACCOUNT)
        self.assertEqual(request["balanceSnapshot"]["availableCents"], 30000)
        self.assertEqual(request["balanceSnapshot"]["requestedAmountCents"], 20000)
        self.assertEqual(request["metadata"]["source"], "unit-test")
        self.assertEqual(withdrawals.calculate_withdrawable_balance(self.conn, "agent_1")["availableCents"], 10000)

        with self.assertRaises(withdrawals.WithdrawalConflict) as exceeded:
            withdrawals.create_withdrawal_request(
                self.conn,
                agent_id="agent_1",
                amount_cents=15000,
                account_snapshot=ACCOUNT,
            )
        self.assertEqual(exceeded.exception.details["availableCents"], 10000)

    def test_list_get_and_status_updates_audit_metadata(self) -> None:
        self._insert_settlement("set_paid", "agent_1", 20000, status="paid")
        request = withdrawals.create_withdrawal_request(
            self.conn,
            agent_id="agent_1",
            amount_cents=10000,
            account_snapshot=ACCOUNT,
            withdrawal_id="wd_1",
        )

        self.assertEqual(withdrawals.get_withdrawal_request(self.conn, "wd_1")["id"], "wd_1")
        self.assertEqual([item["id"] for item in withdrawals.list_withdrawal_requests(self.conn, agent_id="agent_1")], ["wd_1"])
        self.assertEqual([item["id"] for item in withdrawals.list_withdrawal_requests(self.conn, status="pending")], ["wd_1"])
        self.assertEqual(request["metadata"]["statusHistory"][0]["to"], "pending")

        approved = withdrawals.update_withdrawal_request_status(
            self.conn,
            "wd_1",
            "approved",
            reason="manual review passed",
            metadata={"actor": "ops_1"},
        )

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["statusReason"], "manual review passed")
        self.assertIsNotNone(approved["approvedAt"])
        self.assertEqual(approved["metadata"]["statusHistory"][-1]["from"], "pending")
        self.assertEqual(approved["metadata"]["statusHistory"][-1]["to"], "approved")
        self.assertEqual(approved["metadata"]["statusHistory"][-1]["metadata"], {"actor": "ops_1"})

        paid = withdrawals.update_withdrawal_request_status(
            self.conn,
            "wd_1",
            "paid",
            reason="finance transfer confirmed",
            metadata={"transferNo": "T001"},
        )
        self.assertEqual(paid["status"], "paid")
        self.assertIsNotNone(paid["paidAt"])
        self.assertEqual(paid["metadata"]["statusHistory"][-1]["to"], "paid")

        with self.assertRaises(withdrawals.WithdrawalConflict):
            withdrawals.update_withdrawal_request_status(self.conn, "wd_1", "canceled")

    def test_rejected_or_canceled_requests_release_balance(self) -> None:
        self._insert_settlement("set_paid", "agent_1", 20000, status="paid")
        first = withdrawals.create_withdrawal_request(
            self.conn,
            agent_id="agent_1",
            amount_cents=15000,
            account_snapshot=ACCOUNT,
        )
        self.assertEqual(withdrawals.calculate_withdrawable_balance(self.conn, "agent_1")["availableCents"], 5000)

        withdrawals.update_withdrawal_request_status(self.conn, first["id"], "rejected", reason="account mismatch")
        self.assertEqual(withdrawals.calculate_withdrawable_balance(self.conn, "agent_1")["availableCents"], 20000)

        second = withdrawals.create_withdrawal_request(
            self.conn,
            agent_id="agent_1",
            amount_cents=15000,
            account_snapshot=ACCOUNT,
        )
        withdrawals.update_withdrawal_request_status(self.conn, second["id"], "canceled", reason="agent canceled")
        self.assertEqual(withdrawals.calculate_withdrawable_balance(self.conn, "agent_1")["availableCents"], 20000)

    def test_approval_and_payment_require_agent_still_active(self) -> None:
        self._insert_settlement("set_paid", "agent_1", 20000, status="paid")
        request = withdrawals.create_withdrawal_request(
            self.conn,
            agent_id="agent_1",
            amount_cents=10000,
            account_snapshot=ACCOUNT,
        )
        with self.conn:
            self.conn.execute("UPDATE agent_profiles SET status = 'suspended' WHERE id = 'agent_1'")

        with self.assertRaises(withdrawals.WithdrawalConflict) as approval:
            withdrawals.update_withdrawal_request_status(self.conn, request["id"], "approved")
        self.assertEqual(approval.exception.details["status"], "suspended")

        rejected = withdrawals.update_withdrawal_request_status(self.conn, request["id"], "rejected")
        self.assertEqual(rejected["status"], "rejected")

    def _insert_agent(self, agent_id: str, *, status: str = "active") -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_profiles (id, user_id, agent_code, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agent_id, f"user_{agent_id}", agent_id.upper(), status, NOW, NOW),
            )

    def _insert_settlement(self, settlement_id: str, agent_id: str, amount_cents: int, *, status: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO commission_settlements (
                    id, agent_id, settlement_no, total_commission_amount,
                    order_count, status, created_at, updated_at, paid_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    settlement_id,
                    agent_id,
                    settlement_id.upper(),
                    amount_cents,
                    status,
                    NOW,
                    NOW,
                    NOW if status == "paid" else None,
                ),
            )

    def _insert_commission(self, commission_id: str, agent_id: str, amount_cents: int, *, status: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO commission_orders (
                    id, order_id, agent_id, customer_id, relation_id, order_amount,
                    commission_amount, commission_rate_bps, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 2000, ?, ?, ?)
                """,
                (
                    commission_id,
                    f"order_{commission_id}",
                    agent_id,
                    f"customer_{commission_id}",
                    f"relation_{commission_id}",
                    amount_cents * 5,
                    amount_cents,
                    status,
                    NOW,
                    NOW,
                ),
            )


if __name__ == "__main__":
    unittest.main()
