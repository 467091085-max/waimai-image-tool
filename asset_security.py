from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Mapping


PREVIEW = "preview"
ORIGINAL = "original"
EXPORT = "export"
ADMIN_REVIEW = "admin_review"

ASSET_VARIANTS = frozenset((PREVIEW, ORIGINAL, EXPORT, ADMIN_REVIEW))
ASSET_PURPOSES = frozenset((PREVIEW, ORIGINAL, EXPORT, ADMIN_REVIEW))
VALID_ASSET_VARIANTS = ASSET_VARIANTS
VALID_ASSET_PURPOSES = ASSET_PURPOSES

REQUIRED_TOKEN_FIELDS = frozenset(
    (
        "asset_id",
        "user_id",
        "order_id",
        "variant",
        "purpose",
        "expires_at",
        "nonce",
    )
)

PREVIEW_TOKEN_TTL_SECONDS = 5 * 60
DOWNLOAD_TOKEN_TTL_SECONDS = 10 * 60
ONE_TIME_ASSET_PURPOSES = frozenset((ORIGINAL, EXPORT))
CONSUMPTION_KEY_FIELDS = ("asset_id", "user_id", "order_id", "variant", "purpose", "nonce")


class AssetTokenError(ValueError):
    """Base class for asset token verification failures."""


class InvalidAssetTokenError(AssetTokenError):
    """Raised when a token is malformed or has a bad signature."""


class ExpiredAssetTokenError(AssetTokenError):
    """Raised when a token has expired."""


class MissingAssetTokenFieldError(AssetTokenError):
    """Raised when a signed payload is missing required fields."""


class InvalidAssetTokenClaimError(AssetTokenError):
    """Raised when a signed payload has an unsupported claim value."""


def sign_asset_url(payload: Mapping[str, Any], secret: str | bytes, now: int | float | datetime | None = None) -> str:
    """Return a URL-safe signed token for an asset payload.

    The payload is signed as compact JSON. Semantic checks are performed by
    verify_asset_url_token so tests and callers can assert verification errors
    for expired or otherwise invalid tokens.
    """
    del now
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    payload_json = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    encoded_payload = _base64url_encode(payload_json.encode("utf-8"))
    signature = _sign(encoded_payload, secret)
    return f"{encoded_payload}.{signature}"


def verify_asset_url_token(
    token: str,
    secret: str | bytes,
    now: int | float | datetime | None = None,
) -> dict[str, Any]:
    """Verify a signed asset URL token and return its payload."""
    payload_part, signature_part = _split_token(token)
    expected_signature = _sign(payload_part, secret)
    if not hmac.compare_digest(signature_part, expected_signature):
        raise InvalidAssetTokenError("invalid asset token signature")

    payload = _decode_payload(payload_part)
    _validate_payload(payload, now=now)
    return payload


def preview_policy() -> dict[str, Any]:
    """Return the default policy for short-lived preview access."""
    return {
        "ttl_seconds": PREVIEW_TOKEN_TTL_SECONDS,
        "cache_control": f"private, max-age={PREVIEW_TOKEN_TTL_SECONDS}",
        "one_time": False,
    }


def download_policy() -> dict[str, Any]:
    """Return the default policy for protected download access."""
    return {
        "ttl_seconds": DOWNLOAD_TOKEN_TTL_SECONDS,
        "cache_control": "private, no-store, max-age=0",
        "one_time": True,
    }


def requires_one_time_consumption(purpose: str | None) -> bool:
    """Return whether successful access should consume the token nonce."""
    return purpose in ONE_TIME_ASSET_PURPOSES


def asset_token_consumption_key(payload: Mapping[str, Any]) -> str:
    """Return a stable, non-PII key used to track one-time token consumption."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    missing = [field for field in CONSUMPTION_KEY_FIELDS if field not in payload]
    if missing:
        raise MissingAssetTokenFieldError(f"missing asset token fields: {', '.join(missing)}")

    key_payload: dict[str, str] = {}
    for field in CONSUMPTION_KEY_FIELDS:
        value = payload[field]
        if value is None or isinstance(value, bool):
            raise InvalidAssetTokenClaimError(f"asset token {field} must be a non-empty string")
        text = str(value).strip()
        if not text:
            raise InvalidAssetTokenClaimError(f"asset token {field} must be a non-empty string")
        key_payload[field] = text

    raw = json.dumps(key_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return "asset-token:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _split_token(token: str) -> tuple[str, str]:
    if not isinstance(token, str):
        raise InvalidAssetTokenError("asset token must be a string")
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidAssetTokenError("malformed asset token")
    return parts[0], parts[1]


def _decode_payload(encoded_payload: str) -> dict[str, Any]:
    try:
        raw_payload = _base64url_decode(encoded_payload)
        payload = json.loads(raw_payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidAssetTokenError("malformed asset token payload") from exc

    if not isinstance(payload, dict):
        raise InvalidAssetTokenError("asset token payload must be an object")
    return payload


def _validate_payload(payload: Mapping[str, Any], now: int | float | datetime | None = None) -> None:
    missing = sorted(REQUIRED_TOKEN_FIELDS.difference(payload))
    if missing:
        raise MissingAssetTokenFieldError(f"missing asset token fields: {', '.join(missing)}")

    variant = payload["variant"]
    if variant not in ASSET_VARIANTS:
        raise InvalidAssetTokenClaimError("invalid asset token variant")

    purpose = payload["purpose"]
    if purpose not in ASSET_PURPOSES:
        raise InvalidAssetTokenClaimError("invalid asset token purpose")

    expires_at = payload["expires_at"]
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise InvalidAssetTokenClaimError("asset token expires_at must be a timestamp")

    if _timestamp(now) > float(expires_at):
        raise ExpiredAssetTokenError("asset token has expired")


def _timestamp(value: int | float | datetime | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("now must be a timestamp or datetime")
    return float(value)


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        secret_bytes = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        secret_bytes = secret
    else:
        raise TypeError("secret must be str or bytes")

    if not secret_bytes:
        raise ValueError("secret must not be empty")
    return secret_bytes


def _sign(encoded_payload: str, secret: str | bytes) -> str:
    digest = hmac.new(_secret_bytes(secret), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return _base64url_encode(digest)


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    try:
        encoded = (data + padding).encode("ascii")
    except UnicodeEncodeError as exc:
        raise binascii.Error("non-ascii base64 data") from exc
    return base64.b64decode(encoded, altchars=b"-_", validate=True)


__all__ = [
    "ADMIN_REVIEW",
    "ASSET_PURPOSES",
    "ASSET_VARIANTS",
    "AssetTokenError",
    "DOWNLOAD_TOKEN_TTL_SECONDS",
    "CONSUMPTION_KEY_FIELDS",
    "EXPORT",
    "ExpiredAssetTokenError",
    "InvalidAssetTokenClaimError",
    "InvalidAssetTokenError",
    "MissingAssetTokenFieldError",
    "ORIGINAL",
    "ONE_TIME_ASSET_PURPOSES",
    "PREVIEW",
    "PREVIEW_TOKEN_TTL_SECONDS",
    "REQUIRED_TOKEN_FIELDS",
    "VALID_ASSET_PURPOSES",
    "VALID_ASSET_VARIANTS",
    "asset_token_consumption_key",
    "download_policy",
    "preview_policy",
    "requires_one_time_consumption",
    "sign_asset_url",
    "verify_asset_url_token",
]
