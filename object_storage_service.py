from __future__ import annotations

import os
import secrets
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import asset_security
from storage_db import DEFAULT_OBJECT_STORE_DIR, LocalObjectStorage


MENUS_PREFIX = "menus/"
ORIGINALS_PREFIX = "originals/"
GENERATED_PREFIX = "generated/"
EXPORTS_PREFIX = "exports/"
AI_ASSETS_PREFIX = "ai-assets/"

BUCKET_PREFIXES = (
    MENUS_PREFIX,
    ORIGINALS_PREFIX,
    GENERATED_PREFIX,
    EXPORTS_PREFIX,
    AI_ASSETS_PREFIX,
)

DEFAULT_SIGNED_ACCESS_TTL_SECONDS = asset_security.DOWNLOAD_TOKEN_TTL_SECONDS
DEFAULT_SIGNED_ACCESS_BASE_URL = "/objects"

LOCAL_OBJECT_STORAGE_PROVIDERS = frozenset({"local", "mock", "filesystem"})
REMOTE_OBJECT_STORAGE_PROVIDERS = frozenset({"cos", "oss", "r2", "s3", "minio", "remote"})
OBJECT_STORAGE_PROVIDER_ENV_NAMES = ("OBJECT_STORAGE_PROVIDER", "OBJECT_STORE_PROVIDER")
OBJECT_STORAGE_BUCKET_ENV_NAMES = (
    "OBJECT_STORAGE_BUCKET",
    "TENCENT_COS_BUCKET",
    "COS_BUCKET",
    "OSS_BUCKET",
    "R2_BUCKET",
    "S3_BUCKET",
)
OBJECT_STORAGE_SIGNING_SECRET_ENV_NAMES = (
    "OBJECT_SIGNING_SECRET",
    "ASSET_SIGNING_SECRET",
    "DOWNLOAD_SIGNING_SECRET",
)


class ObjectStorageService:
    """Local object storage facade with strict object-key validation."""

    def __init__(self, root: str | os.PathLike[str] = DEFAULT_OBJECT_STORE_DIR) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._local = LocalObjectStorage(self.root)

    def put_bytes(
        self,
        data_or_object_key: bytes | bytearray | memoryview | str,
        data: bytes | bytearray | memoryview | None = None,
        *,
        object_key: str | None = None,
        prefix: str = GENERATED_PREFIX,
        filename: str | None = None,
    ) -> str:
        """Persist bytes and return the object key.

        Preferred usage is ``put_bytes(data, object_key="menus/file.json")`` for
        an explicit key, or ``put_bytes(data, prefix="generated/", filename=...)``
        for a generated local key. ``put_bytes("key", data)`` is accepted as a
        convenience for common object-store call sites.
        """
        if data is not None:
            if object_key is not None:
                raise TypeError("object_key must not be passed when the first argument is an object key")
            object_key = _require_string(data_or_object_key, "object_key")
            payload = _coerce_bytes(data)
        else:
            payload = _coerce_bytes(data_or_object_key)

        if object_key is not None:
            object_key = validate_object_key(object_key)
            target = self.path_for_key(object_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return object_key

        return validate_object_key(
            self._local.put_bytes(payload, prefix=_normalize_prefix(prefix), filename=filename)
        )

    def put_file(
        self,
        source_or_object_key: str | os.PathLike[str],
        source: str | os.PathLike[str] | None = None,
        *,
        object_key: str | None = None,
        prefix: str = GENERATED_PREFIX,
        filename: str | None = None,
    ) -> str:
        """Persist a local file and return the object key."""
        if source is not None:
            if object_key is not None:
                raise TypeError("object_key must not be passed when the first argument is an object key")
            object_key = _require_string(source_or_object_key, "object_key")
            source_path = Path(source).expanduser()
        else:
            source_path = Path(source_or_object_key).expanduser()

        if object_key is not None:
            return self.put_bytes(source_path.read_bytes(), object_key=object_key)

        return validate_object_key(
            self._local.put_file(source_path, prefix=_normalize_prefix(prefix), filename=filename or source_path.name)
        )

    def read_bytes(self, object_key: str) -> bytes:
        return self.path_for_key(object_key).read_bytes()

    def exists(self, object_key: str) -> bool:
        return self.path_for_key(object_key).is_file()

    def delete(self, object_key: str) -> bool:
        target = self.path_for_key(object_key)
        if not target.exists():
            return False
        if not target.is_file():
            raise IsADirectoryError(str(target))
        target.unlink()
        return True

    def stat(self, object_key: str) -> dict[str, Any]:
        object_key = validate_object_key(object_key)
        target = self.path_for_key(object_key)
        file_stat = target.stat()
        modified_at = datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).replace(microsecond=0)
        return {
            "object_key": object_key,
            "bucket": bucket_for_key(object_key),
            "size": file_stat.st_size,
            "file_size": file_stat.st_size,
            "modified_at": modified_at.isoformat(),
            "mtime": file_stat.st_mtime,
        }

    def list_prefix(self, prefix: str = "") -> list[str]:
        normalized_prefix = validate_object_prefix(prefix)
        base = self.root.resolve() if not normalized_prefix else self.path_for_key(normalized_prefix)
        if not base.exists():
            return []
        if base.is_file():
            return [normalized_prefix]

        root = self.root.resolve()
        keys: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            key = path.relative_to(root).as_posix()
            validate_object_key(key)
            keys.append(key)
        return sorted(keys)

    def path_for_key(self, object_key: str) -> Path:
        object_key = validate_object_key(object_key)
        root = self.root.resolve()
        target = (root / Path(*PurePosixPath(object_key).parts)).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"object key escapes store root: {object_key!r}")
        return target


