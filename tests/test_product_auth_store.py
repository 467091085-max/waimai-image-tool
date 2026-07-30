from __future__ import annotations

import hashlib
import hmac
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from shared.product_auth_store import (
    AuthUserUnavailable,
    InvalidProductAuthInput,
    ProductAuthConfigurationError,
    ProductAuthStore,
    product_auth_store_from_env,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "005_product_auth_postgres.sql"
SECRET = "session-test-secret-" + ("s" * 32)
RAW_TOKEN = "A" * 43


@dataclass
class SqlResponse:
    rows: list[dict[str, Any]]
    rowcount: int | None = None


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.rows: list[dict[str, Any]] = []
        self.closed = False

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] = (),
    ) -> None:
        match = re.search(
            r"/\* product_auth_store:([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        name = match.group(1)
        self.connection.calls.append((name, operation, tuple(parameters)))
        responses = self.connection.responses.get(name)
        if not responses:
            raise AssertionError(f"unexpected or exhausted SQL operation: {name}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, int):
            self.rows = []
            self.rowcount = response
            return
        if isinstance(response, SqlResponse):
            self.rows = list(response.rows)
            self.rowcount = (
                len(self.rows)
                if response.rowcount is None
                else response.rowcount
            )
            return
        self.rows = list(response)
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    autocommit = False

    def __init__(self, **responses: list[Any]) -> None:
        self.responses = {name: list(items) for name, items in responses.items()}
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.cursors: list[ScriptedCursor] = []

    def cursor(self) -> ScriptedCursor:
        cursor = ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class AutocommitConnection(ScriptedConnection):
    autocommit = True


def user_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "usr_stable",
        "phone": "+8613800138000",
        "status": "active",
        "metadata": {"source": "otp"},
        "created_at": "2026-07-30T08:00:00+00:00",
        "updated_at": "2026-07-30T08:00:00+00:00",
        "last_login_at": None,
    }
    row.update(overrides)
    return row


def session_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "ses_stable",
        "user_id": "usr_stable",
        "token_hash": "a" * 64,
        "created_at": "2026-07-30T08:01:00+00:00",
        "expires_at": "2026-08-29T08:01:00+00:00",
        "last_seen_at": "2026-07-30T08:01:00+00:00",
        "revoked_at": None,
    }
    row.update(overrides)
    return row


def store_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "store_stable",
        "name": "测试门店",
        "status": "active",
        "created_by_user_id": "usr_stable",
        "metadata": {"source": "customer"},
        "created_at": "2026-07-30T08:00:00+00:00",
        "updated_at": "2026-07-30T08:00:00+00:00",
        "role": "owner",
        "membership_created_at": "2026-07-30T08:00:00+00:00",
    }
    row.update(overrides)
    return row


def resolved_session_row(**overrides: Any) -> dict[str, Any]:
    row = {
        **session_row(),
        "user_phone": "+8613800138000",
        "user_status": "active",
        "user_metadata": {"source": "otp"},
        "user_created_at": "2026-07-30T08:00:00+00:00",
        "user_updated_at": "2026-07-30T08:01:00+00:00",
        "user_last_login_at": "2026-07-30T08:01:00+00:00",
    }
    row.update(overrides)
    return row


def store(
    connection: ScriptedConnection,
    *,
    secret: str = SECRET,
) -> ProductAuthStore:
    return ProductAuthStore(
        connection,
        token_hash_secret=secret,
        id_factory=lambda prefix: f"{prefix}_stable",
        token_factory=lambda: RAW_TOKEN,
    )


