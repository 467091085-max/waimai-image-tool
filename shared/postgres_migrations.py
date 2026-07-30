from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
SCHEMA_MIGRATIONS_TABLE = "waimai_schema_migrations"

# Stable, application-specific signed BIGINT used for the session advisory lock.
MIGRATION_ADVISORY_LOCK_KEY = 0x5741494D494D4752

_LOCK_SQL = "SELECT pg_advisory_lock(%s)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s)"
_BOOTSTRAP_SQL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
    version TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL
        CHECK (checksum_sha256 ~ '^[0-9a-f]{{64}}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_waimai_schema_migrations_version
        CHECK (
            char_length(version) BETWEEN 1 AND 255
            AND version = btrim(version)
        )
)
""".strip()
_LOAD_HISTORY_SQL = (
    f"SELECT version, checksum_sha256 "
    f"FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version"
)
_INSERT_HISTORY_SQL = (
    f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} "
    "(version, checksum_sha256) VALUES (%s, %s)"
)

_OUTER_BEGIN_RE = re.compile(
    r"\A\s*BEGIN(?:\s+TRANSACTION)?\s*;\s*",
    flags=re.IGNORECASE,
)
_OUTER_COMMIT_RE = re.compile(
    r"\s*COMMIT\s*;\s*\Z",
    flags=re.IGNORECASE,
)
_TRANSACTION_CONTROL_RE = re.compile(
    r"^\s*(?:"
    r"BEGIN(?:\s+TRANSACTION)?"
    r"|START\s+TRANSACTION"
    r"|COMMIT"
    r"|ROLLBACK"
    r")\s*;",
    flags=re.IGNORECASE | re.MULTILINE,
)


class PostgresMigrationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        version: str = "",
        cause_type: str = "",
    ) -> None:
        self.code = code
        self.version = version
        self.cause_type = cause_type
        detail = f"{code}:{version}" if version else code
        super().__init__(detail)


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum_sha256: str
    sql: str


@dataclass(frozen=True)
class MigrationRunResult:
    discovered_versions: tuple[str, ...]
    previously_applied_versions: tuple[str, ...]
    applied_versions: tuple[str, ...]


def discover_migrations(
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> tuple[Migration, ...]:
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise PostgresMigrationError("migration_directory_missing")

    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise PostgresMigrationError(
                "migration_file_invalid",
                version=path.name,
            )
        try:
            raw_sql = path.read_bytes()
            source_sql = raw_sql.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise PostgresMigrationError(
                "migration_file_unreadable",
                version=path.name,
                cause_type=type(exc).__name__,
            ) from exc
        if not source_sql.strip() or "\x00" in source_sql:
            raise PostgresMigrationError(
                "migration_file_invalid",
                version=path.name,
            )
        migrations.append(
            Migration(
                version=path.name,
                path=path,
                checksum_sha256=hashlib.sha256(raw_sql).hexdigest(),
                sql=_runner_managed_sql(source_sql, version=path.name),
            )
        )
    return tuple(migrations)


def run_postgres_migrations(
    connection: Any,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> MigrationRunResult:
    """Apply migrations through a dedicated, idle, non-autocommit connection.

    The caller owns the connection. The runner commits its own bootstrap,
    history reads, and per-file transactions but leaves a healthy connection
    open after returning.
    """
    if bool(getattr(connection, "autocommit", False)):
        raise PostgresMigrationError("migration_autocommit_must_be_disabled")

    migrations = discover_migrations(migrations_dir)
    lock_acquired = False
    try:
        _acquire_advisory_lock(connection)
        lock_acquired = True
        _bootstrap_history_table(connection)
        applied_history = _load_applied_history(connection)
        _validate_applied_history(migrations, applied_history)
        connection.commit()

        applied_versions: list[str] = []
        for migration in migrations:
            if migration.version in applied_history:
                continue
            _apply_migration(connection, migration)
            applied_versions.append(migration.version)

        result = MigrationRunResult(
            discovered_versions=tuple(item.version for item in migrations),
            previously_applied_versions=tuple(
                item.version
                for item in migrations
                if item.version in applied_history
            ),
            applied_versions=tuple(applied_versions),
        )
    except BaseException:
        _rollback_quietly(connection)
        if lock_acquired:
            _release_after_error(connection)
        raise

    _release_advisory_lock(connection)
    return result


def _runner_managed_sql(source_sql: str, *, version: str) -> str:
    begin_match = _OUTER_BEGIN_RE.match(source_sql)
    commit_match = _OUTER_COMMIT_RE.search(source_sql)
    if bool(begin_match) != bool(commit_match):
        raise PostgresMigrationError(
            "migration_transaction_wrapper_invalid",
            version=version,
        )

    if begin_match and commit_match:
        body = source_sql[begin_match.end() : commit_match.start()]
    else:
        body = source_sql
    if not body.strip():
        raise PostgresMigrationError(
            "migration_file_invalid",
            version=version,
        )
    if _TRANSACTION_CONTROL_RE.search(body):
        raise PostgresMigrationError(
            "migration_transaction_control_forbidden",
            version=version,
        )
    return body.strip()


def _acquire_advisory_lock(connection: Any) -> None:
    lock_may_be_held = False
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(_LOCK_SQL, (MIGRATION_ADVISORY_LOCK_KEY,))
            lock_may_be_held = True
            cursor.fetchone()
        finally:
            cursor.close()
        connection.commit()
    except Exception as exc:
        _rollback_quietly(connection)
        if lock_may_be_held:
            _release_after_error(connection)
        raise PostgresMigrationError(
            "migration_lock_failed",
            cause_type=type(exc).__name__,
        ) from exc


def _bootstrap_history_table(connection: Any) -> None:
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(_BOOTSTRAP_SQL)
        finally:
            cursor.close()
        connection.commit()
    except Exception as exc:
        _rollback_quietly(connection)
        raise PostgresMigrationError(
            "migration_history_bootstrap_failed",
            cause_type=type(exc).__name__,
        ) from exc


def _load_applied_history(connection: Any) -> dict[str, str]:
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(_LOAD_HISTORY_SQL)
            rows = cursor.fetchall()
        finally:
            cursor.close()
    except Exception as exc:
        raise PostgresMigrationError(
            "migration_history_read_failed",
            cause_type=type(exc).__name__,
        ) from exc

    history: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) < 2:
            raise PostgresMigrationError("migration_history_invalid")
        version = str(row[0])
        checksum = str(row[1])
        if version in history:
            raise PostgresMigrationError(
                "migration_history_invalid",
                version=version,
            )
        history[version] = checksum
    return history


def _validate_applied_history(
    migrations: Iterable[Migration],
    applied_history: dict[str, str],
) -> None:
    ordered_migrations = tuple(migrations)
    discovered = {item.version: item for item in ordered_migrations}
    for version, applied_checksum in applied_history.items():
        migration = discovered.get(version)
        if migration is None:
            raise PostgresMigrationError(
                "applied_migration_missing",
                version=version,
            )
        if migration.checksum_sha256 != applied_checksum:
            raise PostgresMigrationError(
                "migration_checksum_mismatch",
                version=version,
            )

    applied_versions = set(applied_history)
    if not applied_versions:
        return
    newest_applied = max(applied_versions)
    for migration in ordered_migrations:
        if (
            migration.version not in applied_versions
            and migration.version < newest_applied
        ):
            raise PostgresMigrationError(
                "migration_history_gap",
                version=migration.version,
            )


def _apply_migration(connection: Any, migration: Migration) -> None:
    try:
        cursor = connection.cursor()
        try:
            # No parameters keeps psycopg on the multi-statement SQL path.
            cursor.execute(migration.sql)
            cursor.execute(
                _INSERT_HISTORY_SQL,
                (migration.version, migration.checksum_sha256),
            )
        finally:
            cursor.close()
        connection.commit()
    except Exception as exc:
        _rollback_quietly(connection)
        raise PostgresMigrationError(
            "migration_execution_failed",
            version=migration.version,
            cause_type=type(exc).__name__,
        ) from exc


def _release_advisory_lock(connection: Any) -> None:
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                _UNLOCK_SQL,
                (MIGRATION_ADVISORY_LOCK_KEY,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row or row[0] is not True:
            raise PostgresMigrationError("migration_lock_not_held")
        connection.commit()
    except PostgresMigrationError:
        _rollback_quietly(connection)
        _close_quietly(connection)
        raise
    except Exception as exc:
        _rollback_quietly(connection)
        _close_quietly(connection)
        raise PostgresMigrationError(
            "migration_unlock_failed",
            cause_type=type(exc).__name__,
        ) from exc


def _release_after_error(connection: Any) -> None:
    try:
        _release_advisory_lock(connection)
    except BaseException:
        _close_quietly(connection)


def _rollback_quietly(connection: Any) -> None:
    try:
        connection.rollback()
    except BaseException:
        pass


def _close_quietly(connection: Any) -> None:
    try:
        connection.close()
    except BaseException:
        pass
