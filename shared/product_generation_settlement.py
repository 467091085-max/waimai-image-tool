from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

import object_storage_service
from shared.batch_contract import (
    JOB_TYPE as MENU_BATCH_JOB_TYPE,
    request_sha256,
)
from shared.product_job_store import (
    ProductJobStore,
    SettlementConflict,
)


PRODUCT_BATCH_TASK_TYPE = "product_batch"
MAX_GENERATION_MANIFEST_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"[a-f0-9]{64}")
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}


class ProductGenerationSettlementError(RuntimeError):
    pass


class InvalidProductTask(ProductGenerationSettlementError):
    pass


class ProductManifestIntegrityError(ProductGenerationSettlementError):
    pass


@dataclass(frozen=True)
class GenerationCompletion:
    job_id: str
    owner_user_id: str
    request_sha256: str
    fence: int
    terminal_status: str
    requested_count: int
    completed_count: int
    failed_count: int
    refund_target_points: int
    manifest_ref: str | None
    manifest_sha256: str | None
    error_message: str


def completion_from_redis_task(
    task: Mapping[str, Any],
    *,
    object_store: Any | None = None,
) -> GenerationCompletion | None:
    envelope = _mapping(task, "task")
    status = _text(envelope.get("status"), "task.status")
    if status in {"pending", "running"}:
        return None
    if status not in {"done", "failed"}:
        raise InvalidProductTask(f"unsupported terminal Redis status: {status}")

    payload = _mapping(envelope.get("payload"), "task.payload")
    if _text(payload.get("taskType"), "task.payload.taskType") != PRODUCT_BATCH_TASK_TYPE:
        raise InvalidProductTask("Redis task is not a product batch")
    contract = _mapping(
        payload.get("batchContract"),
        "task.payload.batchContract",
    )
    if _text(contract.get("jobType"), "contract.jobType") != MENU_BATCH_JOB_TYPE:
        raise InvalidProductTask("product batch job type mismatch")

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
        calculated_digest = request_sha256(contract)
    except Exception as exc:
        raise InvalidProductTask(
            "product batch request cannot be digested"
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
    quality = _mapping(contract.get("quality"), "contract.quality")
    requested_count = _positive_int(
        billing.get("imageCount"),
        "contract.billing.imageCount",
    )
    total_points = _nonnegative_int(
        billing.get("totalPoints"),
        "contract.billing.totalPoints",
    )
    points_per_image = _nonnegative_int(
        quality.get("pointsPerImage"),
        "contract.quality.pointsPerImage",
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
            requested_count=requested_count,
            completed_count=0,
            failed_count=0 if canceled else requested_count,
            refund_target_points=total_points,
            manifest_ref=None,
            manifest_sha256=None,
            error_message=(
                "user_canceled"
                if canceled
                else _bounded_error(envelope.get("error") or "worker_failed")
            ),
        )

    manifest, document = _load_manifest(
        result,
        contract,
        object_store=object_store,
    )
    generation = _mapping(
        document.get("generation"),
        "manifest.generation",
        error_type=ProductManifestIntegrityError,
    )
    completed_count = _nonnegative_int(
        generation.get("succeeded"),
        "manifest.generation.succeeded",
        error_type=ProductManifestIntegrityError,
    )
    if completed_count > requested_count:
        raise ProductManifestIntegrityError(
            "manifest succeeded count exceeds frozen image count"
        )
    failed_count = requested_count - completed_count
    refund_target = (
        total_points
        if completed_count == 0
        else min(total_points, failed_count * points_per_image)
    )
    return GenerationCompletion(
        job_id=job_id,
        owner_user_id=owner_user_id,
        request_sha256=request_digest,
        fence=fence,
        terminal_status="succeeded",
        requested_count=requested_count,
        completed_count=completed_count,
        failed_count=failed_count,
        refund_target_points=refund_target,
        manifest_ref=manifest["objectKey"],
        manifest_sha256=manifest["sha256"],
        error_message="",
    )


def apply_generation_completion(
    store: ProductJobStore,
    completion: GenerationCompletion,
    *,
    reconciler_id: str,
    settlement_reclaim_after_seconds: int = 300,
) -> dict[str, Any]:
    detail = store.get_owned_job_detail(
        job_id=completion.job_id,
        owner_user_id=completion.owner_user_id,
    )
    if detail is None:
        raise InvalidProductTask("PostgreSQL job is missing or has another owner")
    _require_digest(
        detail.job.get("request_sha256"),
        completion.request_sha256,
        "PostgreSQL request_sha256",
    )
    store.complete_with_fence(
        job_id=completion.job_id,
        fence=completion.fence,
        terminal_status=completion.terminal_status,
        completed_count=completion.completed_count,
        failed_count=completion.failed_count,
        refund_target_points=completion.refund_target_points,
        manifest_ref=completion.manifest_ref,
        manifest_sha256=completion.manifest_sha256,
        error_message=completion.error_message,
    )
    return settle_terminal_job(
        store,
        job_id=completion.job_id,
        owner_user_id=completion.owner_user_id,
        reconciler_id=reconciler_id,
        reclaim_after_seconds=settlement_reclaim_after_seconds,
    )


def settle_terminal_job(
    store: ProductJobStore,
    *,
    job_id: str,
    owner_user_id: str,
    reconciler_id: str,
    reclaim_after_seconds: int = 300,
) -> dict[str, Any]:
    detail = store.get_owned_job_detail(
        job_id=job_id,
        owner_user_id=owner_user_id,
    )
    if detail is None:
        raise InvalidProductTask("PostgreSQL job is missing or has another owner")
    job_status = str(detail.job.get("status") or "")
    settlement = detail.settlement
    if settlement is None:
        raise SettlementConflict(f"generation settlement is missing: {job_id}")
    if job_status not in TERMINAL_JOB_STATUSES:
        return {
            "job": detail.job,
            "settlement": settlement,
            "account": store.get_account(owner_user_id=owner_user_id),
        }

    settlement_status = str(settlement.get("status") or "")
    if settlement_status == "applied":
        return {
            "job": detail.job,
            "settlement": settlement,
            "account": store.get_account(owner_user_id=owner_user_id),
        }
    if settlement_status not in {"pending", "claimed"}:
        raise SettlementConflict(
            f"generation settlement requires review: {job_id}"
        )

    version = _nonnegative_int(
        settlement.get("version"),
        "settlement.version",
    )
    claim = store.claim_settlement_once(
        job_id=job_id,
        expected_version=version,
        claimed_by=_text(reconciler_id, "reconciler_id"),
        claim_token=f"settlement:{job_id}:v{version}",
        reclaim_after_seconds=_positive_int(
            reclaim_after_seconds,
            "reclaim_after_seconds",
        ),
    )
    claimed = claim.settlement
    refund_target = _nonnegative_int(
        claimed.get("refund_target_points"),
        "settlement.refund_target_points",
    )
    applied = store.apply_settlement_refund(
        job_id=job_id,
        claim_token=_text(claimed.get("claim_token"), "settlement.claim_token"),
        provider_reference=f"internal:{job_id}:refund:{refund_target}",
    )
    return {
        "job": detail.job,
        "settlement": applied.settlement,
        "account": applied.account,
    }


def _load_manifest(
    result: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    object_store: Any | None,
) -> tuple[dict[str, str], dict[str, Any]]:
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
        PRODUCT_BATCH_TASK_TYPE,
        "result.task_type",
    )
    _require_digest(
        result.get("request_sha256"),
        request_digest,
        "result.request_sha256",
    )
    expected_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}manifests/"
        f"{job_id}/{request_digest}.json"
    )
    try:
        object_key = object_storage_service.validate_object_key(
            _text(
                result.get("manifest_object_key"),
                "result.manifest_object_key",
            )
        )
    except (TypeError, ValueError) as exc:
        raise ProductManifestIntegrityError(
            "manifest object key is invalid"
        ) from exc
    if not hmac.compare_digest(object_key, expected_key):
        raise ProductManifestIntegrityError(
            "manifest object key does not match the frozen request"
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
    if manifest_size > MAX_GENERATION_MANIFEST_BYTES:
        raise ProductManifestIntegrityError(
            "manifest exceeds size limit"
        )
    storage = (
        object_storage_service.get_object_storage_service()
        if object_store is None
        else object_store
    )
    try:
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            manifest_size,
        )
    except Exception as exc:
        raise ProductManifestIntegrityError(
            "manifest object is unavailable"
        ) from exc
    if (
        len(raw) != manifest_size
        or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            manifest_sha256,
        )
    ):
        raise ProductManifestIntegrityError("manifest digest mismatch")
    try:
        document = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ProductManifestIntegrityError("manifest JSON is invalid") from exc
    if not isinstance(document, dict):
        raise ProductManifestIntegrityError("manifest must be a JSON object")
    generation_batch = _mapping(
        document.get("generationBatch"),
        "manifest.generationBatch",
        error_type=ProductManifestIntegrityError,
    )
    _require_text(
        generation_batch.get("jobId"),
        job_id,
        "manifest.generationBatch.jobId",
        error_type=ProductManifestIntegrityError,
    )
    _require_digest(
        generation_batch.get("requestSha256"),
        request_digest,
        "manifest.generationBatch.requestSha256",
        error_type=ProductManifestIntegrityError,
    )
    return (
        {
            "objectKey": object_key,
            "sha256": manifest_sha256,
        },
        document,
    )


