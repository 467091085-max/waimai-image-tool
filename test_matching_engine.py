from __future__ import annotations

import unittest

from matching_engine import (
    SAMPLE_LIBRARY_RECORDS,
    SAMPLE_MENU_ITEMS,
    classify_kind,
    match_summary,
    match_menu_to_library,
    normalize_dish,
    run_builtin_selftest,
    semantic_family,
    similarity,
    split_components,
    strict_match_allowed,
    style_coverage,
)


class MatchingEngineBuiltinTest(unittest.TestCase):
    def test_normalize_split_and_classify(self) -> None:
        self.assertEqual(normalize_dish("【热销】老长沙辣椒炒肉盖码饭"), "辣椒炒肉")
        self.assertEqual(normalize_dish("农家小炒肉饭"), "辣椒炒肉")
        self.assertEqual(normalize_dish("【美团热销】手打金桔柠檬水(大杯)"), "金桔柠檬水")
        self.assertEqual(normalize_dish("爆炒黄牛肉"), "小炒黄牛肉")
        self.assertEqual(normalize_dish("西红柿炒鸡蛋"), "番茄炒蛋")
        self.assertEqual(normalize_dish("北京炒合菜提示勿点"), "")
        self.assertEqual(split_components("辣椒炒肉+茄子肉末盖码饭"), ["辣椒炒肉", "茄子肉末"])
        self.assertEqual(classify_kind("辣椒炒肉+茄子肉末盖码饭"), "套餐/组合")
        self.assertEqual(classify_kind("双人餐含辣椒炒肉米饭"), "套餐/组合")
        self.assertEqual(classify_kind("康师傅冰红茶"), "饮品/小食")
        self.assertEqual(classify_kind("手打金桔柠檬水"), "饮品/小食")
        self.assertEqual(classify_kind("香干炒肉盖码饭"), "单品")
        self.assertEqual(classify_kind("一碗米饭"), "饮品/小食")
        self.assertEqual(classify_kind("餐具"), "其他")
        self.assertEqual(semantic_family("一碗米饭", normalize_dish("一碗米饭")), "plain_rice")
        self.assertEqual(semantic_family("经典螺蛳粉", normalize_dish("经典螺蛳粉")), "rice_noodle")
        self.assertEqual(split_components("辣椒炒肉+米饭+餐具+茄子肉末套餐"), ["辣椒炒肉", "茄子肉末"])
        self.assertEqual(split_components("双人餐含辣椒炒肉+米饭"), ["辣椒炒肉"])

    def test_similarity_scores_aliases_higher_than_unrelated_dishes(self) -> None:
        alias_score = similarity("老长沙辣椒炒肉盖码饭", "辣椒小炒肉盖饭")
        unrelated_score = similarity("老长沙辣椒炒肉盖码饭", "紫菜蛋花汤")
        self.assertGreater(alias_score, 0.75)
        self.assertLess(unrelated_score, 0.35)
        self.assertEqual(similarity("小炒黄牛肉", "爆炒黄牛肉"), 1.0)
        self.assertEqual(similarity("番茄炒蛋", "西红柿炒鸡蛋"), 1.0)

    def test_strict_matching_rejects_high_risk_hard_negatives(self) -> None:
        records = [
            {"imageId": "drink", "dishName": "金桔柠檬水", "styleId": "style-1", "source": "sample"},
            {"imageId": "meat", "dishName": "辣椒炒肉", "styleId": "style-1", "source": "sample"},
            {"imageId": "rice", "dishName": "一碗米饭", "styleId": "style-1", "source": "sample"},
            {"imageId": "prompt", "dishName": "北京炒合菜提示勿点", "styleId": "style-1", "source": "sample"},
            {"imageId": "beijing", "dishName": "北京炒合菜", "styleId": "style-1", "source": "sample"},
            {"imageId": "noodle", "dishName": "柳州螺蛳粉", "styleId": "style-1", "source": "sample"},
            {"imageId": "rice-noodle-fallback", "dishName": "桂林米粉", "styleId": "style-1", "source": "sample"},
        ]
        items = [
            {"row": 1, "name": "手打金桔柠檬水"},
            {"row": 2, "name": "火爆双脆"},
            {"row": 3, "name": "北京炒合菜"},
            {"row": 4, "name": "经典螺蛳粉"},
        ]

        results = match_menu_to_library(items, records, selected_style="style-1")
        by_name = {row["name"]: row for row in results}

        self.assertEqual([c["dishName"] for c in by_name["手打金桔柠檬水"]["candidates"]], ["金桔柠檬水"])
        self.assertEqual(by_name["火爆双脆"]["candidates"], [])
        self.assertEqual(by_name["火爆双脆"]["matchStatus"], "no_match")
        self.assertTrue(by_name["火爆双脆"]["needsAi"])
        self.assertEqual([c["dishName"] for c in by_name["北京炒合菜"]["candidates"]], ["北京炒合菜"])
        self.assertEqual(by_name["经典螺蛳粉"]["candidates"][0]["dishName"], "柳州螺蛳粉")
        self.assertNotIn("一碗米饭", [c["dishName"] for c in by_name["经典螺蛳粉"]["candidates"]])

        blocked_pairs = [
            ("金桔柠檬水", "辣椒炒肉"),
            ("金桔柠檬水", "一碗米饭"),
            ("火爆双脆", "一碗米饭"),
            ("北京炒合菜", "金桔柠檬水"),
            ("北京炒合菜", "北京炒合菜提示勿点"),
            ("北京炒合菜", "一碗米饭"),
            ("螺蛳粉", "一碗米饭"),
            ("餐具", "辣椒炒肉"),
            ("加鸡蛋", "辣椒炒肉"),
        ]
        for menu_name, image_name in blocked_pairs:
            menu_norm = normalize_dish(menu_name)
            image_norm = normalize_dish(image_name)
            score = similarity(menu_name, image_name, menu_norm, image_norm)
            self.assertFalse(strict_match_allowed(menu_name, image_name, menu_norm, image_norm, score))

    def test_named_mismatch_regressions_return_unmatched_for_generation(self) -> None:
        records = [
            {"imageId": "fries", "dishName": "薯条", "styleId": "style-1", "source": "sample"},
            {"imageId": "rice", "dishName": "一碗米饭", "styleId": "style-1", "source": "sample"},
            {"imageId": "meat", "dishName": "辣椒炒肉", "styleId": "style-1", "source": "sample"},
        ]
        items = [
            {"row": 1, "name": "手打金桔柠檬水"},
            {"row": 2, "name": "北京炒合菜"},
            {"row": 3, "name": "云端秘制新菜"},
        ]

        results = match_menu_to_library(items, records, selected_style="style-1")
        by_name = {row["name"]: row for row in results}

        self.assertEqual(by_name["手打金桔柠檬水"]["candidates"], [])
        self.assertEqual(by_name["北京炒合菜"]["candidates"], [])
        self.assertEqual(by_name["云端秘制新菜"]["candidates"], [])
        for row in results:
            self.assertEqual(row["matchStatus"], "no_match")
            self.assertTrue(row["needs_generation"])
            self.assertTrue(row["needsGeneration"])
            self.assertEqual(row["match_reason"], "unmatched")
            self.assertEqual(row["candidate_id"], "")

    def test_combo_dish_match_does_not_become_single_dish_match(self) -> None:
        records = [
            {"imageId": "single", "dishName": "辣椒炒肉", "styleId": "style-1", "source": "sample"},
            {"imageId": "combo", "dishName": "辣椒炒肉+茄子肉末套餐", "styleId": "style-1", "source": "sample"},
        ]
        items = [{"name": "辣椒炒肉+茄子肉末盖码饭"}, {"name": "辣椒炒肉"}]

        results = match_menu_to_library(items, records, selected_style="style-1")
        combo = results[0]
        single = results[1]

        self.assertEqual(combo["candidates"][0]["dishName"], "辣椒炒肉+茄子肉末套餐")
        self.assertEqual(combo["candidates"][0]["matchType"], "dish")
        self.assertNotIn("辣椒炒肉", [c["dishName"] for c in combo["candidates"]])
        self.assertIn("辣椒炒肉", [c["dishName"] for m in combo["componentMatches"] for c in m["candidates"]])
        self.assertEqual([c["dishName"] for c in single["candidates"]], ["辣椒炒肉"])

    def test_combo_with_only_single_components_stays_generation_needed(self) -> None:
        records = [
            {"imageId": "pork", "dishName": "辣椒炒肉", "styleId": "style-1", "source": "sample"},
            {"imageId": "egg", "dishName": "番茄炒蛋", "styleId": "style-1", "source": "sample"},
        ]
        items = [{"name": "辣椒炒肉+番茄炒蛋套餐"}]

        result = match_menu_to_library(items, records, selected_style="style-1")[0]

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["matchStatus"], "no_match")
        self.assertTrue(result["needsGeneration"])
        self.assertEqual([m["name"] for m in result["componentMatches"]], ["辣椒炒肉", "番茄炒蛋"])
        self.assertTrue(all(match["candidates"] for match in result["componentMatches"]))

    def test_generic_or_cross_family_matches_are_rejected(self) -> None:
        pairs = [
            ("米粉", "桂林米粉"),
            ("经典米线", "桂林米粉"),
            ("北京炒合菜", "薯条"),
            ("手打金桔柠檬水", "鸡米花"),
            ("番茄炒蛋", "紫菜蛋花汤"),
        ]
        for menu_name, image_name in pairs:
            menu_norm = normalize_dish(menu_name)
            image_norm = normalize_dish(image_name)
            score = similarity(menu_name, image_name, menu_norm, image_norm)
            self.assertFalse(strict_match_allowed(menu_name, image_name, menu_norm, image_norm, score), (menu_name, image_name, score))

    def test_builtin_match_outputs_candidates_components_and_style_source(self) -> None:
        results = match_menu_to_library(SAMPLE_MENU_ITEMS, SAMPLE_LIBRARY_RECORDS, selected_style="style-1")
        self.assertEqual(len(results), len(SAMPLE_MENU_ITEMS))
        first = results[0]
        self.assertEqual(first["backgroundAction"], "背景一致，直接复用")
        self.assertEqual(first["candidates"][0]["styleId"], "style-1")
        self.assertEqual(first["candidates"][0]["source"], "sample")
        self.assertGreaterEqual(first["confidence"], 70)
        self.assertEqual(first["candidate_id"], first["candidates"][0]["candidate_id"])
        self.assertIn(first["match_reason"], {"exact_normalized", "alias_canonical", "strong_token"})
        self.assertGreaterEqual(
            first["candidates"][0].keys(),
            {"confidence", "match_reason", "candidate_id", "matchReason"},
        )

        combo = next(row for row in results if row["kind"] == "套餐/组合")
        self.assertGreaterEqual(len(combo["componentMatches"]), 2)
        self.assertTrue(all(component["candidates"] for component in combo["componentMatches"][:2]))

        coverage = style_coverage(results)
        self.assertTrue(any(style["styleId"] == "style-1" and style["direct"] >= 2 for style in coverage))

        summary = match_summary(results, points_per_image=100)
        self.assertEqual(summary["singleImages"], 1)
        self.assertEqual(summary["packageImages"], 1)
        self.assertEqual(summary["snackDrinkImages"], 1)
        self.assertEqual(summary["formalImages"], 3)
        self.assertEqual(summary["estimatedPoints"], 300)

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
        self.assertTrue(all(row["candidates"] for row in results if row["kind"] != "套餐/组合"))

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
