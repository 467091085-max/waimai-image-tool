from __future__ import annotations

import copy

import background_catalog
import pytest
import prompt_compiler

from shared.batch_contract import (
    BatchContractError,
    CONTRACT_ATTESTATION_VERSION,
    EXTRA_PLATFORM_POINTS,
    WATERMARK_POINTS,
    freeze_menu_batch_contract,
    menu_batch_contract_attestation_valid,
    request_sha256,
)
from shared.refinement_contract import (
    CONTRACT_ATTESTATION_VERSION as REVISION_ATTESTATION_VERSION,
    revision_contract_attestation_valid,
)


TEST_GENERATION_PROVENANCE = {
    "taxonomyVersion": "2026-07-30.v2",
    "dishPromptVersion": "dish-generation.v1",
    "pipelineVersion": "exact-background.v1",
    "provider": "tencent-hunyuan",
    "providerMode": "tokenhub-fail-closed-v2",
    "modelName": "hy-image-v3.0",
    "modelVersion": (
        "hy-image-v3.0.aiart-2022-12-29."
        "hunyuan-2023-09-01"
    ),
}
TEST_ATTESTATION_SECRET = "menu-batch-signing-secret-32-bytes-minimum"


def valid_input(**overrides):
    payload = {
        "job_id": "gen_test_001",
        "user_id": "user_test_001",
        "menu_upload_id": "menu_test_001",
        "menu": {
            "objectKey": "menus/user_test_001/menu.xlsx",
            "sha256": "1" * 64,
            "parserVersion": 1,
        },
        "selected_background": {
            "assetId": "bg_test_001",
            "libraryAssetId": "asset-" + ("a" * 40),
            "styleId": "style-2",
            "sha256": "2" * 64,
            "objectKey": "backgrounds/menu_test_001/style-2.jpg",
            "width": 1024,
            "height": 768,
        },
        "generation_provenance": TEST_GENERATION_PROVENANCE,
        "quality": "standard",
        "image_count": 20,
        "platforms": ["meituan"],
        "watermark": {
            "enabled": False,
            "type": "text",
            "text": "测试店",
            "color": "black",
            "position": "bottom-right",
            "pattern": "corner",
        },
        "idempotency_key": "idem_test_001",
        "created_at": "2026-07-29T12:00:00Z",
    }
    payload.update(overrides)
    return payload


def v14_selected_background(*, seed: int = 123456) -> dict[str, object]:
    selected = dict(valid_input()["selected_background"])
    prompt_version = "style-background.v14"
    scene = prompt_compiler.scene_contract_for(
        selected["styleId"],
        "mixed_rice",
        asset_id=selected["assetId"],
        asset_sha256=selected["sha256"],
        prompt_version=prompt_version,
    )
    evidence = {
        "generationProvider": "tencent-hunyuan",
        "providerAction": "TokenHubImageV3",
        "model": "hy-image-v3.0",
        "seed": seed,
        "requestedSeed": seed,
        "seedApplied": True,
        "promptRevisionEnabled": False,
        "promptRevisionControlApplied": True,
        "promptVersion": prompt_version,
        "assetSha256": selected["sha256"],
    }
    selected.update(
        {
            "backgroundPromptVersion": prompt_version,
            "sceneContract": scene.payload(),
            "generationEvidence": evidence,
            "generationEvidenceSha256": (
                background_catalog.generation_evidence_sha256(evidence)
            ),
        }
    )
    return selected


def test_freeze_contract_calculates_server_owned_billing_snapshot() -> None:
    contract = freeze_menu_batch_contract(
        **valid_input(
            quality="premium",
            platforms=["jd", "meituan"],
            watermark={
                "enabled": True,
                "type": "text",
                "text": "测试店",
                "color": "white",
                "position": "center",
                "pattern": "tile",
            },
        )
    )

    assert contract["schemaVersion"] == 3
    assert contract["jobType"] == "menu_batch_generation"
    assert contract["quality"] == {"id": "premium", "pointsPerImage": 20}
    assert [platform["id"] for platform in contract["platforms"]] == ["meituan", "jd"]
    assert contract["billing"]["generationPoints"] == 400
    assert contract["billing"]["watermarkPoints"] == WATERMARK_POINTS
    assert contract["billing"]["extraPlatformPoints"] == EXTRA_PLATFORM_POINTS
    assert contract["billing"]["totalPoints"] == 550
    assert contract["billing"]["debitOrderId"] == "gen:gen_test_001:debit"
    assert contract["billing"]["refundOrderId"] == "gen:gen_test_001:refund"
    assert (
        contract["selectedBackground"]["libraryAssetId"]
        == "asset-" + ("a" * 40)
    )
    assert contract["generationProvenance"] == TEST_GENERATION_PROVENANCE
    assert len(contract["idempotency"]["requestSha256"]) == 64
    assert contract["idempotency"]["requestSha256"] == request_sha256(contract)


