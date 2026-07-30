from __future__ import annotations

import hmac
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from uuid import uuid4

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
PRODUCT_BATCH_REQUEST_JOB_TYPE = "menu_batch_generation"
SENSITIVE_WALLET_METADATA_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
        "token",
    }
)


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductJobStoreError(RuntimeError):
    pass


class InvalidProductJobInput(ProductJobStoreError):
    pass


class JobNotFound(ProductJobStoreError):
    pass


class RequestDigestConflict(ProductJobStoreError):
    def __init__(self, existing_job_id: str) -> None:
        super().__init__(
            f"idempotency key already belongs to a different request: {existing_job_id}"
        )
        self.existing_job_id = existing_job_id


class FenceMismatch(ProductJobStoreError):
    def __init__(self, expected_fence: int, actual_fence: int) -> None:
        super().__init__(
            f"job fence mismatch: expected {expected_fence}, current {actual_fence}"
        )
        self.expected_fence = expected_fence
        self.actual_fence = actual_fence


class JobStateConflict(ProductJobStoreError):
    pass


class CancellationRequested(JobStateConflict):
    pass


class OutboxClaimLost(ProductJobStoreError):
    pass


class ResultWriteConflict(ProductJobStoreError):
    pass


class SettlementConflict(ProductJobStoreError):
    pass


class InsufficientPointBalance(ProductJobStoreError):
    def __init__(self, *, available_points: int, required_points: int) -> None:
        super().__init__(
            "insufficient point balance: "
            f"available={available_points}, required={required_points}"
        )
        self.available_points = available_points
        self.required_points = required_points


class PointOrderConflict(ProductJobStoreError):
    pass


class WalletIntegrityError(ProductJobStoreError):
    pass


@dataclass(frozen=True)
class CreateOrGetResult:
    job: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class RevisionJobCandidate:
    request_sha256: str
    request_payload: Mapping[str, Any]
    menu_upload_id: str
    menu_object_ref: str
    menu_object_sha256: str
    selected_background_ref: str
    selected_background_sha256: str
    requested_count: int
    debit_order_id: str
    debit_points: int
    refund_order_id: str
    job_id: str
    outbox_id: str
    account_metadata: Mapping[str, Any] | None = None
    debit_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RevisionJobCreateResult:
    job: dict[str, Any]
    created: bool
    selected_candidate: str
    request_payload: dict[str, Any]


@dataclass(frozen=True)
class _PreparedRevisionCandidate:
    kind: str
    digest: str
    request_payload: dict[str, Any]
    request_json: str
    menu_upload_id: str
    menu_object_ref: str
    menu_object_sha256: str
    selected_background_ref: str
    selected_background_sha256: str
    requested_count: int
    debit_order_id: str
    debit_points: int
    refund_order_id: str
    job_id: str
    outbox_id: str
    account_metadata_json: str
    debit_metadata_json: str
    event_json: str

    def job_values(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
    ) -> tuple[Any, ...]:
        return (
            self.job_id,
            owner_user_id,
            idempotency_key,
            self.digest,
            self.request_json,
            self.menu_upload_id,
            self.menu_object_ref,
            self.menu_object_sha256,
            self.selected_background_ref,
            self.selected_background_sha256,
            self.requested_count,
            self.debit_order_id,
            self.debit_points,
            self.refund_order_id,
        )


@dataclass(frozen=True)
class CancelRequestResult:
    job: dict[str, Any]
    idempotent: bool


@dataclass(frozen=True)
class CompletionResult:
    job: dict[str, Any]
    idempotent: bool


@dataclass(frozen=True)
class SettlementClaimResult:
    settlement: dict[str, Any]
    idempotent: bool


@dataclass(frozen=True)
class ResultWriteResult:
    result: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class SettlementFinalizationResult:
    settlement: dict[str, Any]
    idempotent: bool


@dataclass(frozen=True)
class AccountCreateResult:
    account: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class PointMutationResult:
    account: dict[str, Any]
    order: dict[str, Any]
    idempotent: bool


@dataclass(frozen=True)
class SettlementRefundResult:
    account: dict[str, Any]
    order: dict[str, Any] | None
    settlement: dict[str, Any]
    idempotent: bool


@dataclass(frozen=True)
class OwnedJobDetail:
    """Private persistence view; callers must map it to an allowlisted public DTO."""

    job: dict[str, Any]
    settlement: dict[str, Any] | None
    outbox: dict[str, Any] | None
    results: tuple[dict[str, Any], ...]


_JOB_COLUMNS = """
    id, owner_user_id, idempotency_key, request_sha256, status, version, fence,
    cancel_requested, requested_count, completed_count, failed_count,
    manifest_ref, manifest_sha256, debit_order_id, debit_points,
    refund_order_id, refund_target_points, error_message,
    created_at, updated_at, started_at, completed_at
"""

_SETTLEMENT_COLUMNS = """
    job_id, owner_user_id, status, version, debit_order_id, debit_points,
    refund_order_id, refund_target_points, refund_applied_points,
    claim_token, claimed_by, claimed_at, applied_at, finalized_at,
    provider_reference, error_message, created_at, updated_at
"""

_RESULT_COLUMNS = """
    id, job_id, job_fence, menu_row, platform, variant, status,
    object_ref, object_sha256, prompt_sha256, provider_request_id,
    metadata, error_message, created_at, updated_at
"""

_PRIVATE_JOB_COLUMNS = """
    id, owner_user_id, idempotency_key, request_sha256, request_payload,
    menu_upload_id, menu_object_ref, menu_object_sha256,
    selected_background_ref, selected_background_sha256,
    status, version, fence, cancel_requested,
    requested_count, completed_count, failed_count,
    manifest_ref, manifest_sha256, debit_order_id, debit_points,
    refund_order_id, refund_target_points, error_message,
    created_at, updated_at, started_at, completed_at
"""

_ACCOUNT_COLUMNS = """
    owner_user_id, balance_points, lifetime_credited_points,
    lifetime_debited_points, lifetime_refunded_points, version,
    metadata, created_at, updated_at
"""

_POINT_ORDER_COLUMNS = """
    id, owner_user_id, order_kind, points, source_order_id,
    source_order_kind, job_id, request_sha256, metadata,
    applied_at, created_at, updated_at
"""

_INSERT_JOB_SQL = f"""
/* product_job_store:create_job */
INSERT INTO product_generation_jobs (
    id, owner_user_id, idempotency_key, request_sha256, request_payload,
    menu_upload_id, menu_object_ref, menu_object_sha256,
    selected_background_ref, selected_background_sha256,
    requested_count, debit_order_id, debit_points, refund_order_id
) VALUES (
    %s, %s, %s, %s, %s::jsonb,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s
)
ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
RETURNING {_JOB_COLUMNS}
"""

_SELECT_JOB_BY_REQUEST_SQL = f"""
/* product_job_store:select_job_by_request */
SELECT {_JOB_COLUMNS}
FROM product_generation_jobs
WHERE owner_user_id = %s AND idempotency_key = %s
"""

_SELECT_JOB_SQL = f"""
/* product_job_store:select_job */
SELECT {_JOB_COLUMNS}
FROM product_generation_jobs
WHERE id = %s
"""

_SELECT_OWNED_JOB_SQL = f"""
/* product_job_store:select_owned_job */
SELECT {_JOB_COLUMNS}
FROM product_generation_jobs
WHERE id = %s AND owner_user_id = %s
"""

_INSERT_SETTLEMENT_SQL = """
/* product_job_store:create_settlement */
INSERT INTO product_generation_settlements (
    job_id, owner_user_id, debit_order_id, debit_points, refund_order_id
) VALUES (%s, %s, %s, %s, %s)
"""

_INSERT_OUTBOX_SQL = """
/* product_job_store:create_outbox */
INSERT INTO product_generation_outbox (
    id, job_id, event_type, payload
) VALUES (%s, %s, 'product_generation.requested', %s::jsonb)
"""

_CLAIM_OUTBOX_SQL = """
/* product_job_store:claim_outbox */
WITH candidates AS (
    SELECT o.id
    FROM product_generation_outbox AS o
    JOIN product_generation_jobs AS j ON j.id = o.job_id
    WHERE (
        (o.status = 'pending' AND o.available_at <= CURRENT_TIMESTAMP)
        OR (
            o.status = 'claimed'
            AND o.claimed_until < CURRENT_TIMESTAMP
        )
    )
      AND j.status IN ('queued', 'running')
      AND j.cancel_requested = FALSE
    ORDER BY o.available_at, o.id
    FOR UPDATE OF o, j SKIP LOCKED
    LIMIT %s
),
claimed AS (
    UPDATE product_generation_outbox AS o
    SET status = 'claimed',
        claimed_by = %s,
        claim_token = %s,
        claimed_until = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
        attempt_count = o.attempt_count + 1,
        updated_at = CURRENT_TIMESTAMP
    FROM candidates AS c
    WHERE o.id = c.id
    RETURNING
        o.id, o.job_id, o.event_type, o.payload, o.attempt_count,
        o.claim_token, o.claimed_until
),
fenced AS (
    UPDATE product_generation_jobs AS j
    SET status = 'running',
        fence = CASE
            WHEN j.status = 'queued' THEN j.fence + 1
            ELSE j.fence
        END,
        version = j.version + 1,
        started_at = COALESCE(j.started_at, CURRENT_TIMESTAMP),
        updated_at = CURRENT_TIMESTAMP
    FROM claimed AS c
    WHERE j.id = c.job_id
      AND j.status IN ('queued', 'running')
      AND j.cancel_requested = FALSE
    RETURNING
        j.id AS job_id, j.owner_user_id, j.request_sha256,
        j.request_payload, j.fence, j.version
)
SELECT
    c.id AS outbox_id, c.job_id, c.event_type,
    c.payload || jsonb_build_object('request', f.request_payload) AS payload,
    c.attempt_count, c.claim_token, c.claimed_until,
    f.owner_user_id, f.request_sha256, f.fence, f.version
FROM claimed AS c
JOIN fenced AS f ON f.job_id = c.job_id
ORDER BY c.id
"""

_MARK_OUTBOX_PUBLISHED_SQL = """
/* product_job_store:mark_outbox_published */
UPDATE product_generation_outbox
SET status = 'published',
    published_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND status = 'claimed'
  AND claim_token = %s
RETURNING id, job_id, status, claim_token, published_at
"""

_REQUEUE_PUBLISHED_OUTBOX_SQL = """
/* product_job_store:requeue_published_outbox */
UPDATE product_generation_outbox AS o
SET status = 'pending',
    available_at = CURRENT_TIMESTAMP,
    claimed_by = NULL,
    claim_token = NULL,
    claimed_until = NULL,
    published_at = NULL,
    last_error = 'redis_task_missing',
    updated_at = CURRENT_TIMESTAMP
FROM product_generation_jobs AS j
WHERE o.job_id = %s
  AND o.job_id = j.id
  AND o.status = 'published'
  AND j.status = 'running'
  AND j.fence = %s
  AND j.cancel_requested = FALSE
RETURNING o.id, o.job_id, o.status, o.available_at, o.last_error
"""

_SELECT_OUTBOX_SQL = """
/* product_job_store:select_outbox */
SELECT id, job_id, status, claim_token, published_at
FROM product_generation_outbox
WHERE id = %s
"""

_REQUEST_CANCEL_SQL = f"""
/* product_job_store:request_cancel */
UPDATE product_generation_jobs
SET cancel_requested = TRUE,
    status = CASE WHEN status = 'queued' THEN 'canceled' ELSE status END,
    refund_target_points = CASE
        WHEN status = 'queued' THEN debit_points
        ELSE refund_target_points
    END,
    version = version + 1,
    completed_at = CASE
        WHEN status = 'queued' THEN CURRENT_TIMESTAMP
        ELSE completed_at
    END,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status IN ('queued', 'running')
  AND cancel_requested = FALSE
RETURNING {_JOB_COLUMNS}
"""

