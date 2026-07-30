from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared.postgres_runtime import (
    PRODUCT_REQUIRED_COLUMNS,
    PRODUCT_TABLES,
    PostgresRuntimeError,
    assess_product_postgres,
    connect_postgres,
    postgres_config_from_env,
    postgres_connection,
)


PRODUCT_FINANCE_TABLES = (
    "product_agent_finance_accounts",
    "product_commission_orders",
    "product_commission_refunds",
    "product_commission_settlements",
    "product_commission_settlement_items",
    "product_agent_withdrawals",
    "product_agent_finance_ledger",
    "product_finance_audit_events",
)

PRODUCT_GROWTH_TABLES = (
    "product_growth_outbox",
    "product_growth_subject_locks",
    "product_growth_agents",
    "product_growth_agent_bindings",
    "product_growth_invite_codes",
    "product_growth_invite_relations",
    "product_growth_business_events",
    "product_growth_reward_grants",
    "product_growth_reward_reversal_states",
    "product_growth_reward_debts",
    "product_growth_reward_debt_recoveries",
    "product_growth_reward_reversals",
    "product_growth_audit_events",
)

ROOT = Path(__file__).resolve().parents[1]
TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()


class FakeCursor:
    def __init__(
        self,
        existing_tables: set[str],
        missing_columns: set[str] | None = None,
    ) -> None:
        self.existing_tables = existing_tables
        self.missing_columns = missing_columns or set()
        self.last_query = ""
        self.last_parameters: tuple[Any, ...] = ()
        self.closed = False

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] = (),
    ) -> None:
        self.last_query = operation
        self.last_parameters = parameters

    def fetchone(self) -> tuple[Any, ...]:
        if "product_postgres_runtime:column_exists" in self.last_query:
            table = str(self.last_parameters[0])
            column = str(self.last_parameters[1])
            return (f"{table}.{column}" not in self.missing_columns,)
        if "to_regclass" in self.last_query:
            table = str(self.last_parameters[0])
            return (table if table in self.existing_tables else None,)
        return (1,)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    autocommit = False

    def __init__(
        self,
        existing_tables: set[str] | None = None,
        missing_columns: set[str] | None = None,
    ) -> None:
        self.cursor_instance = FakeCursor(
            existing_tables or set(PRODUCT_TABLES),
            missing_columns,
        )
        self.closed = False
        self.rollback_count = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


def test_config_requires_postgres_and_redacts_database_url() -> None:
    with pytest.raises(
        PostgresRuntimeError,
        match="postgres_database_url_required",
    ):
        postgres_config_from_env({"DATABASE_URL": "sqlite:///local.db"})

    config = postgres_config_from_env(
        {
            "DATABASE_URL": "postgresql://user:secret@db.example/app",
            "DATABASE_CONNECT_TIMEOUT_SECONDS": "7",
            "DATABASE_APPLICATION_NAME": "waimai-test",
        }
    )

    assert config.connect_timeout_seconds == 7
    assert config.application_name == "waimai-test"
    assert "secret" not in repr(config)
    assert "<redacted>" in repr(config)


def test_config_rejects_explicitly_disabled_product_runtime() -> None:
    with pytest.raises(
        PostgresRuntimeError,
        match="postgres_product_runtime_disabled",
    ):
        postgres_config_from_env(
            {
                "DATABASE_URL": "postgresql://db.example/app",
                "PRODUCT_POSTGRES_ENABLED": "false",
            }
        )


def test_connect_passes_bounded_fail_closed_options() -> None:
    captured: dict[str, Any] = {}
    connection = FakeConnection()

    def connector(database_url: str, **kwargs: Any) -> FakeConnection:
        captured["database_url"] = database_url
        captured.update(kwargs)
        return connection

    result = connect_postgres(
        postgres_config_from_env(
            {"DATABASE_URL": "postgresql://db.example/app"}
        ),
        connector=connector,
    )

    assert result is connection
    assert captured == {
        "database_url": "postgresql://db.example/app",
        "connect_timeout": 3,
        "application_name": "waimai-image-tool",
        "autocommit": False,
    }


def test_connection_error_does_not_expose_credentials() -> None:
    def connector(*_args: Any, **_kwargs: Any) -> FakeConnection:
        raise RuntimeError("postgresql://user:top-secret@db.example/app")

    with pytest.raises(PostgresRuntimeError) as captured:
        connect_postgres(
            postgres_config_from_env(
                {
                    "DATABASE_URL": (
                        "postgresql://user:top-secret@db.example/app"
                    )
                }
            ),
            connector=connector,
        )

    assert captured.value.code == "postgres_connection_failed"
    assert captured.value.cause_type == "RuntimeError"
    assert "top-secret" not in str(captured.value)


def test_connection_context_rolls_back_exception_and_always_closes() -> None:
    connection = FakeConnection()

    with pytest.raises(ValueError, match="stop"):
        with postgres_connection(
            {"DATABASE_URL": "postgresql://db.example/app"},
            connector=lambda *_args, **_kwargs: connection,
        ):
            raise ValueError("stop")

    assert connection.rollback_count == 1
    assert connection.closed is True


