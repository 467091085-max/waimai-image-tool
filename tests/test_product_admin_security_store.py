from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_admin_security_store as security


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT / "migrations" / "011_product_admin_security_postgres.sql"
)
STORE_SOURCE = ROOT / "shared" / "product_admin_security_store.py"
CREATED_AT = "2026-07-30T12:00:00Z"


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
        parameters: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        match = re.search(
            r"/\* product_admin_security_store:([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(
                f"SQL operation has no test marker: {operation}"
            )
        name = match.group(1)
        if "?" in operation:
            raise AssertionError(
                f"{name} contains a SQLite placeholder"
            )
        placeholder_count = operation.count("%s")
        if placeholder_count != len(parameters):
            raise AssertionError(
                f"{name} expected {placeholder_count} SQL parameters, "
                f"received {len(parameters)}"
            )
        self.connection.calls.append(
            (name, operation, tuple(parameters))
        )
        responses = self.connection.responses.get(name)
        if not responses:
            raise AssertionError(
                f"unexpected or exhausted SQL operation: {name}"
            )
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.rows = list(response)
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = list(self.rows)
        self.rows = []
        return rows

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    autocommit = False

    def __init__(
        self,
        *,
        commit_error: BaseException | None = None,
        **responses: list[Any],
    ) -> None:
        self.responses = {
            name: list(items) for name, items in responses.items()
        }
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.cursors: list[ScriptedCursor] = []
        self.commit_error = commit_error

    def cursor(self) -> ScriptedCursor:
        cursor = ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1


class AutocommitConnection(ScriptedConnection):
    autocommit = True


class MissingAutocommitConnection(ScriptedConnection):
    autocommit = None


def admin_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "action_id": "admin-action-1",
        "actor_user_id": "admin.service",
        "action": "settings.changed",
        "target_type": "tenant",
        "target_id": "tenant-1",
        "status": "succeeded",
        "reason": "approved change",
        "metadata": {"scope": "image-generation"},
    }
    values.update(overrides)
    return values


def risk_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "action_id": "risk-action-1",
        "actor_user_id": "risk.service",
        "event_type": "registration.check",
        "subject_type": "user",
        "subject_value": "user-1",
        "decision": "deny",
        "risk_level": "high",
        "deny_reason": "automation threshold exceeded",
        "metadata": {"rule": "registration-v1"},
    }
    values.update(overrides)
    return values


def access_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "action_id": "access-action-1",
        "actor_user_id": "asset.service",
        "request_id": "request-1",
        "asset_id": "asset-1",
        "asset_type": "generated-image",
        "action": "download",
        "allowed": True,
        "user_id": "user-1",
        "agent_id": "",
        "ip": "2001:0db8::1",
        "deny_reason": "",
        "user_agent": "test-client/1.0",
        "metadata": {"route": "objects"},
    }
    values.update(overrides)
    return values


def admin_row(**overrides: Any) -> dict[str, Any]:
    values = admin_values(**overrides)
    payload = security._admin_audit_payload(**values)  # type: ignore[attr-defined]
    return {
        "event_seq": 1,
        **payload,
        "content_sha256": security._content_sha256(  # type: ignore[attr-defined]
            payload
        ),
        "created_at": CREATED_AT,
    }


def risk_row(**overrides: Any) -> dict[str, Any]:
    values = risk_values(**overrides)
    payload = security._risk_decision_payload(  # type: ignore[attr-defined]
        **values
    )
    return {
        "event_seq": 1,
        **payload,
        "content_sha256": security._content_sha256(  # type: ignore[attr-defined]
            payload
        ),
        "created_at": CREATED_AT,
    }


def access_row(**overrides: Any) -> dict[str, Any]:
    values = access_values(**overrides)
    payload = security._asset_access_payload(  # type: ignore[attr-defined]
        **values
    )
    return {
        "event_seq": 1,
        **payload,
        "content_sha256": security._content_sha256(  # type: ignore[attr-defined]
            payload
        ),
        "created_at": CREATED_AT,
    }


def test_migration_is_reentrant_immutable_and_has_required_contract() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert sql.strip().startswith("BEGIN;")
    assert sql.strip().endswith("COMMIT;")
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 3
    assert "product_admin_audit_events" in sql
    assert "product_risk_decisions" in sql
    assert "product_asset_access_events" in sql
    assert sql.count("metadata JSONB") == 3
    assert sql.count("content_sha256 TEXT NOT NULL") == 3
    assert sql.count("BEFORE UPDATE OR DELETE") == 3
    assert sql.count("DROP TRIGGER IF EXISTS") == 3
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "RAISE EXCEPTION '% is immutable'" in sql
    assert re.search(
        r"subject_type,\s+subject_value,\s+created_at DESC",
        sql,
    )
    assert "WHERE NOT allowed" in sql
    assert "char_length(asset_id) BETWEEN 1 AND 512" in sql
    assert "asset_id !~ '(^|/)[.]{1,2}(/|$)'" in sql


