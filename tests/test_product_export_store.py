from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_export_store as exports


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "010_product_exports_postgres.sql"
RESERVATION_MIGRATION = (
    ROOT
    / "migrations"
    / "014_product_export_nonce_reservations.sql"
)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
AUDIT_SECRET = "0123456789abcdef0123456789abcdef"
TOKEN_NONCE = "private-random-export-nonce"
RESERVATION_ID = "export-reservation-1"
IP_ADDRESS = "203.0.113.19"
EXPIRES_AT = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


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
            r"/\* product_export_store:([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        name = match.group(1)
        if "?" in operation:
            raise AssertionError(f"{name} contains a SQLite placeholder")
        placeholder_count = operation.count("%s")
        if placeholder_count != len(parameters):
            raise AssertionError(
                f"{name} expected {placeholder_count} SQL parameters, "
                f"received {len(parameters)}"
            )
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
        self.responses = {name: list(items) for name, items in responses.items()}
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


def export_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "export_id": "export-1",
        "owner_user_id": "user-1",
        "idempotency_key": "export-request-1",
        "generation_job_id": "job-1",
        "generation_request_sha256": DIGEST_A,
        "export_request_sha256": DIGEST_C,
        "manifest_object_ref": (
            "generated/manifests/job-1/" + DIGEST_A + ".json"
        ),
        "manifest_sha256": DIGEST_B,
        "platforms": ["meituan", "eleme"],
        "watermark": {
            "enabled": True,
            "position": "bottom-right",
            "text": "品牌",
        },
        "zip_object_ref": "exports/job-1/result.zip",
        "zip_sha256": DIGEST_A,
        "zip_size_bytes": 8192,
        "status": "ready",
    }
    values.update(overrides)
    return values


def export_row(**overrides: Any) -> dict[str, Any]:
    normalized = exports._normalize_export_values(  # type: ignore[attr-defined]
        **export_values()
    )
    row = {
        **normalized,
        "platform_set": normalized["platform_set"],
        "watermark": normalized["watermark"],
        "download_count": 0,
        "created_at": "2026-07-30T08:00:00Z",
        "updated_at": "2026-07-30T08:00:00Z",
    }
    row.update(overrides)
    return row


def nonce_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "token_nonce_digest": exports.token_nonce_sha256(TOKEN_NONCE),
        "owner_user_id": "user-1",
        "export_id": "export-1",
        "expires_at": EXPIRES_AT,
        "consumed_at": None,
        "consumed_action_id": None,
        "consumed_request_id": None,
        "reservation_id": None,
        "reserved_at": None,
        "reservation_expires_at": None,
        "created_at": "2026-07-30T08:00:00Z",
        "expired": False,
        "reservation_expired": False,
    }
    row.update(overrides)
    return row


def access_request_digest(
    *,
    action_id: str = "access-action-1",
    request_id: str = "access-request-1",
    operation: str = "consume",
    token_nonce: str | None = TOKEN_NONCE,
    token_expires_at: datetime | None = EXPIRES_AT,
    deny_reason: str = "",
) -> str:
    return exports._access_request_sha256(  # type: ignore[attr-defined]
        action_id=action_id,
        request_id=request_id,
        owner_user_id="user-1",
        export_id="export-1",
        token_nonce_digest=(
            exports.token_nonce_sha256(token_nonce)
            if token_nonce is not None
            else None
        ),
        ip_digest=exports.ip_hmac_sha256(IP_ADDRESS, AUDIT_SECRET),
        token_expires_at=token_expires_at,
        metadata={"route": "objects"},
        operation=operation if not deny_reason else f"deny:{deny_reason}",
    )


