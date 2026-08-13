from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import app as app_module
from shared.batch_contract import BatchContractError
from shared.product_generation_settlement import (
    GenerationCompletion,
    InvalidProductTask,
)
from shared.refinement_contract import freeze_revision_batch_contract
from shared.redis_queue import TaskNotFound


JOB_ID = "job-postgres-1"
USER_ID = "user-postgres-1"
REQUEST_SHA256 = "a" * 64
MANIFEST_SHA256 = "b" * 64
REVISION_JOB_ID = "revision-postgres-1"
TEST_ATTESTATION_SECRET = "revision-postgres-signing-secret-32-bytes-minimum"


@pytest.fixture(autouse=True)
def _revision_attestation_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OBJECT_SIGNING_SECRET",
        TEST_ATTESTATION_SECRET,
    )


def generation_contract() -> dict[str, Any]:
    return {
        "jobType": "menu_batch_generation",
        "jobId": JOB_ID,
        "userId": USER_ID,
        "menuUploadId": "menu_" + ("c" * 32),
        "menu": {
            "objectKey": "menus/menu.xlsx",
            "sha256": "d" * 64,
            "parserVersion": "test",
        },
        "selectedBackground": {
            "objectKey": "backgrounds/background.png",
            "sha256": "e" * 64,
            "styleId": "style-1",
        },
        "quality": {
            "id": "standard",
            "pointsPerImage": 10,
        },
        "billing": {
            "imageCount": 3,
            "totalPoints": 30,
            "debitOrderId": "debit-job-postgres-1",
            "refundOrderId": "refund-job-postgres-1",
            "pricingVersion": "test",
        },
        "idempotency": {
            "key": JOB_ID,
            "requestSha256": REQUEST_SHA256,
        },
    }


def principal() -> dict[str, Any]:
    return {
        "userId": USER_ID,
        "internal": False,
        "localDemo": False,
    }


def revision_contract(*, free: bool = False) -> dict[str, Any]:
    return freeze_revision_batch_contract(
        job_id=REVISION_JOB_ID,
        parent_generation_job_id=JOB_ID,
        user_id=USER_ID,
        source_delivery_asset={
            "assetId": "asset-source-1",
            "objectKey": "generated/source-1.png",
            "sha256": "f" * 64,
            "rowNumber": 1,
            "dishName": "辣椒炒肉",
        },
        selected_background={
            "assetId": "background-1",
            "objectKey": "backgrounds/background-1.png",
            "sha256": "e" * 64,
        },
        quality="standard",
        mode="rework",
        refine_prompt=None,
        idempotency_key="revision-idempotency-1",
        free_rework_quota_verified=free,
        created_at="2026-07-29T12:00:00Z",
    )


def parent_compatibility_record() -> dict[str, Any]:
    return {
        "id": JOB_ID,
        "status": "succeeded",
        "request": generation_contract(),
        "menu_upload_id": "menu_" + ("c" * 32),
        "menu_object_ref": "menus/menu.xlsx",
        "menu_object_sha256": "d" * 64,
        "style_id": "style-1",
    }


@contextmanager
def fake_postgres_connection():
    yield object()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    app_module.app.config.update(TESTING=True)
    monkeypatch.setattr(
        app_module,
        "generation_request_principal",
        lambda: (principal(), None),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_product_runtime_enabled",
        lambda: True,
    )
    return app_module.app.test_client()


def test_postgres_manifest_context_requires_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={
                "status": "running",
                "request_payload": generation_contract(),
                "request_sha256": REQUEST_SHA256,
            }
        ),
    )

    with pytest.raises(app_module.MenuUploadError) as error:
        app_module.load_postgres_generation_manifest_context(
            JOB_ID,
            principal(),
        )

    assert error.value.code == "generation_manifest_not_ready"
    assert error.value.status == 425