def expected_token_hash(secret: str = SECRET) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        b"product-session:v1:" + RAW_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_migration_has_stable_user_and_hash_only_session_schema() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS product_users" in sql
    assert "phone TEXT NOT NULL UNIQUE" in sql
    assert "CREATE TABLE IF NOT EXISTS product_auth_sessions" in sql
    assert "token_hash TEXT NOT NULL UNIQUE" in sql
    assert "REFERENCES product_users (id) ON DELETE CASCADE" in sql
    assert "CREATE TABLE IF NOT EXISTS product_stores" in sql
    assert "CREATE TABLE IF NOT EXISTS product_user_stores" in sql
    assert "PRIMARY KEY (user_id, store_id)" in sql
    assert (
        "CREATE TABLE IF NOT EXISTS product_registration_security_events"
        in sql
    )
    assert "ip_hash TEXT" in sql
    assert "device_hash TEXT" in sql
    assert "expires_at > created_at" in sql
    assert re.search(r"\btoken\s+TEXT\b", sql, re.IGNORECASE) is None
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


def test_store_requires_autocommit_disabled_and_strong_secret() -> None:
    with pytest.raises(InvalidProductAuthInput, match="autocommit-disabled"):
        ProductAuthStore(
            AutocommitConnection(),
            token_hash_secret=SECRET,
        )
    with pytest.raises(InvalidProductAuthInput, match="32"):
        ProductAuthStore(
            ScriptedConnection(),
            token_hash_secret="too-short",
        )


def test_get_or_create_user_inserts_normalized_phone() -> None:
    connection = ScriptedConnection(create_user=[[user_row()]])

    result = store(connection).get_or_create_user(
        phone="138 0013 8000",
        metadata={"source": "otp"},
    )

    assert result.created is True
    assert result.user["id"] == "usr_stable"
    assert result.user["phone"] == "+8613800138000"
    assert connection.commits == 1
    assert connection.rollbacks == 0
    name, sql, parameters = connection.calls[0]
    assert name == "create_user"
    assert "ON CONFLICT (phone) DO NOTHING" in sql
    assert parameters[1] == "+8613800138000"


def test_same_phone_conflict_returns_existing_stable_user() -> None:
    connection = ScriptedConnection(
        create_user=[[]],
        select_user_by_phone=[[user_row(id="usr_original")]],
    )

    result = store(connection).get_or_create_user(phone="+8613800138000")

    assert result.created is False
    assert result.user["id"] == "usr_original"
    assert [call[0] for call in connection.calls] == [
        "create_user",
        "select_user_by_phone",
    ]
    assert connection.commits == 1


def test_invalid_phone_fails_before_database_access() -> None:
    connection = ScriptedConnection()

    with pytest.raises(InvalidProductAuthInput, match="phone"):
        store(connection).get_or_create_user(phone="123")

    assert connection.calls == []


def test_create_store_and_membership_are_one_transaction() -> None:
    connection = ScriptedConnection(
        select_active_user_for_session=[[user_row()]],
        create_store=[
            [
                {
                    key: value
                    for key, value in store_row().items()
                    if key not in {"role", "membership_created_at"}
                }
            ]
        ],
        create_store_membership=[
            [
                {
                    "role": "owner",
                    "membership_created_at":
                        "2026-07-30T08:00:00+00:00",
                }
            ]
        ],
    )

    result = store(connection).create_store(
        user_id="usr_stable",
        name="  测试门店  ",
        metadata={"source": "customer"},
    )

    assert result == store_row()
    assert [call[0] for call in connection.calls] == [
        "select_active_user_for_session",
        "create_store",
        "create_store_membership",
    ]
    assert connection.calls[1][2][1] == "测试门店"
    assert connection.commits == 1


def test_create_store_rejects_blank_name_before_database_access() -> None:
    connection = ScriptedConnection()

    with pytest.raises(InvalidProductAuthInput, match="required"):
        store(connection).create_store(
            user_id="usr_stable",
            name="   ",
        )

    assert connection.calls == []


def test_list_user_stores_is_owner_scoped() -> None:
    connection = ScriptedConnection(
        list_user_stores=[
            [
                store_row(),
                store_row(
                    id="store_second",
                    name="第二门店",
                    role="admin",
                ),
            ]
        ]
    )

    result = store(connection).list_user_stores(user_id="usr_stable")

    assert [item["id"] for item in result] == [
        "store_stable",
        "store_second",
    ]
    assert result[1]["role"] == "admin"
    name, sql, parameters = connection.calls[0]
    assert name == "list_user_stores"
    assert "WHERE memberships.user_id = %s" in sql
    assert parameters == ("usr_stable",)


