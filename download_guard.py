from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

import asset_security


PREVIEW = asset_security.PREVIEW
ORIGINAL = asset_security.ORIGINAL
EXPORT = asset_security.EXPORT
ADMIN_REVIEW = asset_security.ADMIN_REVIEW

REASON_ALLOWED = "allowed"
REASON_MISSING_TOKEN = "missing_token"
REASON_MISSING_SECRET = "missing_secret"
REASON_INVALID_TOKEN = "invalid_token"
REASON_INVALID_SIGNATURE = "invalid_signature"
REASON_TOKEN_EXPIRED = "token_expired"
REASON_ASSET_MISSING = "asset_missing"
REASON_ASSET_MISMATCH = "asset_mismatch"
REASON_USER_MISMATCH = "user_mismatch"
REASON_ORDER_MISMATCH = "order_mismatch"
REASON_JOB_MISMATCH = "job_mismatch"
REASON_PURPOSE_MISMATCH = "purpose_mismatch"
REASON_VARIANT_MISMATCH = "variant_mismatch"
REASON_PURPOSE_VARIANT_MISMATCH = "purpose_variant_mismatch"
REASON_ADMIN_REQUIRED = "admin_required"
REASON_PURPOSE_NOT_ALLOWED = "purpose_not_allowed"
REASON_VARIANT_NOT_AVAILABLE = "variant_not_available"
REASON_TOKEN_REPLAYED = "token_replayed"
REASON_NONCE_CONSUMER_REQUIRED = "nonce_consumer_required"
REASON_NONCE_CONSUMER_ERROR = "nonce_consumer_error"


@dataclass(frozen=True)
class PurposePolicy:
    action: str
    allowed_variants: frozenset[str]
    requires_owner: bool = True
    requires_admin: bool = False


@dataclass(frozen=True)
class NonceConsumptionRequest:
    """Verified one-time token claims bound to one atomic nonce operation."""

    consumption_key: str
    nonce_key: str
    purpose: str
    user_id: str
    asset_id: str
    expires_at: float


class NonceConsumptionStatus(str, Enum):
    CONSUMED = "consumed"
    REPLAYED = "replayed"
    EXPIRED = "expired"
    SCOPE_MISMATCH = "scope_mismatch"


@dataclass(frozen=True)
class NonceConsumptionResult:
    status: NonceConsumptionStatus

    @property
    def consumed(self) -> bool:
        return self.status is NonceConsumptionStatus.CONSUMED


@runtime_checkable
class NonceConsumer(Protocol):
    """Atomic one-time nonce boundary for a shared persistent implementation."""

    def consume_once(
        self,
        request: NonceConsumptionRequest,
        *,
        now: int | float | datetime | None = None,
    ) -> NonceConsumptionResult:
        """Atomically consume the nonce or return a non-success status."""


@dataclass(frozen=True)
class _ConsumedNonce:
    scope_key: str
    expires_at: float


class InMemoryNonceConsumer:
    """Thread-safe process-local implementation intended for tests and local use."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._consumed: dict[str, _ConsumedNonce] = {}

    def consume_once(
        self,
        request: NonceConsumptionRequest,
        *,
        now: int | float | datetime | None = None,
    ) -> NonceConsumptionResult:
        _validate_nonce_consumption_request(request)
        scope_key = _nonce_scope_key(request)
        with self._lock:
            timestamp = _timestamp(now, clock=self._clock)
            self._purge_expired(timestamp)
            if timestamp > request.expires_at:
                return NonceConsumptionResult(NonceConsumptionStatus.EXPIRED)
            existing = self._consumed.get(request.nonce_key)
            if existing is not None:
                status = (
                    NonceConsumptionStatus.REPLAYED
                    if existing.scope_key == scope_key
                    else NonceConsumptionStatus.SCOPE_MISMATCH
                )
                return NonceConsumptionResult(status)
            self._consumed[request.nonce_key] = _ConsumedNonce(
                scope_key=scope_key,
                expires_at=request.expires_at,
            )
            return NonceConsumptionResult(NonceConsumptionStatus.CONSUMED)

    def _purge_expired(self, now: float) -> None:
        expired = [
            nonce_key
            for nonce_key, consumed in self._consumed.items()
            if now > consumed.expires_at
        ]
        for nonce_key in expired:
            self._consumed.pop(nonce_key, None)


class RedisNonceConsumer:
    """Redis-backed atomic consumer for multi-process production runtimes."""

    _CONSUME_SCRIPT = """
