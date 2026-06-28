from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "canceled")
IMAGE_STATUSES = ("generated", "failed", "rejected")
EXPORT_STATUSES = ("ready", "failed", "expired")
AGENT_STATUSES = ("active", "inactive", "suspended", "pending")
COMMISSION_ORDER_STATUSES = ("pending", "eligible", "settled", "canceled", "refunded")
COMMISSION_SETTLEMENT_STATUSES = ("pending", "processing", "paid", "failed", "canceled")
WITHDRAWAL_STATUSES = ("pending", "approved", "rejected", "paid", "canceled")
INVITE_STATUSES = ("pending", "accepted", "rewarded", "canceled", "expired")
REWARD_STATUSES = ("pending", "granted", "failed", "canceled")
RISK_LEVELS = ("info", "low", "medium", "high", "critical")
RISK_DECISIONS = ("allow", "deny", "review")


def list_users(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    """List users with lightweight operating metrics for the admin console."""
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "user_id": "id",
            "phone": "phone",
            "status": "status",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "last_login_at": "last_login_at",
            "store_count": "store_count",
            "order_count": "order_count",
            "payment_amount_cents": "payment_amount_cents",
            "point_balance": "point_balance",
        },
        "created_at",
    )
    if not _table_exists(conn, "users"):
        return _page([], 0, limit, offset, sort_key, order)

    columns = _table_columns(conn, "users")
    clauses: list[str] = []
    params: list[Any] = []
    if status and "status" in columns:
        clauses.append("u.status = ?")
        params.append(str(status))
    if created_from and "created_at" in columns:
        clauses.append("u.created_at >= ?")
        params.append(str(created_from))
    if created_to and "created_at" in columns:
        clauses.append("u.created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["u.id"]
        if "phone" in columns:
            searchable.append("u.phone")
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM users u {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            u.id AS id,
            {_column_or_literal("u", "phone", columns)} AS phone,
            {_column_or_literal("u", "status", columns, "active")} AS status,
            {_column_or_literal("u", "created_at", columns)} AS created_at,
            {_column_or_literal("u", "updated_at", columns, fallback_column="created_at")} AS updated_at,
            {_column_or_literal("u", "last_login_at", columns)} AS last_login_at,
            {_user_store_count_sql(conn)} AS store_count,
            {_user_order_count_sql(conn)} AS order_count,
            {_user_payment_amount_sql(conn)} AS payment_amount_cents,
            {_user_point_balance_sql(conn)} AS point_balance
        FROM users u
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_user_payload(row) for row in rows], total, limit, offset, sort_key, order)


def list_stores(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    created_by_user_id: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    """List stores and local activity counts without changing the core schema."""
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "store_id": "id",
            "name": "name",
            "status": "status",
            "created_by_user_id": "created_by_user_id",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "user_count": "user_count",
            "menu_upload_count": "menu_upload_count",
            "job_count": "job_count",
            "asset_count": "asset_count",
        },
        "created_at",
    )
    if not _table_exists(conn, "stores"):
        return _page([], 0, limit, offset, sort_key, order)

    columns = _table_columns(conn, "stores")
    clauses: list[str] = []
    params: list[Any] = []
    if status and "status" in columns:
        clauses.append("s.status = ?")
        params.append(str(status))
    if created_by_user_id and "created_by_user_id" in columns:
        clauses.append("s.created_by_user_id = ?")
        params.append(str(created_by_user_id))
    if created_from and "created_at" in columns:
        clauses.append("s.created_at >= ?")
        params.append(str(created_from))
    if created_to and "created_at" in columns:
        clauses.append("s.created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["s.id"]
        if "name" in columns:
            searchable.append("s.name")
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM stores s {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            s.id AS id,
            {_column_or_literal("s", "name", columns)} AS name,
            {_column_or_literal("s", "status", columns, "active")} AS status,
            {_column_or_literal("s", "created_by_user_id", columns)} AS created_by_user_id,
            {_column_or_literal("s", "created_at", columns)} AS created_at,
            {_column_or_literal("s", "updated_at", columns, fallback_column="created_at")} AS updated_at,
            {_store_user_count_sql(conn)} AS user_count,
            {_store_menu_upload_count_sql(conn)} AS menu_upload_count,
            {_store_job_count_sql(conn)} AS job_count,
            {_store_asset_count_sql(conn)} AS asset_count
        FROM stores s
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_store_payload(row) for row in rows], total, limit, offset, sort_key, order)


