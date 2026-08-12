from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = ROOT / "render.yaml"
BLUEPRINT_SOURCE = BLUEPRINT_PATH.read_text(encoding="utf-8")
BLUEPRINT = yaml.safe_load(BLUEPRINT_SOURCE)

CUSTOMER_WEB = "waimai-image-tool"
API_SERVER = "waimai-image-tool-api"
PROMPT_WORKER = "waimai-image-tool-prompt-worker"
OUTBOX_DISPATCHER = "waimai-image-tool-outbox-dispatcher"
PRODUCT_WORKER = "waimai-image-tool-product-worker"
REVISION_WORKER = "waimai-image-tool-revision-worker"
GROWTH_EVENT_WORKER = "waimai-image-tool-growth-event-worker"
SETTLEMENT_RECONCILER = "waimai-image-tool-settlement-reconciler"
REDIS_SERVICE = "waimai-image-tool-redis"
POSTGRES_DATABASE = "waimai-image-tool-postgres"


def _services() -> dict[str, dict[str, Any]]:
    services = BLUEPRINT.get("services")
    assert isinstance(services, list)
    result = {str(service["name"]): service for service in services}
    assert len(result) == len(services), "Render service names must be unique"
    return result


def _databases() -> dict[str, dict[str, Any]]:
    databases = BLUEPRINT.get("databases")
    assert isinstance(databases, list)
    result = {str(database["name"]): database for database in databases}
    assert len(result) == len(databases), "Render database names must be unique"
    return result


