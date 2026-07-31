from __future__ import annotations

import base64

import app as app_module


def _basic_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )
    return {"Authorization": f"Basic {token}"}


def _configure_staging(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging-demo")
    monkeypatch.setenv("STAGING_BASIC_AUTH_USER", "staging-user")
    monkeypatch.setenv("STAGING_BASIC_AUTH_PASSWORD", "staging-password")
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_AUTH", "true")
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_BILLING", "true")
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_GENERATION", "true")
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_OBJECTS", "true")
    monkeypatch.delenv("PRODUCT_POSTGRES_ENABLED", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BILLING_API_TOKEN", raising=False)
    monkeypatch.delenv("GENERATION_API_TOKEN", raising=False)
    monkeypatch.delenv("OBJECT_API_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)


def test_staging_gate_challenges_missing_or_invalid_credentials(
    monkeypatch,
) -> None:
    _configure_staging(monkeypatch)
    client = app_module.app.test_client()

    missing = client.get("/", environ_base={"REMOTE_ADDR": "198.51.100.8"})
    invalid = client.get(
        "/",
        headers=_basic_header("staging-user", "wrong"),
        environ_base={"REMOTE_ADDR": "198.51.100.8"},
    )

    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == (
        'Basic realm="Waimai Image Tool Staging"'
    )
    assert invalid.status_code == 401


def test_staging_credentials_enable_customer_demo_boundaries(monkeypatch) -> None:
    _configure_staging(monkeypatch)
    client = app_module.app.test_client()
    headers = _basic_header("staging-user", "staging-password")

    page = client.get(
        "/",
        headers=headers,
        environ_base={"REMOTE_ADDR": "198.51.100.8"},
    )
    account = client.get(
        "/api/account",
        headers=headers,
        environ_base={"REMOTE_ADDR": "198.51.100.8"},
    )

    assert page.status_code == 200
    assert account.status_code == 200
    with app_module.app.test_request_context(
        "/",
        headers=headers,
        environ_base={"REMOTE_ADDR": "198.51.100.8"},
    ):
        assert app_module.staging_test_access_allowed() is True
        assert app_module.local_demo_auth_allowed() is True
        assert app_module.local_demo_billing_allowed("default") is True
        assert app_module.local_demo_generation_allowed() is True
        assert app_module.object_write_authorized("default") is True


def test_staging_credentials_never_enable_production_demo(monkeypatch) -> None:
    _configure_staging(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    headers = _basic_header("staging-user", "staging-password")

    with app_module.app.test_request_context(
        "/",
        headers=headers,
        environ_base={"REMOTE_ADDR": "198.51.100.8"},
    ):
        assert app_module.staging_test_access_configured() is False
        assert app_module.staging_test_access_allowed() is False
        assert app_module.local_demo_auth_allowed() is False
        assert app_module.local_demo_generation_allowed() is False
        assert app_module.object_write_authorized("default") is False
