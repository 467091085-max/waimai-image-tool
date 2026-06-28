from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Flask

from ai_asset_repository import AIAssetRepository
from admin_panel import AdminDependencies, create_admin_blueprint


ROOT = Path(__file__).resolve().parents[1]


def make_app(
    upload_dir: Path,
    ai_asset_manifest_path: Path | None = None,
    db_path: Path | None = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )

    images = [
        SimpleNamespace(
            image_id="clean001",
            path=upload_dir / "clean.jpg",
            store="测试门店A",
            dish="辣椒炒肉",
            style_id="style-clean",
            source="clean",
            reusable=True,
        ),
        SimpleNamespace(
            image_id="watermark001",
            path=upload_dir / "watermark.jpg",
            store="测试门店B",
            dish="小炒黄牛肉",
            style_id="style-watermark",
            source="watermark",
            reusable=False,
        ),
        SimpleNamespace(
            image_id="internal001",
            path=upload_dir / "internal.jpg",
            store="演示门店",
            dish="茄子肉末",
            style_id="style-1",
            source="internal",
            reusable=True,
        ),
    ]

    def parse_menu(path: Path | None = None) -> dict[str, Any]:
        if path is not None and path.name == "broken.xlsx":
            raise ValueError("bad workbook")
        return {
            "file": path.name if path else "demo_menu.xlsx",
            "store": "测试门店",
            "count": 3,
            "kindCounts": {"single": 1, "combo": 1, "snack": 1, "total": 3},
            "sheets": [{"sheet": "菜单", "headerRow": 1, "items": 3, "score": 166.5}],
            "errors": [],
            "items": [{"name": "辣椒炒肉"}],
            "demo": path is None,
        }

    app.register_blueprint(
        create_admin_blueprint(
            AdminDependencies(
                library_images=lambda: images,
                media_url_for_path=lambda path: f"/media/{path.name}",
                current_menu_path=lambda: upload_dir / "ok.xlsx",
                parse_menu=parse_menu,
                upload_dir=upload_dir,
                db_path=db_path or upload_dir / "admin-test.db",
                ai_asset_manifest_path=ai_asset_manifest_path,
            )
        )
    )
    return app


