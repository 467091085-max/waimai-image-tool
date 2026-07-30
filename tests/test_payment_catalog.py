from __future__ import annotations

import unittest

from shared import payment_catalog


class PaymentCatalogTests(unittest.TestCase):
    def test_catalog_matches_existing_recharge_packages(self) -> None:
        self.assertEqual(
            payment_catalog.legacy_cash_packages(),
            {49: 500, 99: 1040, 299: 3190},
        )
        self.assertEqual(
            [item["packageId"] for item in payment_catalog.catalog_snapshot()["packages"]],
            ["starter-500", "store-1040", "team-3190"],
        )

    def test_unknown_package_fails_closed(self) -> None:
        with self.assertRaises(payment_catalog.UnknownPaymentPackage):
            payment_catalog.package_snapshot("not-a-package")

    def test_client_request_accepts_only_package_id(self) -> None:
        package = payment_catalog.resolve_client_package({"packageId": "starter-500"})

        self.assertEqual(package["amountCents"], 4900)
        self.assertEqual(package["points"], 500)
        for field, value in (
            ("amountCents", 1),
            ("points", 999999),
            ("userId", "victim"),
            ("currency", "USD"),
            ("catalogVersion", "attacker-v1"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(payment_catalog.InvalidPaymentCatalogRequest):
                    payment_catalog.resolve_client_package(
                        {"packageId": "starter-500", field: value}
                    )

    def test_snapshot_digest_is_stable_and_covers_authoritative_fields(self) -> None:
        first = payment_catalog.package_snapshot("starter-500")
        second = payment_catalog.package_snapshot("starter-500")

        self.assertEqual(first, second)
        self.assertEqual(
            payment_catalog.catalog_snapshot()["catalogDigest"],
            "556190573e3474f07d47d799f478e1b51effe6ffecc66d1bd6ac6d5e19281930",
        )
        self.assertEqual(
            first["snapshotDigest"],
            "27dfaedd0a4016453965b6294751683f1747077010dcbd126fc46f097f3ad979",
        )
        self.assertEqual(
            first["snapshotDigest"],
            payment_catalog.stable_snapshot_digest(
                {key: value for key, value in first.items() if key != "snapshotDigest"}
            ),
        )
        changed = {**first, "points": 501}
        changed.pop("snapshotDigest")
        self.assertNotEqual(
            first["snapshotDigest"],
            payment_catalog.stable_snapshot_digest(changed),
        )


if __name__ == "__main__":
    unittest.main()