def test_store_source_parses_as_python_39() -> None:
    source = STORE_SOURCE.read_text(encoding="utf-8")
    ast.parse(source, filename=str(STORE_SOURCE), feature_version=(3, 9))


@pytest.mark.parametrize(
    "connection_type",
    (AutocommitConnection, MissingAutocommitConnection),
)
def test_store_requires_explicit_autocommit_false(
    connection_type: type[ScriptedConnection],
) -> None:
    with pytest.raises(
        security.InvalidProductAdminSecurityInput,
        match="autocommit=False",
    ):
        security.ProductAdminSecurityStore(connection_type())


def test_caller_owned_cursor_primitive_does_not_commit() -> None:
    row = admin_row()
    connection = ScriptedConnection(insert_admin_audit=[[row]])
    cursor = connection.cursor()

    result = security.record_admin_audit(cursor, **admin_values())

    assert result.created is True
    assert result.record["action_id"] == "admin-action-1"
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert cursor.closed is False


def test_wrapper_uses_explicit_actor_not_metadata_actor() -> None:
    row = admin_row(metadata={"actorUserId": "request-spoof"})
    connection = ScriptedConnection(insert_admin_audit=[[row]])

    result = security.ProductAdminSecurityStore(
        connection
    ).record_admin_audit(
        **admin_values(metadata={"actorUserId": "request-spoof"})
    )

    assert result.record["actor_user_id"] == "admin.service"
    assert result.record["metadata"]["actorUserId"] == "request-spoof"
    _, _, parameters = connection.calls[0]
    assert parameters[1] == "admin.service"
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.cursors[0].closed is True


@pytest.mark.parametrize(
    ("method_name", "insert_name", "select_name", "values", "row"),
    (
        (
            "record_admin_audit",
            "insert_admin_audit",
            "select_admin_audit",
            admin_values(),
            admin_row(),
        ),
        (
            "record_risk_decision",
            "insert_risk_decision",
            "select_risk_decision",
            risk_values(),
            risk_row(),
        ),
        (
            "record_asset_access",
            "insert_asset_access",
            "select_asset_access",
            access_values(),
            access_row(),
        ),
    ),
)
def test_exact_replay_returns_existing_record(
    method_name: str,
    insert_name: str,
    select_name: str,
    values: dict[str, Any],
    row: dict[str, Any],
) -> None:
    connection = ScriptedConnection(
        **{
            insert_name: [[]],
            select_name: [[row]],
        }
    )
    store = security.ProductAdminSecurityStore(connection)

    result = getattr(store, method_name)(**values)

    assert result.created is False
    assert result.record["action_id"] == values["action_id"]
    assert connection.commits == 1
    assert connection.rollbacks == 0


@pytest.mark.parametrize(
    ("method_name", "insert_name", "select_name", "values", "row"),
    (
        (
            "record_admin_audit",
            "insert_admin_audit",
            "select_admin_audit",
            admin_values(),
            admin_row(),
        ),
        (
            "record_risk_decision",
            "insert_risk_decision",
            "select_risk_decision",
            risk_values(),
            risk_row(),
        ),
        (
            "record_asset_access",
            "insert_asset_access",
            "select_asset_access",
            access_values(),
            access_row(),
        ),
    ),
)
def test_same_action_id_with_changed_content_conflicts_and_rolls_back(
    method_name: str,
    insert_name: str,
    select_name: str,
    values: dict[str, Any],
    row: dict[str, Any],
) -> None:
    drifted = {**row, "content_sha256": "f" * 64}
    connection = ScriptedConnection(
        **{
            insert_name: [[]],
            select_name: [[drifted]],
        }
    )
    store = security.ProductAdminSecurityStore(connection)

    with pytest.raises(
        security.ProductAdminSecurityReplayConflict,
        match="replay content changed",
    ):
        getattr(store, method_name)(**values)

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.cursors[0].closed is True


