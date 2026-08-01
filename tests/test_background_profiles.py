from __future__ import annotations

import hashlib

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
        assert all(prompt.startswith("真实空景摄影") for prompt in prompts)
        assert all("EMPTY SET ONLY" in prompt for prompt in prompts)
        assert all(
            "PODIUMS, PLINTHS, RISERS OR DISPLAY SURFACES" in prompt
            for prompt in prompts
        )
        assert all("禁止菜品、饮料、食材、植物、布料" in prompt for prompt in prompts)
        assert all("中央、边缘、前景和后景全部无物" in prompt for prompt in prompts)
        assert all("中央约60%区域只保持连续材质" in prompt for prompt in prompts)
        assert all("承载安全区" not in prompt for prompt in prompts)
        assert all("镜头约25度轻俯视" in prompt for prompt in prompts)
        assert all("不能出现无来源阴影" in prompt for prompt in prompts)
        assert all("展示台、台座、垫板" in prompt for prompt in prompts)
        assert all("不能为了放商品而创造任何矩形" in prompt for prompt in prompts)
        assert all("适合大浅碗轮廓" not in prompt for prompt in prompts)
        assert all("餐盒轮廓" not in prompt for prompt in prompts)
        assert all("轻食沙拉场景" not in prompt for prompt in prompts)
        assert all("轻食/沙拉商品" not in prompt for prompt in prompts)
        assert all("点缀" not in prompt for prompt in prompts)
        assert all(len(prompt) <= 620 for prompt in prompts)


def test_two_slots_are_seamless_and_four_slots_are_edge_to_edge_tables() -> None:
    seamless = background_profiles.pure_background_prompt(
        "light_food",
        "style-1",
    )
    prompt = background_profiles.pure_background_prompt(
        "light_food",
        "style-3",
    )

    assert "仅以鼠尾草绿为唯一主色" in seamless
    assert "哑光微水泥无缝空间" in seamless
    assert "影棚纸" not in seamless
    assert "不得创建纸卷、幕布、水平矩形" in seamless
    assert "一张普通浅色石材餐桌的连续桌面" in prompt
    assert "从左右与下边缘铺满" in prompt
    assert "桌面前沿和厚度位于画幅下方不可见" in prompt


def test_unapproved_v11_profiles_forbid_multicolor_table_surfaces() -> None:
    table = background_profiles.pure_background_prompt(
        "rice_noodles",
        "style-3",
    )
    porridge_cool = background_profiles.pure_background_prompt(
        "porridge_soup_rice",
        "style-2",
    )
    wheat_cool = background_profiles.pure_background_prompt(
        "wheat_noodles",
        "style-2",
    )

    assert "ONE MATERIAL, ONE COLOR TABLETOP" in table
    assert "桌面禁止拼色、拼花、镶嵌、分区或混合材质" in table
    assert "暖白、陶土红与青灰配色" not in table
    assert "仅以燕麦色为唯一主色" in porridge_cool
    assert "仅以暖灰为唯一主色" in wheat_cool


def test_unapproved_cool_solid_uses_one_hue_on_wall_curve_and_floor() -> None:
    for category_id, color in (
        ("fried_chicken", "番茄红"),
        ("burger_hotdog", "芥末黄"),
        ("pizza", "奶油白"),
    ):
        prompt = background_profiles.pure_background_prompt(
            category_id,
            "style-2",
        )
        assert f"墙面、建筑圆弧和地面必须全部使用{color}同一色相" in prompt
        assert "地面不得变成另一色相" in prompt
        assert "禁止任何第二色地面" in prompt
        assert "单一低饱和辅色" not in prompt