def _mapping(
    value: Any,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error_type(f"{field} must be an object")
    return value


def _text(
    value: Any,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or len(clean) > 512 or any(ord(char) < 32 for char in clean):
        raise error_type(f"{field} must be a non-empty bounded string")
    return clean


def _sha256(
    value: Any,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> str:
    clean = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(clean):
        raise error_type(f"{field} must be a SHA-256 digest")
    return clean


def _positive_int(
    value: Any,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise error_type(f"{field} must be a positive integer")
    return value


def _nonnegative_int(
    value: Any,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_type(f"{field} must be a non-negative integer")
    return value


def _require_text(
    value: Any,
    expected: str,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> None:
    actual = _text(value, field, error_type=error_type)
    if not hmac.compare_digest(actual, expected):
        raise error_type(f"{field} does not match the frozen request")


def _require_digest(
    value: Any,
    expected: str,
    field: str,
    *,
    error_type: type[ProductGenerationSettlementError] = InvalidProductTask,
) -> None:
    actual = _sha256(value, field, error_type=error_type)
    if not hmac.compare_digest(actual, expected):
        raise error_type(f"{field} does not match the frozen request")


def _bounded_error(value: Any) -> str:
    text = str(value or "worker_failed").strip() or "worker_failed"
    return text[:500]
