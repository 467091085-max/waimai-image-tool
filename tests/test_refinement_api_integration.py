from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

import app as app_module
import object_storage_service
from shared.redis_queue import (
    IdempotencyConflict,
    QueueError,
    RedisQueueConfig,
    RedisTaskQueue,
    TaskNotFound,
)
from shared.refinement_contract import freeze_revision_batch_contract
from tests.redis_test_double import RedisTestDouble


USER_ID = "server-user"
PARENT_JOB_ID = "generation-parent"
SOURCE_ASSET_ID = "asset_" + ("a" * 32)


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color=color).save(output, "PNG")
    return output.getvalue()


def _principal() -> tuple[dict, None]:
    return (
        {
            "userId": USER_ID,
            "internal": False,
            "localDemo": False,
        },
        None,
    )


def _parent_record(*, image_count: int = 3) -> dict:
    return {
        "id": PARENT_JOB_ID,
        "menu_upload_id": "menu_" + ("b" * 32),
        "style_id": "style-1",
        "status": "succeeded",
        "request": {
            "jobType": "menu_batch_generation",
            "jobId": PARENT_JOB_ID,
            "userId": USER_ID,
            "quality": {"id": "standard"},
            "billing": {"imageCount": image_count},
        },
    }


def _source_snapshot() -> dict:
    return {
        "assetId": SOURCE_ASSET_ID,
        "objectKey": "generated/delivery/parent/source.png",
        "sha256": "1" * 64,
        "rowNumber": 1,
        "dishName": "宫保鸡丁",
    }


def _background_snapshot() -> dict:
    return {
        "assetId": "background_asset",
        "objectKey": "generated/backgrounds/parent/background.png",
        "sha256": "2" * 64,
    }


def _queue() -> RedisTaskQueue:
    return RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(
            namespace="revision-api-test",
            queue_name="product-generate",
        ),
    )


def _post_payload(**overrides) -> dict:
    payload = {
        "parentGenerationJobId": PARENT_JOB_ID,
        "sourceAssetId": SOURCE_ASSET_ID,
        "mode": "rework",
        "idempotencyKey": "browser-revision-key",
    }
    payload.update(overrides)
    return payload


def _existing_contract(*, mode: str = "rework", prompt: str | None = None) -> dict:
    job_id = app_module.revision_job_id(
        user_id=USER_ID,
        idempotency_key="browser-revision-key",
    )
    return freeze_revision_batch_contract(
        job_id=job_id,
        parent_generation_job_id=PARENT_JOB_ID,
        user_id=USER_ID,
        source_delivery_asset=_source_snapshot(),
        selected_background=_background_snapshot(),
        quality="standard",
        mode=mode,
        refine_prompt=prompt,
        idempotency_key="browser-revision-key",
        free_rework_quota_verified=mode == "rework",
        created_at="2026-07-30T08:00:00Z",
    )


def test_revision_create_fails_before_debit_when_provider_is_not_ready() -> None:
    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            side_effect=TaskNotFound("missing"),
        ),
        mock.patch.object(
            app_module,
            "gemini_image_edit_readiness",
            return_value={
                "ready": False,
                "missingConfig": ["GEMINI_API_KEY"],
                "blockingIssues": ["gemini_image_edit_api_key_required"],
            },
        ),
        mock.patch.object(app_module.billing, "debit_account") as debit,
        mock.patch.object(
            app_module,
            "resolve_parent_delivery_asset_snapshot",
        ) as resolve_source,
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(),
        )

    assert response.status_code == 503
    assert response.get_json()["code"] == "image_refinement_provider_not_ready"
    debit.assert_not_called()
    resolve_source.assert_not_called()


