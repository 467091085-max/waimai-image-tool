from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_admin_read_store as admin_reads


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = tuple(
    ROOT / "migrations" / f"{index:03d}_{name}"
    for index, name in (
        (1, "product_generation_postgres.sql"),
        (2, "product_wallet_postgres.sql"),
        (3, "menu_uploads_postgres.sql"),
        (4, "product_payments_postgres.sql"),
        (5, "product_auth_postgres.sql"),
        (6, "product_growth_outbox_postgres.sql"),
        (7, "product_growth_business_postgres.sql"),
        (8, "product_finance_postgres.sql"),
    )
)
SQL_MARKER = re.compile(
    r"/\*\s*product_admin_read_store:([a-z0-9_]+)\s*\*/",
    re.IGNORECASE,
)


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.description = None
        self.rows: list[Any] = []
        self.closed = False

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        marker = SQL_MARKER.search(operation)
        if marker is None:
            raise AssertionError(f"SQL operation has no marker: {operation}")
        name = marker.group(1).lower()
        if "?" in operation:
            raise AssertionError(f"{name} uses a SQLite placeholder")
        expected = operation.count("%s")
        if expected != len(parameters):
            raise AssertionError(
                f"{name} expected {expected} parameters, "
                f"received {len(parameters)}"
            )
        self.connection.calls.append(
            (name, operation, tuple(parameters))
        )
        if name == "begin_read":
            self.rows = []
            return
        scripted = self.connection.responses.get(name)
        if not scripted:
            raise AssertionError(
                f"unexpected or exhausted SQL operation: {name}"
            )
        response = scripted.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.rows = list(response)

    def fetchone(self) -> Any | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self) -> list[Any]:
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
        rollback_error: BaseException | None = None,
        **responses: list[Any],
    ) -> None:
        self.responses = {
            name: list(items) for name, items in responses.items()
        }
        self.rollback_error = rollback_error
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
        if self.rollback_error is not None:
            raise self.rollback_error


class AutocommitConnection(ScriptedConnection):
    autocommit = True


def user_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "user-1",
        "phone": "+8613800000001",
        "status": "active",
        "store_count": 1,
        "order_count": 2,
        "payment_amount_cents": 1500,
        "point_balance": 88,
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T01:00:00+00:00",
        "last_login_at": None,
    }
    row.update(overrides)
    return row


def store_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "store-1",
        "name": "Test Store",
        "status": "active",
        "created_by_user_id": "user-1",
        "user_count": 1,
        "menu_upload_count": 2,
        "job_count": 3,
        "asset_count": None,
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T01:00:00+00:00",
    }
    row.update(overrides)
    return row


def order_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "payment-1",
        "owner_user_id": "user-1",
        "provider": "alipay",
        "provider_order_id": "provider-1",
        "amount_cents": 1000,
        "points": 100,
        "status": "paid",
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T01:00:00+00:00",
        "paid_at": "2026-07-30T01:00:00+00:00",
        "closed_at": None,
        "refunded_at": None,
    }
    row.update(overrides)
    return row


def job_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "job-1",
        "owner_user_id": "user-1",
        "menu_upload_id": "menu-1",
        "store_name": "Test Store",
        "original_filename": "menu.xlsx",
        "style_id": "style-1",
        "quality": "standard",
        "status": "failed",
        "requested_count": 3,
        "completed_count": 2,
        "failed_count": 1,
        "image_count": 2,
        "export_count": None,
        "point_delta": -20,
        "error_message": "one item failed",
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T01:00:00+00:00",
        "started_at": "2026-07-30T00:05:00+00:00",
        "completed_at": "2026-07-30T01:00:00+00:00",
    }
    row.update(overrides)
    return row


def settlement_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "settlement-1",
        "agent_id": "agent-1",
        "settlement_no": "SET-1",
        "total_order_amount_cents": 10_000,
        "total_commission_amount_cents": 2_000,
        "order_count": 1,
        "currency": "CNY",
        "status": "paid",
        "paid_at": "2026-07-30T01:00:00+00:00",
        "failure_reason": "",
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T01:00:00+00:00",
    }
    row.update(overrides)
    return row