def test_list_risk_decisions_is_bounded_and_order_whitelisted() -> None:
    row = risk_row(
        action_id="risk-action-list",
        subject_type="ip",
        subject_value="2001:db8::2",
        decision="review",
        deny_reason="",
    )
    connection = ScriptedConnection(list_risk_decisions=[[row]])
    store = security.ProductAdminSecurityStore(connection)

    records = store.list_risk_decisions(
        subject_type="ip",
        subject_value="2001:0db8::2",
        decision="review",
        risk_level="high",
        actor_user_id="risk.service",
        order="oldest",
        limit=25,
    )

    assert records == [row]
    name, sql, parameters = connection.calls[0]
    assert name == "list_risk_decisions"
    assert "ORDER BY created_at ASC, event_seq ASC" in sql
    assert parameters[2:4] == ("2001:db8::2", "2001:db8::2")
    assert parameters[-1] == 25

    for invalid_kwargs in (
        {"order": "newest; DROP TABLE product_risk_decisions"},
        {"limit": 501},
        {"subject_type": "cookie"},
        {"subject_value": "user-1"},
        {"decision": "block"},
    ):
        invalid_connection = ScriptedConnection()
        with pytest.raises(security.InvalidProductAdminSecurityInput):
            security.ProductAdminSecurityStore(
                invalid_connection
            ).list_risk_decisions(**invalid_kwargs)
        assert invalid_connection.calls == []
        assert invalid_connection.rollbacks == 1


def test_list_asset_access_filters_are_bounded_and_whitelisted() -> None:
    row = access_row()
    connection = ScriptedConnection(list_asset_access=[[row]])
    store = security.ProductAdminSecurityStore(connection)

    records = store.list_asset_access(
        asset_id="asset-1",
        user_id="user-1",
        actor_user_id="asset.service",
        action="download",
        allowed=True,
        order="newest",
        limit=10,
    )

    assert records == [row]
    _, sql, parameters = connection.calls[0]
    assert "ORDER BY created_at DESC, event_seq DESC" in sql
    assert parameters[-3:-1] == ("allowed", "allowed")
    assert parameters[-1] == 10

    invalid_connection = ScriptedConnection()
    with pytest.raises(
        security.InvalidProductAdminSecurityInput,
        match="boolean or None",
    ):
        security.ProductAdminSecurityStore(
            invalid_connection
        ).list_asset_access(allowed="yes")
    assert invalid_connection.calls == []
    assert invalid_connection.rollbacks == 1


def test_registration_risk_checks_each_subject_latest_decision() -> None:
    user_allow = risk_row(
        action_id="risk-user-allow",
        subject_type="user",
        subject_value="user-1",
        decision="allow",
        deny_reason="",
    )
    ip_deny = risk_row(
        action_id="risk-ip-deny",
        subject_type="ip",
        subject_value="203.0.113.8",
    )
    ip_review = risk_row(
        action_id="risk-ip-review",
        subject_type="ip",
        subject_value="203.0.113.8",
        decision="review",
        deny_reason="",
    )
    connection = ScriptedConnection(
        registration_risk_latest=[
            [user_allow, ip_deny],
            [user_allow, ip_review],
        ]
    )
    store = security.ProductAdminSecurityStore(connection)

    assert store.registration_risk_blocked(
        user_id="user-1",
        ip="203.0.113.8",
    )
    assert not store.registration_risk_blocked(
        user_id="user-1",
        ip="203.0.113.8",
    )
    assert connection.commits == 2


def test_paginated_security_reads_validate_filters_and_object_keys() -> None:
    risk_record = risk_row(
        action_id="risk-page-1",
        subject_type="user",
        subject_value="user-1",
        decision="review",
        deny_reason="",
    )
    access_record = access_row(
        action_id="access-page-1",
        asset_id="generated/private/user-1/image-1.jpg",
    )
    connection = ScriptedConnection(
        page_risk_decisions_count=[[{"total": 1}]],
        page_risk_decisions_rows=[[risk_record]],
        page_asset_access_count=[[{"total": 1}]],
        page_asset_access_rows=[[access_record]],
    )
    store = security.ProductAdminSecurityStore(connection)

    risk_page = store.page_risk_decisions(
        subject_type="user",
        subject_value="user-1",
        event_type="registration.check",
        search="registration",
        created_from="2026-07-30T00:00:00Z",
        sort="riskLevel",
        order="asc",
        limit=25,
        offset=5,
    )
    access_page = store.page_asset_access(
        asset_id="generated/private/user-1/image-1.jpg",
        asset_type="generated-image",
        allowed=True,
        sort="assetId",
        order="desc",
        limit=20,
        offset=3,
    )

    assert risk_page == {
        "items": [risk_record],
        "total": 1,
        "limit": 25,
        "offset": 5,
        "sort": "risk_level",
        "order": "asc",
    }
    assert access_page == {
        "items": [access_record],
        "total": 1,
        "limit": 20,
        "offset": 3,
        "sort": "asset_id",
        "order": "desc",
    }
    assert connection.commits == 2
    assert all("?" not in sql for _, sql, _ in connection.calls)

    invalid = ScriptedConnection()
    with pytest.raises(security.InvalidProductAdminSecurityInput):
        security.ProductAdminSecurityStore(
            invalid
        ).page_asset_access(asset_id="../private/image.jpg")
    assert invalid.calls == []
    assert invalid.rollbacks == 1


