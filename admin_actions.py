from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from inspect import Parameter, signature
from typing import Any

from storage_db import json_dumps, json_loads, new_id, utc_now


RISK_LEVELS = {"info", "low", "medium", "high", "critical"}
RISK_DECISIONS = {"allow", "deny", "review"}

COMMISSION_ORDER_STATUSES = {"pending", "eligible", "settled", "canceled", "refunded"}
VALID_COMMISSION_TRANSITIONS = {
    "pending": {"pending", "eligible", "canceled", "refunded"},
    "eligible": {"eligible", "settled", "canceled", "refunded"},
    "settled": {"settled", "refunded"},
    "canceled": {"canceled"},
    "refunded": {"refunded"},
}

AI_ASSET_STATUS_METHODS = {
    "approved": ("approve", "approved"),
    "rejected": ("reject", "rejected"),
    "disabled": ("disable", "disabled"),
    "pending": ("pending", "mark_pending"),
}

ADMIN_ACTIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL DEFAULT '',
    actor_user_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'succeeded',
    reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_actor_created
    ON admin_audit_logs (actor_user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_action_created
    ON admin_audit_logs (action, created_at);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_target_created
    ON admin_audit_logs (target_type, target_id, created_at);
"""


def init_admin_actions_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create action-layer tables not owned by the core storage schema."""
    with conn:
        conn.executescript(ADMIN_ACTIONS_SCHEMA_SQL)
        _ensure_admin_audit_columns(conn)
        conn.execute(
            """
            UPDATE admin_audit_logs
            SET actor = actor_user_id
            WHERE actor = '' AND actor_user_id <> ''
            """
        )
        conn.execute(
            """
            UPDATE admin_audit_logs
            SET target = CASE
                WHEN target_type <> '' AND target_id <> '' THEN target_type || ':' || target_id
                ELSE target_id
            END
            WHERE target = ''
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_actor_alias_created
                ON admin_audit_logs (actor, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_status_created
                ON admin_audit_logs (status, created_at)
            """
        )
    return conn


def record_risk_decision(
    conn: sqlite3.Connection,
    event_type: str,
    decision: str = "allow",
    *,
    user_id: str = "",
    agent_id: str = "",
    asset_id: str = "",
    risk_level: str = "info",
    ip: str = "",
    deny_reason: str = "",
    metadata: Mapping[str, Any] | None = None,
    log_id: str | None = None,
) -> dict[str, Any]:
    clean_event_type = _required_text(event_type, "event_type")
    clean_decision = _normalize_choice(decision, RISK_DECISIONS, "risk decision")
    clean_risk_level = _normalize_choice(risk_level, RISK_LEVELS, "risk level")
    now = utc_now()
    record = {
        "id": log_id or new_id("risk"),
        "user_id": str(user_id or ""),
        "agent_id": str(agent_id or ""),
        "asset_id": str(asset_id or ""),
        "event_type": clean_event_type,
        "risk_level": clean_risk_level,
        "decision": clean_decision,
        "ip": str(ip or ""),
        "deny_reason": str(deny_reason or ""),
        "metadata": dict(metadata or {}),
        "created_at": now,
    }

    with conn:
        conn.execute(
            """
            INSERT INTO risk_audit_logs (
                id, user_id, agent_id, asset_id, event_type, risk_level, decision,
                ip, deny_reason, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["user_id"],
                record["agent_id"],
                record["asset_id"],
                record["event_type"],
                record["risk_level"],
                record["decision"],
                record["ip"],
                record["deny_reason"],
                json_dumps(record["metadata"]),
                record["created_at"],
            ),
        )
    return record


def record_asset_access(
    conn: sqlite3.Connection,
    asset_id: str,
    action: str,
    *,
    user_id: str = "",
    agent_id: str = "",
    asset_type: str = "",
    ip: str = "",
    allowed: bool = True,
    deny_reason: str = "",
    request_id: str = "",
    user_agent: str = "",
    metadata: Mapping[str, Any] | None = None,
    log_id: str | None = None,
) -> dict[str, Any]:
    clean_asset_id = _required_text(asset_id, "asset_id")
    clean_action = _required_text(action, "action")
    now = utc_now()
    record = {
        "id": log_id or new_id("access"),
        "user_id": str(user_id or ""),
        "agent_id": str(agent_id or ""),
        "asset_id": clean_asset_id,
        "asset_type": str(asset_type or ""),
        "action": clean_action,
        "ip": str(ip or ""),
        "allowed": bool(allowed),
        "deny_reason": str(deny_reason or ""),
        "request_id": str(request_id or ""),
        "user_agent": str(user_agent or ""),
        "metadata": dict(metadata or {}),
        "created_at": now,
    }

    with conn:
        conn.execute(
            """
            INSERT INTO asset_access_logs (
                id, user_id, agent_id, asset_id, asset_type, action, ip, allowed,
                deny_reason, request_id, user_agent, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["user_id"],
                record["agent_id"],
                record["asset_id"],
                record["asset_type"],
                record["action"],
                record["ip"],
                1 if record["allowed"] else 0,
                record["deny_reason"],
                record["request_id"],
                record["user_agent"],
                json_dumps(record["metadata"]),
                record["created_at"],
            ),
        )
    return record


def update_commission_status(
    conn: sqlite3.Connection,
    commission_order_id: str,
    status: str,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
    *,
    actor_user_id: str = "",
) -> dict[str, Any]:
    clean_order_id = _required_text(commission_order_id, "commission_order_id")
    clean_status = _normalize_choice(status, COMMISSION_ORDER_STATUSES, "commission status")
    current = _one_dict(conn, "SELECT * FROM commission_orders WHERE id = ?", (clean_order_id,))
    if current is None:
        raise KeyError(f"commission order not found: {clean_order_id}")

    current_status = str(current["status"])
    allowed = VALID_COMMISSION_TRANSITIONS[current_status]
    if clean_status not in allowed:
        raise ValueError(f"invalid commission status transition: {current_status} -> {clean_status}")

    init_admin_actions_schema(conn)
    now = utc_now()
    merged_metadata = _commission_metadata(
        current.get("metadata_json"),
        from_status=current_status,
        to_status=clean_status,
        reason=reason,
        metadata=metadata,
        changed_at=now,
    )
    settled_at = current.get("settled_at")
    if clean_status == "settled" and not settled_at:
        settled_at = now

    audit_metadata = {
        "fromStatus": current_status,
        "toStatus": clean_status,
        "reason": str(reason or ""),
        "metadata": dict(metadata or {}),
    }
    with conn:
        conn.execute(
            """
            UPDATE commission_orders
            SET status = ?, metadata_json = ?, updated_at = ?, settled_at = ?
            WHERE id = ?
            """,
            (clean_status, json_dumps(merged_metadata), now, settled_at, clean_order_id),
        )
        _insert_admin_audit_event(
            conn,
            actor_user_id=actor_user_id,
            action="commission_status_updated",
            target_type="commission_order",
            target_id=clean_order_id,
            status="succeeded",
            reason=reason,
            metadata=audit_metadata,
            created_at=now,
        )

    updated = _one_dict(conn, "SELECT * FROM commission_orders WHERE id = ?", (clean_order_id,))
    return _commission_order_from_row(updated)


def mark_ai_asset_status(repo: Any, asset_id: str, status: str, *, quality_note: str = "") -> dict[str, Any]:
    clean_asset_id = _required_text(asset_id, "asset_id")
    clean_status = _normalize_choice(status, set(AI_ASSET_STATUS_METHODS), "AI asset status")
    clean_quality_note = str(quality_note or "").strip()

    for method_name in AI_ASSET_STATUS_METHODS[clean_status]:
        method = getattr(repo, method_name, None)
        if callable(method):
            return _call_ai_asset_status_method(method, clean_asset_id, clean_quality_note)

    mark_status = getattr(repo, "mark_status", None)
    if callable(mark_status):
        return _call_ai_asset_mark_status(mark_status, clean_asset_id, clean_status, clean_quality_note)

    raise AttributeError(f"repository does not support AI asset status: {clean_status}")


def _call_ai_asset_status_method(method: Any, asset_id: str, quality_note: str) -> dict[str, Any]:
    if quality_note and _accepts_keyword(method, "quality_note"):
        return method(asset_id, quality_note=quality_note)
    return method(asset_id)


def _call_ai_asset_mark_status(method: Any, asset_id: str, status: str, quality_note: str) -> dict[str, Any]:
    if quality_note and _accepts_keyword(method, "quality_note"):
        return method(asset_id, status, quality_note=quality_note)
    return method(asset_id, status)


def _accepts_keyword(method: Any, keyword: str) -> bool:
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == Parameter.VAR_KEYWORD or parameter.name == keyword for parameter in parameters)


