from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from storage_db import json_dumps, json_loads, new_id, utc_now


SETTLEMENT_STATUSES = {"pending", "processing", "paid", "failed", "canceled"}
SETTLEMENT_TRANSITIONS = {
    "pending": {"pending", "processing", "paid", "failed", "canceled"},
    "processing": {"processing", "paid", "failed", "canceled"},
    "paid": {"paid"},
    "failed": {"failed", "processing", "canceled"},
    "canceled": {"canceled"},
}


class CommissionSettlementError(ValueError):
    code = "commission_settlement_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}


class CommissionSettlementNotFound(CommissionSettlementError):
    code = "commission_settlement_not_found"


class CommissionSettlementConflict(CommissionSettlementError):
    code = "commission_settlement_conflict"


class InvalidCommissionSettlementInput(CommissionSettlementError):
    code = "invalid_commission_settlement_input"


def release_eligible_commissions(
    conn: sqlite3.Connection,
    *,
    agent_id: str = "",
    min_age_days: int = 7,
    now: datetime | str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Move aged pending commission orders to eligible."""
    min_age_days = _non_negative_int(min_age_days, "min_age_days")
    limit = max(1, min(_positive_int(limit, "limit"), 1000))
    current = _to_utc_datetime(now)
    cutoff = current - timedelta(days=min_age_days)
    params: list[Any] = ["pending"]
    agent_clause = ""
    if str(agent_id or "").strip():
        agent_clause = "AND agent_id = ?"
        params.append(str(agent_id).strip())
    params.append(limit)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT * FROM commission_orders
        WHERE status = ? AND settlement_id = '' {agent_clause}
        ORDER BY datetime(created_at) ASC, created_at ASC, id ASC
        LIMIT ?
        """,
        tuple(params),
    )
    selected = [row for row in rows if _parse_datetime(str(row["created_at"])) <= cutoff]
    if not selected:
        return {"ok": True, "released": 0, "commissionAmount": 0, "orderIds": []}

    selected_ids = [str(row["id"]) for row in selected]
    updated_at = _isoformat(current)
    placeholders = ",".join("?" for _ in selected_ids)
    with conn:
        conn.execute(
            f"""
            UPDATE commission_orders
            SET status = 'eligible', updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'pending' AND settlement_id = ''
            """,
            (updated_at, *selected_ids),
        )
    return {
        "ok": True,
        "released": len(selected_ids),
        "commissionAmount": sum(int(row["commission_amount"] or 0) for row in selected),
        "orderIds": selected_ids,
        "cutoff": _isoformat(cutoff),
    }


