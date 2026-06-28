from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import auth_rules


OTP_TTL_SECONDS = 5 * 60
SESSION_TTL_DAYS = 30
REGISTRATION_CONTEXT_WINDOW_HOURS = 24

ERR_INVALID_PHONE = "invalid_phone"
ERR_OTP_RATE_LIMITED = "otp_rate_limited"
ERR_CHALLENGE_NOT_FOUND = "challenge_not_found"
ERR_OTP_EXPIRED = "otp_expired"
ERR_OTP_USED = "otp_used"
ERR_OTP_ATTEMPT_LIMITED = "otp_attempt_limited"
ERR_OTP_MISMATCH = "otp_mismatch"
ERR_USER_NOT_FOUND = "user_not_found"
ERR_STORE_NAME_REQUIRED = "store_name_required"


class AuthError(ValueError):
    """Domain error with a stable code for API adapters."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


AUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_phone
    ON users (phone);

CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_by_user_id TEXT REFERENCES users (id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stores_created_by_user
    ON stores (created_by_user_id);

CREATE TABLE IF NOT EXISTS user_stores (
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    store_id TEXT NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'owner'
        CHECK (role IN ('owner', 'admin', 'member')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, store_id)
);

CREATE INDEX IF NOT EXISTS idx_user_stores_store
    ON user_stores (store_id);

CREATE TABLE IF NOT EXISTS otp_challenges (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    phone_requests_1h INTEGER NOT NULL DEFAULT 0,
    ip_requests_1h INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by_user_id TEXT REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_otp_challenges_phone_created
    ON otp_challenges (phone, created_at);

CREATE INDEX IF NOT EXISTS idx_otp_challenges_ip_created
    ON otp_challenges (ip, created_at);

CREATE INDEX IF NOT EXISTS idx_otp_challenges_expires
    ON otp_challenges (expires_at);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions (user_id);

CREATE INDEX IF NOT EXISTS idx_sessions_token_hash
    ON sessions (token_hash);
"""


def init_auth_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executescript(AUTH_SCHEMA_SQL)
    return conn


def request_otp(
    conn: sqlite3.Connection,
    phone: str,
    ip: str = "",
    user_agent: str = "",
    now: datetime | str | None = None,
) -> dict[str, Any]:
    current = _to_utc_datetime(now)
    normalized_phone = _normalize_phone_or_auth_error(phone)
    previous_phone_requests, previous_ip_requests = _recent_request_counts(
        conn,
        phone=normalized_phone,
        ip=ip,
        now=current,
    )
    rule = auth_rules.evaluate_phone_registration_otp(
        normalized_phone,
        phone_requests_1h=previous_phone_requests,
        ip_requests_1h=previous_ip_requests,
        seconds_since_last_otp=_seconds_since_last_otp(
            conn,
            phone=normalized_phone,
            now=current,
        ),
    )
    if not rule.allowed:
        raise AuthError(ERR_OTP_RATE_LIMITED, "too many OTP requests")

    challenge_id = _new_id("otp")
    code = f"{secrets.randbelow(1_000_000):06d}"
    created_at = _isoformat(current)
    expires_at = _isoformat(current + timedelta(seconds=OTP_TTL_SECONDS))
    phone_requests_1h = previous_phone_requests + 1
    ip_requests_1h = previous_ip_requests + 1 if ip else 0
    metadata = {
        "request_counts": {
            "phone_1h": phone_requests_1h,
            "ip_1h": ip_requests_1h,
        }
    }

    with conn:
        conn.execute(
            """
            INSERT INTO otp_challenges (
                id,
                phone,
                code_hash,
                ip,
                user_agent,
                phone_requests_1h,
                ip_requests_1h,
                metadata_json,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                challenge_id,
                normalized_phone,
                _otp_hash(challenge_id, code),
                ip or "",
                user_agent or "",
                phone_requests_1h,
                ip_requests_1h,
                _json_dumps(metadata),
                created_at,
                expires_at,
            ),
        )

    return {
        "challenge_id": challenge_id,
        "code": code,
        "phone": normalized_phone,
        "expires_at": expires_at,
    }


def verify_otp(
    conn: sqlite3.Connection,
    challenge_id: str,
    code: str,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    current = _to_utc_datetime(now)
    row = conn.execute(
        "SELECT * FROM otp_challenges WHERE id = ?",
        (challenge_id,),
    ).fetchone()
    if row is None:
        raise AuthError(ERR_CHALLENGE_NOT_FOUND, "OTP challenge not found")
    if row["used_at"] is not None:
        raise AuthError(ERR_OTP_USED, "OTP challenge has already been used")
    if current >= _parse_datetime(row["expires_at"]):
        raise AuthError(ERR_OTP_EXPIRED, "OTP challenge has expired")
    if not auth_rules.otp_verification_allowed(int(row["attempts"])):
        raise AuthError(ERR_OTP_ATTEMPT_LIMITED, "too many OTP verification attempts")

    expected_hash = row["code_hash"]
    provided_hash = _otp_hash(challenge_id, str(code))
    if not hmac.compare_digest(expected_hash, provided_hash):
        with conn:
            conn.execute(
                "UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?",
                (challenge_id,),
            )
        raise AuthError(ERR_OTP_MISMATCH, "OTP code does not match")

    user = _get_or_create_user(conn, row["phone"], current)
    token = secrets.token_urlsafe(32)
    session_id = _new_id("ses")
    created_at = _isoformat(current)
    expires_at = _isoformat(current + timedelta(days=SESSION_TTL_DAYS))

    with conn:
        conn.execute(
            """
            UPDATE otp_challenges
            SET used_at = ?, used_by_user_id = ?
            WHERE id = ? AND used_at IS NULL
            """,
            (created_at, user["id"], challenge_id),
        )
        conn.execute(
            """
            INSERT INTO sessions (
                id,
                user_id,
                token_hash,
                created_at,
                expires_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user["id"],
                _token_hash(token),
                created_at,
                expires_at,
                created_at,
            ),
        )
        conn.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (created_at, created_at, user["id"]),
        )

    return {
        "user": get_user(conn, user["id"]),
        "session": {
            "id": session_id,
            "user_id": user["id"],
            "token": token,
            "created_at": created_at,
            "expires_at": expires_at,
        },
    }