def withdrawal_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "withdrawal-1",
        "agent_id": "agent-1",
        "amount_cents": 10_000,
        "currency": "CNY",
        "status": "pending",
        "balance_snapshot": {
            "before": {
                "available_cents": 30_000,
                "earned_cents": 40_000,
                "reserved_cents": 0,
            },
            "after": {
                "available_cents": 20_000,
                "earned_cents": 40_000,
                "reserved_cents": 10_000,
            },
        },
        "status_reason": "",
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T01:00:00+00:00",
        "approved_at": None,
        "rejected_at": None,
        "paid_at": None,
        "canceled_at": None,
    }
    row.update(overrides)
    return row


def dashboard_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "user_total": 3,
        "store_total": 2,
        "payment_total": 4,
        "payment_pending": 1,
        "payment_paid": 1,
        "payment_partially_refunded": 1,
        "payment_refunded": 1,
        "payment_failed": 0,
        "payment_closed": 0,
        "payment_gross_amount_cents": 6000,
        "payment_refunded_amount_cents": 2500,
        "payment_net_amount_cents": 3500,
        "payment_credited_points": 600,
        "payment_refunded_points": 250,
        "job_total": 2,
        "job_queued": 0,
        "job_running": 0,
        "job_succeeded": 1,
        "job_failed": 1,
        "job_canceled": 0,
        "job_requested": 5,
        "job_completed": 4,
        "job_failed_items": 1,
        "image_total": 5,
        "image_generated": 4,
        "image_failed": 1,
        "image_rejected": 0,
        "point_entries": 8,
        "point_credited": 700,
        "point_debited": 200,
        "point_refunded": 100,
        "point_balance": 500,
        "agent_total": 2,
        "agent_active": 1,
        "agent_suspended": 1,
        "agent_closed": 0,
        "commission_order_total": 3,
        "commission_order_pending": 1,
        "commission_order_eligible": 1,
        "commission_order_claimed": 0,
        "commission_order_settled": 1,
        "commission_order_canceled": 0,
        "commission_order_refunded": 0,
        "commission_gross_order_amount": 30_000,
        "commission_refunded_order_amount": 5_000,
        "commission_net_order_amount": 25_000,
        "commission_gross_amount": 6_000,
        "commission_reversed_amount": 1_000,
        "commission_net_amount": 5_000,
        "commission_pending_amount": 1_000,
        "commission_eligible_amount": 1_500,
        "commission_settled_amount": 2_500,
        "settlement_total": 1,
        "settlement_pending": 0,
        "settlement_processing": 0,
        "settlement_paid": 1,
        "settlement_failed": 0,
        "settlement_canceled": 0,
        "settlement_commission_amount": 2_500,
        "settlement_paid_commission_amount": 2_500,
        "withdrawal_total": 2,
        "withdrawal_pending": 1,
        "withdrawal_approved": 0,
        "withdrawal_rejected": 0,
        "withdrawal_paid": 1,
        "withdrawal_canceled": 0,
        "withdrawal_requested_amount": 1_200,
        "withdrawal_reserved_amount": 200,
        "withdrawal_paid_amount": 1_000,
        "invite_total": 1,
        "reward_grant_total": 2,
        "reward_points": 120,
        "reward_debt_offset_points": 20,
        "reward_wallet_points": 100,
    }
    row.update(overrides)
    return row


def commission_summary_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "order_total": 3,
        "order_pending": 1,
        "order_eligible": 1,
        "order_claimed": 0,
        "order_settled": 1,
        "order_canceled": 0,
        "order_refunded": 0,
        "gross_order_amount": 30_000,
        "refunded_order_amount": 5_000,
        "net_order_amount": 25_000,
        "gross_commission_amount": 6_000,
        "reversed_commission_amount": 1_000,
        "net_commission_amount": 5_000,
        "pending_commission_amount": 1_000,
        "eligible_commission_amount": 1_500,
        "settled_commission_amount": 2_500,
        "settlement_total": 1,
        "settlement_pending": 0,
        "settlement_processing": 0,
        "settlement_paid": 1,
        "settlement_failed": 0,
        "settlement_canceled": 0,
        "settlement_order_count": 1,
        "settlement_order_amount": 10_000,
        "settlement_commission_amount": 2_500,
        "paid_commission_amount": 2_500,
        "agent_total": 1,
        "agent_active": 1,
        "agent_suspended": 0,
        "agent_closed": 0,
        "invite_total": 1,
        "reward_grant_total": 2,
        "reward_points": 120,
        "debt_offset_points": 20,
        "granted_reward_points": 100,
    }
    row.update(overrides)
    return row