def create_commission_settlement(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    commission_order_ids: Sequence[str] | None = None,
    period_start: str = "",
    period_end: str = "",
    settlement_no: str = "",
    settlement_account: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    settlement_id: str | None = None,
) -> dict[str, Any]:
    agent_id = _required_text(agent_id, "agent_id")
    agent = _one_dict(conn, "SELECT * FROM agent_profiles WHERE id = ?", (agent_id,))
    if agent is None:
        raise CommissionSettlementNotFound("agent profile not found", agentId=agent_id)

    orders = _eligible_orders(conn, agent_id, commission_order_ids)
    if not orders:
        raise CommissionSettlementConflict("no eligible commission orders to settle", agentId=agent_id)

    currency = str(orders[0]["currency"] or "CNY")
    if any(str(order["currency"] or "CNY") != currency for order in orders):
        raise CommissionSettlementConflict("mixed currencies are not supported", agentId=agent_id)

    account = dict(settlement_account or {})
    if not account:
        account = json_loads(agent.get("settlement_account_json"), {})
        if not isinstance(account, dict):
            account = {}

    now = utc_now()
    record_id = settlement_id or new_id("settlement")
    order_amount = sum(int(order["order_amount"] or 0) for order in orders)
    commission_amount = sum(int(order["commission_amount"] or 0) for order in orders)
    order_ids = [str(order["id"]) for order in orders]
    metadata_payload = {**dict(metadata or {}), "commissionOrderIds": order_ids}
    with conn:
        conn.execute(
            """
            INSERT INTO commission_settlements (
                id, agent_id, settlement_no, period_start, period_end,
                total_order_amount, total_commission_amount, order_count,
                currency, status, settlement_account_json, metadata_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                record_id,
                agent_id,
                settlement_no or _settlement_no(agent_id, now),
                str(period_start or ""),
                str(period_end or ""),
                order_amount,
                commission_amount,
                len(order_ids),
                currency,
                json_dumps(account),
                json_dumps(metadata_payload),
                now,
                now,
            ),
        )
        placeholders = ",".join("?" for _ in order_ids)
        conn.execute(
            f"""
            UPDATE commission_orders
            SET settlement_id = ?, updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'eligible' AND settlement_id = ''
            """,
            (record_id, now, *order_ids),
        )
    return get_commission_settlement(conn, record_id)


def update_commission_settlement_status(
    conn: sqlite3.Connection,
    settlement_id: str,
    status: str,
    *,
    failure_reason: str = "",
    metadata: Mapping[str, Any] | None = None,
    paid_at: datetime | str | None = None,
) -> dict[str, Any]:
    settlement_id = _required_text(settlement_id, "settlement_id")
    target_status = _choice(status, SETTLEMENT_STATUSES, "settlement status")
    current = _one_dict(conn, "SELECT * FROM commission_settlements WHERE id = ?", (settlement_id,))
    if current is None:
        raise CommissionSettlementNotFound("commission settlement not found", settlementId=settlement_id)
    current_status = str(current["status"])
    if target_status not in SETTLEMENT_TRANSITIONS[current_status]:
        raise CommissionSettlementConflict(
            "invalid settlement status transition",
            settlementId=settlement_id,
            currentStatus=current_status,
            targetStatus=target_status,
        )

    now = utc_now()
    paid_at_text = current.get("paid_at")
    if target_status == "paid":
        paid_at_text = _isoformat(_to_utc_datetime(paid_at))
    elif target_status in {"failed", "canceled"}:
        paid_at_text = None

    merged_metadata = json_loads(current.get("metadata_json"), {})
    if not isinstance(merged_metadata, dict):
        merged_metadata = {}
    if metadata:
        merged_metadata.update(dict(metadata))
    history = merged_metadata.get("statusHistory")
    if not isinstance(history, list):
        history = []
    history.append({"from": current_status, "to": target_status, "at": now})
    merged_metadata["statusHistory"] = history

    with conn:
        conn.execute(
            """
            UPDATE commission_settlements
            SET status = ?, paid_at = ?, failure_reason = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                target_status,
                paid_at_text,
                str(failure_reason or ""),
                json_dumps(merged_metadata),
                now,
                settlement_id,
            ),
        )
        if target_status == "paid":
            conn.execute(
                """
                UPDATE commission_orders
                SET status = 'settled', settled_at = COALESCE(settled_at, ?), updated_at = ?
                WHERE settlement_id = ? AND status = 'eligible'
                """,
                (paid_at_text or now, now, settlement_id),
            )
        elif target_status in {"failed", "canceled"}:
            conn.execute(
                """
                UPDATE commission_orders
                SET settlement_id = '', updated_at = ?
                WHERE settlement_id = ? AND status = 'eligible'
                """,
                (now, settlement_id),
            )
    return get_commission_settlement(conn, settlement_id)


def get_commission_settlement(conn: sqlite3.Connection, settlement_id: str) -> dict[str, Any]:
    row = _one_dict(conn, "SELECT * FROM commission_settlements WHERE id = ?", (settlement_id,))
    if row is None:
        raise CommissionSettlementNotFound("commission settlement not found", settlementId=settlement_id)
    payload = _settlement_from_row(row)
    orders = _fetch_dicts(
        conn,
        """
        SELECT id, order_id, customer_id, order_amount, commission_amount, status, settled_at
        FROM commission_orders
        WHERE settlement_id = ?
        ORDER BY datetime(created_at) ASC, created_at ASC, id ASC
        """,
        (settlement_id,),
    )
    payload["orders"] = [
        {
            "id": order["id"],
            "orderId": order["order_id"],
            "customerId": order["customer_id"],
            "orderAmount": int(order["order_amount"] or 0),
            "commissionAmount": int(order["commission_amount"] or 0),
            "status": order["status"],
            "settledAt": order["settled_at"],
        }
        for order in orders
    ]
    return payload


