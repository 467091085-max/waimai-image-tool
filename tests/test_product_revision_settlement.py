from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

import object_storage_service
from shared.batch_contract import canonical_json
from shared.product_generation_settlement import (
    InvalidProductTask,
    ProductManifestIntegrityError,
)
from shared.product_revision_settlement import (
    MAX_REVISION_IMAGE_BYTES,
    MAX_REVISION_MANIFEST_BYTES,
    revision_completion_from_redis_task,
)
from shared.refinement_contract import freeze_revision_batch_contract


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def read_bytes(self, object_key: str) -> bytes:
        return self.objects[object_key]

    def read_bytes_limited(self, object_key: str, max_bytes: int) -> bytes:
        raw = self.objects[object_key]
        if len(raw) > max_bytes:
            raise object_storage_service.ObjectStorageReadLimitExceeded(
                "object exceeds read limit"
            )
        return raw


def revision_contract(*, free: bool = False) -> dict[str, Any]:
    return freeze_revision_batch_contract(
        job_id="revision-settle-1",
        parent_generation_job_id="generation-parent-1",
        user_id="user-settle-1",
        source_delivery_asset={
            "assetId": "asset-source-1",
            "objectKey": "generated/source.png",
            "sha256": "1" * 64,
            "rowNumber": 2,
            "dishName": "辣椒炒肉",
        },
        selected_background={
            "assetId": "background-1",
            "objectKey": "backgrounds/background-1.png",
            "sha256": "2" * 64,
        },
        quality="standard",
        mode="rework",
        refine_prompt=None,
        idempotency_key="revision-idem-1",
        free_rework_quota_verified=free,
        created_at="2026-07-29T12:00:00Z",
    )


def completed_revision_task(
    contract: dict[str, Any],
    storage: MemoryStorage,
) -> dict[str, Any]:
    digest = contract["idempotency"]["requestSha256"]
    asset_key = (
        f"generated/revisions/{contract['jobId']}/{digest}/revision.png"
    )
    manifest_key = (
        f"generated/revisions/{contract['jobId']}/{digest}/manifest.json"
    )
    raw_asset = b"\x89PNG\r\n\x1a\nrevision-bytes"
    storage.objects[asset_key] = raw_asset
    document = {
        "schemaVersion": 1,
        "taskType": "product_revision",
        "jobType": "delivery_asset_revision_batch",
        "jobId": contract["jobId"],
        "parentGenerationJobId": contract["parentGenerationJobId"],
        "requestSha256": digest,
        "mode": contract["mode"],
        "sourceDeliveryAsset": {
            key: contract["sourceDeliveryAsset"][key]
            for key in ("assetId", "objectKey", "sha256")
        },
        "selectedBackground": {
            key: contract["selectedBackground"][key]
            for key in ("assetId", "objectKey", "sha256")
        },
        "imageAsset": {
            "assetId": f"revision_{digest[:32]}",
            "objectKey": asset_key,
            "sha256": hashlib.sha256(raw_asset).hexdigest(),
            "size": len(raw_asset),
            "mimeType": "image/png",
        },
        "provider": {},
        "composition": {
            "backgroundIdentityVerified": True,
            "outsideMaskPixelsPreserved": True,
        },
    }
    raw_manifest = canonical_json(document).encode("utf-8")
    storage.objects[manifest_key] = raw_manifest
    return {
        "task_id": contract["jobId"],
        "owner_user_id": contract["userId"],
        "request_sha256": digest,
        "status": "done",
        "payload": {
            "taskType": "product_revision",
            "revisionContract": contract,
            "_productJobFence": 5,
        },
        "result": {
            "manifest_object_key": manifest_key,
            "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "manifest_size": len(raw_manifest),
            "request_sha256": digest,
            "job_id": contract["jobId"],
            "task_type": "product_revision",
        },
    }


def test_revision_completion_verifies_manifest_and_asset() -> None:
    contract = revision_contract()
    storage = MemoryStorage()

    completion = revision_completion_from_redis_task(
        completed_revision_task(contract, storage),
        object_store=storage,
    )

    assert completion is not None
    assert completion.terminal_status == "succeeded"
    assert completion.requested_count == 1
    assert completion.completed_count == 1
    assert completion.refund_target_points == 0
    assert completion.fence == 5