def list_orders(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    user_id: str | None = None,
    provider: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    """List payment orders, falling back to billing orders in local-only DBs."""
    if _table_exists(conn, "payment_orders"):
        return _list_payment_orders(
            conn,
            status=status,
            user_id=user_id,
            provider=provider,
            search=search,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
            sort=sort,
            order=order,
        )
    return _list_billing_orders(
        conn,
        status=status,
        user_id=user_id,
        search=search,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )


def list_generation_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    menu_upload_id: str | None = None,
    store_name: str | None = None,
    style_id: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "status": "status",
            "store_name": "store_name",
            "style_id": "style_id",
            "quality": "quality",
            "requested_count": "requested_count",
            "completed_count": "completed_count",
            "failed_count": "failed_count",
            "image_count": "image_count",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "completed_at": "completed_at",
        },
        "created_at",
    )
    if not _table_exists(conn, "generation_jobs"):
        return _page([], 0, limit, offset, sort_key, order)

    has_menu_uploads = _table_exists(conn, "menu_uploads")
    join_sql = "LEFT JOIN menu_uploads m ON m.id = j.menu_upload_id" if has_menu_uploads else ""
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("j.status = ?")
        params.append(str(status))
    if menu_upload_id:
        clauses.append("j.menu_upload_id = ?")
        params.append(str(menu_upload_id))
    if style_id:
        clauses.append("j.style_id = ?")
        params.append(str(style_id))
    if store_name and has_menu_uploads:
        clauses.append("m.store_name = ?")
        params.append(str(store_name))
    if created_from:
        clauses.append("j.created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("j.created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["j.id", "j.menu_upload_id", "j.style_id"]
        if has_menu_uploads:
            searchable.extend(["m.store_name", "m.original_filename"])
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM generation_jobs j {join_sql} {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            j.id AS id,
            j.menu_upload_id AS menu_upload_id,
            {_menu_column_or_literal(has_menu_uploads, "store_name")} AS store_name,
            {_menu_column_or_literal(has_menu_uploads, "original_filename")} AS original_filename,
            j.style_id AS style_id,
            j.quality AS quality,
            j.status AS status,
            j.requested_count AS requested_count,
            j.completed_count AS completed_count,
            j.failed_count AS failed_count,
            j.error_message AS error_message,
            j.created_at AS created_at,
            j.updated_at AS updated_at,
            j.started_at AS started_at,
            j.completed_at AS completed_at,
            {_job_image_count_sql(conn)} AS image_count,
            {_job_export_count_sql(conn)} AS export_count,
            {_job_point_delta_sql(conn)} AS point_delta
        FROM generation_jobs j
        {join_sql}
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_generation_task_payload(row) for row in rows], total, limit, offset, sort_key, order)


def list_asset_access_logs(
    conn: sqlite3.Connection,
    *,
    allowed: bool | None = None,
    status: str | None = None,
    action: str | None = None,
    asset_type: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    asset_id: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "user_id": "user_id",
            "agent_id": "agent_id",
            "asset_id": "asset_id",
            "asset_type": "asset_type",
            "action": "action",
            "allowed": "allowed",
            "ip": "ip",
            "created_at": "created_at",
        },
        "created_at",
    )
    if not _table_exists(conn, "asset_access_logs"):
        return _page([], 0, limit, offset, sort_key, order)

    if allowed is None:
        allowed = _asset_access_status_to_allowed(status)

    clauses: list[str] = []
    params: list[Any] = []
    if allowed is not None:
        clauses.append("allowed = ?")
        params.append(1 if allowed else 0)
    if action:
        clauses.append("action = ?")
        params.append(str(action))
    if asset_type:
        clauses.append("asset_type = ?")
        params.append(str(asset_type))
    if user_id:
        clauses.append("user_id = ?")
        params.append(str(user_id))
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(str(agent_id))
    if asset_id:
        clauses.append("asset_id = ?")
        params.append(str(asset_id))
    if created_from:
        clauses.append("created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["id", "asset_id", "request_id", "ip", "deny_reason"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM asset_access_logs {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            id, user_id, agent_id, asset_id, asset_type, action, ip, allowed,
            deny_reason, request_id, user_agent, created_at
        FROM asset_access_logs
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_asset_access_payload(row) for row in rows], total, limit, offset, sort_key, order)


def list_risk_events(
    conn: sqlite3.Connection,
    *,
    decision: str | None = None,
    risk_level: str | None = None,
    event_type: str | None = None,
    user_id: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "user_id": "user_id",
            "agent_id": "agent_id",
            "asset_id": "asset_id",
            "event_type": "event_type",
            "risk_level": "risk_level",
            "decision": "decision",
            "ip": "ip",
            "created_at": "created_at",
        },
        "created_at",
    )
    if not _table_exists(conn, "risk_audit_logs"):
        return _page([], 0, limit, offset, sort_key, order)

    clauses: list[str] = []
    params: list[Any] = []
    if decision:
        clauses.append("decision = ?")
        params.append(str(decision))
    if risk_level:
        clauses.append("risk_level = ?")
        params.append(str(risk_level))
    if event_type:
        clauses.append("event_type = ?")
        params.append(str(event_type))
    if user_id:
        clauses.append("user_id = ?")
        params.append(str(user_id))
    if created_from:
        clauses.append("created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["id", "user_id", "agent_id", "asset_id", "event_type", "ip", "deny_reason", "metadata_json"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM risk_audit_logs {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            id, user_id, agent_id, asset_id, event_type, risk_level, decision,
            ip, deny_reason, metadata_json, created_at
        FROM risk_audit_logs
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_risk_event_payload(row) for row in rows], total, limit, offset, sort_key, order)


def list_commission_settlements(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    agent_id: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "agent_id": "agent_id",
            "settlement_no": "settlement_no",
            "total_order_amount": "total_order_amount",
            "total_commission_amount": "total_commission_amount",
            "order_count": "order_count",
            "status": "status",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "paid_at": "paid_at",
        },
        "created_at",
    )
    if not _table_exists(conn, "commission_settlements"):
        return _page([], 0, limit, offset, sort_key, order)

    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(str(agent_id))
    if created_from:
        clauses.append("created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["id", "settlement_no", "agent_id"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM commission_settlements {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            id, agent_id, settlement_no, period_start, period_end, total_order_amount,
            total_commission_amount, order_count, currency, status, paid_at,
            failure_reason, created_at, updated_at
        FROM commission_settlements
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_commission_settlement_payload(row) for row in rows], total, limit, offset, sort_key, order)