def test_free_rework_is_server_counted_and_enqueued_without_debit() -> None:
    queue = _queue()
    persisted: list[dict] = []

    def capture_persist(contract, **_kwargs):
        persisted.append(contract)
        return {"status": "queued", "request": contract}, True

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            side_effect=TaskNotFound("missing"),
        ),
        mock.patch.object(
            app_module,
            "gemini_image_edit_readiness",
            return_value={"ready": True},
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module,
            "resolve_parent_delivery_asset_snapshot",
            return_value=(
                _parent_record(),
                _source_snapshot(),
                _background_snapshot(),
            ),
        ),
        mock.patch.object(
            app_module,
            "revision_free_rework_usage",
            return_value=0,
        ),
        mock.patch.object(
            app_module,
            "persist_revision_batch_contract",
            side_effect=capture_persist,
        ),
        mock.patch.object(app_module, "account_payload", return_value={"balance": 100}),
        mock.patch.object(app_module.billing, "debit_account") as debit,
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(
                points=1,
                quality="premium",
                freeReworkQuotaVerified=False,
                objectKey="../attacker.png",
                userId="attacker",
            ),
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "queued"
    assert body["revision"]["billing"]["totalPoints"] == 0
    assert body["revision"]["billing"]["freeReworkQuotaVerified"] is True
    assert "objectKey" not in str(body)
    debit.assert_not_called()
    assert len(persisted) == 1
    task = queue.get(body["jobId"])
    assert task["payload"]["taskType"] == "product_revision"
    assert task["payload"]["revisionContract"]["sourceDeliveryAsset"] == _source_snapshot()
    assert task["owner_user_id"] == USER_ID


def test_custom_refine_uses_server_price_and_normalized_prompt() -> None:
    queue = _queue()
    debit = {"orderId": "server-order", "idempotent": False, "balanceAfter": 90}
    persisted_contracts: list[dict] = []

    def capture_persist(contract, **_kwargs):
        persisted_contracts.append(contract)
        return {"status": "queued", "request": contract}, True

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            side_effect=TaskNotFound("missing"),
        ),
        mock.patch.object(
            app_module,
            "gemini_image_edit_readiness",
            return_value={"ready": True},
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module,
            "resolve_parent_delivery_asset_snapshot",
            return_value=(
                _parent_record(image_count=80),
                _source_snapshot(),
                _background_snapshot(),
            ),
        ),
        mock.patch.object(
            app_module,
            "revision_free_rework_usage",
            return_value=999,
        ),
        mock.patch.object(
            app_module,
            "persist_revision_batch_contract",
            side_effect=capture_persist,
        ),
        mock.patch.object(
            app_module.billing,
            "debit_account",
            return_value=debit,
        ) as debit_call,
        mock.patch.object(app_module, "account_payload", return_value={"balance": 90}),
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(
                mode="refine",
                prompt="  菜品更亮。\n不要改变背景  ",
                points=1,
                freeReworkQuotaVerified=True,
            ),
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["revision"]["refinePrompt"] == "菜品更亮。 不要改变背景"
    assert body["revision"]["billing"]["totalPoints"] == 10
    assert debit_call.call_args.args[2] == 10
    assert persisted_contracts[0]["quality"]["id"] == "standard"


def test_idempotent_replay_does_not_debit_or_reenqueue() -> None:
    contract = _existing_contract()
    record = {"status": "queued", "request": contract}

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            return_value=(record, contract),
        ),
        mock.patch.object(app_module.billing, "debit_account") as debit,
        mock.patch.object(app_module, "product_redis_queue") as product_queue,
        mock.patch.object(app_module, "account_payload", return_value={"balance": 100}),
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(),
        )

    assert response.status_code == 200
    assert response.get_json()["idempotent"] is True
    debit.assert_not_called()
    product_queue.assert_not_called()


def test_idempotent_replay_rejects_a_different_source_asset() -> None:
    contract = _existing_contract()
    record = {"status": "queued", "request": contract}

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            return_value=(record, contract),
        ),
        mock.patch.object(app_module.billing, "debit_account") as debit,
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(sourceAssetId="revision_" + ("b" * 32)),
        )

    assert response.status_code == 409
    assert response.get_json()["code"] == "idempotency_conflict"
    debit.assert_not_called()


