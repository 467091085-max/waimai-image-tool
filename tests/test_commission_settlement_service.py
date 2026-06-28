from __future__ import annotations

import sqlite3
import unittest

import commission_settlement_service as settlements
import storage_db


NOW = "2026-06-28T00:00:00+00:00"
OLD = "2026-06-18T00:00:00+00:00"
RECENT = "2026-06-25T00:00:00+00:00"


class CommissionSettlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage_db.init_db(self.conn)
        self._insert_agent("agent_1")

    def tearDown(self) -> None:
        self.conn.close()

    def test_release_eligible_commissions_respects_hold_period(self) -> None:
        self._insert_commission("co_old", "order_old", "agent_1", 10000, 2000, OLD)
        self._insert_commission("co_recent", "order_recent", "agent_1", 5000, 1000, RECENT)

        result = settlements.release_eligible_commissions(self.conn, agent_id="agent_1", min_age_days=7, now=NOW)

        self.assertEqual(result["released"], 1)
        self.assertEqual(result["commissionAmount"], 2000)
        self.assertEqual(result["orderIds"], ["co_old"])
        self.assertEqual(self._commission_status("co_old"), "eligible")
        self.assertEqual(self._commission_status("co_recent"), "pending")

    def test_create_settlement_and_mark_paid_settles_attached_orders(self) -> None:
        self._insert_commission("co_1", "order_1", "agent_1", 10000, 2000, OLD, status="eligible")
        self._insert_commission("co_2", "order_2", "agent_1", 5000, 1000, OLD, status="eligible")

        settlement = settlements.create_commission_settlement(
            self.conn,
            agent_id="agent_1",
            period_start="2026-06-01",
            period_end="2026-06-30",
        )

        self.assertEqual(settlement["agentId"], "agent_1")
        self.assertEqual(settlement["totalOrderAmount"], 15000)
        self.assertEqual(settlement["totalCommissionAmount"], 3000)
        self.assertEqual(settlement["orderCount"], 2)
        self.assertEqual(settlement["status"], "pending")
        self.assertEqual({order["id"] for order in settlement["orders"]}, {"co_1", "co_2"})
        self.assertEqual(self._commission_settlement_id("co_1"), settlement["id"])

        paid = settlements.update_commission_settlement_status(
            self.conn,
            settlement["id"],
            "paid",
            paid_at="2026-06-29T00:00:00+00:00",
        )

        self.assertEqual(paid["status"], "paid")
        self.assertEqual(paid["paidAt"], "2026-06-29T00:00:00+00:00")
        self.assertEqual(self._commission_status("co_1"), "settled")
        self.assertEqual(self._commission_status("co_2"), "settled")

    def test_failed_settlement_releases_orders_for_retry(self) -> None:
        self._insert_commission("co_1", "order_1", "agent_1", 10000, 2000, OLD, status="eligible")
        settlement = settlements.create_commission_settlement(self.conn, agent_id="agent_1")

        failed = settlements.update_commission_settlement_status(
            self.conn,
            settlement["id"],
            "failed",
            failure_reason="bank rejected",
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failureReason"], "bank rejected")
        self.assertEqual(self._commission_status("co_1"), "eligible")
        self.assertEqual(self._commission_settlement_id("co_1"), "")

        retry = settlements.create_commission_settlement(self.conn, agent_id="agent_1")
        self.assertNotEqual(retry["id"], settlement["id"])
        self.assertEqual(retry["orderCount"], 1)

    def test_create_settlement_rejects_mixed_agent_or_noneligible_orders(self) -> None:
        self._insert_agent("agent_2")
        self._insert_commission("co_1", "order_1", "agent_1", 10000, 2000, OLD, status="eligible")
        self._insert_commission("co_2", "order_2", "agent_2", 10000, 2000, OLD, status="eligible")
        self._insert_commission("co_3", "order_3", "agent_1", 10000, 2000, OLD, status="pending")

        with self.assertRaises(settlements.CommissionSettlementConflict):
            settlements.create_commission_settlement(
                self.conn,
                agent_id="agent_1",
                commission_order_ids=["co_1", "co_2"],
            )
        with self.assertRaises(settlements.CommissionSettlementConflict):
            settlements.create_commission_settlement(
                self.conn,
                agent_id="agent_1",
                commission_order_ids=["co_3"],
            )

    def _insert_agent(self, agent_id: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_profiles (id, user_id, agent_code, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (agent_id, f"user_{agent_id}", agent_id.upper(), OLD, OLD),
            )

    def _insert_commission(
        self,
        commission_id: str,
        order_id: str,
        agent_id: str,
        order_amount: int,
        commission_amount: int,
        created_at: str,
        *,
        status: str = "pending",
    ) -> None:
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
                    order_id,
                    agent_id,
                    f"customer_{commission_id}",
                    f"relation_{commission_id}",
                    order_amount,
                    commission_amount,
                    status,
                    created_at,
                    created_at,
                ),
            )

    def _commission_status(self, commission_id: str) -> str:
        return str(self.conn.execute("SELECT status FROM commission_orders WHERE id = ?", (commission_id,)).fetchone()[0])

    def _commission_settlement_id(self, commission_id: str) -> str:
        return str(self.conn.execute("SELECT settlement_id FROM commission_orders WHERE id = ?", (commission_id,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
