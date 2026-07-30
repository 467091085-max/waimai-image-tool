from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping

import object_storage_service
from shared.batch_contract import request_sha256


PRODUCT_BATCH_TASK_TYPE = "product_batch"


class NonRetryableProductBatchError(RuntimeError):
    pass


class ProductBatchCancellationRequested(RuntimeError):
    pass


def handle_product_batch(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = payload.get("batchContract")
    if not isinstance(contract, dict):
        raise NonRetryableProductBatchError("product batch contract is required")
    expected_request_sha256 = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if not expected_request_sha256 or not hmac.compare_digest(
        request_sha256(contract),
        expected_request_sha256,
    ):
        raise NonRetryableProductBatchError("product batch request digest mismatch")

    import app as product_app

    existing_manifest = _completed_manifest(product_app, contract)
    if existing_manifest is not None:
        return existing_manifest
    execution_guard = payload.get("_executionGuard")
    if execution_guard is not None and not callable(execution_guard):
        raise NonRetryableProductBatchError("product batch execution guard is invalid")
    try:
        execution = product_app.execute_generation_batch_job(
            contract,
            execution_guard=execution_guard,
        )
    except ProductBatchCancellationRequested:
        raise
    manifest = (
        execution.get("manifest")
        if isinstance(execution, dict)
        and isinstance(execution.get("manifest"), dict)
        else {}
    )
    return _manifest_result(product_app, contract, manifest)


def _completed_manifest(
    product_app: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    job_id = str(contract["jobId"])
    expected_sha = str(contract["idempotency"]["requestSha256"])
    object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}manifests/"
        f"{job_id}/{expected_sha}.json"
    )
    storage = object_storage_service.get_object_storage_service()
    if not storage.exists(object_key):
        return None
    raw = object_storage_service.read_object_bytes_limited(
        storage,
        object_key,
        int(product_app.MAX_GENERATION_MANIFEST_BYTES),
    )
    manifest = {
        "objectKey": object_key,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "requestSha256": expected_sha,
    }
    return _manifest_result(product_app, contract, manifest)


def _manifest_result(
    product_app: Any,
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        product_app.load_generation_result_manifest_pointer(
            dict(manifest),
            dict(contract),
        )
    except Exception as exc:
        raise NonRetryableProductBatchError(
            "product batch manifest integrity mismatch"
        ) from exc
    object_key = object_storage_service.validate_object_key(
        str(manifest.get("objectKey") or "")
    )
    expected_manifest_sha = str(manifest.get("sha256") or "")
    expected_sha = str(contract["idempotency"]["requestSha256"])
    return {
        "image_url": f"/api/generation-jobs/{contract['jobId']}/manifest",
        "manifest_object_key": object_key,
        "manifest_sha256": expected_manifest_sha,
        "manifest_size": int(manifest.get("size") or 0),
        "request_sha256": expected_sha,
        "job_id": str(contract["jobId"]),
        "task_type": PRODUCT_BATCH_TASK_TYPE,
    }