def list_withdrawals(
    conn: sqlite3.Connection,
    *,
    agent_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "id": "id",
            "agent_id": "agent_id",
            "amount_cents": "amount_cents",
            "currency": "currency",
            "status": "status",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "approved_at": "approved_at",
            "rejected_at": "rejected_at",
            "paid_at": "paid_at",
            "canceled_at": "canceled_at",
        },
        "created_at",
    )
    if not _table_exists(conn, "agent_withdrawal_requests"):
        return _page([], 0, limit, offset, sort_key, order)

    clauses: list[str] = []
    params: list[Any] = []
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(str(agent_id))
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if created_from:
        clauses.append("created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["id", "agent_id", "status_reason", "metadata_json"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM agent_withdrawal_requests {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            id, agent_id, amount_cents, currency, status, balance_snapshot_json,
            status_reason, created_at, updated_at, approved_at, rejected_at, paid_at,
            canceled_at
        FROM agent_withdrawal_requests
        {where}
        ORDER BY {sort_expr} {order}, id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_withdrawal_payload(row) for row in rows], total, limit, offset, sort_key, order)


def dashboard_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    job_counts = _status_counts(conn, "generation_jobs", "status", JOB_STATUSES)
    job_sums = _sum_columns(conn, "generation_jobs", ("requested_count", "completed_count", "failed_count"))
    image_counts = _status_counts(conn, "generated_images", "status", IMAGE_STATUSES)
    image_sums = _sum_columns(conn, "generated_images", ("file_size",))
    export_counts = _status_counts(conn, "export_packages", "status", EXPORT_STATUSES)
    export_sums = _sum_columns(conn, "export_packages", ("image_count", "file_size"))
    point_totals = _point_totals(conn)
    agent_counts = _status_counts(conn, "agent_profiles", "status", AGENT_STATUSES)
    order_counts = _status_counts(conn, "commission_orders", "status", COMMISSION_ORDER_STATUSES)
    order_sums = _commission_order_sums(conn)
    settlement_counts = _status_counts(
        conn,
        "commission_settlements",
        "status",
        COMMISSION_SETTLEMENT_STATUSES,
    )
    settlement_sums = _sum_columns(conn, "commission_settlements", ("total_commission_amount",))
    invite_counts = _status_counts(conn, "invite_relations", "status", INVITE_STATUSES)
    invite_sums = _sum_columns(conn, "invite_relations", ("reward_points",))
    risk_decisions = _status_counts(conn, "risk_audit_logs", "decision", RISK_DECISIONS)
    risk_levels = _status_counts(conn, "risk_audit_logs", "risk_level", RISK_LEVELS)
    asset_allowed = _asset_allowed_counts(conn)
    asset_top_deny_reason = _top_deny_reason(conn)

    return {
        "jobs": {
            "total": _sum_counts(job_counts),
            **job_counts,
            "requested": job_sums["requested_count"],
            "completed": job_sums["completed_count"],
            "failedItems": job_sums["failed_count"],
            "successRate": _ratio(job_sums["completed_count"], job_sums["requested_count"]),
        },
        "images": {
            "total": _sum_counts(image_counts),
            **image_counts,
            "totalFileSize": image_sums["file_size"],
        },
        "exports": {
            "total": _sum_counts(export_counts),
            **export_counts,
            "imageCount": export_sums["image_count"],
            "totalFileSize": export_sums["file_size"],
        },
        "points": point_totals,
        "agents": {"total": _sum_counts(agent_counts), **agent_counts},
        "commissions": {
            "pendingAmount": order_sums["pendingCommissionAmount"],
            "eligibleAmount": order_sums["eligibleCommissionAmount"],
            "settledAmount": order_sums["settledCommissionAmount"],
            "orderCount": _sum_counts(order_counts),
            "orders": {
                "total": _sum_counts(order_counts),
                **order_counts,
                "orderAmount": order_sums["orderAmount"],
                "commissionAmount": order_sums["commissionAmount"],
            },
            "settlements": {
                "total": _sum_counts(settlement_counts),
                **settlement_counts,
                "commissionAmount": settlement_sums["total_commission_amount"],
            },
        },
        "invites": {
            "total": _sum_counts(invite_counts),
            **invite_counts,
            "rewardPoints": invite_sums["reward_points"],
        },
        "risk": {
            "total": _sum_counts(risk_decisions),
            "allow": risk_decisions["allow"],
            "deny": risk_decisions["deny"],
            "denied": risk_decisions["deny"],
            "review": risk_decisions["review"],
            "high": risk_levels["high"],
            "critical": risk_levels["critical"],
            "highestLevel": _highest_risk_level(risk_levels),
        },
        "assetAccess": {**asset_allowed, "topDenyReason": asset_top_deny_reason},
    }


