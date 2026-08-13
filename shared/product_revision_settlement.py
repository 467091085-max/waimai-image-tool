from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

import object_storage_service
from shared.batch_contract import canonical_json
from shared.contract_attestation import contract_attestation_secret_from_env
from shared.product_generation_settlement import (
    GenerationCompletion,
    InvalidProductTask,
    ProductManifestIntegrityError,
)
from shared.refinement_contract import (
    JOB_TYPE as REVISION_JOB_TYPE,
    revision_contract_attestation_valid,
    revision_request_sha256,
)


PRODUCT_REVISION_TASK_TYPE = "product_revision"
REVISION_MANIFEST_SCHEMA_VERSION = 1
MAX_REVISION_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_REVISION_IMAGE_BYTES = 30 * 1024 * 1024


def revision_completion_from_redis_task(
    task: Mapping[str, Any],
    *,
    object_store: Any | None = None,
    attestation_secret: str | bytes | None = None,
) -> GenerationCompletion | None:
    envelope = _mapping(task, "task")
    status = _text(envelope.get("status"), "task.status")
    if status in {"pending", "running"}:
        return None
    if status not in {"done", "failed"}:
        raise InvalidProductTask(
            f"unsupported terminal Redis status: {status}"
        )

    payload = _mapping(envelope.get("payload"), "task.payload")
    _require_text(
        payload.get("taskType"),
        PRODUCT_REVISION_TASK_TYPE,
        "task.payload.taskType",
    )
    contract = _mapping(
        payload.get("revisionContract"),
        "task.payload.revisionContract",
    )
    _require_text(
        contract.get("jobType"),
        REVISION_JOB_TYPE,
        "contract.jobType",
    )
    signing_secret = (
        contract_attestation_secret_from_env()
        if attestation_secret is None
        else attestation_secret
    )
    if not revision_contract_attestation_valid(contract, signing_secret):
        raise InvalidProductTask("product revision contract attestation is invalid")

    job_id = _text(contract.get("jobId"), "contract.jobId")
    owner_user_id = _text(contract.get("userId"), "contract.userId")
    idempotency = _mapping(
        contract.get("idempotency"),
        "contract.idempotency",
    )
    request_digest = _sha256(
        idempotency.get("requestSha256"),
        "contract.idempotency.requestSha256",
    )
    try:
        calculated_digest = revision_request_sha256(contract)
    except Exception as exc:
        raise InvalidProductTask(
            "product revision request cannot be digested"
        ) from exc
    _require_digest(calculated_digest, request_digest, "contract digest")
    _require_text(envelope.get("task_id"), job_id, "task.task_id")
    _require_text(
        envelope.get("owner_user_id"),
        owner_user_id,
        "task.owner_user_id",
    )
    _require_digest(
        envelope.get("request_sha256"),
        request_digest,
        "task.request_sha256",
    )
    fence = _positive_int(
        payload.get("_productJobFence"),
        "task.payload._productJobFence",
    )
    billing = _mapping(contract.get("billing"), "contract.billing")
    total_points = _nonnegative_int(
        billing.get("totalPoints"),
        "contract.billing.totalPoints",
    )
    result = (
        envelope.get("result")
        if isinstance(envelope.get("result"), Mapping)
        else {}
    )

    canceled = status == "failed" and bool(result.get("canceled"))
    if status == "failed":
        return GenerationCompletion(
            job_id=job_id,
            owner_user_id=owner_user_id,
            request_sha256=request_digest,
            fence=fence,
            terminal_status="canceled" if canceled else "failed",
            requested_count=1,
            completed_count=0,
            failed_count=0 if canceled else 1,
            refund_target_points=total_points,
            manifest_ref=None,
            manifest_sha256=None,
            error_message=(
                "user_canceled"
                if canceled
                else _bounded_error(envelope.get("error") or "worker_failed")
            ),
        )

    manifest = _load_revision_manifest(
        result,
        contract,
        object_store=object_store,
    )
    return GenerationCompletion(
        job_id=job_id,
        owner_user_id=owner_user_id,
        request_sha256=request_digest,
        fence=fence,
        terminal_status="succeeded",
        requested_count=1,
        completed_count=1,
        failed_count=0,
        refund_target_points=0,
        manifest_ref=manifest["objectKey"],
        manifest_sha256=manifest["sha256"],
        error_message="",
    )