def audit_row(
    *,
    action_id: str = "access-action-1",
    request_id: str = "access-request-1",
    allowed: bool = True,
    deny_reason: str = "",
    nonce_consumed: bool = True,
    download_count_after: int = 1,
    request_sha256: str | None = None,
    token_nonce_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "request_id": request_id,
        "owner_user_id": "user-1",
        "export_id": "export-1",
        "token_nonce_digest": (
            exports.token_nonce_sha256(TOKEN_NONCE)
            if token_nonce_digest is None
            else token_nonce_digest
        ),
        "ip_digest": exports.ip_hmac_sha256(IP_ADDRESS, AUDIT_SECRET),
        "allowed": allowed,
        "deny_reason": deny_reason,
        "one_time_required": True,
        "nonce_consumed": nonce_consumed,
        "download_count_after": download_count_after,
        "request_sha256": request_sha256 or access_request_digest(),
        "content_sha256": DIGEST_B,
        "metadata": {"route": "objects"},
        "created_at": "2026-07-30T08:01:00Z",
    }


def consume_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "action_id": "access-action-1",
        "request_id": "access-request-1",
        "owner_user_id": "user-1",
        "export_id": "export-1",
        "token_nonce": TOKEN_NONCE,
        "token_expires_at": EXPIRES_AT,
        "ip_address": IP_ADDRESS,
        "metadata": {"route": "objects"},
    }
    values.update(overrides)
    return values


def store(connection: ScriptedConnection) -> exports.ProductExportStore:
    return exports.ProductExportStore(
        connection,
        audit_digest_secret=AUDIT_SECRET,
    )


def test_migration_is_transactional_reentrant_and_has_required_boundaries() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert sql.strip().startswith("BEGIN;")
    assert sql.strip().endswith("COMMIT;")
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 3
    assert "product_export_packages" in sql
    assert "product_export_token_nonces" in sql
    assert "product_export_access_audits" in sql
    assert "UNIQUE (owner_user_id, idempotency_key)" in sql
    assert "export_request_sha256" in sql
    assert "UNIQUE (token_nonce_digest, export_id, owner_user_id)" in sql
    assert "download_count" in sql
    assert "token_nonce_digest" in sql
    assert "ip_digest" in sql
    assert "raw_token" not in sql
    assert "raw_ip" not in sql


def test_nonce_reservation_migration_is_reentrant_and_bounded() -> None:
    sql = RESERVATION_MIGRATION.read_text(encoding="utf-8")

    assert sql.strip().startswith("BEGIN;")
    assert sql.strip().endswith("COMMIT;")
    assert "ADD COLUMN IF NOT EXISTS reservation_id TEXT" in sql
    assert "reservation_expires_at" in sql
    assert "consumed_at IS NULL" in sql
    assert "ck_product_export_nonce_reservation" in sql


def test_store_rejects_autocommit_connection() -> None:
    with pytest.raises(exports.InvalidProductExportInput, match="autocommit"):
        exports.ProductExportStore(AutocommitConnection())


@pytest.mark.parametrize(
    ("stored_nonce", "expected"),
    (
        (None, exports.NONCE_UNREGISTERED),
        (nonce_row(), exports.NONCE_AVAILABLE),
        (
            nonce_row(
                consumed_at="2026-07-30T08:01:00Z",
                consumed_action_id="access-action-1",
                consumed_request_id="access-request-1",
            ),
            exports.ACCESS_TOKEN_REPLAYED,
        ),
        (nonce_row(expired=True), exports.ACCESS_TOKEN_EXPIRED),
    ),
)
def test_nonce_preflight_status_is_read_only_and_scope_checked(
    stored_nonce: dict[str, Any] | None,
    expected: str,
) -> None:
    connection = ScriptedConnection(
        select_nonce=[[] if stored_nonce is None else [stored_nonce]],
    )

    status = store(connection).nonce_preflight_status(
        owner_user_id="user-1",
        export_id="export-1",
        token_nonce=TOKEN_NONCE,
        token_expires_at=EXPIRES_AT,
    )

    assert status == expected
    assert [call[0] for call in connection.calls] == ["select_nonce"]
    assert connection.commits == 1


def test_nonce_reservation_is_atomic_before_object_download() -> None:
    reserved_row = nonce_row(
        reservation_id=RESERVATION_ID,
        reserved_at="2026-07-30T08:00:30Z",
        reservation_expires_at=EXPIRES_AT,
    )
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row()]],
        insert_nonce=[[nonce_row()]],
        reserve_nonce=[[reserved_row]],
    )

    status = store(connection).reserve_nonce(
        owner_user_id="user-1",
        export_id="export-1",
        token_nonce=TOKEN_NONCE,
        token_expires_at=EXPIRES_AT,
        reservation_id=RESERVATION_ID,
    )

    assert status == exports.NONCE_RESERVED
    assert [call[0] for call in connection.calls] == [
        "select_owned_export_for_update",
        "insert_nonce",
        "reserve_nonce",
    ]
    assert connection.calls[-1][2][0] == RESERVATION_ID