_CANCEL_OUTBOX_SQL = """
/* product_job_store:cancel_outbox */
UPDATE product_generation_outbox
SET status = 'canceled',
    updated_at = CURRENT_TIMESTAMP
WHERE job_id = %s AND status IN ('pending', 'claimed')
"""

_SET_CANCELED_SETTLEMENT_TARGET_SQL = """
/* product_job_store:set_canceled_settlement_target */
UPDATE product_generation_settlements
SET refund_target_points = debit_points,
    updated_at = CURRENT_TIMESTAMP
WHERE job_id = %s AND status = 'pending'
RETURNING job_id
"""

_COMPLETE_WITH_FENCE_SQL = f"""
/* product_job_store:complete_with_fence */
UPDATE product_generation_jobs
SET status = %s,
    completed_count = %s,
    failed_count = %s,
    manifest_ref = %s,
    manifest_sha256 = %s,
    refund_target_points = CASE
        WHEN %s = 'canceled' THEN debit_points
        ELSE %s
    END,
    error_message = %s,
    version = version + 1,
    completed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND fence = %s
  AND status = 'running'
  AND (%s <> 'succeeded' OR cancel_requested = FALSE)
  AND (%s <> 'canceled' OR cancel_requested = TRUE)
RETURNING {_JOB_COLUMNS}
"""

_SET_SETTLEMENT_TARGET_SQL = """
/* product_job_store:set_settlement_target */
UPDATE product_generation_settlements
SET refund_target_points = %s,
    updated_at = CURRENT_TIMESTAMP
WHERE job_id = %s AND status = 'pending'
RETURNING job_id
"""

_CLAIM_SETTLEMENT_SQL = """
/* product_job_store:claim_settlement */
UPDATE product_generation_settlements AS s
SET status = 'claimed',
    claim_token = %s,
    claimed_by = %s,
    claimed_at = CURRENT_TIMESTAMP,
    version = s.version + 1,
    updated_at = CURRENT_TIMESTAMP
FROM product_generation_jobs AS j
WHERE s.job_id = %s
  AND s.job_id = j.id
  AND (
      s.status = 'pending'
      OR (
          s.status = 'claimed'
          AND s.claimed_at
              < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
      )
  )
  AND s.version = %s
  AND j.status IN ('succeeded', 'failed', 'canceled')
RETURNING
    s.job_id, s.owner_user_id, s.status, s.version,
    s.debit_order_id, s.debit_points,
    s.refund_order_id, s.refund_target_points, s.refund_applied_points,
    s.claim_token, s.claimed_by, s.claimed_at, s.applied_at,
    s.finalized_at, s.provider_reference, s.error_message,
    s.created_at, s.updated_at
"""

_LIST_RECONCILIATION_CANDIDATES_SQL = """
/* product_job_store:list_reconciliation_candidates */
SELECT
    j.id AS job_id,
    j.owner_user_id,
    j.request_sha256,
    j.status AS job_status,
    j.fence,
    j.cancel_requested,
    s.status AS settlement_status,
    s.version AS settlement_version,
    o.status AS outbox_status
FROM product_generation_jobs AS j
JOIN product_generation_settlements AS s ON s.job_id = j.id
LEFT JOIN product_generation_outbox AS o
    ON o.job_id = j.id
   AND o.event_type = 'product_generation.requested'
WHERE j.status IN ('queued', 'running')
   OR (
       j.status IN ('succeeded', 'failed', 'canceled')
       AND s.status IN ('pending', 'claimed')
   )
ORDER BY
    CASE
        WHEN j.status IN ('succeeded', 'failed', 'canceled') THEN 0
        ELSE 1
    END,
    j.updated_at,
    j.id
LIMIT %s
"""

_SELECT_SETTLEMENT_SQL = f"""
/* product_job_store:select_settlement */
SELECT {_SETTLEMENT_COLUMNS}
FROM product_generation_settlements
WHERE job_id = %s
"""

_INSERT_RESULT_SQL = f"""
/* product_job_store:upsert_result */
WITH writable_job AS (
    SELECT id, fence
    FROM product_generation_jobs
    WHERE id = %s
      AND fence = %s
      AND status = 'running'
      AND cancel_requested = FALSE
    FOR UPDATE
)
INSERT INTO product_generation_results (
    job_id, job_fence, menu_row, platform, variant, status,
    object_ref, object_sha256, prompt_sha256, provider_request_id,
    metadata, error_message
)
SELECT
    j.id, j.fence, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s::jsonb, %s
FROM writable_job AS j
ON CONFLICT (job_id, menu_row, platform, variant) DO NOTHING
RETURNING {_RESULT_COLUMNS}
"""

_SELECT_RESULT_CONTEXT_SQL = """
/* product_job_store:select_result_context */
SELECT
    j.id AS current_job_id,
    j.status AS current_job_status,
    j.fence AS current_job_fence,
    j.cancel_requested AS current_cancel_requested,
    r.id, r.job_id, r.job_fence, r.menu_row, r.platform, r.variant,
    r.status, r.object_ref, r.object_sha256, r.prompt_sha256,
    r.provider_request_id, r.metadata, r.error_message,
    r.created_at, r.updated_at
FROM product_generation_jobs AS j
LEFT JOIN product_generation_results AS r
  ON r.job_id = j.id
 AND r.menu_row = %s
 AND r.platform = %s
 AND r.variant = %s
WHERE j.id = %s
"""

_MARK_SETTLEMENT_APPLIED_SQL = """
/* product_job_store:mark_settlement_applied */
UPDATE product_generation_settlements
SET status = 'applied',
    refund_applied_points = %s,
    provider_reference = %s,
    error_message = '',
    applied_at = CURRENT_TIMESTAMP,
    finalized_at = CURRENT_TIMESTAMP,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE job_id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND refund_target_points = %s
RETURNING
    job_id, owner_user_id, status, version, debit_order_id, debit_points,
    refund_order_id, refund_target_points, refund_applied_points,
    claim_token, claimed_by, claimed_at, applied_at, finalized_at,
    provider_reference, error_message, created_at, updated_at
"""

_MARK_SETTLEMENT_PROBLEM_SQL = """
/* product_job_store:mark_settlement_problem */
UPDATE product_generation_settlements
SET status = %s,
    refund_applied_points = %s,
    provider_reference = %s,
    error_message = %s,
    applied_at = CASE
        WHEN %s > 0 THEN COALESCE(applied_at, CURRENT_TIMESTAMP)
        ELSE applied_at
    END,
    finalized_at = CURRENT_TIMESTAMP,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE job_id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND refund_target_points >= %s
RETURNING
    job_id, owner_user_id, status, version, debit_order_id, debit_points,
    refund_order_id, refund_target_points, refund_applied_points,
    claim_token, claimed_by, claimed_at, applied_at, finalized_at,
    provider_reference, error_message, created_at, updated_at
"""

_INSERT_POINT_ACCOUNT_SQL = f"""
/* product_job_store:create_point_account */
INSERT INTO product_point_accounts (owner_user_id, metadata)
VALUES (%s, %s::jsonb)
ON CONFLICT (owner_user_id) DO NOTHING
RETURNING {_ACCOUNT_COLUMNS}
"""

_SELECT_POINT_ACCOUNT_SQL = f"""
/* product_job_store:select_point_account */
SELECT {_ACCOUNT_COLUMNS}
FROM product_point_accounts
WHERE owner_user_id = %s
"""

_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL = f"""
/* product_job_store:select_point_account_for_update */
SELECT {_ACCOUNT_COLUMNS}
FROM product_point_accounts
WHERE owner_user_id = %s
FOR UPDATE
"""

_INSERT_POINT_ORDER_SQL = f"""
/* product_job_store:create_point_order */
INSERT INTO product_point_orders (
    id, owner_user_id, order_kind, points,
    source_order_id, source_order_kind, job_id,
    request_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s::jsonb
)
ON CONFLICT (id) DO NOTHING
RETURNING {_POINT_ORDER_COLUMNS}
"""

_SELECT_POINT_ORDER_SQL = f"""
/* product_job_store:select_point_order */
SELECT {_POINT_ORDER_COLUMNS}
FROM product_point_orders
WHERE id = %s
"""

_CREDIT_POINT_ACCOUNT_SQL = f"""
/* product_job_store:credit_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points + %s,
    lifetime_credited_points = lifetime_credited_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_DEBIT_POINT_ACCOUNT_SQL = f"""
