from __future__ import annotations

import importlib
from typing import Any

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
        "PRODUCT_POSTGRES_ENABLED",
        "REDIS_URL",
        "REDIS_PRODUCT_QUEUE",
        "GROWTH_EVENT_WORKER_ENABLED",
        "GROWTH_EVENT_WORKER_SERVICE_ID",
        "GROWTH_INVITE_CODE_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    module = importlib.import_module("app")
    module = importlib.reload(module)
    module.app.config.update(TESTING=True)
    return module


class HeartbeatQueue:
    def __init__(self, heartbeat: dict[str, Any] | None) -> None:
        self.heartbeat = heartbeat
        self.service_ids: list[str] = []

    def service_liveness(self, service_id: str):
        self.service_ids.append(service_id)
        return self.heartbeat


def configure_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://db.invalid/waimai",
    )
    monkeypatch.setenv("PRODUCT_POSTGRES_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid/0")
    monkeypatch.setenv("GROWTH_EVENT_WORKER_ENABLED", "true")
    monkeypatch.setenv(
        "GROWTH_INVITE_CODE_SECRET",
        "test-growth-invite-secret-" + ("x" * 32),
    )


def test_local_growth_readiness_is_explicit_demo_mode(app_module) -> None:
    readiness = app_module.product_growth_readiness()

    assert readiness["ready"] is True
    assert readiness["mode"] == "sqlite_local_demo"
    assert readiness["blockingIssues"] == []
    assert "sqlite_growth_is_for_local_demo_only" in readiness["warnings"]


def test_live_growth_readiness_fails_closed_on_missing_runtime(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    readiness = app_module.product_growth_readiness()

    assert readiness["ready"] is False
    assert "growth_postgres_database_required" in readiness["blockingIssues"]
    assert "growth_worker_redis_required" in readiness["blockingIssues"]
    assert "growth_event_worker_service_required" in readiness[
        "blockingIssues"
    ]
    assert "growth_event_worker_not_live" in readiness["blockingIssues"]
    assert "growth_invite_code_secret_required" in readiness[
        "blockingIssues"
    ]


def test_live_growth_readiness_requires_real_ttl_heartbeat(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(monkeypatch)
    queue = HeartbeatQueue(
        {
            "queueName": "product-generate",
            "ageMs": 12,
            "ttlSeconds": 30,
        }
    )
    monkeypatch.setattr(
        app_module,
        "redis_product_queue_from_env",
        lambda: queue,
    )

    readiness = app_module.product_growth_readiness()

    assert readiness["ready"] is True
    assert readiness["mode"] == "postgres_worker"
    assert queue.service_ids == ["growth-event-worker"]
    assert readiness["workerLiveness"]["ready"] is True


def test_live_growth_readiness_rejects_expired_heartbeat(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_live(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "redis_product_queue_from_env",
        lambda: HeartbeatQueue(None),
    )

    readiness = app_module.product_growth_readiness()

    assert readiness["ready"] is False
    assert "growth_event_worker_not_live" in readiness["blockingIssues"]
    assert (
        readiness["workerLiveness"]["reason"]
        == "heartbeat_missing_or_expired"
    )


def test_ops_readiness_exposes_growth_status(
    app_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    growth = {
        "ready": False,
        "blockingIssues": ["growth_event_worker_not_live"],
    }
    monkeypatch.setattr(
        app_module,
        "product_growth_readiness",
        lambda: growth,
    )
    for name in (
        "generation_provider_readiness",
        "product_generation_readiness",
        "image_refinement_readiness",
        "product_auth_readiness",
    ):
        monkeypatch.setattr(app_module, name, lambda: {"ready": True})
    monkeypatch.setattr(
        app_module.object_storage_service,
        "assess_object_storage_readiness",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        app_module.payment_service,
        "assess_payment_provider_readiness",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        app_module.generation_queue,
        "snapshot",
        lambda: {"closed": False},
    )

    response = app_module.app.test_client().get("/api/ops/readiness")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ready"] is False
    assert body["productGrowth"] == growth