def test_store_requires_non_autocommit_connection() -> None:
    with pytest.raises(
        admin_reads.InvalidProductAdminReadInput,
        match="autocommit-disabled",
    ):
        admin_reads.ProductAdminReadStore(AutocommitConnection())


def test_user_query_is_read_only_paginated_and_camel_case() -> None:
    connection = ScriptedConnection(
        list_users_count=[([{"item_count": 1}])],
        list_users_rows=[([user_row()])],
    )
    store = admin_reads.ProductAdminReadStore(connection)

    result = store.list_users(
        status="active",
        search="0001",
        sort="paymentAmountCents",
        order="asc",
        limit=999,
        offset=-9,
    )

    assert result == {
        "items": [
            {
                "id": "user-1",
                "userId": "user-1",
                "phone": "+8613800000001",
                "status": "active",
                "storeCount": 1,
                "orderCount": 2,
                "paymentAmountCents": 1500,
                "pointBalance": 88,
                "createdAt": "2026-07-30T00:00:00+00:00",
                "updatedAt": "2026-07-30T01:00:00+00:00",
                "lastLoginAt": None,
            }
        ],
        "total": 1,
        "limit": admin_reads.MAX_PAGE_LIMIT,
        "offset": 0,
        "sort": "payment_amount_cents",
        "order": "asc",
    }
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.calls[0][0] == "begin_read"
    rows_call = next(
        call for call in connection.calls if call[0] == "list_users_rows"
    )
    assert "ORDER BY payment_amount_cents ASC" in rows_call[1]
    assert rows_call[2][-2:] == (admin_reads.MAX_PAGE_LIMIT, 0)


def test_all_list_payloads_match_admin_data_camel_case_shapes() -> None:
    connection = ScriptedConnection(
        list_stores_count=[([{"item_count": 1}])],
        list_stores_rows=[([store_row()])],
        list_orders_count=[([{"item_count": 1}])],
        list_orders_rows=[([order_row()])],
        list_generation_tasks_count=[([{"item_count": 1}])],
        list_generation_tasks_rows=[([job_row()])],
        list_commission_settlements_count=[([{"item_count": 1}])],
        list_commission_settlements_rows=[([settlement_row()])],
        list_withdrawals_count=[([{"item_count": 1}])],
        list_withdrawals_rows=[([withdrawal_row()])],
    )
    store = admin_reads.ProductAdminReadStore(connection)

    store_item = store.list_stores()["items"][0]
    assert store_item["storeId"] == "store-1"
    assert store_item["menuUploadCount"] == 2
    assert store_item["assetCount"] is None

    order_item = store.list_payment_orders()["items"][0]
    assert order_item["orderId"] == "payment-1"
    assert order_item["amountCents"] == 1000

    job_item = store.list_generation_jobs()["items"][0]
    assert job_item["menuUploadId"] == "menu-1"
    assert job_item["progress"] == 0.6667
    assert job_item["exportCount"] is None

    settlement_item = store.list_commission_settlements()["items"][0]
    assert settlement_item["settlementNo"] == "SET-1"
    assert settlement_item["totalCommissionAmount"] == 2000
    assert settlement_item["periodStart"] is None

    withdrawal_item = store.list_withdrawals()["items"][0]
    assert withdrawal_item["withdrawalId"] == "withdrawal-1"
    assert withdrawal_item["balanceAvailableCents"] == 20_000
    assert withdrawal_item["balancePaidSettlementCents"] == 40_000
    assert withdrawal_item["balanceLockedWithdrawalCents"] == 10_000
    assert (
        withdrawal_item["balanceSnapshot"]["after"]["availableCents"]
        == 20_000
    )

    assert connection.commits == 0
    assert connection.rollbacks == 5