def test_concurrent_active_nonce_reservation_is_rejected_without_download() -> None:
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row()]],
        insert_nonce=[[]],
        select_nonce_for_update=[
            [
                nonce_row(
                    reservation_id="other-reservation",
                    reserved_at="2026-07-30T08:00:30Z",
                    reservation_expires_at=EXPIRES_AT,
                )
            ]
        ],
    )

    status = store(connection).reserve_nonce(
        owner_user_id="user-1",
        export_id="export-1",
        token_nonce=TOKEN_NONCE,
        token_expires_at=EXPIRES_AT,
        reservation_id=RESERVATION_ID,
    )

    assert status == exports.ACCESS_TOKEN_IN_USE
    assert "reserve_nonce" not in [call[0] for call in connection.calls]


def test_failed_object_preflight_releases_own_nonce_reservation() -> None:
    connection = ScriptedConnection(
        release_nonce_reservation=[[nonce_row()]],
    )

    released = store(connection).release_nonce_reservation(
        owner_user_id="user-1",
        export_id="export-1",
        token_nonce=TOKEN_NONCE,
        token_expires_at=EXPIRES_AT,
        reservation_id=RESERVATION_ID,
    )

    assert released is True
    assert connection.calls[0][0] == "release_nonce_reservation"
    assert RESERVATION_ID in connection.calls[0][2]


def test_create_wrapper_commits_and_public_dto_hides_private_pointers() -> None:
    connection = ScriptedConnection(create_export=[[export_row()]])

    result = store(connection).create_or_get(**export_values())

    assert result.created is True
    assert result.record["id"] == "export-1"
    assert result.record["platform_set"] == ["eleme", "meituan"]
    assert "owner_user_id" not in result.record
    assert "manifest_object_ref" not in result.record
    assert "zip_object_ref" not in result.record
    assert "content_sha256" not in result.record
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.cursors[0].closed is True
    name, sql, parameters = connection.calls[0]
    assert name == "create_export"
    assert "ON CONFLICT DO NOTHING" in sql
    assert parameters[8] == '["eleme","meituan"]'


def test_external_cursor_create_does_not_commit_callers_transaction() -> None:
    connection = ScriptedConnection(create_export=[[export_row()]])
    cursor = connection.cursor()

    result = exports.create_export_package(cursor, **export_values())

    assert result.created is True
    assert result.record["zip_object_ref"] == "exports/job-1/result.zip"
    assert connection.commits == 0
    assert connection.rollbacks == 0


