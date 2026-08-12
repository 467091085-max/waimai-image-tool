from __future__ import annotations

import pytest

import prompt_compiler


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
    assert "全熟浅棕色中式黑椒无骨猪排" in compiled.prompt
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


def test_chroma_prompt_does_not_request_the_selected_scene() -> None:
    compiled = compile_for(mode="chroma_foreground")

    assert "当前步骤只生成供程序抠取的菜品前景" in compiled.prompt
    assert "完全均匀的纯青色抠图幕布（RGB 0,255,255）" in compiled.prompt
    assert "不得画出所选背景或任何真实场景" in compiled.prompt
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
