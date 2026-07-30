from __future__ import annotations

import importlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

import sms_service


TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
TEST_REDIS_URL = str(os.environ.get("TEST_REDIS_URL") or "").strip()
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "005_product_auth_postgres.sql"
)


class CaptureSmsProvider(sms_service.SmsProvider):
    provider_name = "capture"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send_otp(
        self,
        *,
        phone: str,
        code: str,
        ttl_seconds: int,
        purpose: str = "login",
    ) -> sms_service.SmsSendResult:
        self.sent.append(
            {
                "phone": phone,
                "code": code,
                "ttlSeconds": ttl_seconds,
                "purpose": purpose,
            }
        )
        return sms_service.SmsSendResult(
            provider=self.provider_name,
            status="sent",
            message_id="local-protocol",
        )


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN or not TEST_REDIS_URL,
    reason=(
        "TEST_POSTGRES_DSN and TEST_REDIS_URL are required for the real "
        "auth protocol test"
    ),
)
def test_live_auth_store_and_logout_flow_stay_out_of_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    redis_module = pytest.importorskip("redis")
    namespace = f"waimai:auth:http-{uuid4().hex}"
    phone = f"139{int(uuid4().hex[:8], 16) % 100_000_000:08d}"
    storage_path = tmp_path / "storage.sqlite3"
    session_secret = "auth-session-test-" + ("s" * 32)
    otp_secret = "auth-otp-test-" + ("o" * 32)
    user_id = ""

    with psycopg.connect(TEST_POSTGRES_DSN, autocommit=True) as connection:
        connection.execute(MIGRATION.read_text(encoding="utf-8"))

    redis_client = redis_module.Redis.from_url(TEST_REDIS_URL)
    provider = CaptureSmsProvider()
    env = {
        "APP_ENV": "staging",
        "PRODUCT_POSTGRES_ENABLED": "true",
        "DATABASE_URL": TEST_POSTGRES_DSN,
        "REDIS_URL": TEST_REDIS_URL,
        "AUTH_SESSION_HASH_SECRET": session_secret,
        "AUTH_OTP_HASH_SECRET": otp_secret,
        "AUTH_OTP_REDIS_NAMESPACE": namespace,
        "SMS_PROVIDER": "webhook",
        "SMS_WEBHOOK_URL": "https://sms.invalid.test/send",
        "STORAGE_DB_PATH": str(storage_path),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(
        "ALLOW_SQLITE_PRODUCT_RUNTIME_FOR_TESTS",
        raising=False,
    )

    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)
    monkeypatch.setattr(
        app_module.sms_service,
        "provider_from_env",
        lambda *args, **kwargs: provider,
    )
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    remote = {"REMOTE_ADDR": "203.0.113.21"}

    try:
        requested = client.post(
            "/api/auth/request-otp",
            json={"phone": phone},
            environ_base=remote,
        )
        assert requested.status_code == 200
        requested_payload = requested.get_json()
        assert "mockCode" not in requested_payload
        assert len(provider.sent) == 1

        verified = client.post(
            "/api/auth/verify-otp",
            json={
                "challengeId": requested_payload["challengeId"],
                "code": provider.sent[0]["code"],
            },
            environ_base=remote,
        )
        assert verified.status_code == 200
        verified_payload = verified.get_json()
        user_id = str(verified_payload["user"]["id"])
        raw_token = str(verified_payload["session"]["token"])
        headers = {"Authorization": f"Bearer {raw_token}"}

        assert client.get(
            "/api/auth/session",
            headers=headers,
        ).status_code == 200
        created = client.post(
            "/api/stores",
            json={"name": "协议测试门店"},
            headers=headers,
        )
        assert created.status_code == 200
        listed = client.get("/api/stores", headers=headers)
        assert listed.status_code == 200
        assert [
            item["name"] for item in listed.get_json()["stores"]
        ] == ["协议测试门店"]

        logged_out = client.post("/api/auth/logout", headers=headers)
        assert logged_out.status_code == 200
        assert logged_out.get_json()["loggedOut"] is True
        assert client.get(
            "/api/auth/session",
            headers=headers,
        ).status_code == 401

        with psycopg.connect(TEST_POSTGRES_DSN) as connection:
            row = connection.execute(
                """
                SELECT
                    users.phone,
                    sessions.token_hash,
                    sessions.revoked_at IS NOT NULL,
                    stores.name,
                    memberships.role,
                    security.is_new_user,
                    security.ip_hash,
                    security.device_hash
                FROM product_users AS users
                JOIN product_auth_sessions AS sessions
                  ON sessions.user_id = users.id
                JOIN product_registration_security_events AS security
                  ON security.session_id = sessions.id
                JOIN product_user_stores AS memberships
                  ON memberships.user_id = users.id
                JOIN product_stores AS stores
                  ON stores.id = memberships.store_id
                WHERE users.id = %s
                """,
                (user_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == f"+86{phone}"
        assert row[1] != raw_token
        assert len(row[1]) == 64
        assert row[2] is True
        assert row[3:6] == ("协议测试门店", "owner", True)
        assert len(row[6]) == 64
        assert len(row[7]) == 64
        assert row[6] != remote["REMOTE_ADDR"]
        assert "Werkzeug" not in row[7]
        assert storage_path.exists() is False
    finally:
        if user_id:
            with psycopg.connect(
                TEST_POSTGRES_DSN,
                autocommit=True,
            ) as connection:
                connection.execute(
                    "DELETE FROM product_users WHERE id = %s",
                    (user_id,),
                )
        for key in redis_client.scan_iter(match=f"{namespace}:*"):
            redis_client.delete(key)
        redis_client.close()