class AdminPanelTests(unittest.TestCase):
    def assert_page_contract(self, data: dict[str, Any], resource: str, total: int, limit: int, offset: int) -> None:
        self.assertTrue(data["ok"])
        self.assertEqual(data["resource"], resource)
        self.assertIsInstance(data["items"], list)
        self.assertEqual(data["total"], total)
        self.assertEqual(data["limit"], limit)
        self.assertEqual(data["offset"], offset)
        self.assertIn("generatedAt", data)

    def test_admin_page_loads_without_customer_homepage_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/admin")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("/static/admin.css", body)
        self.assertIn("/static/admin.js", body)
        self.assertNotIn("/static/app.js", body)

    def test_library_sample_returns_counts_samples_and_no_paths_or_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/api/admin/library-sample?limit=10")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["summary"]["total"], 3)
        self.assertEqual(data["sources"]["clean"], 1)
        self.assertEqual(data["sources"]["watermark"], 1)
        self.assertEqual(data["sources"]["internal"], 1)
        self.assertEqual({sample["source"] for sample in data["samples"]}, {"clean", "watermark", "internal"})
        for sample in data["samples"]:
            self.assertGreaterEqual(sample.keys(), {"imageId", "dishName", "store", "source", "reusable", "url"})
            self.assertNotIn("path", sample)
        payload = json.dumps(data, ensure_ascii=False).lower()
        self.assertNotIn("secret", payload)

    def test_menu_audit_returns_current_and_upload_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            (upload_dir / "ok.xlsx").write_text("fake", encoding="utf-8")
            (upload_dir / "broken.xlsx").write_text("fake", encoding="utf-8")
            (upload_dir / "ignore.txt").write_text("fake", encoding="utf-8")
            client = make_app(upload_dir).test_client()
            response = client.get("/api/admin/menu-audit")

            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["current"]["available"])
            self.assertNotIn("items", data["current"])
            self.assertEqual(data["audit"]["files"], 2)
            self.assertEqual(data["audit"]["parsed"], 1)
            self.assertEqual(data["audit"]["failed"], 1)
            self.assertEqual(data["audit"]["totalItems"], 3)
            self.assertEqual(data["parser"]["supportedExtensions"], [".xls", ".xlsx"])
            self.assertNotIn(str(upload_dir), json.dumps(data, ensure_ascii=False))

    def test_admin_dashboard_returns_stable_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/api/admin/dashboard")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("summary", data)
        self.assertIn("jobs", data["summary"])
        self.assertIn("commissions", data["summary"])
        self.assertIn("risk", data["summary"])
        self.assertIn("assetAccess", data["summary"])
        self.assertIsInstance(data["recentJobs"], list)

    def test_admin_list_users_returns_paginated_payload(self) -> None:
        import auth_service
        import storage_db

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            db_path = upload_dir / "admin.db"
            conn = storage_db.init_db(db_path)
            try:
                auth_service.init_auth_schema(conn)
                conn.execute(
                    """
                    INSERT INTO users (id, phone, status, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, '{}', ?, ?)
                    """,
                    (
                        "user_1",
                        "+8613800138000",
                        "active",
                        "2026-06-28T00:00:00+00:00",
                        "2026-06-28T00:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            client = make_app(upload_dir, db_path=db_path).test_client()
            response = client.get("/api/admin/lists/users?status=active&sort=createdAt&order=asc&limit=5")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["resource"], "users")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["userId"], "user_1")
        self.assertEqual(data["items"][0]["phone"], "+8613800138000")

    def test_admin_product_lists_return_empty_paginated_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            responses = {
                resource: client.get(f"/api/admin/lists/{resource}?limit=7&offset=2")
                for resource in [
                    "generation-tasks",
                    "asset-access",
                    "risk-events",
                    "commission-settlements",
                    "withdrawals",
                    "orders",
                ]
            }

        for resource, response in responses.items():
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assert_page_contract(data, resource, total=0, limit=7, offset=2)
            self.assertEqual(data["items"], [])

        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/api/admin/lists/orders?limit=999&offset=-3&sort=dropTable&order=sideways")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assert_page_contract(data, "orders", total=0, limit=200, offset=0)
        self.assertEqual(data["sort"], "created_at")
        self.assertEqual(data["order"], "desc")

    def test_admin_product_lists_return_paginated_items_with_basic_fields(self) -> None:
        import auth_service
        import payment_service
        import storage_db

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            db_path = upload_dir / "admin.db"
            conn = storage_db.init_db(db_path)
            try:
                auth_service.init_auth_schema(conn)
                payment_service.init_payment_schema(conn)
                conn.execute(
                    """
                    INSERT INTO payment_orders (
                        order_id, user_id, provider, provider_order_id, amount_cents,
                        points, status, created_at, updated_at, paid_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "pay_1",
                        "user_1",
                        "fake",
                        "provider_pay_1",
                        1200,
                        120,
                        "paid",
                        "2026-06-28T00:00:00+00:00",
                        "2026-06-28T00:00:00+00:00",
                        "2026-06-28T00:01:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO menu_uploads (
                        id, store_name, original_filename, object_key, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "menu_1",
                        "测试门店",
                        "menu.xlsx",
                        "menus/menu.xlsx",
                        "2026-06-28T01:00:00+00:00",
                        "2026-06-28T01:00:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO generation_jobs (
                        id, menu_upload_id, style_id, quality, status, requested_count,
                        completed_count, failed_count, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "job_1",
                        "menu_1",
                        "style-clean",
                        "standard",
                        "succeeded",
                        3,
                        3,
                        0,
                        "2026-06-28T01:10:00+00:00",
                        "2026-06-28T01:20:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO generated_images (id, job_id, object_key, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "img_1",
                        "job_1",
                        "images/job_1.png",
                        "generated",
                        "2026-06-28T01:15:00+00:00",
                        "2026-06-28T01:15:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO export_packages (id, job_id, object_key, status, image_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "pkg_1",
                        "job_1",
                        "exports/job_1.zip",
                        "ready",
                        1,
                        "2026-06-28T01:25:00+00:00",
                        "2026-06-28T01:25:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO point_ledger (id, account_id, job_id, amount, balance_after, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "pt_1",
                        "acct_1",
                        "job_1",
                        -30,
                        70,
                        "generation",
                        "2026-06-28T01:10:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO asset_access_logs (
                        id, user_id, agent_id, asset_id, asset_type, action, ip,
                        allowed, deny_reason, request_id, user_agent, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "access_1",
                        "user_1",
                        "agent_1",
                        "asset_1",
                        "image",
                        "download",
                        "10.0.0.1",
                        0,
                        "quota",
                        "req_1",
                        "pytest",
                        "2026-06-28T02:00:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO risk_audit_logs (
                        id, user_id, agent_id, asset_id, event_type, risk_level,
                        decision, ip, deny_reason, metadata_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "risk_1",
                        "user_1",
                        "agent_1",
                        "asset_1",
                        "asset_download",
                        "high",
                        "deny",
                        "10.0.0.1",
                        "quota",
                        '{"source":"pytest"}',
                        "2026-06-28T02:30:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO commission_settlements (
                        id, agent_id, settlement_no, period_start, period_end,
                        total_order_amount, total_commission_amount, order_count,
                        currency, status, paid_at, failure_reason, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "set_1",
                        "agent_1",
                        "S001",
                        "2026-06-01",
                        "2026-06-30",
                        1200,
                        240,
                        1,
                        "CNY",
                        "paid",
                        "2026-06-28T03:00:00+00:00",
                        "",
                        "2026-06-28T03:00:00+00:00",
                        "2026-06-28T03:00:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_profiles (id, user_id, agent_code, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent_1",
                        "user_1",
                        "A001",
                        "active",
                        "2026-06-28T03:20:00+00:00",
                        "2026-06-28T03:20:00+00:00",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_withdrawal_requests (
                        id, agent_id, amount_cents, currency, status,
                        account_snapshot_json, balance_snapshot_json, status_reason,
                        metadata_json, created_at, updated_at, approved_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "wd_1",
                        "agent_1",
                        20000,
                        "CNY",
                        "approved",
                        "{}",
                        '{"availableCents":50000,"paidSettlementCents":70000,"lockedWithdrawalCents":20000}',
                        "bank verified",
                        '{"requestNo":"wd-admin-1"}',
                        "2026-06-28T03:30:00+00:00",
                        "2026-06-28T03:40:00+00:00",
                        "2026-06-28T03:40:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            client = make_app(upload_dir, db_path=db_path).test_client()
            cases = {
                "orders": (
                    client.get("/api/admin/lists/orders?status=paid&sort=amountCents&order=desc&limit=1"),
                    {
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
                    },
                    {"orderId": "pay_1", "amountCents": 1200, "points": 120, "status": "paid"},
                ),
                "generation-tasks": (
                    client.get("/api/admin/lists/generation-tasks?status=succeeded&sort=requestedCount&order=desc&limit=1"),
                    {
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
                    },
                    {
                        "id": "job_1",
                        "storeName": "测试门店",
                        "requestedCount": 3,
                        "imageCount": 1,
                        "exportCount": 1,
                        "pointDelta": -30,
                    },
                ),
                "asset-access": (
                    client.get("/api/admin/lists/asset-access?allowed=false&sort=createdAt&order=desc&limit=1"),
                    {
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
                    },
                    {"id": "access_1", "assetId": "asset_1", "allowed": False, "denyReason": "quota"},
                ),
                "risk-events": (
                    client.get("/api/admin/lists/risk-events?decision=deny&sort=riskLevel&order=desc&limit=1"),
                    {
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
                    },
                    {
                        "id": "risk_1",
                        "userId": "user_1",
                        "eventType": "asset_download",
                        "riskLevel": "high",
                        "decision": "deny",
                    },
                ),
                "commission-settlements": (
                    client.get("/api/admin/lists/commission-settlements?status=paid&sort=totalCommissionAmount&order=desc&limit=1"),
                    {
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
                    },
                    {
                        "id": "set_1",
                        "agentId": "agent_1",
                        "totalOrderAmount": 1200,
                        "totalCommissionAmount": 240,
                        "status": "paid",
                    },
                ),
                "withdrawals": (
                    client.get("/api/admin/lists/withdrawals?status=approved&sort=amountCents&order=desc&limit=1"),
                    {
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
                    },
                    {
                        "id": "wd_1",
                        "agentId": "agent_1",
                        "amountCents": 20000,
                        "balanceAvailableCents": 50000,
                        "status": "approved",
                    },
                ),
            }

        for resource, (response, required_fields, expected_values) in cases.items():
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assert_page_contract(data, resource, total=1, limit=1, offset=0)
            self.assertEqual(len(data["items"]), 1)
            item = data["items"][0]
            self.assertGreaterEqual(item.keys(), required_fields)
            for key, value in expected_values.items():
                self.assertEqual(item[key], value)

    def test_admin_product_list_filters_map_query_params(self) -> None:
        import auth_service
        import payment_service
        import storage_db

        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            db_path = upload_dir / "admin.db"
            conn = storage_db.init_db(db_path)
            try:
                auth_service.init_auth_schema(conn)
                payment_service.init_payment_schema(conn)
                conn.executemany(
                    """
                    INSERT INTO payment_orders (
                        order_id, user_id, provider, provider_order_id, amount_cents,
                        points, status, created_at, updated_at, paid_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "pay_old",
                            "user_1",
                            "fake",
                            "provider_pay_old",
                            800,
                            80,
                            "pending",
                            "2026-06-01T00:00:00+00:00",
                            "2026-06-01T00:00:00+00:00",
                            None,
                        ),
                        (
                            "pay_target",
                            "user_2",
                            "fake",
                            "provider_pay_target",
                            1200,
                            120,
                            "paid",
                            "2026-06-02T00:00:00+00:00",
                            "2026-06-02T00:00:00+00:00",
                            "2026-06-02T00:01:00+00:00",
                        ),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO menu_uploads (
                        id, store_name, original_filename, object_key, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "menu_old",
                            "普通门店",
                            "old.xlsx",
                            "menus/old.xlsx",
                            "2026-06-01T01:00:00+00:00",
                            "2026-06-01T01:00:00+00:00",
                        ),
                        (
                            "menu_target",
                            "目标门店",
                            "target-menu.xlsx",
                            "menus/target-menu.xlsx",
                            "2026-06-02T01:00:00+00:00",
                            "2026-06-02T01:00:00+00:00",
                        ),
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
                        (
                            "job_old",
                            "menu_old",
                            "style-a",
                            "standard",
                            "succeeded",
                            2,
                            2,
                            0,
                            "2026-06-01T02:00:00+00:00",
                            "2026-06-01T02:00:00+00:00",
                        ),
                        (
                            "job_target",
                            "menu_target",
                            "style-b",
                            "premium",
                            "failed",
                            5,
                            3,
                            2,
                            "2026-06-02T02:00:00+00:00",
                            "2026-06-02T02:00:00+00:00",
                        ),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO asset_access_logs (
                        id, user_id, agent_id, asset_id, asset_type, action, ip,
                        allowed, deny_reason, request_id, user_agent, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "access_old",
                            "user_1",
                            "agent_1",
                            "asset_old",
                            "image",
                            "preview",
                            "10.0.0.1",
                            1,
                            "",
                            "req_old",
                            "pytest",
                            "2026-06-01T03:00:00+00:00",
                        ),
                        (
                            "access_target",
                            "user_2",
                            "agent_2",
                            "asset_target",
                            "image",
                            "download",
                            "10.0.0.2",
                            0,
                            "quota",
                            "req_target",
                            "pytest",
                            "2026-06-02T03:00:00+00:00",
                        ),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO risk_audit_logs (
                        id, user_id, agent_id, asset_id, event_type, risk_level,
                        decision, ip, deny_reason, metadata_json, created_at
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
                            "2026-06-01T03:30:00+00:00",
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
                            '{"keyword":"target-risk"}',
                            "2026-06-02T03:30:00+00:00",
                        ),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO commission_settlements (
                        id, agent_id, settlement_no, total_order_amount,
                        total_commission_amount, order_count, status, created_at, updated_at, paid_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "set_old",
                            "agent_1",
                            "S-OLD",
                            1000,
                            100,
                            1,
                            "pending",
                            "2026-06-01T04:00:00+00:00",
                            "2026-06-01T04:00:00+00:00",
                            None,
                        ),
                        (
                            "set_target",
                            "agent_2",
                            "S-TARGET",
                            5000,
                            500,
                            3,
                            "paid",
                            "2026-06-02T04:00:00+00:00",
                            "2026-06-02T04:00:00+00:00",
                            "2026-06-02T05:00:00+00:00",
                        ),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO agent_profiles (id, user_id, agent_code, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("agent_1", "user_1", "A001", "active", "2026-06-01T04:20:00+00:00", "2026-06-01T04:20:00+00:00"),
                        ("agent_2", "user_2", "A002", "active", "2026-06-02T04:20:00+00:00", "2026-06-02T04:20:00+00:00"),
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO agent_withdrawal_requests (
                        id, agent_id, amount_cents, currency, status,
                        account_snapshot_json, balance_snapshot_json, status_reason,
                        metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "wd_old",
                            "agent_1",
                            10000,
                            "CNY",
                            "pending",
                            "{}",
                            '{"availableCents":20000,"paidSettlementCents":30000,"lockedWithdrawalCents":10000}',
                            "",
                            '{"requestNo":"old-withdrawal"}',
                            "2026-06-01T04:30:00+00:00",
                            "2026-06-01T04:30:00+00:00",
                        ),
                        (
                            "wd_target",
                            "agent_2",
                            22000,
                            "CNY",
                            "approved",
                            "{}",
                            '{"availableCents":60000,"paidSettlementCents":82000,"lockedWithdrawalCents":22000}',
                            "verified",
                            '{"requestNo":"target-withdrawal"}',
                            "2026-06-02T04:30:00+00:00",
                            "2026-06-02T04:30:00+00:00",
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            client = make_app(upload_dir, db_path=db_path).test_client()
            cases = {
                "orders": (
                    "/api/admin/lists/orders",
                    {
                        "status": "paid",
                        "search": "provider_pay_target",
                        "createdFrom": "2026-06-02T00:00:00+00:00",
                        "createdTo": "2026-06-02T23:59:59+00:00",
                        "sort": "createdAt",
                        "order": "asc",
                        "limit": "1",
                    },
                    "orderId",
                    "pay_target",
                ),
                "generation-tasks": (
                    "/api/admin/lists/generation-tasks",
                    {
                        "status": "failed",
                        "search": "target-menu",
                        "createdFrom": "2026-06-02T00:00:00+00:00",
                        "createdTo": "2026-06-02T23:59:59+00:00",
                        "sort": "requestedCount",
                        "order": "desc",
                        "limit": "1",
                    },
                    "id",
                    "job_target",
                ),
                "asset-access": (
                    "/api/admin/lists/asset-access",
                    {
                        "status": "denied",
                        "search": "quota",
                        "createdFrom": "2026-06-02T00:00:00+00:00",
                        "createdTo": "2026-06-02T23:59:59+00:00",
                        "sort": "createdAt",
                        "order": "asc",
                        "limit": "1",
                    },
                    "id",
                    "access_target",
                ),
                "risk-events": (
                    "/api/admin/lists/risk-events",
                    {
                        "decision": "deny",
                        "riskLevel": "high",
                        "eventType": "invite_bind",
                        "userId": "user_2",
                        "search": "target-risk",
                        "createdFrom": "2026-06-02T00:00:00+00:00",
                        "createdTo": "2026-06-02T23:59:59+00:00",
                        "sort": "riskLevel",
                        "order": "desc",
                        "limit": "1",
                    },
                    "id",
                    "risk_target",
                ),
                "commission-settlements": (
                    "/api/admin/lists/commission-settlements",
                    {
                        "status": "paid",
                        "search": "S-TARGET",
                        "createdFrom": "2026-06-02T00:00:00+00:00",
                        "createdTo": "2026-06-02T23:59:59+00:00",
                        "sort": "totalCommissionAmount",
                        "order": "desc",
                        "limit": "1",
                    },
                    "id",
                    "set_target",
                ),
                "withdrawals": (
                    "/api/admin/lists/withdrawals",
                    {
                        "status": "approved",
                        "agentId": "agent_2",
                        "search": "target-withdrawal",
                        "createdFrom": "2026-06-02T00:00:00+00:00",
                        "createdTo": "2026-06-02T23:59:59+00:00",
                        "sort": "amountCents",
                        "order": "desc",
                        "limit": "1",
                    },
                    "id",
                    "wd_target",
                ),
            }

            for resource, (path, query, id_field, expected_id) in cases.items():
                response = client.get(path, query_string=query)
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assert_page_contract(data, resource, total=1, limit=1, offset=0)
                self.assertEqual(data["items"][0][id_field], expected_id)
                self.assertIn(data["sort"], {"created_at", "requested_count", "risk_level", "total_commission_amount", "amount_cents"})
                self.assertIn(data["order"], {"asc", "desc"})

    def test_admin_list_rejects_unknown_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/api/admin/lists/secrets")

        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "unsupported_admin_list_resource")

    def test_ai_assets_returns_empty_library_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            manifest = upload_dir / "missing-manifest.jsonl"
            client = make_app(upload_dir, ai_asset_manifest_path=manifest).test_client()
            response = client.get("/api/admin/ai-assets")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(
            data["summary"],
            {
                "total": 0,
                "approved": 0,
                "rejected": 0,
                "disabled": 0,
                "pending": 0,
                "byKind": {},
                "byCategory": {},
            },
        )
        self.assertEqual(data["assets"], [])

    def test_ai_assets_reads_manifest_summary_and_hides_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            manifest = upload_dir / "manifest.jsonl"
            repo = AIAssetRepository(manifest)
            statuses = ["approved", "approved", "rejected", "disabled"]
            for index in range(55):
                repo.upsert(
                    {
                        "asset_id": f"asset-{index:02d}",
                        "kind": "product_image" if index % 2 == 0 else "category_background",
                        "category": "粉面米线" if index % 2 == 0 else "轻食健康餐",
                        "style_id": "style-1",
                        "product_name": f"菜品{index:02d}",
                        "quality_score": 0.8,
                        "object_key": f"ai-assets/secret-folder/asset-{index:02d}.jpg",
                        "local_path": str(upload_dir / f"secret-token-{index:02d}.jpg"),
                        "sha256": f"{index:064x}",
                        "created_at": "2026-06-28T00:00:00Z",
                        "status": statuses[index % len(statuses)],
                    }
                )

            client = make_app(upload_dir, ai_asset_manifest_path=manifest).test_client()
            response = client.get("/api/admin/ai-assets")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        summary = data["summary"]
        self.assertEqual(summary["total"], 55)
        self.assertEqual(summary["approved"], 28)
        self.assertEqual(summary["rejected"], 14)
        self.assertEqual(summary["disabled"], 13)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["byKind"], {"category_background": 27, "product_image": 28})
        self.assertEqual(summary["byCategory"], {"粉面米线": 28, "轻食健康餐": 27})
        self.assertEqual(len(data["assets"]), 50)
        self.assertEqual(data["assets"][0]["assetId"], "asset-00")
        self.assertGreaterEqual(
            data["assets"][0].keys(),
            {"assetId", "kind", "category", "styleId", "productName", "qualityScore", "status", "createdAt"},
        )
        payload = json.dumps(data, ensure_ascii=False).lower()
        self.assertNotIn(str(upload_dir).lower(), payload)
        self.assertNotIn("local_path", payload)
        self.assertNotIn("localpath", payload)
        self.assertNotIn("object_key", payload)
        self.assertNotIn("objectkey", payload)
        self.assertNotIn("sha256", payload)
        self.assertNotIn("secret", payload)

    def test_ai_asset_status_action_updates_manifest_and_records_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            manifest = upload_dir / "manifest.jsonl"
            db_path = upload_dir / "admin-test.db"
            repo = AIAssetRepository(manifest)
            repo.upsert(
                {
                    "asset_id": "asset-review-1",
                    "kind": "product_image",
                    "category": "轻食健康餐",
                    "style_id": "style-1",
                    "product_name": "牛油果鸡胸沙拉",
                    "quality_score": 0.28,
                    "quality_status": "failed",
                    "quality_reasons": ["solid_or_placeholder"],
                    "object_key": "ai-assets/secret-folder/asset-review-1.jpg",
                    "local_path": str(upload_dir / "secret-token.jpg"),
                    "sha256": "b" * 64,
                    "created_at": "2026-06-28T00:00:00Z",
                    "status": "rejected",
                }
            )

            client = make_app(upload_dir, ai_asset_manifest_path=manifest, db_path=db_path).test_client()
            response = client.post(
                "/api/admin/actions/ai-assets/asset-review-1/status",
                json={
                    "status": "approved",
                    "qualityNote": "人工复核通过",
                    "actorUserId": "admin-1",
                },
            )

            stored = AIAssetRepository(manifest).get("asset-review-1")
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            audit = conn.execute(
                "SELECT * FROM admin_audit_logs WHERE target_type = ? AND target_id = ?",
                ("ai_asset", "asset-review-1"),
            ).fetchone()
            conn.close()

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["asset"]["assetId"], "asset-review-1")
        self.assertEqual(data["asset"]["status"], "approved")
        self.assertEqual(data["asset"]["qualityReasons"], ["solid_or_placeholder", "人工复核通过"])
        self.assertEqual(stored["status"], "approved")
        self.assertEqual(stored["quality_status"], "manual_approved")
        self.assertEqual(stored["quality_reasons"], ["solid_or_placeholder", "人工复核通过"])
        self.assertIsNotNone(audit)
        self.assertEqual(audit["actor_user_id"], "admin-1")
        self.assertEqual(audit["action"], "ai_asset_status_updated")
        self.assertEqual(audit["reason"], "人工复核通过")
        metadata = json.loads(audit["metadata_json"])
        self.assertEqual(metadata["fromStatus"], "rejected")
        self.assertEqual(metadata["toStatus"], "approved")
        self.assertEqual(metadata["qualityNote"], "人工复核通过")
        payload = json.dumps(data, ensure_ascii=False).lower()
        self.assertNotIn(str(upload_dir).lower(), payload)
        self.assertNotIn("object_key", payload)
        self.assertNotIn("objectkey", payload)
        self.assertNotIn("sha256", payload)
        self.assertNotIn("secret", payload)

    def test_ai_asset_status_action_rejects_invalid_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            manifest = upload_dir / "manifest.jsonl"
            AIAssetRepository(manifest).upsert(
                {
                    "asset_id": "asset-review-2",
                    "kind": "product_image",
                    "category": "轻食健康餐",
                    "style_id": "style-1",
                    "product_name": "牛油果鸡胸沙拉",
                    "quality_score": 0.8,
                    "object_key": "ai-assets/products/asset-review-2.jpg",
                    "local_path": str(upload_dir / "asset-review-2.jpg"),
                    "sha256": "c" * 64,
                    "created_at": "2026-06-28T00:00:00Z",
                    "status": "approved",
                }
            )

            client = make_app(upload_dir, ai_asset_manifest_path=manifest).test_client()
            response = client.post(
                "/api/admin/actions/ai-assets/asset-review-2/status",
                json={"status": "archived"},
            )

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "invalid_ai_asset_status_action")


if __name__ == "__main__":
    unittest.main()