def test_issue_session_never_sends_raw_token_to_postgres() -> None:
    returned_session = session_row(token_hash=expected_token_hash())
    connection = ScriptedConnection(
        select_active_user_for_session=[[user_row()]],
        create_session=[[returned_session]],
        touch_user_login=[
            [
                user_row(
                    updated_at="2026-07-30T08:01:00+00:00",
                    last_login_at="2026-07-30T08:01:00+00:00",
                )
            ]
        ],
    )

    result = store(connection).issue_session(user_id="usr_stable")

    assert result.token == RAW_TOKEN
    assert "token_hash" not in result.session
    assert result.session["id"] == "ses_stable"
    assert result.user["phone"] == "+8613800138000"
    assert result.user["last_login_at"] == "2026-07-30T08:01:00+00:00"
    create_call = next(
        call for call in connection.calls if call[0] == "create_session"
    )
    assert create_call[2][2] == expected_token_hash()
    assert RAW_TOKEN not in create_call[1]
    assert all(RAW_TOKEN != str(value) for value in create_call[2])
    assert connection.commits == 1


def test_issue_session_rejects_missing_or_disabled_user_atomically() -> None:
    connection = ScriptedConnection(select_active_user_for_session=[[]])

    with pytest.raises(AuthUserUnavailable, match="usr_stable"):
        store(connection).issue_session(user_id="usr_stable")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_issue_session_persists_hash_only_registration_context() -> None:
    returned_session = session_row(token_hash=expected_token_hash())
    connection = ScriptedConnection(
        select_active_user_for_session=[[user_row()]],
        create_session=[[returned_session]],
        create_registration_security_event=[[{"id": "regctx_stable"}]],
        touch_user_login=[
            [
                user_row(
                    updated_at="2026-07-30T08:01:00+00:00",
                    last_login_at="2026-07-30T08:01:00+00:00",
                )
            ]
        ],
    )

    store(connection).issue_session(
        user_id="usr_stable",
        registration_context={
            "is_new_user": True,
            "ip": "203.0.113.7",
            "user_agent": "Example Browser/1.0",
        },
    )

    call = next(
        item
        for item in connection.calls
        if item[0] == "create_registration_security_event"
    )
    assert call[2][:4] == (
        "regctx_stable",
        "usr_stable",
        "ses_stable",
        True,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", str(call[2][4]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(call[2][5]))
    assert "203.0.113.7" not in call[1]
    assert "Example Browser/1.0" not in call[1]
    assert all("203.0.113.7" not in str(value) for value in call[2])
    assert all("Example Browser/1.0" not in str(value) for value in call[2])
    assert connection.commits == 1


def test_registration_context_uses_durable_counts_and_new_user_flag() -> None:
    connection = ScriptedConnection(
        select_registration_security_context=[
            [
                {
                    "is_new_user": True,
                    "ip_hash": "a" * 64,
                    "device_hash": "b" * 64,
                    "same_ip_recent_registrations": 2,
                    "same_device_recent_registrations": 1,
                }
            ]
        ]
    )

    context = store(connection).registration_session_context(
        user_id="usr_stable",
        session_id="ses_stable",
    )

    assert context == {
        "phone_verified": True,
        "human_verified": True,
        "same_phone_registered": False,
        "same_device_recent_registrations": 1,
        "same_ip_recent_registrations": 2,
        "risk_blocked": False,
    }
    assert connection.calls[0][2] == ("usr_stable", "ses_stable")


def test_missing_registration_context_fails_reward_closed() -> None:
    connection = ScriptedConnection(
        select_registration_security_context=[[]]
    )

    context = store(connection).registration_session_context(
        user_id="usr_stable",
        session_id="ses_stable",
    )

    assert context["same_phone_registered"] is True
    assert context["risk_blocked"] is True


def test_session_resolves_across_store_instances_with_same_secret() -> None:
    connection_a = ScriptedConnection(
        resolve_session=[[resolved_session_row()]],
    )
    connection_b = ScriptedConnection(
        resolve_session=[[resolved_session_row()]],
    )

    first = store(connection_a).resolve_session(token=RAW_TOKEN)
    second = store(connection_b).resolve_session(token=RAW_TOKEN)

    assert first == second
    assert first is not None
    assert first["user"]["id"] == "usr_stable"
    assert connection_a.calls[0][2] == (expected_token_hash(),)
    assert connection_b.calls[0][2] == (expected_token_hash(),)


def test_resolve_session_filters_expired_revoked_and_disabled_rows() -> None:
    connection = ScriptedConnection(resolve_session=[[]])

    result = store(connection).resolve_session(token=RAW_TOKEN)

    assert result is None
    _, sql, _parameters = connection.calls[0]
    assert "sessions.revoked_at IS NULL" in sql
    assert "sessions.expires_at > CURRENT_TIMESTAMP" in sql
    assert "users.status = 'active'" in sql


def test_different_hash_secret_cannot_resolve_same_raw_token() -> None:
    first_connection = ScriptedConnection(resolve_session=[[]])
    second_connection = ScriptedConnection(resolve_session=[[]])

    store(first_connection, secret=SECRET).resolve_session(token=RAW_TOKEN)
    store(
        second_connection,
        secret="different-session-secret-" + ("d" * 32),
    ).resolve_session(token=RAW_TOKEN)

    assert first_connection.calls[0][2][0] != second_connection.calls[0][2][0]


def test_revoke_session_is_idempotent_and_does_not_expose_hash() -> None:
    revoked_row = session_row(
        token_hash=expected_token_hash(),
        revoked_at="2026-07-30T08:02:00+00:00",
    )
    first_connection = ScriptedConnection(
        revoke_active_session=[[revoked_row]],
    )
    repeat_connection = ScriptedConnection(
        revoke_active_session=[[]],
        select_session_by_hash=[[revoked_row]],
    )

    first = store(first_connection).revoke_session(token=RAW_TOKEN)
    repeat = store(repeat_connection).revoke_session(token=RAW_TOKEN)

    assert first.found is True
    assert first.revoked is True
    assert first.idempotent is False
    assert repeat.found is True
    assert repeat.revoked is False
    assert repeat.idempotent is True
    assert "token_hash" not in (repeat.session or {})


def test_database_error_rolls_back_short_transaction() -> None:
    connection = ScriptedConnection(
        create_user=[RuntimeError("database unavailable")],
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        store(connection).get_or_create_user(phone="13800138000")

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.cursors[0].closed is True


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DATABASE_URL": "sqlite:///local.db", "AUTH_SESSION_HASH_SECRET": SECRET},
        {"DATABASE_URL": "postgresql://db.example/app"},
        {
            "DATABASE_URL": "postgresql://db.example/app",
            "AUTH_SESSION_HASH_SECRET": "short",
        },
    ],
)
def test_production_factory_fails_closed_on_missing_configuration(
    env: dict[str, str],
) -> None:
    with pytest.raises(ProductAuthConfigurationError):
        product_auth_store_from_env(ScriptedConnection(), env)


