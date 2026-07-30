from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import app as app_module
import object_storage_service
from shared.batch_contract import freeze_menu_batch_contract
from shared.redis_queue import QueueError, RedisQueueConfig, RedisTaskQueue
from tests.redis_test_double import RedisTestDouble
from worker.worker import GenerationWorker, dispatch_generation


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color=color).save(output, "PNG")
    return output.getvalue()


def _contract(*, job_id: str = "generation-product-redis") -> dict:
    return freeze_menu_batch_contract(
        job_id=job_id,
        user_id="server-user",
        menu_upload_id="menu_" + ("a" * 32),
        menu={
            "objectKey": "menus/menu.xlsx",
            "sha256": "1" * 64,
            "parserVersion": 1,
        },
        selected_background={
            "assetId": "bg_test",
            "styleId": "style-1",
            "sha256": "2" * 64,
            "objectKey": "generated/selected-backgrounds/bg_test/image",
            "width": 1024,
            "height": 768,
        },
        generation_provenance=(
            app_module.generation_provenance_snapshot()
        ),
        quality="standard",
        image_count=3,
        platforms=["meituan"],
        watermark={"enabled": False},
        idempotency_key="browser-key",
        created_at="2026-07-29T12:00:00Z",
    )


def _queue() -> RedisTaskQueue:
    return RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(namespace="product-test", queue_name="product-generate"),
    )


def _enqueue_contract(
    queue: RedisTaskQueue,
    contract: dict,
) -> dict:
    return queue.enqueue_idempotent(
        {"taskType": "product_batch", "batchContract": contract},
        user_id=str(contract["userId"]),
        idempotency_key=str(contract["idempotency"]["key"]),
        request_sha256=str(contract["idempotency"]["requestSha256"]),
        task_id=str(contract["jobId"]),
    )


def _principal(user_id: str = "server-user") -> tuple[dict, None]:
    return (
        {
            "userId": user_id,
            "internal": False,
            "localDemo": False,
        },
        None,
    )


def test_authenticated_generation_uses_product_redis_without_web_ai(
    tmp_path: Path,
) -> None:
    queue = _queue()
    contract = _contract()
    selected_background = SimpleNamespace(
        public_payload=lambda: {"assetId": "bg_test", "sha256": "2" * 64}
    )
    menu_snapshot = {
        "id": contract["menuUploadId"],
        "objectKey": contract["menu"]["objectKey"],
        "sha256": contract["menu"]["sha256"],
        "parserVersion": 1,
        "summary": {"count": 3},
    }
    forbidden_memory_queue = SimpleNamespace(
        get=mock.Mock(side_effect=AssertionError("memory queue must not be read")),
        enqueue=mock.Mock(side_effect=AssertionError("web must not execute AI")),
    )

    with (
        mock.patch.object(app_module, "generation_queue", forbidden_memory_queue),
        mock.patch.object(app_module, "generation_request_principal", return_value=_principal()),
        mock.patch.object(app_module, "resolve_menu_upload_snapshot", return_value=menu_snapshot),
        mock.patch.object(
            app_module,
            "materialize_menu_upload_snapshot",
            return_value=tmp_path / "menu.xlsx",
        ),
        mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
        mock.patch.object(app_module, "requested_selected_background", return_value=selected_background),
        mock.patch.object(
            app_module,
            "selected_background_batch_snapshot",
            return_value=contract["selectedBackground"],
        ),
        mock.patch.object(app_module, "batch_watermark_snapshot", return_value={"enabled": False}),
        mock.patch.object(app_module, "tencent_ready", return_value=True),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module.billing,
            "debit_account",
            return_value={"idempotent": False, "balance": 970},
        ),
        mock.patch.object(
            app_module,
            "persist_generation_batch_contract",
            return_value=({"status": "queued"}, True),
        ),
        mock.patch.object(app_module, "account_payload", return_value={"balance": 970}),
        mock.patch.object(
            app_module,
            "generation_job_id",
            return_value=contract["jobId"],
        ),
    ):
        response = app_module.app.test_client().post(
            "/api/generation-jobs",
            json={
                "style": "style-1",
                "quality": "standard",
                "menuUploadId": contract["menuUploadId"],
                "idempotencyKey": "browser-key",
                "platforms": ["meituan"],
                "watermark": {"enabled": False},
            },
        )

    assert response.status_code == 200
    assert response.get_json()["jobId"] == contract["jobId"]
    task = queue.get(contract["jobId"])
    assert task["status"] == "pending"
    assert task["payload"]["taskType"] == "product_batch"
    assert task["owner_user_id"] == "server-user"
    assert forbidden_memory_queue.enqueue.call_count == 0