/* product_job_store:debit_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points - %s,
    lifetime_debited_points = lifetime_debited_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
  AND balance_points >= %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_REFUND_POINT_ACCOUNT_SQL = f"""
/* product_job_store:refund_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points + %s,
    lifetime_refunded_points = lifetime_refunded_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_INSERT_POINT_LEDGER_SQL = """
/* product_job_store:create_point_ledger */
INSERT INTO product_point_ledger (
    owner_user_id, order_id, order_kind, points, delta_points,
    balance_after_points, request_sha256, metadata
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
"""

_SELECT_JOB_BY_REQUEST_FOR_UPDATE_SQL = f"""
/* product_job_store:select_job_by_request_for_update */
SELECT {_JOB_COLUMNS}
FROM product_generation_jobs
WHERE owner_user_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_ATOMIC_JOB_INTEGRITY_SQL = """
/* product_job_store:select_atomic_job_integrity */
SELECT
    j.id AS job_id,
    j.owner_user_id,
    j.request_sha256,
    j.debit_order_id,
    j.debit_points,
    p.id AS wallet_debit_order_id,
    p.owner_user_id AS wallet_owner_user_id,
    p.order_kind AS wallet_order_kind,
    p.points AS wallet_order_points,
    p.request_sha256 AS wallet_request_sha256,
    p.job_id AS wallet_job_id,
    s.job_id AS settlement_job_id,
    o.id AS outbox_id,
    o.event_type AS outbox_event_type
FROM product_generation_jobs AS j
LEFT JOIN product_point_orders AS p
  ON p.id = j.debit_order_id
 AND p.owner_user_id = j.owner_user_id
LEFT JOIN product_generation_settlements AS s
  ON s.job_id = j.id
 AND s.owner_user_id = j.owner_user_id
LEFT JOIN product_generation_outbox AS o
  ON o.job_id = j.id
 AND o.event_type = 'product_generation.requested'
WHERE j.id = %s AND j.owner_user_id = %s
"""

_SELECT_SETTLEMENT_REFUND_FOR_UPDATE_SQL = """
/* product_job_store:select_settlement_refund_for_update */
SELECT
    s.job_id, s.owner_user_id, s.status, s.version,
    s.debit_order_id, s.debit_points,
    s.refund_order_id, s.refund_target_points, s.refund_applied_points,
    s.claim_token, s.claimed_by, s.claimed_at, s.applied_at,
    s.finalized_at, s.provider_reference, s.error_message,
    s.created_at, s.updated_at,
    j.request_sha256, j.status AS job_status
FROM product_generation_settlements AS s
JOIN product_generation_jobs AS j ON j.id = s.job_id
WHERE s.job_id = %s
FOR UPDATE OF s, j
"""

_APPLY_SETTLEMENT_REFUND_SQL = """
/* product_job_store:apply_settlement_refund */
UPDATE product_generation_settlements
SET status = 'applied',
    refund_applied_points = refund_target_points,
    provider_reference = %s,
    error_message = '',
    applied_at = CURRENT_TIMESTAMP,
    finalized_at = CURRENT_TIMESTAMP,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE job_id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND refund_applied_points = 0
  AND refund_target_points <= debit_points
RETURNING
    job_id, owner_user_id, status, version, debit_order_id, debit_points,
    refund_order_id, refund_target_points, refund_applied_points,
    claim_token, claimed_by, claimed_at, applied_at, finalized_at,
    provider_reference, error_message, created_at, updated_at
"""

_SELECT_OWNED_PRIVATE_JOB_SQL = f"""
/* product_job_store:select_owned_private_job */
SELECT {_PRIVATE_JOB_COLUMNS}
FROM product_generation_jobs
WHERE id = %s AND owner_user_id = %s
"""

_SELECT_OWNED_PARENT_JOB_FOR_UPDATE_SQL = f"""
/* product_job_store:select_owned_parent_job_for_update */
SELECT {_PRIVATE_JOB_COLUMNS}
FROM product_generation_jobs
WHERE id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_PRIVATE_JOB_BY_REQUEST_FOR_UPDATE_SQL = f"""
/* product_job_store:select_private_job_by_request_for_update */
SELECT {_PRIVATE_JOB_COLUMNS}
FROM product_generation_jobs
WHERE owner_user_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_PRIVATE_JOB_BY_REQUEST_SQL = f"""
/* product_job_store:select_private_job_by_request */
SELECT {_PRIVATE_JOB_COLUMNS}
FROM product_generation_jobs
WHERE owner_user_id = %s AND idempotency_key = %s
"""

_SELECT_OWNED_SETTLEMENT_SUMMARY_SQL = """
/* product_job_store:select_owned_settlement_summary */
SELECT
    s.job_id, s.owner_user_id, s.status, s.version,
    s.debit_order_id, s.debit_points,
    s.refund_order_id, s.refund_target_points, s.refund_applied_points,
    s.applied_at, s.finalized_at, s.provider_reference,
    s.error_message, s.created_at, s.updated_at
FROM product_generation_settlements AS s
JOIN product_generation_jobs AS j ON j.id = s.job_id
WHERE s.job_id = %s AND j.owner_user_id = %s
"""

_SELECT_OWNED_OUTBOX_SUMMARY_SQL = """
/* product_job_store:select_owned_outbox_summary */
SELECT
    o.id, o.job_id, o.event_type, o.status, o.attempt_count,
    o.available_at, o.published_at, o.last_error,
    o.created_at, o.updated_at
FROM product_generation_outbox AS o
JOIN product_generation_jobs AS j ON j.id = o.job_id
WHERE o.job_id = %s AND j.owner_user_id = %s
  AND o.event_type = 'product_generation.requested'
"""

_SELECT_OWNED_RESULTS_SQL = """
/* product_job_store:select_owned_results */
SELECT
    r.id, r.job_id, r.job_fence, r.menu_row, r.platform, r.variant,
    r.status, r.object_ref, r.object_sha256, r.prompt_sha256,
    r.provider_request_id, r.metadata, r.error_message,
    r.created_at, r.updated_at
FROM product_generation_results AS r
JOIN product_generation_jobs AS j ON j.id = r.job_id
WHERE r.job_id = %s AND j.owner_user_id = %s
ORDER BY r.menu_row, r.platform, r.variant
"""

_COUNT_ACTIVE_FREE_REWORKS_SQL = """
/* product_job_store:count_active_free_reworks */
SELECT COUNT(*) AS active_free_rework_count
FROM product_generation_jobs
WHERE owner_user_id = %s
  AND status IN ('queued', 'running', 'succeeded')
  AND request_payload->>'jobType' = %s
  AND request_payload->>'parentGenerationJobId' = %s
  AND request_payload->>'mode' = 'rework'
  AND request_payload #>> '{billing,freeReworkQuotaVerified}' = 'true'
"""


class ProductJobStore:
    """PostgreSQL product-batch state operations over an injected DB-API connection.

    The store never opens a connection and never executes schema migrations.
    Each public method owns one short transaction on the supplied connection.
    """

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductJobInput(
                "ProductJobStore requires an autocommit-disabled connection"
            )
        self.connection = connection

    def get_or_create_account(
        self,
        *,
        owner_user_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> AccountCreateResult:
        """Create a zero-balance account or return the existing account unchanged.

        ``metadata`` is initial account metadata only; a replay never overwrites the
        metadata of an existing account.
        """

        owner = _identifier(owner_user_id, "owner_user_id")
        metadata_json = _wallet_metadata_payload(metadata or {}, "metadata")
        with self._transaction() as cursor:
            cursor.execute(_INSERT_POINT_ACCOUNT_SQL, (owner, metadata_json))
            inserted = _fetchone_dict(cursor)
            if inserted is not None:
                return AccountCreateResult(
                    account=_decode_row(inserted),
                    created=True,
                )
            cursor.execute(_SELECT_POINT_ACCOUNT_SQL, (owner,))
            current = _fetchone_dict(cursor)
            if current is None:
                raise WalletIntegrityError(
                    f"account insert conflict did not resolve: {owner}"
                )
            return AccountCreateResult(
                account=_decode_row(current),
                created=False,
            )

    def get_account(self, *, owner_user_id: str) -> dict[str, Any] | None:
        """Return an existing account, or ``None`` without creating one."""

        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_POINT_ACCOUNT_SQL, (owner,))
            current = _fetchone_dict(cursor)
            return _decode_row(current) if current is not None else None

    def credit_points(
        self,
        *,
        owner_user_id: str,
        order_id: str,
        points: int,
        request_sha256: str,
        metadata: Mapping[str, Any] | None = None,
        transaction_effect: (
            Callable[[CursorLike, PointMutationResult], None] | None
        ) = None,
    ) -> PointMutationResult:
        """Apply one positive credit order exactly once.

        Replaying the same order ID with identical owner, amount, digest, and
        metadata returns ``idempotent=True``. Any content drift is a conflict.
        """

        owner = _identifier(owner_user_id, "owner_user_id")
        order_identifier = _identifier(order_id, "order_id")
        amount = _positive_int(points, "points")
        digest = _sha256(request_sha256, "request_sha256")
        metadata_json = _wallet_metadata_payload(metadata or {}, "metadata")
        expected_metadata = json.loads(metadata_json)

        with self._transaction() as cursor:
            def finish(result: PointMutationResult) -> PointMutationResult:
                if transaction_effect is not None:
                    transaction_effect(cursor, result)
                return result

            account = self._ensure_account_locked(
                cursor,
                owner=owner,
                metadata_json="{}",
            )
            cursor.execute(_SELECT_POINT_ORDER_SQL, (order_identifier,))
            existing = _fetchone_dict(cursor)
            if existing is not None:
                existing = _decode_row(existing)
                if _same_point_order(
                    existing,
                    owner_user_id=owner,
                    order_kind="credit",
                    points=amount,
                    source_order_id=None,
                    job_id=None,
                    request_sha256=digest,
                    metadata=expected_metadata,
                ):
                    return finish(
                        PointMutationResult(
                            account=account,
                            order=existing,
                            idempotent=True,
                        )
                    )
                raise PointOrderConflict(
                    f"point order already exists with different content: {order_identifier}"
                )

            cursor.execute(
                _INSERT_POINT_ORDER_SQL,
                (
                    order_identifier,
                    owner,
                    "credit",
                    amount,
                    None,
                    None,
                    None,
                    digest,
                    metadata_json,
                ),
            )
            inserted = _fetchone_dict(cursor)
            if inserted is None:
                cursor.execute(_SELECT_POINT_ORDER_SQL, (order_identifier,))
                raced = _fetchone_dict(cursor)
                if raced is not None:
                    raced = _decode_row(raced)
                    if _same_point_order(
                        raced,
                        owner_user_id=owner,
                        order_kind="credit",
                        points=amount,
                        source_order_id=None,
                        job_id=None,
                        request_sha256=digest,
                        metadata=expected_metadata,
                    ):
                        return finish(
                            PointMutationResult(
                                account=account,
                                order=raced,
                                idempotent=True,
                            )
                        )
                raise PointOrderConflict(
                    f"point order insert lost an incompatible race: {order_identifier}"
                )

            cursor.execute(
                _CREDIT_POINT_ACCOUNT_SQL,
                (amount, amount, owner),
            )
            credited_account = _fetchone_dict(cursor)
            if credited_account is None:
                raise WalletIntegrityError(
                    f"credit account disappeared while locked: {owner}"
                )
            credited_account = _decode_row(credited_account)
            cursor.execute(
                _INSERT_POINT_LEDGER_SQL,
                (
                    owner,
                    order_identifier,
                    "credit",
                    amount,
                    amount,
                    int(credited_account["balance_points"]),
                    digest,
                    metadata_json,
                ),
            )
            return finish(
                PointMutationResult(
                    account=credited_account,
                    order=_decode_row(inserted),
                    idempotent=False,
                )
            )

    def debit_points(
        self,
        *,
        owner_user_id: str,
        order_id: str,
        points: int,
        request_sha256: str,
        metadata: Mapping[str, Any] | None = None,
        transaction_effect: (
            Callable[[CursorLike, PointMutationResult], None] | None
        ) = None,
    ) -> PointMutationResult:
        """Apply one non-job debit exactly once under an account row lock."""

        owner = _identifier(owner_user_id, "owner_user_id")
        order_identifier = _identifier(order_id, "order_id")
        amount = _positive_int(points, "points")
        digest = _sha256(request_sha256, "request_sha256")
        metadata_json = _wallet_metadata_payload(metadata or {}, "metadata")
        expected_metadata = json.loads(metadata_json)

        with self._transaction() as cursor:
            def finish(result: PointMutationResult) -> PointMutationResult:
                if transaction_effect is not None:
                    transaction_effect(cursor, result)
                return result

            account = self._ensure_account_locked(
                cursor,
                owner=owner,
                metadata_json="{}",
            )
            cursor.execute(_SELECT_POINT_ORDER_SQL, (order_identifier,))
            existing = _fetchone_dict(cursor)
            if existing is not None:
                existing = _decode_row(existing)
                if _same_point_order(
                    existing,
                    owner_user_id=owner,
                    order_kind="debit",
                    points=amount,
                    source_order_id=None,
                    job_id=None,
                    request_sha256=digest,
                    metadata=expected_metadata,
                ):
                    return finish(
                        PointMutationResult(
                            account=account,
                            order=existing,
                            idempotent=True,
                        )
                    )
                raise PointOrderConflict(
                    f"point order already exists with different content: {order_identifier}"
                )

            available = int(account["balance_points"])
            if available < amount:
                raise InsufficientPointBalance(
                    available_points=available,
                    required_points=amount,
                )
            cursor.execute(
                _INSERT_POINT_ORDER_SQL,
                (
                    order_identifier,
                    owner,
                    "debit",
                    amount,
                    None,
                    None,
                    None,
                    digest,
                    metadata_json,
                ),
            )
            inserted = _fetchone_dict(cursor)
            if inserted is None:
                raise PointOrderConflict(
                    f"point debit order already exists: {order_identifier}"
                )
            cursor.execute(
                _DEBIT_POINT_ACCOUNT_SQL,
                (amount, amount, owner, amount),
            )
            debited_account = _fetchone_dict(cursor)
            if debited_account is None:
                raise InsufficientPointBalance(
                    available_points=available,
                    required_points=amount,
                )
            debited_account = _decode_row(debited_account)
            cursor.execute(
                _INSERT_POINT_LEDGER_SQL,
                (
                    owner,
                    order_identifier,
                    "debit",
                    amount,
                    -amount,
                    int(debited_account["balance_points"]),
                    digest,
                    metadata_json,
                ),
            )
            return finish(
                PointMutationResult(
                    account=debited_account,
                    order=_decode_row(inserted),
                    idempotent=False,
                )
            )

    def create_or_get_job_with_debit(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        request_sha256: str,
        request_payload: Mapping[str, Any],
        menu_upload_id: str,
        menu_object_ref: str,
        menu_object_sha256: str,
        selected_background_ref: str,
        selected_background_sha256: str,
        requested_count: int,
        debit_order_id: str,
        debit_points: int,
        refund_order_id: str,
        job_id: str | None = None,
        outbox_id: str | None = None,
        account_metadata: Mapping[str, Any] | None = None,
        debit_metadata: Mapping[str, Any] | None = None,
    ) -> CreateOrGetResult:
        """Atomically debit points and create the job, settlement, and outbox.

        The outbox contains only a server-owned job pointer and request digest.
        Dispatchers must reload the immutable request payload from PostgreSQL.
        """

        owner = _identifier(owner_user_id, "owner_user_id")
        idem_key = _identifier(idempotency_key, "idempotency_key")
        digest = _sha256(request_sha256, "request_sha256")
        menu_digest = _sha256(menu_object_sha256, "menu_object_sha256")
        background_digest = _sha256(
            selected_background_sha256, "selected_background_sha256"
        )
        job_identifier = _identifier(job_id or str(uuid4()), "job_id")
        outbox_identifier = _identifier(outbox_id or str(uuid4()), "outbox_id")
        debit_order = _identifier(debit_order_id, "debit_order_id")
        refund_order = _identifier(refund_order_id, "refund_order_id")
        requested = _nonnegative_int(requested_count, "requested_count")
        debit = _nonnegative_int(debit_points, "debit_points")
        request_json = _json_payload(request_payload, "request_payload")
        account_metadata_json = _wallet_metadata_payload(
            account_metadata or {},
            "account_metadata",
        )
        debit_metadata_json = _wallet_metadata_payload(
            {
                "jobId": job_identifier,
                "purpose": "product_generation",
                "context": dict(debit_metadata or {}),
            },
            "debit_metadata",
        )
        event_json = _json_payload(
            {
                "schemaVersion": 1,
                "jobId": job_identifier,
                "ownerUserId": owner,
                "requestSha256": digest,
            },
            "outbox_payload",
        )
        job_values = (
            job_identifier,
            owner,
            idem_key,
            digest,
            request_json,
            _identifier(menu_upload_id, "menu_upload_id"),
            _identifier(menu_object_ref, "menu_object_ref"),
            menu_digest,
            _identifier(selected_background_ref, "selected_background_ref"),
            background_digest,
            requested,
            debit_order,
            debit,
            refund_order,
        )

        with self._transaction() as cursor:
            account = self._ensure_account_locked(
                cursor,
                owner=owner,
                metadata_json=account_metadata_json,
            )
            cursor.execute(
                _SELECT_JOB_BY_REQUEST_FOR_UPDATE_SQL,
                (owner, idem_key),
            )
            existing = _fetchone_dict(cursor)
            if existing is not None:
                existing = _decode_row(existing)
                self._validate_job_replay(cursor, existing, digest)
                return CreateOrGetResult(job=existing, created=False)

            available = int(account["balance_points"])
            if available < debit:
                raise InsufficientPointBalance(
                    available_points=available,
                    required_points=debit,
                )

            cursor.execute(_INSERT_JOB_SQL, job_values)
            inserted_job = _fetchone_dict(cursor)
            if inserted_job is None:
                cursor.execute(_SELECT_JOB_BY_REQUEST_SQL, (owner, idem_key))
                raced = _fetchone_dict(cursor)
                if raced is None:
                    raise WalletIntegrityError(
                        "job insert conflict did not resolve to a durable job"
                    )
                raced = _decode_row(raced)
                self._validate_job_replay(cursor, raced, digest)
                return CreateOrGetResult(job=raced, created=False)

            cursor.execute(
                _INSERT_POINT_ORDER_SQL,
                (
                    debit_order,
                    owner,
                    "debit",
                    debit,
                    None,
                    None,
                    job_identifier,
                    digest,
                    debit_metadata_json,
                ),
            )
            inserted_order = _fetchone_dict(cursor)
            if inserted_order is None:
                raise PointOrderConflict(
                    f"debit order already exists: {debit_order}"
                )

            cursor.execute(
                _DEBIT_POINT_ACCOUNT_SQL,
                (debit, debit, owner, debit),
            )
            debited_account = _fetchone_dict(cursor)
            if debited_account is None:
                raise InsufficientPointBalance(
                    available_points=available,
                    required_points=debit,
                )
            debited_account = _decode_row(debited_account)
            cursor.execute(
                _INSERT_POINT_LEDGER_SQL,
                (
                    owner,
                    debit_order,
                    "debit",
                    debit,
                    -debit,
                    int(debited_account["balance_points"]),
                    digest,
                    debit_metadata_json,
                ),
            )
            cursor.execute(
                _INSERT_SETTLEMENT_SQL,
                (
                    job_identifier,
                    owner,
                    debit_order,
                    debit,
                    refund_order,
                ),
            )
            cursor.execute(
                _INSERT_OUTBOX_SQL,
                (outbox_identifier, job_identifier, event_json),
            )
            return CreateOrGetResult(
                job=_decode_row(inserted_job),
                created=True,
            )

    def create_or_get_job(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        request_sha256: str,
        request_payload: Mapping[str, Any],
        menu_upload_id: str,
        menu_object_ref: str,
        menu_object_sha256: str,
        selected_background_ref: str,
        selected_background_sha256: str,
        requested_count: int,
        debit_order_id: str,
        debit_points: int,
        refund_order_id: str,
        job_id: str | None = None,
        outbox_id: str | None = None,
    ) -> CreateOrGetResult:
        owner = _identifier(owner_user_id, "owner_user_id")
        idem_key = _identifier(idempotency_key, "idempotency_key")
        digest = _sha256(request_sha256, "request_sha256")
        menu_digest = _sha256(menu_object_sha256, "menu_object_sha256")
        background_digest = _sha256(
            selected_background_sha256, "selected_background_sha256"
        )
        job_identifier = _identifier(job_id or str(uuid4()), "job_id")
        outbox_identifier = _identifier(outbox_id or str(uuid4()), "outbox_id")
        requested = _nonnegative_int(requested_count, "requested_count")
        debit = _nonnegative_int(debit_points, "debit_points")
        request_json = _json_payload(request_payload, "request_payload")
        event_json = _json_payload(
            {
                "jobId": job_identifier,
                "ownerUserId": owner,
                "requestSha256": digest,
                "request": dict(request_payload),
            },
            "outbox_payload",
        )

        values = (
            job_identifier,
            owner,
            idem_key,
            digest,
            request_json,
            _identifier(menu_upload_id, "menu_upload_id"),
            _identifier(menu_object_ref, "menu_object_ref"),
            menu_digest,
            _identifier(selected_background_ref, "selected_background_ref"),
            background_digest,
            requested,
            _identifier(debit_order_id, "debit_order_id"),
            debit,
            _identifier(refund_order_id, "refund_order_id"),
        )

        with self._transaction() as cursor:
            cursor.execute(_INSERT_JOB_SQL, values)
            inserted = _fetchone_dict(cursor)
            if inserted is None:
                cursor.execute(_SELECT_JOB_BY_REQUEST_SQL, (owner, idem_key))
                existing = _fetchone_dict(cursor)
                if existing is None:
                    raise ProductJobStoreError(
                        "idempotency conflict did not resolve to an existing job"
                    )
                existing_digest = str(existing["request_sha256"])
                if not hmac.compare_digest(existing_digest, digest):
                    raise RequestDigestConflict(str(existing["id"]))
                return CreateOrGetResult(job=_decode_row(existing), created=False)

            cursor.execute(
                _INSERT_SETTLEMENT_SQL,
                (
                    job_identifier,
                    owner,
                    _identifier(debit_order_id, "debit_order_id"),
                    debit,
                    _identifier(refund_order_id, "refund_order_id"),
                ),
            )
            cursor.execute(
                _INSERT_OUTBOX_SQL,
                (outbox_identifier, job_identifier, event_json),
            )
            return CreateOrGetResult(job=_decode_row(inserted), created=True)

    def create_or_get_revision_job_with_quota(
        self,
        *,
        owner_user_id: str,
        parent_generation_job_id: str,
        revision_job_type: str,
        idempotency_key: str,
        free_rework_limit: int,
        free_candidate: RevisionJobCandidate,
        paid_candidate: RevisionJobCandidate,
    ) -> RevisionJobCreateResult:
        """Choose and create one immutable revision candidate atomically.

        Locking the owned parent generation row serializes quota decisions for
        every revision of that parent. An existing idempotency key is resolved
        before quota evaluation so a replay keeps its originally selected
        free/paid request even after the quota changes.
        """

        owner = _identifier(owner_user_id, "owner_user_id")
        parent_job_id = _identifier(
            parent_generation_job_id,
            "parent_generation_job_id",
        )
        job_type = _identifier(revision_job_type, "revision_job_type")
        idem_key = _identifier(idempotency_key, "idempotency_key")
        quota = _nonnegative_int(free_rework_limit, "free_rework_limit")
        free = _prepare_revision_candidate(
            free_candidate,
            kind="free",
            owner_user_id=owner,
            parent_generation_job_id=parent_job_id,
            revision_job_type=job_type,
            idempotency_key=idem_key,
        )
        paid = _prepare_revision_candidate(
            paid_candidate,
            kind="paid",
            owner_user_id=owner,
            parent_generation_job_id=parent_job_id,
            revision_job_type=job_type,
            idempotency_key=idem_key,
        )
        _validate_revision_candidate_pair(free, paid)

        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_OWNED_PARENT_JOB_FOR_UPDATE_SQL,
                (parent_job_id, owner),
            )
            parent = _fetchone_dict(cursor)
            if parent is None:
                raise JobNotFound(parent_job_id)
            parent = _decode_row(parent)
            _validate_parent_generation_job(parent)
            _validate_revision_candidate_parent(free, parent)
            _validate_revision_candidate_parent(paid, parent)

            cursor.execute(
                _SELECT_PRIVATE_JOB_BY_REQUEST_FOR_UPDATE_SQL,
                (owner, idem_key),
            )
            existing = _fetchone_dict(cursor)
            if existing is not None:
                return self._revision_replay_result(
                    cursor,
                    existing=_decode_row(existing),
                    free=free,
                    paid=paid,
                    revision_job_type=job_type,
                    parent_generation_job_id=parent_job_id,
                )

            cursor.execute(
                _COUNT_ACTIVE_FREE_REWORKS_SQL,
                (owner, job_type, parent_job_id),
            )
            count_row = _fetchone_dict(cursor)
            if count_row is None:
                raise ProductJobStoreError(
                    "active free rework count query returned no row"
                )
            active_free_reworks = _nonnegative_int(
                count_row.get("active_free_rework_count"),
                "active_free_rework_count",
            )
            selected = free if active_free_reworks < quota else paid

            available_points = 0
            if selected.kind == "paid":
                account = self._ensure_account_locked(
                    cursor,
                    owner=owner,
                    metadata_json=selected.account_metadata_json,
                )
                available_points = int(account["balance_points"])
                if available_points < selected.debit_points:
                    raise InsufficientPointBalance(
                        available_points=available_points,
                        required_points=selected.debit_points,
                    )

            cursor.execute(
                _INSERT_JOB_SQL,
                selected.job_values(
                    owner_user_id=owner,
                    idempotency_key=idem_key,
                ),
            )
            inserted_job = _fetchone_dict(cursor)
            if inserted_job is None:
                cursor.execute(
                    _SELECT_PRIVATE_JOB_BY_REQUEST_SQL,
                    (owner, idem_key),
                )
                raced = _fetchone_dict(cursor)
                if raced is None:
                    raise WalletIntegrityError(
                        "revision job insert conflict did not resolve to a durable job"
                    )
                return self._revision_replay_result(
                    cursor,
                    existing=_decode_row(raced),
                    free=free,
                    paid=paid,
                    revision_job_type=job_type,
                    parent_generation_job_id=parent_job_id,
                )

            if selected.kind == "paid":
                cursor.execute(
                    _INSERT_POINT_ORDER_SQL,
                    (
                        selected.debit_order_id,
                        owner,
                        "debit",
                        selected.debit_points,
                        None,
                        None,
                        selected.job_id,
                        selected.digest,
                        selected.debit_metadata_json,
                    ),
                )
                inserted_order = _fetchone_dict(cursor)
                if inserted_order is None:
                    raise PointOrderConflict(
                        "debit order already exists: "
                        f"{selected.debit_order_id}"
                    )
                cursor.execute(
                    _DEBIT_POINT_ACCOUNT_SQL,
                    (
                        selected.debit_points,
                        selected.debit_points,
                        owner,
                        selected.debit_points,
                    ),
                )
                debited_account = _fetchone_dict(cursor)
                if debited_account is None:
                    raise InsufficientPointBalance(
                        available_points=available_points,
                        required_points=selected.debit_points,
                    )
                debited_account = _decode_row(debited_account)
                cursor.execute(
                    _INSERT_POINT_LEDGER_SQL,
                    (
                        owner,
                        selected.debit_order_id,
                        "debit",
                        selected.debit_points,
                        -selected.debit_points,
                        int(debited_account["balance_points"]),
                        selected.digest,
                        selected.debit_metadata_json,
                    ),
                )

            cursor.execute(
                _INSERT_SETTLEMENT_SQL,
                (
                    selected.job_id,
                    owner,
                    selected.debit_order_id,
                    selected.debit_points,
                    selected.refund_order_id,
                ),
            )
            cursor.execute(
                _INSERT_OUTBOX_SQL,
                (
                    selected.outbox_id,
                    selected.job_id,
                    selected.event_json,
                ),
            )
            job = _decode_row(inserted_job)
            job["request_payload"] = dict(selected.request_payload)
            return RevisionJobCreateResult(
                job=job,
                created=True,
                selected_candidate=selected.kind,
                request_payload=dict(selected.request_payload),
            )

    def get_owned_job(
        self,
        *,
        job_id: str,
        owner_user_id: str,
    ) -> dict[str, Any] | None:
        """Return a private owned job record, or ``None`` for missing/wrong owner.

        The returned mapping contains frozen request and private object references.
        It is a persistence DTO and must not be returned directly from an HTTP API.
        """

        identifier = _identifier(job_id, "job_id")
        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_OWNED_PRIVATE_JOB_SQL,
                (identifier, owner),
            )
            row = _fetchone_dict(cursor)
            return _decode_row(row) if row is not None else None

    def get_owned_job_detail(
        self,
        *,
        job_id: str,
        owner_user_id: str,
    ) -> OwnedJobDetail | None:
        """Return a private recovery view without settlement or outbox claim tokens."""

        identifier = _identifier(job_id, "job_id")
        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_OWNED_PRIVATE_JOB_SQL,
                (identifier, owner),
            )
            job = _fetchone_dict(cursor)
            if job is None:
                return None

            cursor.execute(
                _SELECT_OWNED_SETTLEMENT_SUMMARY_SQL,
                (identifier, owner),
            )
            settlement = _fetchone_dict(cursor)
            cursor.execute(
                _SELECT_OWNED_OUTBOX_SUMMARY_SQL,
                (identifier, owner),
            )
            outbox = _fetchone_dict(cursor)
            cursor.execute(
                _SELECT_OWNED_RESULTS_SQL,
                (identifier, owner),
            )
            results = tuple(
                _decode_row(row) for row in _fetchall_dicts(cursor)
            )
            return OwnedJobDetail(
                job=_decode_row(job),
                settlement=(
                    _decode_row(settlement) if settlement is not None else None
                ),
                outbox=_decode_row(outbox) if outbox is not None else None,
                results=results,
            )

    def count_active_free_reworks(
        self,
        *,
        owner_user_id: str,
        parent_generation_job_id: str,
        revision_job_type: str,
    ) -> int:
        owner = _identifier(owner_user_id, "owner_user_id")
        parent_job = _identifier(
            parent_generation_job_id,
            "parent_generation_job_id",
        )
        job_type = _identifier(revision_job_type, "revision_job_type")
        with self._transaction() as cursor:
            cursor.execute(
                _COUNT_ACTIVE_FREE_REWORKS_SQL,
                (owner, job_type, parent_job),
            )
            row = _fetchone_dict(cursor)
            if row is None:
                raise ProductJobStoreError(
                    "active free rework count query returned no row"
                )
            return _nonnegative_int(
                row.get("active_free_rework_count"),
                "active_free_rework_count",
            )

    def list_reconciliation_candidates(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        batch_limit = _positive_int(limit, "limit")
        if batch_limit > 1000:
            raise InvalidProductJobInput("limit must not exceed 1000")
        with self._transaction() as cursor:
            cursor.execute(
                _LIST_RECONCILIATION_CANDIDATES_SQL,
                (batch_limit,),
            )
            return [
                _decode_row(row)
                for row in _fetchall_dicts(cursor)
            ]

    def claim_outbox(
        self,
        *,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 60,
        claim_token: str | None = None,
    ) -> list[dict[str, Any]]:
        worker = _identifier(worker_id, "worker_id")
        batch_limit = _positive_int(limit, "limit")
        lease = _positive_int(lease_seconds, "lease_seconds")
        token = _identifier(claim_token or str(uuid4()), "claim_token")
        with self._transaction() as cursor:
            cursor.execute(
                _CLAIM_OUTBOX_SQL,
                (batch_limit, worker, token, lease),
            )
            return [_decode_row(row) for row in _fetchall_dicts(cursor)]

    def mark_outbox_published(
        self,
        *,
        outbox_id: str,
        claim_token: str,
    ) -> dict[str, Any]:
        identifier = _identifier(outbox_id, "outbox_id")
        token = _identifier(claim_token, "claim_token")
        with self._transaction() as cursor:
            cursor.execute(_MARK_OUTBOX_PUBLISHED_SQL, (identifier, token))
            published = _fetchone_dict(cursor)
            if published is not None:
                return _decode_row(published)

            cursor.execute(_SELECT_OUTBOX_SQL, (identifier,))
            current = _fetchone_dict(cursor)
            if (
                current is not None
                and current.get("status") == "published"
                and hmac.compare_digest(str(current.get("claim_token") or ""), token)
            ):
                return _decode_row(current)
            raise OutboxClaimLost(f"outbox claim is no longer current: {identifier}")

    def requeue_published_outbox_if_task_missing(
        self,
        *,
        job_id: str,
        expected_fence: int,
    ) -> dict[str, Any] | None:
        identifier = _identifier(job_id, "job_id")
        fence = _positive_int(expected_fence, "expected_fence")
        with self._transaction() as cursor:
            cursor.execute(
                _REQUEUE_PUBLISHED_OUTBOX_SQL,
                (identifier, fence),
            )
            changed = _fetchone_dict(cursor)
            return _decode_row(changed) if changed is not None else None

    def upsert_result(
        self,
        *,
        job_id: str,
        fence: int,
        menu_row: int,
        platform: str,
        variant: str,
        status: str,
        object_ref: str | None = None,
        object_sha256: str | None = None,
        prompt_sha256: str | None = None,
        provider_request_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        error_message: str = "",
    ) -> ResultWriteResult:
        identifier = _identifier(job_id, "job_id")
        current_fence = _positive_int(fence, "fence")
        row_number = _nonnegative_int(menu_row, "menu_row")
        normalized_platform = _slot_part(platform, "platform")
        normalized_variant = _slot_part(variant, "variant")
        result_status = str(status or "").strip().lower()
        if result_status not in {"generated", "failed", "rejected"}:
            raise InvalidProductJobInput(
                "result status must be generated, failed, or rejected"
            )
        object_reference, object_digest = _result_object_pair(
            object_ref, object_sha256, result_status
        )
        prompt_digest = (
            _sha256(prompt_sha256, "prompt_sha256")
            if prompt_sha256 is not None
            else None
        )
        provider_id = _optional_identifier(
            provider_request_id, "provider_request_id"
        )
        metadata_json = _json_payload(metadata or {}, "metadata")
        error = str(error_message or "")
        if result_status == "failed" and not error.strip():
            raise InvalidProductJobInput("failed result requires error_message")

        params = (
            identifier,
            current_fence,
            row_number,
            normalized_platform,
            normalized_variant,
            result_status,
            object_reference,
            object_digest,
            prompt_digest,
            provider_id,
            metadata_json,
            error,
        )
        with self._transaction() as cursor:
            cursor.execute(_INSERT_RESULT_SQL, params)
            inserted = _fetchone_dict(cursor)
            if inserted is not None:
                return ResultWriteResult(
                    result=_decode_row(inserted),
                    created=True,
                )

            cursor.execute(
                _SELECT_RESULT_CONTEXT_SQL,
                (
                    row_number,
                    normalized_platform,
                    normalized_variant,
                    identifier,
                ),
            )
            context = _fetchone_dict(cursor)
            if context is None:
                raise JobNotFound(identifier)
            actual_fence = int(context["current_job_fence"])
            if actual_fence != current_fence:
                raise FenceMismatch(current_fence, actual_fence)
            if bool(context["current_cancel_requested"]):
                raise CancellationRequested(identifier)
            if context["current_job_status"] != "running":
                raise JobStateConflict(
                    "result writes require a running job: "
                    f"{identifier} is {context['current_job_status']}"
                )
            if context.get("id") is None:
                raise ProductJobStoreError(
                    f"result insert produced no row for writable job: {identifier}"
                )

            existing = _result_from_context(context)
            if _same_result(
                existing,
                status=result_status,
                object_ref=object_reference,
                object_sha256=object_digest,
                prompt_sha256=prompt_digest,
                provider_request_id=provider_id,
                metadata=json.loads(metadata_json),
                error_message=error,
            ):
                return ResultWriteResult(result=existing, created=False)
            raise ResultWriteConflict(
                "result slot was already written with different content: "
                f"{identifier}/{row_number}/{normalized_platform}/"
                f"{normalized_variant}"
            )

    def request_cancel(
        self,
        *,
        job_id: str,
        owner_user_id: str,
    ) -> CancelRequestResult:
        identifier = _identifier(job_id, "job_id")
        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(_REQUEST_CANCEL_SQL, (identifier, owner))
            changed = _fetchone_dict(cursor)
            if changed is not None:
                changed = _decode_row(changed)
                if changed["status"] == "canceled":
                    cursor.execute(_CANCEL_OUTBOX_SQL, (identifier,))
                    cursor.execute(
                        _SET_CANCELED_SETTLEMENT_TARGET_SQL, (identifier,)
                    )
                    if _fetchone_dict(cursor) is None:
                        raise SettlementConflict(
                            f"pending settlement is missing for canceled job: {identifier}"
                        )
                return CancelRequestResult(job=changed, idempotent=False)

            cursor.execute(_SELECT_OWNED_JOB_SQL, (identifier, owner))
            current = _fetchone_dict(cursor)
            if current is None:
                raise JobNotFound(identifier)
            current = _decode_row(current)
            if bool(current["cancel_requested"]):
                return CancelRequestResult(job=current, idempotent=True)
            raise JobStateConflict(
                f"cannot cancel job in state {current['status']}: {identifier}"
            )

    def complete_with_fence(
        self,
        *,
        job_id: str,
        fence: int,
        terminal_status: str,
        completed_count: int,
        failed_count: int,
        refund_target_points: int,
        manifest_ref: str | None = None,
        manifest_sha256: str | None = None,
        error_message: str = "",
    ) -> CompletionResult:
        identifier = _identifier(job_id, "job_id")
        current_fence = _nonnegative_int(fence, "fence")
        status = str(terminal_status).strip().lower()
        if status not in TERMINAL_STATUSES:
            raise InvalidProductJobInput(
                "terminal_status must be succeeded, failed, or canceled"
            )
        completed = _nonnegative_int(completed_count, "completed_count")
        failed = _nonnegative_int(failed_count, "failed_count")
        refund_target = _nonnegative_int(
            refund_target_points, "refund_target_points"
        )
        manifest_reference, manifest_digest = _manifest_pair(
            manifest_ref, manifest_sha256, status
        )

        params = (
            status,
            completed,
            failed,
            manifest_reference,
            manifest_digest,
            status,
            refund_target,
            str(error_message or ""),
            identifier,
            current_fence,
            status,
            status,
        )
        with self._transaction() as cursor:
            cursor.execute(_COMPLETE_WITH_FENCE_SQL, params)
            changed = _fetchone_dict(cursor)
            if changed is not None:
                changed = _decode_row(changed)
                cursor.execute(
                    _SET_SETTLEMENT_TARGET_SQL,
                    (changed["refund_target_points"], identifier),
                )
                if _fetchone_dict(cursor) is None:
                    raise SettlementConflict(
                        f"pending settlement is missing for completed job: {identifier}"
                    )
                return CompletionResult(job=changed, idempotent=False)

            cursor.execute(_SELECT_JOB_SQL, (identifier,))
            current = _fetchone_dict(cursor)
            if current is None:
                raise JobNotFound(identifier)
            current = _decode_row(current)
            if _same_completion(
                current,
                fence=current_fence,
                status=status,
                completed_count=completed,
                failed_count=failed,
                manifest_ref=manifest_reference,
                manifest_sha256=manifest_digest,
                refund_target_points=refund_target,
            ):
                return CompletionResult(job=current, idempotent=True)
            actual_fence = int(current["fence"])
            if actual_fence != current_fence:
                raise FenceMismatch(current_fence, actual_fence)
            if bool(current["cancel_requested"]) and status == "succeeded":
                raise CancellationRequested(identifier)
            raise JobStateConflict(
                f"cannot complete job in state {current['status']}: {identifier}"
            )

    def claim_settlement_once(
        self,
        *,
        job_id: str,
        expected_version: int,
        claimed_by: str,
        claim_token: str | None = None,
        reclaim_after_seconds: int = 300,
    ) -> SettlementClaimResult:
        identifier = _identifier(job_id, "job_id")
        version = _nonnegative_int(expected_version, "expected_version")
        actor = _identifier(claimed_by, "claimed_by")
        token = _identifier(claim_token or str(uuid4()), "claim_token")
        reclaim_after = _positive_int(
            reclaim_after_seconds,
            "reclaim_after_seconds",
        )
        with self._transaction() as cursor:
            cursor.execute(
                _CLAIM_SETTLEMENT_SQL,
                (
                    token,
                    actor,
                    identifier,
                    reclaim_after,
                    version,
                ),
            )
            claimed = _fetchone_dict(cursor)
            if claimed is not None:
                return SettlementClaimResult(
                    settlement=_decode_row(claimed), idempotent=False
                )

            cursor.execute(_SELECT_SETTLEMENT_SQL, (identifier,))
            current = _fetchone_dict(cursor)
            if current is None:
                raise JobNotFound(identifier)
            current = _decode_row(current)
            if (
                current.get("status")
                in {"claimed", "applied", "failed", "manual_review"}
                and hmac.compare_digest(
                    str(current.get("claim_token") or ""), token
                )
            ):
                return SettlementClaimResult(
                    settlement=current, idempotent=True
                )
            raise SettlementConflict(
                "settlement CAS failed for "
                f"{identifier}: status={current.get('status')} "
                f"version={current.get('version')}"
            )

    def apply_settlement_refund(
        self,
        *,
        job_id: str,
        claim_token: str,
        provider_reference: str,
    ) -> SettlementRefundResult:
        """Credit the database-owned refund target and finalize once.

        No caller-supplied refund amount is accepted. The locked settlement row is
        the sole source of the amount and source debit order.
        """

        identifier = _identifier(job_id, "job_id")
        token = _identifier(claim_token, "claim_token")
        provider_ref = _identifier(provider_reference, "provider_reference")
        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_SETTLEMENT_REFUND_FOR_UPDATE_SQL,
                (identifier,),
            )
            settlement = _fetchone_dict(cursor)
            if settlement is None:
                raise JobNotFound(identifier)
            settlement = _decode_row(settlement)

            current_token = str(settlement.get("claim_token") or "")
            if not hmac.compare_digest(current_token, token):
                raise SettlementConflict(
                    f"settlement claim token mismatch for {identifier}"
                )
            if settlement.get("job_status") not in TERMINAL_STATUSES:
                raise WalletIntegrityError(
                    f"refund settlement job is not terminal: {identifier}"
                )
            refund_target = _nonnegative_int(
                settlement.get("refund_target_points"),
                "refund_target_points",
            )
            debit_points = _nonnegative_int(
                settlement.get("debit_points"),
                "debit_points",
            )
            if refund_target > debit_points:
                raise WalletIntegrityError(
                    f"refund target exceeds debit for {identifier}"
                )

            owner = _identifier(
                settlement.get("owner_user_id"),
                "owner_user_id",
            )
            refund_order_id = _identifier(
                settlement.get("refund_order_id"),
                "refund_order_id",
            )
            debit_order_id = _identifier(
                settlement.get("debit_order_id"),
                "debit_order_id",
            )
            request_digest = _sha256(
                settlement.get("request_sha256"),
                "request_sha256",
            )
            cursor.execute(
                _SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL,
                (owner,),
            )
            account = _fetchone_dict(cursor)
            if account is None:
                raise WalletIntegrityError(
                    f"refund account is missing: {owner}"
                )
            account = _decode_row(account)

            if refund_target == 0:
                if settlement.get("status") == "applied":
                    if (
                        int(settlement.get("refund_applied_points") or 0) != 0
                        or str(settlement.get("provider_reference") or "")
                        != provider_ref
                    ):
                        raise SettlementConflict(
                            "zero-refund settlement already finalized "
                            f"differently: {identifier}"
                        )
                    return SettlementRefundResult(
                        account=account,
                        order=None,
                        settlement=settlement,
                        idempotent=True,
                    )
                if settlement.get("status") != "claimed":
                    raise SettlementConflict(
                        "zero-refund settlement requires claimed or already "
                        f"applied state: {identifier}"
                    )
                if int(settlement.get("refund_applied_points") or 0) != 0:
                    raise WalletIntegrityError(
                        "zero-refund settlement records a nonzero refund: "
                        f"{identifier}"
                    )
                cursor.execute(
                    _APPLY_SETTLEMENT_REFUND_SQL,
                    (provider_ref, identifier, token),
                )
                applied = _fetchone_dict(cursor)
                if applied is None:
                    raise SettlementConflict(
                        "zero-refund settlement finalization lost its claim: "
                        f"{identifier}"
                    )
                return SettlementRefundResult(
                    account=account,
                    order=None,
                    settlement=_decode_row(applied),
                    idempotent=False,
                )

            if settlement.get("status") == "applied":
                if (
                    int(settlement.get("refund_applied_points") or 0)
                    != refund_target
                    or str(settlement.get("provider_reference") or "")
                    != provider_ref
                ):
                    raise SettlementConflict(
                        f"settlement already finalized differently: {identifier}"
                    )
                cursor.execute(_SELECT_POINT_ORDER_SQL, (refund_order_id,))
                existing_order = _fetchone_dict(cursor)
                if existing_order is None:
                    raise WalletIntegrityError(
                        f"applied settlement has no refund order: {identifier}"
                    )
                existing_order = _decode_row(existing_order)
                if not _same_point_order(
                    existing_order,
                    owner_user_id=owner,
                    order_kind="refund",
                    points=refund_target,
                    source_order_id=debit_order_id,
                    job_id=identifier,
                    request_sha256=request_digest,
                    metadata={
                        "jobId": identifier,
                        "providerReference": provider_ref,
                        "purpose": "product_generation_refund",
                    },
                ):
                    raise WalletIntegrityError(
                        f"applied settlement refund order drifted: {identifier}"
                    )
                return SettlementRefundResult(
                    account=account,
                    order=existing_order,
                    settlement=settlement,
                    idempotent=True,
                )

            if settlement.get("status") != "claimed":
                raise SettlementConflict(
                    "settlement refund requires claimed or already applied state: "
                    f"{identifier}"
                )
            if int(settlement.get("refund_applied_points") or 0) != 0:
                raise WalletIntegrityError(
                    f"claimed settlement already records a partial refund: {identifier}"
                )

            refund_metadata = {
                "jobId": identifier,
                "providerReference": provider_ref,
                "purpose": "product_generation_refund",
            }
            refund_metadata_json = _wallet_metadata_payload(
                refund_metadata,
                "refund_metadata",
            )
            cursor.execute(
                _INSERT_POINT_ORDER_SQL,
                (
                    refund_order_id,
                    owner,
                    "refund",
                    refund_target,
                    debit_order_id,
                    "debit",
                    identifier,
                    request_digest,
                    refund_metadata_json,
                ),
            )
            refund_order = _fetchone_dict(cursor)
            if refund_order is None:
                raise SettlementConflict(
                    f"refund order already exists before settlement finalization: {identifier}"
                )

            cursor.execute(
                _REFUND_POINT_ACCOUNT_SQL,
                (refund_target, refund_target, owner),
            )
            refunded_account = _fetchone_dict(cursor)
            if refunded_account is None:
                raise WalletIntegrityError(
                    f"refund account disappeared while locked: {owner}"
                )
            refunded_account = _decode_row(refunded_account)
            cursor.execute(
                _INSERT_POINT_LEDGER_SQL,
                (
                    owner,
                    refund_order_id,
                    "refund",
                    refund_target,
                    refund_target,
                    int(refunded_account["balance_points"]),
                    request_digest,
                    refund_metadata_json,
                ),
            )
            cursor.execute(
                _APPLY_SETTLEMENT_REFUND_SQL,
                (provider_ref, identifier, token),
            )
            applied = _fetchone_dict(cursor)
            if applied is None:
                raise SettlementConflict(
                    f"settlement refund finalization lost its claim: {identifier}"
                )
            return SettlementRefundResult(
                account=refunded_account,
                order=_decode_row(refund_order),
                settlement=_decode_row(applied),
                idempotent=False,
            )

    def mark_settlement_applied(
        self,
        *,
        job_id: str,
        claim_token: str,
        refund_applied_points: int,
        provider_reference: str,
    ) -> SettlementFinalizationResult:
        identifier = _identifier(job_id, "job_id")
        token = _identifier(claim_token, "claim_token")
        applied_points = _nonnegative_int(
            refund_applied_points, "refund_applied_points"
        )
        provider_ref = _identifier(provider_reference, "provider_reference")
        with self._transaction() as cursor:
            cursor.execute(
                _MARK_SETTLEMENT_APPLIED_SQL,
                (
                    applied_points,
                    provider_ref,
                    identifier,
                    token,
                    applied_points,
                ),
            )
            changed = _fetchone_dict(cursor)
            if changed is not None:
                return SettlementFinalizationResult(
                    settlement=_decode_row(changed),
                    idempotent=False,
                )
            current = self._load_settlement_for_finalization(cursor, identifier)
            return self._idempotent_or_conflicting_settlement(
                current,
                requested_status="applied",
                claim_token=token,
                refund_applied_points=applied_points,
                provider_reference=provider_ref,
                error_message="",
            )

    def mark_settlement_failed(
        self,
        *,
        job_id: str,
        claim_token: str,
        error_message: str,
        refund_applied_points: int = 0,
        provider_reference: str = "",
    ) -> SettlementFinalizationResult:
        return self._mark_settlement_problem(
            job_id=job_id,
            claim_token=claim_token,
            outcome="failed",
            error_message=error_message,
            refund_applied_points=refund_applied_points,
            provider_reference=provider_reference,
        )

    def mark_settlement_manual_review(
        self,
        *,
        job_id: str,
        claim_token: str,
        error_message: str,
        refund_applied_points: int = 0,
        provider_reference: str = "",
    ) -> SettlementFinalizationResult:
        return self._mark_settlement_problem(
            job_id=job_id,
            claim_token=claim_token,
            outcome="manual_review",
            error_message=error_message,
            refund_applied_points=refund_applied_points,
            provider_reference=provider_reference,
        )

    def _mark_settlement_problem(
        self,
        *,
        job_id: str,
        claim_token: str,
        outcome: str,
        error_message: str,
        refund_applied_points: int,
        provider_reference: str,
    ) -> SettlementFinalizationResult:
        identifier = _identifier(job_id, "job_id")
        token = _identifier(claim_token, "claim_token")
        applied_points = _nonnegative_int(
            refund_applied_points, "refund_applied_points"
        )
        provider_ref = _optional_identifier(
            provider_reference, "provider_reference"
        )
        error = _identifier(error_message, "error_message")
        with self._transaction() as cursor:
            cursor.execute(
                _MARK_SETTLEMENT_PROBLEM_SQL,
                (
                    outcome,
                    applied_points,
                    provider_ref,
                    error,
                    applied_points,
                    identifier,
                    token,
                    applied_points,
                ),
            )
            changed = _fetchone_dict(cursor)
            if changed is not None:
                return SettlementFinalizationResult(
                    settlement=_decode_row(changed),
                    idempotent=False,
                )
            current = self._load_settlement_for_finalization(cursor, identifier)
            return self._idempotent_or_conflicting_settlement(
                current,
                requested_status=outcome,
                claim_token=token,
                refund_applied_points=applied_points,
                provider_reference=provider_ref,
                error_message=error,
            )

    @staticmethod
    def _ensure_account_locked(
        cursor: CursorLike,
        *,
        owner: str,
        metadata_json: str,
    ) -> dict[str, Any]:
        cursor.execute(
            _INSERT_POINT_ACCOUNT_SQL,
            (owner, metadata_json),
        )
        _fetchone_dict(cursor)
        cursor.execute(
            _SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL,
            (owner,),
        )
        account = _fetchone_dict(cursor)
        if account is None:
            raise WalletIntegrityError(
                f"point account could not be locked after insert: {owner}"
            )
        return _decode_row(account)

    @staticmethod
    def _validate_job_replay(
        cursor: CursorLike,
        job: Mapping[str, Any],
        request_sha256: str,
    ) -> None:
        existing_digest = str(job.get("request_sha256") or "")
        if not hmac.compare_digest(existing_digest, request_sha256):
            raise RequestDigestConflict(str(job.get("id") or ""))

        job_id = _identifier(job.get("id"), "job_id")
        owner = _identifier(job.get("owner_user_id"), "owner_user_id")
        cursor.execute(
            _SELECT_ATOMIC_JOB_INTEGRITY_SQL,
            (job_id, owner),
        )
        integrity = _fetchone_dict(cursor)
        if integrity is None:
            raise WalletIntegrityError(
                f"atomic job integrity record is missing: {job_id}"
            )
        expected_debit_order = str(job.get("debit_order_id") or "")
        expected_debit_points = int(job.get("debit_points") or 0)
        checks = (
            str(integrity.get("wallet_debit_order_id") or "")
            == expected_debit_order,
            str(integrity.get("wallet_owner_user_id") or "") == owner,
            integrity.get("wallet_order_kind") == "debit",
            int(integrity.get("wallet_order_points") or 0)
            == expected_debit_points,
            str(integrity.get("wallet_request_sha256") or "")
            == request_sha256,
            str(integrity.get("wallet_job_id") or "") == job_id,
            str(integrity.get("settlement_job_id") or "") == job_id,
            bool(integrity.get("outbox_id")),
            integrity.get("outbox_event_type")
            == "product_generation.requested",
        )
        if not all(checks):
            raise WalletIntegrityError(
                f"atomic job wallet/settlement/outbox integrity failed: {job_id}"
            )

    @staticmethod
    def _revision_replay_result(
        cursor: CursorLike,
        *,
        existing: dict[str, Any],
        free: _PreparedRevisionCandidate,
        paid: _PreparedRevisionCandidate,
        revision_job_type: str,
        parent_generation_job_id: str,
    ) -> RevisionJobCreateResult:
        existing_digest = str(existing.get("request_sha256") or "")
        if hmac.compare_digest(existing_digest, free.digest):
            selected = free
        elif hmac.compare_digest(existing_digest, paid.digest):
            selected = paid
        else:
            raise RequestDigestConflict(str(existing.get("id") or ""))

        _validate_persisted_revision_job(
            existing,
            selected_kind=selected.kind,
            revision_job_type=revision_job_type,
            parent_generation_job_id=parent_generation_job_id,
        )
        job_id = _identifier(existing.get("id"), "job_id")
        owner = _identifier(existing.get("owner_user_id"), "owner_user_id")
        cursor.execute(
            _SELECT_ATOMIC_JOB_INTEGRITY_SQL,
            (job_id, owner),
        )
        integrity = _fetchone_dict(cursor)
        if integrity is None:
            raise WalletIntegrityError(
                f"atomic revision integrity record is missing: {job_id}"
            )
        _validate_revision_job_integrity(
            integrity,
            existing,
            selected_kind=selected.kind,
        )
        payload = existing.get("request_payload")
        if not isinstance(payload, Mapping):
            raise WalletIntegrityError(
                f"revision request payload is invalid: {job_id}"
            )
        return RevisionJobCreateResult(
            job=existing,
            created=False,
            selected_candidate=selected.kind,
            request_payload=dict(payload),
        )

    @staticmethod
    def _load_settlement_for_finalization(
        cursor: CursorLike,
        job_id: str,
    ) -> dict[str, Any]:
        cursor.execute(_SELECT_SETTLEMENT_SQL, (job_id,))
        current = _fetchone_dict(cursor)
        if current is None:
            raise JobNotFound(job_id)
        return _decode_row(current)

    @staticmethod
    def _idempotent_or_conflicting_settlement(
        current: dict[str, Any],
        *,
        requested_status: str,
        claim_token: str,
        refund_applied_points: int,
        provider_reference: str,
        error_message: str,
    ) -> SettlementFinalizationResult:
        current_token = str(current.get("claim_token") or "")
        if not hmac.compare_digest(current_token, claim_token):
            raise SettlementConflict(
                f"settlement claim token mismatch for {current.get('job_id')}"
            )
        if _same_settlement_finalization(
            current,
            status=requested_status,
            refund_applied_points=refund_applied_points,
            provider_reference=provider_reference,
            error_message=error_message,
        ):
            return SettlementFinalizationResult(
                settlement=current,
                idempotent=True,
            )
        raise SettlementConflict(
            "settlement was already finalized or requested with different values: "
            f"{current.get('job_id')}"
        )

    @contextmanager
    def _transaction(self) -> Iterator[CursorLike]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        except Exception:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def _prepare_revision_candidate(
    candidate: RevisionJobCandidate,
    *,
    kind: str,
    owner_user_id: str,
    parent_generation_job_id: str,
    revision_job_type: str,
    idempotency_key: str,
) -> _PreparedRevisionCandidate:
    if not isinstance(candidate, RevisionJobCandidate):
        raise InvalidProductJobInput(
            f"{kind}_candidate must be a RevisionJobCandidate"
        )
    if kind not in {"free", "paid"}:
        raise InvalidProductJobInput("revision candidate kind is invalid")

    digest = _sha256(candidate.request_sha256, f"{kind}_candidate.request_sha256")
    request_json = _json_payload(
        candidate.request_payload,
        f"{kind}_candidate.request_payload",
    )
    request_payload = json.loads(request_json)
    job_id = _identifier(candidate.job_id, f"{kind}_candidate.job_id")
    outbox_id = _identifier(candidate.outbox_id, f"{kind}_candidate.outbox_id")
    debit_order_id = _identifier(
        candidate.debit_order_id,
        f"{kind}_candidate.debit_order_id",
    )
    refund_order_id = _identifier(
        candidate.refund_order_id,
        f"{kind}_candidate.refund_order_id",
    )
    debit_points = _nonnegative_int(
        candidate.debit_points,
        f"{kind}_candidate.debit_points",
    )
    requested_count = _positive_int(
        candidate.requested_count,
        f"{kind}_candidate.requested_count",
    )
    menu_upload_id = _identifier(
        candidate.menu_upload_id,
        f"{kind}_candidate.menu_upload_id",
    )
    menu_object_ref = _identifier(
        candidate.menu_object_ref,
        f"{kind}_candidate.menu_object_ref",
    )
    menu_object_sha256 = _sha256(
        candidate.menu_object_sha256,
        f"{kind}_candidate.menu_object_sha256",
    )
    selected_background_ref = _identifier(
        candidate.selected_background_ref,
        f"{kind}_candidate.selected_background_ref",
    )
    selected_background_sha256 = _sha256(
        candidate.selected_background_sha256,
        f"{kind}_candidate.selected_background_sha256",
    )

    billing = request_payload.get("billing")
    idempotency = request_payload.get("idempotency")
    background = request_payload.get("selectedBackground")
    if not isinstance(billing, Mapping):
        raise InvalidProductJobInput(
            f"{kind}_candidate.request_payload.billing must be a mapping"
        )
    if not isinstance(idempotency, Mapping):
        raise InvalidProductJobInput(
            f"{kind}_candidate.request_payload.idempotency must be a mapping"
        )
    if not isinstance(background, Mapping):
        raise InvalidProductJobInput(
            f"{kind}_candidate.request_payload.selectedBackground must be a mapping"
        )

    expected_free = kind == "free"
    total_points = _nonnegative_int(
        billing.get("totalPoints"),
        f"{kind}_candidate.request_payload.billing.totalPoints",
    )
    checks = (
        request_payload.get("jobType") == revision_job_type,
        request_payload.get("parentGenerationJobId")
        == parent_generation_job_id,
        request_payload.get("userId") == owner_user_id,
        request_payload.get("mode") == "rework",
        request_payload.get("jobId") == job_id,
        idempotency.get("key") == idempotency_key,
        idempotency.get("requestSha256") == digest,
        billing.get("freeReworkQuotaVerified") is expected_free,
        total_points == debit_points,
        billing.get("debitOrderId") == debit_order_id,
        billing.get("refundOrderId") == refund_order_id,
        background.get("objectKey") == selected_background_ref,
        background.get("sha256") == selected_background_sha256,
    )
    if not all(checks):
        raise InvalidProductJobInput(
            f"{kind}_candidate does not match its immutable revision request"
        )
    if expected_free and debit_points != 0:
        raise InvalidProductJobInput("free_candidate must debit zero points")
    if not expected_free and debit_points <= 0:
        raise InvalidProductJobInput("paid_candidate must debit positive points")

    account_metadata_json = _wallet_metadata_payload(
        candidate.account_metadata or {},
        f"{kind}_candidate.account_metadata",
    )
    debit_metadata_json = _wallet_metadata_payload(
        {
            "jobId": job_id,
            "purpose": "product_generation",
            "context": dict(candidate.debit_metadata or {}),
        },
        f"{kind}_candidate.debit_metadata",
    )
    event_json = _json_payload(
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "ownerUserId": owner_user_id,
            "requestSha256": digest,
        },
        f"{kind}_candidate.outbox_payload",
    )
    return _PreparedRevisionCandidate(
        kind=kind,
        digest=digest,
        request_payload=request_payload,
        request_json=request_json,
        menu_upload_id=menu_upload_id,
        menu_object_ref=menu_object_ref,
        menu_object_sha256=menu_object_sha256,
        selected_background_ref=selected_background_ref,
        selected_background_sha256=selected_background_sha256,
        requested_count=requested_count,
        debit_order_id=debit_order_id,
        debit_points=debit_points,
        refund_order_id=refund_order_id,
        job_id=job_id,
        outbox_id=outbox_id,
        account_metadata_json=account_metadata_json,
        debit_metadata_json=debit_metadata_json,
        event_json=event_json,
    )