def test_enqueue_failure_refunds_only_original_paid_revision() -> None:
    failing_queue = SimpleNamespace(
        enqueue_idempotent=mock.Mock(side_effect=QueueError("queue unavailable")),
        get=mock.Mock(side_effect=TaskNotFound("missing")),
    )
    contract_updates: list[dict] = []

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            side_effect=TaskNotFound("missing"),
        ),
        mock.patch.object(
            app_module,
            "gemini_image_edit_readiness",
            return_value={"ready": True},
        ),
        mock.patch.object(
            app_module,
            "product_redis_queue",
            return_value=failing_queue,
        ),
        mock.patch.object(
            app_module,
            "resolve_parent_delivery_asset_snapshot",
            return_value=(
                _parent_record(),
                _source_snapshot(),
                _background_snapshot(),
            ),
        ),
        mock.patch.object(
            app_module,
            "revision_free_rework_usage",
            return_value=99,
        ),
        mock.patch.object(
            app_module,
            "persist_revision_batch_contract",
            return_value=({"status": "queued"}, True),
        ),
        mock.patch.object(
            app_module.billing,
            "debit_account",
            return_value={"idempotent": False},
        ),
        mock.patch.object(app_module, "refund_revision_batch") as refund,
        mock.patch.object(
            app_module,
            "update_persisted_generation_job",
            side_effect=lambda _job_id, **updates: contract_updates.append(
                updates
            ),
        ),
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(),
        )

    assert response.status_code == 503
    assert response.get_json()["code"] == "generation_queue_unavailable"
    refund.assert_called_once()
    assert refund.call_args.kwargs["reason"] == "enqueue_failed"
    assert contract_updates[-1]["status"] == "failed"


def test_enqueue_idempotency_conflict_refunds_fresh_non_postgres_debit() -> None:
    failing_queue = SimpleNamespace(
        enqueue_idempotent=mock.Mock(
            side_effect=IdempotencyConflict(
                task_id="other-revision",
                request_sha256="b" * 64,
            )
        ),
    )
    contract_updates: list[dict] = []

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            side_effect=TaskNotFound("missing"),
        ),
        mock.patch.object(
            app_module,
            "gemini_image_edit_readiness",
            return_value={"ready": True},
        ),
        mock.patch.object(
            app_module,
            "product_redis_queue",
            return_value=failing_queue,
        ),
        mock.patch.object(
            app_module,
            "resolve_parent_delivery_asset_snapshot",
            return_value=(
                _parent_record(),
                _source_snapshot(),
                _background_snapshot(),
            ),
        ),
        mock.patch.object(
            app_module,
            "revision_free_rework_usage",
            return_value=99,
        ),
        mock.patch.object(
            app_module,
            "persist_revision_batch_contract",
            return_value=({"status": "queued"}, True),
        ),
        mock.patch.object(
            app_module.billing,
            "debit_account",
            return_value={"idempotent": False},
        ),
        mock.patch.object(app_module, "refund_revision_batch") as refund,
        mock.patch.object(
            app_module,
            "update_persisted_generation_job",
            side_effect=lambda _job_id, **updates: contract_updates.append(
                updates
            ),
        ),
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(),
        )

    assert response.status_code == 409
    assert response.get_json()["code"] == "idempotency_conflict"
    refund.assert_called_once()
    assert refund.call_args.kwargs["reason"] == "idempotency_conflict"
    assert contract_updates[-1]["status"] == "failed"