def create_signed_access(
    object_key: str,
    user_id: str,
    purpose: str,
    variant: str,
    secret: str | bytes,
    expires_in: int | float = DEFAULT_SIGNED_ACCESS_TTL_SECONDS,
    *,
    base_url: str = DEFAULT_SIGNED_ACCESS_BASE_URL,
    now: int | float | datetime | None = None,
) -> dict[str, Any]:
    """Return signed access metadata for an object key."""
    object_key = validate_object_key(object_key)
    if purpose not in asset_security.VALID_ASSET_PURPOSES:
        raise ValueError(f"invalid signed access purpose: {purpose!r}")
    if variant not in asset_security.VALID_ASSET_VARIANTS:
        raise ValueError(f"invalid signed access variant: {variant!r}")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        raise TypeError("expires_in must be a number of seconds")

    issued_at = _timestamp(now)
    expires_at = int(issued_at + float(expires_in))
    payload = {
        "asset_id": object_key,
        "object_key": object_key,
        "user_id": str(user_id),
        "order_id": "",
        "variant": variant,
        "purpose": purpose,
        "expires_at": expires_at,
        "nonce": secrets.token_urlsafe(16),
    }
    token = asset_security.sign_asset_url(payload, secret, now=now)
    url = _signed_url(base_url, object_key, token)
    return {
        "token": token,
        "url": url,
        "signed_url": url,
        "object_key": object_key,
        "user_id": str(user_id),
        "purpose": purpose,
        "variant": variant,
        "expires_at": expires_at,
        "expires_in": float(expires_in),
    }


def verify_signed_access(
    token: str,
    secret: str | bytes,
    now: int | float | datetime | None = None,
) -> dict[str, Any]:
    """Verify a signed object-access token and return its payload."""
    payload = asset_security.verify_asset_url_token(token, secret, now=now)
    raw_object_key = payload.get("object_key", payload.get("asset_id"))
    if not isinstance(raw_object_key, str):
        raise asset_security.InvalidAssetTokenClaimError("asset token object_key must be a string")

    object_key = validate_object_key(raw_object_key)
    asset_id = payload.get("asset_id")
    if not isinstance(asset_id, str):
        raise asset_security.InvalidAssetTokenClaimError("asset token asset_id must be a string")
    if asset_id != object_key:
        raise asset_security.InvalidAssetTokenClaimError("asset token asset_id must match object_key")

    payload["object_key"] = object_key
    return payload


def validate_object_key(object_key: str) -> str:
    if not isinstance(object_key, str):
        raise TypeError("object_key must be a string")

    key = object_key.strip()
    if not key:
        raise ValueError("object_key must not be empty")
    if "\\" in key or "\x00" in key:
        raise ValueError(f"invalid object key: {object_key!r}")
    if key.startswith("/") or PurePosixPath(key).is_absolute():
        raise ValueError(f"invalid object key: {object_key!r}")

    raw_parts = key.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"invalid object key: {object_key!r}")

    path = PurePosixPath(key)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid object key: {object_key!r}")

    normalized = path.as_posix()
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in normalized:
        raise ValueError(f"invalid object key: {object_key!r}")
    return normalized


def validate_object_prefix(prefix: str) -> str:
    if prefix == "":
        return ""
    return validate_object_key(prefix.strip("/"))


def bucket_for_key(object_key: str) -> str | None:
    object_key = validate_object_key(object_key)
    for prefix in BUCKET_PREFIXES:
        if object_key == prefix.rstrip("/") or object_key.startswith(prefix):
            return prefix
    return None