def _validate_revision_candidate_pair(
    free: _PreparedRevisionCandidate,
    paid: _PreparedRevisionCandidate,
) -> None:
    if hmac.compare_digest(free.digest, paid.digest):
        raise InvalidProductJobInput(
            "free and paid candidates must have different request digests"
        )
    shared_fields = (
        "job_id",
        "outbox_id",
        "menu_upload_id",
        "menu_object_ref",
        "menu_object_sha256",
        "selected_background_ref",
        "selected_background_sha256",
        "requested_count",
        "debit_order_id",
        "refund_order_id",
    )
    if any(getattr(free, field) != getattr(paid, field) for field in shared_fields):
        raise InvalidProductJobInput(
            "free and paid candidates must describe the same revision identity"
        )


def _validate_parent_generation_job(parent: Mapping[str, Any]) -> None:
    payload = parent.get("request_payload")
    if (
        parent.get("status") != "succeeded"
        or not isinstance(payload, Mapping)
        or payload.get("jobType") != PRODUCT_BATCH_REQUEST_JOB_TYPE
    ):
        raise JobStateConflict(
            "parent generation job must be a succeeded product_batch"
        )


def _validate_revision_candidate_parent(
    candidate: _PreparedRevisionCandidate,
    parent: Mapping[str, Any],
) -> None:
    expected = (
        (candidate.menu_upload_id, str(parent.get("menu_upload_id") or "")),
        (candidate.menu_object_ref, str(parent.get("menu_object_ref") or "")),
        (
            candidate.menu_object_sha256,
            str(parent.get("menu_object_sha256") or ""),
        ),
        (
            candidate.selected_background_ref,
            str(parent.get("selected_background_ref") or ""),
        ),
        (
            candidate.selected_background_sha256,
            str(parent.get("selected_background_sha256") or ""),
        ),
    )
    if any(left != right for left, right in expected):
        raise InvalidProductJobInput(
            "revision candidate does not match the locked parent snapshots"
        )


