from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def app_module(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "APP_ENV",
        "RENDER",
        "RENDER_SERVICE_ID",
        "RENDER_EXTERNAL_URL",
        "PUBLIC_BASE_URL",
        "DATABASE_URL",
        "REDIS_URL",
        "AUTH_SESSION_HASH_SECRET",
        "AUTH_OTP_HASH_SECRET",
        "SMS_PROVIDER",
        "SMS_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    module = importlib.import_module("app")
    module = importlib.reload(module)
    module.app.config.update(TESTING=True)
    return module


def _configure_live_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://db.invalid/app",
    )
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid/0")
    monkeypatch.setenv(
        "AUTH_SESSION_HASH_SECRET",
        "session-test-" + ("s" * 32),
    )
    monkeypatch.setenv(
        "AUTH_OTP_HASH_SECRET",
        "otp-test-" + ("o" * 32),
    )
    monkeypatch.setenv("SMS_PROVIDER", "webhook")
    monkeypatch.setenv(
        "SMS_WEBHOOK_URL",
        "https://sms.invalid.test/send",
    )


def test_local_auth_readiness_is_explicit_demo_mode(app_module) -> None:
    readiness = app_module.product_auth_readiness()

    assert readiness["ready"] is True
    assert readiness["mode"] == "sqlite_local_demo"
    assert readiness["liveRequired"] is False
    assert readiness["blockingIssues"] == []
    assert "sqlite_auth_is_for_local_demo_only" in readiness["warnings"]


def test_live_auth_missing_config_fails_before_network_probe(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setattr(
        app_module,
        "probe_product_auth_postgres",
        lambda: pytest.fail("missing config must stop PostgreSQL probe"),
    )
    monkeypatch.setattr(
        app_module,
        "probe_product_auth_redis",
        lambda: pytest.fail("missing config must stop Redis probe"),
    )

    readiness = app_module.product_auth_readiness()

    assert readiness["ready"] is False
    assert "auth_postgres_database_required" in readiness["blockingIssues"]
    assert "auth_redis_required" in readiness["blockingIssues"]
    assert "auth_session_hash_secret_required" in readiness["blockingIssues"]
    assert "auth_otp_hash_secret_required" in readiness["blockingIssues"]
    assert "auth_sms_provider_required" in readiness["blockingIssues"]
    assert {
        "DATABASE_URL",
        "REDIS_URL",
        "AUTH_SESSION_HASH_SECRET",
        "AUTH_OTP_HASH_SECRET",
        "SMS_PROVIDER",
    }.issubset(set(readiness["missingConfig"]))


def test_live_auth_readiness_requires_reachable_schema_and_redis(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_live_auth(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "probe_product_auth_postgres",
        lambda: {
            "configured": True,
            "reachable": True,
            "schemaReady": True,
            "missingTables": [],
            "error": "",
        },
    )
    monkeypatch.setattr(
        app_module,
        "probe_product_auth_redis",
        lambda: {
            "configured": True,
            "reachable": True,
            "error": "",
        },
    )

    readiness = app_module.product_auth_readiness()

    assert readiness["ready"] is True
    assert readiness["mode"] == "postgres_redis"
    assert readiness["postgres"]["schemaReady"] is True
    assert readiness["redis"]["reachable"] is True


def test_live_auth_readiness_reports_incomplete_schema(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_live_auth(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "probe_product_auth_postgres",
        lambda: {
            "configured": True,
            "reachable": True,
            "schemaReady": False,
            "missingTables": ["product_auth_sessions"],
            "error": "auth_postgres_schema_incomplete",
        },
    )
    monkeypatch.setattr(
        app_module,
        "probe_product_auth_redis",
        lambda: {
            "configured": True,
            "reachable": True,
            "error": "",
        },
    )

    readiness = app_module.product_auth_readiness()

    assert readiness["ready"] is False
    assert "auth_postgres_schema_incomplete" in readiness["blockingIssues"]
    assert readiness["postgres"]["missingTables"] == [
        "product_auth_sessions"
    ]
