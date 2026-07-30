from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OBJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EXPORT_STATUSES = frozenset({"ready", "failed", "expired"})

ACCESS_ALLOWED = "allowed"
ACCESS_TOKEN_REPLAYED = "token_replayed"
ACCESS_TOKEN_EXPIRED = "token_expired"
ACCESS_TOKEN_IN_USE = "token_in_use"
ACCESS_EXPORT_NOT_READY = "export_not_ready"
NONCE_UNREGISTERED = "unregistered"
NONCE_AVAILABLE = "available"
NONCE_RESERVED = "reserved"

MAX_JSON_BYTES = 128 * 1024
MAX_LIST_LIMIT = 200
_SENSITIVE_AUDIT_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "ip",
        "ipaddress",
        "nonce",
        "rawip",
        "rawnonce",
        "rawtoken",
        "remoteaddr",
        "token",
        "tokennonce",
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


class ProductExportStoreError(RuntimeError):
    pass


class InvalidProductExportInput(ProductExportStoreError, ValueError):
    pass


class ProductExportNotFound(ProductExportStoreError, LookupError):
    pass


class ProductExportConflict(ProductExportStoreError):
    pass


class ProductExportAccessConflict(ProductExportConflict):
    pass


@dataclass(frozen=True)
class ExportCreateResult:
    record: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class ExportAccessResult:
    audit: dict[str, Any]
    allowed: bool
    reason: str
    download_count: int
    nonce_status: str
    idempotent: bool


_EXPORT_COLUMNS = """
    id, owner_user_id, idempotency_key, generation_job_id,
    generation_request_sha256, export_request_sha256,
    manifest_object_ref, manifest_sha256, platform_set, watermark,
    zip_object_ref, zip_sha256, zip_size_bytes, status, content_sha256,
    download_count, created_at, updated_at
""".strip()

_NONCE_COLUMNS = """
    token_nonce_digest, owner_user_id, export_id, expires_at,
    consumed_at, consumed_action_id, consumed_request_id,
    reservation_id, reserved_at, reservation_expires_at, created_at
""".strip()

_AUDIT_COLUMNS = """
    action_id, request_id, owner_user_id, export_id,
    token_nonce_digest, ip_digest, allowed, deny_reason,
    one_time_required, nonce_consumed, download_count_after,
    request_sha256, content_sha256, metadata, created_at
""".strip()

_INSERT_EXPORT_SQL = f"""
/* product_export_store:create_export */
INSERT INTO product_export_packages (
    id, owner_user_id, idempotency_key, generation_job_id,
    generation_request_sha256, export_request_sha256,
    manifest_object_ref, manifest_sha256, platform_set, watermark,
    zip_object_ref, zip_sha256, zip_size_bytes, status, content_sha256
) VALUES (
    %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s::jsonb, %s::jsonb,
    %s, %s, %s, %s, %s
)
ON CONFLICT DO NOTHING
RETURNING {_EXPORT_COLUMNS}
"""

_SELECT_EXPORT_BY_ID_FOR_UPDATE_SQL = f"""
/* product_export_store:select_export_by_id_for_update */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE id = %s
FOR UPDATE
"""

_SELECT_EXPORT_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_export_store:select_export_by_idempotency_for_update */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE owner_user_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_OWNED_EXPORT_BY_IDEMPOTENCY_SQL = f"""
/* product_export_store:select_owned_export_by_idempotency */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE owner_user_id = %s AND idempotency_key = %s
"""

_SELECT_OWNED_EXPORT_SQL = f"""
/* product_export_store:select_owned_export */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE id = %s AND owner_user_id = %s
"""

_SELECT_OWNED_EXPORT_FOR_UPDATE_SQL = f"""
/* product_export_store:select_owned_export_for_update */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE id = %s AND owner_user_id = %s
FOR UPDATE
"""

_LIST_OWNED_EXPORTS_SQL = f"""
/* product_export_store:list_owned_exports */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE owner_user_id = %s
ORDER BY created_at DESC, id DESC
LIMIT %s OFFSET %s
"""

_LIST_OWNED_EXPORTS_BY_STATUS_SQL = f"""
/* product_export_store:list_owned_exports_by_status */
SELECT {_EXPORT_COLUMNS}
FROM product_export_packages
WHERE owner_user_id = %s AND status = %s
ORDER BY created_at DESC, id DESC
LIMIT %s OFFSET %s
"""

_INSERT_NONCE_SQL = f"""
/* product_export_store:insert_nonce */
INSERT INTO product_export_token_nonces (
    token_nonce_digest, owner_user_id, export_id, expires_at
) VALUES (%s, %s, %s, %s)
ON CONFLICT (token_nonce_digest) DO NOTHING
RETURNING {_NONCE_COLUMNS},
          (expires_at <= CURRENT_TIMESTAMP) AS expired,
          (
              reservation_expires_at IS NOT NULL
              AND reservation_expires_at <= CURRENT_TIMESTAMP
          ) AS reservation_expired
"""

_SELECT_NONCE_FOR_UPDATE_SQL = f"""
/* product_export_store:select_nonce_for_update */
SELECT {_NONCE_COLUMNS},
       (expires_at <= CURRENT_TIMESTAMP) AS expired,
       (
           reservation_expires_at IS NOT NULL
           AND reservation_expires_at <= CURRENT_TIMESTAMP
       ) AS reservation_expired
FROM product_export_token_nonces
WHERE token_nonce_digest = %s
FOR UPDATE
"""

_SELECT_NONCE_SQL = f"""
/* product_export_store:select_nonce */
SELECT {_NONCE_COLUMNS},
       (expires_at <= CURRENT_TIMESTAMP) AS expired,
       (
           reservation_expires_at IS NOT NULL
           AND reservation_expires_at <= CURRENT_TIMESTAMP
       ) AS reservation_expired
FROM product_export_token_nonces
WHERE token_nonce_digest = %s
"""

