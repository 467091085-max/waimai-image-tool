"""Protocol tests only; this module does not connect to a real PostgreSQL server."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from shared.menu_upload_store import (
    InvalidMenuUploadInput,
    MenuUploadConflict,
    MenuUploadNotFound,
    MenuUploadStateConflict,
    MenuUploadStore,
    public_menu_upload_dto,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "003_menu_uploads_postgres.sql"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


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
        match = re.search(r"/\* menu_upload_store:([a-z_]+) \*/", operation)
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


class CommitFailureConnection(ScriptedConnection):
    def commit(self) -> None:
        self.commits += 1
        raise RuntimeError("commit failed")


def upload_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "menu-1",
        "owner_user_id": "user-1",
        "object_ref": "private/menu-uploads/menu-1.xlsx",
        "object_sha256": DIGEST_A,
        "original_filename": "午餐菜单.xlsx",
        "content_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "file_size": 2048,
        "parser_version": "menu-parser-v3",
        "item_count": 12,
        "store_name": "测试餐厅",
        "parsed_summary": {
            "categories": ["盖饭", "小吃"],
            "rows": 12,
        },
        "status": "parsed",
        "failure_reason": "",
        "created_at": "2026-07-30T08:00:00Z",
        "updated_at": "2026-07-30T08:00:00Z",
        "frozen_at": None,
    }
    row.update(overrides)
    return row


def upload_values(**overrides: Any) -> dict[str, Any]:
    values = {
        "upload_id": "menu-1",
        "owner_user_id": "user-1",
        "object_ref": "private/menu-uploads/menu-1.xlsx",
        "object_sha256": DIGEST_A,
        "original_filename": "午餐菜单.xlsx",
        "content_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "file_size": 2048,
        "parser_version": "menu-parser-v3",
        "item_count": 12,
        "store_name": "测试餐厅",
        "parsed_summary": {
            "categories": ["盖饭", "小吃"],
            "rows": 12,
        },
    }
    values.update(overrides)
    return values


def create_public(store: MenuUploadStore, **overrides: Any):
    return store.create_or_get(**upload_values(**overrides))


def create_private(store: MenuUploadStore, **overrides: Any):
    return store.create_or_get_private_record(**upload_values(**overrides))


def test_store_requires_autocommit_disabled_connection() -> None:
    with pytest.raises(InvalidMenuUploadInput, match="autocommit-disabled"):
        MenuUploadStore(AutocommitConnection())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("upload_id", ""),
        ("upload_id", "../menu-1"),
        ("upload_id", "menu 1"),
        ("upload_id", "m" * 129),
        ("owner_user_id", ""),
        ("owner_user_id", "user/1"),
        ("owner_user_id", "用户-1"),
        ("owner_user_id", 123),
    ],
)
def test_rejects_invalid_upload_and_owner_ids(field: str, value: Any) -> None:
    with pytest.raises(InvalidMenuUploadInput, match=field):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            **{field: value},
        )


def test_create_commits_one_short_transaction_and_returns_public_dto() -> None:
    connection = ScriptedConnection(create=[[upload_row()]])

    result = create_public(MenuUploadStore(connection))

    assert result.created is True
    assert result.record["id"] == "menu-1"
    assert "object_ref" not in result.record
    assert "object_sha256" not in result.record
    assert "owner_user_id" not in result.record
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(connection.cursors) == 1
    assert connection.cursors[0].closed is True
    name, sql, parameters = connection.calls[0]
    assert name == "create"
    assert "ON CONFLICT (id) DO NOTHING" in sql
    assert parameters[-1] == (
        '{"categories":["盖饭","小吃"],"rows":12}'
    )


def test_private_create_method_is_explicit_and_returns_object_reference() -> None:
    connection = ScriptedConnection(create=[[upload_row()]])

    result = create_private(MenuUploadStore(connection))

    assert result.record["object_ref"] == "private/menu-uploads/menu-1.xlsx"
    assert result.record["object_sha256"] == DIGEST_A


def test_same_id_and_immutable_content_is_idempotent_after_freeze() -> None:
    existing = upload_row(
        status="frozen",
        frozen_at="2026-07-30T09:00:00Z",
        updated_at="2026-07-30T09:00:00Z",
        parsed_summary='{"rows":12,"categories":["盖饭","小吃"]}',
    )
    connection = ScriptedConnection(
        create=[[]],
        select_by_id=[[existing]],
    )

    result = create_private(MenuUploadStore(connection))

    assert result.created is False
    assert result.record["status"] == "frozen"
    assert result.record["parsed_summary"]["rows"] == 12
    assert [call[0] for call in connection.calls] == [
        "create",
        "select_by_id",
    ]
    assert connection.commits == 1


@pytest.mark.parametrize(
    ("existing_override", "input_override"),
    [
        ({"owner_user_id": "user-2"}, {}),
        ({"object_sha256": DIGEST_B}, {}),
        ({}, {"file_size": 2049}),
        ({}, {"item_count": 13}),
        ({}, {"parser_version": "menu-parser-v4"}),
        ({}, {"parsed_summary": {"rows": 13}}),
    ],
)
def test_same_id_with_different_content_conflicts(
    existing_override: dict[str, Any],
    input_override: dict[str, Any],
) -> None:
    connection = ScriptedConnection(
        create=[[]],
        select_by_id=[[upload_row(**existing_override)]],
    )

    with pytest.raises(MenuUploadConflict):
        create_private(MenuUploadStore(connection), **input_override)

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_get_owned_cross_user_is_indistinguishable_from_missing() -> None:
    connection = ScriptedConnection(select_owned=[[]])

    with pytest.raises(MenuUploadNotFound) as error:
        MenuUploadStore(connection).get_owned(
            upload_id="menu-1",
            owner_user_id="user-2",
        )

    assert str(error.value) == "menu-1"
    assert "user-1" not in str(error.value)
    assert connection.calls[0][2] == ("menu-1", "user-2")
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_get_owned_public_and_private_methods_have_distinct_exposure() -> None:
    public_connection = ScriptedConnection(select_owned=[[upload_row()]])
    private_connection = ScriptedConnection(select_owned=[[upload_row()]])

    public_record = MenuUploadStore(public_connection).get_owned(
        upload_id="menu-1",
        owner_user_id="user-1",
    )
    private_record = MenuUploadStore(
        private_connection
    ).get_owned_private_record(
        upload_id="menu-1",
        owner_user_id="user-1",
    )

    assert "object_ref" not in public_record
    assert "object_sha256" not in public_record
    assert private_record["object_ref"].startswith("private/")


def test_latest_owned_orders_by_owner_created_and_redacts_object() -> None:
    connection = ScriptedConnection(select_latest_owned=[[upload_row()]])

    record = MenuUploadStore(connection).latest_owned(owner_user_id="user-1")

    assert record is not None
    assert record["id"] == "menu-1"
    assert "object_ref" not in record
    _, sql, parameters = connection.calls[0]
    assert "WHERE owner_user_id = %s" in sql
    assert "status IN ('parsed', 'frozen')" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert parameters == ("user-1",)


def test_latest_owned_returns_none_without_leaking_other_owner() -> None:
    connection = ScriptedConnection(select_latest_owned=[[]])

    record = MenuUploadStore(connection).latest_owned(owner_user_id="user-2")

    assert record is None
    assert connection.commits == 1


def test_mark_frozen_transitions_only_parsed_and_does_not_consume_upload() -> None:
    frozen = upload_row(
        status="frozen",
        frozen_at="2026-07-30T09:00:00Z",
        updated_at="2026-07-30T09:00:00Z",
    )
    connection = ScriptedConnection(mark_frozen=[[frozen]])

    result = MenuUploadStore(connection).mark_frozen(
        upload_id="menu-1",
        owner_user_id="user-1",
    )

    assert result.idempotent is False
    assert result.record["status"] == "frozen"
    assert "object_ref" not in result.record
    _, sql, parameters = connection.calls[0]
    assert "AND status = 'parsed'" in sql
    assert "COALESCE(frozen_at, CURRENT_TIMESTAMP)" in sql
    assert parameters == ("menu-1", "user-1")


def test_mark_frozen_is_idempotent_for_reuse_by_multiple_jobs() -> None:
    frozen = upload_row(
        status="frozen",
        frozen_at="2026-07-30T09:00:00Z",
    )
    connection = ScriptedConnection(
        mark_frozen=[[]],
        select_owned=[[frozen]],
    )

    result = MenuUploadStore(connection).mark_frozen(
        upload_id="menu-1",
        owner_user_id="user-1",
    )

    assert result.idempotent is True
    assert result.record["frozen_at"] == "2026-07-30T09:00:00Z"
    assert connection.commits == 1


def test_failed_upload_cannot_be_frozen() -> None:
    connection = ScriptedConnection(
        mark_frozen=[[]],
        select_owned=[
            [upload_row(status="failed", failure_reason="parser failed")]
        ],
    )

    with pytest.raises(MenuUploadStateConflict, match="only a parsed"):
        MenuUploadStore(connection).mark_frozen(
            upload_id="menu-1",
            owner_user_id="user-1",
        )

    assert connection.rollbacks == 1


def test_mark_failed_transitions_parsed_and_is_idempotent_for_same_reason() -> None:
    failed = upload_row(status="failed", failure_reason="parser failed")
    first_connection = ScriptedConnection(mark_failed=[[failed]])
    repeat_connection = ScriptedConnection(
        mark_failed=[[]],
        select_owned=[[failed]],
    )

    first = MenuUploadStore(first_connection).mark_failed(
        upload_id="menu-1",
        owner_user_id="user-1",
        failure_reason="parser failed",
    )
    repeat = MenuUploadStore(repeat_connection).mark_failed(
        upload_id="menu-1",
        owner_user_id="user-1",
        failure_reason="parser failed",
    )

    assert first.idempotent is False
    assert repeat.idempotent is True
    assert repeat.record["status"] == "failed"
    assert "failure_reason" not in repeat.record
    assert first_connection.calls[0][2] == (
        "parser failed",
        "menu-1",
        "user-1",
    )


@pytest.mark.parametrize(
    "current",
    [
        upload_row(
            status="frozen",
            frozen_at="2026-07-30T09:00:00Z",
        ),
        upload_row(
            status="failed",
            failure_reason="different reason",
        ),
    ],
)
def test_mark_failed_rejects_invalid_state_or_changed_reason(
    current: dict[str, Any],
) -> None:
    connection = ScriptedConnection(
        mark_failed=[[]],
        select_owned=[[current]],
    )

    with pytest.raises(MenuUploadStateConflict):
        MenuUploadStore(connection).mark_failed(
            upload_id="menu-1",
            owner_user_id="user-1",
            failure_reason="parser failed",
        )

    assert connection.rollbacks == 1


def test_database_error_rolls_back_and_closes_cursor() -> None:
    connection = ScriptedConnection(create=[RuntimeError("database unavailable")])

    with pytest.raises(RuntimeError, match="database unavailable"):
        create_private(MenuUploadStore(connection))

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.cursors[0].closed is True


def test_commit_error_rolls_back_and_closes_cursor() -> None:
    connection = CommitFailureConnection(create=[[upload_row()]])

    with pytest.raises(RuntimeError, match="commit failed"):
        create_private(MenuUploadStore(connection))

    assert connection.commits == 1
    assert connection.rollbacks == 1
    assert connection.cursors[0].closed is True


@pytest.mark.parametrize(
    "object_ref",
    [
        "",
        "/private/menu.xlsx",
        "../private/menu.xlsx",
        "private/../menu.xlsx",
        "private//menu.xlsx",
        "private\\menu.xlsx",
        "s3://bucket/menu.xlsx",
        "private/menu name.xlsx",
        "private/menu.xlsx?token=secret",
        "private/menu.xlsx\x00",
    ],
)
def test_rejects_malicious_or_noncanonical_object_keys(
    object_ref: str,
) -> None:
    connection = ScriptedConnection()

    with pytest.raises(InvalidMenuUploadInput, match="object_ref"):
        create_private(MenuUploadStore(connection), object_ref=object_ref)

    assert connection.cursors == []


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        DIGEST_A + "' OR 1=1",
    ],
)
def test_rejects_invalid_sha256(digest: str) -> None:
    with pytest.raises(InvalidMenuUploadInput, match="SHA-256"):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            object_sha256=digest,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_size", -1),
        ("item_count", -1),
        ("file_size", True),
        ("item_count", False),
        ("file_size", "2048"),
        ("item_count", 12.0),
        ("file_size", 2**63),
        ("item_count", 2**31),
    ],
)
def test_rejects_negative_or_coerced_counts(field: str, value: Any) -> None:
    with pytest.raises(InvalidMenuUploadInput):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            **{field: value},
        )


@pytest.mark.parametrize(
    "parser_version",
    ["", "../v1", "v1 beta", "v1/2", "x" * 65],
)
def test_rejects_invalid_parser_versions(parser_version: str) -> None:
    with pytest.raises(InvalidMenuUploadInput, match="parser_version"):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            parser_version=parser_version,
        )


@pytest.mark.parametrize(
    "filename",
    ["", "../menu.xlsx", "folder/menu.xlsx", "folder\\menu.xlsx", "bad\x00.xlsx"],
)
def test_rejects_path_or_control_characters_in_filename(
    filename: str,
) -> None:
    with pytest.raises(InvalidMenuUploadInput, match="original_filename"):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            original_filename=filename,
        )


@pytest.mark.parametrize(
    "content_type",
    [
        "",
        "application",
        "application/vnd.test; charset=utf-8",
        "application/vnd.test\nx-secret: value",
        "../application/xlsx",
    ],
)
def test_rejects_invalid_content_types(content_type: str) -> None:
    with pytest.raises(InvalidMenuUploadInput, match="content_type"):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            content_type=content_type,
        )


@pytest.mark.parametrize(
    "summary",
    [
        [],
        {"objectRef": "private/menu.xlsx"},
        {"nested": {"private_object_key": "private/menu.xlsx"}},
        {"source": "private/menu-uploads/menu-1.xlsx"},
        {"score": float("nan")},
        {"bad": object()},
    ],
)
def test_rejects_invalid_or_private_parsed_summary(summary: Any) -> None:
    with pytest.raises(InvalidMenuUploadInput, match="parsed_summary"):
        create_private(
            MenuUploadStore(ScriptedConnection()),
            parsed_summary=summary,
        )


def test_public_helper_is_allow_listed_even_with_extra_private_columns() -> None:
    record = upload_row(
        database_debug="do-not-leak",
        signing_secret="do-not-leak",
        failure_reason="internal stack trace",
        parsed_summary={
            "rows": 12,
            "objectRef": "private/menu-uploads/menu-1.xlsx",
            "nested": {
                "source": "private/menu-uploads/menu-1.xlsx",
                "category": "盖饭",
            },
        },
    )

    public = public_menu_upload_dto(record)

    assert "object_ref" not in public
    assert "object_sha256" not in public
    assert "owner_user_id" not in public
    assert "database_debug" not in public
    assert "signing_secret" not in public
    assert "failure_reason" not in public
    assert "objectRef" not in public["parsed_summary"]
    assert "source" not in public["parsed_summary"]["nested"]
    assert public["parsed_summary"]["nested"]["category"] == "盖饭"


def test_migration_has_required_columns_constraints_and_indexes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE IF NOT EXISTS product_menu_uploads" in sql
    for column in (
        "id TEXT PRIMARY KEY",
        "owner_user_id TEXT NOT NULL",
        "object_ref TEXT NOT NULL",
        "object_sha256 TEXT NOT NULL",
        "original_filename TEXT NOT NULL",
        "content_type TEXT NOT NULL",
        "file_size BIGINT NOT NULL",
        "parser_version TEXT NOT NULL",
        "item_count INTEGER NOT NULL",
        "store_name TEXT NOT NULL",
        "parsed_summary JSONB NOT NULL",
        "status TEXT NOT NULL",
        "created_at TIMESTAMPTZ NOT NULL",
        "updated_at TIMESTAMPTZ NOT NULL",
        "frozen_at TIMESTAMPTZ",
    ):
        assert column in sql
    assert "object_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "file_size >= 0" in sql
    assert "item_count >= 0" in sql
    assert "status IN ('parsed', 'frozen', 'failed')" in sql
    assert "jsonb_typeof(parsed_summary) = 'object'" in sql
    assert "object_ref !~ '(^|/)[.]{1,2}(/|$)'" in sql
    assert "idx_product_menu_uploads_owner_created" in sql
    assert "(owner_user_id, created_at DESC, id DESC)" in sql


def test_migration_frozen_and_failure_state_constraints_are_explicit() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "status = 'frozen'" in sql
    assert "frozen_at IS NOT NULL" in sql
    assert "status = 'failed'" in sql
    assert "failure_reason <> ''" in sql
    assert "status = 'parsed'" in sql
    assert "frozen_at IS NULL" in sql