def test_summary_wrappers_return_integer_counts() -> None:
    connection = ScriptedConnection(
        summarize_admin=[
            [
                {
                    "total": 4,
                    "succeeded": 2,
                    "failed": 1,
                    "denied": 1,
                }
            ]
        ],
        summarize_risk=[
            [
                {
                    "total": 6,
                    "allow": 2,
                    "deny": 3,
                    "review": 1,
                    "current_denied_subjects": 1,
                }
            ]
        ],
        summarize_access=[
            [
                {
                    "total": 5,
                    "allowed": 3,
                    "denied": 2,
                    "distinct_assets": 2,
                }
            ]
        ],
    )
    store = security.ProductAdminSecurityStore(connection)

    assert store.admin_summary()["failed"] == 1
    assert store.risk_summary()["current_denied_subjects"] == 1
    assert store.access_summary()["distinct_assets"] == 2
    assert connection.commits == 3


def test_invalid_decisions_and_access_invariants_fail_before_sql() -> None:
    cases = (
        (
            "record_risk_decision",
            risk_values(decision="deny", deny_reason=""),
        ),
        (
            "record_asset_access",
            access_values(allowed=True, deny_reason="not allowed"),
        ),
        (
            "record_asset_access",
            access_values(allowed=False, deny_reason=""),
        ),
        (
            "record_asset_access",
            access_values(ip="not-an-ip"),
        ),
    )
    for method_name, values in cases:
        connection = ScriptedConnection()
        with pytest.raises(security.InvalidProductAdminSecurityInput):
            getattr(
                security.ProductAdminSecurityStore(connection),
                method_name,
            )(**values)
        assert connection.calls == []
        assert connection.rollbacks == 1