def get_session(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    token_hash = _token_hash(token)
    row = conn.execute(
        """
        SELECT
            sessions.*,
            users.phone AS user_phone,
            users.status AS user_status,
            users.metadata_json AS user_metadata_json
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    current = _to_utc_datetime(None)
    if current >= _parse_datetime(row["expires_at"]):
        return None
    seen_at = _isoformat(current)
    with conn:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
            (seen_at, row["id"]),
        )
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_seen_at": seen_at,
        "user": {
            "id": row["user_id"],
            "phone": row["user_phone"],
            "status": row["user_status"],
            "metadata": _json_loads(row["user_metadata_json"], {}),
        },
    }


def logout(conn: sqlite3.Connection, token: str) -> bool:
    token_hash = _token_hash(token)
    revoked_at = _isoformat(_to_utc_datetime(None))
    with conn:
        cursor = conn.execute(
            """
            UPDATE sessions
            SET revoked_at = ?
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (revoked_at, token_hash),
        )
    return cursor.rowcount > 0


def create_store(conn: sqlite3.Connection, user_id: str, name: str) -> dict[str, Any]:
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError(ERR_USER_NOT_FOUND, "user not found")
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise AuthError(ERR_STORE_NAME_REQUIRED, "store name is required")

    store_id = _new_id("store")
    now = _isoformat(_to_utc_datetime(None))
    with conn:
        conn.execute(
            """
            INSERT INTO stores (
                id,
                name,
                created_by_user_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (store_id, normalized_name, user_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO user_stores (user_id, store_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (user_id, store_id, now),
        )
    return _store_from_row(_require_store(conn, store_id))


def list_user_stores(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            stores.*,
            user_stores.role AS role,
            user_stores.created_at AS membership_created_at
        FROM user_stores
        JOIN stores ON stores.id = user_stores.store_id
        WHERE user_stores.user_id = ?
        ORDER BY stores.created_at ASC, stores.id ASC
        """,
        (user_id,),
    ).fetchall()
    return [_store_from_row(row) for row in rows]


def registration_session_context(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_created_at: str,
    session_id: str = "",
    ip: str = "",
    user_agent: str = "",
    now: datetime | str | None = None,
) -> dict[str, Any]:
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError(ERR_USER_NOT_FOUND, "user not found")

    session_created = _to_utc_datetime(session_created_at)
    user_created = _parse_datetime(str(user["created_at"]))
    has_prior_session = _has_prior_session(conn, user_id=user_id, session_id=session_id)
    return {
        "phone_verified": True,
        "human_verified": True,
        "same_phone_registered": user_created != session_created or has_prior_session,
        "same_device_recent_registrations": _recent_registration_count(
            conn,
            user_id=user_id,
            field="user_agent",
            value=user_agent,
            now=now,
        ),
        "same_ip_recent_registrations": _recent_registration_count(
            conn,
            user_id=user_id,
            field="ip",
            value=ip,
            now=now,
        ),
        "risk_blocked": False,
    }


def get_user(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return _user_from_row(row)


def _get_or_create_user(
    conn: sqlite3.Connection,
    phone: str,
    now: datetime,
) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    if existing is not None:
        return _user_from_row(existing)

    user_id = _new_id("usr")
    timestamp = _isoformat(now)
    with conn:
        conn.execute(
            """
            INSERT INTO users (id, phone, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, phone, timestamp, timestamp),
        )
    return get_user(conn, user_id) or {"id": user_id, "phone": phone}


def _recent_request_counts(
    conn: sqlite3.Connection,
    *,
    phone: str,
    ip: str,
    now: datetime,
) -> tuple[int, int]:
    since = _isoformat(now - timedelta(hours=1))
    phone_count = conn.execute(
        """
        SELECT COUNT(*) FROM otp_challenges
        WHERE phone = ? AND created_at >= ?
        """,
        (phone, since),
    ).fetchone()[0]
    ip_count = 0
    if ip:
        ip_count = conn.execute(
            """
            SELECT COUNT(*) FROM otp_challenges
            WHERE ip = ? AND created_at >= ?
            """,
            (ip, since),
        ).fetchone()[0]
    return int(phone_count), int(ip_count)


def _seconds_since_last_otp(
    conn: sqlite3.Connection,
    *,
    phone: str,
    now: datetime,
) -> int | None:
    row = conn.execute(
        """
        SELECT created_at FROM otp_challenges
        WHERE phone = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (phone,),
    ).fetchone()
    if row is None:
        return None
    elapsed = int((_to_utc_datetime(now) - _parse_datetime(row["created_at"])).total_seconds())
    return max(0, elapsed)


def _recent_registration_count(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    field: str,
    value: str,
    now: datetime | str | None,
) -> int:
    if field not in {"ip", "user_agent"}:
        raise ValueError("field must be ip or user_agent")
    if not value:
        return 0

    current = _to_utc_datetime(now)
    since = _isoformat(current - timedelta(hours=REGISTRATION_CONTEXT_WINDOW_HOURS))
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT otp_challenges.used_by_user_id)
        FROM otp_challenges
        JOIN users ON users.id = otp_challenges.used_by_user_id
        WHERE otp_challenges.{field} = ?
          AND otp_challenges.used_by_user_id IS NOT NULL
          AND otp_challenges.used_by_user_id != ?
          AND users.created_at >= ?
        """,
        (value, user_id, since),
    ).fetchone()
    return int(row[0] if row is not None else 0)


def _has_prior_session(conn: sqlite3.Connection, *, user_id: str, session_id: str) -> bool:
    if not session_id:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM sessions
        WHERE user_id = ? AND id != ?
        LIMIT 1
        """,
        (user_id, session_id),
    ).fetchone()
    return row is not None


