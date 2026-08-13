from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import pytest

import app as app_module
import object_storage_service
from shared.batch_contract import freeze_menu_batch_contract, request_sha256
from shared.redis_queue import QueueError, RedisQueueConfig, RedisTaskQueue
from tests.redis_test_double import RedisTestDouble
from worker.worker import GenerationWorker, dispatch_generation
from worker.product_batch_handler import (
    NonRetryableProductBatchError,
    handle_product_batch,
)


TEST_ATTESTATION_SECRET = "product-batch-signing-secret-32-bytes-minimum"


@pytest.fixture(autouse=True)
def _batch_attestation_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OBJECT_SIGNING_SECRET",
        TEST_ATTESTATION_SECRET,
    )


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
        attestation_secret=TEST_ATTESTATION_SECRET,
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


def test_worker_rejects_tampered_batch_contract_before_storage_or_generation() -> None:
    contract = _contract()
    tampered = copy.deepcopy(contract)
    tampered["selectedBackground"]["objectKey"] = (
        "generated/selected-backgrounds/replaced/image"
    )
    tampered["idempotency"]["requestSha256"] = request_sha256(tampered)

    with (
        mock.patch.object(
            object_storage_service,
            "get_object_storage_service",
        ) as storage,
        mock.patch.object(app_module, "execute_generation_batch_job") as execute,
        pytest.raises(
            NonRetryableProductBatchError,
            match="contract attestation is invalid",
        ),
    ):
        handle_product_batch(
            {"taskType": "product_batch", "batchContract": tampered}
        )

    storage.assert_not_called()
    execute.assert_not_called()


def test_formal_generation_fails_before_lookup_or_debit_without_attestation_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OBJECT_SIGNING_SECRET",
        "ASSET_SIGNING_SECRET",
        "DOWNLOAD_SIGNING_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(app_module, "resolve_menu_upload_snapshot") as resolve_menu,
        mock.patch.object(app_module.billing, "debit_account") as debit,
        mock.patch.object(app_module, "product_redis_queue") as product_queue,
    ):
        response = app_module.app.test_client().post(
            "/api/generation-jobs",
            json={
                "menuUploadId": "menu_" + ("a" * 32),
                "style": "style-1",
                "quality": "standard",
            },
        )

    assert response.status_code == 503
    assert response.get_json()["code"] == (
        "generation_contract_attestation_required"
    )
    resolve_menu.assert_not_called()
    debit.assert_not_called()
    product_queue.assert_not_called()


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


def test_generation_rejects_oversized_paid_batch_before_debit_or_enqueue(
    tmp_path: Path,
) -> None:
    selected_background = SimpleNamespace(
        public_payload=lambda: {"assetId": "bg_test", "sha256": "2" * 64}
    )
    menu_upload_id = "menu_" + ("a" * 32)
    menu_snapshot = {
        "id": menu_upload_id,
        "objectKey": "menus/menu.xlsx",
        "sha256": "1" * 64,
        "parserVersion": 1,
        "summary": {"count": 121},
    }
    background_snapshot = {
        "assetId": "bg_test",
        "styleId": "style-1",
        "sha256": "2" * 64,
        "objectKey": "generated/selected-backgrounds/bg_test/image",
        "width": 1024,
        "height": 768,
    }
    debit = mock.Mock(
        side_effect=AssertionError("capacity failure must happen before debit")
    )
    queue_lookup = mock.Mock(
        side_effect=AssertionError("capacity failure must happen before queue lookup")
    )

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(
            app_module,
            "resolve_menu_upload_snapshot",
            return_value=menu_snapshot,
        ),
        mock.patch.object(
            app_module,
            "materialize_menu_upload_snapshot",
            return_value=tmp_path / "menu.xlsx",
        ),
        mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
        mock.patch.object(
            app_module,
            "requested_selected_background",
            return_value=selected_background,
        ),
        mock.patch.object(
            app_module,
            "selected_background_batch_snapshot",
            return_value=background_snapshot,
        ),
        mock.patch.object(
            app_module,
            "batch_watermark_snapshot",
            return_value={"enabled": False},
        ),
        mock.patch.object(app_module, "tencent_ready", return_value=True),
        mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 120),
        mock.patch.object(app_module, "product_redis_queue", queue_lookup),
        mock.patch.object(app_module.billing, "debit_account", debit),
    ):
        response = app_module.app.test_client().post(
            "/api/generation-jobs",
            json={
                "style": "style-1",
                "quality": "standard",
                "menuUploadId": menu_upload_id,
                "idempotencyKey": "capacity-test",
                "platforms": ["meituan"],
            },
        )

    assert response.status_code == 503
    assert response.get_json()["code"] == "generation_batch_capacity_insufficient"
    assert response.get_json()["requestedImages"] == 121
    assert response.get_json()["batchCallLimit"] == 120
    debit.assert_not_called()
    queue_lookup.assert_not_called()


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


