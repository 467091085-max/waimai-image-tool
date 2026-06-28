from __future__ import annotations

import unittest

from matching_engine import (
    SAMPLE_LIBRARY_RECORDS,
    SAMPLE_MENU_ITEMS,
    classify_kind,
    match_menu_to_library,
    normalize_dish,
    run_builtin_selftest,
    similarity,
    split_components,
    style_coverage,
)


class MatchingEngineBuiltinTest(unittest.TestCase):
    def test_normalize_split_and_classify(self) -> None:
        self.assertEqual(normalize_dish("【热销】老长沙辣椒炒肉盖码饭"), "辣椒炒肉")
        self.assertEqual(split_components("辣椒炒肉+茄子肉末盖码饭"), ["辣椒炒肉", "茄子肉末"])
        self.assertEqual(classify_kind("辣椒炒肉+茄子肉末盖码饭"), "套餐/组合")
        self.assertEqual(classify_kind("康师傅冰红茶"), "饮品/小食")
        self.assertEqual(classify_kind("香干炒肉盖码饭"), "单品")

    def test_similarity_scores_aliases_higher_than_unrelated_dishes(self) -> None:
        alias_score = similarity("老长沙辣椒炒肉盖码饭", "辣椒小炒肉盖饭")
        unrelated_score = similarity("老长沙辣椒炒肉盖码饭", "紫菜蛋花汤")
        self.assertGreater(alias_score, 0.75)
        self.assertLess(unrelated_score, 0.35)

    def test_builtin_match_outputs_candidates_components_and_style_source(self) -> None:
        results = match_menu_to_library(SAMPLE_MENU_ITEMS, SAMPLE_LIBRARY_RECORDS, selected_style="style-1")
        self.assertEqual(len(results), len(SAMPLE_MENU_ITEMS))
        first = results[0]
        self.assertEqual(first["backgroundAction"], "背景一致，直接复用")
        self.assertEqual(first["candidates"][0]["styleId"], "style-1")
        self.assertEqual(first["candidates"][0]["source"], "sample")

        combo = next(row for row in results if row["kind"] == "套餐/组合")
        self.assertGreaterEqual(len(combo["componentMatches"]), 2)
        self.assertTrue(all(component["candidates"] for component in combo["componentMatches"][:2]))

        coverage = style_coverage(results)
        self.assertTrue(any(style["styleId"] == "style-1" and style["direct"] >= 2 for style in coverage))

    def test_module_selftest(self) -> None:
        self.assertTrue(run_builtin_selftest()["ok"])


class MatchingEngineDemoLibraryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app import demo_menu_items, ensure_demo_data, library_images

        ensure_demo_data()
        cls.items = demo_menu_items()
        cls.records = library_images()

    def test_demo_library_self_check(self) -> None:
        self.assertGreaterEqual(len(self.records), 20)
        results = match_menu_to_library(self.items, self.records, selected_style="style-1")
        self.assertEqual(len(results), len(self.items))
        self.assertTrue(all(row["candidates"] for row in results))

        combo = next(row for row in results if row["name"] == "辣椒炒肉+茄子肉末盖码饭")
        component_names = [component["name"] for component in combo["componentMatches"]]
        self.assertIn("辣椒炒肉", component_names)
        self.assertIn("茄子肉末", component_names)
        self.assertTrue(all(component["candidates"] for component in combo["componentMatches"]))

        coverage = style_coverage(results)
        style_1 = next(style for style in coverage if style["styleId"] == "style-1")
        self.assertGreater(style_1["direct"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