def test_production_factory_accepts_explicit_postgres_and_secret() -> None:
    instance = product_auth_store_from_env(
        ScriptedConnection(),
        {
            "DATABASE_URL": "postgresql://db.example/app",
            "AUTH_SESSION_HASH_SECRET": SECRET,
            "AUTH_SESSION_TTL_SECONDS": "3600",
        },
        id_factory=lambda prefix: f"{prefix}_stable",
        token_factory=lambda: RAW_TOKEN,
    )

    assert isinstance(instance, ProductAuthStore)
    assert instance.session_ttl_seconds == 3600
    assert "secret" not in repr(instance)


def test_real_postgres_user_concurrency_and_cross_connection_session() -> None:
    dsn = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    if "test" not in urlsplit(dsn).path.lower():
        pytest.skip("TEST_POSTGRES_DSN must target a database named with 'test'")
    psycopg = pytest.importorskip("psycopg")
    sql_module = pytest.importorskip("psycopg.sql")

    schema = f"auth_store_test_{uuid4().hex}"
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        admin.execute(
            sql_module.SQL("CREATE SCHEMA {}").format(
                sql_module.Identifier(schema)
            )
        )
        admin.execute(
            sql_module.SQL("SET search_path TO {}").format(
                sql_module.Identifier(schema)
            )
        )
        migration_sql = MIGRATION.read_text(encoding="utf-8")
        admin.execute(migration_sql)
        admin.execute(migration_sql)

        def create_user() -> tuple[str, bool]:
            connection = psycopg.connect(dsn, autocommit=False)
            try:
                connection.execute(
                    sql_module.SQL("SET search_path TO {}").format(
                        sql_module.Identifier(schema)
                    )
                )
                connection.commit()
                result = ProductAuthStore(
                    connection,
                    token_hash_secret=SECRET,
                ).get_or_create_user(phone="13800138000")
                return result.user["id"], result.created
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _index: create_user(), range(8)))
        user_ids = {result[0] for result in results}
        assert len(user_ids) == 1
        assert sum(1 for _user_id, created in results if created) == 1
        user_id = next(iter(user_ids))

        issuer_connection = psycopg.connect(dsn, autocommit=False)
        reader_connection = psycopg.connect(dsn, autocommit=False)
        revoker_connection = psycopg.connect(dsn, autocommit=False)
        try:
            for connection in (
                issuer_connection,
                reader_connection,
                revoker_connection,
            ):
                connection.execute(
                    sql_module.SQL("SET search_path TO {}").format(
                        sql_module.Identifier(schema)
                    )
                )
                connection.commit()

            created_store = ProductAuthStore(
                issuer_connection,
                token_hash_secret=SECRET,
            ).create_store(
                user_id=user_id,
                name="真实协议门店",
            )
            listed_stores = ProductAuthStore(
                reader_connection,
                token_hash_secret=SECRET,
            ).list_user_stores(user_id=user_id)
            assert len(listed_stores) == 1
            assert listed_stores[0]["id"] == created_store["id"]
            assert listed_stores[0]["role"] == "owner"

            issued = ProductAuthStore(
                issuer_connection,
                token_hash_secret=SECRET,
            ).issue_session(user_id=user_id)
            resolved = ProductAuthStore(
                reader_connection,
                token_hash_secret=SECRET,
            ).resolve_session(token=issued.token)
            assert resolved is not None
            assert resolved["user_id"] == user_id

            stored = reader_connection.execute(
                "SELECT token_hash FROM product_auth_sessions WHERE id = %s",
                (issued.session["id"],),
            ).fetchone()
            assert stored is not None
            assert stored[0] != issued.token
            assert re.fullmatch(r"[0-9a-f]{64}", stored[0])
            reader_connection.rollback()

            ProductAuthStore(
                revoker_connection,
                token_hash_secret=SECRET,
            ).revoke_session(token=issued.token)
            assert (
                ProductAuthStore(
                    reader_connection,
                    token_hash_secret=SECRET,
                ).resolve_session(token=issued.token)
                is None
            )

            expiring = ProductAuthStore(
                issuer_connection,
                token_hash_secret=SECRET,
            ).issue_session(user_id=user_id)
            issuer_connection.execute(
                """
                UPDATE product_auth_sessions
                SET created_at = CURRENT_TIMESTAMP - INTERVAL '2 minutes',
                    expires_at = CURRENT_TIMESTAMP - INTERVAL '1 minute'
                WHERE id = %s
                """,
                (expiring.session["id"],),
            )
            issuer_connection.commit()
            assert (
                ProductAuthStore(
                    reader_connection,
                    token_hash_secret=SECRET,
                ).resolve_session(token=expiring.token)
                is None
            )
        finally:
            issuer_connection.close()
            reader_connection.close()
            revoker_connection.close()
    finally:
        try:
            admin.execute("RESET search_path")
            admin.execute(
                sql_module.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql_module.Identifier(schema)
                )
            )
        finally:
            admin.close()