def test_filter_values_are_parameterized_and_sort_injection_falls_back() -> None:
    attack = "x%' OR TRUE; DROP TABLE product_users; --"
    connection = ScriptedConnection(
        list_orders_count=[([{"item_count": 0}])],
        list_orders_rows=[([])],
    )
    store = admin_reads.ProductAdminReadStore(connection)

    result = store.list_orders(
        search=attack,
        sort="created_at; DROP TABLE product_users",
        order="sideways",
    )

    assert result["sort"] == "created_at"
    assert result["order"] == "desc"
    for name, sql, params in connection.calls:
        if name == "begin_read":
            continue
        assert attack not in sql
        assert "DROP TABLE" not in sql
        assert all("%s" not in str(value) for value in params)
    count_call = next(
        call for call in connection.calls if call[0] == "list_orders_count"
    )
    assert any("\\%" in str(value) for value in count_call[2])


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("list_users", {"status": "root"}),
        ("list_stores", {"created_by_user_id": "contains whitespace"}),
        ("list_orders", {"provider": "cash"}),
        ("list_generation_tasks", {"status": "done"}),
        ("list_commission_settlements", {"status": "released"}),
        ("list_withdrawals", {"status": "processing"}),
    ],
)
def test_invalid_filter_values_fail_before_sql(
    method_name: str,
    kwargs: dict[str, Any],
) -> None:
    connection = ScriptedConnection()
    method = getattr(
        admin_reads.ProductAdminReadStore(connection),
        method_name,
    )

    with pytest.raises(admin_reads.InvalidProductAdminReadInput):
        method(**kwargs)

    assert connection.calls == []
    assert connection.commits == 0
    assert connection.rollbacks == 0


def test_dashboard_finance_totals_are_exposed_without_risk_placeholders() -> None:
    connection = ScriptedConnection(
        dashboard_summary=[([dashboard_row()])],
    )
    summary = admin_reads.ProductAdminReadStore(
        connection
    ).dashboard_summary()

    assert summary["payments"]["grossAmountCents"] == 6000
    assert summary["payments"]["refundedAmountCents"] == 2500
    assert summary["payments"]["netAmountCents"] == 3500
    assert summary["jobs"]["successRate"] == 0.8
    assert summary["commissions"]["orders"]["grossCommissionAmount"] == 6000
    assert (
        summary["commissions"]["orders"]["reversedCommissionAmount"]
        == 1000
    )
    assert summary["commissions"]["orders"]["commissionAmount"] == 5000
    assert summary["withdrawals"]["reservedAmountCents"] == 200
    assert summary["withdrawals"]["paidAmountCents"] == 1000
    assert summary["invites"]["debtOffsetPoints"] == 20
    assert "risk" not in summary
    assert "assetAccess" not in summary
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_recent_jobs_and_commission_summary_use_bounded_read_queries() -> None:
    recent = job_row(
        generated_image_count=2,
        failed_image_count=1,
        rejected_image_count=0,
        points_debited=20,
        points_credited=0,
    )
    top_agent = {
        "agent_id": "agent-1",
        "order_count": 3,
        "order_amount": 25_000,
        "commission_amount": 5_000,
        "settled_commission_amount": 2_500,
    }
    connection = ScriptedConnection(
        recent_jobs=[([recent])],
        commission_summary=[([commission_summary_row()])],
        commission_top_agents=[([top_agent])],
    )
    store = admin_reads.ProductAdminReadStore(connection)

    jobs = store.recent_jobs(limit=100)
    commissions = store.commission_summary()

    assert jobs[0]["generatedImageCount"] == 2
    assert jobs[0]["pointsDebited"] == 20
    assert jobs[0]["imageFileSize"] is None
    assert commissions["orders"]["commissionAmount"] == 5000
    assert commissions["orders"]["reversedCommissionAmount"] == 1000
    assert commissions["settlements"]["paidCommissionAmount"] == 2500
    assert commissions["topAgents"][0] == {
        "agentId": "agent-1",
        "orderCount": 3,
        "orderAmount": 25_000,
        "commissionAmount": 5_000,
        "settledCommissionAmount": 2_500,
    }
    assert connection.commits == 0
    assert connection.rollbacks == 2

    with pytest.raises(admin_reads.InvalidProductAdminReadInput):
        store.recent_jobs(limit=101)