def recent_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    if not _table_exists(conn, "generation_jobs"):
        return []
    limit = max(0, int(limit))
    if limit == 0:
        return []

    jobs = _fetch_dicts(
        conn,
        """
        SELECT
            id,
            menu_upload_id,
            style_id,
            quality,
            status,
            requested_count,
            completed_count,
            failed_count,
            error_message,
            created_at,
            updated_at,
            started_at,
            completed_at
        FROM generation_jobs
        ORDER BY datetime(created_at) DESC, created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    if not jobs:
        return []

    job_ids = [str(job["id"]) for job in jobs]
    image_stats = _stats_by_job(
        conn,
        "generated_images",
        """
        SELECT
            job_id,
            COUNT(*) AS image_count,
            COALESCE(SUM(CASE WHEN status = 'generated' THEN 1 ELSE 0 END), 0) AS generated_count,
            COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS image_failed_count,
            COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) AS rejected_count,
            COALESCE(SUM(file_size), 0) AS image_file_size
        FROM generated_images
        WHERE job_id IN ({placeholders})
        GROUP BY job_id
        """,
        job_ids,
    )
    export_stats = _stats_by_job(
        conn,
        "export_packages",
        """
        SELECT
            job_id,
            COUNT(*) AS export_count,
            COALESCE(SUM(image_count), 0) AS exported_image_count,
            COALESCE(SUM(file_size), 0) AS export_file_size
        FROM export_packages
        WHERE job_id IN ({placeholders})
        GROUP BY job_id
        """,
        job_ids,
    )
    point_stats = _stats_by_job(
        conn,
        "point_ledger",
        """
        SELECT
            job_id,
            COALESCE(SUM(amount), 0) AS point_delta,
            COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS points_debited,
            COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS points_credited
        FROM point_ledger
        WHERE job_id IN ({placeholders})
        GROUP BY job_id
        """,
        job_ids,
    )

    recent: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job["id"])
        images = image_stats.get(job_id, {})
        exports = export_stats.get(job_id, {})
        points = point_stats.get(job_id, {})
        requested = _int(job["requested_count"])
        completed = _int(job["completed_count"])
        recent.append(
            {
                "id": job_id,
                "menuUploadId": job["menu_upload_id"],
                "styleId": job["style_id"],
                "quality": job["quality"],
                "status": job["status"],
                "requestedCount": requested,
                "completedCount": completed,
                "failedCount": _int(job["failed_count"]),
                "progress": _ratio(completed, requested),
                "imageCount": _int(images.get("image_count")),
                "generatedImageCount": _int(images.get("generated_count")),
                "failedImageCount": _int(images.get("image_failed_count")),
                "rejectedImageCount": _int(images.get("rejected_count")),
                "imageFileSize": _int(images.get("image_file_size")),
                "exportCount": _int(exports.get("export_count")),
                "exportedImageCount": _int(exports.get("exported_image_count")),
                "exportFileSize": _int(exports.get("export_file_size")),
                "pointDelta": _int(points.get("point_delta")),
                "pointsDebited": _int(points.get("points_debited")),
                "pointsCredited": _int(points.get("points_credited")),
                "errorMessage": job["error_message"],
                "createdAt": job["created_at"],
                "updatedAt": job["updated_at"],
                "startedAt": job["started_at"],
                "completedAt": job["completed_at"],
            }
        )
    return recent


def commission_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    order_counts = _status_counts(conn, "commission_orders", "status", COMMISSION_ORDER_STATUSES)
    settlement_counts = _status_counts(
        conn,
        "commission_settlements",
        "status",
        COMMISSION_SETTLEMENT_STATUSES,
    )
    agent_counts = _status_counts(conn, "agent_profiles", "status", AGENT_STATUSES)
    invite_counts = _status_counts(conn, "invite_relations", "status", INVITE_STATUSES)
    reward_counts = _status_counts(conn, "invite_relations", "reward_status", REWARD_STATUSES)

    order_sums = _commission_order_sums(conn)
    settlement_sums = _commission_settlement_sums(conn)
    invite_sums = _invite_sums(conn)

    return {
        "orders": {"total": _sum_counts(order_counts), **order_counts, **order_sums},
        "settlements": {
            "total": _sum_counts(settlement_counts),
            **settlement_counts,
            **settlement_sums,
        },
        "agents": {"total": _sum_counts(agent_counts), **agent_counts},
        "invites": {
            "total": _sum_counts(invite_counts),
            **invite_counts,
            "rewardStatus": reward_counts,
            **invite_sums,
        },
        "topAgents": _top_commission_agents(conn),
    }


def risk_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    level_counts = _status_counts(conn, "risk_audit_logs", "risk_level", RISK_LEVELS)
    decision_counts = _status_counts(conn, "risk_audit_logs", "decision", RISK_DECISIONS)

    return {
        "total": _sum_counts(level_counts),
        "highestLevel": _highest_risk_level(level_counts),
        "byLevel": level_counts,
        "byDecision": decision_counts,
        "topEvents": _top_groups(conn, "risk_audit_logs", "event_type", "count"),
        "topIps": _top_groups(conn, "risk_audit_logs", "ip", "count", skip_blank=True),
        "recent": _recent_risk_events(conn),
    }


def asset_access_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    allowed_counts = _asset_allowed_counts(conn)

    return {
        **allowed_counts,
        "topDenyReason": _top_deny_reason(conn),
        "byAction": _top_groups(conn, "asset_access_logs", "action", "count"),
        "byAssetType": _top_groups(conn, "asset_access_logs", "asset_type", "count"),
        "topAssets": _top_asset_access(conn),
        "recentDenied": _recent_denied_asset_access(conn),
    }


def _list_payment_orders(
    conn: sqlite3.Connection,
    *,
    status: str | None,
    user_id: str | None,
    provider: str | None,
    search: str | None,
    created_from: str | None,
    created_to: str | None,
    limit: int,
    offset: int,
    sort: str,
    order: str,
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "order_id": "order_id",
            "id": "order_id",
            "user_id": "user_id",
            "provider": "provider",
            "provider_order_id": "provider_order_id",
            "amount_cents": "amount_cents",
            "points": "points",
            "status": "status",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "paid_at": "paid_at",
            "refunded_at": "refunded_at",
        },
        "created_at",
    )
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if user_id:
        clauses.append("user_id = ?")
        params.append(str(user_id))
    if provider:
        clauses.append("provider = ?")
        params.append(str(provider))
    if created_from:
        clauses.append("created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["order_id", "provider_order_id", "user_id"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM payment_orders {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT
            order_id, user_id, provider, provider_order_id, amount_cents, points,
            status, created_at, updated_at, paid_at, closed_at, refunded_at
        FROM payment_orders
        {where}
        ORDER BY {sort_expr} {order}, order_id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_payment_order_payload(row) for row in rows], total, limit, offset, sort_key, order)


def _list_billing_orders(
    conn: sqlite3.Connection,
    *,
    status: str | None,
    user_id: str | None,
    search: str | None,
    created_from: str | None,
    created_to: str | None,
    limit: int,
    offset: int,
    sort: str,
    order: str,
) -> dict[str, Any]:
    limit, offset = _page_args(limit, offset)
    sort_key, sort_expr, order = _sort_args(
        sort,
        order,
        {
            "order_id": "order_id",
            "id": "order_id",
            "user_id": "user_id",
            "points": "points",
            "status": "status",
            "created_at": "created_at",
        },
        "created_at",
    )
    if not _table_exists(conn, "orders"):
        return _page([], 0, limit, offset, sort_key, order)

    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if user_id:
        clauses.append("user_id = ?")
        params.append(str(user_id))
    if created_from:
        clauses.append("created_at >= ?")
        params.append(str(created_from))
    if created_to:
        clauses.append("created_at <= ?")
        params.append(str(created_to))
    if search:
        searchable = ["order_id", "user_id"]
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in searchable) + ")")
        params.extend([f"%{search}%"] * len(searchable))

    where = _where_sql(clauses)
    total = _count(conn, f"SELECT COUNT(*) AS item_count FROM orders {where}", params)
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT order_id, user_id, kind, points, status, created_at
        FROM orders
        {where}
        ORDER BY {sort_expr} {order}, order_id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    return _page([_billing_order_payload(row) for row in rows], total, limit, offset, sort_key, order)


def _user_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "userId": row["id"],
        "phone": row["phone"],
        "status": row["status"],
        "storeCount": _int(row["store_count"]),
        "orderCount": _int(row["order_count"]),
        "paymentAmountCents": _int(row["payment_amount_cents"]),
        "pointBalance": _int(row["point_balance"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastLoginAt": row["last_login_at"],
    }


def _store_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "storeId": row["id"],
        "name": row["name"],
        "status": row["status"],
        "createdByUserId": row["created_by_user_id"],
        "userCount": _int(row["user_count"]),
        "menuUploadCount": _int(row["menu_upload_count"]),
        "jobCount": _int(row["job_count"]),
        "assetCount": _int(row["asset_count"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _payment_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["order_id"],
        "orderId": row["order_id"],
        "userId": row["user_id"],
        "provider": row["provider"],
        "providerOrderId": row["provider_order_id"],
        "amountCents": _int(row["amount_cents"]),
        "points": _int(row["points"]),
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "paidAt": row["paid_at"],
        "closedAt": row["closed_at"],
        "refundedAt": row["refunded_at"],
    }


def _billing_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["order_id"],
        "orderId": row["order_id"],
        "userId": row["user_id"],
        "provider": "billing",
        "providerOrderId": row["order_id"],
        "amountCents": 0,
        "points": _int(row["points"]),
        "status": row["status"],
        "kind": row["kind"],
        "createdAt": row["created_at"],
        "updatedAt": row["created_at"],
        "paidAt": None,
        "closedAt": None,
        "refundedAt": None,
    }


def _generation_task_payload(row: dict[str, Any]) -> dict[str, Any]:
    requested = _int(row["requested_count"])
    completed = _int(row["completed_count"])
    return {
        "id": row["id"],
        "menuUploadId": row["menu_upload_id"],
        "storeName": row["store_name"],
        "originalFilename": row["original_filename"],
        "styleId": row["style_id"],
        "quality": row["quality"],
        "status": row["status"],
        "requestedCount": requested,
        "completedCount": completed,
        "failedCount": _int(row["failed_count"]),
        "progress": _ratio(completed, requested),
        "imageCount": _int(row["image_count"]),
        "exportCount": _int(row["export_count"]),
        "pointDelta": _int(row["point_delta"]),
        "errorMessage": row["error_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
    }


def _asset_access_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "agentId": row["agent_id"],
        "assetId": row["asset_id"],
        "assetType": row["asset_type"],
        "action": row["action"],
        "ip": row["ip"],
        "allowed": bool(row["allowed"]),
        "denyReason": row["deny_reason"],
        "requestId": row["request_id"],
        "userAgent": row["user_agent"],
        "createdAt": row["created_at"],
    }


def _risk_event_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "agentId": row["agent_id"],
        "assetId": row["asset_id"],
        "eventType": row["event_type"],
        "riskLevel": row["risk_level"],
        "decision": row["decision"],
        "ip": row["ip"],
        "denyReason": row["deny_reason"],
        "metadataJson": row["metadata_json"],
        "createdAt": row["created_at"],
    }


def _commission_settlement_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "agentId": row["agent_id"],
        "settlementNo": row["settlement_no"],
        "periodStart": row["period_start"],
        "periodEnd": row["period_end"],
        "totalOrderAmount": _int(row["total_order_amount"]),
        "totalCommissionAmount": _int(row["total_commission_amount"]),
        "orderCount": _int(row["order_count"]),
        "currency": row["currency"],
        "status": row["status"],
        "paidAt": row["paid_at"],
        "failureReason": row["failure_reason"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _withdrawal_payload(row: dict[str, Any]) -> dict[str, Any]:
    balance = _json_object(row.get("balance_snapshot_json"))
    return {
        "id": row["id"],
        "withdrawalId": row["id"],
        "agentId": row["agent_id"],
        "amountCents": _int(row["amount_cents"]),
        "currency": row["currency"],
        "status": row["status"],
        "balanceSnapshot": balance,
        "balanceAvailableCents": _int(balance.get("availableCents")),
        "balancePaidSettlementCents": _int(balance.get("paidSettlementCents")),
        "balanceLockedWithdrawalCents": _int(balance.get("lockedWithdrawalCents")),
        "statusReason": row["status_reason"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "approvedAt": row["approved_at"],
        "rejectedAt": row["rejected_at"],
        "paidAt": row["paid_at"],
        "canceledAt": row["canceled_at"],
    }


def _user_store_count_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "user_stores"):
        return "(SELECT COUNT(*) FROM user_stores us WHERE us.user_id = u.id)"
    if _table_exists(conn, "stores") and "created_by_user_id" in _table_columns(conn, "stores"):
        return "(SELECT COUNT(*) FROM stores st WHERE st.created_by_user_id = u.id)"
    return "0"


def _user_order_count_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "payment_orders"):
        return "(SELECT COUNT(*) FROM payment_orders po WHERE po.user_id = u.id)"
    if _table_exists(conn, "orders"):
        return "(SELECT COUNT(*) FROM orders bo WHERE bo.user_id = u.id)"
    return "0"


def _user_payment_amount_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "payment_orders"):
        return "(SELECT COALESCE(SUM(po.amount_cents), 0) FROM payment_orders po WHERE po.user_id = u.id)"
    return "0"


def _user_point_balance_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "accounts") and "balance" in _table_columns(conn, "accounts"):
        return "(SELECT COALESCE(MAX(a.balance), 0) FROM accounts a WHERE a.user_id = u.id)"
    return "0"


def _store_user_count_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "user_stores"):
        return "(SELECT COUNT(*) FROM user_stores us WHERE us.store_id = s.id)"
    if "created_by_user_id" in _table_columns(conn, "stores"):
        return "CASE WHEN s.created_by_user_id <> '' THEN 1 ELSE 0 END"
    return "0"


def _store_menu_upload_count_sql(conn: sqlite3.Connection) -> str:
    if _store_name_metrics_supported(conn) and _table_exists(conn, "menu_uploads"):
        return "(SELECT COUNT(*) FROM menu_uploads mu WHERE mu.store_name = s.name)"
    return "0"


def _store_job_count_sql(conn: sqlite3.Connection) -> str:
    if _store_name_metrics_supported(conn) and _table_exists(conn, "menu_uploads") and _table_exists(conn, "generation_jobs"):
        return """
        (
            SELECT COUNT(*)
            FROM generation_jobs gj
            JOIN menu_uploads mu ON mu.id = gj.menu_upload_id
            WHERE mu.store_name = s.name
        )
        """
    return "0"


def _store_asset_count_sql(conn: sqlite3.Connection) -> str:
    if _store_name_metrics_supported(conn) and _table_exists(conn, "library_images"):
        return "(SELECT COUNT(*) FROM library_images li WHERE li.store_name = s.name)"
    return "0"


def _store_name_metrics_supported(conn: sqlite3.Connection) -> bool:
    return "name" in _table_columns(conn, "stores")


def _job_image_count_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "generated_images"):
        return "(SELECT COUNT(*) FROM generated_images gi WHERE gi.job_id = j.id)"
    return "0"


def _job_export_count_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "export_packages"):
        return "(SELECT COUNT(*) FROM export_packages ep WHERE ep.job_id = j.id)"
    return "0"


def _job_point_delta_sql(conn: sqlite3.Connection) -> str:
    if _table_exists(conn, "point_ledger"):
        return "(SELECT COALESCE(SUM(pl.amount), 0) FROM point_ledger pl WHERE pl.job_id = j.id)"
    return "0"


def _menu_column_or_literal(has_menu_uploads: bool, column: str) -> str:
    return f"m.{column}" if has_menu_uploads else "''"


def _column_or_literal(
    alias: str,
    column: str,
    columns: set[str],
    default: Any = "",
    *,
    fallback_column: str | None = None,
) -> str:
    if column in columns:
        return f"{alias}.{column}"
    if fallback_column and fallback_column in columns:
        return f"{alias}.{fallback_column}"
    return _sql_literal(default)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _where_sql(clauses: Sequence[str]) -> str:
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


def _page_args(limit: int, offset: int) -> tuple[int, int]:
    return max(0, min(_coerce_int(limit, DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)), max(0, _coerce_int(offset, 0))


def _sort_args(
    sort: str,
    order: str,
    allowed: dict[str, str],
    default: str,
) -> tuple[str, str, str]:
    key = _normalize_sort_key(sort)
    if key not in allowed:
        key = default
    direction = str(order or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "desc"
    return key, allowed[key], direction.upper()


def _normalize_sort_key(value: Any) -> str:
    text = str(value or "").strip()
    output: list[str] = []
    previous_was_separator = False
    for char in text:
        if char in {"-", " ", "."}:
            if output and not previous_was_separator:
                output.append("_")
            previous_was_separator = True
            continue
        if char.isupper():
            if output and not previous_was_separator:
                output.append("_")
            output.append(char.lower())
        else:
            output.append(char.lower())
        previous_was_separator = char == "_"
    return "".join(output).strip("_")


def _asset_access_status_to_allowed(status: str | None) -> bool | None:
    normalized = str(status or "").strip().lower()
    if normalized in {"allowed", "allow", "true", "1", "yes"}:
        return True
    if normalized in {"denied", "deny", "blocked", "false", "0", "no"}:
        return False
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _page(
    items: list[dict[str, Any]],
    total: int,
    limit: int,
    offset: int,
    sort: str,
    order: str,
) -> dict[str, Any]:
    return {
        "items": items,
        "total": _int(total),
        "limit": _int(limit),
        "offset": _int(offset),
        "sort": sort,
        "order": order.lower(),
    }


def _count(conn: sqlite3.Connection, sql: str, params: Sequence[Any] | None = None) -> int:
    rows = _fetch_dicts(conn, sql, params)
    return _int(rows[0].get("item_count")) if rows else 0


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _point_totals(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "point_ledger"):
        return {"entries": 0, "credited": 0, "debited": 0, "net": 0, "latestBalance": 0}
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            COUNT(*) AS entries,
            COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS credited,
            COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS debited,
            COALESCE(SUM(amount), 0) AS net
        FROM point_ledger
        """,
    )
    latest = _fetch_dicts(
        conn,
        """
        SELECT balance_after
        FROM point_ledger
        WHERE balance_after IS NOT NULL
        ORDER BY datetime(created_at) DESC, created_at DESC, id DESC
        LIMIT 1
        """,
    )
    row = rows[0] if rows else {}
    return {
        "entries": _int(row.get("entries")),
        "credited": _int(row.get("credited")),
        "debited": _int(row.get("debited")),
        "net": _int(row.get("net")),
        "latestBalance": _int(latest[0].get("balance_after")) if latest else 0,
    }