def test_exact_create_replay_is_idempotent() -> None:
    connection = ScriptedConnection(
        create_export=[[]],
        select_export_by_id_for_update=[[export_row()]],
    )

    result = store(connection).create_or_get_private_record(**export_values())

    assert result.created is False
    assert result.record["content_sha256"] == export_row()["content_sha256"]
    assert [call[0] for call in connection.calls] == [
        "create_export",
        "select_export_by_id_for_update",
    ]
    assert connection.commits == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"owner_user_id": "user-2"},
        {"generation_request_sha256": DIGEST_B},
        {"export_request_sha256": DIGEST_B},
        {"platforms": ["jd"]},
        {"watermark": {"enabled": False}},
    ],
)
def test_create_replay_with_drift_rolls_back(changed: dict[str, Any]) -> None:
    connection = ScriptedConnection(
        create_export=[[]],
        select_export_by_id_for_update=[[export_row()]],
    )

    with pytest.raises(exports.ProductExportConflict):
        store(connection).create_or_get_private_record(
            **export_values(**changed)
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_semantic_replay_accepts_a_different_zip_attempt() -> None:
    connection = ScriptedConnection(
        create_export=[[]],
        select_export_by_id_for_update=[[export_row()]],
    )

    result = store(connection).create_or_get_private_record(
        **export_values(
            zip_object_ref="exports/job-1/retry.zip",
            zip_sha256=DIGEST_B,
            zip_size_bytes=8193,
        )
    )

    assert result.created is False
    assert result.record["zip_object_ref"] == "exports/job-1/result.zip"
    assert connection.commits == 1


def test_idempotency_key_collision_with_another_id_conflicts() -> None:
    connection = ScriptedConnection(
        create_export=[[]],
        select_export_by_id_for_update=[[]],
        select_export_by_idempotency_for_update=[[export_row()]],
    )

    with pytest.raises(exports.ProductExportConflict):
        store(connection).create_or_get_private_record(
            **export_values(export_id="export-2")
        )

    assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("export_id", ""),
        ("owner_user_id", "user/1"),
        ("generation_request_sha256", "not-a-digest"),
        ("export_request_sha256", "not-a-digest"),
        ("manifest_object_ref", "../manifest.json"),
        ("manifest_object_ref", "exports/manifest.json"),
        ("zip_object_ref", "generated/result.zip"),
        ("zip_size_bytes", -1),
        ("platforms", []),
        ("platforms", ["MEITUAN", "../jd"]),
        ("watermark", []),
    ],
)
def test_create_validates_frozen_export_fields(field: str, value: Any) -> None:
    with pytest.raises(exports.InvalidProductExportInput):
        exports.create_export_package(
            ScriptedConnection().cursor(),
            **export_values(**{field: value}),
        )


def test_owner_scoped_get_and_list_do_not_return_cross_user_records() -> None:
    get_connection = ScriptedConnection(select_owned_export=[[]])
    with pytest.raises(exports.ProductExportNotFound) as error:
        store(get_connection).get_owned(
            export_id="export-1",
            owner_user_id="user-2",
        )
    assert str(error.value) == "export-1"

    list_connection = ScriptedConnection(list_owned_exports=[[]])
    assert (
        store(list_connection).list_owned(owner_user_id="user-2") == []
    )
    assert list_connection.calls[0][2][0] == "user-2"

    with pytest.raises(exports.InvalidProductExportInput):
        exports.list_owned_exports(
            ScriptedConnection().cursor(),
            owner_user_id="",
        )


def test_private_owner_get_is_explicit() -> None:
    public_connection = ScriptedConnection(
        select_owned_export=[[export_row()]]
    )
    private_connection = ScriptedConnection(
        select_owned_export=[[export_row()]]
    )

    public = store(public_connection).get_owned(
        export_id="export-1",
        owner_user_id="user-1",
    )
    private = store(private_connection).get_owned_private_record(
        export_id="export-1",
        owner_user_id="user-1",
    )

    assert "zip_object_ref" not in public
    assert private["zip_object_ref"] == "exports/job-1/result.zip"


def test_owner_scoped_idempotency_lookup_supports_safe_preflight() -> None:
    hit = ScriptedConnection(
        select_owned_export_by_idempotency=[[export_row()]]
    )
    miss = ScriptedConnection(
        select_owned_export_by_idempotency=[[]]
    )

    private = store(hit).get_owned_private_record_by_idempotency(
        owner_user_id="user-1",
        idempotency_key="export-request-1",
    )
    absent = store(miss).get_owned_private_record_by_idempotency(
        owner_user_id="user-2",
        idempotency_key="export-request-1",
    )

    assert private is not None
    assert private["zip_object_ref"] == "exports/job-1/result.zip"
    assert absent is None
    assert hit.calls[0][2] == ("user-1", "export-request-1")


