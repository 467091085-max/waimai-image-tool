from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest


TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = tuple(sorted((ROOT / "migrations").glob("*.sql")))


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the real admin HTTP protocol",
)
def test_live_admin_reads_and_security_actions_use_postgres_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "test" not in TEST_POSTGRES_DSN.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    schema = f"test_admin_http_{uuid4().hex}"
    sqlite_path = tmp_path / "must-not-exist.sqlite3"

    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        def connect_schema() -> Any:
            connection = psycopg.connect(
                TEST_POSTGRES_DSN,
                autocommit=False,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema)
                    )
                )
            connection.commit()
            return connection

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                for migration in MIGRATIONS:
                    cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

        @contextmanager
        def postgres_connection_for_test(
            *_: Any,
            **__: Any,
        ) -> Iterator[Any]:
            current = connect_schema()
            try:
                yield current
            except BaseException:
                current.rollback()
                raise
            finally:
                current.close()

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PRODUCT_POSTGRES_ENABLED", "true")
        monkeypatch.setenv("DATABASE_URL", TEST_POSTGRES_DSN)
        monkeypatch.setenv("ADMIN_API_TOKEN", "admin-http-test-token")
        monkeypatch.setenv("STORAGE_DB_PATH", str(sqlite_path))

        app_module = importlib.import_module("app")
        app_module = importlib.reload(app_module)
        monkeypatch.setattr(
            app_module,
            "postgres_connection",
            postgres_connection_for_test,
        )

        def sqlite_forbidden(*_: Any, **__: Any) -> Any:
            raise AssertionError("live admin path touched SQLite")

        monkeypatch.setattr(
            app_module,
            "product_db_conn",
            sqlite_forbidden,
        )
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        token_headers = {"X-Admin-Token": "admin-http-test-token"}

        missing_key = client.post(
            "/api/admin/actions/risk",
            json={
                "eventType": "registration.check",
                "subjectType": "user",
                "subjectValue": "user-admin-test",
                "decision": "deny",
                "riskLevel": "high",
                "denyReason": "verified abuse",
            },
            headers=token_headers,
        )
        assert missing_key.status_code == 400
        assert missing_key.get_json()["code"] == "idempotency_key_required"

        risk_headers = {
            **token_headers,
            "Idempotency-Key": "risk-admin-1",
        }
        risk_payload = {
            "eventType": "registration.check",
            "subjectType": "user",
            "subjectValue": "user-admin-test",
            "decision": "deny",
            "riskLevel": "high",
            "denyReason": "verified abuse",
            "metadata": {"caseId": "case-1"},
        }
        risk_created = client.post(
            "/api/admin/actions/risk",
            json=risk_payload,
            headers=risk_headers,
        )
        assert risk_created.status_code == 200
        assert risk_created.get_json()["idempotent"] is False
        risk_replay = client.post(
            "/api/admin/actions/risk",
            json=risk_payload,
            headers=risk_headers,
        )
        assert risk_replay.status_code == 200
        assert risk_replay.get_json()["idempotent"] is True
        risk_conflict = client.post(
            "/api/admin/actions/risk",
            json={**risk_payload, "denyReason": "changed"},
            headers=risk_headers,
        )
        assert risk_conflict.status_code == 409
        assert risk_conflict.get_json()["code"] == "admin_action_conflict"

        access_response = client.post(
            "/api/admin/actions/asset-access",
            json={
                "assetId": (
                    "generated/customer-previews/v1/"
                    "owner/menu/background/image-1.jpg"
                ),
                "assetType": "generated-image",
                "action": "preview",
                "userId": "user-admin-test",
                "allowed": True,
            },
            headers={
                **token_headers,
                "Idempotency-Key": "asset-access-admin-1",
            },
        )
        assert access_response.status_code == 200
        assert access_response.get_json()["record"]["allowed"] is True

        audit_response = client.post(
            "/api/admin/actions/audit",
            json={
                "actorUserId": "client-spoof",
                "action": "settings.changed",
                "targetType": "tenant",
                "targetId": "tenant-1",
                "metadata": {"setting": "generation"},
            },
            headers={
                **token_headers,
                "Idempotency-Key": "generic-audit-1",
            },
        )
        assert audit_response.status_code == 200
        assert audit_response.get_json()["audit"]["actorUserId"] == (
            "service:admin-api-token"
        )

        dashboard = client.get(
            "/api/admin/dashboard",
            headers=token_headers,
        )
        assert dashboard.status_code == 200
        dashboard_payload = dashboard.get_json()
        assert dashboard_payload["ok"] is True
        assert dashboard_payload["summary"]["risk"]["total"] == 1
        assert dashboard_payload["summary"]["assetAccess"]["total"] == 1

        risk_list = client.get(
            "/api/admin/lists/risk-events?decision=deny&limit=10",
            headers=token_headers,
        )
        assert risk_list.status_code == 200
        assert risk_list.get_json()["total"] == 1
        assert risk_list.get_json()["items"][0]["userId"] == (
            "user-admin-test"
        )

        access_list = client.get(
            "/api/admin/lists/asset-access?status=allowed&limit=10",
            headers=token_headers,
        )
        assert access_list.status_code == 200
        assert access_list.get_json()["total"] == 1
        assert "/" in access_list.get_json()["items"][0]["assetId"]

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM product_risk_decisions),
                        (SELECT COUNT(*) FROM product_asset_access_events),
                        (SELECT COUNT(*) FROM product_admin_audit_events),
                        (
                            SELECT actor_user_id
                            FROM product_admin_audit_events
                            LIMIT 1
                        )
                    """
                )
                counts = cursor.fetchone()
            connection.rollback()
        finally:
            connection.close()
        assert counts == (1, 1, 1, "service:admin-api-token")
        assert not sqlite_path.exists()

        @contextmanager
        def unavailable_postgres(
            *_: Any,
            **__: Any,
        ) -> Iterator[Any]:
            raise RuntimeError("database unavailable")
            yield

        monkeypatch.setattr(
            app_module,
            "postgres_connection",
            unavailable_postgres,
        )
        failed_dashboard = client.get(
            "/api/admin/dashboard",
            headers=token_headers,
        )
        assert failed_dashboard.status_code == 503
        assert failed_dashboard.get_json() == {
            "ok": False,
            "code": "postgres_admin_dashboard_unavailable",
            "error": "Admin dashboard unavailable.",
        }
        failed_list = client.get(
            "/api/admin/lists/users",
            headers=token_headers,
        )
        assert failed_list.status_code == 503
        assert failed_list.get_json()["ok"] is False
        assert failed_list.get_json()["code"] == (
            "postgres_admin_list_unavailable"
        )
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        admin.close()