-- WAIMAI_ASSET_NONCE_CONSUME
local existing = redis.call('GET', KEYS[1])
if existing then
    if existing == ARGV[1] then
        return 0
    end
    return -1
end
local redis_time = redis.call('TIME')
local now = tonumber(redis_time[1])
local expires_at = tonumber(ARGV[2])
if not expires_at or expires_at <= now then
    return -2
end
local ttl = math.ceil(expires_at - now)
local stored = redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl, 'NX')
if stored then
    return 1
end
existing = redis.call('GET', KEYS[1])
if existing == ARGV[1] then
    return 0
end
return -1
"""

    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str = "waimai:asset-nonce",
    ) -> None:
        prefix = str(key_prefix or "").strip().strip(":")
        if redis_client is None:
            raise ValueError("redis_client is required")
        if not prefix or any(char.isspace() for char in prefix):
            raise ValueError("key_prefix must be a non-empty Redis key prefix")
        self.redis = redis_client
        self.key_prefix = prefix

    def consume_once(
        self,
        request: NonceConsumptionRequest,
        *,
        now: int | float | datetime | None = None,
    ) -> NonceConsumptionResult:
        del now
        _validate_nonce_consumption_request(request)
        key = f"{self.key_prefix}:{request.nonce_key}"
        scope_key = _nonce_scope_key(request)
        result = int(
            self.redis.eval(
                self._CONSUME_SCRIPT,
                1,
                key,
                scope_key,
                str(float(request.expires_at)),
            )
        )
        statuses = {
            1: NonceConsumptionStatus.CONSUMED,
            0: NonceConsumptionStatus.REPLAYED,
            -1: NonceConsumptionStatus.SCOPE_MISMATCH,
            -2: NonceConsumptionStatus.EXPIRED,
        }
        if result not in statuses:
            raise RuntimeError(
                f"unexpected Redis nonce consumption result: {result}"
            )
        return NonceConsumptionResult(statuses[result])


PURPOSE_POLICIES: dict[str, PurposePolicy] = {
    PREVIEW: PurposePolicy(
        action="preview",
        allowed_variants=frozenset((PREVIEW,)),
    ),
    ORIGINAL: PurposePolicy(
        action="download",
        allowed_variants=frozenset((ORIGINAL,)),
    ),
    EXPORT: PurposePolicy(
        action="export",
        allowed_variants=frozenset((EXPORT,)),
    ),
    ADMIN_REVIEW: PurposePolicy(
        action="admin_review",
        allowed_variants=frozenset(asset_security.ASSET_VARIANTS),
        requires_owner=False,
        requires_admin=True,
    ),
}


def authorize_download(
    asset_record: Mapping[str, Any] | None = None,
    user_context: Any = None,
    order_context: Any = None,
    job_context: Any = None,
    purpose: str | None = None,
    variant: str | None = None,
    token: str | None = None,
    secret: str | bytes | None = None,
    now: int | float | datetime | None = None,
    audit_metadata: Mapping[str, Any] | None = None,
    consumed_token_keys: Any = None,
    nonce_consumer: NonceConsumer | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return a structured allow/deny decision for protected asset access.

    The guard is framework-neutral: callers pass already-loaded records and
    request/session context, then route code can translate the returned
    decision into HTTP responses, object-storage redirects, or audit logs.
    """
    asset_record = kwargs.pop("asset", asset_record)
    user_context = kwargs.pop("user", user_context)
    order_context = kwargs.pop("order", order_context)
    job_context = kwargs.pop("job", job_context)
    secret = kwargs.pop("signing_secret", secret)
    audit_metadata = kwargs.pop("metadata", audit_metadata)
    consumed_token_keys = kwargs.pop("consumed_nonces", consumed_token_keys)
    consumed_token_keys = kwargs.pop("used_nonces", consumed_token_keys)
    consumed_token_keys = kwargs.pop("token_ledger", consumed_token_keys)
    nonce_consumer = kwargs.pop("nonce_store", nonce_consumer)
    nonce_consumer = kwargs.pop("atomic_nonce_consumer", nonce_consumer)

    action = _action_for_purpose(purpose)
    token_claims = _peek_token_payload(token)
    expires_at = _expires_at(token_claims)
    asset_id = _asset_id(asset_record) or _string_id(token_claims.get("asset_id"))
    consumption_key = _consumption_key(token_claims)
    audit = _audit_metadata(
        asset_record=asset_record,
        user_context=user_context,
        order_context=order_context,
        job_context=job_context,
        purpose=purpose,
        variant=variant,
        token_claims=token_claims,
        extra=audit_metadata,
    )
    audit["token_consumption_key"] = consumption_key
    audit["token_one_time_required"] = asset_security.requires_one_time_consumption(purpose)

    def deny(reason: str) -> dict[str, Any]:
        return _decision(
            allowed=False,
            reason=reason,
            asset_id=asset_id,
            action=action,
            expires_at=expires_at,
            audit=audit,
        )

    if not token:
        return deny(REASON_MISSING_TOKEN)
    if secret is None:
        return deny(REASON_MISSING_SECRET)

    try:
        claims = asset_security.verify_asset_url_token(token, secret, now=now)
    except asset_security.ExpiredAssetTokenError:
        return deny(REASON_TOKEN_EXPIRED)
    except asset_security.InvalidAssetTokenError as exc:
        if "signature" in str(exc).lower():
            return deny(REASON_INVALID_SIGNATURE)
        return deny(REASON_INVALID_TOKEN)
    except asset_security.AssetTokenError:
        return deny(REASON_INVALID_TOKEN)
    except (TypeError, ValueError):
        return deny(REASON_INVALID_TOKEN)

    token_claims = claims
    expires_at = _expires_at(claims)
    asset_id = _asset_id(asset_record) or _string_id(claims.get("asset_id"))
    consumption_key = _consumption_key(claims)
    audit = _audit_metadata(
        asset_record=asset_record,
        user_context=user_context,
        order_context=order_context,
        job_context=job_context,
        purpose=purpose,
        variant=variant,
        token_claims=claims,
        extra=audit_metadata,
    )
    audit["token_valid"] = True
    audit["token_consumption_key"] = consumption_key
    audit["token_one_time_required"] = asset_security.requires_one_time_consumption(claims.get("purpose"))

    def verified_deny(reason: str) -> dict[str, Any]:
        return _decision(
            allowed=False,
            reason=reason,
            asset_id=asset_id,
            action=action,
            expires_at=expires_at,
            audit=audit,
        )

    if purpose != claims.get("purpose"):
        return verified_deny(REASON_PURPOSE_MISMATCH)
    if variant != claims.get("variant"):
        return verified_deny(REASON_VARIANT_MISMATCH)
    if asset_record is None:
        return verified_deny(REASON_ASSET_MISSING)

    policy = PURPOSE_POLICIES.get(purpose or "")
    if policy is None:
        return verified_deny(REASON_PURPOSE_MISMATCH)
    action = policy.action
    audit["action"] = action

    if variant not in policy.allowed_variants:
        return verified_deny(REASON_PURPOSE_VARIANT_MISMATCH)

    asset_identifier = _asset_id(asset_record)
    if asset_identifier is not None and asset_identifier != _string_id(claims.get("asset_id")):
        return verified_deny(REASON_ASSET_MISMATCH)

    asset_purposes = _string_set(_read_value(asset_record, ("allowed_purposes", "purposes")))
    if asset_purposes and purpose not in asset_purposes:
        return verified_deny(REASON_PURPOSE_NOT_ALLOWED)

    asset_variants = _string_set(_read_value(asset_record, ("allowed_variants", "available_variants", "variants")))
    if asset_variants and variant not in asset_variants:
        return verified_deny(REASON_VARIANT_NOT_AVAILABLE)

    if _context_mismatch(
        claim_value=claims.get("order_id"),
        record_value=_asset_order_id(asset_record),
        context_value=_order_id(order_context),
    ):
        return verified_deny(REASON_ORDER_MISMATCH)

    if _context_mismatch(
        claim_value=claims.get("job_id"),
        record_value=_asset_job_id(asset_record),
        context_value=_job_id(job_context),
    ):
        return verified_deny(REASON_JOB_MISMATCH)

    if _context_mismatch(
        claim_value=claims.get("user_id"),
        record_value=None if not policy.requires_owner else _asset_user_id(asset_record),
        context_value=_user_id(user_context),
    ):
        return verified_deny(REASON_USER_MISMATCH)

    if policy.requires_admin and not _has_admin_review_access(user_context, claims):
        return verified_deny(REASON_ADMIN_REQUIRED)

    if asset_security.requires_one_time_consumption(claims.get("purpose")):
        if consumption_key and _token_key_consumed(consumed_token_keys, consumption_key):
            audit["token_consumption_status"] = NonceConsumptionStatus.REPLAYED.value
            return verified_deny(REASON_TOKEN_REPLAYED)
        if nonce_consumer is None:
            audit["token_consumption_status"] = "consumer_required"
            return verified_deny(REASON_NONCE_CONSUMER_REQUIRED)
        try:
            consumption_request = nonce_consumption_request_from_claims(claims)
            consumption_result = nonce_consumer.consume_once(consumption_request, now=now)
        except Exception:
            audit["token_consumption_status"] = "consumer_error"
            return verified_deny(REASON_NONCE_CONSUMER_ERROR)
        if not isinstance(consumption_result, NonceConsumptionResult):
            audit["token_consumption_status"] = "consumer_error"
            return verified_deny(REASON_NONCE_CONSUMER_ERROR)
        if not isinstance(consumption_result.status, NonceConsumptionStatus):
            audit["token_consumption_status"] = "consumer_error"
            return verified_deny(REASON_NONCE_CONSUMER_ERROR)
        audit["token_consumption_status"] = consumption_result.status.value
        if consumption_result.status is NonceConsumptionStatus.EXPIRED:
            return verified_deny(REASON_TOKEN_EXPIRED)
        if not consumption_result.consumed:
            return verified_deny(REASON_TOKEN_REPLAYED)

    return _decision(
        allowed=True,
        reason=REASON_ALLOWED,
        asset_id=asset_id,
        action=action,
        expires_at=expires_at,
        audit=audit,
    )


