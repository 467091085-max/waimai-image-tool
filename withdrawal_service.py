from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from storage_db import json_dumps, json_loads, new_id, utc_now


WITHDRAWAL_STATUSES = {"pending", "approved", "rejected", "paid", "canceled"}
WITHDRAWAL_TRANSITIONS = {
    "pending": {"pending", "approved", "rejected", "canceled"},
    "approved": {"approved", "paid", "rejected", "canceled"},
    "rejected": {"rejected"},
    "paid": {"paid"},
    "canceled": {"canceled"},
}
MIN_WITHDRAWAL_AMOUNT_CENTS = 10000
BALANCE_LOCK_STATUSES = {"pending", "approved", "paid"}


class WithdrawalServiceError(ValueError):
    code = "withdrawal_service_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}


class WithdrawalNotFound(WithdrawalServiceError):
    code = "withdrawal_not_found"


class WithdrawalConflict(WithdrawalServiceError):
    code = "withdrawal_conflict"


class InvalidWithdrawalInput(WithdrawalServiceError):
    code = "invalid_withdrawal_input"


def calculate_withdrawable_balance(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
    """Return the local conservative balance for agent withdrawals.

    Local MVP policy: only paid commission settlements are considered
    withdrawable. Pending/eligible commission orders and unpaid settlements are
    intentionally excluded until a real finance ledger exists.
    """
    clean_agent_id = _required_text(agent_id, "agent_id")
    paid_settlements = _sum_int(
        conn,
        """
        SELECT COALESCE(SUM(total_commission_amount), 0)
        FROM commission_settlements
        WHERE agent_id = ? AND status = 'paid'
        """,
        (clean_agent_id,),
    )
    locked_withdrawals = _sum_int(
        conn,
        """
        SELECT COALESCE(SUM(amount_cents), 0)
        FROM agent_withdrawal_requests
        WHERE agent_id = ? AND status IN ('pending', 'approved', 'paid')
        """,
        (clean_agent_id,),
    )
    unpaid_settlements = _sum_int(
        conn,
        """
        SELECT COALESCE(SUM(total_commission_amount), 0)
        FROM commission_settlements
        WHERE agent_id = ? AND status IN ('pending', 'processing')
        """,
        (clean_agent_id,),
    )
    eligible_commissions = _sum_int(
        conn,
        """
        SELECT COALESCE(SUM(commission_amount), 0)
        FROM commission_orders
        WHERE agent_id = ? AND status = 'eligible' AND settlement_id = ''
        """,
        (clean_agent_id,),
    )
    available = max(0, paid_settlements - locked_withdrawals)
    return {
        "agentId": clean_agent_id,
        "currency": "CNY",
        "policy": "paid_settlements_minus_pending_approved_paid_withdrawals",
        "paidSettlementCents": paid_settlements,
        "lockedWithdrawalCents": locked_withdrawals,
        "availableCents": available,
        "excludedUnpaidSettlementCents": unpaid_settlements,
        "excludedEligibleCommissionCents": eligible_commissions,
    }


def create_withdrawal_request(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    amount_cents: int,
    account_snapshot: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
    withdrawal_id: str | None = None,
) -> dict[str, Any]:
    clean_agent_id = _required_text(agent_id, "agent_id")
    amount = _positive_int(amount_cents, "amount_cents")
    if amount < MIN_WITHDRAWAL_AMOUNT_CENTS:
        raise InvalidWithdrawalInput(
            "withdrawal amount is below minimum",
            minimumAmountCents=MIN_WITHDRAWAL_AMOUNT_CENTS,
            amountCents=amount,
        )
    account = _account_snapshot(account_snapshot)
    _require_active_agent(conn, clean_agent_id)

    balance = calculate_withdrawable_balance(conn, clean_agent_id)
    if amount > int(balance["availableCents"]):
        raise WithdrawalConflict(
            "withdrawal amount exceeds available balance",
            agentId=clean_agent_id,
            amountCents=amount,
            availableCents=balance["availableCents"],
        )

    now = utc_now()
    record_id = withdrawal_id or new_id("withdrawal")
    metadata_payload = dict(metadata or {})
    metadata_payload["statusHistory"] = [
        {
            "from": None,
            "to": "pending",
            "at": now,
            "reason": "created",
            "metadata": {"availableCents": balance["availableCents"]},
        }
    ]
    balance_snapshot = {**balance, "requestedAmountCents": amount}
    with conn:
        conn.execute(
            """
            INSERT INTO agent_withdrawal_requests (
                id, agent_id, amount_cents, currency, status, account_snapshot_json,
                balance_snapshot_json, status_reason, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, 'CNY', 'pending', ?, ?, '', ?, ?, ?)
            """,
            (
                record_id,
                clean_agent_id,
                amount,
                json_dumps(account),
                json_dumps(balance_snapshot),
                json_dumps(metadata_payload),
                now,
                now,
            ),
        )
    return get_withdrawal_request(conn, record_id)


def get_withdrawal_request(conn: sqlite3.Connection, withdrawal_id: str) -> dict[str, Any]:
    clean_withdrawal_id = _required_text(withdrawal_id, "withdrawal_id")
    row = _one_dict(conn, "SELECT * FROM agent_withdrawal_requests WHERE id = ?", (clean_withdrawal_id,))
    if row is None:
        raise WithdrawalNotFound("withdrawal request not found", withdrawalId=clean_withdrawal_id)
    return _withdrawal_from_row(row)


def list_withdrawal_requests(
    conn: sqlite3.Connection,
    *,
    agent_id: str = "",
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if str(agent_id or "").strip():
        clauses.append("agent_id = ?")
        params.append(str(agent_id).strip())
    if str(status or "").strip():
        clauses.append("status = ?")
        params.append(_choice(status, WITHDRAWAL_STATUSES, "withdrawal status"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(_positive_int(limit, "limit"), 100)))
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT * FROM agent_withdrawal_requests
        {where}
        ORDER BY datetime(created_at) DESC, created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_withdrawal_from_row(row) for row in rows]


def update_withdrawal_request_status(
    conn: sqlite3.Connection,
    withdrawal_id: str,
    status: str,
    *,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    clean_withdrawal_id = _required_text(withdrawal_id, "withdrawal_id")
    target_status = _choice(status, WITHDRAWAL_STATUSES, "withdrawal status")
    current = _one_dict(conn, "SELECT * FROM agent_withdrawal_requests WHERE id = ?", (clean_withdrawal_id,))
    if current is None:
        raise WithdrawalNotFound("withdrawal request not found", withdrawalId=clean_withdrawal_id)

    current_status = str(current["status"])
    if target_status not in WITHDRAWAL_TRANSITIONS[current_status]:
        raise WithdrawalConflict(
            "invalid withdrawal status transition",
            withdrawalId=clean_withdrawal_id,
            currentStatus=current_status,
            targetStatus=target_status,
        )
    if target_status in {"approved", "paid"}:
        _require_active_agent(conn, str(current["agent_id"]))

    now = utc_now()
    metadata_payload = json_loads(current.get("metadata_json"), {})
    if not isinstance(metadata_payload, dict):
        metadata_payload = {}
    history = metadata_payload.get("statusHistory")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": current_status,
            "to": target_status,
            "at": now,
            "reason": str(reason or ""),
            "metadata": dict(metadata or {}),
        }
    )
    metadata_payload["statusHistory"] = history

    approved_at = current.get("approved_at")
    rejected_at = current.get("rejected_at")
    paid_at = current.get("paid_at")
    canceled_at = current.get("canceled_at")
    if target_status == "approved" and not approved_at:
        approved_at = now
    elif target_status == "rejected" and not rejected_at:
        rejected_at = now
    elif target_status == "paid" and not paid_at:
        paid_at = now
    elif target_status == "canceled" and not canceled_at:
        canceled_at = now

    with conn:
        conn.execute(
            """
            UPDATE agent_withdrawal_requests
            SET status = ?, status_reason = ?, metadata_json = ?, updated_at = ?,
                approved_at = ?, rejected_at = ?, paid_at = ?, canceled_at = ?
            WHERE id = ?
            """,
            (
                target_status,
                str(reason or ""),
                json_dumps(metadata_payload),
                now,
                approved_at,
                rejected_at,
                paid_at,
                canceled_at,
                clean_withdrawal_id,
            ),
        )
    return get_withdrawal_request(conn, clean_withdrawal_id)


