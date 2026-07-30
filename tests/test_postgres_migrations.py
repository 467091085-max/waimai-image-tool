from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from shared.postgres_migrations import (
    MIGRATION_ADVISORY_LOCK_KEY,
    MigrationRunResult,
    PostgresMigrationError,
    discover_migrations,
    run_postgres_migrations,
)


class SqlProtocolCursor:
    def __init__(self, connection: "SqlProtocolConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []
        self.closed = False

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] | None = None,
    ) -> None:
        parameters = parameters or ()
        normalized = " ".join(operation.split())
        self.connection.events.append(
            ("execute", normalized, tuple(parameters))
        )

        if normalized == "SELECT pg_advisory_lock(%s)":
            assert parameters == (MIGRATION_ADVISORY_LOCK_KEY,)
            assert self.connection.locked is False
            self.connection.locked = True
            self.rows = [(None,)]
            return
        if normalized == "SELECT pg_advisory_unlock(%s)":
            assert parameters == (MIGRATION_ADVISORY_LOCK_KEY,)
            was_locked = self.connection.locked
            self.connection.locked = False
            self.rows = [(was_locked,)]
            return
        if normalized.startswith(
            "CREATE TABLE IF NOT EXISTS waimai_schema_migrations"
        ):
            assert self.connection.locked is True
            assert "checksum_sha256" in normalized
            self.rows = []
            return
        if normalized.startswith(
            "SELECT version, checksum_sha256 "
            "FROM waimai_schema_migrations"
        ):
            assert self.connection.locked is True
            self.rows = sorted(self.connection.applied.items())
            return
        if normalized.startswith(
            "INSERT INTO waimai_schema_migrations "
            "(version, checksum_sha256)"
        ):
            assert self.connection.locked is True
            assert len(parameters) == 2
            version, checksum = (str(parameters[0]), str(parameters[1]))
            if version == self.connection.fail_history_insert_for:
                raise RuntimeError("simulated metadata insert failure")
            assert len(self.connection.pending_scripts) == 1
            assert not self.connection.pending_history
            self.connection.pending_history.append((version, checksum))
            self.rows = []
            return

        assert self.connection.locked is True
        assert parameters == ()
        if (
            self.connection.fail_script_token
            and self.connection.fail_script_token in operation
        ):
            raise RuntimeError("simulated migration SQL failure")
        assert not self.connection.pending_scripts
        assert not self.connection.pending_history
        self.connection.pending_scripts.append(operation.strip())
        self.rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def close(self) -> None:
        self.closed = True


class SqlProtocolConnection:
    autocommit = False

    def __init__(
        self,
        *,
        applied: dict[str, str] | None = None,
        fail_script_token: str = "",
        fail_history_insert_for: str = "",
        fail_commit_attempts: set[int] | None = None,
    ) -> None:
        self.applied = dict(applied or {})
        self.fail_script_token = fail_script_token
        self.fail_history_insert_for = fail_history_insert_for
        self.fail_commit_attempts = set(fail_commit_attempts or set())
        self.pending_scripts: list[str] = []
        self.pending_history: list[tuple[str, str]] = []
        self.committed_scripts: list[str] = []
        self.events: list[tuple[Any, ...]] = []
        self.commit_attempt_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.locked = False
        self.closed = False

    def cursor(self) -> SqlProtocolCursor:
        assert self.closed is False
        return SqlProtocolCursor(self)

    def commit(self) -> None:
        assert self.closed is False
        self.commit_attempt_count += 1
        if self.commit_attempt_count in self.fail_commit_attempts:
            self.events.append(("commit_failed",))
            raise RuntimeError("simulated commit failure")
        if self.pending_scripts or self.pending_history:
            assert len(self.pending_scripts) == 1
            assert len(self.pending_history) == 1
            version, checksum = self.pending_history[0]
            assert version not in self.applied
            self.applied[version] = checksum
            self.committed_scripts.extend(self.pending_scripts)
        self.pending_scripts.clear()
        self.pending_history.clear()
        self.commit_count += 1
        self.events.append(("commit",))

    def rollback(self) -> None:
        self.pending_scripts.clear()
        self.pending_history.clear()
        self.rollback_count += 1
        self.events.append(("rollback",))

    def close(self) -> None:
        self.closed = True
        self.events.append(("close",))