def test_approved_v11_prompt_hashes_remain_frozen() -> None:
    expected = {
        "light_food": {
            "style-1": "c149a36abed30e65ba945835b50289ae3f0551dd5a36aa5a4550feb5d4248d20",
            "style-2": "5f2410da9c9bbc5038e8235808a2b84b3891f16f5ae96b046e1d7e3fedfa23c6",
            "style-3": "55f07a690850dbc0c108045a9a36f67bca40a7b9e53027b5897a6303470e1b82",
            "style-4": "5301e218b432e985539ce8e9412440e90db73745aa3525c071730f971a718487",
            "style-5": "04d909cc3c24e95b389eeaa1af9b27261e2c18928dff6d14a6d51b87c8cf8d2c",
            "style-6": "ddc68a34f450cf5e1eb6d0960ce6491debdb9cab59f5eb059ddace5dc98696e7",
        },
        "topped_rice": {
            "style-1": "fb9bc0f51542c92433dd1731daab1c72926bb5cae0889b18db7572742dbdb5a6",
            "style-2": "ff5a57e325a0338693863ae2879b1b11cef7e81fb62f271cd7a2ac761a7fd222",
            "style-3": "2ae04993aa9d8356f9cdcf95b53cdf768b99e97ab7f867731bea3f399c94a5a7",
            "style-4": "662f10e4d062a62d1ec13d6a788c065424ea3d993c12cff3fbbf4739e19c57d5",
            "style-5": "f11c77f1846f8088fb7d7fdf9ac11261a5c88fd4425e0579b7a8368d834dd280",
            "style-6": "0d56d547410e95035fec2fa9285f86aba9885d26f56f4a4ea49a7db3bf171369",
        },
        "mixed_rice": {
            "style-1": "1508c2eb9ff731d3c6db825777a34f7c7ec5541d90e385c63cc96bf82bb64dc5",
            "style-2": "83bf46169926262830d4d25fe9df9a91ee073de5fd0f9bffd5ba1b926c7dc3b4",
            "style-3": "aaa6091df4665ea748c3c7db2b541fbdcfd1f946505d3315d06ccfe2215928c3",
            "style-4": "1e1fb1f046fdd2aa23ab6db302811e5eb6df9ec14bdd56b2bd42c1ce7489924e",
            "style-5": "a80c5ead30c1d221072b2957351fd267ff612606d8b1f3fcd5736054b5430fdd",
            "style-6": "5223b7a7adde018432dd8671a6000321834f802fa578c445e0cc40b413a091b0",
        },
    }

    assert background_profiles.FROZEN_V11_PROMPT_CATEGORIES == set(expected)
    for category_id, hashes in expected.items():
        for style_id, expected_hash in hashes.items():
            prompt = background_profiles.pure_background_prompt(
                category_id,
                style_id,
            )
            assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == expected_hash


def test_approved_profile_v10_cool_prompt_hashes_remain_frozen() -> None:
    expected = {
        "porridge_soup_rice": "3d511d95fed044b4ec442d2b28e1c64ac271afdad518f2caac044fe88dc519bb",
        "rice_noodles": "31b6486bb0a56acdf55ee4074e612a267050fa728b746d0c488235d20bfd9efb",
        "wheat_noodles": "423d0bab42edd75194e3c7a7b0431d9d9394145a49f6f59f73ffc3ce146117d9",
        "dumpling_wonton": "92acd2dd28cebe9b464bc437c240ce0748cc06003e42ec626ce1f3d2fc584551",
        "buns_dim_sum": "f1158dfafd1ccfd34d5f8fb615ddba1c9d06171ca8116e6c53a9d495fdb326f1",
        "chinese_wraps": "567dd68a1bc610907f180721002a3d48046a9799ec5ba180b881f659b382ad00",
        "malatang_maocai": "1063192026dcf27e9b5af51d8faaffcef09763da33d78821f4a2955504530152",
        "hotpot_skewers": "7e0324a4536cedd039ea2b9483a904dc8e3d5ef7e282a8b336ca2b9067919e27",
        "barbecue": "fc8e6c0bc0660b21e445c3d0b24759a682c6fcb2c47105165e4ce0684981fc71",
    }

    assert (
        background_profiles.FROZEN_PROFILE_V10_PROMPT_CATEGORIES
        == set(expected)
    )
    assert background_profiles.HASH_LOCKED_PROMPT_CATEGORIES == (
        background_profiles.FROZEN_V11_PROMPT_CATEGORIES
        | set(expected)
    )
    for category_id, expected_hash in expected.items():
        prompt = background_profiles.pure_background_prompt(
            category_id,
            "style-2",
        )
        assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == expected_hash


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
