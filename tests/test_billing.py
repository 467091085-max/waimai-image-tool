from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import billing


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

    def test_init_db_creates_base_tables(self) -> None:
        billing.init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        self.assertGreaterEqual(tables, {"users", "accounts", "ledger", "orders"})


if __name__ == "__main__":
    unittest.main()