def admin_audit_event(
    conn: sqlite3.Connection,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    status: str = "succeeded",
    reason: str = "",
) -> dict[str, Any]:
    init_admin_actions_schema(conn)
    with conn:
        return _insert_admin_audit_event(
            conn,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            status=status,
            reason=reason,
            metadata=dict(metadata or {}),
            created_at=utc_now(),
        )


def _insert_admin_audit_event(
    conn: sqlite3.Connection,
    *,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    status: str,
    reason: str,
    metadata: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    clean_target_type = _required_text(target_type, "target_type")
    clean_target_id = _required_text(target_id, "target_id")
    target = f"{clean_target_type}:{clean_target_id}"
    actor = str(actor_user_id or "")
    record = {
        "id": new_id("adminaudit"),
        "actor": actor,
        "actor_user_id": actor,
        "actorUserId": actor,
        "action": _required_text(action, "action"),
        "target": target,
        "target_type": clean_target_type,
        "target_id": clean_target_id,
        "targetType": clean_target_type,
        "targetId": clean_target_id,
        "status": _required_text(status, "status"),
        "reason": str(reason or ""),
        "metadata": dict(metadata or {}),
        "created_at": created_at,
        "createdAt": created_at,
    }
    conn.execute(
        """
        INSERT INTO admin_audit_logs (
            id, actor, actor_user_id, action, target, target_type, target_id,
            status, reason, metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["id"],
            record["actor"],
            record["actor_user_id"],
            record["action"],
            record["target"],
            record["target_type"],
            record["target_id"],
            record["status"],
            record["reason"],
            json_dumps(record["metadata"]),
            record["created_at"],
        ),
    )
    return record


def _ensure_admin_audit_columns(conn: sqlite3.Connection) -> None:
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(admin_audit_logs)").fetchall()
    }
    required_columns = {
        "actor": "TEXT NOT NULL DEFAULT ''",
        "target": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'succeeded'",
        "reason": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in required_columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE admin_audit_logs ADD COLUMN {column} {definition}")


def _commission_metadata(
    raw_metadata: str | None,
    *,
    from_status: str,
    to_status: str,
    reason: str,
    metadata: Mapping[str, Any] | None,
    changed_at: str,
) -> dict[str, Any]:
    existing = json_loads(raw_metadata, {})
    if not isinstance(existing, dict):
        existing = {}
    merged = dict(existing)
    if metadata:
        merged.update(dict(metadata))

    history = merged.get("admin_status_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": from_status,
            "to": to_status,
            "reason": str(reason or ""),
            "at": changed_at,
            "metadata": dict(metadata or {}),
        }
    )
    merged["admin_status_history"] = history
    if reason:
        merged["status_reason"] = str(reason)
    return merged


def _commission_order_from_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        raise KeyError("commission order not found")
    data = dict(row)
    data["metadata"] = json_loads(str(data.pop("metadata_json", "") or ""), {})
    return data


def _one_dict(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _normalize_choice(value: Any, allowed: set[str], label: str) -> str:
    clean_value = str(value or "").strip().lower()
    if clean_value not in allowed:
        raise ValueError(f"invalid {label}: {value}")
    return clean_value
