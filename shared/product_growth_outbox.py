from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Protocol, Sequence
from uuid import uuid4


EVENT_FIRST_PAYMENT_REWARD = "growth.first_payment_reward.requested"
EVENT_PAYMENT_REFUND = "growth.payment_refund.requested"
EVENT_INVITE_REWARD = "growth.invite_reward.requested"
EVENT_AGENT_COMMISSION = "growth.agent_commission.requested"
SUPPORTED_EVENT_TYPES = frozenset(
    {
        EVENT_FIRST_PAYMENT_REWARD,
        EVENT_PAYMENT_REFUND,
        EVENT_INVITE_REWARD,
        EVENT_AGENT_COMMISSION,
    }
)

_EVENT_PREFIXES = {
    EVENT_FIRST_PAYMENT_REWARD: "first-payment",
    EVENT_PAYMENT_REFUND: "payment-refund",
    EVENT_INVITE_REWARD: "invite",
    EVENT_AGENT_COMMISSION: "agent-commission",
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_DEDUPE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_MAX_BATCH_SIZE = 1000
_MAX_LEASE_SECONDS = 86400
_MAX_RETRY_DELAY_SECONDS = 604800
_MAX_ERROR_LENGTH = 4000


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


class ProductGrowthOutboxError(RuntimeError):
    pass


class InvalidGrowthOutboxInput(ProductGrowthOutboxError, ValueError):
    pass


class GrowthEventConflict(ProductGrowthOutboxError):
    def __init__(self, dedupe_key: str, existing_event_id: str) -> None:
        super().__init__(
            "growth event dedupe key already belongs to different content: "
            f"{dedupe_key}"
        )
        self.dedupe_key = dedupe_key
        self.existing_event_id = existing_event_id


class GrowthOutboxClaimLost(ProductGrowthOutboxError):
    def __init__(
        self,
        *,
        event_id: str,
        expected_fence: int,
        current_status: str,
        current_fence: int,
    ) -> None:
        super().__init__(
            "growth outbox claim is no longer current: "
            f"event={event_id}, expected_fence={expected_fence}, "
            f"current_status={current_status or 'missing'}, "
            f"current_fence={current_fence}"
        )
        self.event_id = event_id
        self.expected_fence = expected_fence
        self.current_status = current_status
        self.current_fence = current_fence


@dataclass(frozen=True)
class GrowthEventEnqueueResult:
    event: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class GrowthOutboxMutationResult:
    event: dict[str, Any]
    idempotent: bool


_OUTBOX_COLUMNS = """
    id, event_type, dedupe_key, payload, payload_sha256,
    status, max_attempts, attempt_count, available_at,
    claimed_by, claim_token, claimed_until, fence,
    result_payload, last_error, succeeded_at, dead_lettered_at,
    created_at, updated_at
""".strip()

_QUALIFIED_OUTBOX_COLUMNS = """
    o.id, o.event_type, o.dedupe_key, o.payload, o.payload_sha256,
    o.status, o.max_attempts, o.attempt_count, o.available_at,
    o.claimed_by, o.claim_token, o.claimed_until, o.fence,
    o.result_payload, o.last_error, o.succeeded_at, o.dead_lettered_at,
    o.created_at, o.updated_at
""".strip()

_INSERT_EVENT_SQL = f"""
/* product_growth_outbox:insert_event */
INSERT INTO product_growth_outbox (
    id, event_type, dedupe_key, payload, payload_sha256,
    max_attempts, available_at
) VALUES (
    %s, %s, %s, %s::jsonb, %s,
    %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
)
ON CONFLICT (dedupe_key) DO NOTHING
RETURNING {_OUTBOX_COLUMNS}
"""

_SELECT_BY_DEDUPE_FOR_UPDATE_SQL = f"""
/* product_growth_outbox:select_by_dedupe_for_update */
SELECT {_OUTBOX_COLUMNS}
FROM product_growth_outbox
WHERE dedupe_key = %s
FOR UPDATE
"""

_SELECT_BY_DEDUPE_SQL = f"""
/* product_growth_outbox:select_by_dedupe */
SELECT {_OUTBOX_COLUMNS}
FROM product_growth_outbox
WHERE dedupe_key = %s
"""

_SELECT_BY_ID_SQL = f"""
/* product_growth_outbox:select_by_id */
SELECT {_OUTBOX_COLUMNS}
FROM product_growth_outbox
WHERE id = %s
"""

_EXPIRE_EXHAUSTED_CLAIMS_SQL = """
/* product_growth_outbox:expire_exhausted_claims */
WITH exhausted AS (
    SELECT id
    FROM product_growth_outbox
    WHERE status = 'claimed'
      AND claimed_until < CURRENT_TIMESTAMP
      AND attempt_count >= max_attempts
    ORDER BY claimed_until, id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE product_growth_outbox AS o
SET status = 'dead_letter',
    last_error = CASE
        WHEN o.last_error = '' THEN 'claim lease expired after maximum attempts'
        ELSE o.last_error
    END,
    dead_lettered_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
FROM exhausted AS e
WHERE o.id = e.id
"""

_CLAIM_SQL = f"""
/* product_growth_outbox:claim */
WITH candidates AS (
    SELECT id
    FROM product_growth_outbox
    WHERE (
        (status = 'pending' AND available_at <= CURRENT_TIMESTAMP)
        OR (
            status = 'claimed'
            AND claimed_until < CURRENT_TIMESTAMP
        )
    )
      AND attempt_count < max_attempts
    ORDER BY available_at, created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE product_growth_outbox AS o
SET status = 'claimed',
    claimed_by = %s,
    claim_token = %s,
    claimed_until = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
    fence = o.fence + 1,
    attempt_count = o.attempt_count + 1,
    updated_at = CURRENT_TIMESTAMP
FROM candidates AS c
WHERE o.id = c.id
RETURNING {_QUALIFIED_OUTBOX_COLUMNS}
"""

_RENEW_CLAIM_SQL = f"""
/* product_growth_outbox:renew_claim */
UPDATE product_growth_outbox
SET claimed_until = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND fence = %s
  AND claimed_until >= CURRENT_TIMESTAMP
RETURNING {_OUTBOX_COLUMNS}
"""

_MARK_SUCCEEDED_SQL = f"""
/* product_growth_outbox:mark_succeeded */
UPDATE product_growth_outbox
SET status = 'succeeded',
    result_payload = %s::jsonb,
    last_error = '',
    succeeded_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND fence = %s
  AND claimed_until >= CURRENT_TIMESTAMP
RETURNING {_OUTBOX_COLUMNS}
"""

_MARK_RETRY_SQL = f"""
/* product_growth_outbox:mark_retry */
UPDATE product_growth_outbox
SET status = CASE
        WHEN attempt_count >= max_attempts THEN 'dead_letter'
        ELSE 'pending'
    END,
    available_at = CASE
        WHEN attempt_count >= max_attempts THEN available_at
        ELSE CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
    END,
    claimed_by = CASE
        WHEN attempt_count >= max_attempts THEN claimed_by
        ELSE NULL
    END,
    claim_token = CASE
        WHEN attempt_count >= max_attempts THEN claim_token
        ELSE NULL
    END,
    claimed_until = CASE
        WHEN attempt_count >= max_attempts THEN claimed_until
        ELSE NULL
    END,
    last_error = %s,
    dead_lettered_at = CASE
        WHEN attempt_count >= max_attempts THEN CURRENT_TIMESTAMP
        ELSE NULL
    END,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND fence = %s
  AND claimed_until >= CURRENT_TIMESTAMP
RETURNING {_OUTBOX_COLUMNS}
"""

_MARK_DEAD_LETTER_SQL = f"""
/* product_growth_outbox:mark_dead_letter */
UPDATE product_growth_outbox
SET status = 'dead_letter',
    last_error = %s,
    dead_lettered_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND status = 'claimed'
  AND claim_token = %s
  AND fence = %s
  AND claimed_until >= CURRENT_TIMESTAMP
RETURNING {_OUTBOX_COLUMNS}
"""


def stable_growth_dedupe_key(event_type: str, *identity_parts: Any) -> str:
    """Build a stable, opaque key from immutable business identifiers."""

    normalized_type = _event_type(event_type)
    if not identity_parts:
        raise InvalidGrowthOutboxInput(
            "at least one identity part is required for a growth dedupe key"
        )
    normalized_parts = [
        _identity_part(part, f"identity_parts[{index}]")
        for index, part in enumerate(identity_parts)
    ]
    canonical = json.dumps(
        [normalized_type, *normalized_parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"growth:{_EVENT_PREFIXES[normalized_type]}:{digest[:48]}"


def enqueue_growth_event(
    cursor: CursorLike,
    *,
    event_type: str,
    dedupe_key: str,
    payload: Mapping[str, Any],
    max_attempts: int = 8,
    available_in_seconds: int = 0,
) -> GrowthEventEnqueueResult:
    """Insert an event through a caller-owned cursor without committing.

    This is the integration entry point for payment transactions. The caller
    controls commit or rollback, so the payment event, wallet mutation, and
    growth outbox insert can share one PostgreSQL transaction.
    """

    normalized_type = _event_type(event_type)
    normalized_key = _dedupe_key(dedupe_key)
    payload_json = _canonical_json_object(payload, "payload")
    payload_object = json.loads(payload_json)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    attempts = _bounded_positive_int(
        max_attempts,
        "max_attempts",
        maximum=100,
    )
    delay = _bounded_nonnegative_int(
        available_in_seconds,
        "available_in_seconds",
        maximum=_MAX_RETRY_DELAY_SECONDS,
    )
    event_id = _event_id(normalized_key)

    cursor.execute(
        _INSERT_EVENT_SQL,
        (
            event_id,
            normalized_type,
            normalized_key,
            payload_json,
            payload_sha256,
            attempts,
            delay,
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        event = _decode_row(inserted)
        _validate_replay(
            event,
            event_id=event_id,
            event_type=normalized_type,
            dedupe_key=normalized_key,
            payload=payload_object,
            payload_sha256=payload_sha256,
            max_attempts=attempts,
        )
        return GrowthEventEnqueueResult(event=event, created=True)

    cursor.execute(_SELECT_BY_DEDUPE_FOR_UPDATE_SQL, (normalized_key,))
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductGrowthOutboxError(
            "growth outbox conflict did not expose the existing event: "
            f"{normalized_key}"
        )
    event = _decode_row(existing)
    _validate_replay(
        event,
        event_id=event_id,
        event_type=normalized_type,
        dedupe_key=normalized_key,
        payload=payload_object,
        payload_sha256=payload_sha256,
        max_attempts=attempts,
    )
    return GrowthEventEnqueueResult(event=event, created=False)


def mark_growth_event_succeeded(
    cursor: CursorLike,
    *,
    event_id: str,
    claim_token: str,
    fence: int,
    result_payload: Mapping[str, Any] | None = None,
) -> GrowthOutboxMutationResult:
    """Mark a claimed event succeeded through a caller-owned transaction."""

    identifier = _identifier(event_id, "event_id")
    token = _identifier(claim_token, "claim_token")
    expected_fence = _bounded_positive_int(
        fence,
        "fence",
        maximum=None,
    )
    result_json = _canonical_json_object(
        result_payload or {},
        "result_payload",
    )
    expected_result = json.loads(result_json)

    cursor.execute(
        _MARK_SUCCEEDED_SQL,
        (result_json, identifier, token, expected_fence),
    )
    succeeded = _fetchone_dict(cursor)
    if succeeded is not None:
        return GrowthOutboxMutationResult(
            event=_decode_row(succeeded),
            idempotent=False,
        )

    cursor.execute(_SELECT_BY_ID_SQL, (identifier,))
    current_row = _fetchone_dict(cursor)
    current = _decode_row(current_row) if current_row is not None else None
    if (
        current is not None
        and current.get("status") == "succeeded"
        and int(current.get("fence") or 0) == expected_fence
        and hmac.compare_digest(
            str(current.get("claim_token") or ""),
            token,
        )
        and (current.get("result_payload") or {}) == expected_result
    ):
        return GrowthOutboxMutationResult(
            event=current,
            idempotent=True,
        )
    raise GrowthOutboxClaimLost(
        event_id=identifier,
        expected_fence=expected_fence,
        current_status=str((current or {}).get("status") or ""),
        current_fence=int((current or {}).get("fence") or 0),
    )


class ProductGrowthOutbox:
    """Durable PostgreSQL queue state over an injected non-autocommit connection.

    This layer never evaluates growth rules and never writes a wallet. A worker
    must use the claimed event as an input to a separate, idempotent processor.
    """

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidGrowthOutboxInput(
                "ProductGrowthOutbox requires an autocommit-disabled connection"
            )
        self.connection = connection

    def enqueue(
        self,
        *,
        event_type: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        max_attempts: int = 8,
        available_in_seconds: int = 0,
    ) -> GrowthEventEnqueueResult:
        with self._transaction() as cursor:
            return enqueue_growth_event(
                cursor,
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload=payload,
                max_attempts=max_attempts,
                available_in_seconds=available_in_seconds,
            )

    def get_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        normalized_key = _dedupe_key(dedupe_key)
        with self._transaction() as cursor:
            cursor.execute(_SELECT_BY_DEDUPE_SQL, (normalized_key,))
            row = _fetchone_dict(cursor)
            return _decode_row(row) if row is not None else None

    def claim(
        self,
        *,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 60,
        claim_token: str | None = None,
    ) -> list[dict[str, Any]]:
        worker = _identifier(worker_id, "worker_id")
        batch_limit = _bounded_positive_int(
            limit,
            "limit",
            maximum=_MAX_BATCH_SIZE,
        )
        lease = _bounded_positive_int(
            lease_seconds,
            "lease_seconds",
            maximum=_MAX_LEASE_SECONDS,
        )
        token = _identifier(claim_token or str(uuid4()), "claim_token")

        with self._transaction() as cursor:
            cursor.execute(_EXPIRE_EXHAUSTED_CLAIMS_SQL, (batch_limit,))
            cursor.execute(
                _CLAIM_SQL,
                (batch_limit, worker, token, lease),
            )
            return [_decode_row(row) for row in _fetchall_dicts(cursor)]

    def renew_claim(
        self,
        *,
        event_id: str,
        claim_token: str,
        fence: int,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        identifier = _identifier(event_id, "event_id")
        token = _identifier(claim_token, "claim_token")
        expected_fence = _bounded_positive_int(
            fence,
            "fence",
            maximum=None,
        )
        lease = _bounded_positive_int(
            lease_seconds,
            "lease_seconds",
            maximum=_MAX_LEASE_SECONDS,
        )
        with self._transaction() as cursor:
            cursor.execute(
                _RENEW_CLAIM_SQL,
                (lease, identifier, token, expected_fence),
            )
            renewed = _fetchone_dict(cursor)
            if renewed is not None:
                return _decode_row(renewed)
            self._raise_claim_lost(
                cursor,
                event_id=identifier,
                expected_fence=expected_fence,
            )

    def mark_succeeded(
        self,
        *,
        event_id: str,
        claim_token: str,
        fence: int,
        result_payload: Mapping[str, Any] | None = None,
    ) -> GrowthOutboxMutationResult:
        with self._transaction() as cursor:
            return mark_growth_event_succeeded(
                cursor,
                event_id=event_id,
                claim_token=claim_token,
                fence=fence,
                result_payload=result_payload,
            )

    def mark_retry(
        self,
        *,
        event_id: str,
        claim_token: str,
        fence: int,
        error: str,
        retry_in_seconds: int = 30,
    ) -> GrowthOutboxMutationResult:
        identifier = _identifier(event_id, "event_id")
        token = _identifier(claim_token, "claim_token")
        expected_fence = _bounded_positive_int(
            fence,
            "fence",
            maximum=None,
        )
        message = _error_message(error)
        delay = _bounded_nonnegative_int(
            retry_in_seconds,
            "retry_in_seconds",
            maximum=_MAX_RETRY_DELAY_SECONDS,
        )

        with self._transaction() as cursor:
            cursor.execute(
                _MARK_RETRY_SQL,
                (delay, message, identifier, token, expected_fence),
            )
            changed = _fetchone_dict(cursor)
            if changed is not None:
                return GrowthOutboxMutationResult(
                    event=_decode_row(changed),
                    idempotent=False,
                )
            self._raise_claim_lost(
                cursor,
                event_id=identifier,
                expected_fence=expected_fence,
            )

    def mark_dead_letter(
        self,
        *,
        event_id: str,
        claim_token: str,
        fence: int,
        error: str,
    ) -> GrowthOutboxMutationResult:
        identifier = _identifier(event_id, "event_id")
        token = _identifier(claim_token, "claim_token")
        expected_fence = _bounded_positive_int(
            fence,
            "fence",
            maximum=None,
        )
        message = _error_message(error)

        with self._transaction() as cursor:
            cursor.execute(
                _MARK_DEAD_LETTER_SQL,
                (message, identifier, token, expected_fence),
            )
            dead = _fetchone_dict(cursor)
            if dead is not None:
                return GrowthOutboxMutationResult(
                    event=_decode_row(dead),
                    idempotent=False,
                )

            current = self._select_current(cursor, identifier)
            if (
                current is not None
                and current.get("status") == "dead_letter"
                and int(current.get("fence") or 0) == expected_fence
                and hmac.compare_digest(
                    str(current.get("claim_token") or ""),
                    token,
                )
                and str(current.get("last_error") or "") == message
            ):
                return GrowthOutboxMutationResult(
                    event=current,
                    idempotent=True,
                )
            self._raise_claim_lost_from_current(
                current,
                event_id=identifier,
                expected_fence=expected_fence,
            )

    @staticmethod
    def _select_current(
        cursor: CursorLike,
        event_id: str,
    ) -> dict[str, Any] | None:
        cursor.execute(_SELECT_BY_ID_SQL, (event_id,))
        current = _fetchone_dict(cursor)
        return _decode_row(current) if current is not None else None

    def _raise_claim_lost(
        self,
        cursor: CursorLike,
        *,
        event_id: str,
        expected_fence: int,
    ) -> None:
        self._raise_claim_lost_from_current(
            self._select_current(cursor, event_id),
            event_id=event_id,
            expected_fence=expected_fence,
        )

    @staticmethod
    def _raise_claim_lost_from_current(
        current: Mapping[str, Any] | None,
        *,
        event_id: str,
        expected_fence: int,
    ) -> None:
        raise GrowthOutboxClaimLost(
            event_id=event_id,
            expected_fence=expected_fence,
            current_status=str((current or {}).get("status") or ""),
            current_fence=int((current or {}).get("fence") or 0),
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


def _validate_replay(
    event: Mapping[str, Any],
    *,
    event_id: str,
    event_type: str,
    dedupe_key: str,
    payload: Mapping[str, Any],
    payload_sha256: str,
    max_attempts: int,
) -> None:
    checks = (
        str(event.get("id") or "") == event_id,
        str(event.get("event_type") or "") == event_type,
        str(event.get("dedupe_key") or "") == dedupe_key,
        hmac.compare_digest(
            str(event.get("payload_sha256") or ""),
            payload_sha256,
        ),
        (event.get("payload") or {}) == dict(payload),
        int(event.get("max_attempts") or 0) == max_attempts,
    )
    if not all(checks):
        raise GrowthEventConflict(
            dedupe_key,
            str(event.get("id") or ""),
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
        raise ProductGrowthOutboxError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(names, row))


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in ("payload", "result_payload"):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                decoded[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _event_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_EVENT_TYPES:
        raise InvalidGrowthOutboxInput(
            f"unsupported growth event type: {value!r}"
        )
    return normalized


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(text):
        raise InvalidGrowthOutboxInput(
            f"{field} must be a 1-128 character safe identifier"
        )
    return text


def _dedupe_key(value: Any) -> str:
    text = str(value or "").strip()
    if not _DEDUPE_KEY_RE.fullmatch(text):
        raise InvalidGrowthOutboxInput(
            "dedupe_key must be a 1-256 character safe identifier"
        )
    return text


def _identity_part(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidGrowthOutboxInput(f"{field} is required")
    if len(text) > 512 or any(ord(character) < 32 for character in text):
        raise InvalidGrowthOutboxInput(f"{field} is invalid")
    return text


def _event_id(dedupe_key: str) -> str:
    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
    return f"growth_evt_{digest[:48]}"


def _canonical_json_object(value: Mapping[str, Any], field: str) -> str:
    if not isinstance(value, Mapping):
        raise InvalidGrowthOutboxInput(f"{field} must be a mapping")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidGrowthOutboxInput(
            f"{field} must contain only JSON-compatible values"
        ) from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise InvalidGrowthOutboxInput(f"{field} must encode a JSON object")
    return encoded


def _bounded_positive_int(
    value: Any,
    field: str,
    *,
    maximum: int | None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidGrowthOutboxInput(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise InvalidGrowthOutboxInput(
            f"{field} must not exceed {maximum}"
        )
    return value


def _bounded_nonnegative_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidGrowthOutboxInput(
            f"{field} must be a non-negative integer"
        )
    if value > maximum:
        raise InvalidGrowthOutboxInput(
            f"{field} must not exceed {maximum}"
        )
    return value


def _error_message(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidGrowthOutboxInput("error is required")
    if len(text) > _MAX_ERROR_LENGTH:
        raise InvalidGrowthOutboxInput(
            f"error must not exceed {_MAX_ERROR_LENGTH} characters"
        )
    return text