def test_sql_error_fails_closed_with_operation_and_rolls_back() -> None:
    connection = ScriptedConnection(
        list_users_count=[RuntimeError("relation does not exist")],
    )
    store = admin_reads.ProductAdminReadStore(connection)

    with pytest.raises(
        admin_reads.ProductAdminReadQueryError,
        match="list_users.*schema may be missing or incompatible",
    ):
        store.list_users()

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.cursors[0].closed is True


def test_empty_aggregate_is_an_error_not_a_fake_empty_result() -> None:
    connection = ScriptedConnection(
        list_users_count=[([])],
    )

    with pytest.raises(
        admin_reads.ProductAdminReadStoreError,
        match="returned no aggregate row",
    ):
        admin_reads.ProductAdminReadStore(connection).list_users()

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_all_executed_contract_sql_is_read_only() -> None:
    connection = ScriptedConnection(
        dashboard_summary=[([dashboard_row()])],
        recent_jobs=[([])],
        commission_summary=[([commission_summary_row()])],
        commission_top_agents=[([])],
    )
    store = admin_reads.ProductAdminReadStore(connection)
    store.dashboard_summary()
    store.recent_jobs(limit=1)
    store.commission_summary()

    for name, sql, _params in connection.calls:
        normalized = SQL_MARKER.sub("", sql).strip().upper()
        assert re.search(
            r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|ALTER|DROP|CREATE)\b",
            normalized,
        ) is None, name
        assert normalized.startswith(("SET TRANSACTION READ ONLY", "SELECT"))
    assert connection.commits == 0
    assert not hasattr(store, "list_risk_events")
    assert not hasattr(store, "list_asset_access_logs")


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_001_to_008_admin_read_contract() -> None:
    dsn = str(os.environ["TEST_POSTGRES_DSN"]).strip()
    if "test" not in dsn.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")

    psycopg = pytest.importorskip("psycopg")
    sql_module = pytest.importorskip("psycopg.sql")
    schema = f"test_product_admin_read_{uuid4().hex}"
    digest_a = "a" * 64
    digest_b = "b" * 64
    digest_c = "c" * 64
    digest_d = "d" * 64
    agent_id = "growth_agent_" + ("a" * 40)
    invite_code_id = "growth_invite_code_" + ("b" * 40)
    invite_relation_id = "growth_invite_" + ("c" * 40)

    admin_connection = psycopg.connect(dsn, autocommit=True)
    with admin_connection.cursor() as cursor:
        cursor.execute(
            sql_module.SQL("CREATE SCHEMA {}").format(
                sql_module.Identifier(schema)
            )
        )

    connection = psycopg.connect(dsn, autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql_module.SQL("SET search_path TO {}").format(
                    sql_module.Identifier(schema)
                )
            )
        connection.commit()

        with connection.cursor() as cursor:
            for migration in MIGRATIONS:
                cursor.execute(migration.read_text(encoding="utf-8"))

        _seed_real_postgres(
            connection,
            agent_id=agent_id,
            invite_code_id=invite_code_id,
            invite_relation_id=invite_relation_id,
            digests=(digest_a, digest_b, digest_c, digest_d),
        )
        store = admin_reads.ProductAdminReadStore(connection)

        users = store.list_users(
            status="active",
            sort="paymentAmountCents",
            order="desc",
        )
        assert users["total"] == 2
        assert users["items"][0]["userId"] == "user-2"
        assert users["items"][0]["paymentAmountCents"] == 1500
        assert users["items"][1]["paymentAmountCents"] == 1000

        stores = store.list_stores()["items"]
        assert stores[0]["userCount"] == 1
        assert stores[0]["menuUploadCount"] == 0
        assert stores[0]["jobCount"] == 0
        assert stores[0]["assetCount"] is None

        orders = store.list_orders(
            status="partially_refunded"
        )["items"]
        assert orders[0]["orderId"] == "payment-2"
        assert orders[0]["amountCents"] == 2000

        jobs = store.list_generation_tasks()["items"]
        assert jobs[0]["styleId"] == "style-1"
        assert jobs[0]["quality"] == "standard"
        assert jobs[0]["imageCount"] == 1
        assert jobs[0]["pointDelta"] == -20

        settlements = store.list_commission_settlements()["items"]
        assert settlements[0]["totalCommissionAmount"] == 1500
        assert settlements[0]["status"] == "paid"

        withdrawals = store.list_withdrawals(
            sort="amountCents",
            order="desc",
        )["items"]
        assert withdrawals[0]["status"] == "paid"
        assert withdrawals[1]["balanceAvailableCents"] == 2700

        dashboard = store.dashboard_summary()
        assert dashboard["payments"]["grossAmountCents"] == 3000
        assert dashboard["payments"]["refundedAmountCents"] == 500
        assert dashboard["payments"]["netAmountCents"] == 2500
        assert dashboard["jobs"]["requested"] == 2
        assert dashboard["images"]["generated"] == 1
        assert dashboard["commissions"]["eligibleAmount"] == 1500
        assert dashboard["commissions"]["settledAmount"] == 1500
        assert dashboard["withdrawals"]["reservedAmountCents"] == 300
        assert dashboard["withdrawals"]["paidAmountCents"] == 500
        assert dashboard["invites"]["accepted"] == 1

        recent = store.recent_jobs(limit=1)
        assert recent[0]["id"] == "job-1"
        assert recent[0]["pointsDebited"] == 20

        commissions = store.commission_summary()
        assert commissions["orders"]["grossCommissionAmount"] == 3500
        assert commissions["orders"]["reversedCommissionAmount"] == 500
        assert commissions["orders"]["commissionAmount"] == 3000
        assert commissions["settlements"]["paidCommissionAmount"] == 1500
        assert commissions["topAgents"][0]["agentId"] == agent_id
        assert commissions["topAgents"][0]["commissionAmount"] == 3000
    finally:
        connection.close()
        try:
            with admin_connection.cursor() as cursor:
                cursor.execute(
                    sql_module.SQL("DROP SCHEMA {} CASCADE").format(
                        sql_module.Identifier(schema)
                    )
                )
        finally:
            admin_connection.close()


