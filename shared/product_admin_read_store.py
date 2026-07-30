from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterator, Protocol


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MAX_PAGE_OFFSET = 1_000_000
MAX_RECENT_JOBS = 100

USER_STATUSES = frozenset({"active", "disabled"})
STORE_STATUSES = frozenset({"active", "archived"})
PAYMENT_STATUSES = frozenset(
    {
        "pending",
        "paid",
        "partially_refunded",
        "refunded",
        "failed",
        "closed",
    }
)
PAYMENT_PROVIDERS = frozenset({"alipay", "wechat"})
JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "canceled"}
)
COMMISSION_ORDER_STATUSES = (
    "pending",
    "eligible",
    "claimed",
    "settled",
    "canceled",
    "refunded",
)
SETTLEMENT_STATUSES = frozenset(
    {"pending", "processing", "paid", "failed", "canceled"}
)
WITHDRAWAL_STATUSES = frozenset(
    {"pending", "approved", "rejected", "paid", "canceled"}
)

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")


class CursorLike(Protocol):
    description: Sequence[Any] | None

    def execute(
        self,
        operation: str,
        parameters: Sequence[Any] = (),
    ) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    autocommit: bool

    def cursor(self) -> CursorLike: ...

    def rollback(self) -> Any: ...


class ProductAdminReadStoreError(RuntimeError):
    """Base error for fail-closed PostgreSQL admin reads."""


class InvalidProductAdminReadInput(
    ProductAdminReadStoreError,
    ValueError,
):
    """Raised before SQL when a read filter or page argument is invalid."""


class ProductAdminReadQueryError(ProductAdminReadStoreError):
    """Raised when PostgreSQL cannot execute a required admin read."""


_BEGIN_READ_ONLY_SQL = """
/* product_admin_read_store:begin_read */
SET TRANSACTION READ ONLY
""".strip()