def test_successful_nonce_consumption_is_atomic_and_never_sends_raw_values() -> None:
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row()]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_nonce=[[nonce_row()]],
        consume_nonce=[
            [
                nonce_row(
                    consumed_at="2026-07-30T08:01:00Z",
                    consumed_action_id="access-action-1",
                    consumed_request_id="access-request-1",
                )
            ]
        ],
        increment_download_count=[[export_row(download_count=1)]],
        insert_access=[[audit_row()]],
    )

    result = store(connection).consume_nonce(**consume_values())

    assert result.allowed is True
    assert result.reason == exports.ACCESS_ALLOWED
    assert result.nonce_status == "consumed"
    assert result.download_count == 1
    assert result.idempotent is False
    assert [call[0] for call in connection.calls] == [
        "select_owned_export_for_update",
        "select_access_by_action_for_update",
        "select_access_by_request_for_update",
        "insert_nonce",
        "consume_nonce",
        "increment_download_count",
        "insert_access",
    ]
    parameters = repr([call[2] for call in connection.calls])
    assert TOKEN_NONCE not in parameters
    assert IP_ADDRESS not in parameters
    assert exports.token_nonce_sha256(TOKEN_NONCE) in parameters
    assert exports.ip_hmac_sha256(IP_ADDRESS, AUDIT_SECRET) in parameters
    assert connection.commits == 1


def test_exact_access_request_replay_is_idempotent_without_second_consume() -> None:
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row(download_count=1)]],
        select_access_by_action_for_update=[[audit_row()]],
    )

    result = store(connection).consume_nonce(**consume_values())

    assert result.allowed is True
    assert result.idempotent is True
    assert result.download_count == 1
    assert [call[0] for call in connection.calls] == [
        "select_owned_export_for_update",
        "select_access_by_action_for_update",
    ]


def test_same_nonce_second_action_is_denied_without_increment() -> None:
    action = "access-action-2"
    request = "access-request-2"
    request_digest = access_request_digest(
        action_id=action,
        request_id=request,
    )
    denied_audit = audit_row(
        action_id=action,
        request_id=request,
        allowed=False,
        deny_reason=exports.ACCESS_TOKEN_REPLAYED,
        nonce_consumed=False,
        request_sha256=request_digest,
    )
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row(download_count=1)]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_nonce=[[]],
        select_nonce_for_update=[
            [
                nonce_row(
                    consumed_at="2026-07-30T08:01:00Z",
                    consumed_action_id="access-action-1",
                    consumed_request_id="access-request-1",
                )
            ]
        ],
        insert_access=[[denied_audit]],
    )

    result = store(connection).consume_nonce(
        **consume_values(action_id=action, request_id=request)
    )

    assert result.allowed is False
    assert result.reason == exports.ACCESS_TOKEN_REPLAYED
    assert result.nonce_status == "replayed"
    assert result.download_count == 1
    assert "increment_download_count" not in [
        call[0] for call in connection.calls
    ]


def test_expired_nonce_is_audited_and_not_consumed() -> None:
    denied_audit = audit_row(
        allowed=False,
        deny_reason=exports.ACCESS_TOKEN_EXPIRED,
        nonce_consumed=False,
        download_count_after=0,
    )
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row()]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_nonce=[[nonce_row(expired=True)]],
        insert_access=[[denied_audit]],
    )

    result = store(connection).consume_nonce(**consume_values())

    assert result.allowed is False
    assert result.reason == exports.ACCESS_TOKEN_EXPIRED
    assert result.nonce_status == "expired"
    assert "consume_nonce" not in [call[0] for call in connection.calls]
    assert "increment_download_count" not in [
        call[0] for call in connection.calls
    ]


def test_non_ready_export_does_not_consume_nonce() -> None:
    denied_audit = audit_row(
        allowed=False,
        deny_reason=exports.ACCESS_EXPORT_NOT_READY,
        nonce_consumed=False,
        download_count_after=0,
    )
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row(status="failed")]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_nonce=[[nonce_row()]],
        insert_access=[[denied_audit]],
    )

    result = store(connection).consume_nonce(**consume_values())

    assert result.allowed is False
    assert result.reason == exports.ACCESS_EXPORT_NOT_READY
    assert "consume_nonce" not in [call[0] for call in connection.calls]


def test_nonce_scope_or_expiry_drift_fails_closed() -> None:
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row()]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_nonce=[[]],
        select_nonce_for_update=[
            [nonce_row(owner_user_id="user-2")]
        ],
    )

    with pytest.raises(exports.ProductExportAccessConflict, match="scope"):
        store(connection).consume_nonce(**consume_values())

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_access_identifier_drift_fails_before_nonce_mutation() -> None:
    existing = audit_row(
        request_sha256="f" * 64,
    )
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row(download_count=1)]],
        select_access_by_action_for_update=[[existing]],
    )

    with pytest.raises(exports.ProductExportAccessConflict, match="changed"):
        store(connection).consume_nonce(**consume_values())

    assert "insert_nonce" not in [call[0] for call in connection.calls]
    assert connection.rollbacks == 1


