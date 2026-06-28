from __future__ import annotations

import importlib
import io
import json
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from PIL import Image

import auth_service
import billing
import object_storage_service
import payment_service
import sms_service
import storage_db
from generation_queue import InMemoryGenerationQueue


PAYMENT_WEBHOOK_SECRET = "test-payment-webhook-secret"
OBJECT_SIGNING_SECRET = "test-object-signing-secret"
PHONE = "13800138000"
NORMALIZED_PHONE = "+8613800138000"


@dataclass
class ProductApiFixture:
    client: Any
    flask_app: Any
    storage_db_path: Path
    billing_db_path: Path
    object_store_dir: Path


@pytest.fixture()
def product_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProductApiFixture:
    storage_db_path = tmp_path / "storage.sqlite3"
    billing_db_path = tmp_path / "billing.sqlite3"
    object_store_dir = tmp_path / "objects"

    monkeypatch.setenv("STORAGE_DB_PATH", str(storage_db_path))
    monkeypatch.setenv("BILLING_DB_PATH", str(billing_db_path))
    monkeypatch.setenv("OBJECT_STORE_DIR", str(object_store_dir))
    monkeypatch.setenv("OBJECT_SIGNING_SECRET", OBJECT_SIGNING_SECRET)
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", PAYMENT_WEBHOOK_SECRET)
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_BILLING", "true")
    monkeypatch.setenv("AUTH_EXPOSE_MOCK_OTP", "1")

    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)

    return ProductApiFixture(
        client=app_module.app.test_client(),
        flask_app=app_module.app,
        storage_db_path=storage_db_path,
        billing_db_path=billing_db_path,
        object_store_dir=object_store_dir,
    )


def _fresh_product_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **env: str | None,
) -> ProductApiFixture:
    storage_db_path = tmp_path / "storage.sqlite3"
    billing_db_path = tmp_path / "billing.sqlite3"
    object_store_dir = tmp_path / "objects"
    for name in (
        "AUTH_EXPOSE_MOCK_OTP",
        "ENABLE_LOCAL_DEMO_AUTH",
        "SMS_PROVIDER",
        "SMS_WEBHOOK_URL",
        "SMS_WEBHOOK_TOKEN",
        "SMS_WEBHOOK_TIMEOUT",
        "ENABLE_LOCAL_DEMO_BILLING",
        "PAYMENT_PROVIDER",
        "ALLOW_FAKE_PAYMENT_PROVIDER",
        "FAKE_PAYMENT_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STORAGE_DB_PATH", str(storage_db_path))
    monkeypatch.setenv("BILLING_DB_PATH", str(billing_db_path))
    monkeypatch.setenv("OBJECT_STORE_DIR", str(object_store_dir))
    monkeypatch.setenv("OBJECT_SIGNING_SECRET", OBJECT_SIGNING_SECRET)
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", PAYMENT_WEBHOOK_SECRET)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    return ProductApiFixture(
        client=app_module.app.test_client(),
        flask_app=app_module.app,
        storage_db_path=storage_db_path,
        billing_db_path=billing_db_path,
        object_store_dir=object_store_dir,
    )


def test_product_api_routes_are_registered(product_api: ProductApiFixture) -> None:
    required_routes = [
        ("POST", "/api/auth/request-otp"),
        ("POST", "/api/auth/verify-otp"),
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/stores"),
        ("GET", "/api/stores"),
        ("GET", "/api/growth/withdrawals/balance"),
        ("GET", "/api/growth/withdrawals"),
        ("POST", "/api/growth/withdrawals"),
        ("POST", "/api/payments/orders"),
        ("POST", "/api/payments/fake-callback"),
        ("POST", "/api/objects/sign"),
        ("GET", "/objects/<key>"),
        ("POST", "/api/generation-jobs"),
        ("GET", "/api/generation-jobs/<job_id>"),
        ("POST", "/api/generation-jobs/<job_id>/cancel"),
        ("GET", "/api/ops/readiness"),
        ("GET", "/api/admin/queue-snapshot"),
        ("POST", "/api/admin/actions/risk"),
        ("POST", "/api/admin/actions/withdrawals/<withdrawal_id>/status"),
    ]

    missing = [
        f"{method} {path}"
        for method, path in required_routes
        if not _has_route(product_api.flask_app, method, path)
    ]

    assert missing == [], "Missing product API endpoints: " + ", ".join(missing)