def test_completed_product_task_settles_and_hides_private_manifest_pointer() -> None:
    queue = _queue()
    contract = _contract()
    _enqueue_contract(queue, contract)
    claim = queue.claim(worker_id="worker-a", lease_ms=10_000, timeout_seconds=0)
    assert claim is not None
    queue.ack_done(
        contract["jobId"],
        receipt=claim["receipt"],
        lease_token=claim["lease_token"],
        image_url=f"/api/generation-jobs/{contract['jobId']}/manifest",
        result={
            "manifest_object_key": "generated/manifests/private.json",
            "manifest_sha256": "3" * 64,
            "manifest_size": 321,
            "request_sha256": contract["idempotency"]["requestSha256"],
        },
    )
    public_result = {
        "generation": {"succeeded": 2, "failed": 1},
        "generationBatch": {
            "requestSha256": contract["idempotency"]["requestSha256"],
            "refundedPoints": 10,
        },
        "results": [],
    }

    with (
        mock.patch.object(app_module, "generation_request_principal", return_value=_principal()),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module,
            "load_generation_result_manifest_pointer",
            return_value=public_result,
        ),
        mock.patch.object(app_module, "refund_generation_batch") as refund,
        mock.patch.object(app_module, "settle_persisted_generation_job") as settle,
        mock.patch.object(app_module, "account_payload", return_value={"balance": 980}),
    ):
        response = app_module.app.test_client().get(
            f"/api/generation-jobs/{contract['jobId']}"
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "completed"
    assert body["result"]["generation"]["succeeded"] == 2
    assert body["result"]["account"]["balance"] == 980
    assert "manifest_object_key" not in str(body)
    refund.assert_called_once()
    assert refund.call_args.kwargs["points"] == 10
    settle.assert_called_once()
    assert settle.call_args.kwargs["status"] == "succeeded"


def test_generation_asset_route_reads_shared_storage_without_exposing_key(
    tmp_path: Path,
) -> None:
    contract = _contract()
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    raw = _png_bytes((190, 50, 35))
    object_key = "generated/delivery/private/final.png"
    storage.put_bytes(raw, object_key=object_key)
    asset_id = "asset_" + ("a" * 32)
    result_document = {
        "generationBatch": {
            "requestSha256": contract["idempotency"]["requestSha256"],
        },
        "deliveryAssets": [
            {
                "assetId": asset_id,
                "objectKey": object_key,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        ],
    }

    with (
        mock.patch.object(app_module, "generation_request_principal", return_value=_principal()),
        mock.patch.object(
            app_module,
            "load_persisted_generation_manifest_document",
            return_value=result_document,
        ) as load_manifest,
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    ):
        response = app_module.app.test_client().get(
            f"/api/generation-jobs/{contract['jobId']}/assets/{asset_id}"
        )

    assert response.status_code == 200
    assert response.data == raw
    assert response.headers["Cache-Control"] == "private, max-age=300"
    assert object_key not in response.request.path
    load_manifest.assert_called_once()
    assert load_manifest.call_args.args[1]["userId"] == "server-user"


def test_product_task_status_is_hidden_from_another_user() -> None:
    queue = _queue()
    contract = _contract()
    _enqueue_contract(queue, contract)

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal("other-user"),
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
    ):
        response = app_module.app.test_client().get(
            f"/api/generation-jobs/{contract['jobId']}"
        )

    assert response.status_code == 404


def test_missing_redis_task_is_republished_from_persisted_contract() -> None:
    queue = _queue()
    contract = _contract(job_id="generation-outbox-recovery")
    record = {"id": contract["jobId"], "status": "queued", "request": contract}

    with mock.patch.object(
        app_module,
        "persisted_generation_contract",
        return_value=(record, contract),
    ):
        task, terminal = app_module.product_redis_task_or_terminal_record(
            queue,
            contract["jobId"],
            _principal()[0],
        )

    assert terminal is None
    assert task is not None
    assert task["status"] == "pending"
    assert task["request_sha256"] == contract["idempotency"]["requestSha256"]
    assert task["payload"]["batchContract"]["jobId"] == contract["jobId"]


def test_pending_product_cancel_is_terminal_and_refunded() -> None:
    queue = _queue()
    contract = _contract()
    _enqueue_contract(queue, contract)

    with (
        mock.patch.object(app_module, "generation_request_principal", return_value=_principal()),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(app_module, "refund_generation_batch") as refund,
        mock.patch.object(app_module, "settle_persisted_generation_job") as settle,
        mock.patch.object(app_module, "account_payload", return_value={"balance": 1000}),
    ):
        response = app_module.app.test_client().post(
            f"/api/generation-jobs/{contract['jobId']}/cancel"
        )

    assert response.status_code == 200
    assert response.get_json()["status"] == "canceled"
    refund.assert_called_once()
    assert refund.call_args.kwargs["points"] == contract["billing"]["totalPoints"]
    assert settle.call_args.kwargs["status"] == "canceled"


def test_running_product_cancel_preserves_lease_and_defers_refund() -> None:
    queue = _queue()
    contract = _contract()
    _enqueue_contract(queue, contract)
    claim = queue.claim(worker_id="worker-a", lease_ms=10_000, timeout_seconds=0)
    assert claim is not None

    with (
        mock.patch.object(app_module, "generation_request_principal", return_value=_principal()),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(app_module, "refund_generation_batch") as refund,
        mock.patch.object(app_module, "settle_persisted_generation_job") as settle,
        mock.patch.object(app_module, "account_payload", return_value={"balance": 970}),
    ):
        response = app_module.app.test_client().post(
            f"/api/generation-jobs/{contract['jobId']}/cancel"
        )

    assert response.status_code == 200
    assert response.get_json()["status"] == "running"
    assert response.get_json()["cancelRequested"] is True
    task = queue.get(contract["jobId"])
    assert task["lease_token"] == claim["lease_token"]
    assert task["cancel_requested"] is True
    refund.assert_not_called()
    settle.assert_not_called()


def test_worker_acknowledges_product_cancel_without_success_result() -> None:
    queue = _queue()
    contract = _contract(job_id="generation-worker-cancel")
    _enqueue_contract(queue, contract)

    def handler(payload: dict) -> dict:
        queue.request_cancel(contract["jobId"])
        payload["_executionGuard"]()
        raise AssertionError("execution guard must stop canceled work")

    worker = GenerationWorker(queue, handler=handler, max_retries=0)

    assert worker.process_one(timeout_seconds=0) is True
    task = queue.get(contract["jobId"])
    assert task["status"] == "failed"
    assert task["result"] == {"canceled": True}


def test_dispatch_rejects_unknown_task_type(monkeypatch) -> None:
    provider = mock.Mock()
    monkeypatch.setattr("worker.worker.generate_image", provider)

    try:
        dispatch_generation({"taskType": "unknown", "prompt": "do not run"})
    except QueueError as exc:
        assert "unsupported generation task type" in str(exc)
    else:
        raise AssertionError("unknown task type must fail closed")
    provider.assert_not_called()