def _load_revision_manifest(
    result: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    object_store: Any | None,
) -> dict[str, str]:
    job_id = _text(contract.get("jobId"), "contract.jobId")
    request_digest = _sha256(
        _mapping(contract.get("idempotency"), "contract.idempotency").get(
            "requestSha256"
        ),
        "contract.idempotency.requestSha256",
    )
    _require_text(result.get("job_id"), job_id, "result.job_id")
    _require_text(
        result.get("task_type"),
        PRODUCT_REVISION_TASK_TYPE,
        "result.task_type",
    )
    _require_digest(
        result.get("request_sha256"),
        request_digest,
        "result.request_sha256",
    )
    expected_manifest_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}revisions/"
        f"{job_id}/{request_digest}/manifest.json"
    )
    try:
        manifest_key = object_storage_service.validate_object_key(
            _text(
                result.get("manifest_object_key"),
                "result.manifest_object_key",
            )
        )
    except (TypeError, ValueError) as exc:
        raise ProductManifestIntegrityError(
            "revision manifest object key is invalid"
        ) from exc
    if not hmac.compare_digest(manifest_key, expected_manifest_key):
        raise ProductManifestIntegrityError(
            "revision manifest key does not match the frozen request"
        )
    manifest_sha256 = _sha256(
        result.get("manifest_sha256"),
        "result.manifest_sha256",
        error_type=ProductManifestIntegrityError,
    )
    manifest_size = _positive_int(
        result.get("manifest_size"),
        "result.manifest_size",
        error_type=ProductManifestIntegrityError,
    )
    if manifest_size > MAX_REVISION_MANIFEST_BYTES:
        raise ProductManifestIntegrityError(
            "revision manifest exceeds size limit"
        )
    storage = (
        object_storage_service.get_object_storage_service()
        if object_store is None
        else object_store
    )
    try:
        raw_manifest = object_storage_service.read_object_bytes_limited(
            storage,
            manifest_key,
            manifest_size,
        )
    except Exception as exc:
        raise ProductManifestIntegrityError(
            "revision manifest object is unavailable"
        ) from exc
    if (
        len(raw_manifest) != manifest_size
        or not hmac.compare_digest(
            hashlib.sha256(raw_manifest).hexdigest(),
            manifest_sha256,
        )
    ):
        raise ProductManifestIntegrityError(
            "revision manifest integrity mismatch"
        )
    try:
        document = json.loads(raw_manifest)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ProductManifestIntegrityError(
            "revision manifest JSON is invalid"
        ) from exc
    if (
        not isinstance(document, dict)
        or canonical_json(document).encode("utf-8") != raw_manifest
    ):
        raise ProductManifestIntegrityError(
            "revision manifest is not canonical"
        )

    expected_scalars = {
        "schemaVersion": REVISION_MANIFEST_SCHEMA_VERSION,
        "taskType": PRODUCT_REVISION_TASK_TYPE,
        "jobType": REVISION_JOB_TYPE,
        "jobId": job_id,
        "parentGenerationJobId": _text(
            contract.get("parentGenerationJobId"),
            "contract.parentGenerationJobId",
        ),
        "requestSha256": request_digest,
        "mode": _text(contract.get("mode"), "contract.mode"),
    }
    if any(document.get(key) != value for key, value in expected_scalars.items()):
        raise ProductManifestIntegrityError(
            "revision manifest contract mismatch"
        )
    for snapshot_name in ("sourceDeliveryAsset", "selectedBackground"):
        _verify_snapshot(
            document.get(snapshot_name),
            contract.get(snapshot_name),
            snapshot_name,
        )

    image_asset = _mapping(
        document.get("imageAsset"),
        "manifest.imageAsset",
        error_type=ProductManifestIntegrityError,
    )
    expected_asset_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}revisions/"
        f"{job_id}/{request_digest}/revision.png"
    )
    try:
        asset_key = object_storage_service.validate_object_key(
            _text(
                image_asset.get("objectKey"),
                "manifest.imageAsset.objectKey",
                error_type=ProductManifestIntegrityError,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ProductManifestIntegrityError(
            "revision image object key is invalid"
        ) from exc
    if not hmac.compare_digest(asset_key, expected_asset_key):
        raise ProductManifestIntegrityError(
            "revision image key does not match the frozen request"
        )
    _require_text(
        image_asset.get("assetId"),
        f"revision_{request_digest[:32]}",
        "manifest.imageAsset.assetId",
        error_type=ProductManifestIntegrityError,
    )
    _require_text(
        image_asset.get("mimeType"),
        "image/png",
        "manifest.imageAsset.mimeType",
        error_type=ProductManifestIntegrityError,
    )
    asset_sha256 = _sha256(
        image_asset.get("sha256"),
        "manifest.imageAsset.sha256",
        error_type=ProductManifestIntegrityError,
    )
    asset_size = _positive_int(
        image_asset.get("size"),
        "manifest.imageAsset.size",
        error_type=ProductManifestIntegrityError,
    )
    if asset_size > MAX_REVISION_IMAGE_BYTES:
        raise ProductManifestIntegrityError(
            "revision image exceeds size limit"
        )
    try:
        raw_asset = object_storage_service.read_object_bytes_limited(
            storage,
            asset_key,
            asset_size,
        )
    except Exception as exc:
        raise ProductManifestIntegrityError(
            "revision image object is unavailable"
        ) from exc
    if (
        len(raw_asset) != asset_size
        or not hmac.compare_digest(
            hashlib.sha256(raw_asset).hexdigest(),
            asset_sha256,
        )
    ):
        raise ProductManifestIntegrityError(
            "revision image integrity mismatch"
        )
    return {
        "objectKey": manifest_key,
        "sha256": manifest_sha256,
    }


def _verify_snapshot(
    actual_value: Any,
    expected_value: Any,
    label: str,
) -> None:
    actual = _mapping(
        actual_value,
        f"manifest.{label}",
        error_type=ProductManifestIntegrityError,
    )
    expected = _mapping(expected_value, f"contract.{label}")
    for key in ("assetId", "objectKey", "sha256"):
        _require_text(
            actual.get(key),
            _text(expected.get(key), f"contract.{label}.{key}"),
            f"manifest.{label}.{key}",
            error_type=ProductManifestIntegrityError,
        )


def _mapping(
    value: Any,
    field: str,
    *,
    error_type: type[InvalidProductTask] = InvalidProductTask,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error_type(f"{field} must be an object")
    return value


def _text(
    value: Any,
    field: str,
    *,
    error_type: type[InvalidProductTask] = InvalidProductTask,
) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or len(clean) > 512 or any(ord(char) < 32 for char in clean):
        raise error_type(f"{field} must be a non-empty bounded string")
    return clean


def _sha256(
    value: Any,
    field: str,
    *,
    error_type: type[InvalidProductTask] = InvalidProductTask,
) -> str:
    clean = str(value or "").strip().lower()
    if len(clean) != 64 or any(char not in "0123456789abcdef" for char in clean):
        raise error_type(f"{field} must be a SHA-256 digest")
    return clean


def _positive_int(
    value: Any,
    field: str,
    *,
    error_type: type[InvalidProductTask] = InvalidProductTask,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise error_type(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidProductTask(f"{field} must be a non-negative integer")
    return value


def _require_text(
    value: Any,
    expected: str,
    field: str,
    *,
    error_type: type[InvalidProductTask] = InvalidProductTask,
) -> None:
    actual = _text(value, field, error_type=error_type)
    if not hmac.compare_digest(actual, expected):
        raise error_type(f"{field} does not match the frozen request")


def _require_digest(value: Any, expected: str, field: str) -> None:
    actual = _sha256(value, field)
    if not hmac.compare_digest(actual, expected):
        raise InvalidProductTask(
            f"{field} does not match the frozen request"
        )


def _bounded_error(value: Any) -> str:
    text = str(value or "worker_failed").strip() or "worker_failed"
    return text[:500]


__all__ = [
    "PRODUCT_REVISION_TASK_TYPE",
    "revision_completion_from_redis_task",
]
