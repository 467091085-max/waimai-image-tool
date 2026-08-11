from __future__ import annotations

import unittest

from matching_engine import (
    TAXONOMY_RULES,
    TAXONOMY_VERSION,
    classify_kind,
    classify_taxonomy,
    match_menu_to_library,
    normalize_dish,
    similarity,
    split_components,
)
from menu_parser import detect_kind as parser_detect_kind
from menu_parser import split_components as parser_split_components


class MenuTaxonomyTests(unittest.TestCase):
    def test_taxonomy_has_40_stable_leaf_categories(self) -> None:
        taxonomy_ids = [taxonomy_id for taxonomy_id, _label, _keywords in TAXONOMY_RULES]

        self.assertEqual(len(taxonomy_ids), 40)
        self.assertEqual(len(set(taxonomy_ids)), 40)
        self.assertEqual(TAXONOMY_VERSION, "2026-07-30.v2")

    def test_main_food_shapes_and_customer_categories_are_explicit(self) -> None:
        cases = {
            "土豆牛腩盖饭": ("topped_rice", "单品"),
            "兰州牛肉拉面": ("wheat_noodles", "单品"),
            "柳州螺蛳粉": ("rice_noodles", "单品"),
            "皮蛋瘦肉粥": ("porridge_soup_rice", "单品"),
            "香酥薯条": ("fried_snacks", "饮品/小食"),
            "冰镇可乐": ("bottled_drinks", "饮品/小食"),
            "炭烤牛肉串": ("barbecue", "单品"),
            "重庆牛油小火锅": ("hotpot_skewers", "单品"),
            "牛肉面+可乐套餐": ("combo", "套餐/组合"),
        }

        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual((classify_taxonomy(name), classify_kind(name)), expected)

    def test_common_aliases_share_a_canonical_name(self) -> None:
        self.assertEqual(normalize_dish("西红柿炒鸡蛋"), "番茄炒蛋")
        self.assertEqual(normalize_dish("番茄炒蛋"), "番茄炒蛋")
        self.assertEqual(normalize_dish("宫爆鸡丁"), normalize_dish("宫保鸡丁"))
        self.assertEqual(normalize_dish("肉沫茄子"), normalize_dish("肉末茄子"))

    def test_staple_shape_prevents_rice_and_noodle_category_collision(self) -> None:
        self.assertEqual(classify_taxonomy("番茄牛腩饭"), "topped_rice")
        self.assertEqual(classify_taxonomy("番茄牛腩面"), "wheat_noodles")
        self.assertEqual(classify_taxonomy("酸辣粉"), "rice_noodles")
        self.assertEqual(classify_kind("牛肉汤面"), "单品")
        self.assertEqual(classify_taxonomy("未收录创意菜"), "unknown")

    def test_marketing_flavor_and_longer_ingredient_words_do_not_hijack_shape(self) -> None:
        self.assertEqual(classify_taxonomy("【粉丝福利】烤翅一对"), "unknown")
        self.assertEqual(classify_taxonomy("火锅味微辣"), "unknown")
        self.assertEqual(classify_taxonomy("酥炸牛肉锅贴"), "dumpling_wonton")


class ComboRecognitionTests(unittest.TestCase):
    def test_explicit_combo_components_are_preserved(self) -> None:
        cases = {
            "炸鸡+薯条+可乐三人套餐": ["炸鸡", "薯条", "可乐"],
            "鸡腿堡套餐（含薯条、可乐）": ["鸡腿堡", "薯条", "可乐"],
            "牛排&鸡胸双拼能量碗": ["牛排", "鸡胸"],
            "鸡腿饭含饮料小食": ["鸡腿饭", "饮料", "小食"],
        }

        for name, expected_components in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_kind(name), "套餐/组合")
                self.assertEqual(classify_taxonomy(name), "combo")
                self.assertEqual(split_components(name), expected_components)
                self.assertEqual(parser_detect_kind(name), "套餐/组合")
                self.assertEqual(parser_split_components(name, ""), expected_components)

    def test_bracketed_combo_ingredients_are_not_dropped_or_malformed(self) -> None:
        name = "豪华三拼【烤肉+烤排+鸡排】+煎蛋/热狗肠/饮品三选一"
        expected = ["烤肉", "烤排", "鸡排", "煎蛋", "热狗肠", "饮品三选一"]

        self.assertEqual(classify_kind(name), "套餐/组合")
        self.assertEqual(split_components(name), expected)
        self.assertEqual(parser_split_components(name, ""), expected)

    def test_unresolved_multi_item_markers_still_force_combo_generation(self) -> None:
        for name in ("人气海陆空三拼烤时蔬健康碗", "招牌全家福", "家庭分享组合"):
            with self.subTest(name=name):
                self.assertEqual(classify_kind(name), "套餐/组合")
                self.assertEqual(classify_taxonomy(name), "combo")

    def test_flavor_or_sauce_choices_do_not_turn_a_single_dish_into_combo(self) -> None:
        attrs = "酱料自选#黑椒#番茄，口味二选一"

        self.assertEqual(classify_kind("意大利面", attrs), "单品")
        self.assertEqual(parser_detect_kind("意大利面", attrs, "意面"), "单品")

    def test_ambiguous_punctuation_and_ingredients_do_not_force_combo(self) -> None:
        cases = (
            "家常豆腐（豆腐+肉末）",
            "牛肉面/酸辣粉二选一",
            "招牌螺蛳粉丨虎皮鸡爪",
            "牛排&鸡胸全麦三明治",
            "加一份低卡泡菜(限一份,多点不送)",
        )

        for name in cases:
            with self.subTest(name=name):
                self.assertNotEqual(classify_kind(name), "套餐/组合")
                self.assertNotEqual(classify_taxonomy(name), "combo")

    def test_combo_section_is_supporting_evidence_not_a_blanket_override(self) -> None:
        self.assertEqual(classify_kind("韩式无骨炸鸡300克", category="超值套餐"), "单品")
        self.assertEqual(classify_kind("韩式无骨炸鸡+薯条", category="超值套餐"), "套餐/组合")
        self.assertEqual(classify_kind("牛肉面+酸辣粉二选一"), "单品")
        self.assertEqual(classify_kind("牛肉面+酸辣粉二选一", category="自选套餐"), "单品")
        self.assertEqual(classify_kind("主食自选+赠小食+赠饮品"), "套餐/组合")


