from __future__ import annotations

import pytest

import prompt_compiler
from matching_engine import extract_menu_semantics


def mixed_rice_combo() -> dict[str, object]:
    return {
        "name": "超值爆款豪华三拼【烤肉+烤排+鸡排】+煎蛋/热狗肠/饮品三选一",
        "kind": "套餐/组合",
        "components": [
            "烤肉",
            "烤排",
            "鸡排",
            "煎蛋/热狗肠/饮品三选一",
        ],
    }


def compile_for(
    style_id: str = "style-3",
    mode: str = "chroma_foreground",
) -> prompt_compiler.CompiledGeneration:
    scene = prompt_compiler.scene_contract_for(
        style_id,
        "mixed_rice",
        asset_id=f"bg-{style_id}",
        asset_sha256="a" * 64,
    )
    return prompt_compiler.compile_product_image(
        mixed_rice_combo(),
        menu_taxonomy_id="mixed_rice",
        background=scene,
        quality="standard",
        mode=mode,
    )


def test_compiler_resolves_food_semantics_choices_and_camera() -> None:
    compiled = compile_for()

    assert "必须清楚出现白米饭" in compiled.prompt
    assert "全熟浅棕色中式黑椒无骨猪肉排" in compiled.prompt
    assert "切成6片整齐排列" in compiled.prompt
    assert "备选已固定为煎蛋" in compiled.prompt
    assert "热狗肠" not in compiled.prompt
    assert "饮品三选一" not in compiled.prompt
    assert "56度俯拍" in compiled.prompt
    assert "52mm标准镜头" in compiled.prompt
    assert "器皿底面与承托面平行" in compiled.prompt
    assert "竖立" in compiled.prompt
    assert "完全均匀的纯青色" in compiled.prompt
    assert compiled.audit["passed"] is True


def test_real_meizizi_structured_combo_uses_only_required_and_selected_food() -> None:
    name = "豪华三拼【烤肉+烤排+鸡排】+煎蛋/热狗肠/饮品三选一"
    attrs = (
        "口味自选#人气香辣（粉）#蜜汁味（酱）##"
        "赠品三选一#热狗肠#煎蛋#随机饮品##"
    )
    semantics = extract_menu_semantics(name, attrs, "|进店|必点")
    row = {"name": name, "kind": "套餐/组合", "components": semantics["requiredComponents"], **semantics}
    scene = prompt_compiler.scene_contract_for("style-3", "mixed_rice")

    compiled = prompt_compiler.compile_product_image(
        row,
        menu_taxonomy_id="mixed_rice",
        background=scene,
        quality="standard",
        mode="chroma_foreground",
    )

    assert compiled.dish.required_components == ("烤肉", "烤排", "鸡排")
    assert compiled.dish.components == ("烤肉", "烤排", "鸡排", "煎蛋")
    assert compiled.dish.selected_choices == ("煎蛋",)
    assert compiled.dish.flavor_modifiers == ("人气香辣", "蜜汁味")
    assert "套餐构成：烤肉、烤排、鸡排、煎蛋" in compiled.prompt
    assert "热狗肠" not in compiled.prompt
    assert "随机饮品" not in compiled.prompt
    assert "饮品只放一杯" not in compiled.prompt
    assert "饮品三选一" not in compiled.dish.visual_name
    assert compiled.dish.visual_name == "烤肉+烤排+鸡排+煎蛋套餐"
    assert "人气香辣" not in compiled.prompt
    assert "套餐构成：品" not in compiled.prompt


def test_real_meizizi_combo_with_drink_still_uses_platter() -> None:
    name = "招牌拌饭套餐自选+大鸡腿+饮品自选"
    attrs = (
        "肉自选#烤肉#烤排#鸡排#腿排##"
        "口味#蜜汁味（酱）#麻辣味（酱）##"
        "饮品#随机饮品##"
    )
    semantics = extract_menu_semantics(name, attrs, "美滋滋神枪手")
    row = {"name": name, "kind": "套餐/组合", "components": semantics["requiredComponents"], **semantics}

    dish = prompt_compiler.analyze_dish(row, "mixed_rice")

    assert dish.container == "一个水平放置的宽大低矮纯色分格餐盘"
    assert dish.required_components == ("招牌拌饭", "大鸡腿")
    assert dish.selected_choices == ("烤肉", "随机饮品")
    assert dish.visual_name == "拌饭+大鸡腿+烤肉+随机饮品套餐"
    assert any(
        requirement.startswith("饮品只放一杯")
        for requirement in dish.semantic_requirements
    )
    assert "品" not in dish.required_components


