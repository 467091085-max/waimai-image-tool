from __future__ import annotations

import importlib
import os
import random
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import payment_service
from shared.product_auth_store import ProductAuthStore


TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
AUTH_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "005_product_auth_postgres.sql"
)


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for real PostgreSQL protocol tests",
)
def test_live_alipay_order_and_replayed_callback_stay_out_of_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    env = {
        "APP_ENV": "staging",
        "PRODUCT_POSTGRES_ENABLED": "true",
        "DATABASE_URL": TEST_POSTGRES_DSN,
        "PAYMENT_PROVIDER": "alipay",
        "ENABLE_LOCAL_DEMO_BILLING": "false",
        "ALIPAY_APP_ID": "2021000000000000",
        "ALIPAY_PRIVATE_KEY": private_pem,
        "ALIPAY_PUBLIC_KEY": public_pem,
        "PAYMENT_NOTIFY_URL": (
            "https://example.test/api/payments/alipay/notify"
        ),
        "ALIPAY_GATEWAY_URL": "https://example.test/alipay",
        "STORAGE_DB_PATH": str(tmp_path / "storage.sqlite3"),
        "BILLING_DB_PATH": str(tmp_path / "billing.sqlite3"),
        "OBJECT_STORE_DIR": str(tmp_path / "objects"),
        "OBJECT_SIGNING_SECRET": "test-only-object-signing-secret",
        "AUTH_SESSION_HASH_SECRET": "payment-auth-test-" + ("s" * 32),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(
        "ALLOW_SQLITE_PRODUCT_RUNTIME_FOR_TESTS",
        raising=False,
    )

    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    with psycopg.connect(
        TEST_POSTGRES_DSN,
        autocommit=True,
    ) as migration_connection:
        migration_connection.execute(
            AUTH_MIGRATION.read_text(encoding="utf-8")
        )
    phone = "+86139" + "".join(
        str(random.randrange(10)) for _ in range(8)
    )
    with psycopg.connect(
        TEST_POSTGRES_DSN,
        autocommit=False,
    ) as auth_connection:
        auth_store = ProductAuthStore(
            auth_connection,
            token_hash_secret=env["AUTH_SESSION_HASH_SECRET"],
        )
        user_result = auth_store.get_or_create_user(phone=phone)
        issued = auth_store.issue_session(
            user_id=str(user_result.user["id"])
        )
    auth = {
        "user": issued.user,
        "session": {
            **issued.session,
            "token": issued.token,
        },
    }

    client = app_module.app.test_client()
    created = client.post(
        "/api/payments/orders",
        json={"packageId": "starter-500"},
        headers={
            "Authorization": f"Bearer {auth['session']['token']}",
            "Idempotency-Key": f"pg-api-{uuid4().hex}",
        },
    )
    assert created.status_code == 200
    created_payload = created.get_json()
    assert created_payload["instructions"]["paymentUrl"].startswith(
        "https://example.test/alipay?"
    )
    order = created_payload["order"]

    notify = {
        "app_id": env["ALIPAY_APP_ID"],
        "out_trade_no": order["providerOrderId"],
        "trade_no": f"trade-{uuid4().hex}",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "49.00",
        "sign_type": "RSA2",
        "notify_id": f"notify-{uuid4().hex}",
    }
    notify["sign"] = payment_service._alipay_rsa2_sign(  # type: ignore[attr-defined]
        payment_service._alipay_signing_string(notify),  # type: ignore[attr-defined]
        payment_service._load_alipay_private_key(env),  # type: ignore[attr-defined]
    )

    def reject_sqlite_growth(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "live payment must not write SQLite growth state"
        )

    monkeypatch.setattr(
        app_module,
        "apply_payment_growth_rewards",
        reject_sqlite_growth,
    )
    first = client.post("/api/payments/alipay/notify", data=notify)
    replay = client.post("/api/payments/alipay/notify", data=notify)

    assert first.status_code == 200
    assert first.get_data(as_text=True) == "success"
    assert replay.status_code == 200
    assert replay.get_data(as_text=True) == "success"
    with psycopg.connect(TEST_POSTGRES_DSN) as pg_conn:
        with pg_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, credited_points
                FROM product_payment_orders
                WHERE id = %s
                """,
                (order["orderId"],),
            )
            assert cursor.fetchone() == ("paid", 500)
            cursor.execute(
                """
                SELECT balance_points
                FROM product_point_accounts
                WHERE owner_user_id = %s
                """,
                (auth["user"]["id"],),
            )
            assert cursor.fetchone() == (500,)
            cursor.execute(
                """
                SELECT count(*)
                FROM product_payment_events
                WHERE order_id = %s
                """,
                (order["orderId"],),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                """
                SELECT event_type, status, payload->>'orderId'
                FROM product_growth_outbox
                WHERE payload->>'orderId' = %s
                """,
                (order["orderId"],),
            )
            assert cursor.fetchall() == [
                (
                    "growth.first_payment_reward.requested",
                    "pending",
                    order["orderId"],
                )
            ]

    assert not Path(env["STORAGE_DB_PATH"]).exists()
    assert not Path(env["BILLING_DB_PATH"]).exists()