def _env_entries(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = service.get("envVars", [])
    assert isinstance(entries, list)
    return {
        str(entry["key"]): entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("key")
    }


def test_blueprint_parses_and_preserves_customer_web_entrypoint() -> None:
    assert isinstance(BLUEPRINT, dict)
    customer = _services()[CUSTOMER_WEB]

    assert customer["type"] == "web"
    assert customer["startCommand"].startswith("gunicorn app:app ")
    assert "--chdir api-server" not in customer["startCommand"]
    assert customer["healthCheckPath"] == "/healthz"

    assert (ROOT / "app.py").is_file()
    assert "app = Flask(__name__)" in (ROOT / "app.py").read_text(
        encoding="utf-8"
    )


def test_api_and_workers_use_real_independent_entrypoints() -> None:
    services = _services()
    api = services[API_SERVER]
    outbox = services[OUTBOX_DISPATCHER]
    prompt_worker = services[PROMPT_WORKER]
    worker = services[PRODUCT_WORKER]
    revision_worker = services[REVISION_WORKER]
    growth_worker = services[GROWTH_EVENT_WORKER]
    reconciler = services[SETTLEMENT_RECONCILER]

    assert api["type"] == "web"
    assert api["startCommand"].startswith(
        "gunicorn --chdir api-server app:app "
    )
    assert api["healthCheckPath"] == "/healthz"
    assert api["startCommand"] != services[CUSTOMER_WEB]["startCommand"]

    assert outbox["type"] == "worker"
    assert outbox["startCommand"] == "python -m worker.outbox_dispatcher"
    assert outbox["plan"] != "free"
    assert worker["type"] == "worker"
    assert worker["startCommand"] == "python -m worker.worker"
    assert worker["plan"] != "free"
    assert _env_entries(worker)["WORKER_TASK_MODE"]["value"] == "product"
    assert prompt_worker["type"] == "worker"
    assert prompt_worker["startCommand"] == "python -m worker.prompt_worker"
    assert prompt_worker["plan"] != "free"
    assert revision_worker["type"] == "worker"
    assert revision_worker["startCommand"] == "python -m worker.worker"
    assert revision_worker["plan"] != "free"
    assert _env_entries(revision_worker)["WORKER_TASK_MODE"]["value"] == "revision"
    assert _env_entries(revision_worker)["WORKER_CONCURRENCY"]["value"] == "10"
    assert growth_worker["type"] == "worker"
    assert growth_worker["startCommand"] == "python -m worker.growth_worker"
    assert growth_worker["plan"] != "free"
    assert reconciler["type"] == "worker"
    assert reconciler["startCommand"] == (
        "python -m worker.product_settlement_reconciler"
    )
    assert reconciler["plan"] != "free"

    for relative_path in (
        "api-server/app.py",
        "worker/outbox_dispatcher.py",
        "worker/prompt_worker.py",
        "worker/worker.py",
        "worker/growth_worker.py",
        "worker/product_settlement_reconciler.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "if __name__ == \"__main__\":" in source or relative_path == (
            "api-server/app.py"
        )


def test_all_active_compute_services_share_explicit_redis_contract() -> None:
    services = _services()
    for service_name in (
        CUSTOMER_WEB,
        API_SERVER,
        PROMPT_WORKER,
        OUTBOX_DISPATCHER,
        PRODUCT_WORKER,
        REVISION_WORKER,
        GROWTH_EVENT_WORKER,
        SETTLEMENT_RECONCILER,
    ):
        redis_env = _env_entries(services[service_name])["REDIS_URL"]
        assert "value" not in redis_env
        assert redis_env["fromService"] == {
            "type": "keyvalue",
            "name": REDIS_SERVICE,
            "property": "connectionString",
        }

    for service_name in (
        CUSTOMER_WEB,
        OUTBOX_DISPATCHER,
        PRODUCT_WORKER,
        REVISION_WORKER,
        GROWTH_EVENT_WORKER,
        SETTLEMENT_RECONCILER,
    ):
        database_env = _env_entries(services[service_name])["DATABASE_URL"]
        assert "value" not in database_env
        assert database_env["fromDatabase"] == {
            "name": POSTGRES_DATABASE,
            "property": "connectionString",
        }

    assert "DATABASE_URL" not in _env_entries(services[API_SERVER])
    assert "DATABASE_URL" not in _env_entries(services[PROMPT_WORKER])


def test_generation_capacity_and_dual_provider_contracts_are_explicit() -> None:
    groups = {
        str(group["name"]): group
        for group in BLUEPRINT["envVarGroups"]
    }
    core = _env_entries(groups["waimai-image-tool-core"])
    product = _env_entries(groups["waimai-image-tool-product-runtime"])
    services = _services()
    prompt = _env_entries(services[PROMPT_WORKER])
    revision = _env_entries(services[REVISION_WORKER])

    assert core["FINAL_GENERATION_WORKERS"]["value"] == "10"
    assert core["TENCENT_TOKENHUB_MAX_CONCURRENCY"]["value"] == "10"
    assert core["GENERATION_TARGET_BATCH_IMAGES"]["value"] == "100"
    assert core["GENERATION_TARGET_SECONDS"]["value"] == "3600"
    assert core["REDIS_REVISION_QUEUE"]["value"] == "product-revision"
    assert core["TENCENT_TOKENHUB_VERIFIED_CONCURRENCY"]["value"] == "0"
    assert core["TENCENT_TOKENHUB_MEASURED_P95_SECONDS"]["value"] == "0"
    assert core["GENERATION_TARGET_BATCH_OBSERVED_SECONDS"]["value"] == "0"

    assert product["TENCENT_HUNYUAN_SYNC_LIMIT"]["value"] == "120"
    assert product["EXACT_BACKGROUND_CHROMA_FAST_PATH"]["value"] == "true"
    assert product["EXACT_BACKGROUND_CLOUD_MASK_FALLBACK"]["value"] == "false"
    assert product["TENCENT_MASK_VERIFIED_CONCURRENCY"]["value"] == "0"
    assert product["TENCENT_TOKENHUB_IMAGE_MODEL"]["value"] == "hy-image-v3"
    assert product["TENCENT_TOKENHUB_PROTOCOL"]["value"] == "wand-sync-v1"
    assert product["REVISION_WORKER_ENABLED"]["value"] == "true"
    assert product["REVISION_WORKER_SERVICE_ID"]["value"] == "revision-worker"
    assert product["GEMINI_IMAGE_EDIT_MODEL"]["value"] == "gemini-3.1-flash-image"
    assert product["GEMINI_INTERACTIONS_URL"]["value"] == (
        "https://generativelanguage.googleapis.com/v1beta/interactions"
    )

    assert prompt["TENCENT_TOKENHUB_IMAGE_MODEL"]["value"] == "hy-image-v3"
    assert prompt["TENCENT_TOKENHUB_PROTOCOL"]["value"] == "wand-sync-v1"
    assert prompt["WORKER_CONCURRENCY"]["value"] == "10"
    assert revision["WORKER_CONCURRENCY"]["value"] == "10"
    assert revision["GEMINI_API_KEY"]["fromService"]["name"] == CUSTOMER_WEB


def test_growth_event_worker_uses_durable_runtime_and_real_ttl_heartbeat() -> None:
    service = _services()[GROWTH_EVENT_WORKER]
    env = _env_entries(service)

    assert service["autoDeployTrigger"] == "off"
    assert service["maxShutdownDelaySeconds"] == 60
    assert env["DATABASE_URL"]["fromDatabase"] == {
        "name": POSTGRES_DATABASE,
        "property": "connectionString",
    }
    assert env["REDIS_URL"]["fromService"] == {
        "type": "keyvalue",
        "name": REDIS_SERVICE,
        "property": "connectionString",
    }
    assert env["GROWTH_WORKER_ID"]["value"] == "growth-event-worker"
    assert env["GROWTH_WORKER_BATCH_SIZE"]["value"] == "10"
    assert env["GROWTH_WORKER_LEASE_SECONDS"]["value"] == "60"
    assert env["GROWTH_WORKER_RETRY_SECONDS"]["value"] == "30"
    assert env["GROWTH_WORKER_HEARTBEAT_TTL_SECONDS"]["value"] == "30"
    product_runtime = next(
        group
        for group in BLUEPRINT["envVarGroups"]
        if group["name"] == "waimai-image-tool-product-runtime"
    )
    shared_env = _env_entries(product_runtime)
    assert shared_env["GROWTH_EVENT_WORKER_ENABLED"]["value"] == "true"
    assert (
        shared_env["GROWTH_EVENT_WORKER_SERVICE_ID"]["value"]
        == "growth-event-worker"
    )
    assert shared_env["PRODUCT_GROWTH_TENANT_ID"]["value"] == "waimai"


def test_secrets_are_generated_referenced_or_dashboard_managed() -> None:
    services = _services()
    secret_keys = {
        "OBJECT_SIGNING_SECRET",
        "DOWNLOAD_SIGNING_SECRET",
        "ADMIN_API_TOKEN",
        "SMS_WEBHOOK_TOKEN",
        "AUTH_SESSION_HASH_SECRET",
        "AUTH_OTP_HASH_SECRET",
        "GROWTH_INVITE_CODE_SECRET",
        "PAYMENT_WEBHOOK_SECRET",
        "ALIPAY_PRIVATE_KEY",
        "TENCENT_TOKENHUB_API_KEY",
        "TENCENTCLOUD_SECRET_ID",
        "TENCENTCLOUD_SECRET_KEY",
        "GEMINI_API_KEY",
    }

    found: set[str] = set()
    for service in services.values():
        for key, entry in _env_entries(service).items():
            if key not in secret_keys:
                continue
            found.add(key)
            assert "value" not in entry, f"{key} must not be hardcoded"
            assert (
                entry.get("sync") is False
                or entry.get("generateValue") is True
                or isinstance(entry.get("fromService"), dict)
            ), f"{key} must use a Render secret/reference contract"

    assert found == secret_keys
    assert "-----BEGIN PRIVATE KEY-----" not in BLUEPRINT_SOURCE
    assert "postgresql://" not in BLUEPRINT_SOURCE
    assert "redis://" not in BLUEPRINT_SOURCE


def test_persistent_service_boundaries_are_explicit() -> None:
    services = _services()
    redis = services[REDIS_SERVICE]
    postgres = _databases()[POSTGRES_DATABASE]

    assert redis["type"] == "keyvalue"
    assert redis["plan"] != "free"
    assert redis["ipAllowList"] == []
    assert redis["maxmemoryPolicy"] == "noeviction"
    assert redis["persistenceMode"] == "journal-snapshot"

    assert postgres["plan"] != "free"
    assert postgres["connectionPool"] == "none"
    assert postgres["ipAllowList"] == []

    for name, service in services.items():
        if service["type"] in {"web", "worker"}:
            assert "disk" not in service, (
                f"{name} must not imply that local filesystem state is durable"
            )
            assert service["autoDeployTrigger"] == "off"

    assert BLUEPRINT["previews"]["generation"] == "off"


def test_settlement_reconciler_is_an_active_fenced_service() -> None:
    services = _services()

    service = services[SETTLEMENT_RECONCILER]
    assert (
        ROOT / "worker/product_settlement_reconciler.py"
    ).is_file()
    assert service["autoDeployTrigger"] == "off"
    assert service["maxShutdownDelaySeconds"] == 60
    assert _env_entries(service)["DATABASE_URL"]["fromDatabase"] == {
        "name": POSTGRES_DATABASE,
        "property": "connectionString",
    }
    product_group = next(
        group
        for group in BLUEPRINT["envVarGroups"]
        if group["name"] == "waimai-image-tool-product-runtime"
    )
    group_env = {
        entry["key"]: entry.get("value")
        for entry in product_group["envVars"]
    }
    assert group_env["PRODUCT_OUTBOX_DISPATCHER_ENABLED"] == "true"
    assert group_env["PRODUCT_SETTLEMENT_RECONCILER_ENABLED"] == "true"