def test_selected_side_choice_does_not_reintroduce_rejected_drink() -> None:
    row = {
        "name": "黑椒烤排拌饭+时蔬+小吃饮品自选套餐",
        "kind": "套餐/组合",
        "requiredComponents": ["黑椒烤排拌饭", "时蔬"],
        "choiceGroups": [
            {
                "name": "小吃饮品",
                "role": "side",
                "options": ["热狗肠", "煎蛋", "随机饮品"],
                "selected": "热狗肠",
            }
        ],
    }
    scene = prompt_compiler.scene_contract_for(
        "style-1",
        "mixed_rice",
        prompt_version=prompt_compiler.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
    )

    compiled = prompt_compiler.compile_product_image(
        row,
        menu_taxonomy_id="mixed_rice",
        background=scene,
        quality="standard",
        mode="chroma_foreground",
    )

    assert compiled.dish.visual_name == "黑椒烤排拌饭+时蔬+热狗肠套餐"
    assert compiled.dish.selected_choices == ("热狗肠",)
    assert compiled.dish.rejected_choices == ("煎蛋", "随机饮品")
    assert "饮品只放一杯" not in compiled.prompt
    assert "随机饮品" not in compiled.prompt
    assert "小吃饮品自选" not in compiled.prompt


def test_structured_drink_role_adds_cup_for_brand_name() -> None:
    row = {
        "name": "招牌拌饭饮料套餐",
        "kind": "套餐/组合",
        "requiredComponents": ["招牌拌饭"],
        "choiceGroups": [
            {
                "name": "饮料",
                "role": "drink",
                "options": ["王老吉", "果粒橙"],
                "selected": "王老吉",
            },
            {
                "name": "配菜",
                "role": "side",
                "options": ["薯条", "煎蛋"],
                "selected": "薯条",
            },
        ],
    }

    dish = prompt_compiler.analyze_dish(row, "mixed_rice")

    assert dish.selected_choices == ("王老吉", "薯条")
    assert sum(
        requirement.startswith("饮品只放一杯")
        for requirement in dish.semantic_requirements
    ) == 1


def test_non_drink_choice_role_does_not_add_cup_for_unknown_name() -> None:
    row = {
        "name": "招牌拌饭套餐",
        "kind": "套餐/组合",
        "requiredComponents": ["招牌拌饭"],
        "choiceGroups": [
            {
                "name": "配菜",
                "role": "side",
                "options": ["脆脆条", "煎蛋"],
                "selected": "脆脆条",
            }
        ],
    }

    dish = prompt_compiler.analyze_dish(row, "mixed_rice")

    assert not any(
        requirement.startswith("饮品只放一杯")
        for requirement in dish.semantic_requirements
    )


def test_rejected_main_choice_does_not_trigger_its_semantic_rules() -> None:
    row = {
        "name": "烤肉/烤排二选一套餐",
        "kind": "套餐/组合",
        "requiredComponents": [],
        "choiceGroups": [
            {
                "name": "肉类二选一",
                "role": "main",
                "options": ["烤肉", "烤排"],
                "selected": "烤肉",
            }
        ],
    }
    scene = prompt_compiler.scene_contract_for("style-1", "mixed_rice")

    compiled = prompt_compiler.compile_product_image(
        row,
        menu_taxonomy_id="mixed_rice",
        background=scene,
        quality="standard",
        mode="chroma_foreground",
    )

    assert compiled.dish.visual_name == "烤肉套餐"
    assert compiled.dish.rejected_choices == ("烤排",)
    assert "烤肉画成切片中式蜜汁猪肉" in compiled.prompt
    assert "烤排画成" not in compiled.prompt
    assert "烤排" not in compiled.prompt


def test_chroma_prompt_does_not_request_the_selected_scene() -> None:
    compiled = compile_for(mode="chroma_foreground")

    assert "当前步骤只生成供程序抠取的菜品前景" in compiled.prompt
    assert "完全均匀的纯青色抠图幕布（RGB 0,255,255）" in compiled.prompt
    assert "不得画出所选背景或任何真实场景" in compiled.prompt
    assert "餐盘、餐盒、杯子、碗、托盘" in compiled.prompt
    assert "绝不能使用青色、蓝绿色" in compiled.prompt
    assert "器皿边缘不得染上幕布颜色" in compiled.prompt
    assert "背景必须跟所选背景一致" not in compiled.prompt
    assert "背景必须遵循" not in compiled.prompt
    assert "晨光浅洞石餐桌" not in compiled.prompt
    assert "一整块纹理连续的浅色洞石桌面" not in compiled.prompt


def test_reference_prompt_still_requires_the_selected_scene() -> None:
    compiled = compile_for(mode="reference")

    assert "背景必须跟所选背景一致" in compiled.prompt
    assert "背景必须遵循“晨光浅洞石餐桌”" in compiled.prompt
    assert "一整块纹理连续的浅色洞石桌面" in compiled.prompt
    assert "纯青色抠图幕布" not in compiled.prompt


def test_compiler_is_deterministic_and_scene_bound() -> None:
    first = compile_for("style-3")
    duplicate = compile_for("style-3")
    changed_scene = compile_for("style-6")

    assert first.prompt == duplicate.prompt
    assert first.seed == duplicate.seed
    assert first.compile_digest == duplicate.compile_digest
    assert first.scene_contract_sha256 != changed_scene.scene_contract_sha256
    assert first.compile_digest != changed_scene.compile_digest
    assert first.seed != changed_scene.seed