def _commission_order_sums(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "commission_orders"):
        return {
            "orderAmount": 0,
            "commissionAmount": 0,
            "pendingCommissionAmount": 0,
            "eligibleCommissionAmount": 0,
            "settledCommissionAmount": 0,
        }
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            COALESCE(SUM(order_amount), 0) AS order_amount,
            COALESCE(SUM(commission_amount), 0) AS commission_amount,
            COALESCE(SUM(CASE WHEN status = 'pending' THEN commission_amount ELSE 0 END), 0)
                AS pending_commission_amount,
            COALESCE(SUM(CASE WHEN status = 'eligible' THEN commission_amount ELSE 0 END), 0)
                AS eligible_commission_amount,
            COALESCE(SUM(CASE WHEN status = 'settled' THEN commission_amount ELSE 0 END), 0)
                AS settled_commission_amount
        FROM commission_orders
        """,
    )
    row = rows[0] if rows else {}
    return {
        "orderAmount": _int(row.get("order_amount")),
        "commissionAmount": _int(row.get("commission_amount")),
        "pendingCommissionAmount": _int(row.get("pending_commission_amount")),
        "eligibleCommissionAmount": _int(row.get("eligible_commission_amount")),
        "settledCommissionAmount": _int(row.get("settled_commission_amount")),
    }


def _commission_settlement_sums(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "commission_settlements"):
        return {"orderCount": 0, "orderAmount": 0, "commissionAmount": 0, "paidCommissionAmount": 0}
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            COALESCE(SUM(order_count), 0) AS order_count,
            COALESCE(SUM(total_order_amount), 0) AS order_amount,
            COALESCE(SUM(total_commission_amount), 0) AS commission_amount,
            COALESCE(SUM(CASE WHEN status = 'paid' THEN total_commission_amount ELSE 0 END), 0)
                AS paid_commission_amount
        FROM commission_settlements
        """,
    )
    row = rows[0] if rows else {}
    return {
        "orderCount": _int(row.get("order_count")),
        "orderAmount": _int(row.get("order_amount")),
        "commissionAmount": _int(row.get("commission_amount")),
        "paidCommissionAmount": _int(row.get("paid_commission_amount")),
    }