def test_v12_background_prompt_version_survives_contract_freeze() -> None:
    selected = dict(valid_input()["selected_background"])
    scene = prompt_compiler.scene_contract_for(
        selected["styleId"],
        "mixed_rice",
        asset_id=selected["assetId"],
        asset_sha256=selected["sha256"],
        prompt_version=prompt_compiler.CURRENT_BACKGROUND_PROMPT_VERSION,
    )
    selected["backgroundPromptVersion"] = (
        prompt_compiler.CURRENT_BACKGROUND_PROMPT_VERSION
    )
    selected["sceneContract"] = scene.payload()

    contract = freeze_menu_batch_contract(
        **valid_input(selected_background=selected)
    )

    frozen = contract["selectedBackground"]
    assert frozen["backgroundPromptVersion"] == "style-background.v12"
    assert (
        frozen["sceneContract"]["backgroundPromptVersion"]
        == "style-background.v12"
    )
    assert frozen["sceneContract"]["contractSha256"] == scene.contract_sha256


def test_v14_background_generation_evidence_survives_contract_freeze() -> None:
    selected = v14_selected_background()

    contract = freeze_menu_batch_contract(
        **valid_input(selected_background=selected)
    )

    frozen = contract["selectedBackground"]
    assert frozen["generationEvidence"] == selected["generationEvidence"]
    assert (
        frozen["generationEvidenceSha256"]
        == selected["generationEvidenceSha256"]
    )
    assert frozen["generationEvidence"]["assetSha256"] == frozen["sha256"]


@pytest.mark.parametrize("mutation", ["missing", "tampered", "wrong_asset"])
def test_v14_background_rejects_invalid_generation_evidence(
    mutation: str,
) -> None:
    selected = v14_selected_background()
    if mutation == "missing":
        selected.pop("generationEvidence")
    elif mutation == "tampered":
        selected["generationEvidence"] = {
            **selected["generationEvidence"],
            "seed": 999,
        }
    else:
        evidence = {
            **selected["generationEvidence"],
            "assetSha256": "9" * 64,
        }
        selected["generationEvidence"] = evidence
        selected["generationEvidenceSha256"] = (
            background_catalog.generation_evidence_sha256(evidence)
        )

    with pytest.raises(BatchContractError) as raised:
        freeze_menu_batch_contract(
            **valid_input(selected_background=selected)
        )

    assert raised.value.code == "invalid_background_generation_evidence"


def test_v14_generation_evidence_is_part_of_request_hash() -> None:
    first = freeze_menu_batch_contract(
        **valid_input(selected_background=v14_selected_background(seed=111))
    )
    second = freeze_menu_batch_contract(
        **valid_input(selected_background=v14_selected_background(seed=222))
    )

    assert (
        first["idempotency"]["requestSha256"]
        != second["idempotency"]["requestSha256"]
    )


def test_signed_menu_batch_contract_blocks_v14_scene_downgrade() -> None:
    contract = freeze_menu_batch_contract(
        **valid_input(
            selected_background=v14_selected_background(),
            attestation_secret=TEST_ATTESTATION_SECRET,
        )
    )

    assert contract["contractAttestation"]["version"] == (
        CONTRACT_ATTESTATION_VERSION
    )
    assert menu_batch_contract_attestation_valid(
        contract,
        TEST_ATTESTATION_SECRET,
    )
    downgraded = copy.deepcopy(contract)
    background = downgraded["selectedBackground"]
    legacy_scene = prompt_compiler.scene_contract_for(
        background["styleId"],
        "unknown",
        asset_id=background["assetId"],
        asset_sha256=background["sha256"],
        prompt_version=prompt_compiler.LEGACY_BACKGROUND_PROMPT_VERSION,
    )
    background["backgroundPromptVersion"] = (
        prompt_compiler.LEGACY_BACKGROUND_PROMPT_VERSION
    )
    background["sceneContract"] = legacy_scene.payload()
    background.pop("generationEvidence")
    background.pop("generationEvidenceSha256")
    downgraded["idempotency"]["requestSha256"] = request_sha256(downgraded)

    assert not menu_batch_contract_attestation_valid(
        downgraded,
        TEST_ATTESTATION_SECRET,
    )


def test_signed_legacy_menu_batch_contract_remains_compatible() -> None:
    contract = freeze_menu_batch_contract(
        **valid_input(attestation_secret=TEST_ATTESTATION_SECRET)
    )

    assert contract["selectedBackground"]["backgroundPromptVersion"] == (
        prompt_compiler.LEGACY_BACKGROUND_PROMPT_VERSION
    )
    assert menu_batch_contract_attestation_valid(
        contract,
        TEST_ATTESTATION_SECRET,
    )


def test_menu_batch_attestation_cannot_be_replayed_as_revision_attestation() -> None:
    contract = freeze_menu_batch_contract(
        **valid_input(attestation_secret=TEST_ATTESTATION_SECRET)
    )
    replay = copy.deepcopy(contract)
    replay["contractAttestation"]["version"] = (
        REVISION_ATTESTATION_VERSION
    )

    assert not revision_contract_attestation_valid(
        replay,
        TEST_ATTESTATION_SECRET,
    )