def _validate_persisted_revision_job(
    job: Mapping[str, Any],
    *,
    selected_kind: str,
    revision_job_type: str,
    parent_generation_job_id: str,
) -> None:
    job_id = _identifier(job.get("id"), "job_id")
    owner = _identifier(job.get("owner_user_id"), "owner_user_id")
    digest = _sha256(job.get("request_sha256"), "request_sha256")
    debit_points = _nonnegative_int(job.get("debit_points"), "debit_points")
    payload = job.get("request_payload")
    if not isinstance(payload, Mapping):
        raise WalletIntegrityError(
            f"revision request payload is invalid: {job_id}"
        )
    billing = payload.get("billing")
    idempotency = payload.get("idempotency")
    if not isinstance(billing, Mapping) or not isinstance(idempotency, Mapping):
        raise WalletIntegrityError(
            f"revision request billing/idempotency is invalid: {job_id}"
        )
    expected_free = selected_kind == "free"
    try:
        total_points = _nonnegative_int(
            billing.get("totalPoints"),
            "billing.totalPoints",
        )
    except InvalidProductJobInput as exc:
        raise WalletIntegrityError(
            f"revision billing points are invalid: {job_id}"
        ) from exc
    checks = (
        payload.get("jobType") == revision_job_type,
        payload.get("parentGenerationJobId") == parent_generation_job_id,
        payload.get("userId") == owner,
        payload.get("mode") == "rework",
        payload.get("jobId") == job_id,
        idempotency.get("requestSha256") == digest,
        billing.get("freeReworkQuotaVerified") is expected_free,
        total_points == debit_points,
        billing.get("debitOrderId") == job.get("debit_order_id"),
        billing.get("refundOrderId") == job.get("refund_order_id"),
        (debit_points == 0 if expected_free else debit_points > 0),
    )
    if not all(checks):
        raise WalletIntegrityError(
            f"persisted revision request integrity failed: {job_id}"
        )