def get_object_storage_service(root: str | os.PathLike[str] | None = None) -> ObjectStorageService:
    configured = os.environ.get("OBJECT_STORE_DIR")
    return ObjectStorageService(root or configured or DEFAULT_OBJECT_STORE_DIR)


def assess_object_storage_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Evaluate object-storage configuration without initializing a remote SDK.

    The current runtime only implements the local storage facade. This function
    gives deployment code and docs a single, explicit production-readiness
    answer for local/mock storage versus a future private remote provider.
    """
    values = os.environ if env is None else env
    provider = _configured_provider(values)
    app_env = _env_value(values, "APP_ENV").lower()
    production_env = app_env in {"production", "prod"}
    local_demo_enabled = _env_truthy(values, "ENABLE_LOCAL_DEMO_STORAGE", default=True)
    signing_secret_configured = bool(_first_env_value(values, OBJECT_STORAGE_SIGNING_SECRET_ENV_NAMES))
    bucket_configured = bool(_first_env_value(values, OBJECT_STORAGE_BUCKET_ENV_NAMES))
    public_read_enabled = _env_truthy(values, "OBJECT_STORAGE_PUBLIC_READ", default=False)
    private_bucket_enabled = _env_truthy(values, "OBJECT_STORAGE_PRIVATE", default=True)

    blocking_issues: list[str] = []
    warnings: list[str] = []
    provider_kind = _provider_kind(provider)

    if provider_kind == "local":
        mode = "mock_demo" if provider == "mock" else "local_demo"
        if production_env or not local_demo_enabled:
            blocking_issues.append("private_remote_object_storage_provider_required")
            if not signing_secret_configured:
                blocking_issues.append("object_signing_secret_required")
        else:
            warnings.append("local_object_storage_is_for_development_only")
            if not signing_secret_configured:
                warnings.append("object_signing_secret_not_configured")
    elif provider_kind == "remote":
        mode = "remote_private"
        if not bucket_configured:
            blocking_issues.append("object_storage_bucket_required")
        if not signing_secret_configured:
            blocking_issues.append("object_signing_secret_required")
        if public_read_enabled or not private_bucket_enabled:
            blocking_issues.append("private_object_storage_required")
        if provider == "remote":
            warnings.append("generic_remote_provider_requires_runtime_adapter")
        else:
            warnings.append("remote_provider_sdk_not_initialized_by_readiness_check")
    else:
        mode = "unknown"
        blocking_issues.append("unsupported_object_storage_provider")
        if production_env or not local_demo_enabled:
            blocking_issues.append("private_remote_object_storage_provider_required")
        if not signing_secret_configured:
            blocking_issues.append("object_signing_secret_required")

    return {
        "ready": not blocking_issues,
        "provider": provider,
        "mode": mode,
        "blockingIssues": blocking_issues,
        "warnings": warnings,
    }


def _normalize_prefix(prefix: str) -> str:
    return validate_object_prefix(prefix) or GENERATED_PREFIX.rstrip("/")


def _configured_provider(env: Mapping[str, str]) -> str:
    provider = _first_env_value(env, OBJECT_STORAGE_PROVIDER_ENV_NAMES)
    return (provider or "local").strip().lower()


def _provider_kind(provider: str) -> str:
    if provider in LOCAL_OBJECT_STORAGE_PROVIDERS:
        return "local"
    if provider in REMOTE_OBJECT_STORAGE_PROVIDERS:
        return "remote"
    return "unknown"


def _first_env_value(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = _env_value(env, name)
        if value:
            return value
    return ""


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if value is None:
        return ""
    return str(value).strip()


def _env_truthy(env: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    value = _env_value(env, name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on", "enabled"}


def _coerce_bytes(value: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TypeError("data must be bytes-like")


def _require_string(value: str | os.PathLike[str] | bytes | bytearray | memoryview, name: str) -> str:
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


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


def _signed_url(base_url: str, object_key: str, token: str) -> str:
    base = base_url.rstrip("/") or DEFAULT_SIGNED_ACCESS_BASE_URL
    return f"{base}/{quote(object_key, safe='/')}?token={quote(token, safe='')}"


__all__ = [
    "AI_ASSETS_PREFIX",
    "BUCKET_PREFIXES",
    "DEFAULT_SIGNED_ACCESS_BASE_URL",
    "DEFAULT_SIGNED_ACCESS_TTL_SECONDS",
    "EXPORTS_PREFIX",
    "GENERATED_PREFIX",
    "MENUS_PREFIX",
    "ORIGINALS_PREFIX",
    "ObjectStorageService",
    "assess_object_storage_readiness",
    "bucket_for_key",
    "create_signed_access",
    "get_object_storage_service",
    "validate_object_key",
    "validate_object_prefix",
    "verify_signed_access",
]