def test_sql_exception_and_commit_exception_both_roll_back() -> None:
    sql_connection = ScriptedConnection(
        insert_admin_audit=[RuntimeError("database unavailable")]
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        security.ProductAdminSecurityStore(
            sql_connection
        ).record_admin_audit(**admin_values())
    assert sql_connection.commits == 0
    assert sql_connection.rollbacks == 1
    assert sql_connection.cursors[0].closed is True

    commit_connection = ScriptedConnection(
        commit_error=RuntimeError("commit failed"),
        insert_admin_audit=[[admin_row()]],
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        security.ProductAdminSecurityStore(
            commit_connection
        ).record_admin_audit(**admin_values())
    assert commit_connection.commits == 1
    assert commit_connection.rollbacks == 1
    assert commit_connection.cursors[0].closed is True


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_replay_risk_access_and_immutability() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_product_admin_security_{uuid4().hex}"

    admin_connection = psycopg.connect(dsn, autocommit=True)
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin_connection.close()

    connection = psycopg.connect(dsn, autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()

        migration_sql = MIGRATION.read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute(migration_sql)
            cursor.execute(migration_sql)

        store = security.ProductAdminSecurityStore(connection)
        admin_kwargs = admin_values(
            action_id="admin-real-1",
            metadata={"actorUserId": "untrusted-request-value"},
        )
        created = store.record_admin_audit(**admin_kwargs)
        assert created.created is True
        assert created.record["actor_user_id"] == "admin.service"
        replay = store.record_admin_audit(**admin_kwargs)
        assert replay.created is False
        with pytest.raises(security.ProductAdminSecurityReplayConflict):
            store.record_admin_audit(
                **{**admin_kwargs, "reason": "changed replay"}
            )

        def record_risk(
            action_id: str,
            subject_type: str,
            subject_value: str,
            decision: str,
        ) -> None:
            store.record_risk_decision(
                **risk_values(
                    action_id=action_id,
                    subject_type=subject_type,
                    subject_value=subject_value,
                    decision=decision,
                    deny_reason=(
                        "registration denied"
                        if decision == "deny"
                        else ""
                    ),
                )
            )

        record_risk("risk-real-user-deny-1", "user", "user-real-1", "deny")
        record_risk("risk-real-ip-deny-1", "ip", "203.0.113.31", "deny")
        assert store.registration_risk_blocked(
            user_id="user-real-1",
            ip="203.0.113.31",
        )

        record_risk(
            "risk-real-user-allow-1",
            "user",
            "user-real-1",
            "allow",
        )
        assert store.registration_risk_blocked(
            user_id="user-real-1",
            ip="203.0.113.31",
        )

        record_risk(
            "risk-real-ip-review-1",
            "ip",
            "203.0.113.31",
            "review",
        )
        assert not store.registration_risk_blocked(
            user_id="user-real-1",
            ip="203.0.113.31",
        )

        record_risk("risk-real-user-deny-2", "user", "user-real-1", "deny")
        record_risk(
            "risk-real-ip-allow-1",
            "ip",
            "203.0.113.31",
            "allow",
        )
        assert store.registration_risk_blocked(
            user_id="user-real-1",
            ip="203.0.113.31",
        )

        record_risk(
            "risk-real-user-allow-2",
            "user",
            "user-real-1",
            "allow",
        )
        assert not store.registration_risk_blocked(
            user_id="user-real-1",
            ip="203.0.113.31",
        )
        assert len(
            store.list_risk_decisions(
                subject_type="user",
                subject_value="user-real-1",
                order="newest",
                limit=10,
            )
        ) == 4

        access_allowed = access_values(
            action_id="access-real-1",
            request_id="request-real-1",
            asset_id=(
                "generated/customer-previews/v1/"
                "owner/menu/background/image-real-1.jpg"
            ),
            user_id="user-real-1",
            ip="2001:0db8::9",
        )
        assert store.record_asset_access(
            **access_allowed
        ).created is True
        assert store.record_asset_access(
            **access_allowed
        ).created is False
        with pytest.raises(security.ProductAdminSecurityReplayConflict):
            store.record_asset_access(
                **{**access_allowed, "user_agent": "changed-agent"}
            )
        store.record_asset_access(
            **access_values(
                action_id="access-real-2",
                request_id="request-real-2",
                asset_id=access_allowed["asset_id"],
                user_id="user-real-1",
                ip="203.0.113.31",
                allowed=False,
                deny_reason="token replayed",
            )
        )
        denied = store.list_asset_access(
            asset_id=access_allowed["asset_id"],
            allowed=False,
            limit=10,
        )
        assert len(denied) == 1
        assert denied[0]["deny_reason"] == "token replayed"
        access_page = store.page_asset_access(
            asset_id=access_allowed["asset_id"],
            allowed=False,
            limit=10,
            offset=0,
        )
        assert access_page["total"] == 1
        assert access_page["items"][0]["asset_id"] == (
            access_allowed["asset_id"]
        )
        risk_page = store.page_risk_decisions(
            subject_type="user",
            subject_value="user-real-1",
            limit=10,
            offset=0,
        )
        assert risk_page["total"] == 4

        assert store.admin_summary() == {
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "denied": 0,
        }
        assert store.risk_summary() == {
            "total": 7,
            "allow": 3,
            "deny": 3,
            "review": 1,
            "current_denied_subjects": 0,
        }
        assert store.access_summary() == {
            "total": 2,
            "allowed": 1,
            "denied": 1,
            "distinct_assets": 1,
        }

        for operation in (
            """
            UPDATE product_admin_audit_events
            SET reason = 'tampered'
            WHERE action_id = 'admin-real-1'
            """,
            """
            DELETE FROM product_risk_decisions
            WHERE action_id = 'risk-real-user-deny-1'
            """,
            """
            UPDATE product_asset_access_events
            SET allowed = FALSE
            WHERE action_id = 'access-real-1'
            """,
        ):
            with pytest.raises(psycopg.Error) as exc_info:
                with connection.cursor() as cursor:
                    cursor.execute(operation)
            assert exc_info.value.sqlstate == "55000"
            connection.rollback()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM product_admin_audit_events),
                    (SELECT COUNT(*) FROM product_risk_decisions),
                    (SELECT COUNT(*) FROM product_asset_access_events),
                    (
                        SELECT COUNT(*)
                        FROM product_admin_audit_events
                        WHERE content_sha256 ~ '^[0-9a-f]{64}$'
                    )
                """
            )
            counts = cursor.fetchone()
        connection.commit()
        assert counts == (1, 7, 2, 1)
    finally:
        connection.rollback()
        connection.close()
        cleanup_connection = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup_connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
                )
        finally:
            cleanup_connection.close()
