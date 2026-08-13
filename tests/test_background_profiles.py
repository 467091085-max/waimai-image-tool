from __future__ import annotations

import hashlib

import pytest

from background_design_contracts import CATEGORY_BACKGROUND_DIRECTIONS
import background_profiles
import prompt_compiler
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


def test_mixed_rice_v12_pilot_binds_all_six_scene_contracts() -> None:
    prompts = []
    for style_id in background_profiles.STYLE_IDS:
        contract = prompt_compiler.scene_contract_for(style_id, "mixed_rice")
        prompt = background_profiles.pure_background_prompt(
            "mixed_rice",
            style_id,
            prompt_version=background_profiles.MIXED_RICE_PILOT_PROMPT_VERSION,
        )
        prompts.append(prompt)

        assert f"上方{contract.camera.pitch_degrees}度俯拍" in prompt
        assert f"约{contract.camera.lens_mm}mm标准镜头" in prompt
        assert contract.surface_description in prompt
        assert "整张画面必须是一个可承托餐盘的连续平面" in prompt
        assert "绝不能看见桌沿、桌面厚度、桌腿" in prompt
        assert "中央约70%区域保持完整、干净、连续" in prompt
        assert "真实摄影，不是3D渲染" in prompt

    assert len(prompts) == 6
    assert len(set(prompts)) == 6


def test_mixed_rice_v12_pilot_rejects_other_categories() -> None:
    with pytest.raises(ValueError, match="restricted to mixed_rice"):
        background_profiles.pure_background_prompt(
            "light_food",
            "style-1",
            prompt_version=background_profiles.MIXED_RICE_PILOT_PROMPT_VERSION,
        )


def test_v13_binds_all_40_categories_to_six_distinct_original_directions() -> None:
    taxonomy_ids = {taxonomy_id for taxonomy_id, _label, _words in TAXONOMY_RULES}
    prompts = set()

    assert set(CATEGORY_BACKGROUND_DIRECTIONS) == taxonomy_ids
    for taxonomy_id in sorted(taxonomy_ids):
        category_prompts = []
        for style_id in background_profiles.STYLE_IDS:
            prompt = background_profiles.pure_background_prompt(
                taxonomy_id,
                style_id,
                prompt_version=(
                    background_profiles.BENCHMARKED_BACKGROUND_PROMPT_VERSION
                ),
            )
            category_prompts.append(prompt)
            prompts.add(prompt)

            assert "原创高品质外卖菜品商业摄影空背景" in prompt
            assert "中央72%和四周裁切安全区" in prompt
            assert "不模仿任何品牌的专有版式" in prompt
            assert "桌沿、桌面厚度、桌腿、墙桌分界" in prompt
            assert "悬浮平台、展台、底座、台阶" in prompt
            assert "不是3D渲染" in prompt
            assert len(prompt) <= 760

        assert len(category_prompts) == 6
        assert len(set(category_prompts)) == 6

    assert len(prompts) == 240
    assert (
        background_profiles.background_profile_version(
            background_profiles.BENCHMARKED_BACKGROUND_PROMPT_VERSION
        )
        == background_profiles.BENCHMARKED_BACKGROUND_PROFILE_VERSION
    )