def _normalize_phone_or_auth_error(phone: str) -> str:
    try:
        return auth_rules.normalize_phone(phone)
    except (TypeError, ValueError) as exc:
        raise AuthError(ERR_INVALID_PHONE, str(exc)) from exc


def _require_store(conn: sqlite3.Connection, store_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
    if row is None:
        raise KeyError(f"store not found: {store_id}")
    return row


def _user_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "phone": row["phone"],
        "status": row["status"],
        "metadata": _json_loads(row["metadata_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def _store_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "created_by_user_id": row["created_by_user_id"],
        "metadata": _json_loads(row["metadata_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if _row_has_key(row, "role"):
        data["role"] = row["role"]
    if _row_has_key(row, "membership_created_at"):
        data["membership_created_at"] = row["membership_created_at"]
    return data


def _row_has_key(row: sqlite3.Row, key: str) -> bool:
    return key in row.keys()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _otp_hash(challenge_id: str, code: str) -> str:
    return hashlib.sha256(f"{challenge_id}:{code}".encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _to_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("now must be a datetime, ISO timestamp, or None")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _parse_datetime(value: str) -> datetime:
    return _to_utc_datetime(value)


def _isoformat(value: datetime) -> str:
    return _to_utc_datetime(value).isoformat()


def _json_dumps(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback
