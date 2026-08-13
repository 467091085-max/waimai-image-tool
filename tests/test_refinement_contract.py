from __future__ import annotations

import copy
import json

import background_catalog
import pytest

from shared.refinement_contract import (
    CONTRACT_ATTESTATION_VERSION,
    MAX_REFINE_PROMPT_LENGTH,
    RefinementContractError,
    freeze_revision_batch_contract,
    public_revision_payload,
    revision_contract_attestation_valid,
    revision_request_sha256,
)


TEST_ATTESTATION_SECRET = "revision-test-signing-secret-32-bytes-minimum"


def valid_input(**overrides):
    payload = {
        "job_id": "revision_test_001",
        "parent_generation_job_id": "gen_test_001",
        "user_id": "user_test_001",
        "source_delivery_asset": {
            "assetId": "delivery_asset_001",
            "objectKey": "generated/deliveries/gen_test_001/row-7.jpg",
            "sha256": "1" * 64,
            "rowNumber": 7,
            "dishName": "招牌牛肉饭",
        },
        "selected_background": {
            "assetId": "background_asset_001",
            "objectKey": "generated/backgrounds/gen_test_001/style-2.jpg",
            "sha256": "2" * 64,
        },
        "quality": "standard",
        "mode": "rework",
        "refine_prompt": None,
        "idempotency_key": "idem_revision_001",
        "created_at": "2026-07-30T08:00:00Z",
    }
    payload.update(overrides)
    return payload


def v14_selected_background(*, seed: int = 123456) -> dict[str, object]:
    selected = dict(valid_input()["selected_background"])
    evidence = {
        "generationProvider": "tencent-hunyuan",
        "providerAction": "TokenHubImageV3",
        "model": "hy-image-v3.0",
        "seed": seed,
        "requestedSeed": seed,
        "seedApplied": True,
        "promptRevisionEnabled": False,
        "promptRevisionControlApplied": True,
        "promptVersion": "style-background.v14",
        "assetSha256": selected["sha256"],
    }
    selected.update(
        {
            "backgroundPromptVersion": "style-background.v14",
            "generationEvidence": evidence,
            "generationEvidenceSha256": (
                background_catalog.generation_evidence_sha256(evidence)
            ),
        }
    )
    return selected


def test_standard_rework_freezes_asset_bindings_and_server_price() -> None:
    contract = freeze_revision_batch_contract(**valid_input())

    assert contract["schemaVersion"] == 2
    assert contract["jobType"] == "delivery_asset_revision_batch"
    assert contract["parentGenerationJobId"] == "gen_test_001"
    assert contract["userId"] == "user_test_001"
    assert contract["sourceDeliveryAsset"]["rowNumber"] == 7
    assert contract["sourceDeliveryAsset"]["dishName"] == "招牌牛肉饭"
    assert contract["selectedBackground"] == {
        "assetId": "background_asset_001",
        "objectKey": "generated/backgrounds/gen_test_001/style-2.jpg",
        "sha256": "2" * 64,
    }
    assert contract["providerSnapshot"] == {
        "provider": "google-gemini",
        "model": "gemini-3.1-flash-image",
        "apiSurface": "interactions-v1beta",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/interactions",
        "promptVersion": "food-refinement.v1",
        "output": {
            "mimeType": "image/jpeg",
            "imageSize": "1K",
            "aspectRatio": "source-nearest-supported",
            "canvasNormalization": "same-as-source.v1",
        },
    }
    assert contract["quality"] == {"id": "standard"}
    assert contract["mode"] == "rework"
    assert contract["refinePrompt"] == ""
    assert contract["billing"]["basePoints"] == 10
    assert contract["billing"]["totalPoints"] == 10
    assert contract["billing"]["freeReworkQuotaVerified"] is False
    assert contract["billing"]["debitOrderId"] == "revision:revision_test_001:debit"
    assert contract["billing"]["refundOrderId"] == "revision:revision_test_001:refund"
    assert contract["idempotency"]["requestSha256"] == revision_request_sha256(contract)


def test_v14_background_generation_evidence_survives_revision_freeze() -> None:
    selected = v14_selected_background()

    contract = freeze_revision_batch_contract(
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
def test_v14_revision_rejects_invalid_background_generation_evidence(
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

    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(
            **valid_input(selected_background=selected)
        )

    assert raised.value.code == "invalid_background_generation_evidence"


def test_v14_background_generation_evidence_changes_revision_digest() -> None:
    first = freeze_revision_batch_contract(
        **valid_input(selected_background=v14_selected_background(seed=111))
    )
    second = freeze_revision_batch_contract(
        **valid_input(selected_background=v14_selected_background(seed=222))
    )

    assert (
        first["idempotency"]["requestSha256"]
        != second["idempotency"]["requestSha256"]
    )


def test_provider_snapshot_is_part_of_revision_request_hash() -> None:
    contract = freeze_revision_batch_contract(**valid_input())
    changed = copy.deepcopy(contract)
    changed["providerSnapshot"]["model"] = "gemini-3-pro-image-preview"

    assert revision_request_sha256(changed) != contract["idempotency"]["requestSha256"]


def test_signed_revision_contract_blocks_version_downgrade() -> None:
    contract = freeze_revision_batch_contract(
        **valid_input(
            selected_background=v14_selected_background(),
            attestation_secret=TEST_ATTESTATION_SECRET,
        )
    )

    assert contract["contractAttestation"]["version"] == (
        CONTRACT_ATTESTATION_VERSION
    )
    assert revision_contract_attestation_valid(
        contract,
        TEST_ATTESTATION_SECRET,
    )
    downgraded = copy.deepcopy(contract)
    downgraded["selectedBackground"].pop("backgroundPromptVersion")
    downgraded["selectedBackground"].pop("generationEvidence")
    downgraded["selectedBackground"].pop("generationEvidenceSha256")
    downgraded["idempotency"]["requestSha256"] = revision_request_sha256(
        downgraded
    )

    assert not revision_contract_attestation_valid(
        downgraded,
        TEST_ATTESTATION_SECRET,
    )


def test_signed_legacy_background_contract_remains_compatible() -> None:
    contract = freeze_revision_batch_contract(
        **valid_input(attestation_secret=TEST_ATTESTATION_SECRET)
    )

    assert "backgroundPromptVersion" not in contract["selectedBackground"]
    assert revision_contract_attestation_valid(
        contract,
        TEST_ATTESTATION_SECRET,
    )
    assert "contractAttestation" not in public_revision_payload(contract)


def test_premium_rework_is_server_priced_at_twenty_points() -> None:
    contract = freeze_revision_batch_contract(**valid_input(quality="premium"))

    assert contract["billing"]["basePoints"] == 20
    assert contract["billing"]["totalPoints"] == 20


@pytest.mark.parametrize("quality", ["standard", "premium"])
def test_refine_is_ten_points_and_normalizes_prompt(quality: str) -> None:
    contract = freeze_revision_batch_contract(
        **valid_input(
            quality=quality,
            mode="refine",
            refine_prompt="  把盘子换成全角Ａ款。\n  菜品更亮一些  ",
        )
    )

    assert contract["refinePrompt"] == "把盘子换成全角A款。 菜品更亮一些"
    assert contract["billing"]["basePoints"] == 10
    assert contract["billing"]["totalPoints"] == 10


def test_verified_free_rework_is_zero_points_and_auditable() -> None:
    contract = freeze_revision_batch_contract(
        **valid_input(
            quality="premium",
            free_rework_quota_verified=True,
        )
    )

    assert contract["billing"]["basePoints"] == 20
    assert contract["billing"]["totalPoints"] == 0
    assert contract["billing"]["freeReworkQuotaVerified"] is True


@pytest.mark.parametrize(
    ("override", "code", "field"),
    [
        ({"mode": "refine", "refine_prompt": ""}, "missing_refine_prompt", "refinePrompt"),
        (
            {"mode": "rework", "refine_prompt": "暗中夹带修改要求"},
            "unexpected_refine_prompt",
            "refinePrompt",
        ),
        (
            {"mode": "refine", "refine_prompt": "x" * (MAX_REFINE_PROMPT_LENGTH + 1)},
            "refine_prompt_too_long",
            "refinePrompt",
        ),
        (
            {"mode": "refine", "refine_prompt": "放大主体\u200b"},
            "invalid_text_control",
            "refinePrompt",
        ),
        ({"mode": "replace"}, "invalid_mode", "mode"),
        ({"quality": "ultra"}, "invalid_quality", "quality"),
        (
            {
                "mode": "refine",
                "refine_prompt": "放大主体",
                "free_rework_quota_verified": True,
            },
            "invalid_free_rework",
            "billing.freeReworkQuotaVerified",
        ),
        (
            {"free_rework_quota_verified": 1},
            "invalid_boolean",
            "billing.freeReworkQuotaVerified",
        ),
    ],
)
def test_contract_rejects_invalid_revision_rules(override, code: str, field: str) -> None:
    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(**valid_input(**override))

    assert raised.value.code == code
    assert raised.value.field == field


@pytest.mark.parametrize(
    ("field_name", "value", "error_field"),
    [
        ("job_id", "../revision", "jobId"),
        ("parent_generation_job_id", "https://example.test/job", "parentGenerationJobId"),
        ("user_id", "", "userId"),
        ("idempotency_key", "key with spaces", "idempotency.key"),
        ("debit_order_id", "../debit", "billing.debitOrderId"),
        ("debit_order_id", "", "billing.debitOrderId"),
        ("refund_order_id", "refund/path", "billing.refundOrderId"),
        ("refund_order_id", "", "billing.refundOrderId"),
    ],
)
def test_contract_strictly_validates_identifiers(
    field_name: str,
    value,
    error_field: str,
) -> None:
    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(**valid_input(**{field_name: value}))

    assert raised.value.code == "invalid_identifier"
    assert raised.value.field == error_field


@pytest.mark.parametrize(
    ("container_name", "field_name", "value", "error_field"),
    [
        (
            "source_delivery_asset",
            "assetId",
            "asset/one",
            "sourceDeliveryAsset.assetId",
        ),
        (
            "source_delivery_asset",
            "objectKey",
            "../private.jpg",
            "sourceDeliveryAsset.objectKey",
        ),
        (
            "source_delivery_asset",
            "objectKey",
            "https://cdn.example/private.jpg",
            "sourceDeliveryAsset.objectKey",
        ),
        (
            "source_delivery_asset",
            "sha256",
            "not-a-digest",
            "sourceDeliveryAsset.sha256",
        ),
        (
            "selected_background",
            "assetId",
            "background/one",
            "selectedBackground.assetId",
        ),
        (
            "selected_background",
            "objectKey",
            "backgrounds//image.jpg",
            "selectedBackground.objectKey",
        ),
        (
            "selected_background",
            "sha256",
            "a" * 63,
            "selectedBackground.sha256",
        ),
    ],
)
def test_contract_rejects_malicious_asset_references(
    container_name: str,
    field_name: str,
    value,
    error_field: str,
) -> None:
    payload = valid_input()
    payload[container_name] = {
        **payload[container_name],
        field_name: value,
    }

    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(**payload)

    assert raised.value.field == error_field


@pytest.mark.parametrize("row_number", [True, 0, -1, 1.0, "1", None])
def test_row_number_requires_a_strict_positive_integer(row_number) -> None:
    source = {
        **valid_input()["source_delivery_asset"],
        "rowNumber": row_number,
    }

    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(
            **valid_input(source_delivery_asset=source)
        )

    assert raised.value.code == "invalid_integer"
    assert raised.value.field == "sourceDeliveryAsset.rowNumber"


@pytest.mark.parametrize("dish_name", ["", " \n ", 123, "菜名\u0000注入", "菜" * 161])
def test_dish_name_is_required_bounded_text(dish_name) -> None:
    source = {
        **valid_input()["source_delivery_asset"],
        "dishName": dish_name,
    }

    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(
            **valid_input(source_delivery_asset=source)
        )

    assert raised.value.field == "sourceDeliveryAsset.dishName"


def test_contract_does_not_alias_or_mutate_caller_assets() -> None:
    payload = valid_input()
    original = copy.deepcopy(payload)

    contract = freeze_revision_batch_contract(**payload)
    contract["sourceDeliveryAsset"]["dishName"] = "changed"
    contract["selectedBackground"]["assetId"] = "changed"

    assert payload == original


def test_request_digest_is_stable_for_same_logical_request() -> None:
    first = freeze_revision_batch_contract(**valid_input())
    second = freeze_revision_batch_contract(
        **valid_input(
            job_id="revision_test_002",
            debit_order_id="revision:revision_test_002:debit",
            refund_order_id="revision:revision_test_002:refund",
            created_at="2026-07-30T09:00:00Z",
        )
    )

    assert first["idempotency"]["requestSha256"] == second["idempotency"]["requestSha256"]


@pytest.mark.parametrize(
    "override",
    [
        {"parent_generation_job_id": "gen_test_002"},
        {"user_id": "user_test_002"},
        {
            "source_delivery_asset": {
                **valid_input()["source_delivery_asset"],
                "assetId": "delivery_asset_002",
            }
        },
        {
            "source_delivery_asset": {
                **valid_input()["source_delivery_asset"],
                "objectKey": "generated/deliveries/gen_test_001/row-8.jpg",
            }
        },
        {
            "source_delivery_asset": {
                **valid_input()["source_delivery_asset"],
                "sha256": "3" * 64,
            }
        },
        {
            "source_delivery_asset": {
                **valid_input()["source_delivery_asset"],
                "rowNumber": 8,
            }
        },
        {
            "source_delivery_asset": {
                **valid_input()["source_delivery_asset"],
                "dishName": "招牌鸡肉饭",
            }
        },
        {
            "selected_background": {
                **valid_input()["selected_background"],
                "assetId": "background_asset_002",
            }
        },
        {
            "selected_background": {
                **valid_input()["selected_background"],
                "objectKey": "generated/backgrounds/gen_test_001/style-3.jpg",
            }
        },
        {
            "selected_background": {
                **valid_input()["selected_background"],
                "sha256": "4" * 64,
            }
        },
        {"quality": "premium"},
        {"pricing_version": "revision-v2"},
        {"free_rework_quota_verified": True},
    ],
)
def test_each_critical_request_change_changes_digest(override) -> None:
    baseline = freeze_revision_batch_contract(**valid_input())
    changed = freeze_revision_batch_contract(**valid_input(**override))

    assert (
        baseline["idempotency"]["requestSha256"]
        != changed["idempotency"]["requestSha256"]
    )


def test_mode_and_normalized_prompt_are_digest_boundaries() -> None:
    rework = freeze_revision_batch_contract(**valid_input())
    refine = freeze_revision_batch_contract(
        **valid_input(mode="refine", refine_prompt="把主体放大")
    )
    other_refine = freeze_revision_batch_contract(
        **valid_input(mode="refine", refine_prompt="把主体缩小")
    )

    assert rework["idempotency"]["requestSha256"] != refine["idempotency"]["requestSha256"]
    assert refine["idempotency"]["requestSha256"] != other_refine["idempotency"]["requestSha256"]


def test_custom_order_ids_are_validated_and_preserved() -> None:
    contract = freeze_revision_batch_contract(
        **valid_input(
            debit_order_id="billing:revision-001:debit",
            refund_order_id="billing:revision-001:refund",
        )
    )

    assert contract["billing"]["debitOrderId"] == "billing:revision-001:debit"
    assert contract["billing"]["refundOrderId"] == "billing:revision-001:refund"

    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(
            **valid_input(
                debit_order_id="billing:same",
                refund_order_id="billing:same",
            )
        )
    assert raised.value.code == "duplicate_order_id"


def test_public_payload_does_not_leak_private_object_keys() -> None:
    contract = freeze_revision_batch_contract(
        **valid_input(mode="refine", refine_prompt="背景更亮，主体放大")
    )

    public = public_revision_payload(contract)
    serialized = json.dumps(public, ensure_ascii=False)

    assert "objectKey" not in serialized
    assert contract["sourceDeliveryAsset"]["objectKey"] not in serialized
    assert contract["selectedBackground"]["objectKey"] not in serialized
    assert public["sourceDeliveryAsset"]["assetId"] == "delivery_asset_001"
    assert public["selectedBackground"]["assetId"] == "background_asset_001"
    assert public["idempotency"]["requestSha256"] == revision_request_sha256(contract)


@pytest.mark.parametrize("created_at", ["not-a-date", "2026-07-30T08:00:00", 123])
def test_created_at_requires_timezone_aware_timestamp(created_at) -> None:
    with pytest.raises(RefinementContractError) as raised:
        freeze_revision_batch_contract(**valid_input(created_at=created_at))

    assert raised.value.code == "invalid_timestamp"
    assert raised.value.field == "createdAt"