def _invite_sums(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "invite_relations"):
        return {"rewardPoints": 0, "grantedRewardPoints": 0}
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            COALESCE(SUM(reward_points), 0) AS reward_points,
            COALESCE(SUM(CASE WHEN reward_status = 'granted' THEN reward_points ELSE 0 END), 0)
                AS granted_reward_points
        FROM invite_relations
        """,
    )
    row = rows[0] if rows else {}
    return {
        "rewardPoints": _int(row.get("reward_points")),
        "grantedRewardPoints": _int(row.get("granted_reward_points")),
    }


def _top_commission_agents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "commission_orders"):
        return []
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            agent_id,
            COUNT(*) AS order_count,
            COALESCE(SUM(order_amount), 0) AS order_amount,
            COALESCE(SUM(commission_amount), 0) AS commission_amount,
            COALESCE(SUM(CASE WHEN status = 'settled' THEN commission_amount ELSE 0 END), 0)
                AS settled_commission_amount
        FROM commission_orders
        GROUP BY agent_id
        ORDER BY commission_amount DESC, order_count DESC, agent_id ASC
        LIMIT 5
        """,
    )
    return [
        {
            "agentId": row["agent_id"],
            "orderCount": _int(row["order_count"]),
            "orderAmount": _int(row["order_amount"]),
            "commissionAmount": _int(row["commission_amount"]),
            "settledCommissionAmount": _int(row["settled_commission_amount"]),
        }
        for row in rows
    ]


