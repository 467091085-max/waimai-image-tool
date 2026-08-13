from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw
import pytest

import app as app_module
import object_storage_service
from image_edit_provider import ImageEditResult
from shared.redis_queue import RedisQueueConfig, RedisTaskQueue
from shared.refinement_contract import freeze_revision_batch_contract
from tests.redis_test_double import RedisTestDouble
from worker.product_revision_handler import handle_product_revision


TEST_ATTESTATION_SECRET = "revision-queue-signing-secret-32-bytes-minimum"


@pytest.fixture(autouse=True)
def _revision_attestation_secret(monkeypatch) -> None:
    monkeypatch.setenv(
        "OBJECT_SIGNING_SECRET",
        TEST_ATTESTATION_SECRET,
    )


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _images() -> tuple[bytes, bytes, bytes]:
    background = Image.new("RGB", (160, 120), (235, 228, 214))
    source = background.copy()
    edited = background.copy()
    ImageDraw.Draw(source).ellipse((40, 25, 120, 105), fill=(190, 50, 35))
    ImageDraw.Draw(edited).ellipse((40, 25, 120, 105), fill=(45, 145, 70))
    return _png(background), _png(source), _png(edited)


class _Provider:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.calls = 0

    def edit(self, _source: bytes, _prompt: str, *, source_mime_type: str):
        assert source_mime_type == "image/png"
        self.calls += 1
        return ImageEditResult(
            image_bytes=self.output,
            mime_type="image/png",
            provider="fake-gemini",
            model="fake-image-edit",
            request_id="request-1",
        )


def _contract(
    storage: object_storage_service.ObjectStorageService,
    *,
    job_id: str = "revision-queue-test",
    mode: str = "refine",
) -> tuple[dict, bytes]:
    background, source, edited = _images()
    source_key = f"generated/delivery/{job_id}/source.png"
    background_key = f"generated/backgrounds/{job_id}/background.png"
    storage.put_bytes(source, object_key=source_key)
    storage.put_bytes(background, object_key=background_key)
    contract = freeze_revision_batch_contract(
        job_id=job_id,
        parent_generation_job_id="generation-parent",
        user_id="server-user",
        source_delivery_asset={
            "assetId": "asset_" + ("a" * 32),
            "objectKey": source_key,
            "sha256": hashlib.sha256(source).hexdigest(),
            "rowNumber": 2,
            "dishName": "宫保鸡丁",
        },
        selected_background={
            "assetId": "background_asset",
            "objectKey": background_key,
            "sha256": hashlib.sha256(background).hexdigest(),
        },
        quality="standard",
        mode=mode,
        refine_prompt="减少辣椒并让鸡丁更明亮" if mode == "refine" else None,
        idempotency_key=f"key-{job_id}",
        created_at="2026-07-30T08:00:00Z",
        attestation_secret=TEST_ATTESTATION_SECRET,
    )
    return contract, edited


def _queue() -> RedisTaskQueue:
    return RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(
            namespace="revision-flow-test",
            queue_name="product-generate",
        ),
    )