def write_migration(
    directory: Path,
    filename: str,
    statement: str,
    *,
    wrapped: bool = True,
) -> str:
    if wrapped:
        source = f"BEGIN;\n\n{statement}\n\nCOMMIT;\n"
    else:
        source = f"{statement}\n"
    path = directory / filename
    path.write_text(source, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute_events(
    connection: SqlProtocolConnection,
) -> list[tuple[Any, ...]]:
    return [event for event in connection.events if event[0] == "execute"]


def test_discovery_sorts_by_filename_and_hashes_exact_file_bytes(
    tmp_path: Path,
) -> None:
    checksum_010 = write_migration(
        tmp_path,
        "010_last.sql",
        "CREATE TABLE migration_010 (id INTEGER);",
    )
    checksum_001 = write_migration(
        tmp_path,
        "001_first.sql",
        "CREATE TABLE migration_001 (id INTEGER);",
    )
    checksum_002 = write_migration(
        tmp_path,
        "002_middle.sql",
        "CREATE TABLE migration_002 (id INTEGER);",
        wrapped=False,
    )

    migrations = discover_migrations(tmp_path)

    assert tuple(item.version for item in migrations) == (
        "001_first.sql",
        "002_middle.sql",
        "010_last.sql",
    )
    assert tuple(item.checksum_sha256 for item in migrations) == (
        checksum_001,
        checksum_002,
        checksum_010,
    )
    assert migrations[0].sql == "CREATE TABLE migration_001 (id INTEGER);"
    assert migrations[2].sql == "CREATE TABLE migration_010 (id INTEGER);"
    assert all("BEGIN;" not in item.sql for item in migrations)
    assert all("COMMIT;" not in item.sql for item in migrations)


def test_blocking_advisory_lock_precedes_schema_work_and_order_is_stable(
    tmp_path: Path,
) -> None:
    write_migration(
        tmp_path,
        "010_last.sql",
        "CREATE TABLE migration_010 (id INTEGER);",
    )
    write_migration(
        tmp_path,
        "001_first.sql",
        "CREATE TABLE migration_001 (id INTEGER);",
    )
    write_migration(
        tmp_path,
        "002_middle.sql",
        "CREATE TABLE migration_002 (id INTEGER);",
    )
    connection = SqlProtocolConnection()

    result = run_postgres_migrations(connection, tmp_path)

    assert result == MigrationRunResult(
        discovered_versions=(
            "001_first.sql",
            "002_middle.sql",
            "010_last.sql",
        ),
        previously_applied_versions=(),
        applied_versions=(
            "001_first.sql",
            "002_middle.sql",
            "010_last.sql",
        ),
    )
    first_query = execute_events(connection)[0]
    assert first_query == (
        "execute",
        "SELECT pg_advisory_lock(%s)",
        (MIGRATION_ADVISORY_LOCK_KEY,),
    )
    assert "pg_try_advisory_lock" not in first_query[1]
    assert connection.committed_scripts == [
        "CREATE TABLE migration_001 (id INTEGER);",
        "CREATE TABLE migration_002 (id INTEGER);",
        "CREATE TABLE migration_010 (id INTEGER);",
    ]
    assert connection.locked is False
    assert execute_events(connection)[-1][1] == (
        "SELECT pg_advisory_unlock(%s)"
    )


def test_repeated_run_is_idempotent_and_does_not_execute_sql_twice(
    tmp_path: Path,
) -> None:
    checksum_001 = write_migration(
        tmp_path,
        "001_first.sql",
        "CREATE TABLE migration_001 (id INTEGER);",
    )
    checksum_002 = write_migration(
        tmp_path,
        "002_second.sql",
        "CREATE TABLE migration_002 (id INTEGER);",
    )
    connection = SqlProtocolConnection()

    first = run_postgres_migrations(connection, tmp_path)
    second = run_postgres_migrations(connection, tmp_path)

    assert first.applied_versions == ("001_first.sql", "002_second.sql")
    assert second == MigrationRunResult(
        discovered_versions=("001_first.sql", "002_second.sql"),
        previously_applied_versions=("001_first.sql", "002_second.sql"),
        applied_versions=(),
    )
    assert connection.applied == {
        "001_first.sql": checksum_001,
        "002_second.sql": checksum_002,
    }
    assert connection.committed_scripts == [
        "CREATE TABLE migration_001 (id INTEGER);",
        "CREATE TABLE migration_002 (id INTEGER);",
    ]
    assert sum(
        event[1] == "SELECT pg_advisory_lock(%s)"
        for event in execute_events(connection)
    ) == 2


def test_changed_applied_migration_fails_closed_before_any_new_sql(
    tmp_path: Path,
) -> None:
    write_migration(
        tmp_path,
        "001_first.sql",
        "CREATE TABLE migration_001 (id INTEGER);",
    )
    write_migration(
        tmp_path,
        "002_pending.sql",
        "CREATE TABLE migration_002 (id INTEGER);",
    )
    connection = SqlProtocolConnection(
        applied={"001_first.sql": "0" * 64}
    )

    with pytest.raises(PostgresMigrationError) as captured:
        run_postgres_migrations(connection, tmp_path)

    assert captured.value.code == "migration_checksum_mismatch"
    assert captured.value.version == "001_first.sql"
    assert connection.committed_scripts == []
    assert connection.applied == {"001_first.sql": "0" * 64}
    assert connection.rollback_count >= 1
    assert connection.locked is False


def test_migration_sql_failure_rolls_back_and_releases_lock(
    tmp_path: Path,
) -> None:
    write_migration(
        tmp_path,
        "001_failure.sql",
        "CREATE TABLE explode_marker (id INTEGER);",
    )
    connection = SqlProtocolConnection(
        fail_script_token="explode_marker"
    )

    with pytest.raises(PostgresMigrationError) as captured:
        run_postgres_migrations(connection, tmp_path)

    assert captured.value.code == "migration_execution_failed"
    assert captured.value.version == "001_failure.sql"
    assert captured.value.cause_type == "RuntimeError"
    assert connection.applied == {}
    assert connection.committed_scripts == []
    assert connection.pending_scripts == []
    assert connection.pending_history == []
    assert connection.rollback_count >= 1
    assert connection.locked is False


def test_lock_commit_failure_does_not_leak_session_lock(
    tmp_path: Path,
) -> None:
    write_migration(
        tmp_path,
        "001_never_reached.sql",
        "CREATE TABLE migration_001 (id INTEGER);",
    )
    connection = SqlProtocolConnection(fail_commit_attempts={1})

    with pytest.raises(PostgresMigrationError) as captured:
        run_postgres_migrations(connection, tmp_path)

    assert captured.value.code == "migration_lock_failed"
    assert captured.value.cause_type == "RuntimeError"
    assert connection.applied == {}
    assert connection.committed_scripts == []
    assert connection.locked is False
    assert execute_events(connection)[-1][1] == (
        "SELECT pg_advisory_unlock(%s)"
    )


def test_history_insert_failure_rolls_back_migration_sql_atomically(
    tmp_path: Path,
) -> None:
    write_migration(
        tmp_path,
        "001_atomic.sql",
        "CREATE TABLE migration_atomic (id INTEGER);",
    )
    connection = SqlProtocolConnection(
        fail_history_insert_for="001_atomic.sql"
    )

    with pytest.raises(PostgresMigrationError) as captured:
        run_postgres_migrations(connection, tmp_path)

    assert captured.value.code == "migration_execution_failed"
    assert captured.value.version == "001_atomic.sql"
    assert connection.applied == {}
    assert connection.committed_scripts == []
    assert connection.pending_scripts == []
    assert connection.pending_history == []
    assert connection.rollback_count >= 1
    assert connection.locked is False


def test_applied_version_missing_from_files_fails_closed(
    tmp_path: Path,
) -> None:
    write_migration(
        tmp_path,
        "002_present.sql",
        "CREATE TABLE migration_002 (id INTEGER);",
    )
    connection = SqlProtocolConnection(
        applied={"001_deleted.sql": "a" * 64}
    )

    with pytest.raises(PostgresMigrationError) as captured:
        run_postgres_migrations(connection, tmp_path)

    assert captured.value.code == "applied_migration_missing"
    assert captured.value.version == "001_deleted.sql"
    assert connection.committed_scripts == []
    assert connection.locked is False


def test_incomplete_or_nested_transaction_control_is_rejected(
    tmp_path: Path,
) -> None:
    (tmp_path / "001_incomplete.sql").write_text(
        "BEGIN;\nCREATE TABLE migration_001 (id INTEGER);\n",
        encoding="utf-8",
    )

    with pytest.raises(PostgresMigrationError) as incomplete:
        discover_migrations(tmp_path)

    assert incomplete.value.code == "migration_transaction_wrapper_invalid"

    (tmp_path / "001_incomplete.sql").write_text(
        "BEGIN;\n"
        "CREATE TABLE migration_001 (id INTEGER);\n"
        "COMMIT;\n"
        "BEGIN;\n"
        "SELECT 1;\n"
        "COMMIT;\n",
        encoding="utf-8",
    )

    with pytest.raises(PostgresMigrationError) as nested:
        discover_migrations(tmp_path)

    assert nested.value.code == "migration_transaction_control_forbidden"