def test_postgres_manifest_context_verifies_private_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = generation_contract()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={
                "status": "succeeded",
                "request_payload": contract,
                "request_sha256": REQUEST_SHA256,
                "manifest_ref": (
                    f"generated/manifests/{JOB_ID}/{REQUEST_SHA256}.json"
                ),
                "manifest_sha256": MANIFEST_SHA256,
            }
        ),
    )

    def load_document(
        manifest: dict[str, Any],
        frozen_contract: dict[str, Any],
    ) -> dict[str, Any]:
        captured["manifest"] = manifest
        captured["contract"] = frozen_contract
        return {"generation": {"succeeded": 3}}

    monkeypatch.setattr(
        app_module,
        "load_generation_result_manifest_document",
        load_document,
    )

    document, resolved_contract = (
        app_module.load_postgres_generation_manifest_context(
            JOB_ID,
            principal(),
        )
    )

    assert document == {"generation": {"succeeded": 3}}
    assert resolved_contract is contract
    assert captured["manifest"] == {
        "objectKey": (
            f"generated/manifests/{JOB_ID}/{REQUEST_SHA256}.json"
        ),
        "sha256": MANIFEST_SHA256,
        "requestSha256": REQUEST_SHA256,
    }


def test_terminal_redis_task_completes_postgres_with_fence_and_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[dict[str, Any]] = []

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

    monkeypatch.setattr(
        app_module,
        "completion_from_redis_task",
        lambda _task: GenerationCompletion(
            job_id=JOB_ID,
            owner_user_id=USER_ID,
            request_sha256=REQUEST_SHA256,
            fence=7,
            terminal_status="succeeded",
            requested_count=3,
            completed_count=2,
            failed_count=1,
            refund_target_points=10,
            manifest_ref=(
                f"generated/manifests/{JOB_ID}/{REQUEST_SHA256}.json"
            ),
            manifest_sha256=MANIFEST_SHA256,
            error_message="",
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)
    monkeypatch.setattr(
        app_module,
        "apply_generation_completion",
        lambda _store, value, **kwargs: applied.append(
            {
                "completion": value,
                **kwargs,
            }
        ),
    )

    app_module.settle_postgres_generation_task(
        {"task_id": JOB_ID},
        principal(),
    )

    assert len(applied) == 1
    assert applied[0]["completion"].fence == 7
    assert applied[0]["completion"].refund_target_points == 10
    assert applied[0]["reconciler_id"] == "web-status-reconciler"


def test_invalid_terminal_task_is_mapped_to_batch_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "completion_from_redis_task",
        lambda _task: (_ for _ in ()).throw(
            InvalidProductTask("task.payload._productJobFence is required")
        ),
    )

    with pytest.raises(BatchContractError) as error:
        app_module.settle_postgres_generation_task(
            {"task_id": JOB_ID},
            principal(),
        )

    assert error.value.code == "generation_task_contract_mismatch"