def guard_download(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return authorize_download(*args, **kwargs)


def check_download_access(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return authorize_download(*args, **kwargs)


def evaluate_download_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return authorize_download(*args, **kwargs)


def _decision(
    *,
    allowed: bool,
    reason: str,
    asset_id: str | None,
    action: str,
    expires_at: int | float | None,
    audit: dict[str, Any],
) -> dict[str, Any]:
    audit = dict(audit)
    audit["allowed"] = allowed
    audit["deny_reason"] = None if allowed else reason
    audit["action"] = action
    return {
        "allowed": allowed,
        "reason": reason,
        "asset_id": asset_id,
        "action": action,
        "expires_at": expires_at,
        "audit": audit,
        "audit_metadata": audit,
    }


def _audit_metadata(
    *,
    asset_record: Mapping[str, Any] | None,
    user_context: Any,
    order_context: Any,
    job_context: Any,
    purpose: str | None,
    variant: str | None,
    token_claims: Mapping[str, Any],
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "asset_id": _asset_id(asset_record) or _string_id(token_claims.get("asset_id")),
        "asset_user_id": _asset_user_id(asset_record),
        "asset_order_id": _asset_order_id(asset_record),
        "asset_job_id": _asset_job_id(asset_record),
        "actor_user_id": _user_id(user_context),
        "context_order_id": _order_id(order_context),
        "context_job_id": _job_id(job_context),
        "requested_purpose": purpose,
        "requested_variant": variant,
        "token_asset_id": _string_id(token_claims.get("asset_id")),
        "token_user_id": _string_id(token_claims.get("user_id")),
        "token_order_id": _string_id(token_claims.get("order_id")),
        "token_job_id": _string_id(token_claims.get("job_id")),
        "token_purpose": _string_id(token_claims.get("purpose")),
        "token_variant": _string_id(token_claims.get("variant")),
        "token_nonce": _string_id(token_claims.get("nonce")),
        "expires_at": _expires_at(token_claims),
    }
    if extra:
        audit["metadata"] = dict(extra)
    return audit


def _action_for_purpose(purpose: str | None) -> str:
    policy = PURPOSE_POLICIES.get(purpose or "")
    if policy is None:
        return "unknown"
    return policy.action


def _context_mismatch(*, claim_value: Any, record_value: str | None, context_value: str | None) -> bool:
    claim_id = _string_id(claim_value)
    values = [value for value in (claim_id, record_value, context_value) if value is not None]
    if len(values) < 2:
        return False
    return len(set(values)) != 1


def _has_admin_review_access(user_context: Any, claims: Mapping[str, Any]) -> bool:
    admin_flags = ("can_admin_review", "is_admin", "admin", "is_staff")
    admin_roles = ("admin", "reviewer", "staff")
    return (
        _truthy_flag(user_context, admin_flags)
        or _truthy_flag(claims, admin_flags)
        or _has_role(user_context, admin_roles)
        or _has_role(claims, admin_roles)
    )


def _has_role(source: Any, allowed_roles: tuple[str, ...]) -> bool:
    roles = _read_value(source, ("role", "roles", "permissions", "scopes"))
    if roles is None:
        return False
    return bool({role.lower() for role in _string_set(roles)}.intersection(allowed_roles))


def _truthy_flag(source: Any, names: tuple[str, ...]) -> bool:
    value = _read_value(source, names)
    return bool(value) if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes"}


def _asset_id(asset_record: Mapping[str, Any] | None) -> str | None:
    return _string_id(_read_value(asset_record, ("asset_id", "id", "uuid")))


def _asset_user_id(asset_record: Mapping[str, Any] | None) -> str | None:
    return _string_id(_read_value(asset_record, ("user_id", "owner_user_id", "customer_user_id")))


def _asset_order_id(asset_record: Mapping[str, Any] | None) -> str | None:
    return _string_id(_read_value(asset_record, ("order_id", "payment_order_id", "point_order_id")))


def _asset_job_id(asset_record: Mapping[str, Any] | None) -> str | None:
    return _string_id(_read_value(asset_record, ("job_id", "generation_job_id")))


def _user_id(user_context: Any) -> str | None:
    return _string_id(_read_value(user_context, ("user_id", "id", "uuid")))


def _order_id(order_context: Any) -> str | None:
    return _string_id(_read_value(order_context, ("order_id", "id", "uuid")))


def _job_id(job_context: Any) -> str | None:
    return _string_id(_read_value(job_context, ("job_id", "id", "uuid")))


def _read_value(source: Any, names: tuple[str, ...]) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
        return None
    if isinstance(source, (str, int, float)) and not isinstance(source, bool):
        return source
    for name in names:
        if hasattr(source, name):
            value = getattr(source, name)
            if value is not None:
                return value
    return None


def _string_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {str(item) for item, enabled in value.items() if enabled}
    try:
        return {str(item) for item in value if item is not None}
    except TypeError:
        return {str(value)}


def _expires_at(payload: Mapping[str, Any]) -> int | float | None:
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return None
    return expires_at


def _peek_token_payload(token: str | None) -> dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        return {}
    payload_part = token.split(".", 1)[0]
    try:
        padding = "=" * (-len(payload_part) % 4)
        raw_payload = base64.urlsafe_b64decode((payload_part + padding).encode("ascii"))
        payload = json.loads(raw_payload.decode("utf-8"))
    except (binascii.Error, UnicodeEncodeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _consumption_key(payload: Mapping[str, Any]) -> str | None:
    if not payload:
        return None
    try:
        return asset_security.asset_token_consumption_key(payload)
    except (asset_security.AssetTokenError, TypeError, ValueError):
        return None


def _token_key_consumed(consumed_token_keys: Any, consumption_key: str) -> bool:
    if consumed_token_keys is None:
        return False
    if isinstance(consumed_token_keys, Mapping):
        return bool(consumed_token_keys.get(consumption_key))
    if isinstance(consumed_token_keys, str):
        return consumed_token_keys == consumption_key
    try:
        return consumption_key in consumed_token_keys
    except TypeError:
        return False


def nonce_consumption_request_from_claims(
    claims: Mapping[str, Any],
) -> NonceConsumptionRequest:
    """Build a scoped nonce request from already verified token claims."""
    consumption_key = asset_security.asset_token_consumption_key(claims)
    nonce = _required_claim_text(claims, "nonce")
    expires_at = _expires_at(claims)
    if expires_at is None:
        raise asset_security.InvalidAssetTokenClaimError(
            "asset token expires_at must be a timestamp"
        )
    return NonceConsumptionRequest(
        consumption_key=consumption_key,
        nonce_key="asset-nonce:" + hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
        purpose=_required_claim_text(claims, "purpose"),
        user_id=_required_claim_text(claims, "user_id"),
        asset_id=_required_claim_text(claims, "asset_id"),
        expires_at=float(expires_at),
    )


def _required_claim_text(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if value is None or isinstance(value, bool):
        raise asset_security.InvalidAssetTokenClaimError(
            f"asset token {name} must be a non-empty string"
        )
    text = str(value).strip()
    if not text:
        raise asset_security.InvalidAssetTokenClaimError(
            f"asset token {name} must be a non-empty string"
        )
    return text


def _validate_nonce_consumption_request(request: NonceConsumptionRequest) -> None:
    if not isinstance(request, NonceConsumptionRequest):
        raise TypeError("request must be a NonceConsumptionRequest")
    for name in (
        "consumption_key",
        "nonce_key",
        "purpose",
        "user_id",
        "asset_id",
    ):
        value = getattr(request, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if (
        isinstance(request.expires_at, bool)
        or not isinstance(request.expires_at, (int, float))
    ):
        raise ValueError("expires_at must be a timestamp")


def _nonce_scope_key(request: NonceConsumptionRequest) -> str:
    scope = {
        "asset_id": request.asset_id,
        "consumption_key": request.consumption_key,
        "purpose": request.purpose,
        "user_id": request.user_id,
    }
    return json.dumps(scope, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _timestamp(
    value: int | float | datetime | None,
    *,
    clock: Callable[[], float],
) -> float:
    if value is None:
        return float(clock())
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("now must be a timestamp or datetime")
    return float(value)


__all__ = [
    "ADMIN_REVIEW",
    "EXPORT",
    "InMemoryNonceConsumer",
    "NonceConsumer",
    "NonceConsumptionRequest",
    "NonceConsumptionResult",
    "NonceConsumptionStatus",
    "RedisNonceConsumer",
    "ORIGINAL",
    "PREVIEW",
    "PURPOSE_POLICIES",
    "PurposePolicy",
    "REASON_ADMIN_REQUIRED",
    "REASON_ALLOWED",
    "REASON_ASSET_MISSING",
    "REASON_ASSET_MISMATCH",
    "REASON_INVALID_SIGNATURE",
    "REASON_INVALID_TOKEN",
    "REASON_JOB_MISMATCH",
    "REASON_MISSING_SECRET",
    "REASON_MISSING_TOKEN",
    "REASON_NONCE_CONSUMER_ERROR",
    "REASON_NONCE_CONSUMER_REQUIRED",
    "REASON_ORDER_MISMATCH",
    "REASON_PURPOSE_MISMATCH",
    "REASON_PURPOSE_NOT_ALLOWED",
    "REASON_PURPOSE_VARIANT_MISMATCH",
    "REASON_TOKEN_EXPIRED",
    "REASON_TOKEN_REPLAYED",
    "REASON_USER_MISMATCH",
    "REASON_VARIANT_MISMATCH",
    "REASON_VARIANT_NOT_AVAILABLE",
    "authorize_download",
    "check_download_access",
    "evaluate_download_request",
    "guard_download",
    "nonce_consumption_request_from_claims",
]