def _validate_revision_job_integrity(
    integrity: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    selected_kind: str,
) -> None:
    job_id = str(job.get("id") or "")
    owner = str(job.get("owner_user_id") or "")
    digest = str(job.get("request_sha256") or "")
    debit_order_id = str(job.get("debit_order_id") or "")
    debit_points = int(job.get("debit_points") or 0)
    common_checks = (
        str(integrity.get("job_id") or "") == job_id,
        str(integrity.get("owner_user_id") or "") == owner,
        str(integrity.get("request_sha256") or "") == digest,
        str(integrity.get("debit_order_id") or "") == debit_order_id,
        int(integrity.get("debit_points") or 0) == debit_points,
        str(integrity.get("settlement_job_id") or "") == job_id,
        bool(integrity.get("outbox_id")),
        integrity.get("outbox_event_type")
        == "product_generation.requested",
    )
    if selected_kind == "paid":
        wallet_checks = (
            str(integrity.get("wallet_debit_order_id") or "")
            == debit_order_id,
            str(integrity.get("wallet_owner_user_id") or "") == owner,
            integrity.get("wallet_order_kind") == "debit",
            int(integrity.get("wallet_order_points") or 0) == debit_points,
            str(integrity.get("wallet_request_sha256") or "") == digest,
            str(integrity.get("wallet_job_id") or "") == job_id,
        )
    else:
        wallet_checks = (
            not integrity.get("wallet_debit_order_id"),
            not integrity.get("wallet_owner_user_id"),
            not integrity.get("wallet_order_kind"),
            integrity.get("wallet_order_points") in (None, 0),
            not integrity.get("wallet_request_sha256"),
            not integrity.get("wallet_job_id"),
        )
    if not all((*common_checks, *wallet_checks)):
        raise WalletIntegrityError(
            f"atomic revision wallet/settlement/outbox integrity failed: {job_id}"
        )


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(cursor, row)


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def _row_to_dict(cursor: CursorLike, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    names = [
        str(column[0] if isinstance(column, Sequence) else column.name)
        for column in description
    ]
    if not names or len(names) != len(row):
        raise ProductJobStoreError("DB-API cursor did not expose usable row metadata")
    return dict(zip(names, row))


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in ("payload", "request_payload", "metadata"):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                decoded[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidProductJobInput(f"{field} is required")
    if len(text) > 512:
        raise InvalidProductJobInput(f"{field} is too long")
    return text


def _optional_identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) > 512:
        raise InvalidProductJobInput(f"{field} is too long")
    return text


def _slot_part(value: Any, field: str) -> str:
    return _identifier(value, field).lower()


def _sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(digest):
        raise InvalidProductJobInput(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _json_payload(value: Mapping[str, Any], field: str) -> str:
    if not isinstance(value, Mapping):
        raise InvalidProductJobInput(f"{field} must be a mapping")
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidProductJobInput(f"{field} must be JSON serializable") from exc


def _wallet_metadata_payload(value: Mapping[str, Any], field: str) -> str:
    _reject_sensitive_wallet_metadata(value, field)
    return _json_payload(value, field)


def _reject_sensitive_wallet_metadata(value: Any, field: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if (
                normalized in SENSITIVE_WALLET_METADATA_KEYS
                or normalized.endswith("apikey")
                or normalized.endswith("authtoken")
                or normalized.endswith("accesstoken")
                or normalized.endswith("refreshtoken")
                or normalized.endswith("password")
                or normalized.endswith("privatekey")
                or normalized.endswith("secret")
            ):
                raise InvalidProductJobInput(
                    f"{field} must not contain credentials or tokens"
                )
            _reject_sensitive_wallet_metadata(nested, field)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive_wallet_metadata(nested, field)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidProductJobInput(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductJobInput(f"{field} must be an integer") from exc
    if number < 0:
        raise InvalidProductJobInput(f"{field} must be nonnegative")
    return number


def _positive_int(value: Any, field: str) -> int:
    number = _nonnegative_int(value, field)
    if number <= 0:
        raise InvalidProductJobInput(f"{field} must be positive")
    return number


def _manifest_pair(
    manifest_ref: str | None,
    manifest_sha256: str | None,
    terminal_status: str,
) -> tuple[str | None, str | None]:
    if manifest_ref is None and manifest_sha256 is None:
        if terminal_status == "succeeded":
            raise InvalidProductJobInput(
                "successful completion requires a manifest reference and digest"
            )
        return None, None
    if manifest_ref is None or manifest_sha256 is None:
        raise InvalidProductJobInput(
            "manifest_ref and manifest_sha256 must be supplied together"
        )
    return (
        _identifier(manifest_ref, "manifest_ref"),
        _sha256(manifest_sha256, "manifest_sha256"),
    )


def _result_object_pair(
    object_ref: str | None,
    object_sha256: str | None,
    result_status: str,
) -> tuple[str | None, str | None]:
    if object_ref is None and object_sha256 is None:
        if result_status == "generated":
            raise InvalidProductJobInput(
                "generated result requires an object reference and digest"
            )
        return None, None
    if object_ref is None or object_sha256 is None:
        raise InvalidProductJobInput(
            "object_ref and object_sha256 must be supplied together"
        )
    return (
        _identifier(object_ref, "object_ref"),
        _sha256(object_sha256, "object_sha256"),
    )


def _result_from_context(context: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "job_id",
        "job_fence",
        "menu_row",
        "platform",
        "variant",
        "status",
        "object_ref",
        "object_sha256",
        "prompt_sha256",
        "provider_request_id",
        "metadata",
        "error_message",
        "created_at",
        "updated_at",
    )
    return _decode_row({field: context.get(field) for field in fields})


def _same_point_order(
    order: Mapping[str, Any],
    *,
    owner_user_id: str,
    order_kind: str,
    points: int,
    source_order_id: str | None,
    job_id: str | None,
    request_sha256: str,
    metadata: Mapping[str, Any],
) -> bool:
    expected_source_kind = "debit" if source_order_id is not None else None
    return (
        str(order.get("owner_user_id") or "") == owner_user_id
        and order.get("order_kind") == order_kind
        and int(order.get("points") or 0) == points
        and order.get("source_order_id") == source_order_id
        and order.get("source_order_kind") == expected_source_kind
        and order.get("job_id") == job_id
        and str(order.get("request_sha256") or "") == request_sha256
        and (order.get("metadata") or {}) == dict(metadata)
    )


def _same_result(
    result: Mapping[str, Any],
    *,
    status: str,
    object_ref: str | None,
    object_sha256: str | None,
    prompt_sha256: str | None,
    provider_request_id: str,
    metadata: Mapping[str, Any],
    error_message: str,
) -> bool:
    return (
        result.get("status") == status
        and result.get("object_ref") == object_ref
        and result.get("object_sha256") == object_sha256
        and result.get("prompt_sha256") == prompt_sha256
        and str(result.get("provider_request_id") or "") == provider_request_id
        and (result.get("metadata") or {}) == dict(metadata)
        and str(result.get("error_message") or "") == error_message
    )


def _same_settlement_finalization(
    settlement: Mapping[str, Any],
    *,
    status: str,
    refund_applied_points: int,
    provider_reference: str,
    error_message: str,
) -> bool:
    return (
        settlement.get("status") == status
        and int(settlement.get("refund_applied_points") or 0)
        == refund_applied_points
        and str(settlement.get("provider_reference") or "")
        == provider_reference
        and str(settlement.get("error_message") or "") == error_message
    )


def _same_completion(
    job: Mapping[str, Any],
    *,
    fence: int,
    status: str,
    completed_count: int,
    failed_count: int,
    manifest_ref: str | None,
    manifest_sha256: str | None,
    refund_target_points: int,
) -> bool:
    expected_refund = (
        int(job.get("debit_points") or 0)
        if status == "canceled"
        else refund_target_points
    )
    return (
        job.get("status") == status
        and int(job.get("fence") or 0) == fence
        and int(job.get("completed_count") or 0) == completed_count
        and int(job.get("failed_count") or 0) == failed_count
        and job.get("manifest_ref") == manifest_ref
        and job.get("manifest_sha256") == manifest_sha256
        and int(job.get("refund_target_points") or 0) == expected_refund
    )


__all__ = [
    "AccountCreateResult",
    "CancelRequestResult",
    "CancellationRequested",
    "CompletionResult",
    "CreateOrGetResult",
    "FenceMismatch",
    "InsufficientPointBalance",
    "InvalidProductJobInput",
    "JobNotFound",
    "JobStateConflict",
    "OwnedJobDetail",
    "OutboxClaimLost",
    "PointMutationResult",
    "PointOrderConflict",
    "ProductJobStore",
    "ProductJobStoreError",
    "RequestDigestConflict",
    "RevisionJobCandidate",
    "RevisionJobCreateResult",
    "ResultWriteConflict",
    "ResultWriteResult",
    "SettlementClaimResult",
    "SettlementConflict",
    "SettlementFinalizationResult",
    "SettlementRefundResult",
    "WalletIntegrityError",
]
