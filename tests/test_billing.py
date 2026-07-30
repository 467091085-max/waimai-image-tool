from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import billing
from shared import payment_catalog


class BillingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "app.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recharge_packages_and_custom_recharge_credit_balance(self) -> None:
        first = billing.credit_recharge("u1", "recharge-49", 49, db_path=self.db_path)
        second = billing.credit_recharge("u1", "recharge-custom", 120, db_path=self.db_path)

        self.assertEqual(first["points"], 500)
        self.assertEqual(first["balance"], 500)
        self.assertEqual(second["points"], 1200)
        self.assertEqual(second["balance"], 1700)
        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 1700)

    def test_recharge_package_payload_comes_from_versioned_server_catalog(self) -> None:
        packages = billing.recharge_packages_payload()
        catalog = payment_catalog.catalog_snapshot()

        self.assertEqual(billing.RECHARGE_PACKAGES, {49: 500, 99: 1040, 299: 3190})
        self.assertEqual(
            [(item["cash"], item["points"]) for item in packages],
            [(49, 500), (99, 1040), (299, 3190)],
        )
        self.assertEqual(
            [item["packageId"] for item in packages],
            ["starter-500", "store-1040", "team-3190"],
        )
        self.assertEqual(billing.pricing_payload()["catalogDigest"], catalog["catalogDigest"])

    def test_debit_uses_image_charge_formula(self) -> None:
        billing.credit_recharge("u1", "recharge-99", 99, db_path=self.db_path)

        charge = billing.calculate_image_charge(
            image_count=3,
            quality="premium",
            watermark=True,
            platforms=["meituan", "jd"],
        )
        result = billing.debit_account("u1", "generation-1", charge, db_path=self.db_path)

        self.assertEqual(charge, 3 * 20 + 50 + 100)
        self.assertEqual(result["points"], 210)
        self.assertEqual(result["balance"], 830)

    def test_debit_rejects_insufficient_balance_without_negative_balance(self) -> None:
        billing.credit_recharge("u1", "recharge-49", 49, db_path=self.db_path)

        with self.assertRaises(billing.InsufficientBalance):
            billing.debit_account("u1", "too-expensive", 501, db_path=self.db_path)

        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 500)
        with sqlite3.connect(self.db_path) as conn:
            ledger_count = conn.execute("SELECT COUNT(*) FROM ledger WHERE direction = 'debit'").fetchone()[0]
        self.assertEqual(ledger_count, 0)

    def test_credit_and_debit_are_idempotent_by_order_id(self) -> None:
        first_credit = billing.credit_recharge("u1", "recharge-49", 49, db_path=self.db_path)
        second_credit = billing.credit_recharge("u1", "recharge-49", 49, db_path=self.db_path)
        first_debit = billing.debit_account("u1", "generation-1", 200, db_path=self.db_path)
        second_debit = billing.debit_account("u1", "generation-1", 200, db_path=self.db_path)

        self.assertFalse(first_credit["idempotent"])
        self.assertTrue(second_credit["idempotent"])
        self.assertFalse(first_debit["idempotent"])
        self.assertTrue(second_debit["idempotent"])
        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 300)

        with self.assertRaises(billing.OrderConflict):
            billing.debit_account("u1", "generation-1", 201, db_path=self.db_path)

    def test_refund_is_bounded_to_the_owned_source_debit_and_idempotent(self) -> None:
        billing.credit_account("u1", "seed", 500, db_path=self.db_path)
        billing.debit_account("u1", "generation-1", 210, db_path=self.db_path)

        first = billing.refund_debit(
            "u1",
            "generation-1",
            points=110,
            refund_order_id="refund-generation-1",
            db_path=self.db_path,
        )
        second = billing.refund_debit(
            "u1",
            "generation-1",
            points=110,
            refund_order_id="refund-generation-1",
            db_path=self.db_path,
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 400)
        with self.assertRaises(billing.InvalidBillingInput):
            billing.refund_debit(
                "u1",
                "generation-1",
                points=211,
                db_path=self.db_path,
            )
        with self.assertRaises(billing.InvalidBillingInput):
            billing.refund_debit(
                "u2",
                "generation-1",
                points=1,
                db_path=self.db_path,
            )

    def test_refund_to_total_only_credits_the_unsettled_remainder(self) -> None:
        billing.credit_account("u1", "seed", 500, db_path=self.db_path)
        billing.debit_account("u1", "generation-1", 210, db_path=self.db_path)

        partial = billing.refund_debit_to_total(
            "u1",
            "generation-1",
            target_points=40,
            refund_order_prefix="refund-generation-1",
            db_path=self.db_path,
        )
        full = billing.refund_debit_to_total(
            "u1",
            "generation-1",
            target_points=210,
            refund_order_prefix="refund-generation-1",
            db_path=self.db_path,
        )
        repeated = billing.refund_debit_to_total(
            "u1",
            "generation-1",
            target_points=210,
            refund_order_prefix="refund-generation-1",
            db_path=self.db_path,
        )

        self.assertEqual(partial["points"], 40)
        self.assertEqual(partial["refundedTotal"], 40)
        self.assertEqual(full["points"], 170)
        self.assertEqual(full["refundedTotal"], 210)
        self.assertEqual(repeated["points"], 0)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 500)
        with self.assertRaises(billing.InvalidBillingInput):
            billing.refund_debit(
                "u1",
                "generation-1",
                points=1,
                refund_order_id="refund-generation-1-extra",
                db_path=self.db_path,
            )

    def test_concurrent_refund_to_total_never_exceeds_source_debit(self) -> None:
        billing.credit_account("u1", "seed", 500, db_path=self.db_path)
        billing.debit_account("u1", "generation-1", 210, db_path=self.db_path)

        def settle() -> dict:
            return billing.refund_debit_to_total(
                "u1",
                "generation-1",
                target_points=210,
                refund_order_prefix="refund-generation-1",
                db_path=self.db_path,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _index: settle(), range(8)))

        self.assertEqual(sum(int(result["points"]) for result in results), 210)
        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 500)
        with sqlite3.connect(self.db_path) as conn:
            refunded = conn.execute(
                """
                SELECT COALESCE(SUM(points), 0)
                FROM refund_allocations
                WHERE source_order_id = ?
                """,
                ("generation-1",),
            ).fetchone()[0]
        self.assertEqual(refunded, 210)

    def test_init_db_creates_base_tables(self) -> None:
        billing.init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        self.assertGreaterEqual(
            tables,
            {"users", "accounts", "ledger", "orders", "refund_allocations"},
        )


if __name__ == "__main__":
    unittest.main()