class ConservativeMatchingTests(unittest.TestCase):
    def test_similarity_entrypoint_rejects_cross_taxonomy_customer_matches(self) -> None:
        self.assertEqual(similarity("番茄牛腩饭", "番茄牛腩面"), 0.0)
        self.assertEqual(similarity("香辣鸡腿堡", "香辣鸡腿饭"), 0.0)
        self.assertEqual(similarity("冰镇可乐", "香酥薯条"), 0.0)
        self.assertEqual(similarity("牛肉面+可乐套餐", "牛肉面"), 0.0)
        self.assertEqual(similarity("未收录创意菜", "另一道创意菜"), 0.0)
        self.assertGreater(similarity("西红柿炒鸡蛋", "番茄炒蛋"), 0.9)

    def test_aliases_match_but_easy_to_confuse_shapes_do_not(self) -> None:
        records = [
            {"imageId": "egg", "dishName": "西红柿炒鸡蛋", "styleId": "style-1"},
            {"imageId": "rice", "dishName": "番茄牛腩饭", "styleId": "style-1"},
            {"imageId": "noodle", "dishName": "番茄牛腩面", "styleId": "style-1"},
            {"imageId": "burger", "dishName": "香辣鸡腿堡", "styleId": "style-1"},
            {"imageId": "chicken-rice", "dishName": "香辣鸡腿饭", "styleId": "style-1"},
        ]

        rows = match_menu_to_library(
            [{"name": "番茄炒蛋"}, {"name": "番茄牛腩面"}, {"name": "香辣鸡腿堡"}],
            records,
        )

        self.assertEqual([candidate["dishName"] for candidate in rows[0]["candidates"]], ["西红柿炒鸡蛋"])
        self.assertEqual([candidate["dishName"] for candidate in rows[1]["candidates"]], ["番茄牛腩面"])
        self.assertEqual([candidate["dishName"] for candidate in rows[2]["candidates"]], ["香辣鸡腿堡"])

    def test_unknown_or_low_confidence_name_requires_generation(self) -> None:
        rows = match_menu_to_library(
            [{"name": "未收录创意菜"}],
            [{"imageId": "other", "dishName": "红烧茄子", "styleId": "style-1"}],
            min_score=0.01,
        )

        self.assertEqual(rows[0]["taxonomy"], "unknown")
        self.assertEqual(rows[0]["status"], "未找到")
        self.assertEqual(rows[0]["candidates"], [])
        self.assertEqual(rows[0]["backgroundAction"], "需要定制/生成")

    def test_combo_components_never_become_complete_product_candidates(self) -> None:
        menu_item = {"name": "炸鸡+薯条+可乐三人套餐"}
        single_records = [
            {"imageId": "chicken", "dishName": "炸鸡", "styleId": "style-1"},
            {"imageId": "fries", "dishName": "薯条", "styleId": "style-1"},
            {"imageId": "cola", "dishName": "可乐", "styleId": "style-1"},
            {"imageId": "incomplete", "dishName": "炸鸡+薯条套餐", "styleId": "style-1"},
        ]

        row = match_menu_to_library([menu_item], single_records)[0]

        self.assertEqual(row["kind"], "套餐/组合")
        self.assertEqual(row["candidates"], [])
        self.assertEqual(row["backgroundAction"], "需要定制/生成")
        self.assertEqual([match["name"] for match in row["componentMatches"]], ["炸鸡", "薯条", "可乐"])
        self.assertTrue(all(match["candidates"] for match in row["componentMatches"]))

        exact_combo = {
            "imageId": "exact-combo",
            "dishName": "可乐+炸鸡+薯条套餐",
            "styleId": "style-1",
        }
        exact_row = match_menu_to_library([menu_item], [*single_records, exact_combo])[0]
        self.assertEqual([candidate["imageId"] for candidate in exact_row["candidates"]], ["exact-combo"])
        self.assertEqual(exact_row["candidates"][0]["kind"], "套餐/组合")

    def test_combo_candidate_must_include_staple_and_drink_components(self) -> None:
        menu_item = {"name": "鱼香肉丝+麻婆豆腐+米饭+饮料任选套餐"}
        incomplete = {
            "imageId": "missing-extras",
            "dishName": "鱼香肉丝+麻婆豆腐套餐",
            "styleId": "style-1",
        }
        complete = {
            "imageId": "complete",
            "dishName": "饮料任选+麻婆豆腐+米饭+鱼香肉丝套餐",
            "styleId": "style-1",
        }

        incomplete_row = match_menu_to_library([menu_item], [incomplete])[0]
        complete_row = match_menu_to_library([menu_item], [incomplete, complete])[0]

        self.assertEqual(incomplete_row["candidates"], [])
        self.assertEqual(incomplete_row["backgroundAction"], "需要定制/生成")
        self.assertEqual([candidate["imageId"] for candidate in complete_row["candidates"]], ["complete"])


if __name__ == "__main__":
    unittest.main()
