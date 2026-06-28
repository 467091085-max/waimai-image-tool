import sqlite3

import auth_service
import payment_service
from admin_data import (
    asset_access_summary,
    commission_summary,
    dashboard_summary,
    list_asset_access_logs,
    list_commission_settlements,
    list_generation_tasks,
    list_orders,
    list_risk_events,
    list_stores,
    list_users,
    list_withdrawals,
    recent_jobs,
    risk_summary,
)
from storage_db import SCHEMA_SQL


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    return conn


def make_product_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    auth_service.init_auth_schema(conn)
    payment_service.init_payment_schema(conn)
    return conn


def assert_page_contract(page: dict, *, total: int, limit: int, offset: int) -> None:
    assert set(page) >= {"items", "total", "limit", "offset", "sort", "order"}
    assert isinstance(page["items"], list)
    assert page["total"] == total
    assert page["limit"] == limit
    assert page["offset"] == offset
    assert page["order"] in {"asc", "desc"}


def test_empty_database_returns_stable_structures() -> None:
    raw_conn = sqlite3.connect(":memory:")

    assert recent_jobs(raw_conn) == []
    assert list_users(raw_conn)["items"] == []
    assert list_stores(raw_conn)["total"] == 0
    assert list_orders(raw_conn, limit=0)["limit"] == 0
    assert list_generation_tasks(raw_conn)["items"] == []
    assert list_asset_access_logs(raw_conn)["items"] == []
    assert list_risk_events(raw_conn)["items"] == []
    assert list_commission_settlements(raw_conn)["items"] == []
    assert list_withdrawals(raw_conn)["items"] == []
    raw_dashboard = dashboard_summary(raw_conn)
    assert raw_dashboard["jobs"]["total"] == 0
    assert raw_dashboard["commissions"]["pendingAmount"] == 0
    assert raw_dashboard["commissions"]["eligibleAmount"] == 0
    assert raw_dashboard["commissions"]["settledAmount"] == 0
    assert raw_dashboard["commissions"]["orderCount"] == 0
    assert raw_dashboard["risk"]["highestLevel"] == "info"
    assert raw_dashboard["assetAccess"]["topDenyReason"] == ""
    assert commission_summary(raw_conn)["orders"]["total"] == 0
    assert risk_summary(raw_conn)["byLevel"]["critical"] == 0
    assert risk_summary(raw_conn)["highestLevel"] == "info"
    assert asset_access_summary(raw_conn)["denied"] == 0
    assert asset_access_summary(raw_conn)["topDenyReason"] == ""
    for page in [
        list_orders(raw_conn, limit=3, offset=2),
        list_generation_tasks(raw_conn, limit=3, offset=2),
        list_asset_access_logs(raw_conn, limit=3, offset=2),
        list_risk_events(raw_conn, limit=3, offset=2),
        list_commission_settlements(raw_conn, limit=3, offset=2),
        list_withdrawals(raw_conn, limit=3, offset=2),
    ]:
        assert_page_contract(page, total=0, limit=3, offset=2)
        assert page["items"] == []
    bounded = list_orders(raw_conn, limit=999, offset=-3, sort="dropTable", order="sideways")
    assert_page_contract(bounded, total=0, limit=200, offset=0)
    assert bounded["sort"] == "created_at"
    assert bounded["order"] == "desc"

    conn = make_conn()

    dashboard = dashboard_summary(conn)
    assert dashboard["jobs"] == {
        "total": 0,
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "canceled": 0,
        "requested": 0,
        "completed": 0,
        "failedItems": 0,
        "successRate": 0.0,
    }
    assert dashboard["points"] == {
        "entries": 0,
        "credited": 0,
        "debited": 0,
        "net": 0,
        "latestBalance": 0,
    }
    assert dashboard["commissions"]["pendingAmount"] == 0
    assert dashboard["commissions"]["eligibleAmount"] == 0
    assert dashboard["commissions"]["settledAmount"] == 0
    assert dashboard["commissions"]["orderCount"] == 0
    assert dashboard["commissions"]["orders"]["total"] == 0
    assert dashboard["commissions"]["settlements"]["total"] == 0
    assert dashboard["risk"]["highestLevel"] == "info"
    assert dashboard["assetAccess"]["topDenyReason"] == ""
    assert recent_jobs(conn) == []
    assert commission_summary(conn)["topAgents"] == []
    assert risk_summary(conn)["recent"] == []
    assert asset_access_summary(conn)["recentDenied"] == []
    for page in [
        list_orders(conn, limit=4, offset=1),
        list_generation_tasks(conn, limit=4, offset=1),
        list_asset_access_logs(conn, limit=4, offset=1),
        list_risk_events(conn, limit=4, offset=1),
        list_commission_settlements(conn, limit=4, offset=1),
        list_withdrawals(conn, limit=4, offset=1),
    ]:
        assert_page_contract(page, total=0, limit=4, offset=1)
        assert page["items"] == []