def test_revision_manifest_tampering_fails_closed() -> None:
    contract = revision_contract()
    storage = MemoryStorage()
    task = completed_revision_task(contract, storage)
    storage.objects[task["result"]["manifest_object_key"]] += b" "

    with pytest.raises(ProductManifestIntegrityError):
        revision_completion_from_redis_task(task, object_store=storage)


def test_oversized_revision_manifest_is_rejected_before_object_read() -> None:
    contract = revision_contract()
    storage = MemoryStorage()
    task = completed_revision_task(contract, storage)
    task["result"]["manifest_size"] = MAX_REVISION_MANIFEST_BYTES + 1
    storage.read_bytes_limited = pytest.fail

    with pytest.raises(
        ProductManifestIntegrityError,
        match="revision manifest exceeds size limit",
    ):
        revision_completion_from_redis_task(task, object_store=storage)


def test_oversized_revision_asset_is_rejected_before_asset_read() -> None:
    contract = revision_contract()
    storage = MemoryStorage()
    task = completed_revision_task(contract, storage)
    manifest_key = task["result"]["manifest_object_key"]
    document = json.loads(storage.objects[manifest_key])
    document["imageAsset"]["size"] = MAX_REVISION_IMAGE_BYTES + 1
    raw_manifest = canonical_json(document).encode("utf-8")
    storage.objects[manifest_key] = raw_manifest
    task["result"]["manifest_sha256"] = hashlib.sha256(
        raw_manifest
    ).hexdigest()
    task["result"]["manifest_size"] = len(raw_manifest)
    original_reader = storage.read_bytes_limited
    reads: list[str] = []

    def bounded_reader(object_key: str, max_bytes: int) -> bytes:
        reads.append(object_key)
        return original_reader(object_key, max_bytes)

    storage.read_bytes_limited = bounded_reader

    with pytest.raises(
        ProductManifestIntegrityError,
        match="revision image exceeds size limit",
    ):
        revision_completion_from_redis_task(task, object_store=storage)

    assert reads == [manifest_key]


def test_revision_asset_tampering_fails_closed() -> None:
    contract = revision_contract()
    storage = MemoryStorage()
    task = completed_revision_task(contract, storage)
    asset_key = json.loads(
        storage.objects[task["result"]["manifest_object_key"]]
    )["imageAsset"]["objectKey"]
    storage.objects[asset_key] += b"tampered"

    with pytest.raises(ProductManifestIntegrityError):
        revision_completion_from_redis_task(task, object_store=storage)


def test_revision_contract_tampering_fails_before_settlement() -> None:
    contract = revision_contract()
    storage = MemoryStorage()
    task = completed_revision_task(contract, storage)
    task["payload"]["revisionContract"]["billing"]["totalPoints"] += 10

    with pytest.raises(InvalidProductTask):
        revision_completion_from_redis_task(task, object_store=storage)


def test_failed_paid_revision_refunds_full_frozen_charge() -> None:
    contract = revision_contract()
    digest = contract["idempotency"]["requestSha256"]
    task = {
        "task_id": contract["jobId"],
        "owner_user_id": contract["userId"],
        "request_sha256": digest,
        "status": "failed",
        "payload": {
            "taskType": "product_revision",
            "revisionContract": contract,
            "_productJobFence": 3,
        },
        "error": "provider failed",
        "result": {},
    }

    completion = revision_completion_from_redis_task(task)

    assert completion is not None
    assert completion.terminal_status == "failed"
    assert completion.failed_count == 1
    assert completion.refund_target_points == 10


def test_canceled_free_revision_has_zero_refund() -> None:
    contract = revision_contract(free=True)
    digest = contract["idempotency"]["requestSha256"]
    task = {
        "task_id": contract["jobId"],
        "owner_user_id": contract["userId"],
        "request_sha256": digest,
        "status": "failed",
        "payload": {
            "taskType": "product_revision",
            "revisionContract": contract,
            "_productJobFence": 7,
        },
        "result": {"canceled": True},
    }

    completion = revision_completion_from_redis_task(task)

    assert completion is not None
    assert completion.terminal_status == "canceled"
    assert completion.failed_count == 0
    assert completion.refund_target_points == 0