def test_pre_verification_denial_supports_missing_nonce_without_raw_ip() -> None:
    reason = "missing_token"
    request_digest = access_request_digest(
        operation=f"deny:{reason}",
        token_nonce=None,
        token_expires_at=None,
    )
    denied_audit = audit_row(
        allowed=False,
        deny_reason=reason,
        nonce_consumed=False,
        download_count_after=4,
        request_sha256=request_digest,
        token_nonce_digest=None,
    )
    denied_audit["token_nonce_digest"] = None
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row(download_count=4)]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_access=[[denied_audit]],
    )

    result = store(connection).record_denial(
        action_id="access-action-1",
        request_id="access-request-1",
        owner_user_id="user-1",
        export_id="export-1",
        ip_address=IP_ADDRESS,
        deny_reason=reason,
        metadata={"route": "objects"},
    )

    assert result.allowed is False
    assert result.reason == reason
    assert result.download_count == 4
    parameters = repr([call[2] for call in connection.calls])
    assert IP_ADDRESS not in parameters


@pytest.mark.parametrize(
    "metadata",
    [
        {"token": TOKEN_NONCE},
        {"nested": {"nonce": TOKEN_NONCE}},
        {"clientIp": IP_ADDRESS},
        {"note": TOKEN_NONCE},
        {"note": IP_ADDRESS},
    ],
)
def test_audit_metadata_rejects_raw_secrets(
    metadata: dict[str, Any],
) -> None:
    connection = ScriptedConnection()

    with pytest.raises(exports.InvalidProductExportInput, match="metadata"):
        store(connection).consume_nonce(
            **consume_values(metadata=metadata)
        )

    assert connection.calls == []


def test_access_audit_reads_are_owner_scoped() -> None:
    get_connection = ScriptedConnection(select_owned_access=[[]])
    with pytest.raises(exports.ProductExportNotFound):
        store(get_connection).get_owned_audit(
            action_id="access-action-1",
            owner_user_id="user-2",
            export_id="export-1",
        )

    list_connection = ScriptedConnection(list_owned_access=[[]])
    assert (
        store(list_connection).list_owned_audits(owner_user_id="user-2")
        == []
    )
    assert list_connection.calls[0][2][0] == "user-2"