def _recent_risk_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "risk_audit_logs"):
        return []
    rows = _fetch_dicts(
        conn,
        """
        SELECT id, user_id, agent_id, asset_id, event_type, risk_level, decision, ip, deny_reason, created_at
        FROM risk_audit_logs
        ORDER BY datetime(created_at) DESC, created_at DESC, id DESC
        LIMIT 10
        """,
    )
    return [
        {
            "id": row["id"],
            "userId": row["user_id"],
            "agentId": row["agent_id"],
            "assetId": row["asset_id"],
            "eventType": row["event_type"],
            "riskLevel": row["risk_level"],
            "decision": row["decision"],
            "ip": row["ip"],
            "denyReason": row["deny_reason"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def _asset_allowed_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "asset_access_logs"):
        return {"total": 0, "allowed": 0, "denied": 0}
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN allowed = 1 THEN 1 ELSE 0 END), 0) AS allowed,
            COALESCE(SUM(CASE WHEN allowed = 0 THEN 1 ELSE 0 END), 0) AS denied
        FROM asset_access_logs
        """,
    )
    row = rows[0] if rows else {}
    return {"total": _int(row.get("total")), "allowed": _int(row.get("allowed")), "denied": _int(row.get("denied"))}


def _top_deny_reason(conn: sqlite3.Connection) -> str:
    if not _table_exists(conn, "asset_access_logs"):
        return ""
    rows = _fetch_dicts(
        conn,
        """
        SELECT deny_reason
        FROM asset_access_logs
        WHERE allowed = 0 AND TRIM(deny_reason) <> ''
        GROUP BY deny_reason
        ORDER BY COUNT(*) DESC, deny_reason ASC
        LIMIT 1
        """,
    )
    return str(rows[0].get("deny_reason") or "") if rows else ""


def _top_asset_access(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "asset_access_logs"):
        return []
    rows = _fetch_dicts(
        conn,
        """
        SELECT
            asset_id,
            COUNT(*) AS access_count,
            COALESCE(SUM(CASE WHEN allowed = 0 THEN 1 ELSE 0 END), 0) AS denied_count
        FROM asset_access_logs
        GROUP BY asset_id
        ORDER BY access_count DESC, denied_count DESC, asset_id ASC
        LIMIT 5
        """,
    )
    return [
        {
            "assetId": row["asset_id"],
            "accessCount": _int(row["access_count"]),
            "deniedCount": _int(row["denied_count"]),
        }
        for row in rows
    ]


def _recent_denied_asset_access(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "asset_access_logs"):
        return []
    rows = _fetch_dicts(
        conn,
        """
        SELECT id, user_id, agent_id, asset_id, asset_type, action, ip, deny_reason, request_id, created_at
        FROM asset_access_logs
        WHERE allowed = 0
        ORDER BY datetime(created_at) DESC, created_at DESC, id DESC
        LIMIT 10
        """,
    )
    return [
        {
            "id": row["id"],
            "userId": row["user_id"],
            "agentId": row["agent_id"],
            "assetId": row["asset_id"],
            "assetType": row["asset_type"],
            "action": row["action"],
            "ip": row["ip"],
            "denyReason": row["deny_reason"],
            "requestId": row["request_id"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def _stats_by_job(
    conn: sqlite3.Connection,
    table: str,
    sql_template: str,
    job_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not job_ids or not _table_exists(conn, table):
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = _fetch_dicts(conn, sql_template.format(placeholders=placeholders), job_ids)
    return {str(row["job_id"]): row for row in rows if row.get("job_id") is not None}


def _top_groups(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    count_key: str,
    *,
    skip_blank: bool = False,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    where = f"WHERE {column} <> ''" if skip_blank else ""
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT {column} AS name, COUNT(*) AS item_count
        FROM {table}
        {where}
        GROUP BY {column}
        ORDER BY item_count DESC, name ASC
        LIMIT 5
        """,
    )
    return [{"name": row["name"], count_key: _int(row["item_count"])} for row in rows]