_RESERVE_NONCE_SQL = f"""
/* product_export_store:reserve_nonce */
UPDATE product_export_token_nonces
SET reservation_id = %s,
    reserved_at = CURRENT_TIMESTAMP,
    reservation_expires_at = expires_at
WHERE token_nonce_digest = %s
  AND owner_user_id = %s
  AND export_id = %s
  AND consumed_at IS NULL
  AND expires_at > CURRENT_TIMESTAMP
  AND (
      %s::boolean
      OR reservation_id IS NULL
      OR reservation_expires_at <= CURRENT_TIMESTAMP
  )
RETURNING {_NONCE_COLUMNS},
          FALSE AS expired,
          FALSE AS reservation_expired
"""

_RELEASE_NONCE_RESERVATION_SQL = f"""
/* product_export_store:release_nonce_reservation */
UPDATE product_export_token_nonces
SET reservation_id = NULL,
    reserved_at = NULL,
    reservation_expires_at = NULL
WHERE token_nonce_digest = %s
  AND owner_user_id = %s
  AND export_id = %s
  AND reservation_id = %s
  AND consumed_at IS NULL
RETURNING {_NONCE_COLUMNS},
          (expires_at <= CURRENT_TIMESTAMP) AS expired,
          FALSE AS reservation_expired
"""

_CONSUME_NONCE_SQL = f"""
/* product_export_store:consume_nonce */
UPDATE product_export_token_nonces
SET consumed_at = CURRENT_TIMESTAMP,
    consumed_action_id = %s,
    consumed_request_id = %s,
    reservation_id = NULL,
    reserved_at = NULL,
    reservation_expires_at = NULL
WHERE token_nonce_digest = %s
  AND owner_user_id = %s
  AND export_id = %s
  AND consumed_at IS NULL
  AND expires_at > CURRENT_TIMESTAMP
  AND (%s::text IS NULL OR reservation_id = %s)
RETURNING {_NONCE_COLUMNS},
          FALSE AS expired,
          FALSE AS reservation_expired
"""

_INCREMENT_DOWNLOAD_COUNT_SQL = f"""
/* product_export_store:increment_download_count */
UPDATE product_export_packages
SET download_count = download_count + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status = 'ready'
RETURNING {_EXPORT_COLUMNS}
"""

_SELECT_ACCESS_BY_ACTION_FOR_UPDATE_SQL = f"""
/* product_export_store:select_access_by_action_for_update */
SELECT {_AUDIT_COLUMNS}
FROM product_export_access_audits
WHERE action_id = %s
  AND owner_user_id = %s
  AND export_id = %s
FOR UPDATE
"""

_SELECT_ACCESS_BY_REQUEST_FOR_UPDATE_SQL = f"""
/* product_export_store:select_access_by_request_for_update */
SELECT {_AUDIT_COLUMNS}
FROM product_export_access_audits
WHERE request_id = %s
  AND owner_user_id = %s
  AND export_id = %s
FOR UPDATE
"""

_INSERT_ACCESS_SQL = f"""
/* product_export_store:insert_access */
INSERT INTO product_export_access_audits (
    action_id, request_id, owner_user_id, export_id,
    token_nonce_digest, ip_digest, allowed, deny_reason,
    one_time_required, nonce_consumed, download_count_after,
    request_sha256, content_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    TRUE, %s, %s,
    %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_AUDIT_COLUMNS}
"""

_SELECT_OWNED_ACCESS_SQL = f"""
/* product_export_store:select_owned_access */
SELECT {_AUDIT_COLUMNS}
FROM product_export_access_audits
WHERE action_id = %s
  AND owner_user_id = %s
  AND export_id = %s
"""

_LIST_OWNED_ACCESS_SQL = f"""
/* product_export_store:list_owned_access */
SELECT {_AUDIT_COLUMNS}
FROM product_export_access_audits
WHERE owner_user_id = %s
ORDER BY created_at DESC, action_id DESC
LIMIT %s OFFSET %s
"""

_LIST_OWNED_ACCESS_FOR_EXPORT_SQL = f"""
/* product_export_store:list_owned_access_for_export */
SELECT {_AUDIT_COLUMNS}
FROM product_export_access_audits
WHERE owner_user_id = %s AND export_id = %s
ORDER BY created_at DESC, action_id DESC
LIMIT %s OFFSET %s
"""