def test_scene_contract_payload_is_hash_bound_to_asset() -> None:
    contract = prompt_compiler.scene_contract_for(
        "style-4",
        "mixed_rice",
        asset_id="bg-style-4",
        asset_sha256="b" * 64,
    )
    payload = contract.payload()

    restored = prompt_compiler.scene_contract_from_payload(
        payload,
        expected_asset_id="bg-style-4",
        expected_asset_sha256="b" * 64,
    )
    assert restored.contract_sha256 == contract.contract_sha256

    payload["camera"]["pitch_degrees"] = 25
    with pytest.raises(prompt_compiler.PromptCompilationError):
        prompt_compiler.scene_contract_from_payload(payload)


def test_scene_contract_uses_the_backgrounds_actual_prompt_geometry() -> None:
    legacy = prompt_compiler.scene_contract_for(
        "style-3",
        "mixed_rice",
        prompt_version=prompt_compiler.LEGACY_BACKGROUND_PROMPT_VERSION,
    )
    current = prompt_compiler.scene_contract_for(
        "style-3",
        "mixed_rice",
        prompt_version=prompt_compiler.CURRENT_BACKGROUND_PROMPT_VERSION,
    )

    assert legacy.camera.pitch_degrees == 25
    assert current.camera.pitch_degrees == 56
    assert legacy.background_prompt_version == "style-background.v11"
    assert current.background_prompt_version == "style-background.v12"
    assert legacy.contract_sha256 != current.contract_sha256
    assert (
        prompt_compiler.scene_contract_from_payload(legacy.payload())
        .contract_sha256
        == legacy.contract_sha256
    )


def test_v13_scene_geometry_tracks_product_orientation() -> None:
    upright = prompt_compiler.scene_contract_for(
        "style-3",
        "milk_fruit_tea",
        prompt_version=prompt_compiler.BENCHMARKED_BACKGROUND_PROMPT_VERSION,
    )
    plate = prompt_compiler.scene_contract_for(
        "style-3",
        "mixed_rice",
        prompt_version=prompt_compiler.BENCHMARKED_BACKGROUND_PROMPT_VERSION,
    )
    top_down = prompt_compiler.scene_contract_for(
        "style-3",
        "pizza",
        prompt_version=prompt_compiler.BENCHMARKED_BACKGROUND_PROMPT_VERSION,
    )

    assert upright.camera.pitch_degrees == 18
    assert plate.camera.pitch_degrees == 46
    assert top_down.camera.pitch_degrees == 62
    assert upright.placement.max_subject_width_ratio == 0.58
    assert plate.placement.max_subject_width_ratio == 0.84
    assert top_down.placement.max_subject_width_ratio == 0.88
    assert upright.background_prompt_version == "style-background.v13"
    assert plate.contract_sha256 != top_down.contract_sha256


def test_v14_uses_its_versioned_full_frame_table_contract() -> None:
    v13 = prompt_compiler.scene_contract_for(
        "style-4",
        "mixed_rice",
        prompt_version=prompt_compiler.BENCHMARKED_BACKGROUND_PROMPT_VERSION,
    )
    v14 = prompt_compiler.scene_contract_for(
        "style-4",
        "mixed_rice",
        prompt_version=prompt_compiler.EMPTY_SET_BACKGROUND_PROMPT_VERSION,
    )

    assert v14.camera != v13.camera
    assert v14.camera.pitch_degrees == 46
    assert v14.style_name == "温润木质用餐桌景"
    assert v14.scene_type == "full-frame-tabletop"
    assert "桌面从四边延伸画外" in v14.scene_description
    assert "桌腿" in v14.scene_description
    assert v14.support == v13.support
    assert v14.placement == v13.placement
    assert v14.background_prompt_version == "style-background.v14"
    assert v14.contract_sha256 != v13.contract_sha256
    assert (
        prompt_compiler.scene_contract_from_payload(v14.payload()).contract_sha256
        == v14.contract_sha256
    )


def test_compiler_never_silently_truncates_mandatory_contract() -> None:
    scene = prompt_compiler.scene_contract_for("style-3", "mixed_rice")
    provider = prompt_compiler.ProviderCapabilities(
        "test-provider",
        max_prompt_chars=120,
    )

    with pytest.raises(
        prompt_compiler.PromptCompilationError,
        match="mandatory prompt contract exceeds provider limit",
    ):
        prompt_compiler.compile_product_image(
            mixed_rice_combo(),
            menu_taxonomy_id="mixed_rice",
            background=scene,
            quality="standard",
            mode="chroma_foreground",
            provider=provider,
        )


def test_hunyuan_v3_payload_disables_revision_and_records_hashes() -> None:
    compiled = compile_for()

    assert compiled.provider_payload["Prompt"] == compiled.prompt
    assert compiled.provider_payload["Revise"] == 0
    assert compiled.provider_payload["Seed"] == compiled.seed
    assert "NegativePrompt" not in compiled.provider_payload
    assert len(compiled.prompt_sha256) == 64
    assert len(compiled.provider_payload_sha256) == 64