def _status_counts(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    expected: Iterable[str],
) -> dict[str, int]:
    counts = {key: 0 for key in expected}
    if not _table_exists(conn, table):
        return counts
    rows = _fetch_dicts(
        conn,
        f"""
        SELECT {column} AS status_key, COUNT(*) AS status_count
        FROM {table}
        GROUP BY {column}
        """,
    )
    for row in rows:
        key = str(row["status_key"] or "")
        if key:
            counts[key] = _int(row["status_count"])
    return counts


def _highest_risk_level(level_counts: dict[str, int]) -> str:
    for level in reversed(RISK_LEVELS):
        if _int(level_counts.get(level)):
            return level
    return "info"


def _sum_columns(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> dict[str, int]:
    sums = {column: 0 for column in columns}
    if not _table_exists(conn, table):
        return sums
    select_sql = ", ".join(f"COALESCE(SUM({column}), 0) AS {column}" for column in columns)
    rows = _fetch_dicts(conn, f"SELECT {select_sql} FROM {table}")
    if not rows:
        return sums
    return {column: _int(rows[0].get(column)) for column in columns}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _fetch_dicts(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params or ()))
    columns = [description[0] for description in cursor.description]
    return [{column: row[index] for index, column in enumerate(columns)} for row in cursor.fetchall()]


def _sum_counts(counts: dict[str, int]) -> int:
    return sum(_int(value) for value in counts.values())


def _ratio(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round(part / whole, 4)


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)