def test_dashboard_and_recent_jobs_aggregate_generation_tables() -> None:
    conn = make_conn()
    conn.executemany(
        """
        INSERT INTO generation_jobs (
            id, style_id, quality, status, requested_count, completed_count, failed_count,
            error_message, created_at, updated_at, started_at, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "job_old",
                "style-a",
                "standard",
                "succeeded",
                3,
                3,
                0,
                "",
                "2026-06-01T10:00:00+00:00",
                "2026-06-01T10:03:00+00:00",
                "2026-06-01T10:01:00+00:00",
                "2026-06-01T10:03:00+00:00",
            ),
            (
                "job_new",
                "style-b",
                "premium",
                "failed",
                2,
                1,
                1,
                "model timeout",
                "2026-06-02T10:00:00+00:00",
                "2026-06-02T10:02:00+00:00",
                "2026-06-02T10:01:00+00:00",
                "2026-06-02T10:02:00+00:00",
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO generated_images (
            id, job_id, object_key, status, file_size, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("img_1", "job_old", "images/1.png", "generated", 100, "2026-06-01T10:02:00+00:00", "2026-06-01T10:02:00+00:00"),
            ("img_2", "job_old", "images/2.png", "generated", 200, "2026-06-01T10:02:00+00:00", "2026-06-01T10:02:00+00:00"),
            ("img_3", "job_new", "images/3.png", "failed", 0, "2026-06-02T10:02:00+00:00", "2026-06-02T10:02:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO export_packages (
            id, job_id, object_key, status, image_count, file_size, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("pkg_1", "job_old", "exports/1.zip", "ready", 2, 300, "2026-06-01T10:04:00+00:00", "2026-06-01T10:04:00+00:00"),
            ("pkg_2", "job_new", "exports/2.zip", "failed", 1, 0, "2026-06-02T10:04:00+00:00", "2026-06-02T10:04:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO point_ledger (
            id, account_id, job_id, amount, balance_after, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("pt_1", "acct_1", "job_old", 500, 500, "recharge", "2026-06-01T09:00:00+00:00"),
            ("pt_2", "acct_1", "job_old", -30, 470, "generation", "2026-06-01T10:00:00+00:00"),
            ("pt_3", "acct_1", "job_new", -20, 450, "generation", "2026-06-02T10:00:00+00:00"),
        ],
    )

    dashboard = dashboard_summary(conn)
    assert dashboard["jobs"]["total"] == 2
    assert dashboard["jobs"]["succeeded"] == 1
    assert dashboard["jobs"]["failed"] == 1
    assert dashboard["jobs"]["requested"] == 5
    assert dashboard["jobs"]["successRate"] == 0.8
    assert dashboard["images"]["generated"] == 2
    assert dashboard["images"]["failed"] == 1
    assert dashboard["images"]["totalFileSize"] == 300
    assert dashboard["exports"]["ready"] == 1
    assert dashboard["points"]["credited"] == 500
    assert dashboard["points"]["debited"] == 50
    assert dashboard["points"]["net"] == 450
    assert dashboard["points"]["latestBalance"] == 450

    jobs = recent_jobs(conn)
    assert [job["id"] for job in jobs] == ["job_new", "job_old"]
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["imageCount"] == 1
    assert jobs[0]["failedImageCount"] == 1
    assert jobs[0]["exportCount"] == 1
    assert jobs[0]["pointDelta"] == -20
    assert jobs[1]["generatedImageCount"] == 2
    assert jobs[1]["pointsCredited"] == 500
    assert jobs[1]["pointsDebited"] == 30
    assert [job["id"] for job in recent_jobs(conn, limit=1)] == ["job_new"]
    assert recent_jobs(conn, limit=0) == []


def test_commission_risk_and_asset_access_summaries() -> None:
    conn = make_conn()
    conn.executemany(
        """
        INSERT INTO agent_profiles (id, user_id, agent_code, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("agent_1", "user_1", "A001", "active", "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
            ("agent_2", "user_2", "A002", "pending", "2026-06-02T00:00:00+00:00", "2026-06-02T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO commission_orders (
            id, order_id, agent_id, customer_id, order_amount, commission_amount,
            commission_rate_bps, status, created_at, updated_at, settled_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("co_1", "ord_1", "agent_1", "cust_1", 10000, 1000, 1000, "eligible", "2026-06-01T01:00:00+00:00", "2026-06-01T01:00:00+00:00", None),
            ("co_2", "ord_2", "agent_1", "cust_2", 5000, 500, 1000, "settled", "2026-06-02T01:00:00+00:00", "2026-06-02T01:00:00+00:00", "2026-06-03T01:00:00+00:00"),
            ("co_3", "ord_3", "agent_2", "cust_3", 3000, 300, 1000, "canceled", "2026-06-03T01:00:00+00:00", "2026-06-03T01:00:00+00:00", None),
            ("co_4", "ord_4", "agent_2", "cust_4", 7000, 700, 1000, "pending", "2026-06-04T01:00:00+00:00", "2026-06-04T01:00:00+00:00", None),
        ],
    )
    conn.executemany(
        """
        INSERT INTO commission_settlements (
            id, agent_id, settlement_no, total_order_amount, total_commission_amount,
            order_count, status, created_at, updated_at, paid_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("set_1", "agent_1", "S001", 5000, 500, 1, "paid", "2026-06-03T02:00:00+00:00", "2026-06-03T02:00:00+00:00", "2026-06-03T03:00:00+00:00"),
            ("set_2", "agent_2", "S002", 3000, 300, 1, "pending", "2026-06-04T02:00:00+00:00", "2026-06-04T02:00:00+00:00", None),
        ],
    )
    conn.executemany(
        """
        INSERT INTO invite_relations (
            id, inviter_user_id, invitee_user_id, agent_id, status, reward_points,
            reward_status, created_at, updated_at, rewarded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("inv_1", "user_1", "cust_1", "agent_1", "accepted", 100, "granted", "2026-06-01T03:00:00+00:00", "2026-06-01T03:00:00+00:00", "2026-06-01T04:00:00+00:00"),
            ("inv_2", "user_2", "cust_2", "agent_2", "pending", 50, "pending", "2026-06-02T03:00:00+00:00", "2026-06-02T03:00:00+00:00", None),
        ],
    )
    conn.executemany(
        """
        INSERT INTO risk_audit_logs (
            id, user_id, agent_id, asset_id, event_type, risk_level, decision, ip,
            deny_reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("risk_1", "user_1", "agent_1", "asset_1", "asset_download", "high", "deny", "10.0.0.1", "quota", "2026-06-01T05:00:00+00:00"),
            ("risk_2", "user_2", "agent_2", "asset_2", "invite_bind", "medium", "review", "10.0.0.2", "", "2026-06-02T05:00:00+00:00"),
            ("risk_3", "user_3", "", "asset_3", "asset_download", "info", "allow", "10.0.0.1", "", "2026-06-03T05:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO asset_access_logs (
            id, user_id, agent_id, asset_id, asset_type, action, ip, allowed,
            deny_reason, request_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("access_1", "user_1", "agent_1", "asset_1", "image", "download", "10.0.0.1", 0, "quota", "req_1", "2026-06-01T06:00:00+00:00"),
            ("access_2", "user_1", "agent_1", "asset_1", "image", "preview", "10.0.0.1", 1, "", "req_2", "2026-06-02T06:00:00+00:00"),
            ("access_3", "user_2", "agent_2", "asset_2", "package", "download", "10.0.0.2", 1, "", "req_3", "2026-06-03T06:00:00+00:00"),
        ],
    )

    dashboard = dashboard_summary(conn)
    assert dashboard["commissions"]["pendingAmount"] == 700
    assert dashboard["commissions"]["eligibleAmount"] == 1000
    assert dashboard["commissions"]["settledAmount"] == 500
    assert dashboard["commissions"]["orderCount"] == 4
    assert dashboard["commissions"]["orders"]["total"] == 4
    assert dashboard["commissions"]["settlements"]["total"] == 2
    assert dashboard["risk"]["highestLevel"] == "high"
    assert dashboard["assetAccess"]["topDenyReason"] == "quota"

    commissions = commission_summary(conn)
    assert commissions["orders"]["total"] == 4
    assert commissions["orders"]["pendingCommissionAmount"] == 700
    assert commissions["orders"]["eligible"] == 1
    assert commissions["orders"]["settledCommissionAmount"] == 500
    assert commissions["settlements"]["paidCommissionAmount"] == 500
    assert commissions["agents"]["active"] == 1
    assert commissions["invites"]["rewardPoints"] == 150
    assert commissions["invites"]["grantedRewardPoints"] == 100
    assert commissions["topAgents"][0]["agentId"] == "agent_1"
    assert commissions["topAgents"][0]["commissionAmount"] == 1500

    risk = risk_summary(conn)
    assert risk["total"] == 3
    assert risk["highestLevel"] == "high"
    assert risk["byLevel"]["high"] == 1
    assert risk["byDecision"]["deny"] == 1
    assert risk["topEvents"][0] == {"name": "asset_download", "count": 2}
    assert risk["recent"][0]["id"] == "risk_3"

    asset_access = asset_access_summary(conn)
    assert asset_access["total"] == 3
    assert asset_access["allowed"] == 2
    assert asset_access["denied"] == 1
    assert asset_access["topDenyReason"] == "quota"
    assert asset_access["topAssets"][0] == {
        "assetId": "asset_1",
        "accessCount": 2,
        "deniedCount": 1,
    }
    assert asset_access["recentDenied"][0]["id"] == "access_1"


def test_user_store_and_order_lists_filter_page_and_sort() -> None:
    conn = make_product_conn()
    conn.executemany(
        """
        INSERT INTO users (id, phone, status, metadata_json, created_at, updated_at, last_login_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("user_1", "+8613000000001", "active", "{}", "2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00", "2026-06-03T00:00:00+00:00"),
            ("user_2", "+8613000000002", "disabled", "{}", "2026-06-02T00:00:00+00:00", "2026-06-02T00:00:00+00:00", None),
            ("user_3", "+8613000000003", "active", "{}", "2026-06-03T00:00:00+00:00", "2026-06-03T00:00:00+00:00", None),
        ],
    )
    conn.executemany(
        """
        INSERT INTO stores (id, name, status, created_by_user_id, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("store_1", "Store A", "active", "user_1", "{}", "2026-06-01T01:00:00+00:00", "2026-06-01T01:00:00+00:00"),
            ("store_2", "Store B", "archived", "user_2", "{}", "2026-06-02T01:00:00+00:00", "2026-06-02T01:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO user_stores (user_id, store_id, role, created_at) VALUES (?, ?, ?, ?)",
        [
            ("user_1", "store_1", "owner", "2026-06-01T01:00:00+00:00"),
            ("user_2", "store_2", "owner", "2026-06-02T01:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO payment_orders (
            order_id, user_id, provider, provider_order_id, amount_cents, points,
            status, created_at, updated_at, paid_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("pay_1", "user_1", "fake", "pay_1", 1000, 100, "paid", "2026-06-03T00:00:00+00:00", "2026-06-03T00:00:00+00:00", "2026-06-03T00:01:00+00:00"),
            ("pay_2", "user_2", "fake", "pay_2", 2000, 200, "pending", "2026-06-04T00:00:00+00:00", "2026-06-04T00:00:00+00:00", None),
            ("pay_3", "user_1", "fake", "pay_3", 500, 50, "refunded", "2026-06-05T00:00:00+00:00", "2026-06-05T00:00:00+00:00", None),
        ],
    )
    conn.executemany(
        """
        INSERT INTO menu_uploads (id, store_name, original_filename, object_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("menu_1", "Store A", "a.xlsx", "menus/a.xlsx", "2026-06-01T02:00:00+00:00", "2026-06-01T02:00:00+00:00"),
            ("menu_2", "Store A", "b.xlsx", "menus/b.xlsx", "2026-06-02T02:00:00+00:00", "2026-06-02T02:00:00+00:00"),
            ("menu_3", "Store B", "c.xlsx", "menus/c.xlsx", "2026-06-03T02:00:00+00:00", "2026-06-03T02:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO generation_jobs (id, menu_upload_id, style_id, quality, status, requested_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("job_1", "menu_1", "style-a", "standard", "succeeded", 2, "2026-06-01T03:00:00+00:00", "2026-06-01T03:00:00+00:00"),
            ("job_2", "menu_2", "style-a", "standard", "failed", 3, "2026-06-02T03:00:00+00:00", "2026-06-02T03:00:00+00:00"),
        ],
    )
    conn.execute(
        """
        INSERT INTO library_images (id, object_key, store_name, dish_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("lib_1", "library/a.png", "Store A", "Dish A", "2026-06-01T04:00:00+00:00", "2026-06-01T04:00:00+00:00"),
    )

    users = list_users(conn, status="active", sort="orderCount", order="desc", limit=1)
    assert users["total"] == 2
    assert users["items"][0]["userId"] == "user_1"
    assert users["items"][0]["orderCount"] == 2
    assert users["items"][0]["storeCount"] == 1
    assert list_users(conn, status="active", sort="orderCount", order="desc", limit=1, offset=1)["items"][0]["userId"] == "user_3"
    assert list_users(conn, search="0002")["items"][0]["userId"] == "user_2"

    stores = list_stores(conn, sort="menuUploadCount", order="desc", limit=1)
    assert stores["total"] == 2
    assert stores["items"][0]["storeId"] == "store_1"
    assert stores["items"][0]["menuUploadCount"] == 2
    assert stores["items"][0]["jobCount"] == 2
    assert stores["items"][0]["assetCount"] == 1
    assert list_stores(conn, status="active")["items"][0]["name"] == "Store A"

    orders = list_orders(conn, user_id="user_1", sort="amountCents", order="asc")
    assert_page_contract(orders, total=2, limit=50, offset=0)
    assert orders["total"] == 2
    assert [item["orderId"] for item in orders["items"]] == ["pay_3", "pay_1"]
    assert set(orders["items"][0]) >= {
        "id",
        "orderId",
        "userId",
        "provider",
        "providerOrderId",
        "amountCents",
        "points",
        "status",
        "createdAt",
        "updatedAt",
        "paidAt",
        "closedAt",
        "refundedAt",
    }
    assert list_orders(conn, status="paid")["items"][0]["points"] == 100
    assert list_orders(conn, search="pay_2")["items"][0]["orderId"] == "pay_2"
    paid_window = list_orders(
        conn,
        status="paid",
        created_from="2026-06-03T00:00:00+00:00",
        created_to="2026-06-03T23:59:59+00:00",
    )
    assert [item["orderId"] for item in paid_window["items"]] == ["pay_1"]


def test_task_asset_and_commission_lists_filter_page_and_sort() -> None:
    conn = make_conn()
    conn.executemany(
        """
        INSERT INTO menu_uploads (id, store_name, original_filename, object_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("menu_1", "Store A", "a.xlsx", "menus/a.xlsx", "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
            ("menu_2", "Store B", "b.xlsx", "menus/b.xlsx", "2026-06-02T00:00:00+00:00", "2026-06-02T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO generation_jobs (
            id, menu_upload_id, style_id, quality, status, requested_count,
            completed_count, failed_count, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("job_small", "menu_1", "style-a", "standard", "succeeded", 2, 2, 0, "2026-06-01T01:00:00+00:00", "2026-06-01T01:00:00+00:00"),
            ("job_big", "menu_2", "style-b", "premium", "failed", 5, 3, 2, "2026-06-02T01:00:00+00:00", "2026-06-02T01:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO generated_images (id, job_id, object_key, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("img_1", "job_big", "images/1.png", "generated", "2026-06-02T02:00:00+00:00", "2026-06-02T02:00:00+00:00"),
            ("img_2", "job_big", "images/2.png", "failed", "2026-06-02T02:00:00+00:00", "2026-06-02T02:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO asset_access_logs (
            id, user_id, agent_id, asset_id, asset_type, action, ip, allowed,
            deny_reason, request_id, user_agent, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("access_old", "user_1", "agent_1", "asset_1", "image", "download", "10.0.0.1", 0, "quota", "req_1", "pytest", "2026-06-01T03:00:00+00:00"),
            ("access_new", "user_2", "agent_2", "asset_2", "package", "download", "10.0.0.2", 0, "blocked", "req_2", "pytest", "2026-06-02T03:00:00+00:00"),
            ("access_allowed", "user_2", "agent_2", "asset_2", "package", "preview", "10.0.0.2", 1, "", "req_3", "pytest", "2026-06-03T03:00:00+00:00"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO commission_settlements (
            id, agent_id, settlement_no, total_order_amount, total_commission_amount,
            order_count, status, created_at, updated_at, paid_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("set_small", "agent_1", "S001", 1000, 100, 1, "pending", "2026-06-01T04:00:00+00:00", "2026-06-01T04:00:00+00:00", None),
            ("set_big", "agent_1", "S002", 5000, 500, 3, "paid", "2026-06-02T04:00:00+00:00", "2026-06-02T04:00:00+00:00", "2026-06-02T05:00:00+00:00"),
        ],
    )

    tasks = list_generation_tasks(conn, sort="requestedCount", order="desc", limit=1)
    assert_page_contract(tasks, total=2, limit=1, offset=0)
    assert tasks["total"] == 2
    assert tasks["items"][0]["id"] == "job_big"
    assert tasks["items"][0]["imageCount"] == 2
    assert set(tasks["items"][0]) >= {
        "id",
        "menuUploadId",
        "storeName",
        "originalFilename",
        "styleId",
        "quality",
        "status",
        "requestedCount",
        "completedCount",
        "failedCount",
        "progress",
        "imageCount",
        "exportCount",
        "pointDelta",
        "errorMessage",
        "createdAt",
        "updatedAt",
        "startedAt",
        "completedAt",
    }
    assert list_generation_tasks(conn, status="failed")["items"][0]["storeName"] == "Store B"
    task_window = list_generation_tasks(
        conn,
        status="failed",
        search="b.xlsx",
        created_from="2026-06-02T00:00:00+00:00",
        created_to="2026-06-02T23:59:59+00:00",
    )
    assert [item["id"] for item in task_window["items"]] == ["job_big"]
    assert list_generation_tasks(conn, search="Store A")["items"][0]["id"] == "job_small"

    denied = list_asset_access_logs(conn, allowed=False, sort="createdAt", order="desc", limit=1)
    assert_page_contract(denied, total=2, limit=1, offset=0)
    assert denied["total"] == 2
    assert denied["items"][0]["id"] == "access_new"
    assert denied["items"][0]["allowed"] is False
    assert set(denied["items"][0]) >= {
        "id",
        "userId",
        "agentId",
        "assetId",
        "assetType",
        "action",
        "ip",
        "allowed",
        "denyReason",
        "requestId",
        "userAgent",
        "createdAt",
    }
    assert list_asset_access_logs(conn, search="req_3")["items"][0]["id"] == "access_allowed"
    access_window = list_asset_access_logs(
        conn,
        status="denied",
        search="blocked",
        created_from="2026-06-02T00:00:00+00:00",
        created_to="2026-06-02T23:59:59+00:00",
    )
    assert [item["id"] for item in access_window["items"]] == ["access_new"]

    settlements = list_commission_settlements(conn, agent_id="agent_1", sort="totalCommissionAmount", order="desc", limit=1)
    assert_page_contract(settlements, total=2, limit=1, offset=0)
    assert settlements["total"] == 2
    assert settlements["items"][0]["id"] == "set_big"
    assert settlements["items"][0]["totalCommissionAmount"] == 500
    assert set(settlements["items"][0]) >= {
        "id",
        "agentId",
        "settlementNo",
        "periodStart",
        "periodEnd",
        "totalOrderAmount",
        "totalCommissionAmount",
        "orderCount",
        "currency",
        "status",
        "paidAt",
        "failureReason",
        "createdAt",
        "updatedAt",
    }
    assert list_commission_settlements(conn, status="pending")["items"][0]["id"] == "set_small"
    settlement_window = list_commission_settlements(
        conn,
        status="paid",
        search="S002",
        created_from="2026-06-02T00:00:00+00:00",
        created_to="2026-06-02T23:59:59+00:00",
    )
    assert [item["id"] for item in settlement_window["items"]] == ["set_big"]


def test_risk_event_list_filters_page_and_sort() -> None:
    conn = make_conn()
    conn.executemany(
        """
        INSERT INTO risk_audit_logs (
            id, user_id, agent_id, asset_id, event_type, risk_level, decision,
            ip, deny_reason, metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "risk_old",
                "user_1",
                "agent_1",
                "asset_old",
                "asset_download",
                "low",
                "allow",
                "10.0.0.1",
                "",
                '{"keyword":"old"}',
                "2026-06-01T05:00:00+00:00",
            ),
            (
                "risk_target",
                "user_2",
                "agent_2",
                "asset_target",
                "invite_bind",
                "high",
                "deny",
                "10.0.0.2",
                "same phone",
                '{"keyword":"target-user"}',
                "2026-06-02T05:00:00+00:00",
            ),
            (
                "risk_review",
                "user_2",
                "agent_2",
                "asset_review",
                "asset_download",
                "medium",
                "review",
                "10.0.0.3",
                "velocity",
                '{"keyword":"review"}',
                "2026-06-03T05:00:00+00:00",
            ),
        ],
    )

    denied = list_risk_events(conn, decision="deny", sort="riskLevel", order="desc", limit=1)
    assert_page_contract(denied, total=1, limit=1, offset=0)
    assert denied["items"][0]["id"] == "risk_target"
    assert set(denied["items"][0]) >= {
        "id",
        "userId",
        "agentId",
        "assetId",
        "eventType",
        "riskLevel",
        "decision",
        "ip",
        "denyReason",
        "metadataJson",
        "createdAt",
    }
    assert denied["items"][0]["riskLevel"] == "high"
    assert denied["items"][0]["denyReason"] == "same phone"
    assert denied["sort"] == "risk_level"

    event_window = list_risk_events(
        conn,
        risk_level="high",
        event_type="invite_bind",
        user_id="user_2",
        search="target-user",
        created_from="2026-06-02T00:00:00+00:00",
        created_to="2026-06-02T23:59:59+00:00",
    )
    assert [item["id"] for item in event_window["items"]] == ["risk_target"]

    user_events = list_risk_events(conn, user_id="user_2", sort="createdAt", order="asc", limit=1, offset=1)
    assert_page_contract(user_events, total=2, limit=1, offset=1)
    assert user_events["items"][0]["id"] == "risk_review"


def test_withdrawal_list_filters_page_sort_and_balance_fields() -> None:
    conn = make_conn()
    conn.executemany(
        """
        INSERT INTO agent_withdrawal_requests (
            id, agent_id, amount_cents, currency, status, account_snapshot_json,
            balance_snapshot_json, status_reason, metadata_json, created_at, updated_at,
            approved_at, rejected_at, paid_at, canceled_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "wd_old",
                "agent_1",
                10000,
                "CNY",
                "pending",
                "{}",
                '{"availableCents":30000,"paidSettlementCents":40000,"lockedWithdrawalCents":10000}',
                "",
                '{"requestNo":"old"}',
                "2026-06-01T07:00:00+00:00",
                "2026-06-01T07:00:00+00:00",
                None,
                None,
                None,
                None,
            ),
            (
                "wd_target",
                "agent_2",
                25000,
                "CNY",
                "approved",
                "{}",
                '{"availableCents":50000,"paidSettlementCents":80000,"lockedWithdrawalCents":30000}',
                "bank verified",
                '{"requestNo":"target-withdrawal"}',
                "2026-06-02T07:00:00+00:00",
                "2026-06-02T08:00:00+00:00",
                "2026-06-02T08:00:00+00:00",
                None,
                None,
                None,
            ),
            (
                "wd_paid",
                "agent_2",
                15000,
                "CNY",
                "paid",
                "{}",
                '{"availableCents":25000,"paidSettlementCents":80000,"lockedWithdrawalCents":55000}',
                "",
                '{"requestNo":"paid"}',
                "2026-06-03T07:00:00+00:00",
                "2026-06-03T08:00:00+00:00",
                "2026-06-03T08:00:00+00:00",
                None,
                "2026-06-03T09:00:00+00:00",
                None,
            ),
        ],
    )

    approved = list_withdrawals(conn, status="approved", sort="amountCents", order="desc", limit=1)
    assert_page_contract(approved, total=1, limit=1, offset=0)
    item = approved["items"][0]
    assert item["id"] == "wd_target"
    assert item["withdrawalId"] == "wd_target"
    assert item["agentId"] == "agent_2"
    assert item["amountCents"] == 25000
    assert item["currency"] == "CNY"
    assert item["status"] == "approved"
    assert item["balanceAvailableCents"] == 50000
    assert item["balancePaidSettlementCents"] == 80000
    assert item["balanceLockedWithdrawalCents"] == 30000
    assert item["balanceSnapshot"]["availableCents"] == 50000
    assert item["statusReason"] == "bank verified"
    assert item["approvedAt"] == "2026-06-02T08:00:00+00:00"
    assert set(item) >= {
        "id",
        "withdrawalId",
        "agentId",
        "amountCents",
        "currency",
        "status",
        "balanceSnapshot",
        "balanceAvailableCents",
        "balancePaidSettlementCents",
        "balanceLockedWithdrawalCents",
        "statusReason",
        "createdAt",
        "updatedAt",
        "approvedAt",
        "rejectedAt",
        "paidAt",
        "canceledAt",
    }

    target_window = list_withdrawals(
        conn,
        agent_id="agent_2",
        search="target-withdrawal",
        created_from="2026-06-02T00:00:00+00:00",
        created_to="2026-06-02T23:59:59+00:00",
    )
    assert [row["id"] for row in target_window["items"]] == ["wd_target"]

    second_agent = list_withdrawals(conn, agent_id="agent_2", sort="createdAt", order="asc", limit=1, offset=1)
    assert_page_contract(second_agent, total=2, limit=1, offset=1)
    assert second_agent["items"][0]["id"] == "wd_paid"