def create_export_package(
    cursor: CursorLike,
    *,
    export_id: str,
    owner_user_id: str,
    idempotency_key: str,
    generation_job_id: str,
    generation_request_sha256: str,
    export_request_sha256: str,
    manifest_object_ref: str,
    manifest_sha256: str,
    platforms: Sequence[str],
    watermark: Mapping[str, Any],
    zip_object_ref: str,
    zip_sha256: str,
    zip_size_bytes: int,
    status: str = "ready",
) -> ExportCreateResult:
    """Create an immutable export record inside a caller-owned transaction."""

    values = _normalize_export_values(
        export_id=export_id,
        owner_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        generation_job_id=generation_job_id,
        generation_request_sha256=generation_request_sha256,
        export_request_sha256=export_request_sha256,
        manifest_object_ref=manifest_object_ref,
        manifest_sha256=manifest_sha256,
        platforms=platforms,
        watermark=watermark,
        zip_object_ref=zip_object_ref,
        zip_sha256=zip_sha256,
        zip_size_bytes=zip_size_bytes,
        status=status,
    )
    cursor.execute(
        _INSERT_EXPORT_SQL,
        (
            values["id"],
            values["owner_user_id"],
            values["idempotency_key"],
            values["generation_job_id"],
            values["generation_request_sha256"],
            values["export_request_sha256"],
            values["manifest_object_ref"],
            values["manifest_sha256"],
            _canonical_json(values["platform_set"]),
            _canonical_json(values["watermark"]),
            values["zip_object_ref"],
            values["zip_sha256"],
            values["zip_size_bytes"],
            values["status"],
            values["content_sha256"],
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return ExportCreateResult(record=_decode_row(inserted), created=True)

    cursor.execute(_SELECT_EXPORT_BY_ID_FOR_UPDATE_SQL, (values["id"],))
    existing = _fetchone_dict(cursor)
    if existing is None:
        cursor.execute(
            _SELECT_EXPORT_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
            (values["owner_user_id"], values["idempotency_key"]),
        )
        existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductExportConflict(
            "export create conflicted without a resolvable record"
        )

    record = _decode_row(existing)
    if (
        str(record.get("id") or "") != values["id"]
        or str(record.get("owner_user_id") or "") != values["owner_user_id"]
        or str(record.get("idempotency_key") or "")
        != values["idempotency_key"]
        or not _same_export_request(record, values)
    ):
        raise ProductExportConflict(
            f"export replay content changed: {values['id']}"
        )
    return ExportCreateResult(record=record, created=False)


def get_owned_export_by_idempotency(
    cursor: CursorLike,
    *,
    owner_user_id: str,
    idempotency_key: str,
    include_private: bool = False,
) -> dict[str, Any] | None:
    owner = _identifier(owner_user_id, "owner_user_id")
    key = _identifier(idempotency_key, "idempotency_key")
    cursor.execute(
        _SELECT_OWNED_EXPORT_BY_IDEMPOTENCY_SQL,
        (owner, key),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        return None
    record = _decode_row(row)
    return record if include_private else public_export_dto(record)


def get_owned_export(
    cursor: CursorLike,
    *,
    export_id: str,
    owner_user_id: str,
    include_private: bool = False,
) -> dict[str, Any]:
    identifier = _identifier(export_id, "export_id")
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_OWNED_EXPORT_SQL, (identifier, owner))
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductExportNotFound(identifier)
    record = _decode_row(row)
    return record if include_private else public_export_dto(record)


def list_owned_exports(
    cursor: CursorLike,
    *,
    owner_user_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_private: bool = False,
) -> list[dict[str, Any]]:
    owner = _identifier(owner_user_id, "owner_user_id")
    clean_limit = _bounded_limit(limit)
    clean_offset = _nonnegative_int(offset, "offset")
    if status is None:
        cursor.execute(
            _LIST_OWNED_EXPORTS_SQL,
            (owner, clean_limit, clean_offset),
        )
    else:
        clean_status = _choice(status, EXPORT_STATUSES, "status")
        cursor.execute(
            _LIST_OWNED_EXPORTS_BY_STATUS_SQL,
            (owner, clean_status, clean_limit, clean_offset),
        )
    records = [_decode_row(row) for row in _fetchall_dicts(cursor)]
    if include_private:
        return records
    return [public_export_dto(record) for record in records]


def consume_export_nonce(
    cursor: CursorLike,
    *,
    action_id: str,
    request_id: str,
    owner_user_id: str,
    export_id: str,
    token_nonce: str,
    token_expires_at: datetime | int | float | str,
    ip_address: str,
    digest_secret: str | bytes,
    metadata: Mapping[str, Any] | None = None,
    reservation_id: str | None = None,
) -> ExportAccessResult:
    """Atomically consume one export token and record its access decision.

    Only the nonce SHA-256 and a keyed IP digest are sent to PostgreSQL. The
    caller owns the transaction and must roll it back when this function raises.
    """

    action = _identifier(action_id, "action_id")
    request = _identifier(request_id, "request_id")
    owner = _identifier(owner_user_id, "owner_user_id")
    identifier = _identifier(export_id, "export_id")
    reservation = (
        _identifier(reservation_id, "reservation_id")
        if reservation_id is not None
        else None
    )
    nonce_digest = token_nonce_sha256(token_nonce)
    ip_value_digest = ip_hmac_sha256(ip_address, digest_secret)
    expires_at = _timestamp(token_expires_at, "token_expires_at")
    safe_metadata = _audit_metadata(
        metadata or {},
        raw_token=token_nonce,
        raw_ip=ip_address,
    )
    request_sha256 = _access_request_sha256(
        action_id=action,
        request_id=request,
        owner_user_id=owner,
        export_id=identifier,
        token_nonce_digest=nonce_digest,
        ip_digest=ip_value_digest,
        token_expires_at=expires_at,
        metadata=safe_metadata,
        operation="consume",
    )

    export_record = _locked_owned_export(cursor, identifier, owner)
    existing = _existing_access_replay(
        cursor,
        action_id=action,
        request_id=request,
        owner_user_id=owner,
        export_id=identifier,
        request_sha256=request_sha256,
    )
    if existing is not None:
        return _access_result(existing, idempotent=True)

    cursor.execute(
        _INSERT_NONCE_SQL,
        (nonce_digest, owner, identifier, expires_at),
    )
    nonce_record = _fetchone_dict(cursor)
    if nonce_record is None:
        cursor.execute(_SELECT_NONCE_FOR_UPDATE_SQL, (nonce_digest,))
        nonce_record = _fetchone_dict(cursor)
    if nonce_record is None:
        raise ProductExportAccessConflict(
            "nonce insert conflicted without a resolvable record"
        )
    nonce_record = _decode_row(nonce_record)
    _validate_nonce_scope(
        nonce_record,
        owner_user_id=owner,
        export_id=identifier,
        expires_at=expires_at,
    )

    allowed = False
    nonce_consumed = False
    reason = ACCESS_EXPORT_NOT_READY
    nonce_status = "not_consumed"
    download_count = int(export_record.get("download_count") or 0)

    if str(export_record.get("status") or "") != "ready":
        reason = ACCESS_EXPORT_NOT_READY
        nonce_status = "not_consumed"
    elif nonce_record.get("consumed_at") is not None:
        reason = ACCESS_TOKEN_REPLAYED
        nonce_status = "replayed"
    elif bool(nonce_record.get("expired")):
        reason = ACCESS_TOKEN_EXPIRED
        nonce_status = "expired"
    elif (
        nonce_record.get("reservation_id") is not None
        and not bool(nonce_record.get("reservation_expired"))
        and nonce_record.get("reservation_id") != reservation
    ):
        reason = ACCESS_TOKEN_IN_USE
        nonce_status = "reserved"
    else:
        cursor.execute(
            _CONSUME_NONCE_SQL,
            (
                action,
                request,
                nonce_digest,
                owner,
                identifier,
                reservation,
                reservation,
            ),
        )
        consumed = _fetchone_dict(cursor)
        if consumed is None:
            cursor.execute(_SELECT_NONCE_FOR_UPDATE_SQL, (nonce_digest,))
            refreshed = _fetchone_dict(cursor)
            if refreshed is None:
                raise ProductExportAccessConflict(
                    "nonce disappeared during consumption"
                )
            refreshed = _decode_row(refreshed)
            if refreshed.get("consumed_at") is not None:
                reason = ACCESS_TOKEN_REPLAYED
                nonce_status = "replayed"
            elif bool(refreshed.get("expired")):
                reason = ACCESS_TOKEN_EXPIRED
                nonce_status = "expired"
            elif (
                refreshed.get("reservation_id") is not None
                and not bool(refreshed.get("reservation_expired"))
            ):
                reason = ACCESS_TOKEN_IN_USE
                nonce_status = "reserved"
            else:
                raise ProductExportAccessConflict(
                    "nonce consumption failed without a terminal state"
                )
        else:
            cursor.execute(
                _INCREMENT_DOWNLOAD_COUNT_SQL,
                (identifier, owner),
            )
            updated_export = _fetchone_dict(cursor)
            if updated_export is None:
                raise ProductExportAccessConflict(
                    "export became unavailable during nonce consumption"
                )
            export_record = _decode_row(updated_export)
            download_count = int(export_record["download_count"])
            allowed = True
            nonce_consumed = True
            reason = ACCESS_ALLOWED
            nonce_status = "consumed"

    audit = _insert_access_audit(
        cursor,
        action_id=action,
        request_id=request,
        owner_user_id=owner,
        export_id=identifier,
        token_nonce_digest=nonce_digest,
        ip_digest=ip_value_digest,
        allowed=allowed,
        deny_reason="" if allowed else reason,
        nonce_consumed=nonce_consumed,
        download_count_after=download_count,
        request_sha256=request_sha256,
        metadata=safe_metadata,
    )
    return ExportAccessResult(
        audit=audit,
        allowed=allowed,
        reason=reason,
        download_count=download_count,
        nonce_status=nonce_status,
        idempotent=False,
    )


def reserve_export_nonce(
    cursor: CursorLike,
    *,
    owner_user_id: str,
    export_id: str,
    token_nonce: str,
    token_expires_at: datetime | int | float | str,
    reservation_id: str,
    replace_active_reservation: bool = False,
) -> str:
    """Atomically reserve one nonce before downloading its export object.

    Active-reservation replacement is only valid while the caller holds the
    nonce-scoped PostgreSQL session advisory lock.
    """

    owner = _identifier(owner_user_id, "owner_user_id")
    identifier = _identifier(export_id, "export_id")
    reservation = _identifier(reservation_id, "reservation_id")
    nonce_digest = token_nonce_sha256(token_nonce)
    expires_at = _timestamp(token_expires_at, "token_expires_at")

    export_record = _locked_owned_export(cursor, identifier, owner)
    if str(export_record.get("status") or "") != "ready":
        return ACCESS_EXPORT_NOT_READY

    cursor.execute(
        _INSERT_NONCE_SQL,
        (nonce_digest, owner, identifier, expires_at),
    )
    nonce_record = _fetchone_dict(cursor)
    if nonce_record is None:
        cursor.execute(_SELECT_NONCE_FOR_UPDATE_SQL, (nonce_digest,))
        nonce_record = _fetchone_dict(cursor)
    if nonce_record is None:
        raise ProductExportAccessConflict(
            "nonce insert conflicted without a resolvable record"
        )
    nonce_record = _decode_row(nonce_record)
    _validate_nonce_scope(
        nonce_record,
        owner_user_id=owner,
        export_id=identifier,
        expires_at=expires_at,
    )
    if nonce_record.get("consumed_at") is not None:
        return ACCESS_TOKEN_REPLAYED
    if bool(nonce_record.get("expired")):
        return ACCESS_TOKEN_EXPIRED
    if not isinstance(replace_active_reservation, bool):
        raise InvalidProductExportInput(
            "replace_active_reservation must be a boolean"
        )
    replace_active = replace_active_reservation
    if (
        nonce_record.get("reservation_id") is not None
        and not bool(nonce_record.get("reservation_expired"))
        and not replace_active
    ):
        return ACCESS_TOKEN_IN_USE

    cursor.execute(
        _RESERVE_NONCE_SQL,
        (
            reservation,
            nonce_digest,
            owner,
            identifier,
            replace_active,
        ),
    )
    reserved = _fetchone_dict(cursor)
    if reserved is None:
        cursor.execute(_SELECT_NONCE_FOR_UPDATE_SQL, (nonce_digest,))
        refreshed = _fetchone_dict(cursor)
        if refreshed is None:
            raise ProductExportAccessConflict(
                "nonce disappeared during reservation"
            )
        refreshed = _decode_row(refreshed)
        if refreshed.get("consumed_at") is not None:
            return ACCESS_TOKEN_REPLAYED
        if bool(refreshed.get("expired")):
            return ACCESS_TOKEN_EXPIRED
        if (
            refreshed.get("reservation_id") is not None
            and not bool(refreshed.get("reservation_expired"))
        ):
            return ACCESS_TOKEN_IN_USE
        raise ProductExportAccessConflict(
            "nonce reservation failed without a terminal state"
        )
    reserved = _decode_row(reserved)
    if reserved.get("reservation_id") != reservation:
        raise ProductExportAccessConflict(
            "nonce reservation identity mismatch"
        )
    return NONCE_RESERVED


def release_export_nonce_reservation(
    cursor: CursorLike,
    *,
    owner_user_id: str,
    export_id: str,
    token_nonce: str,
    token_expires_at: datetime | int | float | str,
    reservation_id: str,
) -> bool:
    """Release this request's unconsumed reservation after preflight failure."""

    owner = _identifier(owner_user_id, "owner_user_id")
    identifier = _identifier(export_id, "export_id")
    reservation = _identifier(reservation_id, "reservation_id")
    nonce_digest = token_nonce_sha256(token_nonce)
    expires_at = _timestamp(token_expires_at, "token_expires_at")
    cursor.execute(
        _RELEASE_NONCE_RESERVATION_SQL,
        (nonce_digest, owner, identifier, reservation),
    )
    released = _fetchone_dict(cursor)
    if released is not None:
        _validate_nonce_scope(
            _decode_row(released),
            owner_user_id=owner,
            export_id=identifier,
            expires_at=expires_at,
        )
        return True

    cursor.execute(_SELECT_NONCE_FOR_UPDATE_SQL, (nonce_digest,))
    current = _fetchone_dict(cursor)
    if current is None:
        return False
    current = _decode_row(current)
    _validate_nonce_scope(
        current,
        owner_user_id=owner,
        export_id=identifier,
        expires_at=expires_at,
    )
    return False


def export_nonce_preflight_status(
    cursor: CursorLike,
    *,
    owner_user_id: str,
    export_id: str,
    token_nonce: str,
    token_expires_at: datetime | int | float | str,
) -> str:
    owner = _identifier(owner_user_id, "owner_user_id")
    identifier = _identifier(export_id, "export_id")
    nonce_digest = token_nonce_sha256(token_nonce)
    expires_at = _timestamp(token_expires_at, "token_expires_at")
    cursor.execute(_SELECT_NONCE_SQL, (nonce_digest,))
    row = _fetchone_dict(cursor)
    if row is None:
        return NONCE_UNREGISTERED
    record = _decode_row(row)
    _validate_nonce_scope(
        record,
        owner_user_id=owner,
        export_id=identifier,
        expires_at=expires_at,
    )
    if record.get("consumed_at") is not None:
        return ACCESS_TOKEN_REPLAYED
    if bool(record.get("expired")):
        return ACCESS_TOKEN_EXPIRED
    return NONCE_AVAILABLE


def record_export_access_denial(
    cursor: CursorLike,
    *,
    action_id: str,
    request_id: str,
    owner_user_id: str,
    export_id: str,
    ip_address: str,
    digest_secret: str | bytes,
    deny_reason: str,
    token_nonce: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExportAccessResult:
    """Record a pre-consumption denial such as a missing or invalid token."""

    action = _identifier(action_id, "action_id")
    request = _identifier(request_id, "request_id")
    owner = _identifier(owner_user_id, "owner_user_id")
    identifier = _identifier(export_id, "export_id")
    reason = _reason(deny_reason)
    nonce_digest = (
        token_nonce_sha256(token_nonce) if token_nonce is not None else None
    )
    ip_value_digest = ip_hmac_sha256(ip_address, digest_secret)
    safe_metadata = _audit_metadata(
        metadata or {},
        raw_token=token_nonce,
        raw_ip=ip_address,
    )
    request_sha256 = _access_request_sha256(
        action_id=action,
        request_id=request,
        owner_user_id=owner,
        export_id=identifier,
        token_nonce_digest=nonce_digest,
        ip_digest=ip_value_digest,
        token_expires_at=None,
        metadata=safe_metadata,
        operation=f"deny:{reason}",
    )

    export_record = _locked_owned_export(cursor, identifier, owner)
    existing = _existing_access_replay(
        cursor,
        action_id=action,
        request_id=request,
        owner_user_id=owner,
        export_id=identifier,
        request_sha256=request_sha256,
    )
    if existing is not None:
        return _access_result(existing, idempotent=True)

    download_count = int(export_record.get("download_count") or 0)
    audit = _insert_access_audit(
        cursor,
        action_id=action,
        request_id=request,
        owner_user_id=owner,
        export_id=identifier,
        token_nonce_digest=nonce_digest,
        ip_digest=ip_value_digest,
        allowed=False,
        deny_reason=reason,
        nonce_consumed=False,
        download_count_after=download_count,
        request_sha256=request_sha256,
        metadata=safe_metadata,
    )
    return ExportAccessResult(
        audit=audit,
        allowed=False,
        reason=reason,
        download_count=download_count,
        nonce_status="not_consumed",
        idempotent=False,
    )


def release_reservation_and_record_denial(
    cursor: CursorLike,
    *,
    action_id: str,
    request_id: str,
    owner_user_id: str,
    export_id: str,
    token_nonce: str,
    token_expires_at: datetime | int | float | str,
    reservation_id: str,
    ip_address: str,
    digest_secret: str | bytes,
    deny_reason: str,
    metadata: Mapping[str, Any] | None = None,
) -> ExportAccessResult:
    """Release a reserved nonce and persist its denial in one transaction."""

    released = release_export_nonce_reservation(
        cursor,
        owner_user_id=owner_user_id,
        export_id=export_id,
        token_nonce=token_nonce,
        token_expires_at=token_expires_at,
        reservation_id=reservation_id,
    )
    if not released:
        raise ProductExportAccessConflict(
            "nonce reservation could not be released for denial"
        )
    return record_export_access_denial(
        cursor,
        action_id=action_id,
        request_id=request_id,
        owner_user_id=owner_user_id,
        export_id=export_id,
        ip_address=ip_address,
        digest_secret=digest_secret,
        deny_reason=deny_reason,
        token_nonce=token_nonce,
        metadata=metadata,
    )


def get_owned_access_audit(
    cursor: CursorLike,
    *,
    action_id: str,
    owner_user_id: str,
    export_id: str,
) -> dict[str, Any]:
    action = _identifier(action_id, "action_id")
    owner = _identifier(owner_user_id, "owner_user_id")
    identifier = _identifier(export_id, "export_id")
    cursor.execute(_SELECT_OWNED_ACCESS_SQL, (action, owner, identifier))
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductExportNotFound(action)
    return _decode_row(row)


def list_owned_access_audits(
    cursor: CursorLike,
    *,
    owner_user_id: str,
    export_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    owner = _identifier(owner_user_id, "owner_user_id")
    clean_limit = _bounded_limit(limit)
    clean_offset = _nonnegative_int(offset, "offset")
    if export_id is None:
        cursor.execute(
            _LIST_OWNED_ACCESS_SQL,
            (owner, clean_limit, clean_offset),
        )
    else:
        identifier = _identifier(export_id, "export_id")
        cursor.execute(
            _LIST_OWNED_ACCESS_FOR_EXPORT_SQL,
            (owner, identifier, clean_limit, clean_offset),
        )
    return [_decode_row(row) for row in _fetchall_dicts(cursor)]


class ProductExportStore:
    """Transactional wrapper for the caller-owned cursor functions above."""

    def __init__(
        self,
        connection: ConnectionLike,
        *,
        audit_digest_secret: str | bytes | None = None,
    ) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductExportInput(
                "ProductExportStore requires an autocommit-disabled connection"
            )
        self.connection = connection
        self.audit_digest_secret = audit_digest_secret

    def create_or_get(self, **kwargs: Any) -> ExportCreateResult:
        with self._transaction() as cursor:
            result = create_export_package(cursor, **kwargs)
            return ExportCreateResult(
                record=public_export_dto(result.record),
                created=result.created,
            )

    def create_or_get_private_record(
        self,
        **kwargs: Any,
    ) -> ExportCreateResult:
        with self._transaction() as cursor:
            return create_export_package(cursor, **kwargs)

    def get_owned(
        self,
        *,
        export_id: str,
        owner_user_id: str,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            return get_owned_export(
                cursor,
                export_id=export_id,
                owner_user_id=owner_user_id,
            )

    def get_owned_private_record(
        self,
        *,
        export_id: str,
        owner_user_id: str,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            return get_owned_export(
                cursor,
                export_id=export_id,
                owner_user_id=owner_user_id,
                include_private=True,
            )

    def get_owned_by_idempotency(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_owned_export_by_idempotency(
                cursor,
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
            )

    def get_owned_private_record_by_idempotency(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_owned_export_by_idempotency(
                cursor,
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                include_private=True,
            )

    def list_owned(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_owned_exports(cursor, **kwargs)

    def list_owned_private_records(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_owned_exports(
                cursor,
                include_private=True,
                **kwargs,
            )

    def consume_nonce(self, **kwargs: Any) -> ExportAccessResult:
        with self._transaction() as cursor:
            return consume_export_nonce(
                cursor,
                digest_secret=self._audit_secret(),
                **kwargs,
            )

    def reserve_nonce(self, **kwargs: Any) -> str:
        with self._transaction() as cursor:
            return reserve_export_nonce(cursor, **kwargs)

    def release_nonce_reservation(self, **kwargs: Any) -> bool:
        with self._transaction() as cursor:
            return release_export_nonce_reservation(cursor, **kwargs)

    def nonce_preflight_status(self, **kwargs: Any) -> str:
        with self._transaction() as cursor:
            return export_nonce_preflight_status(
                cursor,
                **kwargs,
            )

    def record_denial(self, **kwargs: Any) -> ExportAccessResult:
        with self._transaction() as cursor:
            return record_export_access_denial(
                cursor,
                digest_secret=self._audit_secret(),
                **kwargs,
            )

    def release_and_record_denial(
        self,
        **kwargs: Any,
    ) -> ExportAccessResult:
        with self._transaction() as cursor:
            return release_reservation_and_record_denial(
                cursor,
                digest_secret=self._audit_secret(),
                **kwargs,
            )

    def get_owned_audit(self, **kwargs: Any) -> dict[str, Any]:
        with self._transaction() as cursor:
            return get_owned_access_audit(cursor, **kwargs)

    def list_owned_audits(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_owned_access_audits(cursor, **kwargs)

    def _audit_secret(self) -> str | bytes:
        if self.audit_digest_secret is None:
            raise InvalidProductExportInput(
                "audit_digest_secret is required for access auditing"
            )
        _digest_secret_bytes(self.audit_digest_secret)
        return self.audit_digest_secret

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


def public_export_dto(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in _decode_row(record).items()
        if key
        not in {
            "owner_user_id",
            "idempotency_key",
            "generation_request_sha256",
            "export_request_sha256",
            "manifest_object_ref",
            "manifest_sha256",
            "zip_object_ref",
            "zip_sha256",
            "content_sha256",
        }
    }


def token_nonce_sha256(token_nonce: str) -> str:
    nonce = _secret_text(token_nonce, "token_nonce")
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def ip_hmac_sha256(
    ip_address: str,
    digest_secret: str | bytes,
) -> str:
    raw_ip = _secret_text(ip_address, "ip_address")
    try:
        normalized_ip = ipaddress.ip_address(raw_ip).compressed
    except ValueError:
        normalized_ip = raw_ip.lower()
    return hmac.new(
        _digest_secret_bytes(digest_secret),
        f"product-export-ip-v1\\0{normalized_ip}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalize_export_values(
    *,
    export_id: str,
    owner_user_id: str,
    idempotency_key: str,
    generation_job_id: str,
    generation_request_sha256: str,
    export_request_sha256: str,
    manifest_object_ref: str,
    manifest_sha256: str,
    platforms: Sequence[str],
    watermark: Mapping[str, Any],
    zip_object_ref: str,
    zip_sha256: str,
    zip_size_bytes: int,
    status: str,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": _identifier(export_id, "export_id"),
        "owner_user_id": _identifier(owner_user_id, "owner_user_id"),
        "idempotency_key": _identifier(
            idempotency_key,
            "idempotency_key",
        ),
        "generation_job_id": _identifier(
            generation_job_id,
            "generation_job_id",
        ),
        "generation_request_sha256": _sha256(
            generation_request_sha256,
            "generation_request_sha256",
        ),
        "export_request_sha256": _sha256(
            export_request_sha256,
            "export_request_sha256",
        ),
        "manifest_object_ref": _object_ref(
            manifest_object_ref,
            "manifest_object_ref",
            required_prefix="generated/",
        ),
        "manifest_sha256": _sha256(
            manifest_sha256,
            "manifest_sha256",
        ),
        "platform_set": _platform_set(platforms),
        "watermark": _json_object_value(watermark, "watermark"),
        "zip_object_ref": _object_ref(
            zip_object_ref,
            "zip_object_ref",
            required_prefix="exports/",
        ),
        "zip_sha256": _sha256(zip_sha256, "zip_sha256"),
        "zip_size_bytes": _nonnegative_int(
            zip_size_bytes,
            "zip_size_bytes",
        ),
        "status": _choice(status, EXPORT_STATUSES, "status"),
    }
    values["content_sha256"] = _content_sha256(
        {
            "id": values["id"],
            "ownerUserId": values["owner_user_id"],
            "idempotencyKey": values["idempotency_key"],
            "generationJobId": values["generation_job_id"],
            "generationRequestSha256": values[
                "generation_request_sha256"
            ],
            "exportRequestSha256": values["export_request_sha256"],
            "manifestObjectRef": values["manifest_object_ref"],
            "manifestSha256": values["manifest_sha256"],
            "platformSet": values["platform_set"],
            "watermark": values["watermark"],
            "zipObjectRef": values["zip_object_ref"],
            "zipSha256": values["zip_sha256"],
            "zipSizeBytes": values["zip_size_bytes"],
            "status": values["status"],
        }
    )
    return values


def _same_export_request(
    record: Mapping[str, Any],
    values: Mapping[str, Any],
) -> bool:
    digest_fields = (
        "generation_request_sha256",
        "export_request_sha256",
        "manifest_sha256",
    )
    text_fields = (
        "generation_job_id",
        "manifest_object_ref",
        "status",
    )
    return (
        all(
            hmac.compare_digest(
                str(record.get(field) or ""),
                str(values.get(field) or ""),
            )
            for field in digest_fields
        )
        and all(
            str(record.get(field) or "") == str(values.get(field) or "")
            for field in text_fields
        )
        and _canonical_json(record.get("platform_set") or [])
        == _canonical_json(values.get("platform_set") or [])
        and _canonical_json(record.get("watermark") or {})
        == _canonical_json(values.get("watermark") or {})
    )


def _locked_owned_export(
    cursor: CursorLike,
    export_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    cursor.execute(
        _SELECT_OWNED_EXPORT_FOR_UPDATE_SQL,
        (export_id, owner_user_id),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductExportNotFound(export_id)
    return _decode_row(row)


def _existing_access_replay(
    cursor: CursorLike,
    *,
    action_id: str,
    request_id: str,
    owner_user_id: str,
    export_id: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    cursor.execute(
        _SELECT_ACCESS_BY_ACTION_FOR_UPDATE_SQL,
        (action_id, owner_user_id, export_id),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        cursor.execute(
            _SELECT_ACCESS_BY_REQUEST_FOR_UPDATE_SQL,
            (request_id, owner_user_id, export_id),
        )
        row = _fetchone_dict(cursor)
    if row is None:
        return None

    record = _decode_row(row)
    if (
        str(record.get("action_id") or "") != action_id
        or str(record.get("request_id") or "") != request_id
        or str(record.get("owner_user_id") or "") != owner_user_id
        or str(record.get("export_id") or "") != export_id
        or not hmac.compare_digest(
            str(record.get("request_sha256") or ""),
            request_sha256,
        )
    ):
        raise ProductExportAccessConflict(
            f"export access replay content changed: {action_id}"
        )
    return record


def _insert_access_audit(
    cursor: CursorLike,
    *,
    action_id: str,
    request_id: str,
    owner_user_id: str,
    export_id: str,
    token_nonce_digest: str | None,
    ip_digest: str,
    allowed: bool,
    deny_reason: str,
    nonce_consumed: bool,
    download_count_after: int,
    request_sha256: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    content_sha256 = _content_sha256(
        {
            "actionId": action_id,
            "requestId": request_id,
            "ownerUserId": owner_user_id,
            "exportId": export_id,
            "tokenNonceDigest": token_nonce_digest,
            "ipDigest": ip_digest,
            "allowed": allowed,
            "denyReason": deny_reason,
            "oneTimeRequired": True,
            "nonceConsumed": nonce_consumed,
            "downloadCountAfter": download_count_after,
            "requestSha256": request_sha256,
            "metadata": dict(metadata),
        }
    )
    cursor.execute(
        _INSERT_ACCESS_SQL,
        (
            action_id,
            request_id,
            owner_user_id,
            export_id,
            token_nonce_digest,
            ip_digest,
            allowed,
            deny_reason,
            nonce_consumed,
            download_count_after,
            request_sha256,
            content_sha256,
            _canonical_json(metadata),
        ),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductExportAccessConflict(
            "export access action or request identifier already exists"
        )
    return _decode_row(row)


def _access_result(
    audit: Mapping[str, Any],
    *,
    idempotent: bool,
) -> ExportAccessResult:
    record = _decode_row(audit)
    allowed = bool(record.get("allowed"))
    reason = ACCESS_ALLOWED if allowed else str(record.get("deny_reason") or "")
    nonce_status = (
        "consumed"
        if allowed
        else (
            "replayed"
            if reason == ACCESS_TOKEN_REPLAYED
            else "expired" if reason == ACCESS_TOKEN_EXPIRED else "not_consumed"
        )
    )
    return ExportAccessResult(
        audit=record,
        allowed=allowed,
        reason=reason,
        download_count=int(record.get("download_count_after") or 0),
        nonce_status=nonce_status,
        idempotent=idempotent,
    )


def _validate_nonce_scope(
    record: Mapping[str, Any],
    *,
    owner_user_id: str,
    export_id: str,
    expires_at: datetime,
) -> None:
    if (
        str(record.get("owner_user_id") or "") != owner_user_id
        or str(record.get("export_id") or "") != export_id
        or _timestamp(record.get("expires_at"), "stored expires_at")
        != expires_at
    ):
        raise ProductExportAccessConflict(
            "token nonce was already registered for another scope or expiry"
        )


def _access_request_sha256(
    *,
    action_id: str,
    request_id: str,
    owner_user_id: str,
    export_id: str,
    token_nonce_digest: str | None,
    ip_digest: str,
    token_expires_at: datetime | None,
    metadata: Mapping[str, Any],
    operation: str,
) -> str:
    return _content_sha256(
        {
            "actionId": action_id,
            "requestId": request_id,
            "ownerUserId": owner_user_id,
            "exportId": export_id,
            "tokenNonceDigest": token_nonce_digest,
            "ipDigest": ip_digest,
            "tokenExpiresAt": (
                token_expires_at.isoformat() if token_expires_at else None
            ),
            "metadata": dict(metadata),
            "operation": operation,
        }
    )


def _audit_metadata(
    value: Mapping[str, Any],
    *,
    raw_token: str | None,
    raw_ip: str,
) -> dict[str, Any]:
    metadata = _json_object_value(value, "metadata")

    def inspect(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized_key in _SENSITIVE_AUDIT_KEYS:
                    raise InvalidProductExportInput(
                        f"metadata contains sensitive field: {key}"
                    )
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
        elif isinstance(item, str):
            if raw_token and item == raw_token:
                raise InvalidProductExportInput(
                    "metadata must not contain the raw token nonce"
                )
            if raw_ip and item == raw_ip:
                raise InvalidProductExportInput(
                    "metadata must not contain the raw IP address"
                )

    inspect(metadata)
    return metadata


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
    columns = [
        str(getattr(column, "name", column[0] if column else ""))
        for column in description
    ]
    if not columns or len(columns) != len(row):
        raise ProductExportStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(columns, row))


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in ("platform_set", "watermark", "metadata"):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                decoded[field] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ProductExportStoreError(
                    f"database returned invalid JSON for {field}"
                ) from exc
    return decoded


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductExportInput(f"{field} must be a string")
    text = value.strip()
    if not IDENTIFIER_RE.fullmatch(text):
        raise InvalidProductExportInput(f"{field} is invalid")
    return text


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductExportInput(
            f"{field} must be a lowercase SHA-256 digest"
        )
    digest = value.strip()
    if not SHA256_RE.fullmatch(digest):
        raise InvalidProductExportInput(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return digest


def _object_ref(
    value: Any,
    field: str,
    *,
    required_prefix: str,
) -> str:
    if not isinstance(value, str):
        raise InvalidProductExportInput(f"{field} must be a string")
    reference = value.strip()
    parts = reference.split("/")
    if (
        not OBJECT_REF_RE.fullmatch(reference)
        or reference.startswith("/")
        or not reference.startswith(required_prefix)
        or "\\" in reference
        or "://" in reference
        or "//" in reference
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise InvalidProductExportInput(f"{field} is invalid")
    return reference


def _platform_set(platforms: Sequence[str]) -> list[str]:
    if isinstance(platforms, (str, bytes)) or not isinstance(
        platforms,
        Sequence,
    ):
        raise InvalidProductExportInput("platforms must be a sequence")
    normalized: set[str] = set()
    for platform in platforms:
        if not isinstance(platform, str):
            raise InvalidProductExportInput(
                "platforms must contain strings"
            )
        value = platform.strip().lower()
        if not PLATFORM_RE.fullmatch(value):
            raise InvalidProductExportInput(
                f"invalid export platform: {platform!r}"
            )
        normalized.add(value)
    if not normalized or len(normalized) > 16:
        raise InvalidProductExportInput(
            "platforms must contain between 1 and 16 values"
        )
    return sorted(normalized)


def _json_object_value(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidProductExportInput(f"{field} must be a mapping")
    candidate = dict(value)
    try:
        serialized = _canonical_json(candidate)
        decoded = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidProductExportInput(
            f"{field} must be JSON serializable"
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidProductExportInput(f"{field} is too large")
    return decoded


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(dict(value)).encode("utf-8")
    ).hexdigest()


def _choice(value: Any, choices: frozenset[str], field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductExportInput(f"{field} must be a string")
    normalized = value.strip().lower()
    if normalized not in choices:
        raise InvalidProductExportInput(f"unsupported {field}: {value!r}")
    return normalized


def _reason(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductExportInput("deny_reason must be a string")
    reason = value.strip().lower()
    if (
        not reason
        or len(reason) > 128
        or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", reason)
    ):
        raise InvalidProductExportInput("deny_reason is invalid")
    return reason


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidProductExportInput(
            f"{field} must be a non-negative integer"
        )
    return value


def _bounded_limit(value: Any) -> int:
    number = _nonnegative_int(value, "limit")
    if number < 1 or number > MAX_LIST_LIMIT:
        raise InvalidProductExportInput(
            f"limit must be between 1 and {MAX_LIST_LIMIT}"
        )
    return number


def _secret_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductExportInput(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > 4096:
        raise InvalidProductExportInput(f"{field} is invalid")
    return text


def _digest_secret_bytes(value: str | bytes) -> bytes:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise InvalidProductExportInput(
            "digest_secret must be a string or bytes"
        )
    if len(encoded) < 16:
        raise InvalidProductExportInput(
            "digest_secret must contain at least 16 bytes"
        )
    return encoded


def _timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, bool):
        raise InvalidProductExportInput(f"{field} must be a timestamp")
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, (int, float)):
        try:
            timestamp = datetime.fromtimestamp(value, timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise InvalidProductExportInput(
                f"{field} must be a valid timestamp"
            ) from exc
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidProductExportInput(
                f"{field} must be a valid timestamp"
            ) from exc
    else:
        raise InvalidProductExportInput(f"{field} must be a timestamp")
    if timestamp.tzinfo is None:
        raise InvalidProductExportInput(
            f"{field} must include a timezone"
        )
    return timestamp.astimezone(timezone.utc)


__all__ = [
    "ACCESS_ALLOWED",
    "ACCESS_EXPORT_NOT_READY",
    "ACCESS_TOKEN_EXPIRED",
    "ACCESS_TOKEN_IN_USE",
    "ACCESS_TOKEN_REPLAYED",
    "NONCE_AVAILABLE",
    "NONCE_RESERVED",
    "NONCE_UNREGISTERED",
    "ExportAccessResult",
    "ExportCreateResult",
    "InvalidProductExportInput",
    "ProductExportAccessConflict",
    "ProductExportConflict",
    "ProductExportNotFound",
    "ProductExportStore",
    "ProductExportStoreError",
    "create_export_package",
    "export_nonce_preflight_status",
    "get_owned_access_audit",
    "get_owned_export",
    "get_owned_export_by_idempotency",
    "ip_hmac_sha256",
    "list_owned_access_audits",
    "list_owned_exports",
    "public_export_dto",
    "record_export_access_denial",
    "release_export_nonce_reservation",
    "release_reservation_and_record_denial",
    "reserve_export_nonce",
    "token_nonce_sha256",
    "consume_export_nonce",
]
