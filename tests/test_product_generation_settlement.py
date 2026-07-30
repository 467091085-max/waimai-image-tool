from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

import object_storage_service
from shared.batch_contract import freeze_menu_batch_contract
from shared.product_generation_settlement import (
    InvalidProductTask,
    MAX_GENERATION_MANIFEST_BYTES,
    ProductManifestIntegrityError,
    apply_generation_completion,
    completion_from_redis_task,
    settle_terminal_job,
)


TEST_GENERATION_PROVENANCE = {
    "taxonomyVersion": "2026-07-30.v2",
    "dishPromptVersion": "dish-generation.v1",
    "pipelineVersion": "exact-background.v1",
    "provider": "tencent-hunyuan",
    "providerMode": "tokenhub-cloud-fallback-v1",
    "modelName": "hy-image-v3.0",
    "modelVersion": (
        "hy-image-v3.0.aiart-2022-12-29."
        "hunyuan-2023-09-01"
    ),
}


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


def frozen_contract() -> dict[str, Any]:
    return freeze_menu_batch_contract(
        job_id="job-settle-1",
        user_id="user-settle-1",
        menu_upload_id="menu-settle-1",
        menu={
            "objectKey": "menus/user-settle-1/menu.xlsx",
            "sha256": "1" * 64,
            "parserVersion": 1,
        },
        selected_background={
            "assetId": "bg-settle-1",
            "styleId": "style-2",
            "sha256": "2" * 64,
            "objectKey": "backgrounds/menu-settle-1/style-2.png",
            "width": 1024,
            "height": 768,
        },
        generation_provenance=TEST_GENERATION_PROVENANCE,
        quality="standard",
        image_count=3,
        platforms=["meituan"],
        watermark={"enabled": False},
        idempotency_key="idem-settle-1",
        created_at="2026-07-29T12:00:00Z",
    )