def _seed_real_postgres(
    connection: Any,
    *,
    agent_id: str,
    invite_code_id: str,
    invite_relation_id: str,
    digests: tuple[str, str, str, str],
) -> None:
    digest_a, digest_b, digest_c, digest_d = digests
    catalog_1 = {
        "packageId": "starter-100",
        "catalogVersion": "test-v1",
        "currency": "CNY",
        "amountCents": 1000,
        "points": 100,
        "snapshotDigest": digest_a,
    }
    catalog_2 = {
        "packageId": "starter-200",
        "catalogVersion": "test-v1",
        "currency": "CNY",
        "amountCents": 2000,
        "points": 200,
        "snapshotDigest": digest_b,
    }
    request_payload = {
        "jobType": "menu_batch_generation",
        "selectedBackground": {"styleId": "style-1"},
        "quality": {"id": "standard"},
    }
    balance_snapshot = {
        "before": {
            "available_cents": 3000,
            "earned_cents": 3500,
            "reserved_cents": 0,
        },
        "after": {
            "available_cents": 2700,
            "earned_cents": 3500,
            "reserved_cents": 300,
        },
    }

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO product_users (
                id, phone, status, last_login_at
            ) VALUES
                ('user-1', '+8613800000001', 'active', CURRENT_TIMESTAMP),
                ('user-2', '+8613800000002', 'active', CURRENT_TIMESTAMP)
            """
        )
        cursor.execute(
            """
            INSERT INTO product_stores (
                id, name, status, created_by_user_id
            ) VALUES ('store-1', 'Test Store', 'active', 'user-1')
            """
        )
        cursor.execute(
            """
            INSERT INTO product_user_stores (
                user_id, store_id, role
            ) VALUES ('user-1', 'store-1', 'owner')
            """
        )
        cursor.execute(
            """
            INSERT INTO product_point_accounts (
                owner_user_id, balance_points,
                lifetime_credited_points, lifetime_debited_points,
                lifetime_refunded_points
            ) VALUES
                ('user-1', 80, 100, 20, 0),
                ('user-2', 150, 200, 50, 0)
            """
        )
        cursor.execute(
            """
            INSERT INTO product_generation_jobs (
                id, owner_user_id, idempotency_key,
                request_sha256, request_payload,
                menu_upload_id, menu_object_ref, menu_object_sha256,
                selected_background_ref, selected_background_sha256,
                status, fence, requested_count, completed_count,
                failed_count, debit_order_id, debit_points,
                refund_order_id, error_message, started_at,
                completed_at
            ) VALUES (
                'job-1', 'user-1', 'job-1',
                %s, %s::jsonb,
                'menu-1', 'menus/menu-1.xlsx', %s,
                'backgrounds/style-1.png', %s,
                'failed', 1, 2, 1,
                1, 'debit-job-1', 20,
                'refund-job-1', 'one item failed',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (
                digest_d,
                json.dumps(request_payload, sort_keys=True),
                digest_c,
                digest_b,
            ),
        )
        cursor.execute(
            """
            INSERT INTO product_generation_results (
                job_id, job_fence, menu_row, platform, variant,
                status, object_ref, object_sha256
            ) VALUES (
                'job-1', 1, 1, 'meituan', 'default',
                'generated', 'generated/job-1.png', %s
            )
            """,
            (digest_a,),
        )
        cursor.execute(
            """
            INSERT INTO product_point_orders (
                id, owner_user_id, order_kind, points,
                request_sha256, metadata
            ) VALUES
                ('credit-payment-1', 'user-1', 'credit', 100,
                    %s, '{}'::jsonb),
                ('credit-payment-2', 'user-2', 'credit', 200,
                    %s, '{}'::jsonb)
            """,
            (digest_a, digest_b),
        )
        cursor.execute(
            """
            INSERT INTO product_point_orders (
                id, owner_user_id, order_kind, points, job_id,
                request_sha256, metadata
            ) VALUES (
                'debit-job-1', 'user-1', 'debit', 20, 'job-1',
                %s, '{}'::jsonb
            )
            """,
            (digest_c,),
        )
        cursor.execute(
            """
            INSERT INTO product_point_ledger (
                owner_user_id, order_id, order_kind, points,
                delta_points, balance_after_points,
                request_sha256, metadata
            ) VALUES (
                'user-1', 'debit-job-1', 'debit', 20,
                -20, 80, %s, '{}'::jsonb
            )
            """,
            (digest_c,),
        )
        cursor.execute(
            """
            INSERT INTO product_payment_orders (
                id, owner_user_id, provider, provider_order_id,
                idempotency_key, package_id, catalog_version,
                currency, amount_cents, points, catalog_snapshot,
                catalog_snapshot_sha256, status, credited_points,
                credit_point_order_id, paid_at
            ) VALUES (
                'payment-1', 'user-1', 'alipay', 'provider-1',
                'payment-1', 'starter-100', 'test-v1',
                'CNY', 1000, 100, %s::jsonb,
                %s, 'paid', 100,
                'credit-payment-1', CURRENT_TIMESTAMP
            )
            """,
            (json.dumps(catalog_1, sort_keys=True), digest_a),
        )
        cursor.execute(
            """
            INSERT INTO product_payment_orders (
                id, owner_user_id, provider, provider_order_id,
                idempotency_key, package_id, catalog_version,
                currency, amount_cents, points, catalog_snapshot,
                catalog_snapshot_sha256, status, credited_points,
                refunded_amount_cents, refunded_points,
                credit_point_order_id, paid_at, refunded_at
            ) VALUES (
                'payment-2', 'user-2', 'wechat', 'provider-2',
                'payment-2', 'starter-200', 'test-v1',
                'CNY', 2000, 200, %s::jsonb,
                %s, 'partially_refunded', 200,
                500, 50,
                'credit-payment-2', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (json.dumps(catalog_2, sort_keys=True), digest_b),
        )
        cursor.execute(
            """
            INSERT INTO product_growth_agents (
                id, tenant_id, owner_user_id, idempotency_key,
                agent_code, status, rule_version,
                first_order_commission_bps,
                repeat_order_commission_bps, request_sha256
            ) VALUES (
                %s, 'waimai', 'user-1', 'agent-create-1',
                'AGENT001', 'active', 'growth-direct-v2-2026-07-30',
                2000, 1000, %s
            )
            """,
            (agent_id, digest_a),
        )
        cursor.execute(
            """
            INSERT INTO product_agent_finance_accounts (
                agent_id, earned_cents, reserved_cents,
                withdrawn_cents
            ) VALUES (%s, 3500, 300, 500)
            """,
            (agent_id,),
        )
        cursor.execute(
            """
            INSERT INTO product_commission_settlements (
                id, agent_id, idempotency_key, request_sha256,
                settlement_no, total_order_amount_cents,
                total_commission_amount_cents, order_count,
                currency, status, paid_at
            ) VALUES (
                'settlement-1', %s, 'settlement-1', %s,
                'SET-1', 8000, 1500, 1,
                'CNY', 'paid', CURRENT_TIMESTAMP
            )
            """,
            (agent_id, digest_b),
        )
        cursor.execute(
            """
            INSERT INTO product_commission_orders (
                id, source_order_id, agent_id, customer_user_id,
                order_amount_cents, commission_amount_cents,
                refunded_order_amount_cents,
                reversed_commission_cents, refund_status,
                commission_rate_bps, currency, status,
                content_sha256, eligible_at, last_refunded_at
            ) VALUES (
                'commission-eligible', 'payment-1', %s, 'user-1',
                10000, 2000,
                2500, 500, 'partial',
                2000, 'CNY', 'eligible',
                %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (agent_id, digest_c),
        )
        cursor.execute(
            """
            INSERT INTO product_commission_orders (
                id, source_order_id, agent_id, customer_user_id,
                order_amount_cents, commission_amount_cents,
                commission_rate_bps, currency, status,
                settlement_id, content_sha256,
                eligible_at, claimed_at, settled_at
            ) VALUES (
                'commission-settled', 'payment-2', %s, 'user-2',
                8000, 1500,
                2000, 'CNY', 'settled',
                'settlement-1', %s,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (agent_id, digest_d),
        )
        cursor.execute(
            """
            INSERT INTO product_agent_withdrawals (
                id, agent_id, idempotency_key, request_sha256,
                amount_cents, currency, status,
                account_snapshot, balance_snapshot,
                approved_at, paid_at
            ) VALUES (
                'withdrawal-paid', %s, 'withdrawal-paid', %s,
                500, 'CNY', 'paid',
                '{"account":"test"}'::jsonb, %s::jsonb,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (agent_id, digest_a, json.dumps(balance_snapshot)),
        )
        cursor.execute(
            """
            INSERT INTO product_agent_withdrawals (
                id, agent_id, idempotency_key, request_sha256,
                amount_cents, currency, status,
                account_snapshot, balance_snapshot
            ) VALUES (
                'withdrawal-pending', %s, 'withdrawal-pending', %s,
                300, 'CNY', 'pending',
                '{"account":"test"}'::jsonb, %s::jsonb
            )
            """,
            (agent_id, digest_b, json.dumps(balance_snapshot)),
        )
        cursor.execute(
            """
            INSERT INTO product_growth_invite_codes (
                id, tenant_id, owner_user_id, idempotency_key,
                code_sha256, rule_version, request_sha256
            ) VALUES (
                %s, 'waimai', 'user-1', 'invite-code-1',
                %s, 'growth-direct-v2-2026-07-30', %s
            )
            """,
            (invite_code_id, digest_c, digest_d),
        )
        cursor.execute(
            """
            INSERT INTO product_growth_invite_relations (
                id, tenant_id, owner_user_id, idempotency_key,
                inviter_user_id, invitee_user_id, invite_code_id,
                invite_code_sha256, rule_version,
                registration_inviter_points,
                registration_invitee_points,
                first_recharge_rebate_percent,
                cash_points_per_yuan, cents_per_yuan,
                request_sha256
            ) VALUES (
                %s, 'waimai', 'user-2', 'invite-relation-1',
                'user-1', 'user-2', %s,
                %s, 'growth-direct-v2-2026-07-30',
                100, 20, 10,
                10, 100, %s
            )
            """,
            (
                invite_relation_id,
                invite_code_id,
                digest_c,
                digest_a,
            ),
        )
    connection.commit()
