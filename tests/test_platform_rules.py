from __future__ import annotations

import unittest

import platform_rules as rules


class PlatformRulesTests(unittest.TestCase):
    def test_platform_dimensions_and_limits(self) -> None:
        meituan = rules.get_platform_rule("meituan")
        taobao = rules.get_platform_rule("taobao")
        jd = rules.get_platform_rule("jd")

        self.assertEqual(
            (meituan["width"], meituan["height"], meituan["max_mb"], meituan["max_kb"]),
            (800, 600, 5, 5120),
        )
        self.assertEqual(
            (taobao["width"], taobao["height"], taobao["max_mb"], taobao["max_kb"]),
            (800, 800, 20, 20480),
        )
        self.assertEqual(
            (jd["width"], jd["height"], jd["max_mb"], jd["max_kb"]),
            (800, 800, 5, 5120),
        )

        self.assertEqual(
            [rule["platform_id"] for rule in rules.list_platform_rules()],
            ["meituan", "taobao", "jd"],
        )

    def test_validate_platform_selection_deduplicates_preserving_order(self) -> None:
        selected = rules.validate_platform_selection(
            ["taobao", "meituan", "taobao", "jd", "meituan"]
        )

        self.assertEqual(selected, ["taobao", "meituan", "jd"])

    def test_validate_platform_selection_rejects_unknown_platform(self) -> None:
        with self.assertRaises(ValueError):
            rules.validate_platform_selection(["meituan", "pdd"])

        with self.assertRaises(ValueError):
            rules.get_platform_rule("pdd")

    def test_validate_platform_selection_rejects_empty_selection(self) -> None:
        with self.assertRaises(ValueError):
            rules.validate_platform_selection([])

        with self.assertRaises(ValueError):
            rules.validate_platform_selection(["", "  "])

    def test_platform_charge_points_first_platform_free_and_extras_charged(self) -> None:
        self.assertEqual(rules.platform_charge_points(["meituan"]), 0)
        self.assertEqual(rules.platform_charge_points(["meituan", "taobao"]), 100)
        self.assertEqual(rules.platform_charge_points(["meituan", "taobao", "jd"]), 200)
        self.assertEqual(
            rules.platform_charge_points(["meituan", "taobao", "taobao", "jd"]),
            200,
        )
        self.assertEqual(
            rules.platform_charge_points(["meituan", "taobao", "jd"], extra_platform_points=50),
            100,
        )


if __name__ == "__main__":
    unittest.main()