def _enqueue(queue: RedisTaskQueue, contract: dict) -> None:
    queue.enqueue_idempotent(
        {
            "taskType": "product_revision",
            "revisionContract": contract,
        },
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


def test_tampered_revision_contract_is_not_republished(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _edited = _contract(
        storage,
        job_id="revision-tampered-recovery",
    )
    tampered = copy.deepcopy(contract)
    tampered["billing"]["debitOrderId"] = "revision:other-job:debit"
    record = {
        "id": tampered["jobId"],
        "status": "queued",
        "request": tampered,
    }
    queue = _queue()

    with (
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            return_value=(record, tampered),
        ),
        pytest.raises(
            app_module.RefinementContractError,
            match="attestation",
        ),
    ):
        app_module.recover_missing_revision_redis_task(
            queue,
            tampered["jobId"],
            _principal()[0],
        )

    with pytest.raises(app_module.RedisTaskNotFound):
        queue.get(tampered["jobId"])


def test_queue_worker_web_status_and_opaque_asset_flow(tmp_path: Path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, edited = _contract(storage)
    provider = _Provider(edited)
    queue = _queue()
    _enqueue(queue, contract)
    claim = queue.claim(
        worker_id="worker-revision",
        lease_ms=30_000,
        timeout_seconds=0,
    )
    assert claim is not None
    worker_result = handle_product_revision(
        {
            **claim["payload"],
            "_executionGuard": lambda: None,
        },
        provider=provider,
        storage=storage,
    )
    queue.ack_done(
        contract["jobId"],
        receipt=claim["receipt"],
        lease_token=claim["lease_token"],
        image_url=str(worker_result["image_url"]),
        result=dict(worker_result),
    )

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            app_module,
            "settle_persisted_revision_job",
        ) as settle,
        mock.patch.object(
            app_module,
            "account_payload",
            return_value={"balance": 90},
        ),
    ):
        response = app_module.app.test_client().get(
            f"/api/image-refinements/{contract['jobId']}"
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "completed"
    assert body["result"]["dishName"] == "宫保鸡丁"
    assert body["result"]["image"]["url"].endswith("/asset")
    assert body["result"]["backgroundIdentityVerified"] is True
    assert body["result"]["outsideMaskPixelsPreserved"] is True
    assert "objectKey" not in str(body)
    assert "requestId" not in str(body)
    assert provider.calls == 1
    settle.assert_called_once()
    assert settle.call_args.kwargs["status"] == "succeeded"


def test_owner_checked_revision_asset_route_reads_verified_object(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, edited = _contract(storage)
    worker_result = handle_product_revision(
        {
            "taskType": "product_revision",
            "revisionContract": contract,
        },
        provider=_Provider(edited),
        storage=storage,
    )
    manifest = app_module.revision_result_manifest_pointer(
        dict(worker_result),
        contract,
    )
    record = {
        "id": contract["jobId"],
        "status": "succeeded",
        "request": contract,
        "result": {"manifest": manifest},
    }
    expected_asset = storage.read_bytes(
        worker_result["manifest_object_key"].removesuffix(
            "manifest.json"
        )
        + "revision.png"
    )
    storage.read_bytes = mock.Mock(
        side_effect=AssertionError("unbounded object read is forbidden")
    )

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
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    ):
        response = app_module.app.test_client().get(
            f"/api/image-refinements/{contract['jobId']}/asset"
        )

    assert response.status_code == 200
    assert response.data == expected_asset
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, max-age=300"
    storage.read_bytes.assert_not_called()


def test_pending_paid_revision_cancel_is_refunded_and_settled(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _edited = _contract(storage)
    queue = _queue()
    _enqueue(queue, contract)

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal(),
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
        mock.patch.object(
            app_module,
            "refund_revision_batch",
        ) as refund,
        mock.patch.object(
            app_module,
            "settle_persisted_revision_job",
        ) as settle,
        mock.patch.object(
            app_module,
            "account_payload",
            return_value={"balance": 100},
        ),
    ):
        response = app_module.app.test_client().post(
            f"/api/image-refinements/{contract['jobId']}/cancel"
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "canceled"
    assert body["billing"]["chargedPoints"] == 10
    assert body["billing"]["refundedPoints"] == 10
    assert body["billing"]["netPoints"] == 0
    refund.assert_called_once()
    assert refund.call_args.kwargs["reason"] == "user_canceled"
    settle.assert_called_once()
    assert settle.call_args.kwargs["status"] == "canceled"


def test_revision_status_is_concealed_from_another_user(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _edited = _contract(storage)
    queue = _queue()
    _enqueue(queue, contract)

    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=_principal("other-user"),
        ),
        mock.patch.object(app_module, "product_redis_queue", return_value=queue),
    ):
        response = app_module.app.test_client().get(
            f"/api/image-refinements/{contract['jobId']}"
        )

    assert response.status_code == 404


def test_export_override_uses_owned_completed_revision_bytes(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, edited = _contract(storage)
    worker_result = handle_product_revision(
        {
            "taskType": "product_revision",
            "revisionContract": contract,
        },
        provider=_Provider(edited),
        storage=storage,
    )
    manifest = app_module.revision_result_manifest_pointer(
        dict(worker_result),
        contract,
    )
    record = {
        "id": contract["jobId"],
        "status": "succeeded",
        "request": contract,
        "result": {"manifest": manifest},
    }
    original = tmp_path / "original.png"
    original.write_bytes(b"original")
    results = [
        {
            "row": 2,
            "name": "宫保鸡丁",
            "candidates": [{"path": str(original)}],
        }
    ]
    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            return_value=(record, contract),
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    ):
        count = app_module.apply_revision_export_overrides(
            results,
            parent_generation_job_id="generation-parent",
            revision_job_ids=[contract["jobId"]],
            principal=_principal()[0],
            staging_dir=staging,
        )

    assert count == 1
    candidate = results[0]["candidates"][0]
    assert candidate["revisionJobId"] == contract["jobId"]
    assert candidate["path"] != str(original)
    assert Path(candidate["path"]).read_bytes().startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    assert candidate["url"].endswith("/asset")


def test_export_override_rejects_revision_from_another_parent(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, edited = _contract(storage)
    worker_result = handle_product_revision(
        {
            "taskType": "product_revision",
            "revisionContract": contract,
        },
        provider=_Provider(edited),
        storage=storage,
    )
    record = {
        "id": contract["jobId"],
        "status": "succeeded",
        "request": contract,
        "result": {
            "manifest": app_module.revision_result_manifest_pointer(
                dict(worker_result),
                contract,
            )
        },
    }

    with mock.patch.object(
        app_module,
        "persisted_revision_record",
        return_value=(record, contract),
    ):
        try:
            app_module.apply_revision_export_overrides(
                [{"row": 2, "candidates": []}],
                parent_generation_job_id="generation-other",
                revision_job_ids=[contract["jobId"]],
                principal=_principal()[0],
                staging_dir=tmp_path,
            )
        except app_module.MenuUploadError as exc:
            assert exc.code == "revision_export_not_ready"
        else:
            raise AssertionError("cross-parent revision export must fail")


def test_completed_revision_can_be_frozen_as_the_next_edit_source(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, edited = _contract(storage)
    worker_result = handle_product_revision(
        {
            "taskType": "product_revision",
            "revisionContract": contract,
        },
        provider=_Provider(edited),
        storage=storage,
    )
    manifest = app_module.revision_result_manifest_pointer(
        dict(worker_result),
        contract,
    )
    revision_record = {
        "id": contract["jobId"],
        "status": "succeeded",
        "request": contract,
        "result": {"manifest": manifest},
    }
    parent_contract = {
        "jobType": "menu_batch_generation",
        "jobId": "generation-parent",
        "userId": "server-user",
        "selectedBackground": contract["selectedBackground"],
    }
    parent_record = {
        "id": "generation-parent",
        "status": "succeeded",
        "request": parent_contract,
    }
    with (
        mock.patch.object(
            app_module,
            "persisted_revision_record",
            return_value=(revision_record, contract),
        ),
        mock.patch.object(
            app_module,
            "persisted_generation_contract",
            return_value=(parent_record, parent_contract),
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    ):
        revision_asset_id = (
            app_module.load_revision_result_manifest_document(
                manifest,
                contract,
            )["imageAsset"]["assetId"]
        )
        _parent, source, background = (
            app_module.resolve_revision_delivery_asset_snapshot(
                contract["jobId"],
                revision_asset_id,
                "generation-parent",
                _principal()[0],
            )
        )

    assert source["assetId"] == revision_asset_id
    assert source["rowNumber"] == 2
    assert source["dishName"] == "宫保鸡丁"
    assert source["objectKey"].endswith("/revision.png")
    assert background["sha256"] == contract["selectedBackground"]["sha256"]
