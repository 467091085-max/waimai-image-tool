import json
import sqlite3

import pytest

from admin_actions import (
    admin_audit_event,
    init_admin_actions_schema,
    mark_ai_asset_status,
    record_asset_access,
    record_risk_decision,
    update_commission_status,
)
from storage_db import SCHEMA_SQL


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_record_risk_decision_writes_risk_audit_log() -> None:
    conn = make_conn()

    record = record_risk_decision(
        conn,
        "asset_download",
        "deny",
        user_id="user_1",
        agent_id="agent_1",
        asset_id="asset_1",
        risk_level="high",
        ip="10.0.0.1",
        deny_reason="quota",
        metadata={"rule": "daily_download_limit"},
    )

    row = conn.execute("SELECT * FROM risk_audit_logs WHERE id = ?", (record["id"],)).fetchone()
    assert row["event_type"] == "asset_download"
    assert row["decision"] == "deny"
    assert row["risk_level"] == "high"
    assert row["deny_reason"] == "quota"
    assert json.loads(row["metadata_json"]) == {"rule": "daily_download_limit"}

    with pytest.raises(ValueError):
        record_risk_decision(conn, "asset_download", "blocked")


def test_record_asset_access_writes_access_log() -> None:
    conn = make_conn()

    record = record_asset_access(
        conn,
        "asset_1",
        "download",
        user_id="user_1",
        agent_id="agent_1",
        asset_type="image",
        ip="10.0.0.2",
        allowed=False,
        deny_reason="agent_mismatch",
        request_id="req_1",
        user_agent="pytest",
        metadata={"source": "admin"},
    )

    row = conn.execute("SELECT * FROM asset_access_logs WHERE id = ?", (record["id"],)).fetchone()
    assert row["asset_id"] == "asset_1"
    assert row["action"] == "download"
    assert row["allowed"] == 0
    assert row["deny_reason"] == "agent_mismatch"
    assert row["request_id"] == "req_1"
    assert json.loads(row["metadata_json"]) == {"source": "admin"}


def test_update_commission_status_validates_flow_and_records_audit_metadata() -> None:
    conn = make_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO commission_orders (
                id, order_id, agent_id, customer_id, order_amount, commission_amount,
                commission_rate_bps, status, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "co_1",
                "order_1",
                "agent_1",
                "customer_1",
                10000,
                1000,
                1000,
                "pending",
                json.dumps({"existing": True}),
                "2026-06-28T00:00:00+00:00",
                "2026-06-28T00:00:00+00:00",
            ),
        )

    eligible = update_commission_status(
        conn,
        "co_1",
        "eligible",
        reason="payment confirmed",
        metadata={"reviewer": "admin_1"},
    )
    assert eligible["status"] == "eligible"
    assert eligible["metadata"]["existing"] is True
    assert eligible["metadata"]["reviewer"] == "admin_1"
    assert eligible["metadata"]["status_reason"] == "payment confirmed"
    assert eligible["metadata"]["admin_status_history"][-1]["from"] == "pending"
    assert eligible["metadata"]["admin_status_history"][-1]["to"] == "eligible"

    audit = conn.execute(
        "SELECT * FROM admin_audit_logs WHERE target_type = ? AND target_id = ?",
        ("commission_order", "co_1"),
    ).fetchone()
    assert audit["action"] == "commission_status_updated"
    assert audit["target"] == "commission_order:co_1"
    assert audit["status"] == "succeeded"
    assert audit["reason"] == "payment confirmed"
    assert json.loads(audit["metadata_json"])["toStatus"] == "eligible"

    settled = update_commission_status(conn, "co_1", "settled", reason="settlement paid")
    assert settled["status"] == "settled"
    assert settled["settled_at"]

    with pytest.raises(ValueError):
        update_commission_status(conn, "co_1", "pending")

    with pytest.raises(KeyError):
        update_commission_status(conn, "missing", "eligible")


def test_mark_ai_asset_status_dispatches_repository_methods() -> None:
    repo = FakeAssetRepository()

    assert mark_ai_asset_status(repo, "asset_1", "approved") == {"asset_id": "asset_1", "status": "approved"}
    assert mark_ai_asset_status(repo, "asset_2", "rejected") == {"asset_id": "asset_2", "status": "rejected"}
    assert mark_ai_asset_status(repo, "asset_3", "disabled") == {"asset_id": "asset_3", "status": "disabled"}
    assert mark_ai_asset_status(repo, "asset_4", "pending") == {"asset_id": "asset_4", "status": "pending"}
    assert repo.calls == [
        ("approve", "asset_1"),
        ("reject", "asset_2"),
        ("disable", "asset_3"),
        ("pending", "asset_4"),
    ]

    with pytest.raises(ValueError):
        mark_ai_asset_status(repo, "asset_5", "archived")


def test_admin_audit_event_initializes_schema_and_writes_event() -> None:
    conn = make_conn()
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'admin_audit_logs'"
    ).fetchone() is None

    event = admin_audit_event(
        conn,
        "admin_1",
        "asset_disabled",
        "ai_asset",
        "asset_1",
        metadata={"reason": "watermark"},
        status="succeeded",
        reason="watermark",
    )

    assert event["actor"] == "admin_1"
    assert event["action"] == "asset_disabled"
    assert event["target"] == "ai_asset:asset_1"
    assert event["status"] == "succeeded"
    assert event["reason"] == "watermark"
    assert event["created_at"]

    row = conn.execute("SELECT * FROM admin_audit_logs WHERE id = ?", (event["id"],)).fetchone()
    assert row["actor"] == "admin_1"
    assert row["actor_user_id"] == "admin_1"
    assert row["action"] == "asset_disabled"
    assert row["target"] == "ai_asset:asset_1"
    assert row["target_type"] == "ai_asset"
    assert row["target_id"] == "asset_1"
    assert row["status"] == "succeeded"
    assert row["reason"] == "watermark"
    assert json.loads(row["metadata_json"]) == {"reason": "watermark"}


def test_admin_audit_schema_migrates_legacy_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE admin_audit_logs (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            target_type TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        INSERT INTO admin_audit_logs (
            id, actor_user_id, action, target_type, target_id, metadata_json, created_at
        )
        VALUES ('audit_legacy', 'admin_legacy', 'legacy_action', 'legacy_target', 'target_1', '{}', '2026-06-01T00:00:00+00:00');
        """
    )

    init_admin_actions_schema(conn)

    row = conn.execute("SELECT * FROM admin_audit_logs WHERE id = 'audit_legacy'").fetchone()
    assert row["actor"] == "admin_legacy"
    assert row["target"] == "legacy_target:target_1"
    assert row["status"] == "succeeded"
    assert row["reason"] == ""


class FakeAssetRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def approve(self, asset_id: str) -> dict[str, str]:
        self.calls.append(("approve", asset_id))
        return {"asset_id": asset_id, "status": "approved"}

    def reject(self, asset_id: str) -> dict[str, str]:
        self.calls.append(("reject", asset_id))
        return {"asset_id": asset_id, "status": "rejected"}

    def disable(self, asset_id: str) -> dict[str, str]:
        self.calls.append(("disable", asset_id))
        return {"asset_id": asset_id, "status": "disabled"}

    def pending(self, asset_id: str) -> dict[str, str]:
        self.calls.append(("pending", asset_id))
        return {"asset_id": asset_id, "status": "pending"}