update_withdrawal_status = update_withdrawal_request_status


def _require_active_agent(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
    row = _one_dict(conn, "SELECT * FROM agent_profiles WHERE id = ?", (agent_id,))
    if row is None:
        raise WithdrawalNotFound("agent profile not found", agentId=agent_id)
    if str(row["status"]) != "active":
        raise WithdrawalConflict("agent profile is not active", agentId=agent_id, status=row["status"])
    return row


def _withdrawal_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "agentId": data["agent_id"],
        "amountCents": int(data["amount_cents"] or 0),
        "currency": data["currency"],
        "status": data["status"],
        "accountSnapshot": json_loads(data.get("account_snapshot_json"), {}),
        "balanceSnapshot": json_loads(data.get("balance_snapshot_json"), {}),
        "statusReason": data.get("status_reason") or "",
        "metadata": json_loads(data.get("metadata_json"), {}),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
        "approvedAt": data.get("approved_at"),
        "rejectedAt": data.get("rejected_at"),
        "paidAt": data.get("paid_at"),
        "canceledAt": data.get("canceled_at"),
    }


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


def _sum_int(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidWithdrawalInput(f"{field_name} is required")
    return text


def _choice(value: Any, allowed: set[str], label: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise InvalidWithdrawalInput(f"invalid {label}: {value}")
    return text


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidWithdrawalInput(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWithdrawalInput(f"{name} must be an integer") from exc
    if number <= 0:
        raise InvalidWithdrawalInput(f"{name} must be positive")
    return number


def _account_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidWithdrawalInput("account_snapshot must be an object")
    snapshot = dict(value)
    if not snapshot:
        raise InvalidWithdrawalInput("account_snapshot is required")
    return snapshot