class ProductAdminReadStore:
    """Read-only PostgreSQL queries for the production admin console."""

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductAdminReadInput(
                "ProductAdminReadStore requires an autocommit-disabled "
                "connection"
            )
        self.connection = connection

    def list_users(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        created_from: str | datetime | None = None,
        created_to: str | datetime | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _page_args(limit, offset)
        sort_key, sort_expr, direction = _sort_args(
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
        clean_status = _optional_choice(status, USER_STATUSES, "user status")
        clauses: list[str] = []
        params: list[Any] = []
        _append_equal(clauses, params, "u.status", clean_status)
        _append_timestamp(
            clauses,
            params,
            "u.created_at",
            ">=",
            created_from,
            "created_from",
        )
        _append_timestamp(
            clauses,
            params,
            "u.created_at",
            "<=",
            created_to,
            "created_to",
        )
        _append_search(
            clauses,
            params,
            search,
            ("u.id", "u.phone"),
        )
        where_sql = _where_sql(clauses)

        with self._read_cursor("list_users") as cursor:
            cursor.execute(
                f"""
                /* product_admin_read_store:list_users_count */
                SELECT COUNT(*) AS item_count
                FROM product_users u
                {where_sql}
                """,
                tuple(params),
            )
            total = _required_count(cursor, "list_users")
            cursor.execute(
                f"""
                /* product_admin_read_store:list_users_rows */
                SELECT
                    u.id AS id,
                    u.phone AS phone,
                    u.status AS status,
                    u.created_at AS created_at,
                    u.updated_at AS updated_at,
                    u.last_login_at AS last_login_at,
                    (
                        SELECT COUNT(*)
                        FROM product_user_stores us
                        WHERE us.user_id = u.id
                    ) AS store_count,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders po
                        WHERE po.owner_user_id = u.id
                    ) AS order_count,
                    (
                        SELECT COALESCE(
                            SUM(
                                CASE
                                    WHEN po.status IN (
                                        'paid',
                                        'partially_refunded',
                                        'refunded'
                                    )
                                    THEN po.amount_cents
                                        - po.refunded_amount_cents
                                    ELSE 0
                                END
                            ),
                            0
                        )
                        FROM product_payment_orders po
                        WHERE po.owner_user_id = u.id
                    ) AS payment_amount_cents,
                    COALESCE(pa.balance_points, 0) AS point_balance
                FROM product_users u
                LEFT JOIN product_point_accounts pa
                    ON pa.owner_user_id = u.id
                {where_sql}
                ORDER BY {sort_expr} {direction}, id ASC
                LIMIT %s OFFSET %s
                """,
                (*params, page_limit, page_offset),
            )
            rows = _fetchall_dicts(cursor)
        return _page(
            [_user_payload(row) for row in rows],
            total,
            page_limit,
            page_offset,
            sort_key,
            direction,
        )

    def list_stores(
        self,
        *,
        status: str | None = None,
        created_by_user_id: str | None = None,
        search: str | None = None,
        created_from: str | datetime | None = None,
        created_to: str | datetime | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _page_args(limit, offset)
        sort_key, sort_expr, direction = _sort_args(
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
            },
            "created_at",
        )
        clean_status = _optional_choice(status, STORE_STATUSES, "store status")
        creator = _optional_identifier(
            created_by_user_id,
            "created_by_user_id",
        )
        clauses: list[str] = []
        params: list[Any] = []
        _append_equal(clauses, params, "s.status", clean_status)
        _append_equal(clauses, params, "s.created_by_user_id", creator)
        _append_timestamp(
            clauses,
            params,
            "s.created_at",
            ">=",
            created_from,
            "created_from",
        )
        _append_timestamp(
            clauses,
            params,
            "s.created_at",
            "<=",
            created_to,
            "created_to",
        )
        _append_search(
            clauses,
            params,
            search,
            ("s.id", "s.name"),
        )
        where_sql = _where_sql(clauses)

        with self._read_cursor("list_stores") as cursor:
            cursor.execute(
                f"""
                /* product_admin_read_store:list_stores_count */
                SELECT COUNT(*) AS item_count
                FROM product_stores s
                {where_sql}
                """,
                tuple(params),
            )
            total = _required_count(cursor, "list_stores")
            cursor.execute(
                f"""
                /* product_admin_read_store:list_stores_rows */
                SELECT
                    s.id AS id,
                    s.name AS name,
                    s.status AS status,
                    s.created_by_user_id AS created_by_user_id,
                    s.created_at AS created_at,
                    s.updated_at AS updated_at,
                    (
                        SELECT COUNT(*)
                        FROM product_user_stores us
                        WHERE us.store_id = s.id
                    ) AS user_count,
                    (
                        SELECT COUNT(*)
                        FROM product_menu_uploads mu
                        WHERE mu.store_name = s.name
                          AND EXISTS (
                              SELECT 1
                              FROM product_user_stores owner_store
                              WHERE owner_store.store_id = s.id
                                AND owner_store.user_id = mu.owner_user_id
                          )
                    ) AS menu_upload_count,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_jobs gj
                        JOIN product_menu_uploads mu
                            ON mu.id = gj.menu_upload_id
                        WHERE mu.store_name = s.name
                          AND EXISTS (
                              SELECT 1
                              FROM product_user_stores owner_store
                              WHERE owner_store.store_id = s.id
                                AND owner_store.user_id = mu.owner_user_id
                          )
                    ) AS job_count,
                    NULL::BIGINT AS asset_count
                FROM product_stores s
                {where_sql}
                ORDER BY {sort_expr} {direction}, id ASC
                LIMIT %s OFFSET %s
                """,
                (*params, page_limit, page_offset),
            )
            rows = _fetchall_dicts(cursor)
        return _page(
            [_store_payload(row) for row in rows],
            total,
            page_limit,
            page_offset,
            sort_key,
            direction,
        )

    def list_orders(
        self,
        *,
        status: str | None = None,
        user_id: str | None = None,
        provider: str | None = None,
        search: str | None = None,
        created_from: str | datetime | None = None,
        created_to: str | datetime | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _page_args(limit, offset)
        sort_key, sort_expr, direction = _sort_args(
            sort,
            order,
            {
                "id": "id",
                "order_id": "id",
                "user_id": "owner_user_id",
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
        clean_status = _optional_choice(
            status,
            PAYMENT_STATUSES,
            "payment status",
        )
        owner = _optional_identifier(user_id, "user_id")
        clean_provider = _optional_choice(
            provider,
            PAYMENT_PROVIDERS,
            "payment provider",
        )
        clauses: list[str] = []
        params: list[Any] = []
        _append_equal(clauses, params, "po.status", clean_status)
        _append_equal(clauses, params, "po.owner_user_id", owner)
        _append_equal(clauses, params, "po.provider", clean_provider)
        _append_timestamp(
            clauses,
            params,
            "po.created_at",
            ">=",
            created_from,
            "created_from",
        )
        _append_timestamp(
            clauses,
            params,
            "po.created_at",
            "<=",
            created_to,
            "created_to",
        )
        _append_search(
            clauses,
            params,
            search,
            ("po.id", "po.provider_order_id", "po.owner_user_id"),
        )
        where_sql = _where_sql(clauses)

        with self._read_cursor("list_orders") as cursor:
            cursor.execute(
                f"""
                /* product_admin_read_store:list_orders_count */
                SELECT COUNT(*) AS item_count
                FROM product_payment_orders po
                {where_sql}
                """,
                tuple(params),
            )
            total = _required_count(cursor, "list_orders")
            cursor.execute(
                f"""
                /* product_admin_read_store:list_orders_rows */
                SELECT
                    po.id AS id,
                    po.owner_user_id AS owner_user_id,
                    po.provider AS provider,
                    po.provider_order_id AS provider_order_id,
                    po.amount_cents AS amount_cents,
                    po.points AS points,
                    po.status AS status,
                    po.created_at AS created_at,
                    po.updated_at AS updated_at,
                    po.paid_at AS paid_at,
                    po.closed_at AS closed_at,
                    po.refunded_at AS refunded_at
                FROM product_payment_orders po
                {where_sql}
                ORDER BY {sort_expr} {direction}, id ASC
                LIMIT %s OFFSET %s
                """,
                (*params, page_limit, page_offset),
            )
            rows = _fetchall_dicts(cursor)
        return _page(
            [_payment_order_payload(row) for row in rows],
            total,
            page_limit,
            page_offset,
            sort_key,
            direction,
        )

    def list_payment_orders(self, **kwargs: Any) -> dict[str, Any]:
        return self.list_orders(**kwargs)

    def list_generation_tasks(
        self,
        *,
        status: str | None = None,
        menu_upload_id: str | None = None,
        store_name: str | None = None,
        style_id: str | None = None,
        search: str | None = None,
        created_from: str | datetime | None = None,
        created_to: str | datetime | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _page_args(limit, offset)
        sort_key, sort_expr, direction = _sort_args(
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
        clean_status = _optional_choice(status, JOB_STATUSES, "job status")
        menu_id = _optional_identifier(menu_upload_id, "menu_upload_id")
        clean_store_name = _optional_text(
            store_name,
            "store_name",
            maximum=120,
        )
        clean_style_id = _optional_text(
            style_id,
            "style_id",
            maximum=128,
        )
        clauses: list[str] = []
        params: list[Any] = []
        _append_equal(clauses, params, "j.status", clean_status)
        _append_equal(clauses, params, "j.menu_upload_id", menu_id)
        _append_equal(clauses, params, "m.store_name", clean_store_name)
        if clean_style_id:
            clauses.append(
                "COALESCE("
                "j.request_payload #>> '{selectedBackground,styleId}', "
                "''"
                ") = %s"
            )
            params.append(clean_style_id)
        _append_timestamp(
            clauses,
            params,
            "j.created_at",
            ">=",
            created_from,
            "created_from",
        )
        _append_timestamp(
            clauses,
            params,
            "j.created_at",
            "<=",
            created_to,
            "created_to",
        )
        _append_search(
            clauses,
            params,
            search,
            (
                "j.id",
                "j.menu_upload_id",
                "m.store_name",
                "m.original_filename",
                "COALESCE("
                "j.request_payload #>> "
                "'{selectedBackground,styleId}', '')",
            ),
        )
        where_sql = _where_sql(clauses)

        with self._read_cursor("list_generation_tasks") as cursor:
            cursor.execute(
                f"""
                /* product_admin_read_store:list_generation_tasks_count */
                SELECT COUNT(*) AS item_count
                FROM product_generation_jobs j
                LEFT JOIN product_menu_uploads m
                    ON m.id = j.menu_upload_id
                {where_sql}
                """,
                tuple(params),
            )
            total = _required_count(cursor, "list_generation_tasks")
            cursor.execute(
                f"""
                /* product_admin_read_store:list_generation_tasks_rows */
                SELECT
                    j.id AS id,
                    j.owner_user_id AS owner_user_id,
                    j.menu_upload_id AS menu_upload_id,
                    COALESCE(m.store_name, '') AS store_name,
                    COALESCE(m.original_filename, '')
                        AS original_filename,
                    COALESCE(
                        j.request_payload
                            #>> '{{selectedBackground,styleId}}',
                        ''
                    ) AS style_id,
                    CASE
                        WHEN jsonb_typeof(j.request_payload->'quality')
                            = 'object'
                        THEN COALESCE(
                            j.request_payload #>> '{{quality,id}}',
                            ''
                        )
                        ELSE COALESCE(
                            j.request_payload->>'quality',
                            ''
                        )
                    END AS quality,
                    j.status AS status,
                    j.requested_count AS requested_count,
                    j.completed_count AS completed_count,
                    j.failed_count AS failed_count,
                    j.error_message AS error_message,
                    j.created_at AS created_at,
                    j.updated_at AS updated_at,
                    j.started_at AS started_at,
                    j.completed_at AS completed_at,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results result
                        WHERE result.job_id = j.id
                    ) AS image_count,
                    NULL::BIGINT AS export_count,
                    (
                        SELECT COALESCE(SUM(ledger.delta_points), 0)
                        FROM product_point_ledger ledger
                        JOIN product_point_orders point_order
                            ON point_order.id = ledger.order_id
                        WHERE point_order.job_id = j.id
                    ) AS point_delta
                FROM product_generation_jobs j
                LEFT JOIN product_menu_uploads m
                    ON m.id = j.menu_upload_id
                {where_sql}
                ORDER BY {sort_expr} {direction}, id ASC
                LIMIT %s OFFSET %s
                """,
                (*params, page_limit, page_offset),
            )
            rows = _fetchall_dicts(cursor)
        return _page(
            [_generation_task_payload(row) for row in rows],
            total,
            page_limit,
            page_offset,
            sort_key,
            direction,
        )

    def list_generation_jobs(self, **kwargs: Any) -> dict[str, Any]:
        return self.list_generation_tasks(**kwargs)

    def list_commission_settlements(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        search: str | None = None,
        created_from: str | datetime | None = None,
        created_to: str | datetime | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _page_args(limit, offset)
        sort_key, sort_expr, direction = _sort_args(
            sort,
            order,
            {
                "id": "id",
                "agent_id": "agent_id",
                "settlement_no": "settlement_no",
                "total_order_amount": "total_order_amount_cents",
                "total_order_amount_cents": "total_order_amount_cents",
                "total_commission_amount": "total_commission_amount_cents",
                "total_commission_amount_cents":
                    "total_commission_amount_cents",
                "order_count": "order_count",
                "status": "status",
                "created_at": "created_at",
                "updated_at": "updated_at",
                "paid_at": "paid_at",
            },
            "created_at",
        )
        clean_status = _optional_choice(
            status,
            SETTLEMENT_STATUSES,
            "commission settlement status",
        )
        agent = _optional_identifier(agent_id, "agent_id")
        clauses: list[str] = []
        params: list[Any] = []
        _append_equal(clauses, params, "settlement.status", clean_status)
        _append_equal(clauses, params, "settlement.agent_id", agent)
        _append_timestamp(
            clauses,
            params,
            "settlement.created_at",
            ">=",
            created_from,
            "created_from",
        )
        _append_timestamp(
            clauses,
            params,
            "settlement.created_at",
            "<=",
            created_to,
            "created_to",
        )
        _append_search(
            clauses,
            params,
            search,
            (
                "settlement.id",
                "settlement.settlement_no",
                "settlement.agent_id",
            ),
        )
        where_sql = _where_sql(clauses)

        with self._read_cursor("list_commission_settlements") as cursor:
            cursor.execute(
                f"""
                /* product_admin_read_store:list_commission_settlements_count */
                SELECT COUNT(*) AS item_count
                FROM product_commission_settlements settlement
                {where_sql}
                """,
                tuple(params),
            )
            total = _required_count(
                cursor,
                "list_commission_settlements",
            )
            cursor.execute(
                f"""
                /* product_admin_read_store:list_commission_settlements_rows */
                SELECT
                    settlement.id AS id,
                    settlement.agent_id AS agent_id,
                    settlement.settlement_no AS settlement_no,
                    settlement.total_order_amount_cents
                        AS total_order_amount_cents,
                    settlement.total_commission_amount_cents
                        AS total_commission_amount_cents,
                    settlement.order_count AS order_count,
                    settlement.currency AS currency,
                    settlement.status AS status,
                    settlement.paid_at AS paid_at,
                    settlement.failure_reason AS failure_reason,
                    settlement.created_at AS created_at,
                    settlement.updated_at AS updated_at
                FROM product_commission_settlements settlement
                {where_sql}
                ORDER BY {sort_expr} {direction}, id ASC
                LIMIT %s OFFSET %s
                """,
                (*params, page_limit, page_offset),
            )
            rows = _fetchall_dicts(cursor)
        return _page(
            [_commission_settlement_payload(row) for row in rows],
            total,
            page_limit,
            page_offset,
            sort_key,
            direction,
        )

    def list_withdrawals(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        created_from: str | datetime | None = None,
        created_to: str | datetime | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _page_args(limit, offset)
        sort_key, sort_expr, direction = _sort_args(
            sort,
            order,
            {
                "id": "id",
                "withdrawal_id": "id",
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
        clean_status = _optional_choice(
            status,
            WITHDRAWAL_STATUSES,
            "withdrawal status",
        )
        agent = _optional_identifier(agent_id, "agent_id")
        clauses: list[str] = []
        params: list[Any] = []
        _append_equal(clauses, params, "withdrawal.agent_id", agent)
        _append_equal(clauses, params, "withdrawal.status", clean_status)
        _append_timestamp(
            clauses,
            params,
            "withdrawal.created_at",
            ">=",
            created_from,
            "created_from",
        )
        _append_timestamp(
            clauses,
            params,
            "withdrawal.created_at",
            "<=",
            created_to,
            "created_to",
        )
        _append_search(
            clauses,
            params,
            search,
            (
                "withdrawal.id",
                "withdrawal.agent_id",
                "withdrawal.status_reason",
                "withdrawal.metadata::TEXT",
            ),
        )
        where_sql = _where_sql(clauses)

        with self._read_cursor("list_withdrawals") as cursor:
            cursor.execute(
                f"""
                /* product_admin_read_store:list_withdrawals_count */
                SELECT COUNT(*) AS item_count
                FROM product_agent_withdrawals withdrawal
                {where_sql}
                """,
                tuple(params),
            )
            total = _required_count(cursor, "list_withdrawals")
            cursor.execute(
                f"""
                /* product_admin_read_store:list_withdrawals_rows */
                SELECT
                    withdrawal.id AS id,
                    withdrawal.agent_id AS agent_id,
                    withdrawal.amount_cents AS amount_cents,
                    withdrawal.currency AS currency,
                    withdrawal.status AS status,
                    withdrawal.balance_snapshot AS balance_snapshot,
                    withdrawal.status_reason AS status_reason,
                    withdrawal.created_at AS created_at,
                    withdrawal.updated_at AS updated_at,
                    withdrawal.approved_at AS approved_at,
                    withdrawal.rejected_at AS rejected_at,
                    withdrawal.paid_at AS paid_at,
                    withdrawal.canceled_at AS canceled_at
                FROM product_agent_withdrawals withdrawal
                {where_sql}
                ORDER BY {sort_expr} {direction}, id ASC
                LIMIT %s OFFSET %s
                """,
                (*params, page_limit, page_offset),
            )
            rows = _fetchall_dicts(cursor)
        return _page(
            [_withdrawal_payload(row) for row in rows],
            total,
            page_limit,
            page_offset,
            sort_key,
            direction,
        )

    def dashboard_summary(self) -> dict[str, Any]:
        with self._read_cursor("dashboard_summary") as cursor:
            cursor.execute(
                """
                /* product_admin_read_store:dashboard_summary */
                SELECT
                    (SELECT COUNT(*) FROM product_users)
                        AS user_total,
                    (SELECT COUNT(*) FROM product_stores)
                        AS store_total,

                    (SELECT COUNT(*) FROM product_payment_orders)
                        AS payment_total,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders
                        WHERE status = 'pending'
                    ) AS payment_pending,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders
                        WHERE status = 'paid'
                    ) AS payment_paid,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders
                        WHERE status = 'partially_refunded'
                    ) AS payment_partially_refunded,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders
                        WHERE status = 'refunded'
                    ) AS payment_refunded,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders
                        WHERE status = 'failed'
                    ) AS payment_failed,
                    (
                        SELECT COUNT(*)
                        FROM product_payment_orders
                        WHERE status = 'closed'
                    ) AS payment_closed,
                    (
                        SELECT COALESCE(SUM(amount_cents), 0)
                        FROM product_payment_orders
                        WHERE status IN (
                            'paid',
                            'partially_refunded',
                            'refunded'
                        )
                    ) AS payment_gross_amount_cents,
                    (
                        SELECT COALESCE(
                            SUM(refunded_amount_cents),
                            0
                        )
                        FROM product_payment_orders
                    ) AS payment_refunded_amount_cents,
                    (
                        SELECT COALESCE(
                            SUM(
                                CASE
                                    WHEN status IN (
                                        'paid',
                                        'partially_refunded',
                                        'refunded'
                                    )
                                    THEN amount_cents
                                        - refunded_amount_cents
                                    ELSE 0
                                END
                            ),
                            0
                        )
                        FROM product_payment_orders
                    ) AS payment_net_amount_cents,
                    (
                        SELECT COALESCE(SUM(credited_points), 0)
                        FROM product_payment_orders
                    ) AS payment_credited_points,
                    (
                        SELECT COALESCE(SUM(refunded_points), 0)
                        FROM product_payment_orders
                    ) AS payment_refunded_points,

                    (SELECT COUNT(*) FROM product_generation_jobs)
                        AS job_total,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_jobs
                        WHERE status = 'queued'
                    ) AS job_queued,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_jobs
                        WHERE status = 'running'
                    ) AS job_running,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_jobs
                        WHERE status = 'succeeded'
                    ) AS job_succeeded,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_jobs
                        WHERE status = 'failed'
                    ) AS job_failed,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_jobs
                        WHERE status = 'canceled'
                    ) AS job_canceled,
                    (
                        SELECT COALESCE(SUM(requested_count), 0)
                        FROM product_generation_jobs
                    ) AS job_requested,
                    (
                        SELECT COALESCE(SUM(completed_count), 0)
                        FROM product_generation_jobs
                    ) AS job_completed,
                    (
                        SELECT COALESCE(SUM(failed_count), 0)
                        FROM product_generation_jobs
                    ) AS job_failed_items,

                    (SELECT COUNT(*) FROM product_generation_results)
                        AS image_total,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results
                        WHERE status = 'generated'
                    ) AS image_generated,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results
                        WHERE status = 'failed'
                    ) AS image_failed,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results
                        WHERE status = 'rejected'
                    ) AS image_rejected,

                    (SELECT COUNT(*) FROM product_point_ledger)
                        AS point_entries,
                    (
                        SELECT COALESCE(
                            SUM(
                                lifetime_credited_points
                                    + lifetime_refunded_points
                            ),
                            0
                        )
                        FROM product_point_accounts
                    ) AS point_credited,
                    (
                        SELECT COALESCE(
                            SUM(lifetime_debited_points),
                            0
                        )
                        FROM product_point_accounts
                    ) AS point_debited,
                    (
                        SELECT COALESCE(
                            SUM(lifetime_refunded_points),
                            0
                        )
                        FROM product_point_accounts
                    ) AS point_refunded,
                    (
                        SELECT COALESCE(SUM(balance_points), 0)
                        FROM product_point_accounts
                    ) AS point_balance,

                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                    ) AS agent_total,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                        WHERE status = 'active'
                    ) AS agent_active,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                        WHERE status = 'suspended'
                    ) AS agent_suspended,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                        WHERE status = 'closed'
                    ) AS agent_closed,

                    (SELECT COUNT(*) FROM product_commission_orders)
                        AS commission_order_total,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'pending'
                    ) AS commission_order_pending,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'eligible'
                    ) AS commission_order_eligible,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'claimed'
                    ) AS commission_order_claimed,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'settled'
                    ) AS commission_order_settled,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'canceled'
                    ) AS commission_order_canceled,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'refunded'
                    ) AS commission_order_refunded,
                    (
                        SELECT COALESCE(
                            SUM(order_amount_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS commission_gross_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(refunded_order_amount_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS commission_refunded_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                order_amount_cents
                                    - refunded_order_amount_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                    ) AS commission_net_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(commission_amount_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS commission_gross_amount,
                    (
                        SELECT COALESCE(
                            SUM(reversed_commission_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS commission_reversed_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                    ) AS commission_net_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                        WHERE status = 'pending'
                    ) AS commission_pending_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                        WHERE status = 'eligible'
                    ) AS commission_eligible_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                        WHERE status = 'settled'
                    ) AS commission_settled_amount,

                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                    ) AS settlement_total,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'pending'
                    ) AS settlement_pending,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'processing'
                    ) AS settlement_processing,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'paid'
                    ) AS settlement_paid,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'failed'
                    ) AS settlement_failed,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'canceled'
                    ) AS settlement_canceled,
                    (
                        SELECT COALESCE(
                            SUM(total_commission_amount_cents),
                            0
                        )
                        FROM product_commission_settlements
                    ) AS settlement_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(total_commission_amount_cents),
                            0
                        )
                        FROM product_commission_settlements
                        WHERE status = 'paid'
                    ) AS settlement_paid_commission_amount,

                    (
                        SELECT COUNT(*)
                        FROM product_agent_withdrawals
                    ) AS withdrawal_total,
                    (
                        SELECT COUNT(*)
                        FROM product_agent_withdrawals
                        WHERE status = 'pending'
                    ) AS withdrawal_pending,
                    (
                        SELECT COUNT(*)
                        FROM product_agent_withdrawals
                        WHERE status = 'approved'
                    ) AS withdrawal_approved,
                    (
                        SELECT COUNT(*)
                        FROM product_agent_withdrawals
                        WHERE status = 'rejected'
                    ) AS withdrawal_rejected,
                    (
                        SELECT COUNT(*)
                        FROM product_agent_withdrawals
                        WHERE status = 'paid'
                    ) AS withdrawal_paid,
                    (
                        SELECT COUNT(*)
                        FROM product_agent_withdrawals
                        WHERE status = 'canceled'
                    ) AS withdrawal_canceled,
                    (
                        SELECT COALESCE(SUM(amount_cents), 0)
                        FROM product_agent_withdrawals
                    ) AS withdrawal_requested_amount,
                    (
                        SELECT COALESCE(SUM(amount_cents), 0)
                        FROM product_agent_withdrawals
                        WHERE status IN ('pending', 'approved')
                    ) AS withdrawal_reserved_amount,
                    (
                        SELECT COALESCE(SUM(amount_cents), 0)
                        FROM product_agent_withdrawals
                        WHERE status = 'paid'
                    ) AS withdrawal_paid_amount,

                    (
                        SELECT COUNT(*)
                        FROM product_growth_invite_relations
                    ) AS invite_total,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_reward_grants
                    ) AS reward_grant_total,
                    (
                        SELECT COALESCE(SUM(points), 0)
                        FROM product_growth_reward_grants
                    ) AS reward_points,
                    (
                        SELECT COALESCE(SUM(debt_offset_points), 0)
                        FROM product_growth_reward_grants
                    ) AS reward_debt_offset_points,
                    (
                        SELECT COALESCE(SUM(net_wallet_points), 0)
                        FROM product_growth_reward_grants
                    ) AS reward_wallet_points
                """,
            )
            row = _required_row(cursor, "dashboard_summary")
        return _dashboard_payload(row)

    def recent_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        clean_limit = _bounded_int(
            limit,
            "limit",
            minimum=0,
            maximum=MAX_RECENT_JOBS,
        )
        if clean_limit == 0:
            return []
        with self._read_cursor("recent_jobs") as cursor:
            cursor.execute(
                """
                /* product_admin_read_store:recent_jobs */
                SELECT
                    j.id AS id,
                    j.menu_upload_id AS menu_upload_id,
                    COALESCE(
                        j.request_payload
                            #>> '{selectedBackground,styleId}',
                        ''
                    ) AS style_id,
                    CASE
                        WHEN jsonb_typeof(j.request_payload->'quality')
                            = 'object'
                        THEN COALESCE(
                            j.request_payload #>> '{quality,id}',
                            ''
                        )
                        ELSE COALESCE(
                            j.request_payload->>'quality',
                            ''
                        )
                    END AS quality,
                    j.status AS status,
                    j.requested_count AS requested_count,
                    j.completed_count AS completed_count,
                    j.failed_count AS failed_count,
                    j.error_message AS error_message,
                    j.created_at AS created_at,
                    j.updated_at AS updated_at,
                    j.started_at AS started_at,
                    j.completed_at AS completed_at,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results result
                        WHERE result.job_id = j.id
                    ) AS image_count,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results result
                        WHERE result.job_id = j.id
                          AND result.status = 'generated'
                    ) AS generated_image_count,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results result
                        WHERE result.job_id = j.id
                          AND result.status = 'failed'
                    ) AS failed_image_count,
                    (
                        SELECT COUNT(*)
                        FROM product_generation_results result
                        WHERE result.job_id = j.id
                          AND result.status = 'rejected'
                    ) AS rejected_image_count,
                    (
                        SELECT COALESCE(SUM(ledger.delta_points), 0)
                        FROM product_point_ledger ledger
                        JOIN product_point_orders point_order
                            ON point_order.id = ledger.order_id
                        WHERE point_order.job_id = j.id
                    ) AS point_delta,
                    (
                        SELECT COALESCE(
                            SUM(
                                CASE
                                    WHEN ledger.delta_points < 0
                                    THEN -ledger.delta_points
                                    ELSE 0
                                END
                            ),
                            0
                        )
                        FROM product_point_ledger ledger
                        JOIN product_point_orders point_order
                            ON point_order.id = ledger.order_id
                        WHERE point_order.job_id = j.id
                    ) AS points_debited,
                    (
                        SELECT COALESCE(
                            SUM(
                                CASE
                                    WHEN ledger.delta_points > 0
                                    THEN ledger.delta_points
                                    ELSE 0
                                END
                            ),
                            0
                        )
                        FROM product_point_ledger ledger
                        JOIN product_point_orders point_order
                            ON point_order.id = ledger.order_id
                        WHERE point_order.job_id = j.id
                    ) AS points_credited
                FROM product_generation_jobs j
                ORDER BY j.created_at DESC, j.id DESC
                LIMIT %s
                """,
                (clean_limit,),
            )
            rows = _fetchall_dicts(cursor)
        return [_recent_job_payload(row) for row in rows]

    def commission_summary(self) -> dict[str, Any]:
        with self._read_cursor("commission_summary") as cursor:
            cursor.execute(
                """
                /* product_admin_read_store:commission_summary */
                SELECT
                    (SELECT COUNT(*) FROM product_commission_orders)
                        AS order_total,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'pending'
                    ) AS order_pending,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'eligible'
                    ) AS order_eligible,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'claimed'
                    ) AS order_claimed,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'settled'
                    ) AS order_settled,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'canceled'
                    ) AS order_canceled,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_orders
                        WHERE status = 'refunded'
                    ) AS order_refunded,
                    (
                        SELECT COALESCE(
                            SUM(order_amount_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS gross_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(refunded_order_amount_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS refunded_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                order_amount_cents
                                    - refunded_order_amount_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                    ) AS net_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(commission_amount_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS gross_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(reversed_commission_cents),
                            0
                        )
                        FROM product_commission_orders
                    ) AS reversed_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                    ) AS net_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                        WHERE status = 'pending'
                    ) AS pending_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                        WHERE status = 'eligible'
                    ) AS eligible_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(
                                commission_amount_cents
                                    - reversed_commission_cents
                            ),
                            0
                        )
                        FROM product_commission_orders
                        WHERE status = 'settled'
                    ) AS settled_commission_amount,

                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                    ) AS settlement_total,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'pending'
                    ) AS settlement_pending,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'processing'
                    ) AS settlement_processing,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'paid'
                    ) AS settlement_paid,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'failed'
                    ) AS settlement_failed,
                    (
                        SELECT COUNT(*)
                        FROM product_commission_settlements
                        WHERE status = 'canceled'
                    ) AS settlement_canceled,
                    (
                        SELECT COALESCE(SUM(order_count), 0)
                        FROM product_commission_settlements
                    ) AS settlement_order_count,
                    (
                        SELECT COALESCE(
                            SUM(total_order_amount_cents),
                            0
                        )
                        FROM product_commission_settlements
                    ) AS settlement_order_amount,
                    (
                        SELECT COALESCE(
                            SUM(total_commission_amount_cents),
                            0
                        )
                        FROM product_commission_settlements
                    ) AS settlement_commission_amount,
                    (
                        SELECT COALESCE(
                            SUM(total_commission_amount_cents),
                            0
                        )
                        FROM product_commission_settlements
                        WHERE status = 'paid'
                    ) AS paid_commission_amount,

                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                    ) AS agent_total,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                        WHERE status = 'active'
                    ) AS agent_active,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                        WHERE status = 'suspended'
                    ) AS agent_suspended,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_agents
                        WHERE status = 'closed'
                    ) AS agent_closed,

                    (
                        SELECT COUNT(*)
                        FROM product_growth_invite_relations
                    ) AS invite_total,
                    (
                        SELECT COUNT(*)
                        FROM product_growth_reward_grants
                    ) AS reward_grant_total,
                    (
                        SELECT COALESCE(SUM(points), 0)
                        FROM product_growth_reward_grants
                    ) AS reward_points,
                    (
                        SELECT COALESCE(SUM(debt_offset_points), 0)
                        FROM product_growth_reward_grants
                    ) AS debt_offset_points,
                    (
                        SELECT COALESCE(SUM(net_wallet_points), 0)
                        FROM product_growth_reward_grants
                    ) AS granted_reward_points
                """,
            )
            summary_row = _required_row(cursor, "commission_summary")
            cursor.execute(
                """
                /* product_admin_read_store:commission_top_agents */
                SELECT
                    agent_id,
                    COUNT(*) AS order_count,
                    COALESCE(
                        SUM(
                            order_amount_cents
                                - refunded_order_amount_cents
                        ),
                        0
                    ) AS order_amount,
                    COALESCE(
                        SUM(
                            commission_amount_cents
                                - reversed_commission_cents
                        ),
                        0
                    ) AS commission_amount,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN status = 'settled'
                                THEN commission_amount_cents
                                    - reversed_commission_cents
                                ELSE 0
                            END
                        ),
                        0
                    ) AS settled_commission_amount
                FROM product_commission_orders
                GROUP BY agent_id
                ORDER BY commission_amount DESC,
                    order_count DESC,
                    agent_id ASC
                LIMIT %s
                """,
                (5,),
            )
            top_rows = _fetchall_dicts(cursor)
        return _commission_summary_payload(summary_row, top_rows)

    @contextmanager
    def _read_cursor(self, operation: str) -> Iterator[CursorLike]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(_BEGIN_READ_ONLY_SQL)
            yield cursor
        except ProductAdminReadStoreError:
            self._rollback_after_read(operation)
            raise
        except Exception as exc:
            try:
                self._rollback_after_read(operation)
            except ProductAdminReadStoreError as rollback_exc:
                raise ProductAdminReadQueryError(
                    f"PostgreSQL admin read '{operation}' failed and its "
                    f"transaction could not be rolled back: {rollback_exc}"
                ) from exc
            raise ProductAdminReadQueryError(
                f"PostgreSQL admin read '{operation}' failed; required "
                f"product schema may be missing or incompatible: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        else:
            self._rollback_after_read(operation)
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def _rollback_after_read(self, operation: str) -> None:
        try:
            self.connection.rollback()
        except Exception as exc:
            raise ProductAdminReadQueryError(
                f"PostgreSQL admin read '{operation}' could not release its "
                f"read-only transaction: {type(exc).__name__}: {exc}"
            ) from exc


def _user_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(row.get("id") or "")
    return {
        "id": identifier,
        "userId": identifier,
        "phone": row.get("phone"),
        "status": row.get("status"),
        "storeCount": _int(row.get("store_count")),
        "orderCount": _int(row.get("order_count")),
        "paymentAmountCents": _int(row.get("payment_amount_cents")),
        "pointBalance": _int(row.get("point_balance")),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
        "lastLoginAt": _public_value(row.get("last_login_at")),
    }


def _store_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(row.get("id") or "")
    asset_count = row.get("asset_count")
    return {
        "id": identifier,
        "storeId": identifier,
        "name": row.get("name"),
        "status": row.get("status"),
        "createdByUserId": row.get("created_by_user_id"),
        "userCount": _int(row.get("user_count")),
        "menuUploadCount": _int(row.get("menu_upload_count")),
        "jobCount": _int(row.get("job_count")),
        "assetCount": None if asset_count is None else _int(asset_count),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
    }


def _payment_order_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(row.get("id") or "")
    return {
        "id": identifier,
        "orderId": identifier,
        "userId": row.get("owner_user_id"),
        "provider": row.get("provider"),
        "providerOrderId": row.get("provider_order_id"),
        "amountCents": _int(row.get("amount_cents")),
        "points": _int(row.get("points")),
        "status": row.get("status"),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
        "paidAt": _public_value(row.get("paid_at")),
        "closedAt": _public_value(row.get("closed_at")),
        "refundedAt": _public_value(row.get("refunded_at")),
    }


def _generation_task_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    requested = _int(row.get("requested_count"))
    completed = _int(row.get("completed_count"))
    export_count = row.get("export_count")
    return {
        "id": row.get("id"),
        "menuUploadId": row.get("menu_upload_id"),
        "storeName": row.get("store_name"),
        "originalFilename": row.get("original_filename"),
        "styleId": row.get("style_id"),
        "quality": row.get("quality"),
        "status": row.get("status"),
        "requestedCount": requested,
        "completedCount": completed,
        "failedCount": _int(row.get("failed_count")),
        "progress": _ratio(completed, requested),
        "imageCount": _int(row.get("image_count")),
        "exportCount": (
            None if export_count is None else _int(export_count)
        ),
        "pointDelta": _int(row.get("point_delta")),
        "errorMessage": row.get("error_message"),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
        "startedAt": _public_value(row.get("started_at")),
        "completedAt": _public_value(row.get("completed_at")),
    }


def _commission_settlement_payload(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "agentId": row.get("agent_id"),
        "settlementNo": row.get("settlement_no"),
        "periodStart": None,
        "periodEnd": None,
        "totalOrderAmount": _int(
            row.get("total_order_amount_cents")
        ),
        "totalCommissionAmount": _int(
            row.get("total_commission_amount_cents")
        ),
        "orderCount": _int(row.get("order_count")),
        "currency": row.get("currency"),
        "status": row.get("status"),
        "paidAt": _public_value(row.get("paid_at")),
        "failureReason": row.get("failure_reason"),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
    }


def _withdrawal_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_snapshot = _json_object(row.get("balance_snapshot"))
    snapshot = _camelize_mapping(raw_snapshot)
    balance = snapshot.get("after")
    if not isinstance(balance, Mapping):
        balance = snapshot
    identifier = str(row.get("id") or "")
    return {
        "id": identifier,
        "withdrawalId": identifier,
        "agentId": row.get("agent_id"),
        "amountCents": _int(row.get("amount_cents")),
        "currency": row.get("currency"),
        "status": row.get("status"),
        "balanceSnapshot": snapshot,
        "balanceAvailableCents": _int(balance.get("availableCents")),
        "balancePaidSettlementCents": _int(
            balance.get("earnedCents")
        ),
        "balanceLockedWithdrawalCents": _int(
            balance.get("reservedCents")
        ),
        "statusReason": row.get("status_reason"),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
        "approvedAt": _public_value(row.get("approved_at")),
        "rejectedAt": _public_value(row.get("rejected_at")),
        "paidAt": _public_value(row.get("paid_at")),
        "canceledAt": _public_value(row.get("canceled_at")),
    }


def _dashboard_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    requested = _int(row.get("job_requested"))
    completed = _int(row.get("job_completed"))
    order_counts = {
        status: _int(row.get(f"commission_order_{status}"))
        for status in COMMISSION_ORDER_STATUSES
    }
    settlement_counts = {
        status: _int(row.get(f"settlement_{status}"))
        for status in sorted(SETTLEMENT_STATUSES)
    }
    return {
        "users": {"total": _int(row.get("user_total"))},
        "stores": {"total": _int(row.get("store_total"))},
        "payments": {
            "total": _int(row.get("payment_total")),
            "pending": _int(row.get("payment_pending")),
            "paid": _int(row.get("payment_paid")),
            "partiallyRefunded": _int(
                row.get("payment_partially_refunded")
            ),
            "refunded": _int(row.get("payment_refunded")),
            "failed": _int(row.get("payment_failed")),
            "closed": _int(row.get("payment_closed")),
            "grossAmountCents": _int(
                row.get("payment_gross_amount_cents")
            ),
            "refundedAmountCents": _int(
                row.get("payment_refunded_amount_cents")
            ),
            "netAmountCents": _int(
                row.get("payment_net_amount_cents")
            ),
            "creditedPoints": _int(
                row.get("payment_credited_points")
            ),
            "refundedPoints": _int(
                row.get("payment_refunded_points")
            ),
        },
        "jobs": {
            "total": _int(row.get("job_total")),
            "queued": _int(row.get("job_queued")),
            "running": _int(row.get("job_running")),
            "succeeded": _int(row.get("job_succeeded")),
            "failed": _int(row.get("job_failed")),
            "canceled": _int(row.get("job_canceled")),
            "requested": requested,
            "completed": completed,
            "failedItems": _int(row.get("job_failed_items")),
            "successRate": _ratio(completed, requested),
        },
        "images": {
            "total": _int(row.get("image_total")),
            "generated": _int(row.get("image_generated")),
            "failed": _int(row.get("image_failed")),
            "rejected": _int(row.get("image_rejected")),
            "totalFileSize": None,
        },
        "points": {
            "entries": _int(row.get("point_entries")),
            "credited": _int(row.get("point_credited")),
            "debited": _int(row.get("point_debited")),
            "refunded": _int(row.get("point_refunded")),
            "net": _int(row.get("point_balance")),
            "latestBalance": _int(row.get("point_balance")),
            "totalBalance": _int(row.get("point_balance")),
        },
        "agents": {
            "total": _int(row.get("agent_total")),
            "active": _int(row.get("agent_active")),
            "suspended": _int(row.get("agent_suspended")),
            "closed": _int(row.get("agent_closed")),
            "inactive": 0,
            "pending": 0,
        },
        "commissions": {
            "pendingAmount": _int(
                row.get("commission_pending_amount")
            ),
            "eligibleAmount": _int(
                row.get("commission_eligible_amount")
            ),
            "settledAmount": _int(
                row.get("commission_settled_amount")
            ),
            "orderCount": _int(row.get("commission_order_total")),
            "orders": {
                "total": _int(row.get("commission_order_total")),
                **order_counts,
                "orderAmount": _int(
                    row.get("commission_net_order_amount")
                ),
                "grossOrderAmount": _int(
                    row.get("commission_gross_order_amount")
                ),
                "refundedOrderAmount": _int(
                    row.get("commission_refunded_order_amount")
                ),
                "commissionAmount": _int(
                    row.get("commission_net_amount")
                ),
                "grossCommissionAmount": _int(
                    row.get("commission_gross_amount")
                ),
                "reversedCommissionAmount": _int(
                    row.get("commission_reversed_amount")
                ),
            },
            "settlements": {
                "total": _int(row.get("settlement_total")),
                **settlement_counts,
                "commissionAmount": _int(
                    row.get("settlement_commission_amount")
                ),
                "paidCommissionAmount": _int(
                    row.get("settlement_paid_commission_amount")
                ),
            },
        },
        "withdrawals": {
            "total": _int(row.get("withdrawal_total")),
            "pending": _int(row.get("withdrawal_pending")),
            "approved": _int(row.get("withdrawal_approved")),
            "rejected": _int(row.get("withdrawal_rejected")),
            "paid": _int(row.get("withdrawal_paid")),
            "canceled": _int(row.get("withdrawal_canceled")),
            "requestedAmountCents": _int(
                row.get("withdrawal_requested_amount")
            ),
            "reservedAmountCents": _int(
                row.get("withdrawal_reserved_amount")
            ),
            "paidAmountCents": _int(
                row.get("withdrawal_paid_amount")
            ),
        },
        "invites": {
            "total": _int(row.get("invite_total")),
            "accepted": _int(row.get("invite_total")),
            "rewardGrants": _int(row.get("reward_grant_total")),
            "rewardPoints": _int(row.get("reward_points")),
            "debtOffsetPoints": _int(
                row.get("reward_debt_offset_points")
            ),
            "grantedRewardPoints": _int(
                row.get("reward_wallet_points")
            ),
        },
    }


def _recent_job_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    requested = _int(row.get("requested_count"))
    completed = _int(row.get("completed_count"))
    return {
        "id": row.get("id"),
        "menuUploadId": row.get("menu_upload_id"),
        "styleId": row.get("style_id"),
        "quality": row.get("quality"),
        "status": row.get("status"),
        "requestedCount": requested,
        "completedCount": completed,
        "failedCount": _int(row.get("failed_count")),
        "progress": _ratio(completed, requested),
        "imageCount": _int(row.get("image_count")),
        "generatedImageCount": _int(
            row.get("generated_image_count")
        ),
        "failedImageCount": _int(row.get("failed_image_count")),
        "rejectedImageCount": _int(
            row.get("rejected_image_count")
        ),
        "imageFileSize": None,
        "exportCount": None,
        "exportedImageCount": None,
        "exportFileSize": None,
        "pointDelta": _int(row.get("point_delta")),
        "pointsDebited": _int(row.get("points_debited")),
        "pointsCredited": _int(row.get("points_credited")),
        "errorMessage": row.get("error_message"),
        "createdAt": _public_value(row.get("created_at")),
        "updatedAt": _public_value(row.get("updated_at")),
        "startedAt": _public_value(row.get("started_at")),
        "completedAt": _public_value(row.get("completed_at")),
    }


def _commission_summary_payload(
    row: Mapping[str, Any],
    top_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    order_statuses = {
        status: _int(row.get(f"order_{status}"))
        for status in COMMISSION_ORDER_STATUSES
    }
    settlement_statuses = {
        status: _int(row.get(f"settlement_{status}"))
        for status in sorted(SETTLEMENT_STATUSES)
    }
    return {
        "orders": {
            "total": _int(row.get("order_total")),
            **order_statuses,
            "orderAmount": _int(row.get("net_order_amount")),
            "grossOrderAmount": _int(row.get("gross_order_amount")),
            "refundedOrderAmount": _int(
                row.get("refunded_order_amount")
            ),
            "commissionAmount": _int(
                row.get("net_commission_amount")
            ),
            "grossCommissionAmount": _int(
                row.get("gross_commission_amount")
            ),
            "reversedCommissionAmount": _int(
                row.get("reversed_commission_amount")
            ),
            "pendingCommissionAmount": _int(
                row.get("pending_commission_amount")
            ),
            "eligibleCommissionAmount": _int(
                row.get("eligible_commission_amount")
            ),
            "settledCommissionAmount": _int(
                row.get("settled_commission_amount")
            ),
        },
        "settlements": {
            "total": _int(row.get("settlement_total")),
            **settlement_statuses,
            "orderCount": _int(row.get("settlement_order_count")),
            "orderAmount": _int(row.get("settlement_order_amount")),
            "commissionAmount": _int(
                row.get("settlement_commission_amount")
            ),
            "paidCommissionAmount": _int(
                row.get("paid_commission_amount")
            ),
        },
        "agents": {
            "total": _int(row.get("agent_total")),
            "active": _int(row.get("agent_active")),
            "suspended": _int(row.get("agent_suspended")),
            "closed": _int(row.get("agent_closed")),
            "inactive": 0,
            "pending": 0,
        },
        "invites": {
            "total": _int(row.get("invite_total")),
            "accepted": _int(row.get("invite_total")),
            "rewardStatus": {
                "pending": 0,
                "granted": _int(row.get("reward_grant_total")),
                "failed": 0,
                "canceled": 0,
            },
            "rewardPoints": _int(row.get("reward_points")),
            "debtOffsetPoints": _int(row.get("debt_offset_points")),
            "grantedRewardPoints": _int(
                row.get("granted_reward_points")
            ),
        },
        "topAgents": [
            {
                "agentId": item.get("agent_id"),
                "orderCount": _int(item.get("order_count")),
                "orderAmount": _int(item.get("order_amount")),
                "commissionAmount": _int(
                    item.get("commission_amount")
                ),
                "settledCommissionAmount": _int(
                    item.get("settled_commission_amount")
                ),
            }
            for item in top_rows
        ],
    }


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
        "total": int(total),
        "limit": int(limit),
        "offset": int(offset),
        "sort": sort,
        "order": order.lower(),
    }


def _page_args(limit: Any, offset: Any) -> tuple[int, int]:
    return (
        _bounded_int(
            limit,
            "limit",
            minimum=0,
            maximum=MAX_PAGE_LIMIT,
            clamp=True,
        ),
        _bounded_int(
            offset,
            "offset",
            minimum=0,
            maximum=MAX_PAGE_OFFSET,
            clamp=True,
        ),
    )


def _bounded_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
    clamp: bool = False,
) -> int:
    if isinstance(value, bool):
        raise InvalidProductAdminReadInput(
            f"{field} must be an integer"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductAdminReadInput(
            f"{field} must be an integer"
        ) from exc
    if clamp:
        return max(minimum, min(parsed, maximum))
    if parsed < minimum or parsed > maximum:
        raise InvalidProductAdminReadInput(
            f"{field} must be between {minimum} and {maximum}"
        )
    return parsed


def _sort_args(
    sort: Any,
    order: Any,
    allowed: Mapping[str, str],
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
    previous_separator = False
    for char in text:
        if char in {"-", " ", "."}:
            if output and not previous_separator:
                output.append("_")
            previous_separator = True
            continue
        if char.isupper():
            if output and not previous_separator:
                output.append("_")
            output.append(char.lower())
        else:
            output.append(char.lower())
        previous_separator = char == "_"
    return "".join(output).strip("_")


def _optional_choice(
    value: Any,
    allowed: frozenset[str],
    field: str,
) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text not in allowed:
        raise InvalidProductAdminReadInput(
            f"{field} has an invalid value"
        )
    return text


def _optional_identifier(value: Any, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not IDENTIFIER_RE.fullmatch(text):
        raise InvalidProductAdminReadInput(
            f"{field} has an invalid identifier"
        )
    return text


def _optional_text(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > maximum or any(ord(char) < 32 for char in text):
        raise InvalidProductAdminReadInput(
            f"{field} has an invalid value"
        )
    return text


def _timestamp_value(value: Any, field: str) -> Any | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text or len(text) > 64 or any(ord(char) < 32 for char in text):
        raise InvalidProductAdminReadInput(
            f"{field} has an invalid timestamp"
        )
    return text


def _append_equal(
    clauses: list[str],
    params: list[Any],
    column: str,
    value: Any | None,
) -> None:
    if value is None:
        return
    clauses.append(f"{column} = %s")
    params.append(value)


def _append_timestamp(
    clauses: list[str],
    params: list[Any],
    column: str,
    operator: str,
    value: Any,
    field: str,
) -> None:
    clean_value = _timestamp_value(value, field)
    if clean_value is None:
        return
    if operator not in {">=", "<="}:
        raise InvalidProductAdminReadInput(
            "timestamp comparison operator is not allowed"
        )
    clauses.append(f"{column} {operator} %s::timestamptz")
    params.append(clean_value)


def _append_search(
    clauses: list[str],
    params: list[Any],
    search: Any,
    columns: Sequence[str],
) -> None:
    clean_search = _optional_text(search, "search", maximum=200)
    if clean_search is None:
        return
    pattern = "%" + _escape_like(clean_search) + "%"
    clauses.append(
        "("
        + " OR ".join(
            f"{column} ILIKE %s ESCAPE E'\\\\'"
            for column in columns
        )
        + ")"
    )
    params.extend(pattern for _ in columns)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where_sql(clauses: Sequence[str]) -> str:
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


def _required_count(cursor: CursorLike, operation: str) -> int:
    row = _required_row(cursor, f"{operation} count")
    if "item_count" not in row:
        raise ProductAdminReadQueryError(
            f"PostgreSQL admin read '{operation}' returned no item_count"
        )
    return _int(row["item_count"])


def _required_row(
    cursor: CursorLike,
    operation: str,
) -> dict[str, Any]:
    row = cursor.fetchone()
    if row is None:
        raise ProductAdminReadQueryError(
            f"PostgreSQL admin read '{operation}' returned no aggregate row"
        )
    return _row_dict(cursor, row)


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _row_dict(cursor: CursorLike, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description
    if description is None:
        raise ProductAdminReadQueryError(
            "PostgreSQL admin read returned tuple rows without a description"
        )
    names = [
        str(getattr(column, "name", column[0]))
        for column in description
    ]
    return dict(zip(names, row))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProductAdminReadQueryError(
            "PostgreSQL admin read returned invalid JSONB data"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProductAdminReadQueryError(
            "PostgreSQL admin read returned non-object JSONB data"
        )
    return parsed


def _camelize_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            _camel_key(str(key)): _camelize_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_camelize_mapping(item) for item in value]
    return _public_value(value)


def _camel_key(value: str) -> str:
    return re.sub(
        r"_([a-zA-Z0-9])",
        lambda match: match.group(1).upper(),
        value,
    )


def _public_value(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        return int(integral) if value == integral else float(value)
    return value


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ProductAdminReadQueryError(
            "PostgreSQL admin read returned a non-integer numeric value"
        ) from exc


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)
