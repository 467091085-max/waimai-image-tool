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
        | background_profiles.FROZEN_PROFILE_V11_SINGLE_HUE_PROMPT_CATEGORIES
    )
    for category_id, expected_hash in expected.items():
        prompt = background_profiles.pure_background_prompt(
            category_id,
            "style-2",
        )
        assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == expected_hash


def test_approved_profile_v11_single_hue_prompts_remain_frozen() -> None:
    expected = {
        "fried_chicken": {
            "style-1": "5368db1dd2b05384ed146a436f309f7aef51c9880b758155713886668716b109",
            "style-2": "465ea7c11d4d667ddd21be6f6814131fe747cdd7960eb0a030f932e90d95310b",
            "style-3": "cd95f65ed958e79a3d08f72f6de679a5f080df77f2e083744cf4fb73027a1914",
            "style-4": "c167fc495793853cff6e102b709fb168fe756deb40c20a8b5c6851104650f58c",
            "style-5": "b75b91ffc6383e63848c1ef5339a6603e582f6df0e2872425802580b35db4554",
            "style-6": "927ba8c3f87f79c90b1c5584ff44e4b2c32b8e30e819b31a9d2a6150d1413018",
        },
        "burger_hotdog": {
            "style-1": "9e73a0430750a905479218c6c800870430f3542774d87397d34e722c7bd3d02f",
            "style-2": "79c253c472f030eab9600092553632e485a33a752c77852eed09306cde7ca3fd",
            "style-3": "8acd78ba9cbb35116c6e85c1c41b1c388e4ae99690c18246c0a8560f71db18a2",
            "style-4": "60cf9409ed6ce7084ad3e0dd77633cbffe47f9ac5ca7bdff5b2f3393de58cfea",
            "style-5": "867c011194e0d9444528be1d1e91f75c5e60f4b6edeccb6e0db88f132b9645d4",
            "style-6": "c78deecf92e6a19592a54687685d244b0c66d838de610e6688db6eb198823836",
        },
        "pizza": {
            "style-1": "50c283741524f8a3b2cfbce4827c4d7ef9e1a5131cffbe3b762048ea3f1234a7",
            "style-2": "e43a90a8b559ea65f74d43a88a9d79d6b9dfc161247a16372eb8abd2a796e40e",
            "style-3": "46a68962bdf246c040050760e1cdc4ad4852f0bbfb33c4fb007dccc5514fe97d",
            "style-4": "735f9fd4ffc501bc8e829594e51f5d3b596b7946339d4202498af0deed73154b",
            "style-5": "21db73106c31e20ca6bc1a30d33be6c6ee15e02b595db71bec7191609f241c1f",
            "style-6": "7c6f5416abf68f8920198a94426df0887019ba9bf324fc63f197cf761fafbfe1",
        },
        "pasta_steak": {
            "style-1": "ffb4cabf6ca7fa7065f55e12688fd5b6bbfc93f3a825b59168eb94922cfacce3",
            "style-2": "bb0e03fd065918b6701b543dd31267fc721ed16aeb9a740156b10e2ef4303937",
            "style-3": "f5b8fc44704d6f623c6025083d17dffc2b41f19718adfd13c242bae4820e381c",
            "style-4": "9e56c698ce0b8b84a0751c28f20276a5f2248b6ecd4ba3ea51af77d706da4e8b",
            "style-5": "ed48ed4cfeac049df2353a33bbf1add11aed31bc4efd0c58d3bafecc37fbcb29",
            "style-6": "70332535090a724bd9ce40ca27d3418fc68220a2cc26d88878dcfb4a6c84dc16",
        },
        "korean": {
            "style-1": "fb9bc0f51542c92433dd1731daab1c72926bb5cae0889b18db7572742dbdb5a6",
            "style-2": "227aa8b0eb6aa98a619d1e214931a3d238dfd08ffb347d09f4c585b36543ac52",
            "style-3": "e68712deafc5cdccc424a5b4ba1a31a3511bae7884bcaf7ef7db6c5e941fca1d",
            "style-4": "2d15bd9bc7719236509b34d8a916f20aaef469990c81ce0a32b26ed940802889",
            "style-5": "fe3a850f62f821a3ebe3c2776b04a5d1be8d8dac64861b7d06091469ead2f2b6",
            "style-6": "a532bbe7096c46067ed6423f5b4ab27db1ee6ef27495f68b063db0eb1287afed",
        },
        "southeast_asian": {
            "style-1": "5578a6c8545786150def4dbc3e62e82e1c0a957a835ffe91eabc4148bf97a08f",
            "style-2": "28a458a03fedde1d768ab87d37d7da83ab67ef971b35577dd538d76b4a185ce7",
            "style-3": "e9c5061b6ffcbc45780bd43f85e398784d4dfa4744f57105936699ad7c7c035e",
            "style-4": "50899a5f74c2d00916f7bc9216cd13b7d9d1dcdaa4ff8b40f9e0a4fd6a0f1f8a",
            "style-5": "4fe3e70858c9188dc1e0bfba42f21682e1fb7fb1737ba8d38a736bdf51a9b1d3",
            "style-6": "6baccb98f90b0f1bafc23669360f4e017295c9ae316070c947255cffa0364473",
        },
    }

    assert (
        background_profiles.FROZEN_PROFILE_V11_SINGLE_HUE_PROMPT_CATEGORIES
        == set(expected)
    )
    for category_id, hashes in expected.items():
        for style_id, expected_hash in hashes.items():
            prompt = background_profiles.pure_background_prompt(
                category_id,
                style_id,
            )
            assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == expected_hash


def test_material_like_palette_terms_become_plain_solid_colors() -> None:
    sandwich_prompt = background_profiles.pure_background_prompt(
        "sandwich_bagel",
        "style-2",
    )
    japanese_prompt = background_profiles.pure_background_prompt(
        "japanese",
        "style-2",
    )
    northeast_prompt = background_profiles.pure_background_prompt(
        "northeast_chinese",
        "style-1",
    )

    assert "浅暖米色" in sandwich_prompt
    assert "浅木" not in sandwich_prompt
    assert "浅焦糖棕" in japanese_prompt
    assert "原木" not in japanese_prompt
    assert "暖焦糖棕" in northeast_prompt
    assert "暖木" not in northeast_prompt


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
