from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit


PRODUCT_TABLES = (
    "product_generation_jobs",
    "product_generation_outbox",
    "product_generation_settlements",
    "product_generation_results",
    "product_point_accounts",
    "product_point_orders",
    "product_point_ledger",
    "product_menu_uploads",
    "product_payment_orders",
    "product_payment_events",
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
    "product_agent_finance_accounts",
    "product_commission_orders",
    "product_commission_refunds",
    "product_commission_settlements",
    "product_commission_settlement_items",
    "product_agent_withdrawals",
    "product_agent_finance_ledger",
    "product_finance_audit_events",
    "product_export_packages",
    "product_export_token_nonces",
    "product_export_access_audits",
    "product_asset_library_entries",
    "product_asset_library_object_refs",
    "product_asset_library_status_events",
    "product_admin_audit_events",
    "product_risk_decisions",
    "product_asset_access_events",
    "product_library_import_batches",
    "product_library_import_items",
)

PRODUCT_REQUIRED_COLUMNS = {
    "product_export_token_nonces": (
        "reservation_id",
        "reserved_at",
        "reservation_expires_at",
    ),
}


class PostgresRuntimeError(RuntimeError):
    def __init__(self, code: str, *, cause_type: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.cause_type = cause_type


@dataclass(frozen=True)
class PostgresRuntimeConfig:
    database_url: str
    connect_timeout_seconds: int = 3
    application_name: str = "waimai-image-tool"

    def __repr__(self) -> str:
        return (
            "PostgresRuntimeConfig(database_url='<redacted>', "
            f"connect_timeout_seconds={self.connect_timeout_seconds!r}, "
            f"application_name={self.application_name!r})"
        )


def postgres_config_from_env(
    env: Mapping[str, str] | None = None,
) -> PostgresRuntimeConfig:
    values = os.environ if env is None else env
    enabled = str(values.get("PRODUCT_POSTGRES_ENABLED") or "").strip().lower()
    if enabled in {"0", "false", "no", "off", "n"}:
        raise PostgresRuntimeError("postgres_product_runtime_disabled")
    database_url = str(values.get("DATABASE_URL") or "").strip()
    parsed = urlsplit(database_url)
    if parsed.scheme.lower() not in {"postgres", "postgresql"}:
        raise PostgresRuntimeError("postgres_database_url_required")
    timeout = _bounded_int(
        values.get("DATABASE_CONNECT_TIMEOUT_SECONDS", "3"),
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        minimum=1,
        maximum=30,
    )
    application_name = (
        str(values.get("DATABASE_APPLICATION_NAME") or "waimai-image-tool").strip()
        or "waimai-image-tool"
    )
    if len(application_name) > 63:
        raise PostgresRuntimeError("postgres_application_name_invalid")
    return PostgresRuntimeConfig(
        database_url=database_url,
        connect_timeout_seconds=timeout,
        application_name=application_name,
    )


def connect_postgres(
    config: PostgresRuntimeConfig,
    *,
    connector: Callable[..., Any] | None = None,
) -> Any:
    resolved_connector = connector
    if resolved_connector is None:
        try:
            import psycopg
        except ImportError as exc:
            raise PostgresRuntimeError(
                "postgres_driver_missing",
                cause_type=type(exc).__name__,
            ) from exc
        resolved_connector = psycopg.connect
    try:
        connection = resolved_connector(
            config.database_url,
            connect_timeout=config.connect_timeout_seconds,
            application_name=config.application_name,
            autocommit=False,
        )
    except Exception as exc:
        raise PostgresRuntimeError(
            "postgres_connection_failed",
            cause_type=type(exc).__name__,
        ) from exc
    if bool(getattr(connection, "autocommit", False)):
        try:
            connection.close()
        finally:
            raise PostgresRuntimeError("postgres_autocommit_must_be_disabled")
    return connection


@contextmanager
def postgres_connection(
    env: Mapping[str, str] | None = None,
    *,
    connector: Callable[..., Any] | None = None,
) -> Iterator[Any]:
    connection = connect_postgres(
        postgres_config_from_env(env),
        connector=connector,
    )
    try:
        yield connection
    except BaseException:
        try:
            connection.rollback()
        finally:
            connection.close()
        raise
    else:
        connection.close()


def assess_product_postgres(
    env: Mapping[str, str] | None = None,
    *,
    connector: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    try:
        config = postgres_config_from_env(env)
    except PostgresRuntimeError as exc:
        return {
            "configured": False,
            "reachable": False,
            "schemaReady": False,
            "error": exc.code,
            "missingTables": list(PRODUCT_TABLES),
            "missingColumns": [],
        }
    try:
        with postgres_connection(
            env,
            connector=connector,
        ) as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1")
                cursor.fetchone()
                missing_tables: list[str] = []
                for table in PRODUCT_TABLES:
                    cursor.execute("SELECT to_regclass(%s)", (table,))
                    row = cursor.fetchone()
                    if not row or row[0] is None:
                        missing_tables.append(table)
                missing_columns: list[str] = []
                for table, columns in PRODUCT_REQUIRED_COLUMNS.items():
                    if table in missing_tables:
                        continue
                    for column in columns:
                        cursor.execute(
                            """
                            /* product_postgres_runtime:column_exists */
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_attribute
                                WHERE attrelid = to_regclass(%s)
                                  AND attname = %s
                                  AND attnum > 0
                                  AND NOT attisdropped
                            )
                            """,
                            (table, column),
                        )
                        row = cursor.fetchone()
                        if not row or not bool(row[0]):
                            missing_columns.append(
                                f"{table}.{column}"
                            )
            finally:
                cursor.close()
    except PostgresRuntimeError as exc:
        return {
            "configured": True,
            "reachable": False,
            "schemaReady": False,
            "error": exc.code,
            "errorType": exc.cause_type,
            "missingTables": [],
            "missingColumns": [],
        }
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "schemaReady": False,
            "error": "postgres_probe_failed",
            "errorType": type(exc).__name__,
            "missingTables": [],
            "missingColumns": [],
        }
    return {
        "configured": True,
        "reachable": True,
        "schemaReady": not missing_tables and not missing_columns,
        "error": (
            ""
            if not missing_tables and not missing_columns
            else "postgres_schema_incomplete"
        ),
        "missingTables": missing_tables,
        "missingColumns": missing_columns,
        "connectTimeoutSeconds": config.connect_timeout_seconds,
    }


def _bounded_int(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PostgresRuntimeError(
            "postgres_runtime_config_invalid",
            cause_type=name,
        ) from exc
    if number < minimum or number > maximum:
        raise PostgresRuntimeError(
            "postgres_runtime_config_invalid",
            cause_type=name,
        )
    return number