def completed_task(
    contract: dict[str, Any],
    storage: MemoryStorage,
    *,
    succeeded: int = 2,
) -> dict[str, Any]:
    digest = contract["idempotency"]["requestSha256"]
    object_key = (
        f"generated/manifests/{contract['jobId']}/{digest}.json"
    )
    raw = json.dumps(
        {
            "generationBatch": {
                "jobId": contract["jobId"],
                "requestSha256": digest,
            },
            "generation": {
                "succeeded": succeeded,
            },
            "results": [],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    storage.objects[object_key] = raw
    return {
        "task_id": contract["jobId"],
        "owner_user_id": contract["userId"],
        "request_sha256": digest,
        "status": "done",
        "payload": {
            "taskType": "product_batch",
            "batchContract": contract,
            "_productJobFence": 4,
        },
        "result": {
            "image_url": (
                f"/api/generation-jobs/{contract['jobId']}/manifest"
            ),
            "manifest_object_key": object_key,
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "manifest_size": len(raw),
            "request_sha256": digest,
            "job_id": contract["jobId"],
            "task_type": "product_batch",
        },
    }


def test_completion_verifies_manifest_and_partial_refund() -> None:
    contract = frozen_contract()
    storage = MemoryStorage()

    completion = completion_from_redis_task(
        completed_task(contract, storage, succeeded=2),
        object_store=storage,
    )

    assert completion is not None
    assert completion.terminal_status == "succeeded"
    assert completion.fence == 4
    assert completion.completed_count == 2
    assert completion.failed_count == 1
    assert completion.refund_target_points == 10
    assert completion.manifest_ref is not None


def test_oversized_manifest_claim_is_rejected_before_object_read() -> None:
    contract = frozen_contract()
    storage = MemoryStorage()
    task = completed_task(contract, storage)
    task["result"]["manifest_size"] = MAX_GENERATION_MANIFEST_BYTES + 1
    storage.read_bytes_limited = pytest.fail

    with pytest.raises(
        ProductManifestIntegrityError,
        match="manifest exceeds size limit",
    ):
        completion_from_redis_task(task, object_store=storage)


def test_zero_delivery_refunds_entire_frozen_charge() -> None:
    contract = frozen_contract()
    storage = MemoryStorage()

    completion = completion_from_redis_task(
        completed_task(contract, storage, succeeded=0),
        object_store=storage,
    )

    assert completion is not None
    assert completion.refund_target_points == contract["billing"]["totalPoints"]


def test_manifest_byte_tampering_fails_closed() -> None:
    contract = frozen_contract()
    storage = MemoryStorage()
    task = completed_task(contract, storage)
    storage.objects[task["result"]["manifest_object_key"]] += b" "

    with pytest.raises(ProductManifestIntegrityError):
        completion_from_redis_task(task, object_store=storage)


def test_contract_digest_tampering_fails_before_settlement() -> None:
    contract = frozen_contract()
    storage = MemoryStorage()
    task = completed_task(contract, storage)
    task["payload"]["batchContract"]["billing"]["totalPoints"] += 1000

    with pytest.raises(InvalidProductTask):
        completion_from_redis_task(task, object_store=storage)


def test_failed_and_canceled_tasks_use_full_refund() -> None:
    contract = frozen_contract()
    digest = contract["idempotency"]["requestSha256"]
    base = {
        "task_id": contract["jobId"],
        "owner_user_id": contract["userId"],
        "request_sha256": digest,
        "status": "failed",
        "payload": {
            "taskType": "product_batch",
            "batchContract": contract,
            "_productJobFence": 9,
        },
    }

    failed = completion_from_redis_task(
        {**base, "error": "provider failed", "result": {}}
    )
    canceled = completion_from_redis_task(
        {**base, "error": "canceled", "result": {"canceled": True}}
    )

    assert failed is not None
    assert failed.terminal_status == "failed"
    assert failed.failed_count == 3
    assert failed.refund_target_points == 30
    assert canceled is not None
    assert canceled.terminal_status == "canceled"
    assert canceled.failed_count == 0
    assert canceled.refund_target_points == 30


def test_nonterminal_task_has_no_side_effect() -> None:
    contract = frozen_contract()
    digest = contract["idempotency"]["requestSha256"]
    task = {
        "task_id": contract["jobId"],
        "owner_user_id": contract["userId"],
        "request_sha256": digest,
        "status": "running",
        "payload": {
            "taskType": "product_batch",
            "batchContract": contract,
        },
    }

    assert completion_from_redis_task(task) is None


def test_apply_completion_uses_fence_then_claims_and_refunds() -> None:
    contract = frozen_contract()
    storage = MemoryStorage()
    completion = completion_from_redis_task(
        completed_task(contract, storage),
        object_store=storage,
    )
    assert completion is not None
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeStore:
        def __init__(self) -> None:
            self.status = "running"
            self.settlement_status = "pending"
            self.settlement_version = 0

        def get_owned_job_detail(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(("detail", kwargs))
            return SimpleNamespace(
                job={
                    "id": contract["jobId"],
                    "status": self.status,
                    "request_sha256": contract["idempotency"][
                        "requestSha256"
                    ],
                },
                settlement={
                    "status": self.settlement_status,
                    "version": self.settlement_version,
                    "refund_target_points": 10,
                },
            )

        def complete_with_fence(self, **kwargs: Any) -> None:
            calls.append(("complete", kwargs))
            self.status = "succeeded"

        def claim_settlement_once(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(("claim", kwargs))
            self.settlement_status = "claimed"
            self.settlement_version = 1
            return SimpleNamespace(
                settlement={
                    "status": "claimed",
                    "version": 1,
                    "refund_target_points": 10,
                    "claim_token": kwargs["claim_token"],
                }
            )

        def apply_settlement_refund(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(("refund", kwargs))
            self.settlement_status = "applied"
            return SimpleNamespace(
                settlement={
                    "status": "applied",
                    "refund_applied_points": 10,
                },
                account={"balance_points": 80},
            )

        def get_account(self, **_kwargs: Any) -> dict[str, Any]:
            return {"balance_points": 80}

    result = apply_generation_completion(
        FakeStore(),
        completion,
        reconciler_id="reconciler-1",
        settlement_reclaim_after_seconds=60,
    )

    complete_call = next(value for name, value in calls if name == "complete")
    assert complete_call["fence"] == 4
    assert complete_call["refund_target_points"] == 10
    claim_call = next(value for name, value in calls if name == "claim")
    assert claim_call["reclaim_after_seconds"] == 60
    assert result["settlement"]["status"] == "applied"
    assert result["account"]["balance_points"] == 80


def test_applied_settlement_is_idempotent_without_reclaim() -> None:
    class AppliedStore:
        def get_owned_job_detail(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                job={"id": "job-1", "status": "succeeded"},
                settlement={
                    "status": "applied",
                    "version": 2,
                    "refund_applied_points": 10,
                },
            )

        def get_account(self, **_kwargs: Any) -> dict[str, Any]:
            return {"balance_points": 90}

        def claim_settlement_once(self, **_kwargs: Any) -> None:
            pytest.fail("applied settlement must not be reclaimed")

    result = settle_terminal_job(
        AppliedStore(),
        job_id="job-1",
        owner_user_id="user-1",
        reconciler_id="reconciler-1",
    )

    assert result["settlement"]["status"] == "applied"
    assert result["account"]["balance_points"] == 90