def test_v14_produces_240_category_specific_commercial_backplates() -> None:
    taxonomy_ids = {taxonomy_id for taxonomy_id, _label, _words in TAXONOMY_RULES}
    prompts = set()
    forbidden_category_leaks = (
        "炒饭",
        "拌饭",
        "水果",
        "咖啡",
        "可可",
        "奶油",
        "蜂蜜",
        "芥末",
        "番茄",
        "辣椒",
        "面皮",
        "麦芽",
        "焦糖",
        "卤酱",
        "果肉",
        "酥皮",
        "烘焙",
        "脆壳",
    )

    for taxonomy_id in sorted(taxonomy_ids):
        category_prompts = []
        for style_id in background_profiles.STYLE_IDS:
            prompt = background_profiles.pure_background_prompt(
                taxonomy_id,
                style_id,
                prompt_version=(
                    background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
                ),
            )
            category_prompts.append(prompt)
            prompts.add(prompt)

            assert prompt.startswith(
                "ORIGINAL COMMERCIAL FOOD-PHOTOGRAPHY SET, BACKPLATE ONLY"
            )
            assert "原创高品质外卖商业摄影布景底板" in prompt
            assert "中央约62%保持完整" in prompt
            assert "低信息纯色块" in prompt
            assert "不复制或模仿任何品牌的专有版式" in prompt
            assert not any(term in prompt for term in forbidden_category_leaks)
            assert len(prompt) <= 900

        assert len(category_prompts) == 6
        assert len(set(category_prompts)) == 6

    assert len(prompts) == 240
    mixed_prompt = background_profiles.pure_background_prompt(
        "mixed_rice",
        "style-4",
        prompt_version=background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
    )
    assert "炒饭/拌饭" not in mixed_prompt
    assert "温润胡桃木" in mixed_prompt
    assert "深灰粗麻餐巾边角与胡桃木筷" in mixed_prompt
    assert (
        background_profiles.background_profile_version(
            background_profiles.EMPTY_SET_BACKGROUND_PROMPT_VERSION
        )
        == background_profiles.EMPTY_SET_BACKGROUND_PROFILE_VERSION
    )


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
        "sandwich_bagel": {
            "style-1": "19584dfb9a73779157cfbe29b03715a19b8bd49913f34f450cc9c8a827878d39",
            "style-2": "228ea0bad722c5446bd09d651ad1ef662e93ff1ff753afec2b85c6be83efbcbd",
            "style-3": "85159ba60fc1805712db0754babc100ff15c2da3bdecba101591a722c88c0071",
            "style-4": "895ecca2f9c68cbdd0bbd0f8716b625780fd194fbec3bc3667f6a107bc6faf13",
            "style-5": "59d7a8263fbeb99522e80b3f6a011f099c198da2e5ec3a7bf30bc4852dc0112c",
            "style-6": "d528d27941609651565172349da660bd1b60868b053be37b32f760b4f8d75b4f",
        },
        "japanese": {
            "style-1": "1508c2eb9ff731d3c6db825777a34f7c7ec5541d90e385c63cc96bf82bb64dc5",
            "style-2": "9de6d5678eff7a79e9521ca034f6e7ccc2cc741e5bf7d86d1f1bbdf382ee9fa4",
            "style-3": "2aedeb53b1c4616738ca6040f43cbe9a7658fd2e6c8c601af4912ba2bf443954",
            "style-4": "a669b02054b944a708c97ec351a8533d841ebbc50d4b08765481056f56b79eb5",
            "style-5": "ceccbcf94e253aaa780d49ac4224775567457245fe84850a9f35f085bdeb043e",
            "style-6": "9a87bb8dab5e70ed49796c46f2f74cd08956ff9f80816ebdce77c6a733d8c258",
        },
        "sichuan_hunan": {
            "style-1": "5c3393bc1c80d5f48ea3cad5b5bae6277fc731f7f776ab6911e0904f1d448112",
            "style-2": "d0def69a86f2ffea53603f3a2d57a3546caeedad0b400e951a3da56c4256a91f",
            "style-3": "76cd9a6f3b21ed08f57f031e035c24bce7d37a6a4cd392157edee314fdd74d35",
            "style-4": "b170bf3ba862c3e7c297b4b7faf6d6753622151232215b29433c7fd1aa77b8c4",
            "style-5": "5537a210166725ecaaeb619da4b420c5d1bc9bcf31c377de97bb14ff6fba0f99",
            "style-6": "f212ac663788093f125ed50a1b08680a45591f27b4f0c1e519d6b9444017305e",
        },
        "cantonese_roast": {
            "style-1": "e9d73c5fca3e68398eed7b82338ac0933e4444d24faf461b864cb1e3d740ad85",
            "style-2": "93f9feab84c8d6f2ea97707474d99614c09b18b3a13444820c75a3f03872b0ed",
            "style-3": "4847c24b319fcf995af5fd8a5dcbc2ee5515cdd8cb7e57f99705686dafde7eab",
            "style-4": "7b099d1c251561735e5579a358bc6b20eaa1b44f58034e2b93b948787a27d292",
            "style-5": "83fee6db3477636414eb98200de209b37f71bf315b3b50d682981b94f4c470c5",
            "style-6": "d096a37e85f5005999e7ed88972607eb3c0069b144549771850bbd0e6a2b12a9",
        },
        "jiangzhe": {
            "style-1": "68a787d1f19b0b17d25d34bd672912acd35d3657d141d809bd7f8dbaa6dcb5c3",
            "style-2": "6b7b1d72937820d5e3e0278b2ef178bac4a26b6dfc40af637a91f6e5ee4f3cc7",
            "style-3": "10588e032b778b2882fdf736af0b5e7ec78c5d423bca40ad02bb645a947a6fb1",
            "style-4": "6e0c2e4d82f2c427820634261e25d2c6f3bc6832fe0e5843ed1f8178915c0502",
            "style-5": "524ce2f7855e756d3558cd829ceec9bcdf0ce2afcbbd067f298a091849ea3d5c",
            "style-6": "8a1cbc7bb62f1bc88ee66373ec9cd240d81ff67643aa414187b29603f30084a9",
        },
        "northeast_chinese": {
            "style-1": "ddf9ff34db9727058f7518616560d883a1dc7c0379fc40ad481c4f3bf8a46d11",
            "style-2": "0849dece85f45c6e9329f3376ec0e9e7ac4d0cbc1c25d12756e46d2e697a9687",
            "style-3": "4c85de4d077cd4db8e89aa5e56d910a1da76eaece3efae6bcfa0c896622f06a2",
            "style-4": "d2c9df6b18e4a6057743493d76d60bec024f0e0621a7fbd74e8bc4fcf6e90fa8",
            "style-5": "a529e2d177abf20d158ef878958592ab14616419dec31922fc604b3c518d1b40",
            "style-6": "fe99e59fc6b38a0abec7eb89e438930b5d9d2ca865cbb4de3708fc57d54586d8",
        },
        "northwest_xinjiang": {
            "style-1": "4bac3d45326f6e54187afac92666ddfdbc83386376103f5ea72d9c04013c92fe",
            "style-2": "383ea7fc2d794e7ba54b04b4ac18778c94bbb59cc3e2181088852de44cac64b9",
            "style-3": "6d8e97a945752fe14982a09708a5672573022dd32bf4b0231a4b9f7728e20aee",
            "style-4": "1e7583f18242a49d38af0279f253c1ca4b7efa46045f0effe4ddde0b63542d27",
            "style-5": "485374a8e628fb19d5e44ecb46eef17b2b44797fb38616be4d9d61f2710cf251",
            "style-6": "4fb8689b98af903b4ba724ba7715d3144a9cb9a41fea28c43b5ac482a146eefd",
        },
        "northern_lu": {
            "style-1": "ad699667576759f5f2dc8fa0118c6ea100b582491d68e36bba54a9453571d1fe",
            "style-2": "93f9feab84c8d6f2ea97707474d99614c09b18b3a13444820c75a3f03872b0ed",
            "style-3": "eae65b4927d3f25dcf0c9150375f0a87464722d4af0bdc6e308ab94e3053ccd5",
            "style-4": "d6c1e2c6b2ba81546ac62f3196e8edcc6a666bae53af11659ed62ce9545e581d",
            "style-5": "189ca134adc34db6ae777cc7ba26ea06612abaf7b5cc83327c243fbc8bdd864c",
            "style-6": "b6098f05d344aeb98ee036147ec114ff8126f6d0df5e720b4bf3ffe181b713a4",
        },
        "fujian_taiwan": {
            "style-1": "1f2ad4c5de9acc0ed9aa76cac75841f86be7af28d22af571d2135d7dc608959a",
            "style-2": "2a95e6aa54238d338c29311ed5e8bcb6af48a4d4891ab7c5894929e8b6f0ba28",
            "style-3": "b8a46fe0b0114fe1612ec9cbf5a0b7b67fa70c1004608f77e44525368bfaef87",
            "style-4": "64046c805eb1f755c1ab5878bc677330d2415ad59a0274eba8f335d4c6a206a9",
            "style-5": "99929e6594495ccb7482a817de1a48519fa33d077fd9e1ce1962689979df052c",
            "style-6": "8e397a2ecbdccc16aaa5fadc34f3800d085220baaf4ea2fd9aa7c65c06aad614",
        },
        "home_stir_fry": {
            "style-1": "ddf9ff34db9727058f7518616560d883a1dc7c0379fc40ad481c4f3bf8a46d11",
            "style-2": "d9515531d3b8583aabe821a1e19540e3491bb07c535c1c326fbe7c7e91d159f9",
            "style-3": "4c85de4d077cd4db8e89aa5e56d910a1da76eaece3efae6bcfa0c896622f06a2",
            "style-4": "d2c9df6b18e4a6057743493d76d60bec024f0e0621a7fbd74e8bc4fcf6e90fa8",
            "style-5": "a529e2d177abf20d158ef878958592ab14616419dec31922fc604b3c518d1b40",
            "style-6": "fe99e59fc6b38a0abec7eb89e438930b5d9d2ca865cbb4de3708fc57d54586d8",
        },
        "fish_seafood": {
            "style-1": "1f2ad4c5de9acc0ed9aa76cac75841f86be7af28d22af571d2135d7dc608959a",
            "style-2": "060081cf1cc8657bd0a360c4c7d39ea816f8ef558c14e112b06e7f579e2675a6",
            "style-3": "b8a46fe0b0114fe1612ec9cbf5a0b7b67fa70c1004608f77e44525368bfaef87",
            "style-4": "64046c805eb1f755c1ab5878bc677330d2415ad59a0274eba8f335d4c6a206a9",
            "style-5": "99929e6594495ccb7482a817de1a48519fa33d077fd9e1ce1962689979df052c",
            "style-6": "8e397a2ecbdccc16aaa5fadc34f3800d085220baaf4ea2fd9aa7c65c06aad614",
        },
        "beef_lamb_pot": {
            "style-1": "c184afd5445bef0f840758dcbcbfbb2bc7ac20e33a38bf6092654368c1ccc5f0",
            "style-2": "341b5ec986c6c2cc6ccc75158d131c412123ee527250262e517c0d14f8757a98",
            "style-3": "9c9afab64977cba9c12c1a60ac64c9526b5d1510b4077bef943f64b92d2c9939",
            "style-4": "750dc834ef8bffa30ce16fd910ab716af967eb7431904dfb2a850da6027d7d59",
            "style-5": "cdd6b46dc1e9813d1b260079191c3ab6918ce44cf822acbdaf21a725273643e8",
            "style-6": "bb0ceef29cb3f3ab14eb14cdac1fa816c0788eb0a6e3eb499535abe71115d5d2",
        },
        "braised_cooked_food": {
            "style-1": "4b217f5f04e6deeeb16ba6d342754af448aace9411b8c7383d6c03fadbeb77d1",
            "style-2": "262933c253b125c49fb4beb4d0653b4390921930287c2399e56186ecc4471839",
            "style-3": "7064a67ffafbde1d132976877296cafb2c535a0abd1ae82c2504ef19bbd669b5",
            "style-4": "07c9ca3922b6b7d1e644312d123428c1c35ba1e051a2827655947551b484c5dc",
            "style-5": "7c951fadf7788f3fcbe5f14f470fd99bf91b1df5947aa4fadfed5fd184499b1c",
            "style-6": "114f0dd8ad495e109b1f5a2bffae162cc106ae63a0def39c99d77baba4ff6da0",
        },
        "soup_stew": {
            "style-1": "d89cdb64cffba02dcbffa05c789302cc9d48387429f2694a05920bee64b1a98e",
            "style-2": "f37136e999605c69298cf217dfa826c01eedc317611a951fb6a16dea22d6c099",
            "style-3": "3883347046cca3ee09c622a09a5e6a29066a80560aa1be1775013ef1207c077f",
            "style-4": "5989f1708b22e5ad3f93bf1e78e8f9406196b0982ae310fe241800e8a5af4faa",
            "style-5": "bd1159267e39dfd519c418d33f54ff1bb9bd9106508f5f38d05caba68dfd44fa",
            "style-6": "f6c20c57fe6958f835f73db63b9abaeda38025e9be93dc776d6c207775458893",
        },
        "steamed_claypot": {
            "style-1": "d9f73da9ebd9c01bf4b2b73e712c4822e267c8febab357a0c4a8d387f2ec2a22",
            "style-2": "2a877c58ff7866e1221fa4f16452c213bcd293c8280ade5f372d22b546932992",
            "style-3": "f28a0471b50728e9f82155fb78d08da471c03218ae1630ade348e08522ace5ce",
            "style-4": "c4271dd438dbdc90eacd13369a6f9cd04cef9a7f0e5ce7336fd84b8bb04bfa05",
            "style-5": "98a01744873d1a5727f8ecad0e5802b7ece4b75deeb87a2cb35cb1530d66d12c",
            "style-6": "725ee501f8d658eb3b639a5b2d2391f17688b1a9b9a8027b057b818de075d70c",
        },
        "milk_fruit_tea": {
            "style-1": "95d67a0a5ed17d0f23eb77b6f0f2c25398c11db05cf026850d3040280c711662",
            "style-2": "e47533bddee30c6c086e0cf64a26fdf8da2270aab2581b79c2c332d135e97b4a",
            "style-3": "bd4fb5aa105db07c2b80ad980114acb8b0bf0909d158451a17bda638360d1b56",
            "style-4": "370605bb5e94fa03cfed59924718669fc34f3e4397dce89a697108ed88506900",
            "style-5": "c45260a040d1fc5d275cba19fca828f787c9ff3ac45083205dcaceb439cd6674",
            "style-6": "643c1c3f20267dc5198641fc262202cf6c8e976291eea98c132629e1a76dda79",
        },
        "coffee_cocoa": {
            "style-1": "2fd15d871babaeaef9bf89d88f90a051c616f53c51b6322147563f2428f87064",
            "style-2": "e43a90a8b559ea65f74d43a88a9d79d6b9dfc161247a16372eb8abd2a796e40e",
            "style-3": "d162794eba6a72032a04c76db69c2ea4c8a6c4d4df60a3f2f7c6d363b10d0a72",
            "style-4": "8fa8acb0962112d1d45573d6f53b9f868b4b69e4fcf88fab1a7e9ae485af97a1",
            "style-5": "c188c3232f516f190a508844b29eb3d8fa4faaebfa7b5180379bcee7c1aa4bbc",
            "style-6": "926752ac248acca330daf6363d0d70dc523dbe89a35df0615bb385848c26a2a0",
        },
        "bottled_drinks": {
            "style-1": "594d45241449f6b7f0c38210efce168df979163c9e50fd459c01e8c4de07914b",
            "style-2": "19f002de37e5a987777eca78da41f46fb8f007be57db354266bc19689a5e4725",
            "style-3": "31b7ce8874a4b6242e922ff20bcf44aa257623ad0cdbb0f4540d4b888c175e3b",
            "style-4": "a3d095e2e1bd4403021b9f838a604175ce88a3588b438ecc14f4362777ef37c6",
            "style-5": "9c3875e68876c9e01ddedeae00a1b15ec1f091d5f5ab3d76c460799f4a2978d8",
            "style-6": "0db7f244a8c73822838cdbd43091dfcaa61a33a3425b52af19185cfdd9d3de5b",
        },
        "fresh_drinks": {
            "style-1": "fb9bc0f51542c92433dd1731daab1c72926bb5cae0889b18db7572742dbdb5a6",
            "style-2": "eca043612bc63edb6d7a9562de093daee5ca1f38cfa367375a5be267a1c6ac9e",
            "style-3": "e68712deafc5cdccc424a5b4ba1a31a3511bae7884bcaf7ef7db6c5e941fca1d",
            "style-4": "2d15bd9bc7719236509b34d8a916f20aaef469990c81ce0a32b26ed940802889",
            "style-5": "fe3a850f62f821a3ebe3c2776b04a5d1be8d8dac64861b7d06091469ead2f2b6",
            "style-6": "a532bbe7096c46067ed6423f5b4ab27db1ee6ef27495f68b063db0eb1287afed",
        },
        "dessert_bakery": {
            "style-1": "95d67a0a5ed17d0f23eb77b6f0f2c25398c11db05cf026850d3040280c711662",
            "style-2": "062bab789a89215e3e94ab137188342cfc93ca165dac541ad9990f8384566d02",
            "style-3": "bd4fb5aa105db07c2b80ad980114acb8b0bf0909d158451a17bda638360d1b56",
            "style-4": "370605bb5e94fa03cfed59924718669fc34f3e4397dce89a697108ed88506900",
            "style-5": "c45260a040d1fc5d275cba19fca828f787c9ff3ac45083205dcaceb439cd6674",
            "style-6": "643c1c3f20267dc5198641fc262202cf6c8e976291eea98c132629e1a76dda79",
        },
        "fried_snacks": {
            "style-1": "3d6dbe0803ce955648004f6ca2922a38b39a9495fb492caea9cfb9828b1b2881",
            "style-2": "6439a94c43f0dc7e21de1a591964773f681546450ad9f047983bc654f6b4247d",
            "style-3": "883cb270e862843132b545abf0d2d8e3614e7849366782157672eaa1260fc72e",
            "style-4": "a3ea37298e0a9512c42521fb81e5510ae384541c88ef08e2e3a47bd639789042",
            "style-5": "2cd92000cd2b1650d7438555982e8d093405521b4722520ec56ad7be77e9765c",
            "style-6": "05517fbdc14662f7ea5995f8ccc8b89cae3d030c6a6ce220ad158ee381277bff",
        },
        "fruit": {
            "style-1": "594d45241449f6b7f0c38210efce168df979163c9e50fd459c01e8c4de07914b",
            "style-2": "aa4bc72ee16992a1b5feb30100dd0bdd8d96ec5e9a0a7da885e91ce0cfcbbe63",
            "style-3": "31b7ce8874a4b6242e922ff20bcf44aa257623ad0cdbb0f4540d4b888c175e3b",
            "style-4": "a3d095e2e1bd4403021b9f838a604175ce88a3588b438ecc14f4362777ef37c6",
            "style-5": "9c3875e68876c9e01ddedeae00a1b15ec1f091d5f5ab3d76c460799f4a2978d8",
            "style-6": "0db7f244a8c73822838cdbd43091dfcaa61a33a3425b52af19185cfdd9d3de5b",
        },
    }

    assert (
        background_profiles.FROZEN_PROFILE_V11_SINGLE_HUE_PROMPT_CATEGORIES
        == set(expected)
    )
    assert (
        background_profiles.FROZEN_PROFILE_V12_NORMALIZED_PROMPT_CATEGORIES
        == {
            "sandwich_bagel",
            "japanese",
            "sichuan_hunan",
            "cantonese_roast",
            "jiangzhe",
            "northeast_chinese",
            "northwest_xinjiang",
            "northern_lu",
            "fujian_taiwan",
            "home_stir_fry",
            "fish_seafood",
            "beef_lamb_pot",
            "braised_cooked_food",
            "soup_stew",
            "steamed_claypot",
            "milk_fruit_tea",
            "coffee_cocoa",
            "bottled_drinks",
            "fresh_drinks",
            "dessert_bakery",
            "fried_snacks",
            "fruit",
        }
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