def test_manifest_route_uses_postgres_and_never_sqlite(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "product_redis_queue", lambda: None)
    monkeypatch.setattr(
        app_module,
        "load_postgres_generation_manifest_context",
        lambda _job_id, _principal: (
            {"generation": {"succeeded": 3}, "results": []},
            generation_contract(),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "load_generation_result_manifest",
        lambda *_args, **_kwargs: pytest.fail(
            "PostgreSQL route must not fall back to SQLite"
        ),
    )

    response = client.get(f"/api/generation-jobs/{JOB_ID}/manifest")

    assert response.status_code == 200
    assert response.get_json()["generation"]["succeeded"] == 3


def test_asset_route_uses_postgres_and_never_sqlite(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = "asset_" + ("f" * 32)
    monkeypatch.setattr(
        app_module,
        "load_postgres_generation_manifest_context",
        lambda _job_id, _principal: (
            {"deliveryAssets": [{"assetId": asset_id}]},
            generation_contract(),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "load_persisted_generation_manifest_document",
        lambda *_args, **_kwargs: pytest.fail(
            "PostgreSQL route must not fall back to SQLite"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "generation_delivery_asset",
        lambda _document, _asset_id: (
            "generated/delivery/test.png",
            b"png-bytes",
        ),
    )

    response = client.get(
        f"/api/generation-jobs/{JOB_ID}/assets/{asset_id}"
    )

    assert response.status_code == 200
    assert response.data == b"png-bytes"


def test_cancel_route_records_intent_without_web_refund(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def request_cancel(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(("cancel", kwargs))
            return SimpleNamespace(
                job={
                    "id": JOB_ID,
                    "status": "canceled",
                }
            )

    monkeypatch.setattr(app_module, "product_redis_queue", lambda: None)
    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "queued"}
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)
    monkeypatch.setattr(
        app_module,
        "postgres_apply_generation_settlement",
        lambda job_id, owner_user_id: calls.append(
            ("settle", (job_id, owner_user_id))
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_generation_job_payload",
        lambda _job_id, _principal: {
            "jobId": JOB_ID,
            "status": "canceled",
        },
    )

    response = client.post(
        f"/api/generation-jobs/{JOB_ID}/cancel"
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "canceled"
    assert calls == [
        (
            "cancel",
            {
                "job_id": JOB_ID,
                "owner_user_id": USER_ID,
            },
        ),
    ]


def test_running_cancel_without_redis_task_fails_closed(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def request_cancel(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                job={
                    "id": JOB_ID,
                    "status": "running",
                    "cancel_requested": True,
                }
            )

    class MissingQueue:
        def get(self, _job_id: str) -> dict[str, Any]:
            raise TaskNotFound("missing")

    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: MissingQueue(),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "running"}
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)

    response = client.post(
        f"/api/generation-jobs/{JOB_ID}/cancel"
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "generation_queue_unavailable"


def test_paid_revision_creation_uses_atomic_postgres_debit_and_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = revision_contract()
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def create_or_get_job_with_debit(
            self,
            **kwargs: Any,
        ) -> SimpleNamespace:
            calls.append(("paid", kwargs))
            return SimpleNamespace(
                job={
                    "id": contract["jobId"],
                    "status": "queued",
                    "created_at": "now",
                },
                created=True,
            )

        def create_or_get_job(self, **_kwargs: Any) -> None:
            pytest.fail("paid revision must use atomic debit creation")

        def get_account(self, **_kwargs: Any) -> dict[str, Any]:
            return {"balance_points": 90}

    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)

    job, account, created = app_module.create_postgres_revision_job(
        contract,
        parent_compatibility_record(),
    )

    assert created is True
    assert job["id"] == REVISION_JOB_ID
    assert account["balance_points"] == 90
    assert calls[0][1]["debit_points"] == 10
    assert calls[0][1]["requested_count"] == 1
    assert calls[0][1]["menu_object_ref"] == "menus/menu.xlsx"
    assert (
        calls[0][1]["request_payload"]["jobType"]
        == "delivery_asset_revision_batch"
    )


def test_free_revision_creates_no_postgres_debit_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = revision_contract(free=True)
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def get_or_create_account(
            self,
            **kwargs: Any,
        ) -> SimpleNamespace:
            calls.append(("account", kwargs))
            return SimpleNamespace(created=False)

        def create_or_get_job(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(("free", kwargs))
            return SimpleNamespace(
                job={"id": contract["jobId"], "status": "queued"},
                created=True,
            )

        def create_or_get_job_with_debit(self, **_kwargs: Any) -> None:
            pytest.fail("free revision must not create a debit order")

        def get_account(self, **_kwargs: Any) -> dict[str, Any]:
            return {"balance_points": 100}

    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)

    _job, account, created = app_module.create_postgres_revision_job(
        contract,
        parent_compatibility_record(),
    )

    assert created is True
    assert account["balance_points"] == 100
    assert [name for name, _payload in calls] == ["account", "free"]
    assert calls[1][1]["debit_points"] == 0


def test_atomic_rework_creation_passes_free_and_paid_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free_contract = revision_contract(free=True)
    paid_contract = revision_contract(free=False)
    captured: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def create_or_get_revision_job_with_quota(
            self,
            **kwargs: Any,
        ) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(
                request_payload=free_contract,
                job={
                    "id": REVISION_JOB_ID,
                    "status": "queued",
                },
                created=True,
            )

        def get_account(self, **_kwargs: Any) -> dict[str, Any]:
            return {"balance_points": 100}

    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)

    selected, job, account, created = (
        app_module.create_postgres_rework_revision_job_with_quota(
            free_contract=free_contract,
            paid_contract=paid_contract,
            parent_record=parent_compatibility_record(),
            free_rework_limit=3,
        )
    )

    assert selected is free_contract
    assert job["id"] == REVISION_JOB_ID
    assert account["balance_points"] == 100
    assert created is True
    assert captured["free_rework_limit"] == 3
    assert captured["free_candidate"].debit_points == 0
    assert captured["paid_candidate"].debit_points == 10
    assert (
        captured["free_candidate"].job_id
        == captured["paid_candidate"].job_id
        == REVISION_JOB_ID
    )


def test_revision_submission_uses_postgres_and_never_sqlite_or_direct_redis(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QueueMustNotPublish:
        def enqueue_idempotent(self, **_kwargs: Any) -> None:
            pytest.fail("Web must not publish a PostgreSQL revision directly")

    monkeypatch.setattr(
        app_module,
        "persisted_revision_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TaskNotFound("missing")
        ),
    )
    monkeypatch.setattr(
        app_module,
        "gemini_image_edit_readiness",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: QueueMustNotPublish(),
    )
    parent = parent_compatibility_record()
    parent["request"]["billing"]["imageCount"] = 1
    monkeypatch.setattr(
        app_module,
        "resolve_parent_delivery_asset_snapshot",
        lambda *_args, **_kwargs: (
            parent,
            {
                "assetId": "asset-source-1",
                "objectKey": "generated/source-1.png",
                "sha256": "f" * 64,
                "rowNumber": 1,
                "dishName": "辣椒炒肉",
            },
            {
                "assetId": "background-1",
                "objectKey": "backgrounds/background-1.png",
                "sha256": "e" * 64,
            },
        ),
    )
    monkeypatch.setattr(
        app_module,
        "revision_free_rework_usage",
        lambda **_kwargs: pytest.fail(
            "PostgreSQL free quota must be decided inside the job transaction"
        ),
    )
    captured: dict[str, Any] = {}

    def create_job_with_quota(
        *,
        free_contract: dict[str, Any],
        paid_contract: dict[str, Any],
        parent_record: dict[str, Any],
        free_rework_limit: int,
    ):
        captured["freeContract"] = free_contract
        captured["paidContract"] = paid_contract
        captured["parent"] = parent_record
        captured["freeReworkLimit"] = free_rework_limit
        return (
            paid_contract,
            {
                "id": paid_contract["jobId"],
                "status": "queued",
                "created_at": "now",
            },
            {"balance_points": 90},
            True,
        )

    monkeypatch.setattr(
        app_module,
        "create_postgres_rework_revision_job_with_quota",
        create_job_with_quota,
    )
    monkeypatch.setattr(
        app_module,
        "ensure_demo_balance",
        lambda *_args, **_kwargs: pytest.fail(
            "PostgreSQL revision must not initialize SQLite demo balance"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "persist_revision_batch_contract",
        lambda *_args, **_kwargs: pytest.fail(
            "PostgreSQL revision must not persist to SQLite"
        ),
    )

    response = client.post(
        "/api/image-refinements",
        json={
            "parentGenerationJobId": JOB_ID,
            "sourceAssetId": "asset-source-1",
            "mode": "rework",
            "idempotencyKey": "revision-route-idem-1",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "queued"
    assert body["transaction"]["points"] == 10
    assert captured["parent"] is parent
    assert captured["freeContract"]["billing"]["totalPoints"] == 0
    assert captured["paidContract"]["billing"]["totalPoints"] == 10
    assert captured["freeReworkLimit"] == 3


@pytest.mark.parametrize(
    "live_env",
    (
        {
            "APP_ENV": "production",
            "PRODUCT_POSTGRES_ENABLED": "true",
            "DATABASE_URL": None,
        },
        {
            "APP_ENV": "staging",
            "PRODUCT_POSTGRES_ENABLED": "false",
            "DATABASE_URL": "postgresql://db.example/app",
        },
        {
            "APP_ENV": "",
            "RENDER_SERVICE_ID": "srv-refinement-test",
            "PRODUCT_POSTGRES_ENABLED": "false",
            "DATABASE_URL": "postgresql://db.example/app",
        },
    ),
    ids=("missing-database-url", "disabled-staging", "disabled-render"),
)
def test_live_revision_submission_fails_closed_without_postgres(
    monkeypatch: pytest.MonkeyPatch,
    live_env: dict[str, str | None],
) -> None:
    app_module.app.config.update(TESTING=True)
    for name in (
        "APP_ENV",
        "RENDER",
        "RENDER_SERVICE_ID",
        "RENDER_EXTERNAL_URL",
        "DATABASE_URL",
        "PRODUCT_POSTGRES_ENABLED",
        "ALLOW_SQLITE_PRODUCT_RUNTIME_FOR_TESTS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in live_env.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        app_module,
        "generation_request_principal",
        lambda: (principal(), None),
    )
    monkeypatch.setattr(
        app_module,
        "product_db_conn",
        lambda: pytest.fail("live revision must not open SQLite"),
    )
    monkeypatch.setattr(
        app_module,
        "gemini_image_edit_readiness",
        lambda: pytest.fail("provider gate must not run before PostgreSQL"),
    )

    response = app_module.app.test_client().post(
        "/api/image-refinements",
        json={
            "parentGenerationJobId": JOB_ID,
            "sourceAssetId": "asset-source-1",
            "mode": "rework",
            "idempotencyKey": "revision-live-fail-closed",
        },
    )

    assert response.status_code == 503
    body = response.get_json()
    assert body["code"] == "postgres_revision_unavailable"


def test_live_readiness_reports_explicitly_disabled_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://db.example/app",
    )
    monkeypatch.setenv("PRODUCT_POSTGRES_ENABLED", "false")

    readiness = app_module.product_generation_readiness()

    assert readiness["ready"] is False
    assert readiness["postgresDatabaseConfigured"] is True
    assert readiness["postgresEnabled"] is False
    assert readiness["postgresConfigured"] is False
    assert (
        "postgres_product_runtime_enabled_required"
        in readiness["blockingIssues"]
    )
    assert "PRODUCT_POSTGRES_ENABLED" in readiness["missingConfig"]


def test_revision_status_is_durable_read_without_redis_or_web_settlement(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("PostgreSQL status must not read Redis"),
    )
    monkeypatch.setattr(
        app_module,
        "settle_postgres_revision_task",
        lambda *_args, **_kwargs: pytest.fail(
            "customer status must not reconcile PostgreSQL"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_revision_job_payload",
        lambda _job_id, _principal: {
            "jobId": REVISION_JOB_ID,
            "status": "running",
        },
    )
    monkeypatch.setattr(
        app_module,
        "revision_redis_task_or_terminal_record",
        lambda *_args, **_kwargs: pytest.fail(
            "PostgreSQL status must not use SQLite recovery"
        ),
    )

    response = client.get(
        f"/api/image-refinements/{REVISION_JOB_ID}"
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "running"


def test_generation_status_is_durable_read_without_redis_or_web_settlement(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("PostgreSQL status must not read Redis"),
    )
    monkeypatch.setattr(
        app_module,
        "settle_postgres_generation_task",
        lambda *_args, **_kwargs: pytest.fail(
            "customer status must not reconcile PostgreSQL"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_generation_job_payload",
        lambda _job_id, _principal: {
            "jobId": JOB_ID,
            "status": "queued",
        },
    )

    response = client.get(f"/api/generation-jobs/{JOB_ID}")

    assert response.status_code == 200
    assert response.get_json()["status"] == "queued"


def test_generation_manifest_is_durable_read_without_redis_or_web_settlement(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("PostgreSQL manifest must not read Redis"),
    )
    monkeypatch.setattr(
        app_module,
        "settle_postgres_generation_task",
        lambda *_args, **_kwargs: pytest.fail(
            "customer manifest must not reconcile PostgreSQL"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "load_postgres_generation_manifest_context",
        lambda _job_id, _principal: (
            {"generation": {"succeeded": 1}, "images": []},
            generation_contract(),
        ),
    )

    response = client.get(f"/api/generation-jobs/{JOB_ID}/manifest")

    assert response.status_code == 200
    assert response.get_json()["generation"]["succeeded"] == 1


def test_export_reads_postgres_manifest_without_sqlite_fallback(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = generation_contract()
    monkeypatch.setattr(
        app_module,
        "load_persisted_generation_manifest_context",
        lambda *_args, **_kwargs: pytest.fail(
            "production export must not read the SQLite manifest"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "load_postgres_generation_export_context",
        lambda _job_id, _principal: (
            {
                "generationBatch": contract,
                "deliveryAssets": [],
            },
            contract,
            {
                "objectKey": "generated/manifests/job-postgres-1/"
                + ("a" * 64)
                + ".json",
                "sha256": "b" * 64,
                "requestSha256": "a" * 64,
            },
        ),
    )
    monkeypatch.setattr(
        app_module,
        "generation_manifest_export_results",
        lambda _document, _staging_dir: [],
    )
    monkeypatch.setattr(
        app_module,
        "generation_contract_export_platforms",
        lambda _contract, _requested: ["meituan"],
    )
    monkeypatch.setattr(
        app_module,
        "generation_contract_export_watermark",
        lambda _contract: {"enabled": False},
    )
    monkeypatch.setattr(
        app_module,
        "postgres_export_generation_context",
        lambda _contract, _principal, **_kwargs: {
            "exportId": "export_test",
            "ownerUserId": "user-postgres-1",
            "idempotencyKey": "export_test_idem",
            "exportRequestSha256": "c" * 64,
        },
    )
    monkeypatch.setattr(
        app_module,
        "postgres_existing_export_payload",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "apply_revision_export_overrides",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "export_delivery_zip",
        lambda *_args, **_kwargs: {
            "download": "/download/export.zip",
            "images": 0,
            "platforms": ["meituan"],
            "watermark": False,
        },
    )
    monkeypatch.setattr(
        app_module,
        "object_storage_export_payload",
        lambda payload, **_kwargs: payload,
    )

    response = client.post(
        "/api/export",
        json={"jobId": JOB_ID, "platforms": ["meituan"]},
    )

    assert response.status_code == 200
    assert response.get_json()["platforms"] == ["meituan"]


def test_postgres_job_payload_builders_do_not_apply_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = generation_contract()
    revision = revision_contract()
    generation_detail = SimpleNamespace(
        job={
            "id": JOB_ID,
            "status": "queued",
            "request_payload": generation,
            "requested_count": 1,
        },
        settlement={"refund_applied_points": 0},
    )
    revision_detail = SimpleNamespace(
        job={
            "id": REVISION_JOB_ID,
            "status": "running",
            "request_payload": revision,
            "requested_count": 1,
        },
        settlement={"refund_applied_points": 0},
    )
    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda _job_id, _principal: generation_detail,
    )
    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda _job_id, _principal: revision_detail,
    )
    monkeypatch.setattr(
        app_module,
        "postgres_account_snapshot",
        lambda _owner: {"balance_points": 123, "updated_at": "now"},
    )
    monkeypatch.setattr(
        app_module,
        "postgres_compatibility_job_record",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        app_module,
        "postgres_apply_generation_settlement",
        lambda *_args, **_kwargs: pytest.fail(
            "public payload construction must be read-only"
        ),
    )

    generation_payload = app_module.postgres_generation_job_payload(
        JOB_ID,
        principal(),
    )
    revision_payload = app_module.postgres_revision_job_payload(
        REVISION_JOB_ID,
        principal(),
    )

    assert generation_payload["status"] == "queued"
    assert generation_payload["account"]["balance"] == 123
    assert revision_payload["status"] == "running"
    assert revision_payload["account"]["balance"] == 123


def test_revision_status_validates_revision_type_before_settlement(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            app_module.RefinementContractError(
                "revision_job_contract_mismatch",
                "not a revision",
                field="jobType",
            )
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_apply_generation_settlement",
        lambda *_args, **_kwargs: pytest.fail(
            "wrong job type must not be settled"
        ),
    )
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("wrong job type must not read Redis"),
    )

    response = client.get(
        f"/api/image-refinements/{REVISION_JOB_ID}"
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "revision_job_contract_mismatch"


def test_terminal_revision_status_does_not_require_redis(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "succeeded"}
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_revision_job_payload",
        lambda _job_id, _principal: {
            "jobId": REVISION_JOB_ID,
            "status": "completed",
        },
    )
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("durable terminal status must not read Redis"),
    )

    response = client.get(
        f"/api/image-refinements/{REVISION_JOB_ID}"
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "completed"


def test_queued_revision_cancel_records_intent_without_web_refund(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def request_cancel(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(("postgres-cancel", kwargs))
            return SimpleNamespace(
                job={
                    "id": REVISION_JOB_ID,
                    "status": "canceled",
                }
            )

    class Queue:
        def get(self, _job_id: str) -> None:
            raise TaskNotFound("not published yet")

    monkeypatch.setattr(app_module, "product_redis_queue", lambda: Queue())
    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "queued"}
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)
    monkeypatch.setattr(
        app_module,
        "postgres_apply_generation_settlement",
        lambda job_id, owner: calls.append(
            ("settle", (job_id, owner))
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_revision_job_payload",
        lambda _job_id, _principal: {
            "jobId": REVISION_JOB_ID,
            "status": "canceled",
        },
    )

    response = client.post(
        f"/api/image-refinements/{REVISION_JOB_ID}/cancel"
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "canceled"
    assert calls == [
        (
            "postgres-cancel",
            {
                "job_id": REVISION_JOB_ID,
                "owner_user_id": USER_ID,
            },
        ),
    ]


def test_revision_cancel_validates_revision_type_before_mutation(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            app_module.RefinementContractError(
                "revision_job_contract_mismatch",
                "not a revision",
                field="jobType",
            )
        ),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        lambda: pytest.fail("wrong job type must not mutate PostgreSQL"),
    )
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("wrong job type must not mutate Redis"),
    )

    response = client.post(
        f"/api/image-refinements/{REVISION_JOB_ID}/cancel"
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "revision_job_contract_mismatch"


def test_revision_cancel_leaves_completed_redis_task_to_reconciler(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    task = {
        "task_id": REVISION_JOB_ID,
        "status": "done",
        "result": {},
    }

    class Queue:
        def get(self, _job_id: str) -> dict[str, Any]:
            return task

        def request_cancel(self, _job_id: str) -> None:
            pytest.fail("completed Redis task must not be canceled")

    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "running"}
        ),
    )
    monkeypatch.setattr(app_module, "product_redis_queue", lambda: Queue())
    monkeypatch.setattr(
        app_module,
        "redis_revision_contract",
        lambda _task, _principal: revision_contract(),
    )
    monkeypatch.setattr(
        app_module,
        "settle_postgres_revision_task",
        lambda _task, _principal: calls.append("settle"),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_revision_job_payload",
        lambda _job_id, _principal: {
            "jobId": REVISION_JOB_ID,
            "status": "completed",
        },
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        lambda: pytest.fail("completed task must not request cancel"),
    )

    response = client.post(
        f"/api/image-refinements/{REVISION_JOB_ID}/cancel"
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "revision_job_already_finished"
    assert calls == []


def test_running_revision_cancel_requests_redis_before_postgres(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    task = {
        "task_id": REVISION_JOB_ID,
        "status": "running",
        "result": {},
    }

    class Queue:
        def get(self, _job_id: str) -> dict[str, Any]:
            return task

        def request_cancel(self, _job_id: str) -> dict[str, Any]:
            calls.append("redis-cancel")
            return task

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def request_cancel(self, **_kwargs: Any) -> SimpleNamespace:
            calls.append("postgres-cancel")
            return SimpleNamespace(
                job={"id": REVISION_JOB_ID, "status": "running"}
            )

    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "running"}
        ),
    )
    monkeypatch.setattr(app_module, "product_redis_queue", lambda: Queue())
    monkeypatch.setattr(
        app_module,
        "redis_revision_contract",
        lambda _task, _principal: revision_contract(),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)
    monkeypatch.setattr(
        app_module,
        "postgres_revision_job_payload",
        lambda _job_id, _principal: {
            "jobId": REVISION_JOB_ID,
            "status": "running",
            "cancelRequested": True,
        },
    )

    response = client.post(
        f"/api/image-refinements/{REVISION_JOB_ID}/cancel"
    )

    assert response.status_code == 200
    assert response.get_json()["cancelRequested"] is True
    assert calls == ["redis-cancel", "postgres-cancel"]


def test_running_generation_cancel_requests_redis_before_postgres(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    task = {
        "task_id": JOB_ID,
        "status": "running",
        "result": {},
    }

    class Queue:
        def get(self, _job_id: str) -> dict[str, Any]:
            return task

        def request_cancel(self, _job_id: str) -> dict[str, Any]:
            calls.append("redis-cancel")
            return task

    class FakeStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def request_cancel(self, **_kwargs: Any) -> SimpleNamespace:
            calls.append("postgres-cancel")
            return SimpleNamespace(
                job={"id": JOB_ID, "status": "running"}
            )

    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "running"}
        ),
    )
    monkeypatch.setattr(app_module, "product_redis_queue", lambda: Queue())
    monkeypatch.setattr(
        app_module,
        "redis_batch_contract",
        lambda _task, _principal: generation_contract(),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "ProductJobStore", FakeStore)
    monkeypatch.setattr(
        app_module,
        "postgres_generation_job_payload",
        lambda _job_id, _principal: {
            "jobId": JOB_ID,
            "status": "running",
            "cancelRequested": True,
        },
    )

    response = client.post(f"/api/generation-jobs/{JOB_ID}/cancel")

    assert response.status_code == 200
    assert response.get_json()["cancelRequested"] is True
    assert calls == ["redis-cancel", "postgres-cancel"]


def test_generation_cancel_validates_batch_type_before_side_effects(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "postgres_owned_generation_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            app_module.BatchContractError(
                "generation_job_contract_mismatch",
                "not a generation batch",
                field="jobType",
            )
        ),
    )
    monkeypatch.setattr(
        app_module,
        "product_redis_queue",
        lambda: pytest.fail("wrong job type must not read Redis"),
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        lambda: pytest.fail("wrong job type must not mutate PostgreSQL"),
    )

    response = client.post(f"/api/generation-jobs/{JOB_ID}/cancel")

    assert response.status_code == 400
    assert response.get_json()["code"] == "generation_job_contract_mismatch"


def test_running_revision_missing_redis_task_does_not_mutate_postgres(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Queue:
        def get(self, _job_id: str) -> None:
            raise TaskNotFound("missing")

    monkeypatch.setattr(
        app_module,
        "postgres_owned_revision_detail",
        lambda _job_id, _principal: SimpleNamespace(
            job={"status": "running"}
        ),
    )
    monkeypatch.setattr(app_module, "product_redis_queue", lambda: Queue())
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        lambda: pytest.fail("missing running task must not mutate PostgreSQL"),
    )

    response = client.post(
        f"/api/image-refinements/{REVISION_JOB_ID}/cancel"
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "generation_queue_unavailable"