def test_web_rejects_tampered_terminal_contract_before_refund_or_manifest() -> None:
    queue = _queue()
    contract = _contract(job_id="generation-web-attestation-boundary")
    tampered = copy.deepcopy(contract)
    tampered["billing"]["debitOrderId"] = "gen:other-job:debit"
    tampered["idempotency"]["requestSha256"] = request_sha256(tampered)
    _enqueue_contract(queue, tampered)
    claim = queue.claim(
        worker_id="worker-a",
        lease_ms=10_000,
        timeout_seconds=0,
    )
    assert claim is not None
    queue.ack_failed(
        tampered["jobId"],
        receipt=claim["receipt"],
        lease_token=claim["lease_token"],
        error="provider failed",
        attempts=1,
    )

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(app_module, "refund_generation_batch") as refund,
        mock.patch.object(
            app_module,
            "settle_persisted_generation_job",
        ) as settle,
        mock.patch.object(
            app_module,
            "load_generation_result_manifest_pointer",
        ) as load_manifest,
    ):
        response = app_module.app.test_client().get(
            f"/api/generation-jobs/{tampered['jobId']}"
        )

    assert response.status_code == 400
    assert response.get_json()["code"] == "generation_task_contract_mismatch"
    refund.assert_not_called()
    settle.assert_not_called()
    load_manifest.assert_not_called()


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


def test_tampered_persisted_contract_is_not_republished() -> None:
    queue = _queue()
    contract = _contract(job_id="generation-tampered-recovery")
    tampered = copy.deepcopy(contract)
    tampered["billing"]["debitOrderId"] = "gen:other-job:debit"
    record = {
        "id": tampered["jobId"],
        "status": "queued",
        "request": tampered,
    }

    with (
        mock.patch.object(
            app_module,
            "persisted_generation_contract",
            return_value=(record, tampered),
        ),
        pytest.raises(
            app_module.BatchContractError,
            match="attestation",
        ),
    ):
        app_module.recover_missing_product_redis_task(
            queue,
            tampered["jobId"],
            _principal()[0],
        )

    with pytest.raises(app_module.RedisTaskNotFound):
        queue.get(tampered["jobId"])


def test_tampered_contract_cannot_refund_another_same_user_debit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "billing.db"
    monkeypatch.setenv("BILLING_DB_PATH", str(db_path))
    contract = _contract(job_id="generation-refund-boundary")
    user_id = str(contract["userId"])
    app_module.billing.credit_account(user_id, "seed-credit", 100)
    app_module.billing.debit_account(
        user_id,
        str(contract["billing"]["debitOrderId"]),
        30,
    )
    app_module.billing.debit_account(
        user_id,
        "gen:other-job:debit",
        20,
    )
    assert app_module.billing.get_account(user_id)["balance"] == 50
    tampered = copy.deepcopy(contract)
    tampered["billing"]["debitOrderId"] = "gen:other-job:debit"
    tampered["idempotency"]["requestSha256"] = request_sha256(tampered)

    with pytest.raises(
        app_module.BatchContractError,
        match="attestation",
    ):
        app_module.refund_generation_batch(
            tampered,
            points=20,
            reason="forged-worker-failure",
        )

    assert app_module.billing.get_account(user_id)["balance"] == 50


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
