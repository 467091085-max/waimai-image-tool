from __future__ import annotations

import background_profiles
from matching_engine import TAXONOMY_RULES


def menu_item(
    name: str,
    taxonomy: str,
    *,
    kind: str = "单品",
    components: list[str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "category": "",
        "taxonomy": taxonomy,
        "kind": kind,
        "components": components or [],
    }


def test_all_40_taxonomies_have_six_unique_background_prompts() -> None:
    taxonomy_ids = {taxonomy_id for taxonomy_id, _label, _words in TAXONOMY_RULES}

    assert len(taxonomy_ids) == 40
    assert set(background_profiles.BACKGROUND_SCENES) == taxonomy_ids
    for taxonomy_id in sorted(taxonomy_ids):
        prompts = [
            background_profiles.pure_background_prompt(taxonomy_id, style_id)
            for style_id in background_profiles.STYLE_IDS
        ]
        assert len(prompts) == 6
        assert len(set(prompts)) == 6
        assert all(prompt.startswith("纯背景场景商业摄影") for prompt in prompts)
        assert all("EMPTY SET ONLY" in prompt for prompt in prompts)
        assert all("PROPS, PODIUMS OR PLINTHS" in prompt for prompt in prompts)
        assert all("禁止菜品、饮料、果蔬、植物、花、布料" in prompt for prompt in prompts)
        assert all("中央、边缘、前景和后景全部无物" in prompt for prompt in prompts)
        assert all("中央下半部承载安全区至少占画面60%宽" in prompt for prompt in prompts)
        assert all("50%高" in prompt for prompt in prompts)
        assert all("镜头约25度轻俯视" in prompt for prompt in prompts)
        assert all("不能出现无来源阴影" in prompt for prompt in prompts)
        assert all("展示台、底座、台座、方台、圆台、台阶" in prompt for prompt in prompts)
        assert all("表面铺满画幅并延伸到边缘" in prompt for prompt in prompts)
        assert all("适合大浅碗轮廓" not in prompt for prompt in prompts)
        assert all("餐盒轮廓" not in prompt for prompt in prompts)
        assert all("轻食沙拉场景" not in prompt for prompt in prompts)
        assert all("轻食/沙拉商品" not in prompt for prompt in prompts)
        assert all("点缀" not in prompt for prompt in prompts)
        assert all(len(prompt) <= 520 for prompt in prompts)


def test_two_slots_are_seamless_and_four_slots_are_edge_to_edge_tables() -> None:
    seamless = background_profiles.pure_background_prompt(
        "light_food",
        "style-1",
    )
    prompt = background_profiles.pure_background_prompt(
        "light_food",
        "style-3",
    )

    assert "暖色单色无缝摄影棚弧面" in seamless
    assert "平整浅色石材桌面" in prompt
    assert "从左右与下边缘连续铺满" in prompt


def test_menu_context_prefers_explicit_store_category_over_side_dishes() -> None:
    menu = {
        "store": "熊猫说烧烤",
        "file": "烧烤菜单.xlsx",
        "items": [
            *[
                menu_item(f"烤生蚝{index}", "fish_seafood")
                for index in range(12)
            ],
            menu_item("招牌牛肉串", "barbecue"),
        ],
    }

    context = background_profiles.menu_background_context(menu)

    assert context["taxonomyId"] == "barbecue"
    assert context["category"] == "烧烤"
    assert context["selectionReason"] == "store_taxonomy"
    assert context["confidence"] >= 78


def test_store_name_does_not_override_a_conflicting_menu_without_support() -> None:
    context = background_profiles.menu_background_context(
        {
            "store": "咖啡故事餐厅",
            "file": "午餐菜单.xlsx",
            "items": [
                menu_item("鸡胸能量碗", "light_food"),
                menu_item("牛肉沙拉", "light_food"),
                menu_item("鲜虾藜麦沙拉", "light_food"),
                menu_item("低脂谷物碗", "light_food"),
            ],
        }
    )

    assert context["taxonomyId"] == background_profiles.MIXED_CATEGORY_ID
    assert context["selectionReason"] == "insufficient_or_conflicting_evidence"
    assert context["storeTaxonomyId"] == "coffee_cocoa"
    assert context["fileTaxonomyId"] == ""


def test_menu_context_uses_dish_and_combo_component_evidence() -> None:
    menu = {
        "store": "全时段餐厅",
        "file": "菜单.xlsx",
        "items": [
            menu_item("柳州螺蛳粉", "rice_noodles"),
            menu_item("酸辣粉套餐", "combo", kind="套餐/组合", components=["酸辣粉", "可乐"]),
            menu_item("桂林米粉", "rice_noodles"),
        ],
    }

    context = background_profiles.menu_background_context(menu)

    assert context["taxonomyId"] == "rice_noodles"
    assert context["selectionReason"] == "menu_taxonomy_evidence"
    assert context["knownSignalCount"] >= 3
    assert context["candidates"][0]["taxonomyId"] == "rice_noodles"


def test_menu_context_fails_truthfully_to_mixed_without_evidence() -> None:
    context = background_profiles.menu_background_context(
        {
            "store": "示例门店",
            "file": "menu.xlsx",
            "items": [menu_item("今日特供", "unknown")],
        }
    )

    assert context["taxonomyId"] == background_profiles.MIXED_CATEGORY_ID
    assert context["category"] == "复合餐饮"
    assert context["confidence"] == 35
    assert context["selectionReason"] == "insufficient_evidence"


def test_reserved_marketing_phrases_do_not_create_menu_category_evidence() -> None:
    context = background_profiles.menu_background_context(
        {
            "store": "示例门店",
            "file": "menu.xlsx",
            "items": [
                menu_item("【粉丝福利】今日特供", "unknown"),
                menu_item("火锅味微辣", "unknown"),
            ],
        }
    )

    assert context["taxonomyId"] == background_profiles.MIXED_CATEGORY_ID


def test_balanced_menu_categories_require_review_instead_of_guessing() -> None:
    context = background_profiles.menu_background_context(
        {
            "store": "综合餐厅",
            "file": "menu.xlsx",
            "items": [
                menu_item("鸡胸能量碗", "light_food"),
                menu_item("牛肉沙拉", "light_food"),
                menu_item("黑椒牛排", "pasta_steak"),
                menu_item("奶油意面", "pasta_steak"),
            ],
        }
    )

    assert context["taxonomyId"] == background_profiles.MIXED_CATEGORY_ID
    assert context["selectionReason"] == "insufficient_or_conflicting_evidence"
    assert context["confidence"] == 35
    assert len(context["candidates"]) >= 2


def test_profile_lookup_accepts_taxonomy_label() -> None:
    assert background_profiles.normalize_category_id("轻食/沙拉") == "light_food"
    assert background_profiles.profile_keywords("light_food")
    assert (
        background_profiles.style_prompt("轻食/沙拉", "style-2")
        == background_profiles.style_prompt("light_food", "style-2")
    )
