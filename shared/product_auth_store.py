from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import auth_rules


DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_METADATA_BYTES = 64 * 1024
MAX_STORE_NAME_LENGTH = 120
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    autocommit: bool

    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductAuthStoreError(RuntimeError):
    pass


class InvalidProductAuthInput(ProductAuthStoreError):
    pass


class ProductAuthConfigurationError(ProductAuthStoreError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AuthUserUnavailable(ProductAuthStoreError):
    pass


@dataclass(frozen=True)
class UserCreateResult:
    user: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class SessionIssueResult:
    session: dict[str, Any]
    user: dict[str, Any]
    token: str


@dataclass(frozen=True)
class SessionRevokeResult:
    found: bool
    revoked: bool
    idempotent: bool
    session: dict[str, Any] | None


_USER_COLUMNS = """
    id, phone, status, metadata, created_at, updated_at, last_login_at
"""

_SESSION_COLUMNS = """
    id, user_id, token_hash, created_at, expires_at, last_seen_at, revoked_at
"""

_STORE_COLUMNS = """
    id, name, status, created_by_user_id, metadata, created_at, updated_at
"""

_INSERT_USER_SQL = f"""
/* product_auth_store:create_user */
INSERT INTO product_users (
    id, phone, metadata
) VALUES (
    %s, %s, %s::jsonb
)
ON CONFLICT (phone) DO NOTHING
RETURNING {_USER_COLUMNS}
"""

_SELECT_USER_BY_PHONE_SQL = f"""
/* product_auth_store:select_user_by_phone */
SELECT {_USER_COLUMNS}
FROM product_users
WHERE phone = %s
"""

_SELECT_USER_BY_ID_SQL = f"""
/* product_auth_store:select_user_by_id */
SELECT {_USER_COLUMNS}
FROM product_users
WHERE id = %s
"""

_SELECT_ACTIVE_USER_FOR_SESSION_SQL = f"""
/* product_auth_store:select_active_user_for_session */
SELECT {_USER_COLUMNS}
FROM product_users
WHERE id = %s AND status = 'active'
FOR KEY SHARE
"""

_INSERT_SESSION_SQL = f"""
/* product_auth_store:create_session */
INSERT INTO product_auth_sessions (
    id, user_id, token_hash, expires_at
) VALUES (
    %s,
    %s,
    %s,
    CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
)
RETURNING {_SESSION_COLUMNS}
"""

_INSERT_REGISTRATION_SECURITY_EVENT_SQL = """
/* product_auth_store:create_registration_security_event */
INSERT INTO product_registration_security_events (
    id, user_id, session_id, is_new_user, ip_hash, device_hash
) VALUES (
    %s, %s, %s, %s, %s, %s
)
RETURNING id
"""

_SELECT_REGISTRATION_SECURITY_CONTEXT_SQL = """
/* product_auth_store:select_registration_security_context */
SELECT
    events.is_new_user,
    events.ip_hash,
    events.device_hash,
    (
        SELECT COUNT(DISTINCT recent.user_id)
        FROM product_registration_security_events AS recent
        WHERE events.ip_hash IS NOT NULL
          AND recent.ip_hash = events.ip_hash
          AND recent.is_new_user
          AND recent.user_id != events.user_id
          AND recent.created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
    ) AS same_ip_recent_registrations,
    (
        SELECT COUNT(DISTINCT recent.user_id)
        FROM product_registration_security_events AS recent
        WHERE events.device_hash IS NOT NULL
          AND recent.device_hash = events.device_hash
          AND recent.is_new_user
          AND recent.user_id != events.user_id
          AND recent.created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
    ) AS same_device_recent_registrations
FROM product_registration_security_events AS events
WHERE events.user_id = %s AND events.session_id = %s
"""

_TOUCH_USER_LOGIN_SQL = f"""
/* product_auth_store:touch_user_login */
UPDATE product_users
SET last_login_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s AND status = 'active'
RETURNING {_USER_COLUMNS}
"""

_RESOLVE_SESSION_SQL = """
/* product_auth_store:resolve_session */
UPDATE product_auth_sessions AS sessions
SET last_seen_at = CURRENT_TIMESTAMP
FROM product_users AS users
WHERE sessions.token_hash = %s
  AND sessions.user_id = users.id
  AND sessions.revoked_at IS NULL
  AND sessions.expires_at > CURRENT_TIMESTAMP
  AND users.status = 'active'
RETURNING
    sessions.id,
    sessions.user_id,
    sessions.created_at,
    sessions.expires_at,
    sessions.last_seen_at,
    sessions.revoked_at,
    users.phone AS user_phone,
    users.status AS user_status,
    users.metadata AS user_metadata,
    users.created_at AS user_created_at,
    users.updated_at AS user_updated_at,
    users.last_login_at AS user_last_login_at
"""

_REVOKE_ACTIVE_SESSION_SQL = f"""
/* product_auth_store:revoke_active_session */
UPDATE product_auth_sessions
SET revoked_at = CURRENT_TIMESTAMP
WHERE token_hash = %s AND revoked_at IS NULL
RETURNING {_SESSION_COLUMNS}
"""

_SELECT_SESSION_BY_HASH_SQL = f"""
/* product_auth_store:select_session_by_hash */
SELECT {_SESSION_COLUMNS}
FROM product_auth_sessions
WHERE token_hash = %s
"""

_INSERT_STORE_SQL = f"""
/* product_auth_store:create_store */
INSERT INTO product_stores (
    id, name, created_by_user_id, metadata
) VALUES (
    %s, %s, %s, %s::jsonb
)
RETURNING {_STORE_COLUMNS}
"""

_INSERT_STORE_MEMBERSHIP_SQL = """
/* product_auth_store:create_store_membership */
INSERT INTO product_user_stores (
    user_id, store_id, role
) VALUES (
    %s, %s, 'owner'
)
RETURNING role, created_at AS membership_created_at
"""

_LIST_USER_STORES_SQL = f"""
/* product_auth_store:list_user_stores */
SELECT
    {", ".join(f"stores.{column.strip()}" for column in _STORE_COLUMNS.split(","))},
    memberships.role,
    memberships.created_at AS membership_created_at
FROM product_user_stores AS memberships
JOIN product_stores AS stores ON stores.id = memberships.store_id
WHERE memberships.user_id = %s
ORDER BY stores.created_at ASC, stores.id ASC
"""


class ProductAuthStore:
    """PostgreSQL user and session persistence over an injected connection.

    The raw session token is returned exactly once and never appears in SQL
    parameters. PostgreSQL stores only an HMAC-SHA-256 digest. Callers own the
    connection lifecycle and schema migration.
    """

    def __init__(
        self,
        connection: ConnectionLike,
        *,
        token_hash_secret: str | bytes,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        id_factory: Callable[[str], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductAuthInput(
                "ProductAuthStore requires an autocommit-disabled connection"
            )
        self.connection = connection
        self._token_hash_secret = _secret_bytes(
            token_hash_secret,
            "token_hash_secret",
        )
        self.session_ttl_seconds = _bounded_int(
            session_ttl_seconds,
            "session_ttl_seconds",
            minimum=60,
            maximum=90 * 24 * 60 * 60,
        )
        self._id_factory = id_factory or _new_id
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def get_or_create_user(
        self,
        *,
        phone: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> UserCreateResult:
        normalized_phone = _normalize_phone(phone)
        metadata_json = _json_mapping(metadata or {}, "metadata")
        candidate_id = _identifier(self._id_factory("usr"), "generated_user_id")

        with self._transaction() as cursor:
            cursor.execute(
                _INSERT_USER_SQL,
                (candidate_id, normalized_phone, metadata_json),
            )
            inserted = _fetchone_dict(cursor)
            if inserted is not None:
                return UserCreateResult(
                    user=_decode_user(inserted),
                    created=True,
                )

            cursor.execute(_SELECT_USER_BY_PHONE_SQL, (normalized_phone,))
            existing = _fetchone_dict(cursor)
            if existing is None:
                raise ProductAuthStoreError(
                    "phone conflict did not resolve to an existing user"
                )
            return UserCreateResult(
                user=_decode_user(existing),
                created=False,
            )

    def get_user(self, *, user_id: str) -> dict[str, Any] | None:
        identifier = _identifier(user_id, "user_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_USER_BY_ID_SQL, (identifier,))
            row = _fetchone_dict(cursor)
            return _decode_user(row) if row is not None else None

    def create_store(
        self,
        *,
        user_id: str,
        name: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identifier = _identifier(user_id, "user_id")
        normalized_name = _store_name(name)
        metadata_json = _json_mapping(metadata or {}, "metadata")
        store_id = _identifier(self._id_factory("store"), "generated_store_id")

        with self._transaction() as cursor:
            cursor.execute(_SELECT_ACTIVE_USER_FOR_SESSION_SQL, (identifier,))
            if _fetchone_dict(cursor) is None:
                raise AuthUserUnavailable(identifier)

            cursor.execute(
                _INSERT_STORE_SQL,
                (store_id, normalized_name, identifier, metadata_json),
            )
            store_row = _fetchone_dict(cursor)
            if store_row is None:
                raise ProductAuthStoreError(
                    "store insert did not return a record"
                )

            cursor.execute(
                _INSERT_STORE_MEMBERSHIP_SQL,
                (identifier, store_id),
            )
            membership_row = _fetchone_dict(cursor)
            if membership_row is None:
                raise ProductAuthStoreError(
                    "store membership insert did not return a record"
                )

        return _decode_store({**store_row, **membership_row})

    def list_user_stores(self, *, user_id: str) -> list[dict[str, Any]]:
        identifier = _identifier(user_id, "user_id")
        with self._transaction() as cursor:
            cursor.execute(_LIST_USER_STORES_SQL, (identifier,))
            return [
                _decode_store(row)
                for row in _fetchall_dicts(cursor)
            ]

    def issue_session(
        self,
        *,
        user_id: str,
        registration_context: Mapping[str, Any] | None = None,
    ) -> SessionIssueResult:
        identifier = _identifier(user_id, "user_id")
        session_id = _identifier(self._id_factory("ses"), "generated_session_id")
        token = _session_token(self._token_factory())
        token_hash = self._token_hash(token)
        security_event: tuple[str, bool, str | None, str | None] | None = None
        if registration_context is not None:
            if not isinstance(registration_context, Mapping):
                raise InvalidProductAuthInput(
                    "registration_context must be a mapping"
                )
            is_new_user = registration_context.get("is_new_user")
            if not isinstance(is_new_user, bool):
                raise InvalidProductAuthInput(
                    "registration_context.is_new_user must be a boolean"
                )
            security_event = (
                _identifier(
                    self._id_factory("regctx"),
                    "generated_registration_context_id",
                ),
                is_new_user,
                self._context_hash(
                    "registration-ip",
                    registration_context.get("ip"),
                ),
                self._context_hash(
                    "registration-device",
                    registration_context.get("user_agent"),
                ),
            )

        with self._transaction() as cursor:
            cursor.execute(_SELECT_ACTIVE_USER_FOR_SESSION_SQL, (identifier,))
            user_row = _fetchone_dict(cursor)
            if user_row is None:
                raise AuthUserUnavailable(identifier)

            cursor.execute(
                _INSERT_SESSION_SQL,
                (
                    session_id,
                    identifier,
                    token_hash,
                    self.session_ttl_seconds,
                ),
            )
            session_row = _fetchone_dict(cursor)
            if session_row is None:
                raise ProductAuthStoreError(
                    "session insert did not return a record"
                )

            if security_event is not None:
                event_id, is_new_user, ip_hash, device_hash = security_event
                cursor.execute(
                    _INSERT_REGISTRATION_SECURITY_EVENT_SQL,
                    (
                        event_id,
                        identifier,
                        session_id,
                        is_new_user,
                        ip_hash,
                        device_hash,
                    ),
                )
                if _fetchone_dict(cursor) is None:
                    raise ProductAuthStoreError(
                        "registration security event insert did not return a record"
                    )

            cursor.execute(_TOUCH_USER_LOGIN_SQL, (identifier,))
            touched_user = _fetchone_dict(cursor)
            if touched_user is None:
                raise AuthUserUnavailable(identifier)
            user_row = touched_user

        return SessionIssueResult(
            session=_decode_session(session_row),
            user=_decode_user(user_row),
            token=token,
        )

    def registration_session_context(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        identifier = _identifier(user_id, "user_id")
        clean_session_id = _identifier(session_id, "session_id")
        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_REGISTRATION_SECURITY_CONTEXT_SQL,
                (identifier, clean_session_id),
            )
            row = _fetchone_dict(cursor)
        if row is None:
            return {
                "phone_verified": True,
                "human_verified": True,
                "same_phone_registered": True,
                "same_device_recent_registrations": 0,
                "same_ip_recent_registrations": 0,
                "risk_blocked": True,
            }
        return {
            "phone_verified": True,
            "human_verified": True,
            "same_phone_registered": not bool(row.get("is_new_user")),
            "same_device_recent_registrations": max(
                int(row.get("same_device_recent_registrations") or 0),
                0,
            ),
            "same_ip_recent_registrations": max(
                int(row.get("same_ip_recent_registrations") or 0),
                0,
            ),
            "risk_blocked": False,
        }

    def resolve_session(self, *, token: str) -> dict[str, Any] | None:
        token_hash = self._token_hash(_session_token(token))
        with self._transaction() as cursor:
            cursor.execute(_RESOLVE_SESSION_SQL, (token_hash,))
            row = _fetchone_dict(cursor)
            if row is None:
                return None
            return _decode_resolved_session(row)

    def revoke_session(self, *, token: str) -> SessionRevokeResult:
        token_hash = self._token_hash(_session_token(token))
        with self._transaction() as cursor:
            cursor.execute(_REVOKE_ACTIVE_SESSION_SQL, (token_hash,))
            revoked = _fetchone_dict(cursor)
            if revoked is not None:
                return SessionRevokeResult(
                    found=True,
                    revoked=True,
                    idempotent=False,
                    session=_decode_session(revoked),
                )

            cursor.execute(_SELECT_SESSION_BY_HASH_SQL, (token_hash,))
            existing = _fetchone_dict(cursor)
            if existing is None:
                return SessionRevokeResult(
                    found=False,
                    revoked=False,
                    idempotent=False,
                    session=None,
                )
            return SessionRevokeResult(
                found=True,
                revoked=False,
                idempotent=True,
                session=_decode_session(existing),
            )

    def _token_hash(self, token: str) -> str:
        return hmac.new(
            self._token_hash_secret,
            b"product-session:v1:" + token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _context_hash(self, domain: str, value: Any) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        return hmac.new(
            self._token_hash_secret,
            f"product-auth:{domain}:v1:".encode("ascii")
            + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @contextmanager
    def _transaction(self) -> Iterator[CursorLike]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        except BaseException:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def product_auth_store_from_env(
    connection: ConnectionLike,
    env: Mapping[str, str] | None = None,
    *,
    id_factory: Callable[[str], str] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> ProductAuthStore:
    """Build the production store and reject incomplete configuration."""

    values = os.environ if env is None else env
    database_url = str(values.get("DATABASE_URL") or "").strip()
    if urlsplit(database_url).scheme.lower() not in {"postgres", "postgresql"}:
        raise ProductAuthConfigurationError("auth_postgres_database_url_required")
    secret = values.get("AUTH_SESSION_HASH_SECRET")
    try:
        token_hash_secret = _secret_bytes(secret, "AUTH_SESSION_HASH_SECRET")
    except InvalidProductAuthInput as exc:
        raise ProductAuthConfigurationError(
            "auth_session_hash_secret_required"
        ) from exc
    try:
        ttl_seconds = _bounded_int(
            values.get(
                "AUTH_SESSION_TTL_SECONDS",
                DEFAULT_SESSION_TTL_SECONDS,
            ),
            "AUTH_SESSION_TTL_SECONDS",
            minimum=60,
            maximum=90 * 24 * 60 * 60,
        )
    except InvalidProductAuthInput as exc:
        raise ProductAuthConfigurationError(
            "auth_session_ttl_invalid"
        ) from exc
    return ProductAuthStore(
        connection,
        token_hash_secret=token_hash_secret,
        session_ttl_seconds=ttl_seconds,
        id_factory=id_factory,
        token_factory=token_factory,
    )


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    names = [
        str(column[0] if isinstance(column, Sequence) else column.name)
        for column in description
    ]
    if not names or len(names) != len(row):
        raise ProductAuthStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(names, row))


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while True:
        row = _fetchone_dict(cursor)
        if row is None:
            return rows
        rows.append(row)


def _decode_user(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return {
        "id": str(row.get("id") or ""),
        "phone": str(row.get("phone") or ""),
        "status": str(row.get("status") or ""),
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        "created_at": _timestamp(row.get("created_at")),
        "updated_at": _timestamp(row.get("updated_at")),
        "last_login_at": _timestamp(row.get("last_login_at")),
    }


def _decode_session(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "user_id": str(row.get("user_id") or ""),
        "created_at": _timestamp(row.get("created_at")),
        "expires_at": _timestamp(row.get("expires_at")),
        "last_seen_at": _timestamp(row.get("last_seen_at")),
        "revoked_at": _timestamp(row.get("revoked_at")),
    }


def _decode_store(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    payload = {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "status": str(row.get("status") or ""),
        "created_by_user_id": str(row.get("created_by_user_id") or ""),
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        "created_at": _timestamp(row.get("created_at")),
        "updated_at": _timestamp(row.get("updated_at")),
    }
    if row.get("role") is not None:
        payload["role"] = str(row["role"])
    if row.get("membership_created_at") is not None:
        payload["membership_created_at"] = _timestamp(
            row["membership_created_at"]
        )
    return payload


def _decode_resolved_session(row: Mapping[str, Any]) -> dict[str, Any]:
    session = _decode_session(row)
    user_metadata = row.get("user_metadata")
    if isinstance(user_metadata, str):
        try:
            user_metadata = json.loads(user_metadata)
        except json.JSONDecodeError:
            user_metadata = {}
    session["user"] = {
        "id": session["user_id"],
        "phone": str(row.get("user_phone") or ""),
        "status": str(row.get("user_status") or ""),
        "metadata": (
            dict(user_metadata) if isinstance(user_metadata, Mapping) else {}
        ),
        "created_at": _timestamp(row.get("user_created_at")),
        "updated_at": _timestamp(row.get("user_updated_at")),
        "last_login_at": _timestamp(row.get("user_last_login_at")),
    }
    return session


def _normalize_phone(phone: Any) -> str:
    try:
        return auth_rules.normalize_phone(phone)
    except (TypeError, ValueError) as exc:
        raise InvalidProductAuthInput("phone is invalid") from exc


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductAuthInput(f"{field} must be a string")
    if value != value.strip() or not IDENTIFIER_RE.fullmatch(value):
        raise InvalidProductAuthInput(f"{field} has an invalid format")
    return value


def _session_token(value: Any) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise InvalidProductAuthInput("session token has an invalid format")
    return value


def _store_name(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductAuthInput("store name must be a string")
    normalized = value.strip()
    if not normalized:
        raise InvalidProductAuthInput("store name is required")
    if len(normalized) > MAX_STORE_NAME_LENGTH:
        raise InvalidProductAuthInput("store name is too long")
    return normalized


def _secret_bytes(value: Any, field: str) -> bytes:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise InvalidProductAuthInput(f"{field} must be configured")
    if len(encoded) < 32 or len(encoded) > 4096:
        raise InvalidProductAuthInput(
            f"{field} must contain between 32 and 4096 bytes"
        )
    return bytes(encoded)


def _json_mapping(value: Any, field: str) -> str:
    if not isinstance(value, Mapping):
        raise InvalidProductAuthInput(f"{field} must be a mapping")
    try:
        serialized = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidProductAuthInput(
            f"{field} must be JSON serializable"
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidProductAuthInput(f"{field} is too large")
    return serialized


def _bounded_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise InvalidProductAuthInput(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductAuthInput(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise InvalidProductAuthInput(f"{field} is out of range")
    return number


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"