def test_upload_menu_persists_original_file_to_object_storage_and_db(
    product_api: ProductApiFixture,
) -> None:
    app_module = importlib.import_module("app")
    menu_summary = {
        "file": "menu.xlsx",
        "store": "测试门店",
        "count": 1,
        "kindCounts": {"single": 1, "combo": 0, "snack": 0, "total": 1},
        "items": [{"row": 1, "name": "辣椒炒肉盖码饭"}],
    }

    with mock.patch.object(app_module, "parse_menu", return_value=menu_summary):
        response = product_api.client.post(
            "/api/upload-menu",
            data={"file": (io.BytesIO(b"fake-xlsx-bytes"), "menu.xlsx")},
            content_type="multipart/form-data",
        )

    payload = _json_for_status(response, 200, "POST /api/upload-menu")
    assert payload["ok"] is True
    assert payload["menuUploadId"].startswith("menu_")
    assert payload["menu"]["store"] == "测试门店"
    assert "items" not in payload["menu"]
    assert "objectKey" not in json.dumps(payload)

    conn = storage_db.get_conn(product_api.storage_db_path)
    try:
        row = conn.execute("SELECT * FROM menu_uploads WHERE id = ?", (payload["menuUploadId"],)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["store_name"] == "测试门店"
    assert row["original_filename"] == "menu.xlsx"
    assert row["object_key"].startswith("menus/")
    assert row["status"] == "parsed"
    assert row["file_size"] == len(b"fake-xlsx-bytes")

    storage = object_storage_service.ObjectStorageService(product_api.object_store_dir)
    assert storage.read_bytes(row["object_key"]) == b"fake-xlsx-bytes"


def test_upload_library_persists_images_to_object_storage_and_db(
    product_api: ProductApiFixture,
) -> None:
    app_module = importlib.import_module("app")
    image_bytes = _test_image_bytes()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("湘菜/宫保鸡丁.jpg", image_bytes)
        zf.writestr("notes.txt", b"ignore")
    zip_buffer.seek(0)

    with mock.patch.object(app_module, "build_plan", return_value={"results": [], "styles": []}):
        response = product_api.client.post(
            "/api/upload-library",
            data={"file": (zip_buffer, "library.zip")},
            content_type="multipart/form-data",
        )

    payload = _json_for_status(response, 200, "POST /api/upload-library")
    assert payload["ok"] is True
    assert payload["uploadedImageCount"] == 1
    assert len(payload["libraryImageIds"]) == 1
    assert "objectKey" not in json.dumps(payload)

    conn = storage_db.get_conn(product_api.storage_db_path)
    try:
        row = conn.execute("SELECT * FROM library_images WHERE id = ?", (payload["libraryImageIds"][0],)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["dish_name"] == "宫保鸡丁"
    assert row["source"] == "uploaded"
    assert row["style_id"] == "style-upload"
    assert row["object_key"].startswith("originals/uploaded_")
    assert row["file_size"] == len(image_bytes)
    assert row["width"] == 2
    assert row["height"] == 3

    storage = object_storage_service.ObjectStorageService(product_api.object_store_dir)
    assert storage.read_bytes(row["object_key"]) == image_bytes


def _test_image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), color=(240, 60, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_ops_readiness_reports_storage_and_generation_queue(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("app")
    queue = InMemoryGenerationQueue(worker_count=1, max_pending_jobs=3)
    try:
        queue.store.reserve("ready-job", requested=1, metadata={"style": "style-1"})
        monkeypatch.setattr(app_module, "generation_queue", queue)
        monkeypatch.setattr(
            app_module.object_storage_service,
            "assess_object_storage_readiness",
            lambda: {
                "ready": True,
                "provider": "local",
                "mode": "local_demo",
                "blockingIssues": [],
                "warnings": ["local_object_storage_is_for_development_only"],
            },
        )

        response = product_api.client.get("/api/ops/readiness")
    finally:
        queue.shutdown()

    payload = _json_for_status(response, 200, "GET /api/ops/readiness")
    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["objectStorage"]["provider"] == "local"
    assert payload["objectStorage"]["blockingIssues"] == []
    assert payload["generationProvider"]["ready"] is True
    assert payload["generationProvider"]["mode"] in {"tokenhub", "legacy_cloud_api", "unconfigured"}
    assert payload["payments"]["provider"] == "fake"
    assert payload["payments"]["ready"] is True
    assert payload["generationQueue"]["countsByStatus"]["queued"] == 1
    assert payload["generationQueue"]["limits"]["maxPendingJobs"] == 3
    assert payload["generationQueue"]["closed"] is False


def test_ops_readiness_is_false_when_generation_provider_missing_tokenhub_in_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fresh_product_api(
        tmp_path,
        monkeypatch,
        APP_ENV="staging",
        TENCENT_TOKENHUB_API_KEY=None,
        TENCENT_TOKENHUB_ENABLED=None,
        TOKENHUB_API_KEY=None,
        HUNYUAN_TOKENHUB_API_KEY=None,
        TENCENTCLOUD_SECRET_ID=None,
        TENCENTCLOUD_SECRET_KEY=None,
        TENCENT_SECRET_ID=None,
        TENCENT_SECRET_KEY=None,
        TENCENT_HUNYUAN_ENABLED=None,
        TENCENT_AIART_ENABLED=None,
        OBJECT_STORAGE_PROVIDER="cos",
        OBJECT_STORAGE_BUCKET="waimai-assets-prod",
        OBJECT_STORAGE_REGION="ap-guangzhou",
        OBJECT_STORAGE_SECRET_ID="object-storage-sid",
        OBJECT_STORAGE_SECRET_KEY="object-storage-skey",
        OBJECT_STORAGE_PRIVATE="true",
    )

    response = fixture.client.get("/api/ops/readiness")

    payload = _json_for_status(response, 200, "GET /api/ops/readiness missing TokenHub")
    generation = payload["generationProvider"]
    assert payload["ready"] is False
    assert generation["ready"] is False
    assert generation["mode"] == "unconfigured"
    assert generation["tokenhubRequired"] is True
    assert "live_generation_provider_required" in generation["blockingIssues"]
    assert "tokenhub_image_provider_required" in generation["blockingIssues"]
    assert "TENCENT_TOKENHUB_API_KEY" in generation["missingConfig"]


def test_ops_readiness_treats_render_runtime_as_live_generation_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fresh_product_api(
        tmp_path,
        monkeypatch,
        APP_ENV=None,
        PUBLIC_BASE_URL="https://waimai-image-tool-1.onrender.com",
        TENCENT_TOKENHUB_API_KEY=None,
        TENCENT_TOKENHUB_ENABLED=None,
        TOKENHUB_API_KEY=None,
        HUNYUAN_TOKENHUB_API_KEY=None,
        TENCENTCLOUD_SECRET_ID="legacy-id",
        TENCENTCLOUD_SECRET_KEY="legacy-key",
        TENCENT_HUNYUAN_ENABLED="true",
    )

    response = fixture.client.get("/api/ops/readiness")

    payload = _json_for_status(response, 200, "GET /api/ops/readiness render runtime")
    generation = payload["generationProvider"]
    assert payload["ready"] is False
    assert generation["appEnv"] == "render"
    assert generation["mode"] == "legacy_cloud_api"
    assert generation["cloudApiReady"] is True
    assert generation["tokenhubRequired"] is True
    assert "tokenhub_image_provider_required" in generation["blockingIssues"]


def test_ops_readiness_accepts_tokenhub_generation_provider_in_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fresh_product_api(
        tmp_path,
        monkeypatch,
        APP_ENV="staging",
        TENCENT_TOKENHUB_API_KEY="tokenhub-test-key",
        TENCENT_TOKENHUB_ENABLED="true",
        TENCENT_TOKENHUB_IMAGE_MODEL="hy-image-v3.0",
        TENCENTCLOUD_SECRET_ID=None,
        TENCENTCLOUD_SECRET_KEY=None,
        TENCENT_SECRET_ID=None,
        TENCENT_SECRET_KEY=None,
        TENCENT_HUNYUAN_ENABLED=None,
        TENCENT_AIART_ENABLED=None,
        OBJECT_STORAGE_PROVIDER="cos",
        OBJECT_STORAGE_BUCKET="waimai-assets-prod",
        OBJECT_STORAGE_REGION="ap-guangzhou",
        OBJECT_STORAGE_SECRET_ID="object-storage-sid",
        OBJECT_STORAGE_SECRET_KEY="object-storage-skey",
        OBJECT_STORAGE_PRIVATE="true",
    )

    response = fixture.client.get("/api/ops/readiness")

    payload = _json_for_status(response, 200, "GET /api/ops/readiness TokenHub ready")
    generation = payload["generationProvider"]
    assert payload["ready"] is True
    assert generation["ready"] is True
    assert generation["mode"] == "tokenhub"
    assert generation["tokenhubReady"] is True
    assert generation["tokenhubRequired"] is True
    assert generation["tokenhubModel"] == "hy-image-v3.0"


def test_ops_readiness_is_false_when_storage_or_queue_is_not_ready(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("app")
    queue = InMemoryGenerationQueue(worker_count=1)
    queue.shutdown()
    monkeypatch.setattr(app_module, "generation_queue", queue)
    monkeypatch.setattr(
        app_module.object_storage_service,
        "assess_object_storage_readiness",
        lambda: {
            "ready": False,
            "provider": "local",
            "mode": "local_demo",
            "blockingIssues": ["private_remote_object_storage_provider_required"],
            "warnings": [],
        },
    )

    response = product_api.client.get("/api/ops/readiness")

    payload = _json_for_status(response, 200, "GET /api/ops/readiness not ready")
    assert payload["ready"] is False
    assert payload["objectStorage"]["ready"] is False
    assert payload["payments"]["ready"] is True
    assert payload["generationQueue"]["closed"] is True


def test_ops_readiness_is_false_when_payment_provider_is_not_ready(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("app")
    queue = InMemoryGenerationQueue(worker_count=1)
    try:
        monkeypatch.setattr(app_module, "generation_queue", queue)
        monkeypatch.setattr(
            app_module.object_storage_service,
            "assess_object_storage_readiness",
            lambda: {
                "ready": True,
                "provider": "local",
                "mode": "local_demo",
                "blockingIssues": [],
                "warnings": [],
            },
        )
        monkeypatch.setattr(
            app_module.payment_service,
            "assess_payment_provider_readiness",
            lambda: {
                "ready": False,
                "provider": "fake",
                "mode": "local_demo",
                "errors": ["real_payment_provider_required"],
                "blockingIssues": ["real_payment_provider_required"],
                "warnings": [],
                "requiredConfig": [],
                "missingConfig": [],
            },
        )

        response = product_api.client.get("/api/ops/readiness")
    finally:
        queue.shutdown()

    payload = _json_for_status(response, 200, "GET /api/ops/readiness payment not ready")
    assert payload["ready"] is False
    assert payload["objectStorage"]["ready"] is True
    assert payload["payments"]["ready"] is False
    assert payload["payments"]["blockingIssues"] == ["real_payment_provider_required"]


def test_admin_queue_snapshot_returns_read_only_queue_metrics(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("app")
    queue = InMemoryGenerationQueue(worker_count=2, max_pending_jobs=5)
    try:
        queue.store.reserve("admin-queued", requested=2, metadata={"style": "style-1"})
        monkeypatch.setattr(app_module, "generation_queue", queue)

        response = product_api.client.get("/api/admin/queue-snapshot")
    finally:
        queue.shutdown()

    payload = _json_for_status(response, 200, "GET /api/admin/queue-snapshot")
    assert payload["ok"] is True
    assert payload["queue"]["workerCount"] == 2
    assert payload["queue"]["countsByStatus"]["queued"] == 1
    assert payload["queue"]["limits"]["maxPendingJobs"] == 5


def test_style_preview_does_not_generate_without_explicit_flag(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("app")
    generate_flags: list[bool] = []

    def fake_preview_samples(style: str, *, generate: bool = False) -> dict[str, Any]:
        generate_flags.append(generate)
        return {"style": style, "samples": []}

    def fake_public_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {"style": payload["style"], "samples": payload["samples"], "generate": generate_flags[-1]}

    monkeypatch.setattr(app_module, "preview_samples", fake_preview_samples)
    monkeypatch.setattr(app_module, "public_preview_payload", fake_public_preview_payload)

    passive_response = product_api.client.get("/api/style-preview?style=style-1")
    passive_payload = _json_for_status(passive_response, 200, "GET /api/style-preview passive")
    explicit_response = product_api.client.get("/api/style-preview?style=style-1&generate=1")
    explicit_payload = _json_for_status(explicit_response, 200, "GET /api/style-preview generate=1")

    assert passive_payload["generate"] is False
    assert explicit_payload["generate"] is True
    assert generate_flags == [False, True]


def test_generation_job_status_payload_keeps_frontend_timing_contract(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("app")
    queue = InMemoryGenerationQueue(stale_after_seconds=0.001, timeout_seconds=60)
    try:
        queue.store.reserve("contract-job", requested=1, metadata={"style": "style-1", "quality": "standard"})
        time.sleep(0.01)

        monkeypatch.setattr(app_module, "generation_queue", queue)
        monkeypatch.setattr(app_module, "tencent_ready", lambda: False)

        response = product_api.client.get("/api/generation-jobs/contract-job")
        payload = _json_for_status(response, 200, "GET /api/generation-jobs/<job_id>")
    finally:
        queue.shutdown()

    assert payload["jobId"] == "contract-job"
    assert payload["status"] == "queued"
    assert payload["stale"] is True
    assert payload["timedOut"] is False
    for required in [
        "finishedAt",
        "elapsed",
        "elapsedSeconds",
        "timingReason",
        "ageSeconds",
        "inactiveSeconds",
        "staleAfterSeconds",
        "timeoutSeconds",
    ]:
        assert required in payload


def test_auth_session_and_store_flow(product_api: ProductApiFixture) -> None:
    auth = _login(product_api.client)
    token = auth["token"]
    user = auth["user"]

    session_response = product_api.client.get(
        "/api/auth/session",
        headers=_auth_header(token),
    )
    session_payload = _json_for_status(session_response, 200, "GET /api/auth/session")
    assert _nested(session_payload, "user")["id"] == user["id"]
    assert _nested(session_payload, "user")["phone"] == NORMALIZED_PHONE

    create_response = product_api.client.post(
        "/api/stores",
        json={"name": "测试门店"},
        headers=_auth_header(token),
    )
    create_payload = _json_for_status(create_response, (200, 201), "POST /api/stores")
    store = _nested(create_payload, "store", fallback=create_payload)
    assert store["name"] == "测试门店"
    assert store["id"]

    list_response = product_api.client.get(
        "/api/stores",
        headers=_auth_header(token),
    )
    list_payload = _json_for_status(list_response, 200, "GET /api/stores")
    stores = _nested(list_payload, "stores", fallback=list_payload)
    assert isinstance(stores, list)
    assert any(item["id"] == store["id"] and item["name"] == "测试门店" for item in stores)

    logout_response = product_api.client.post(
        "/api/auth/logout",
        headers=_auth_header(token),
    )
    logout_payload = _json_for_status(logout_response, 200, "POST /api/auth/logout")
    assert logout_payload["loggedOut"] is True

    revoked_response = product_api.client.get(
        "/api/auth/session",
        headers=_auth_header(token),
    )
    _assert_status(revoked_response, 401, "GET /api/auth/session after logout")


def test_request_otp_requires_sms_provider_outside_local_demo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _fresh_product_api(
        tmp_path,
        monkeypatch,
        ENABLE_LOCAL_DEMO_AUTH="0",
        AUTH_EXPOSE_MOCK_OTP=None,
        SMS_PROVIDER=None,
    )

    response = api.client.post(
        "/api/auth/request-otp",
        json={"phone": PHONE},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    payload = _json_for_status(response, 503, "POST /api/auth/request-otp without sms provider")

    assert payload["code"] == sms_service.ERR_SMS_PROVIDER_UNAVAILABLE
    assert "mockCode" not in payload
    if api.storage_db_path.exists():
        conn = storage_db.init_db(api.storage_db_path)
        auth_service.init_auth_schema(conn)
        try:
            assert conn.execute("SELECT COUNT(*) FROM otp_challenges").fetchone()[0] == 0
        finally:
            conn.close()


def test_request_otp_rejects_mock_provider_outside_local_demo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _fresh_product_api(
        tmp_path,
        monkeypatch,
        ENABLE_LOCAL_DEMO_AUTH="0",
        AUTH_EXPOSE_MOCK_OTP=None,
        SMS_PROVIDER="mock",
    )

    response = api.client.post(
        "/api/auth/request-otp",
        json={"phone": PHONE},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    payload = _json_for_status(response, 503, "POST /api/auth/request-otp guarded mock provider")

    assert payload["code"] == sms_service.ERR_SMS_PROVIDER_UNAVAILABLE
    assert "local/mock" in payload["error"]


def test_request_otp_uses_webhook_provider_without_exposing_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_payloads: list[dict[str, Any]] = []

    class FakeSmsResponse:
        status = 202

        def read(self) -> bytes:
            return b'{"messageId":"api_msg_1"}'

        def close(self) -> None:
            return None

    def fake_urlopen(request, timeout):
        sent_payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeSmsResponse()

    monkeypatch.setattr(sms_service.urllib.request, "urlopen", fake_urlopen)
    api = _fresh_product_api(
        tmp_path,
        monkeypatch,
        ENABLE_LOCAL_DEMO_AUTH="0",
        AUTH_EXPOSE_MOCK_OTP=None,
        SMS_PROVIDER="webhook",
        SMS_WEBHOOK_URL="https://sms.example.test/send",
    )

    response = api.client.post(
        "/api/auth/request-otp",
        json={"phone": PHONE},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    payload = _json_for_status(response, 200, "POST /api/auth/request-otp webhook")

    assert payload["challengeId"]
    assert "mockCode" not in payload
    assert payload["sms"] == {"provider": "webhook", "status": "sent", "messageId": "api_msg_1"}
    assert sent_payloads[0]["phone"] == NORMALIZED_PHONE
    assert sent_payloads[0]["code"]

    verify_response = api.client.post(
        "/api/auth/verify-otp",
        json={"challengeId": payload["challengeId"], "code": sent_payloads[0]["code"]},
    )
    verify_payload = _json_for_status(verify_response, 200, "POST /api/auth/verify-otp webhook code")
    assert verify_payload["user"]["phone"] == NORMALIZED_PHONE


def test_authenticated_invite_cannot_override_session_user(product_api: ProductApiFixture) -> None:
    auth = _login(product_api.client)
    response = product_api.client.post(
        "/api/growth/invites/accept",
        json={
            "inviterUserId": "inviter-user",
            "inviteeUserId": "different-user",
            "phoneVerified": True,
            "humanVerified": True,
        },
        headers=_auth_header(auth["token"]),
    )

    payload = _json_for_status(response, 403, "POST /api/growth/invites/accept invitee override")
    assert payload["code"] == "growth_write_forbidden"


def test_authenticated_invite_reward_uses_server_registration_context(product_api: ProductApiFixture) -> None:
    first_login = _login(product_api.client)
    _age_otp_challenges(product_api.storage_db_path)
    second_login = _login(product_api.client)

    assert second_login["user"]["id"] == first_login["user"]["id"]

    response = product_api.client.post(
        "/api/growth/invites/accept",
        json={
            "inviterUserId": "inviter-existing-phone",
            "phoneVerified": True,
            "humanVerified": True,
            "samePhoneRegistered": False,
            "sameDeviceRecentRegistrations": 0,
            "sameIpRecentRegistrations": 0,
            "riskBlocked": False,
        },
        headers=_auth_header(second_login["token"]),
    )
    payload = _json_for_status(response, 200, "POST /api/growth/invites/accept existing phone")
    invite = payload["invite"]

    assert invite["inviteeUserId"] == first_login["user"]["id"]
    assert invite["rewardStatus"] == "canceled"
    assert invite["registrationRewards"] == {"inviterPoints": 0, "inviteePoints": 0}
    assert billing.get_account("inviter-existing-phone", db_path=product_api.billing_db_path)["balance"] == 0


def test_agent_withdrawal_api_uses_session_agent_and_admin_status_flow(product_api: ProductApiFixture) -> None:
    auth = _login(product_api.client)
    agent_id = "agent_withdraw_api_1"
    _insert_agent_with_paid_settlement(
        product_api,
        user_id=auth["user"]["id"],
        agent_id=agent_id,
        paid_commission_cents=30000,
    )

    balance_response = product_api.client.get(
        "/api/growth/withdrawals/balance",
        headers=_auth_header(auth["token"]),
    )
    balance_payload = _json_for_status(balance_response, 200, "GET /api/growth/withdrawals/balance")
    assert balance_payload["agentId"] == agent_id
    assert balance_payload["balance"]["availableCents"] == 30000

    create_response = product_api.client.post(
        "/api/growth/withdrawals",
        json={
            "amountCents": 20000,
            "accountSnapshot": {
                "type": "bank",
                "accountName": "测试代理",
                "accountNoMasked": "6222****8888",
            },
            "metadata": {"requestNo": "withdraw-api-1"},
        },
        headers=_auth_header(auth["token"]),
    )
    create_payload = _json_for_status(create_response, 201, "POST /api/growth/withdrawals")
    withdrawal = create_payload["withdrawal"]
    assert create_payload["agentId"] == agent_id
    assert withdrawal["agentId"] == agent_id
    assert withdrawal["status"] == "pending"
    assert withdrawal["amountCents"] == 20000
    assert withdrawal["metadata"]["userId"] == auth["user"]["id"]
    assert withdrawal["metadata"]["requestNo"] == "withdraw-api-1"

    list_response = product_api.client.get(
        "/api/growth/withdrawals?status=pending",
        headers=_auth_header(auth["token"]),
    )
    list_payload = _json_for_status(list_response, 200, "GET /api/growth/withdrawals")
    assert [item["id"] for item in list_payload["withdrawals"]] == [withdrawal["id"]]

    approve_response = product_api.client.post(
        f"/api/admin/actions/withdrawals/{withdrawal['id']}/status",
        json={"status": "approved", "reason": "manual review passed"},
        headers={"X-Admin-User-Id": "ops_1"},
    )
    approved = _json_for_status(approve_response, 200, "POST withdrawal admin approve")["withdrawal"]
    assert approved["status"] == "approved"
    assert approved["statusReason"] == "manual review passed"
    assert approved["metadata"]["statusHistory"][-1]["metadata"]["actorUserId"] == "ops_1"

    paid_response = product_api.client.post(
        f"/api/admin/actions/withdrawals/{withdrawal['id']}/status",
        json={"status": "paid", "reason": "finance transfer confirmed"},
    )
    paid = _json_for_status(paid_response, 200, "POST withdrawal admin paid")["withdrawal"]
    assert paid["status"] == "paid"
    assert paid["paidAt"]

    final_balance_response = product_api.client.get(
        "/api/growth/withdrawals/balance",
        headers=_auth_header(auth["token"]),
    )
    final_balance = _json_for_status(final_balance_response, 200, "GET withdrawal balance final")["balance"]
    assert final_balance["lockedWithdrawalCents"] == 20000
    assert final_balance["availableCents"] == 10000


def test_agent_withdrawal_api_rejects_missing_agent_and_cross_agent(product_api: ProductApiFixture) -> None:
    unauthenticated_response = product_api.client.get("/api/growth/withdrawals/balance")
    unauthenticated_payload = _json_for_status(unauthenticated_response, 401, "GET withdrawal balance unauthenticated")
    assert unauthenticated_payload["code"] == "auth_required"

    auth = _login(product_api.client)
    missing_agent_response = product_api.client.get(
        "/api/growth/withdrawals/balance",
        headers=_auth_header(auth["token"]),
    )
    missing_agent_payload = _json_for_status(missing_agent_response, 404, "GET withdrawal balance without agent")
    assert missing_agent_payload["code"] == "agent_profile_required"

    _insert_agent_with_paid_settlement(
        product_api,
        user_id=auth["user"]["id"],
        agent_id="agent_session_owner",
        paid_commission_cents=20000,
    )
    _insert_agent_with_paid_settlement(
        product_api,
        user_id="other-user",
        agent_id="agent_other_owner",
        paid_commission_cents=20000,
    )

    cross_create_response = product_api.client.post(
        "/api/growth/withdrawals",
        json={
            "agentId": "agent_other_owner",
            "amountCents": 10000,
            "accountSnapshot": {"type": "bank", "accountName": "bad", "accountNoMasked": "0000****0000"},
        },
        headers=_auth_header(auth["token"]),
    )
    cross_create_payload = _json_for_status(cross_create_response, 403, "POST withdrawal cross-agent")
    assert cross_create_payload["code"] == "agent_access_forbidden"

    cross_list_response = product_api.client.get(
        "/api/growth/withdrawals?agentId=agent_other_owner",
        headers=_auth_header(auth["token"]),
    )
    cross_list_payload = _json_for_status(cross_list_response, 403, "GET withdrawals cross-agent")
    assert cross_list_payload["code"] == "agent_access_forbidden"


def test_withdrawal_api_maps_service_errors_to_json(product_api: ProductApiFixture) -> None:
    auth = _login(product_api.client)
    _insert_agent_with_paid_settlement(
        product_api,
        user_id=auth["user"]["id"],
        agent_id="agent_error_mapping",
        paid_commission_cents=10000,
    )

    below_minimum_response = product_api.client.post(
        "/api/growth/withdrawals",
        json={
            "amountCents": 9999,
            "accountSnapshot": {"type": "bank", "accountName": "bad", "accountNoMasked": "0000****0000"},
        },
        headers=_auth_header(auth["token"]),
    )
    below_minimum_payload = _json_for_status(below_minimum_response, 400, "POST withdrawal below minimum")
    assert below_minimum_payload["code"] == "invalid_withdrawal_input"
    assert below_minimum_payload["minimumAmountCents"] == 10000

    exceeded_response = product_api.client.post(
        "/api/growth/withdrawals",
        json={
            "amountCents": 20000,
            "accountSnapshot": {"type": "bank", "accountName": "bad", "accountNoMasked": "0000****0000"},
        },
        headers=_auth_header(auth["token"]),
    )
    exceeded_payload = _json_for_status(exceeded_response, 409, "POST withdrawal exceeds balance")
    assert exceeded_payload["code"] == "withdrawal_conflict"
    assert exceeded_payload["availableCents"] == 10000

    invalid_admin_status_response = product_api.client.post(
        "/api/admin/actions/withdrawals/missing-withdrawal/status",
        json={"status": "pending"},
    )
    invalid_admin_status_payload = _json_for_status(invalid_admin_status_response, 400, "POST withdrawal invalid status")
    assert invalid_admin_status_payload["code"] == "invalid_withdrawal_input"

    missing_admin_response = product_api.client.post(
        "/api/admin/actions/withdrawals/missing-withdrawal/status",
        json={"status": "approved"},
    )
    missing_admin_payload = _json_for_status(missing_admin_response, 404, "POST withdrawal missing")
    assert missing_admin_payload["code"] == "withdrawal_not_found"


def test_fake_payment_order_is_available_in_local_demo(product_api: ProductApiFixture) -> None:
    response = product_api.client.post(
        "/api/payments/orders",
        json={"cash": 49, "idempotencyKey": "local-demo-fake-pay"},
    )
    payload = _json_for_status(response, (200, 201), "POST /api/payments/orders local demo")
    order = _nested(payload, "order", fallback=payload)

    assert _field(order, "provider") == "fake"
    assert _field(order, "status") == "pending"
    assert _field(order, "paymentUrl", "payment_url").startswith("fakepay://checkout?")
    assert _field(_nested(payload, "instructions"), "paymentUrl", "payment_url").startswith("fakepay://checkout?")


def test_fake_payment_order_blocked_when_demo_billing_disabled_without_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _fresh_product_api(
        tmp_path,
        monkeypatch,
        ENABLE_LOCAL_DEMO_BILLING="false",
        PAYMENT_PROVIDER=None,
        ALLOW_FAKE_PAYMENT_PROVIDER=None,
    )

    response = api.client.post(
        "/api/payments/orders",
        json={"cash": 49, "provider": "fake"},
    )
    payload = _json_for_status(response, 503, "POST /api/payments/orders fake provider disabled")

    assert payload["code"] == "payment_provider_unavailable"
    assert payload["provider"] == "fake"
    assert payload["required"] == "PAYMENT_PROVIDER=fake or ALLOW_FAKE_PAYMENT_PROVIDER=true"


def test_fake_payment_callback_forbidden_when_demo_billing_disabled_without_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _fresh_product_api(
        tmp_path,
        monkeypatch,
        ENABLE_LOCAL_DEMO_BILLING="false",
        PAYMENT_PROVIDER=None,
        ALLOW_FAKE_PAYMENT_PROVIDER=None,
    )

    response = api.client.post(
        "/api/payments/fake-callback",
        json={
            "provider": "fake",
            "providerOrderId": "missing-fake-order",
            "eventType": "pay_success",
            "payload": {"eventId": "evt-disabled", "signature": "unused"},
        },
    )
    payload = _json_for_status(response, 403, "POST /api/payments/fake-callback disabled")

    assert payload["code"] == "fake_payment_provider_forbidden"
    assert payload["provider"] == "fake"
    assert payload["required"] == "PAYMENT_PROVIDER=fake or ALLOW_FAKE_PAYMENT_PROVIDER=true"


@pytest.mark.parametrize(
    "explicit_env",
    (
        {"PAYMENT_PROVIDER": "fake"},
        {"ALLOW_FAKE_PAYMENT_PROVIDER": "true"},
    ),
)
def test_explicit_fake_payment_config_allows_order_when_demo_billing_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_env: dict[str, str],
) -> None:
    api = _fresh_product_api(
        tmp_path,
        monkeypatch,
        ENABLE_LOCAL_DEMO_BILLING="false",
        **explicit_env,
    )

    response = api.client.post(
        "/api/payments/orders",
        json={"cash": 49, "idempotencyKey": f"explicit-{next(iter(explicit_env)).lower()}"},
    )
    payload = _json_for_status(response, (200, 201), "POST /api/payments/orders explicit fake config")
    order = _nested(payload, "order", fallback=payload)

    assert _field(order, "provider") == "fake"
    assert _field(order, "status") == "pending"


def test_fake_payment_callback_credits_billing_once(product_api: ProductApiFixture) -> None:
    auth = _login(product_api.client)
    token = auth["token"]
    user_id = auth["user"]["id"]

    create_response = product_api.client.post(
        "/api/payments/orders",
        json={"cash": 49, "idempotencyKey": "pay-49-once"},
        headers=_auth_header(token),
    )
    create_payload = _json_for_status(create_response, (200, 201), "POST /api/payments/orders")
    order = _nested(create_payload, "order", fallback=create_payload)
    expected_points = billing.points_for_recharge(49)

    assert _field(order, "provider") == "fake"
    assert _field(order, "status") == "pending"
    assert _field(order, "points") == expected_points
    assert _field(order, "amountCents", "amount_cents") == 4900
    assert _field(order, "idempotencyKey", "idempotency_key") == "pay-49-once"

    retry_response = product_api.client.post(
        "/api/payments/orders",
        json={"cash": 49, "idempotencyKey": "pay-49-once"},
        headers=_auth_header(token),
    )
    retry_payload = _json_for_status(retry_response, (200, 201), "POST /api/payments/orders idempotent retry")
    retry_order = _nested(retry_payload, "order", fallback=retry_payload)
    assert _field(retry_order, "orderId", "order_id") == _field(order, "orderId", "order_id")

    provider_order_id = _field(order, "providerOrderId", "provider_order_id")
    event_payload = {"eventId": "evt-pay-success-1", "status": "paid"}
    event_payload["signature"] = payment_service.fake_callback_signature(
        "fake",
        provider_order_id,
        "pay_success",
        event_payload,
        PAYMENT_WEBHOOK_SECRET,
    )
    callback_body = {
        "provider": "fake",
        "providerOrderId": provider_order_id,
        "eventType": "pay_success",
        "payload": event_payload,
    }

    callback_response = product_api.client.post("/api/payments/fake-callback", json=callback_body)
    callback_payload = _json_for_status(callback_response, 200, "POST /api/payments/fake-callback")
    assert callback_payload["ok"] is True
    callback = _nested(callback_payload, "callback", fallback=callback_payload)
    assert _field(callback, "pointsToCredit", "points_to_credit") == expected_points
    assert _field(callback, "status") == "paid"
    assert billing.get_account(user_id, db_path=product_api.billing_db_path)["balance"] == expected_points

    duplicate_response = product_api.client.post("/api/payments/fake-callback", json=callback_body)
    duplicate_payload = _json_for_status(duplicate_response, 200, "POST /api/payments/fake-callback duplicate")
    assert duplicate_payload["ok"] is True
    duplicate_callback = _nested(duplicate_payload, "callback", fallback=duplicate_payload)
    assert _field(duplicate_callback, "pointsToCredit", "points_to_credit") == 0
    assert billing.get_account(user_id, db_path=product_api.billing_db_path)["balance"] == expected_points


def test_signed_object_download_requires_token(product_api: ProductApiFixture) -> None:
    auth = _login(product_api.client)
    token = auth["token"]
    storage = object_storage_service.get_object_storage_service()
    object_key = storage.put_bytes(
        b"hello product object",
        object_key="generated/product-api/demo.txt",
    )

    sign_response = product_api.client.post(
        "/api/objects/sign",
        json={"objectKey": object_key, "purpose": "preview", "variant": "preview"},
        headers=_auth_header(token),
    )
    sign_payload = _json_for_status(sign_response, 200, "POST /api/objects/sign")
    assert sign_payload["token"]
    assert sign_payload["url"]
    assert object_key in sign_payload["url"]

    download_response = product_api.client.get(
        f"/objects/{object_key}",
        query_string={"token": sign_payload["token"]},
    )
    _assert_status(download_response, 200, f"GET /objects/{object_key}?token=...")
    assert download_response.data == b"hello product object"

    missing_token_response = product_api.client.get(f"/objects/{object_key}")
    _assert_status(missing_token_response, (401, 403), f"GET /objects/{object_key} without token")

    conn = storage_db.get_conn(product_api.storage_db_path)
    try:
        rows = conn.execute(
            """
            SELECT asset_id, action, allowed, deny_reason
            FROM asset_access_logs
            WHERE asset_id = ?
            ORDER BY rowid ASC
            """,
            (object_key,),
        ).fetchall()
    finally:
        conn.close()
    assert [(row["action"], row["allowed"], row["deny_reason"]) for row in rows] == [
        ("preview", 1, ""),
        ("object_access", 0, "missing_token"),
    ]


def test_admin_risk_action_smoke_writes_event(product_api: ProductApiFixture) -> None:
    response = product_api.client.post(
        "/api/admin/actions/risk",
        json={
            "eventType": "asset_download",
            "decision": "review",
            "riskLevel": "medium",
            "userId": "user_contract_1",
            "assetId": "asset_contract_1",
            "metadata": {"source": "integration-test"},
        },
    )
    payload = _json_for_status(response, (200, 201), "POST /api/admin/actions/risk")

    assert payload["ok"] is True
    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    risk_id = payload.get("id")
    assert risk_id, (
        "POST /api/admin/actions/risk must return a top-level id; "
        f"got keys={sorted(payload.keys())}, record.id={record.get('id')!r}"
    )

    conn = storage_db.get_conn(product_api.storage_db_path)
    try:
        try:
            row = conn.execute(
                "SELECT event_type, decision, risk_level, asset_id FROM risk_audit_logs WHERE id = ?",
                (risk_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            pytest.fail(f"risk action did not initialize/write risk_audit_logs in STORAGE_DB_PATH: {exc}")
        assert row is not None
        assert row["event_type"] == "asset_download"
        assert row["decision"] == "review"
        assert row["risk_level"] == "medium"
        assert row["asset_id"] == "asset_contract_1"
    finally:
        conn.close()


def test_admin_write_rbac_accepts_token_and_admin_session(
    product_api: ProductApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_ADMIN", "0")
    monkeypatch.setenv("ADMIN_API_TOKEN", "")

    no_auth_response = product_api.client.post(
        "/api/admin/actions/risk",
        json={"eventType": "rbac_no_auth", "decision": "review", "riskLevel": "low"},
    )
    no_auth_payload = _json_for_status(no_auth_response, 403, "POST admin risk without auth")
    assert no_auth_payload["code"] == "admin_write_forbidden"

    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-secret")
    token_response = product_api.client.post(
        "/api/admin/actions/risk",
        json={"eventType": "rbac_token", "decision": "allow", "riskLevel": "low"},
        headers={"X-Admin-Token": "admin-secret"},
    )
    token_payload = _json_for_status(token_response, 200, "POST admin risk with admin token")
    assert token_payload["ok"] is True

    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    login = _login(product_api.client)
    ordinary_response = product_api.client.post(
        "/api/admin/actions/risk",
        json={"eventType": "rbac_ordinary_session", "decision": "review", "riskLevel": "medium"},
        headers=_auth_header(login["token"]),
    )
    ordinary_payload = _json_for_status(ordinary_response, 403, "POST admin risk with ordinary session")
    assert ordinary_payload["code"] == "admin_write_forbidden"

    _set_user_metadata(product_api.storage_db_path, login["user"]["id"], {"role": "admin"})
    admin_session_response = product_api.client.post(
        "/api/admin/actions/risk",
        json={"eventType": "rbac_admin_session", "decision": "allow", "riskLevel": "low"},
        headers=_auth_header(login["token"]),
    )
    admin_session_payload = _json_for_status(admin_session_response, 200, "POST admin risk with admin session")
    assert admin_session_payload["ok"] is True


def _insert_agent_with_paid_settlement(
    product_api: ProductApiFixture,
    *,
    user_id: str,
    agent_id: str,
    paid_commission_cents: int,
) -> None:
    now = "2026-06-28T00:00:00+00:00"
    conn = storage_db.get_conn(product_api.storage_db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO agent_profiles (
                    id, user_id, agent_code, status, settlement_account_json,
                    contact_json, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, 'active', '{}', '{}', '{}', ?, ?)
                """,
                (agent_id, user_id, agent_id.upper(), now, now),
            )
            conn.execute(
                """
                INSERT INTO commission_settlements (
                    id, agent_id, settlement_no, total_commission_amount,
                    order_count, status, created_at, updated_at, paid_at
                )
                VALUES (?, ?, ?, ?, 1, 'paid', ?, ?, ?)
                """,
                (
                    f"settlement_{agent_id}",
                    agent_id,
                    f"SETTLEMENT_{agent_id.upper()}",
                    paid_commission_cents,
                    now,
                    now,
                    now,
                ),
            )
    finally:
        conn.close()


def _set_user_metadata(db_path: Path, user_id: str, metadata: dict[str, Any]) -> None:
    conn = storage_db.get_conn(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE users SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata), "2026-06-28T00:00:00+00:00", user_id),
            )
    finally:
        conn.close()


def _login(client: Any) -> dict[str, Any]:
    request_response = client.post("/api/auth/request-otp", json={"phone": PHONE})
    request_payload = _json_for_status(request_response, 200, "POST /api/auth/request-otp")

    assert request_payload["challengeId"]
    assert request_payload["mockCode"]
    assert request_payload["phone"] == NORMALIZED_PHONE

    verify_response = client.post(
        "/api/auth/verify-otp",
        json={
            "challengeId": request_payload["challengeId"],
            "code": request_payload["mockCode"],
        },
    )
    verify_payload = _json_for_status(verify_response, 200, "POST /api/auth/verify-otp")

    user = _nested(verify_payload, "user")
    session = _nested(verify_payload, "session")
    assert user["id"]
    assert user["phone"] == NORMALIZED_PHONE
    assert session["token"]
    return {"user": user, "token": session["token"]}


def _json_for_status(response: Any, expected_status: int | tuple[int, ...], label: str) -> dict[str, Any]:
    _assert_status(response, expected_status, label)
    payload = response.get_json(silent=True)
    assert isinstance(payload, dict), f"{label} expected JSON object, got: {response.get_data(as_text=True)[:500]}"
    return payload


def _assert_status(response: Any, expected_status: int | tuple[int, ...], label: str) -> None:
    expected = (expected_status,) if isinstance(expected_status, int) else expected_status
    assert response.status_code in expected, (
        f"{label} expected HTTP {expected}, got {response.status_code}: "
        f"{response.get_data(as_text=True)[:500]}"
    )


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _age_otp_challenges(db_path: Path) -> None:
    conn = storage_db.get_conn(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE otp_challenges SET created_at = ?",
                ("2000-01-01T00:00:00+00:00",),
            )
    finally:
        conn.close()


def _nested(payload: dict[str, Any], key: str, *, fallback: Any | None = None) -> Any:
    if key in payload:
        return payload[key]
    if fallback is not None:
        return fallback
    raise AssertionError(f"response missing {key!r}: {payload}")


def _field(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    raise AssertionError(f"response missing any of {names!r}: {payload}")


def _has_route(flask_app: Any, method: str, path: str) -> bool:
    for rule in flask_app.url_map.iter_rules():
        if method not in rule.methods:
            continue
        rule_text = str(rule)
        if rule_text == path:
            return True
        if path == "/objects/<key>" and rule_text.startswith("/objects/<"):
            return True
    return False