def list_commission_settlements(
    conn: sqlite3.Connection,
    *,
    agent_id: str = "",
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if str(agent_id or "").strip():
        clauses.append("agent_id = ?")
        params.append(str(agent_id).strip())
    if str(status or "").strip():
        clauses.append("status = ?")
        params.append(_choice(status, SETTLEMENT_STATUSES, "settlement status"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 100)))
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT * FROM commission_settlements
        {where}
        ORDER BY datetime(created_at) DESC, created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_settlement_from_row(row) for row in rows]


def _eligible_orders(
    conn: sqlite3.Connection,
    agent_id: str,
    commission_order_ids: Sequence[str] | None,
) -> list[dict[str, Any]]:
    if commission_order_ids:
        clean_ids = [_required_text(item, "commission_order_id") for item in commission_order_ids]
        placeholders = ",".join("?" for _ in clean_ids)
        rows = _fetch_dicts(
            conn,
            f"""
            SELECT * FROM commission_orders
            WHERE id IN ({placeholders})
            ORDER BY datetime(created_at) ASC, created_at ASC, id ASC
            """,
            tuple(clean_ids),
        )
        found_ids = {str(row["id"]) for row in rows}
        missing = [item for item in clean_ids if item not in found_ids]
        if missing:
            raise CommissionSettlementNotFound("commission order not found", missingIds=missing)
        bad = [
            row
            for row in rows
            if row["agent_id"] != agent_id or row["status"] != "eligible" or str(row["settlement_id"] or "")
        ]
        if bad:
            raise CommissionSettlementConflict("commission orders are not eligible for this settlement", orderIds=[row["id"] for row in bad])
        return rows

    return _fetch_dicts(
        conn,
        """
        SELECT * FROM commission_orders
        WHERE agent_id = ? AND status = 'eligible' AND settlement_id = ''
        ORDER BY datetime(created_at) ASC, created_at ASC, id ASC
        """,
        (agent_id,),
    )


def _settlement_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "agentId": data["agent_id"],
        "settlementNo": data["settlement_no"],
        "periodStart": data["period_start"],
        "periodEnd": data["period_end"],
        "totalOrderAmount": int(data["total_order_amount"] or 0),
        "totalCommissionAmount": int(data["total_commission_amount"] or 0),
        "orderCount": int(data["order_count"] or 0),
        "currency": data["currency"],
        "status": data["status"],
        "settlementAccount": json_loads(data.get("settlement_account_json"), {}),
        "paidAt": data.get("paid_at"),
        "failureReason": data.get("failure_reason") or "",
        "metadata": json_loads(data.get("metadata_json"), {}),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _settlement_no(agent_id: str, created_at: str) -> str:
    compact_time = "".join(ch for ch in created_at if ch.isdigit())[:14]
    return f"SET-{compact_time}-{agent_id[-6:]}"


def _fetch_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], sqlite3.Row):
        return [dict(row) for row in rows]
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _one_dict(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    rows = _fetch_dicts(conn, sql, params)
    return rows[0] if rows else None


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidCommissionSettlementInput(f"{field_name} is required")
    return text


def _choice(value: Any, allowed: set[str], label: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise InvalidCommissionSettlementInput(f"invalid {label}: {value}")
    return text


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidCommissionSettlementInput(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCommissionSettlementInput(f"{name} must be an integer") from exc
    if number < 0:
        raise InvalidCommissionSettlementInput(f"{name} must be non-negative")
    return number


def _positive_int(value: Any, name: str) -> int:
    number = _non_negative_int(value, name)
    if number <= 0:
        raise InvalidCommissionSettlementInput(f"{name} must be positive")
    return number


def _to_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise InvalidCommissionSettlementInput("now must be datetime, ISO string, or None")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _parse_datetime(value: str) -> datetime:
    return _to_utc_datetime(value)


def _isoformat(value: datetime) -> str:
    return _to_utc_datetime(value).isoformat()