def test_wrapper_rolls_back_when_access_audit_insert_conflicts() -> None:
    connection = ScriptedConnection(
        select_owned_export_for_update=[[export_row()]],
        select_access_by_action_for_update=[[]],
        select_access_by_request_for_update=[[]],
        insert_nonce=[[nonce_row()]],
        consume_nonce=[
            [
                nonce_row(
                    consumed_at="2026-07-30T08:01:00Z",
                    consumed_action_id="access-action-1",
                    consumed_request_id="access-request-1",
                )
            ]
        ],
        increment_download_count=[[export_row(download_count=1)]],
        insert_access=[[]],
    )

    with pytest.raises(exports.ProductExportAccessConflict):
        store(connection).consume_nonce(**consume_values())

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_commit_failure_rolls_back_and_closes_cursor() -> None:
    connection = ScriptedConnection(
        commit_error=RuntimeError("commit failed"),
        create_export=[[export_row()]],
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        store(connection).create_or_get(**export_values())

    assert connection.commits == 1
    assert connection.rollbacks == 1
    assert connection.cursors[0].closed is True


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_export_protocol_and_concurrent_nonce_consumption() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_product_exports_{uuid4().hex}"

    admin_connection = psycopg.connect(dsn, autocommit=True)
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin_connection.close()

    def connect() -> Any:
        connection = psycopg.connect(dsn, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    connection = connect()
    try:
        migration_sql = MIGRATION.read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute(migration_sql)
            cursor.execute(migration_sql)
            reservation_migration_sql = (
                RESERVATION_MIGRATION.read_text(encoding="utf-8")
            )
            cursor.execute(reservation_migration_sql)
            cursor.execute(reservation_migration_sql)

        product_store = exports.ProductExportStore(
            connection,
            audit_digest_secret=AUDIT_SECRET,
        )
        created = product_store.create_or_get_private_record(
            **export_values(
                export_id="export-real-1",
                owner_user_id="user-real-1",
                idempotency_key="export-real-request-1",
                generation_job_id="job-real-1",
                manifest_object_ref=(
                    "generated/manifests/job-real-1/"
                    + DIGEST_A
                    + ".json"
                ),
                zip_object_ref="exports/job-real-1/result.zip",
            )
        )
        assert created.created is True
        replay = product_store.create_or_get_private_record(
            **export_values(
                export_id="export-real-1",
                owner_user_id="user-real-1",
                idempotency_key="export-real-request-1",
                generation_job_id="job-real-1",
                manifest_object_ref=(
                    "generated/manifests/job-real-1/"
                    + DIGEST_A
                    + ".json"
                ),
                zip_object_ref="exports/job-real-1/result.zip",
            )
        )
        assert replay.created is False

        concurrent_nonce = "real-private-export-nonce"
        concurrent_ip = "198.51.100.27"
        concurrent_expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, Any]] = []

        def consume(index: int) -> None:
            race_connection = connect()
            try:
                barrier.wait(timeout=10)
                result = exports.ProductExportStore(
                    race_connection,
                    audit_digest_secret=AUDIT_SECRET,
                ).consume_nonce(
                    action_id=f"access-real-action-{index}",
                    request_id=f"access-real-request-{index}",
                    owner_user_id="user-real-1",
                    export_id="export-real-1",
                    token_nonce=concurrent_nonce,
                    token_expires_at=concurrent_expiry,
                    ip_address=concurrent_ip,
                    metadata={"route": "objects"},
                )
                outcomes.append(("success", result))
            except Exception as exc:  # noqa: BLE001 - assertion inspects type
                outcomes.append(("error", exc))
            finally:
                race_connection.close()

        threads = [
            threading.Thread(target=consume, args=(index,))
            for index in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        errors = [value for kind, value in outcomes if kind == "error"]
        results = [value for kind, value in outcomes if kind == "success"]
        assert errors == []
        assert len(results) == 2
        assert sum(result.allowed for result in results) == 1
        assert sorted(result.reason for result in results) == [
            exports.ACCESS_ALLOWED,
            exports.ACCESS_TOKEN_REPLAYED,
        ]

        private_record = product_store.get_owned_private_record(
            export_id="export-real-1",
            owner_user_id="user-real-1",
        )
        assert private_record["download_count"] == 1
        with pytest.raises(exports.ProductExportNotFound):
            product_store.get_owned_private_record(
                export_id="export-real-1",
                owner_user_id="user-real-2",
            )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT token_nonce_digest, owner_user_id, export_id,
                       consumed_at, consumed_action_id, consumed_request_id
                FROM product_export_token_nonces
                """
            )
            nonce_records = cursor.fetchall()
            cursor.execute(
                """
                SELECT token_nonce_digest, ip_digest, allowed,
                       deny_reason, download_count_after
                FROM product_export_access_audits
                ORDER BY created_at, action_id
                """
            )
            audit_records = cursor.fetchall()
        connection.commit()

        assert len(nonce_records) == 1
        assert nonce_records[0][0] == exports.token_nonce_sha256(
            concurrent_nonce
        )
        assert nonce_records[0][0] != concurrent_nonce
        assert nonce_records[0][3] is not None
        assert len(audit_records) == 2
        assert {row[2] for row in audit_records} == {True, False}
        assert {row[4] for row in audit_records} == {1}
        assert all(row[0] != concurrent_nonce for row in audit_records)
        assert all(row[1] != concurrent_ip for row in audit_records)
        assert {
            row[1] for row in audit_records
        } == {exports.ip_hmac_sha256(concurrent_ip, AUDIT_SECRET)}
    finally:
        connection.rollback()
        connection.close()
        cleanup_connection = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup_connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cleanup_connection.close()