def test_background_prompt_version_must_match_scene_contract() -> None:
    selected = dict(valid_input()["selected_background"])
    scene = prompt_compiler.scene_contract_for(
        selected["styleId"],
        "mixed_rice",
        asset_id=selected["assetId"],
        asset_sha256=selected["sha256"],
        prompt_version=prompt_compiler.CURRENT_BACKGROUND_PROMPT_VERSION,
    )
    selected["backgroundPromptVersion"] = "style-background.v11"
    selected["sceneContract"] = scene.payload()

    with pytest.raises(BatchContractError) as raised:
        freeze_menu_batch_contract(
            **valid_input(selected_background=selected)
        )

    assert raised.value.code == "invalid_background_prompt_version"


def test_request_hash_is_stable_across_job_ids_timestamps_and_platform_order() -> None:
    first = freeze_menu_batch_contract(
        **valid_input(
            job_id="gen_first",
            platforms=["jd", "meituan"],
            created_at="2026-07-29T12:00:00Z",
        )
    )
    second = freeze_menu_batch_contract(
        **valid_input(
            job_id="gen_second",
            platforms=["meituan", "jd"],
            created_at="2026-07-29T12:05:00Z",
        )
    )

    assert first["idempotency"]["requestSha256"] == second["idempotency"]["requestSha256"]
    assert first["billing"]["debitOrderId"] != second["billing"]["debitOrderId"]


def test_background_library_asset_identity_is_part_of_request_hash() -> None:
    first = freeze_menu_batch_contract(**valid_input())
    changed_background = dict(valid_input()["selected_background"])
    changed_background["libraryAssetId"] = "asset-" + ("b" * 40)
    second = freeze_menu_batch_contract(
        **valid_input(selected_background=changed_background)
    )

    assert (
        first["idempotency"]["requestSha256"]
        != second["idempotency"]["requestSha256"]
    )


def test_generation_provenance_is_part_of_request_hash() -> None:
    first = freeze_menu_batch_contract(**valid_input())
    changed_provenance = dict(TEST_GENERATION_PROVENANCE)
    changed_provenance["modelName"] = "hy-image-v3.1"
    second = freeze_menu_batch_contract(
        **valid_input(generation_provenance=changed_provenance)
    )

    assert (
        first["idempotency"]["requestSha256"]
        != second["idempotency"]["requestSha256"]
    )


def test_contract_does_not_mutate_caller_objects() -> None:
    payload = valid_input()
    original = copy.deepcopy(payload)

    contract = freeze_menu_batch_contract(**payload)

    assert payload == original
    contract["menu"]["objectKey"] = "changed"
    assert payload["menu"]["objectKey"] == original["menu"]["objectKey"]


@pytest.mark.parametrize(
    ("override", "code", "field"),
    [
        ({"quality": "ultra"}, "invalid_quality", "quality"),
        ({"image_count": 0}, "invalid_integer", "billing.imageCount"),
        ({"platforms": []}, "invalid_platforms", "platforms"),
        ({"platforms": ["meituan", "meituan"]}, "duplicate_platform", "platforms"),
        ({"platforms": ["unknown"]}, "unsupported_platform", "platforms"),
        (
            {"menu": {"objectKey": "../menu.xlsx", "sha256": "1" * 64, "parserVersion": 1}},
            "invalid_object_key",
            "menu.objectKey",
        ),
        (
            {"menu": {"objectKey": "menus/menu.xlsx", "sha256": "bad", "parserVersion": 1}},
            "invalid_sha256",
            "menu.sha256",
        ),
        (
            {"generation_provenance": {}},
            "invalid_identifier",
            "generationProvenance.taxonomyVersion",
        ),
    ],
)
def test_contract_rejects_invalid_frozen_inputs(override, code: str, field: str) -> None:
    with pytest.raises(BatchContractError) as raised:
        freeze_menu_batch_contract(**valid_input(**override))

    assert raised.value.code == code
    assert raised.value.field == field


def test_enabled_logo_watermark_requires_private_object_key() -> None:
    with pytest.raises(BatchContractError) as raised:
        freeze_menu_batch_contract(
            **valid_input(
                watermark={
                    "enabled": True,
                    "type": "logo",
                    "logoObjectKey": "https://public.example/logo.png",
                }
            )
        )

    assert raised.value.code == "invalid_object_key"
    assert raised.value.field == "watermark.logoObjectKey"


def test_disabled_logo_watermark_drops_untrusted_logo_url() -> None:
    contract = freeze_menu_batch_contract(
        **valid_input(
            watermark={
                "enabled": False,
                "type": "logo",
                "logoObjectKey": "https://public.example/logo.png",
            }
        )
    )

    assert contract["watermark"]["logoObjectKey"] == ""


@pytest.mark.parametrize("created_at", ["not-a-date", "2026-07-29T12:00:00"])
def test_contract_requires_timezone_aware_created_at(created_at: str) -> None:
    with pytest.raises(BatchContractError) as raised:
        freeze_menu_batch_contract(**valid_input(created_at=created_at))

    assert raised.value.code == "invalid_timestamp"
    assert raised.value.field == "createdAt"