def test_source_snapshot_requires_asset_row_binding_and_background_sha(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    source = _png_bytes((190, 50, 35))
    background = _png_bytes((235, 228, 214))
    source_key = "generated/delivery/parent/source.png"
    background_key = "generated/backgrounds/parent/background.png"
    storage.put_bytes(source, object_key=source_key)
    storage.put_bytes(background, object_key=background_key)
    contract = {
        "jobType": "menu_batch_generation",
        "jobId": PARENT_JOB_ID,
        "userId": USER_ID,
        "selectedBackground": {
            "assetId": "background_asset",
            "objectKey": background_key,
            "sha256": hashlib.sha256(background).hexdigest(),
        },
    }
    record = {
        "status": "succeeded",
        "request": contract,
        "result": {"manifest": {"objectKey": "private-manifest"}},
    }
    document = {
        "deliveryAssets": [
            {
                "assetId": SOURCE_ASSET_ID,
                "objectKey": source_key,
                "sha256": hashlib.sha256(source).hexdigest(),
                "row": 7,
            }
        ],
        "results": [
            {
                "row": 7,
                "name": "宫保鸡丁",
                "candidates": [{"deliveryAssetId": SOURCE_ASSET_ID}],
            }
        ],
    }

    with (
        mock.patch.object(
            app_module,
            "persisted_generation_contract",
            return_value=(record, contract),
        ),
        mock.patch.object(
            app_module,
            "load_generation_result_manifest_document",
            return_value=document,
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    ):
        _record, source_snapshot, background_snapshot = (
            app_module.resolve_parent_delivery_asset_snapshot(
                PARENT_JOB_ID,
                SOURCE_ASSET_ID,
                _principal()[0],
            )
        )

    assert source_snapshot["rowNumber"] == 7
    assert source_snapshot["dishName"] == "宫保鸡丁"
    assert source_snapshot["sha256"] == hashlib.sha256(source).hexdigest()
    assert background_snapshot["sha256"] == hashlib.sha256(background).hexdigest()


def test_selected_background_snapshot_rejects_oversized_object_before_body_read() -> None:
    storage = mock.Mock()
    storage.read_bytes_limited.side_effect = (
        object_storage_service.ObjectStorageReadLimitExceeded(
            "object exceeds read limit"
        )
    )
    contract = {
        "selectedBackground": {
            "assetId": "background_asset",
            "objectKey": "generated/backgrounds/parent/background.png",
            "sha256": "a" * 64,
        }
    }

    with mock.patch.object(
        app_module.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        with pytest.raises(app_module.MenuUploadError) as raised:
            app_module.verified_selected_background_snapshot(contract)

    assert raised.value.code == "selected_background_too_large"
    storage.read_bytes_limited.assert_called_once_with(
        "generated/backgrounds/parent/background.png",
        app_module.MAX_AI_ASSET_BYTES,
    )
    storage.read_bytes.assert_not_called()


@pytest.mark.parametrize("mode", ["", "unknown", "refine"])
def test_invalid_or_promptless_modes_never_debit(mode: str) -> None:
    queue = _queue()
    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            side_effect=TaskNotFound("missing"),
        ),
        mock.patch.object(
            app_module,
            "gemini_image_edit_readiness",
            return_value={"ready": True},
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module,
            "resolve_parent_delivery_asset_snapshot",
            return_value=(
                _parent_record(),
                _source_snapshot(),
                _background_snapshot(),
            ),
        ),
        mock.patch.object(
            app_module,
            "revision_free_rework_usage",
            return_value=99,
        ),
        mock.patch.object(app_module.billing, "debit_account") as debit,
    ):
        response = app_module.app.test_client().post(
            "/api/image-refinements",
            json=_post_payload(mode=mode),
        )

    assert response.status_code == 400
    debit.assert_not_called()


def test_refinement_readiness_blocks_live_without_gemini_key() -> None:
    with mock.patch.dict(
        app_module.os.environ,
        {
            "APP_ENV": "render",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        },
        clear=False,
    ):
        readiness = app_module.image_refinement_readiness()

    assert readiness["ready"] is False
    assert readiness["liveRequired"] is True
    assert readiness["providerConfigured"] is False
    assert "gemini_image_edit_api_key_required" in readiness["blockingIssues"]


def test_refinement_readiness_is_green_with_configured_gemini_and_live_worker() -> None:
    queue = mock.Mock()
    queue.service_liveness.return_value = {
        "queueName": "product-revision",
        "ageMs": 120,
        "ttlSeconds": 30,
    }
    with (
        mock.patch.dict(
            app_module.os.environ,
            {
                "APP_ENV": "render",
                "GEMINI_API_KEY": "test-key",
                "GOOGLE_API_KEY": "",
                "REDIS_URL": "redis://test.invalid/0",
                "REDIS_REVISION_QUEUE": "product-revision",
                "REVISION_WORKER_ENABLED": "true",
                "REVISION_WORKER_SERVICE_ID": "revision-worker",
            },
            clear=False,
        ),
        mock.patch.object(
            app_module,
            "redis_revision_queue_from_env",
            return_value=queue,
        ),
    ):
        readiness = app_module.image_refinement_readiness()
        report = app_module.deployment_config_report()

    assert readiness["ready"] is True
    section = next(
        value
        for value in report["sections"]
        if value["id"] == "imageRefinement"
    )
    key_item = next(
        value for value in section["items"] if value["key"] == "gemini_api_key"
    )
    assert section["ready"] is True
    assert key_item["sensitive"] is True
    assert key_item["value"] == "configured"
    assert "test-key" not in str(report)
