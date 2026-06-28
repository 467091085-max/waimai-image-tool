from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

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


@dataclass(frozen=True)
class PurposePolicy:
    action: str
    allowed_variants: frozenset[str]
    requires_owner: bool = True
    requires_admin: bool = False


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
    if (
        consumption_key
        and asset_security.requires_one_time_consumption(claims.get("purpose"))
        and _token_key_consumed(consumed_token_keys, consumption_key)
    ):
        return verified_deny(REASON_TOKEN_REPLAYED)
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


__all__ = [
    "ADMIN_REVIEW",
    "EXPORT",
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
]