def test_probe_reports_reachability_and_missing_schema_without_secrets() -> None:
    existing = set(PRODUCT_TABLES) - {"product_menu_uploads"}
    connection = FakeConnection(existing)

    result = assess_product_postgres(
        {"DATABASE_URL": "postgresql://user:secret@db.example/app"},
        connector=lambda *_args, **_kwargs: connection,
    )

    assert result == {
        "configured": True,
        "reachable": True,
        "schemaReady": False,
        "error": "postgres_schema_incomplete",
        "missingTables": ["product_menu_uploads"],
        "missingColumns": [],
        "connectTimeoutSeconds": 3,
    }
    assert connection.cursor_instance.closed is True
    assert connection.closed is True
    assert "secret" not in repr(result)


@pytest.mark.parametrize("missing_table", PRODUCT_FINANCE_TABLES)
def test_probe_requires_every_product_finance_table(
    missing_table: str,
) -> None:
    connection = FakeConnection(set(PRODUCT_TABLES) - {missing_table})

    result = assess_product_postgres(
        {"DATABASE_URL": "postgresql://db.example/app"},
        connector=lambda *_args, **_kwargs: connection,
    )

    assert result["schemaReady"] is False
    assert result["error"] == "postgres_schema_incomplete"
    assert result["missingTables"] == [missing_table]


@pytest.mark.parametrize("missing_table", PRODUCT_GROWTH_TABLES)
def test_probe_requires_every_product_growth_table(
    missing_table: str,
) -> None:
    connection = FakeConnection(set(PRODUCT_TABLES) - {missing_table})

    result = assess_product_postgres(
        {"DATABASE_URL": "postgresql://db.example/app"},
        connector=lambda *_args, **_kwargs: connection,
    )

    assert result["schemaReady"] is False
    assert result["error"] == "postgres_schema_incomplete"
    assert result["missingTables"] == [missing_table]


@pytest.mark.parametrize(
    "column",
    PRODUCT_REQUIRED_COLUMNS["product_export_token_nonces"],
)
def test_probe_requires_export_nonce_reservation_columns(
    column: str,
) -> None:
    missing = f"product_export_token_nonces.{column}"
    connection = FakeConnection(missing_columns={missing})

    result = assess_product_postgres(
        {"DATABASE_URL": "postgresql://db.example/app"},
        connector=lambda *_args, **_kwargs: connection,
    )

    assert result["schemaReady"] is False
    assert result["error"] == "postgres_schema_incomplete"
    assert result["missingTables"] == []
    assert result["missingColumns"] == [missing]


def test_probe_reports_unconfigured_without_attempting_connection() -> None:
    called = False

    def connector(*_args: Any, **_kwargs: Any) -> FakeConnection:
        nonlocal called
        called = True
        return FakeConnection()

    result = assess_product_postgres({}, connector=connector)

    assert result["configured"] is False
    assert result["reachable"] is False
    assert result["schemaReady"] is False
    assert result["error"] == "postgres_database_url_required"
    assert called is False


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the real schema probe",
)
def test_real_postgres_probe_requires_growth_debt_recovery_table() -> None:
    if "test" not in TEST_POSTGRES_DSN.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    schema = f"test_product_probe_{uuid4().hex}"
    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)

    def schema_connection() -> Any:
        connection = psycopg.connect(
            TEST_POSTGRES_DSN,
            autocommit=False,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(schema)
                )
            )
        connection.commit()
        return connection

    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema)
                )
            )
        connection = schema_connection()
        try:
            with connection.cursor() as cursor:
                for migration in sorted((ROOT / "migrations").glob("*.sql")):
                    cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

        configured = {
            "DATABASE_URL": "postgresql://probe.invalid/waimai_test",
            "PRODUCT_POSTGRES_ENABLED": "true",
        }
        ready = assess_product_postgres(
            configured,
            connector=lambda *_args, **_kwargs: schema_connection(),
        )
        assert ready["schemaReady"] is True
        assert ready["missingTables"] == []
        assert ready["missingColumns"] == []

        connection = schema_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    ALTER TABLE product_export_token_nonces
                    DROP COLUMN reservation_id
                    """
                )
            connection.commit()
        finally:
            connection.close()

        missing_reservation_column = assess_product_postgres(
            configured,
            connector=lambda *_args, **_kwargs: schema_connection(),
        )
        assert missing_reservation_column["schemaReady"] is False
        assert missing_reservation_column["missingTables"] == []
        assert missing_reservation_column["missingColumns"] == [
            "product_export_token_nonces.reservation_id"
        ]

        connection = schema_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    (
                        ROOT
                        / "migrations"
                        / "014_product_export_nonce_reservations.sql"
                    ).read_text(encoding="utf-8")
                )
            connection.commit()
        finally:
            connection.close()

        connection = schema_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DROP TABLE product_growth_reward_debt_recoveries"
                )
            connection.commit()
        finally:
            connection.close()

        incomplete = assess_product_postgres(
            configured,
            connector=lambda *_args, **_kwargs: schema_connection(),
        )
        assert incomplete["schemaReady"] is False
        assert incomplete["missingTables"] == [
            "product_growth_reward_debt_recoveries"
        ]
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        admin.close()
