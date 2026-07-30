from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import shutil
from typing import Any
from unittest import mock
from urllib.parse import quote, urlsplit

import pandas as pd
import pytest
from PIL import Image

import app as app_module
import auth_service
import object_storage_service


REMOTE_ADDR = {"REMOTE_ADDR": "203.0.113.50"}
SYNTACTIC_MENU_UPLOAD_ID = "menu_" + ("a" * 32)


def _image_bytes(suffix: str) -> bytes:
    output = io.BytesIO()
    image_format = "PNG" if suffix.lower() == ".png" else "JPEG"
    Image.new("RGB", (8, 8), (190, 50, 35)).save(
        output,
        image_format,
    )
    return output.getvalue()


def test_private_preview_metadata_rejects_before_serialization() -> None:
    with (
        mock.patch.object(
            app_module,
            "MAX_PREVIEW_METADATA_BYTES",
            64,
        ),
        mock.patch.object(
            app_module.json,
            "dumps",
            side_effect=AssertionError(
                "oversized metadata must not be serialized"
            ),
        ) as json_dumps,
    ):
        with pytest.raises(
            app_module.PreviewObjectStorageError,
            match="元数据超过大小限制",
        ):
            app_module.private_preview_metadata_bytes(
                {"providerMetadata": "x" * 128}
            )

    json_dumps.assert_not_called()


def test_ai_output_metadata_rejects_before_serialization(
    tmp_path: Path,
) -> None:
    target = tmp_path / "preview.png"
    with (
        mock.patch.object(
            app_module,
            "MAX_PREVIEW_METADATA_BYTES",
            64,
        ),
        mock.patch.object(
            app_module.json,
            "dumps",
            side_effect=AssertionError(
                "oversized metadata must not be serialized"
            ),
        ) as json_dumps,
    ):
        with pytest.raises(
            app_module.json_limits.JsonSizeLimitExceeded,
        ):
            app_module.write_ai_output_metadata(
                target,
                {"providerMetadata": "x" * 128},
            )

    json_dumps.assert_not_called()
    assert not app_module.ai_output_metadata_path(target).exists()


def test_ai_output_metadata_rejects_oversized_legacy_sidecar_before_read(
    tmp_path: Path,
) -> None:
    target = tmp_path / "preview.png"
    metadata_path = app_module.ai_output_metadata_path(target)
    metadata_path.write_bytes(b"x" * 65)

    with (
        mock.patch.object(
            app_module,
            "MAX_PREVIEW_METADATA_BYTES",
            64,
        ),
        mock.patch.object(
            app_module.json,
            "loads",
            side_effect=AssertionError(
                "oversized sidecar must not be parsed"
            ),
        ) as json_loads,
    ):
        assert app_module.load_ai_output_metadata(target) is None

    json_loads.assert_not_called()


@dataclass(frozen=True)
class PreviewRouteCase:
    name: str
    path: str
    generation_boundary: str
    boundary_result: dict[str, Any]
    resolves_background: bool = False


PREVIEW_ROUTE_CASES = (
    PreviewRouteCase(
        name="plan",
        path="/api/plan?style=style-1",
        generation_boundary="build_plan",
        boundary_result={"styles": [], "results": []},
    ),
    PreviewRouteCase(
        name="style-background",
        path="/api/style-background?style=style-1&generate=1",
        generation_boundary="style_sample_candidate",
        boundary_result={
            "imageId": "background-test",
            "styleId": "style-1",
            "url": "",
        },
    ),
    PreviewRouteCase(
        name="style-preview",
        path="/api/style-preview?style=style-1&generate=1",
        generation_boundary="preview_samples",
        boundary_result={"style": "style-1", "samples": []},
        resolves_background=True,
    ),
    PreviewRouteCase(
        name="style-preview-sample",
        path="/api/style-preview-sample?style=style-1&index=0",
        generation_boundary="preview_sample_payload",
        boundary_result={
            "row": 1,
            "name": "测试菜",
            "candidate": None,
            "generation": {"status": "pending"},
        },
        resolves_background=True,
    ),
)


@dataclass(frozen=True)
class LiveSecurityFixture:
    client: Any
    library_dir: Path
    model_input_dir: Path
    upload_dir: Path


@pytest.fixture()
def live_security_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LiveSecurityFixture:
    storage_db_path = tmp_path / "storage.sqlite3"
    billing_db_path = tmp_path / "billing.sqlite3"
    object_store_dir = tmp_path / "objects"
    library_dir = tmp_path / "library"
    model_input_dir = tmp_path / "model-inputs"
    upload_dir = tmp_path / "uploads"
    for directory in (
        object_store_dir,
        library_dir,
        model_input_dir,
        upload_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_AUTH", "false")
    monkeypatch.setenv("ENABLE_LOCAL_DEMO_STORAGE", "true")
    monkeypatch.setenv("OBJECT_STORAGE_PROVIDER", "local")
    monkeypatch.setenv("PRODUCT_POSTGRES_ENABLED", "false")
    monkeypatch.setenv("ALLOW_SQLITE_PRODUCT_RUNTIME_FOR_TESTS", "true")
    monkeypatch.setenv("REQUIRE_SELECTED_BACKGROUND_IDENTITY", "false")
    monkeypatch.setenv("STORAGE_DB_PATH", str(storage_db_path))
    monkeypatch.setenv("BILLING_DB_PATH", str(billing_db_path))
    monkeypatch.setenv("OBJECT_STORE_DIR", str(object_store_dir))
    for name in (
        "DATABASE_URL",
        "RENDER",
        "RENDER_SERVICE_ID",
        "RENDER_EXTERNAL_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(app_module, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(app_module, "MODEL_INPUT_DIR", model_input_dir)
    monkeypatch.setattr(app_module, "UPLOAD_DIR", upload_dir)
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    app_module.library_images.cache_clear()

    yield LiveSecurityFixture(
        client=app_module.app.test_client(),
        library_dir=library_dir,
        model_input_dir=model_input_dir,
        upload_dir=upload_dir,
    )

    app_module.library_images.cache_clear()


@dataclass(frozen=True)
class RouteSpies:
    generation_boundary: mock.Mock
    background_lookup: mock.Mock | None
    paid_providers: tuple[mock.Mock, ...]


def _install_route_spies(
    monkeypatch: pytest.MonkeyPatch,
    case: PreviewRouteCase,
) -> RouteSpies:
    generation_boundary = mock.Mock(
        name=case.generation_boundary,
        return_value=case.boundary_result,
    )
    monkeypatch.setattr(
        app_module,
        case.generation_boundary,
        generation_boundary,
    )

    background_lookup = None
    if case.resolves_background:
        background_lookup = mock.Mock(
            name="requested_selected_background",
            return_value=None,
        )
        monkeypatch.setattr(
            app_module,
            "requested_selected_background",
            background_lookup,
        )

    paid_providers = tuple(
        mock.Mock(name=provider_name)
        for provider_name in (
            "tencent_api_request",
            "tokenhub_image_request",
            "tencent_style_background",
            "tencent_text_to_image",
        )
    )
    for provider_name, provider_spy in zip(
        (
            "tencent_api_request",
            "tokenhub_image_request",
            "tencent_style_background",
            "tencent_text_to_image",
        ),
        paid_providers,
    ):
        monkeypatch.setattr(app_module, provider_name, provider_spy)

    return RouteSpies(
        generation_boundary=generation_boundary,
        background_lookup=background_lookup,
        paid_providers=paid_providers,
    )


def _route_url(
    case: PreviewRouteCase,
    menu_upload_id: str | None,
) -> str:
    if menu_upload_id is None:
        return case.path
    separator = "&" if "?" in case.path else "?"
    return f"{case.path}{separator}menuUploadId={menu_upload_id}"


def _create_real_customer_session(
    *,
    phone: str,
    ip: str,
) -> dict[str, Any]:
    conn = app_module.product_db_conn()
    try:
        challenge = auth_service.request_otp(
            conn,
            phone,
            ip=ip,
            user_agent="security-contract-test",
        )
        return auth_service.verify_otp(
            conn,
            challenge["challenge_id"],
            challenge["code"],
        )
    finally:
        conn.close()


def _auth_headers(session: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {session['session']['token']}",
    }


def _configure_remote_preview_storage(
    fixture: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> object_storage_service.ObjectStorageService:
    monkeypatch.setenv("OBJECT_STORAGE_PROVIDER", "cos")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "preview-test-private")
    monkeypatch.setenv("OBJECT_STORAGE_REGION", "ap-guangzhou")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_ID", "preview-test-id")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "preview-test-key")
    monkeypatch.setenv("OBJECT_STORAGE_PRIVATE", "true")
    monkeypatch.setenv("OBJECT_STORAGE_PUBLIC_READ", "false")
    monkeypatch.setenv(
        "OBJECT_SIGNING_SECRET",
        "preview-media-test-secret",
    )
    storage = object_storage_service.ObjectStorageService(
        fixture.library_dir.parent / "remote-preview-objects"
    )
    monkeypatch.setattr(
        app_module.object_storage_service,
        "get_object_storage_service",
        lambda *args, **kwargs: storage,
    )
    return storage


def _persist_owned_menu(
    fixture: LiveSecurityFixture,
    *,
    owner_user_id: str,
) -> str:
    menu_path = fixture.upload_dir / f"{owner_user_id}.xlsx"
    pd.DataFrame(
        [
            {
                "分类": "热销",
                "菜品名": "辣椒炒肉盖码饭",
                "价格": 19.8,
                "类型": "单品",
            }
        ]
    ).to_excel(menu_path, index=False)
    return app_module.persist_menu_upload(
        menu_path,
        original_filename="owner-menu.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        menu={
            "file": "owner-menu.xlsx",
            "store": "Owner Store",
            "count": 1,
            "kindCounts": {
                "single": 1,
                "combo": 0,
                "snack": 0,
                "total": 1,
            },
            "items": [
                {
                    "row": 2,
                    "name": "辣椒炒肉盖码饭",
                    "kind": "单品",
                }
            ],
        },
        owner_user_id=owner_user_id,
    )


def _security_violations(
    response: Any,
    spies: RouteSpies,
    *,
    expected_status: int,
    expected_code: str,
) -> list[str]:
    violations: list[str] = []
    if spies.generation_boundary.call_count:
        violations.append(
            "request reached generation boundary "
            f"{spies.generation_boundary._mock_name}"
        )
    if (
        spies.background_lookup is not None
        and spies.background_lookup.call_count
    ):
        violations.append(
            "request resolved selected-background state before authorization"
        )
    called_providers = [
        str(provider._mock_name)
        for provider in spies.paid_providers
        if provider.call_count
    ]
    if called_providers:
        violations.append(
            "request invoked paid provider(s): " + ", ".join(called_providers)
        )
    if response.status_code != expected_status:
        violations.append(
            f"expected HTTP {expected_status}, got HTTP {response.status_code}"
        )
    payload = response.get_json(silent=True)
    actual_code = payload.get("code") if isinstance(payload, dict) else None
    if actual_code != expected_code:
        violations.append(
            f"expected error code {expected_code!r}, got {actual_code!r}"
        )
    return violations


@pytest.mark.parametrize(
    "case",
    PREVIEW_ROUTE_CASES,
    ids=lambda case: case.name,
)
def test_live_preview_routes_reject_anonymous_before_generation(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: PreviewRouteCase,
) -> None:
    spies = _install_route_spies(monkeypatch, case)

    response = live_security_app.client.get(
        _route_url(case, SYNTACTIC_MENU_UPLOAD_ID),
        environ_base=REMOTE_ADDR,
    )

    violations = _security_violations(
        response,
        spies,
        expected_status=401,
        expected_code="auth_required",
    )
    assert not violations, "; ".join(violations)


@pytest.mark.parametrize(
    "case",
    PREVIEW_ROUTE_CASES,
    ids=lambda case: case.name,
)
def test_live_preview_routes_require_menu_upload_id(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: PreviewRouteCase,
) -> None:
    session = _create_real_customer_session(
        phone="13800138000",
        ip="203.0.113.51",
    )
    spies = _install_route_spies(monkeypatch, case)

    response = live_security_app.client.get(
        _route_url(case, None),
        headers=_auth_headers(session),
        environ_base=REMOTE_ADDR,
    )

    violations = _security_violations(
        response,
        spies,
        expected_status=400,
        expected_code="menu_upload_id_required",
    )
    assert not violations, "; ".join(violations)


@pytest.mark.parametrize(
    "case",
    PREVIEW_ROUTE_CASES,
    ids=lambda case: case.name,
)
def test_live_preview_routes_hide_another_customers_menu_upload(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: PreviewRouteCase,
) -> None:
    owner_session = _create_real_customer_session(
        phone="13800138000",
        ip="203.0.113.52",
    )
    other_session = _create_real_customer_session(
        phone="13900139000",
        ip="203.0.113.53",
    )
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=str(owner_session["user"]["id"]),
    )
    spies = _install_route_spies(monkeypatch, case)

    response = live_security_app.client.get(
        _route_url(case, menu_upload_id),
        headers=_auth_headers(other_session),
        environ_base=REMOTE_ADDR,
    )

    violations = _security_violations(
        response,
        spies,
        expected_status=404,
        expected_code="menu_upload_not_found",
    )
    assert not violations, "; ".join(violations)


@pytest.mark.parametrize(
    "case",
    PREVIEW_ROUTE_CASES,
    ids=lambda case: case.name,
)
def test_live_preview_routes_allow_owner_with_owned_menu_upload(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: PreviewRouteCase,
) -> None:
    owner_session = _create_real_customer_session(
        phone="13800138000",
        ip="203.0.113.54",
    )
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=str(owner_session["user"]["id"]),
    )
    spies = _install_route_spies(monkeypatch, case)

    response = live_security_app.client.get(
        _route_url(case, menu_upload_id),
        headers=_auth_headers(owner_session),
        environ_base=REMOTE_ADDR,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert spies.generation_boundary.call_count == 1
    assert all(
        provider.call_count == 0 for provider in spies.paid_providers
    )


@pytest.mark.parametrize(
    "relative_path",
    (
        "seed_public/style-1/public-seed.jpg",
        "demo_store/style-2/public-demo.png",
    ),
)
def test_media_anonymously_serves_only_public_seed_demo_allowlist(
    live_security_app: LiveSecurityFixture,
    relative_path: str,
) -> None:
    expected = f"public:{relative_path}".encode()
    target = live_security_app.library_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(expected)

    response = live_security_app.client.get(
        f"/media/{quote(relative_path, safe='/')}",
        environ_base=REMOTE_ADDR,
    )

    assert response.status_code == 200
    assert response.data == expected


@pytest.mark.parametrize(
    "relative_path",
    (
        "_ai_outputs/customer/job/output.jpg",
        "_generated_previews/customer/menu/sample.jpg",
        "_selected_backgrounds/customer/menu/background.jpg",
        "_selected_background_assets/customer/menu/background.jpg",
        "uploaded_123456/style-upload/customer-upload.jpg",
        "seed_public/style-1/public-seed.jpg.json",
    ),
)
def test_media_rejects_private_generated_uploaded_and_metadata_files(
    live_security_app: LiveSecurityFixture,
    relative_path: str,
) -> None:
    secret = f"private:{relative_path}".encode()
    target = live_security_app.library_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(secret)

    response = live_security_app.client.get(
        f"/media/{quote(relative_path, safe='/')}",
        environ_base=REMOTE_ADDR,
    )

    assert response.status_code in {403, 404}, (
        f"/media leaked private path {relative_path!r} with "
        f"HTTP {response.status_code}"
    )
    assert response.data != secret


@pytest.mark.parametrize(
    ("app_env", "render_marker"),
    (
        ("staging", None),
        ("production", None),
        (None, "srv-security-contract"),
    ),
    ids=("staging", "production", "render"),
)
def test_model_inputs_cannot_be_read_by_guessing_valid_names_in_live_runtime(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
    app_env: str | None,
    render_marker: str | None,
) -> None:
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    if render_marker is None:
        monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    else:
        monkeypatch.setenv("RENDER_SERVICE_ID", render_marker)

    guessed_name = ("a" * 24) + ".jpg"
    secret = b"private-provider-input"
    (live_security_app.model_input_dir / guessed_name).write_bytes(secret)

    response = live_security_app.client.get(
        f"/model-inputs/{guessed_name}",
        environ_base=REMOTE_ADDR,
    )

    assert response.status_code in {403, 404}, (
        "guessed model-input filename was readable in "
        f"{app_env or 'render'} with HTTP {response.status_code}"
    )
    assert response.data != secret


@pytest.mark.parametrize(
    "relative_path",
    (
        "_style_backgrounds/menu-owner/style-1/background.jpg",
        "_generated_previews/menu-owner/style-1/0001_dish.png",
    ),
    ids=("background", "free-sample"),
)
def test_private_preview_media_persists_recovers_and_requires_owner(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138000",
        ip="203.0.113.61",
    )
    other_session = _create_real_customer_session(
        phone="13900139000",
        ip="203.0.113.62",
    )
    principal = {
        "userId": str(owner_session["user"]["id"]),
        "localDemo": False,
    }
    with app_module.customer_preview_menu_path(
        None,
        principal,
        SYNTACTIC_MENU_UPLOAD_ID,
    ):
        template_parts = Path(relative_path).parts
        target = app_module.generation_cache_root(
            live_security_app.library_dir / template_parts[0],
            menu_key=template_parts[1],
        ).joinpath(*template_parts[2:])
        relative_path = app_module.private_preview_relative_name(target)
        expected = _image_bytes(target.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
        app_module.write_ai_output_metadata(
            target,
            {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": "TokenHubImageV3",
            },
        )
        record = app_module.persist_private_preview_asset(target)
        candidate = app_module.public_candidate_payload(
            {
                "url": f"/media/{relative_path}",
                "path": str(target),
            }
        )
    signed_url = str(candidate["url"])
    assert record is not None
    expected_object_key = (
        object_storage_service.private_preview_object_key(
            principal["userId"],
            SYNTACTIC_MENU_UPLOAD_ID,
            relative_path,
        )
    )
    assert storage.read_bytes(expected_object_key) == expected
    assert storage.exists(f"{expected_object_key}.json")

    target.unlink()
    app_module.ai_output_metadata_path(target).unlink()

    assert signed_url.startswith(
        f"/api/private-media/{relative_path}?token="
    )
    anonymous = live_security_app.client.get(
        signed_url,
        environ_base=REMOTE_ADDR,
    )
    cross_user = live_security_app.client.get(
        signed_url,
        headers=_auth_headers(other_session),
        environ_base=REMOTE_ADDR,
    )
    owner = live_security_app.client.get(
        signed_url,
        headers=_auth_headers(owner_session),
        environ_base=REMOTE_ADDR,
    )

    assert anonymous.status_code == 401
    assert cross_user.status_code == 403
    assert cross_user.get_json()["reason"] == "user_mismatch"
    assert owner.status_code == 200
    assert owner.data == expected
    assert owner.headers["Cache-Control"].startswith("private")
    assert target.read_bytes() == expected

    other_relative_path = relative_path.replace("style-1", "style-2")
    other_target = live_security_app.library_dir / other_relative_path
    other_target.parent.mkdir(parents=True, exist_ok=True)
    other_target.write_bytes(b"other-private-preview")
    parsed = urlsplit(signed_url)
    tampered_url = (
        f"/api/private-media/{other_relative_path}?{parsed.query}"
    )
    tampered = live_security_app.client.get(
        tampered_url,
        headers=_auth_headers(owner_session),
        environ_base=REMOTE_ADDR,
    )

    assert tampered.status_code == 403
    assert tampered.get_json()["reason"] == "asset_mismatch"


def test_six_generated_backgrounds_are_reused_after_restart(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138010",
        ip="203.0.113.70",
    )
    owner_user_id = str(owner_session["user"]["id"])
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=owner_user_id,
    )
    snapshot = app_module.resolve_menu_upload_snapshot(
        menu_upload_id,
        {"userId": owner_user_id, "localDemo": False},
    )
    menu_path = app_module.materialize_menu_upload_snapshot(snapshot)
    provider_calls: list[str] = []

    def generate_background(style_id: str, target: Path) -> dict[str, Any]:
        provider_calls.append(style_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (34, 120, 76)).save(
            target,
            format="JPEG",
        )
        return {
            "action": "TokenHubImageV3",
            "promptType": "category_background",
        }

    monkeypatch.setattr(app_module, "tencent_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "tencent_style_background",
        generate_background,
    )
    monkeypatch.setattr(
        app_module,
        "require_generated_output_quality",
        lambda target: {"status": "passed", "quality_score": 100},
    )
    monkeypatch.setattr(
        app_module,
        "persist_ai_generated_asset",
        lambda **kwargs: None,
    )
    principal = {"userId": owner_user_id, "localDemo": False}

    with app_module.customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        styles = tuple(app_module.STYLE_COLORS)
        generated = {
            style: app_module.style_sample_candidate(
                style,
                generate=True,
            )
            for style in styles
        }
        targets = {
            style: app_module.style_background_target(style)
            for style in styles
        }
        expected = {
            style: target.read_bytes()
            for style, target in targets.items()
        }
        for target in targets.values():
            target.unlink()
            app_module.ai_output_metadata_path(target).unlink()
        recovered = {
            style: app_module.style_sample_candidate(
                style,
                generate=False,
            )
            for style in styles
        }

    assert provider_calls == list(styles)
    for style in styles:
        assert generated[style]["generationStatus"] == "succeeded"
        assert recovered[style]["generationStatus"] == "succeeded"
        assert targets[style].read_bytes() == expected[style]
        relative_name = app_module.private_preview_relative_name(
            targets[style]
        )
        object_key = object_storage_service.private_preview_object_key(
            owner_user_id,
            menu_upload_id,
            relative_name,
        )
        assert storage.read_bytes(object_key) == expected[style]


def test_identical_menu_cache_never_crosses_preview_owners(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    first_session = _create_real_customer_session(
        phone="13800138020",
        ip="203.0.113.80",
    )
    second_session = _create_real_customer_session(
        phone="13800138021",
        ip="203.0.113.81",
    )
    first_owner = str(first_session["user"]["id"])
    second_owner = str(second_session["user"]["id"])
    first_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=first_owner,
    )
    shared_source = live_security_app.upload_dir / f"{first_owner}.xlsx"
    second_upload_id = app_module.persist_menu_upload(
        shared_source,
        original_filename="owner-menu.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        menu={
            "file": "owner-menu.xlsx",
            "store": "Owner Store",
            "count": 1,
            "kindCounts": {
                "single": 1,
                "combo": 0,
                "snack": 0,
                "total": 1,
            },
            "items": [
                {
                    "row": 2,
                    "name": "辣椒炒肉盖码饭",
                    "kind": "单品",
                }
            ],
        },
        owner_user_id=second_owner,
    )
    first_snapshot = app_module.resolve_menu_upload_snapshot(
        first_upload_id,
        {"userId": first_owner, "localDemo": False},
    )
    second_snapshot = app_module.resolve_menu_upload_snapshot(
        second_upload_id,
        {"userId": second_owner, "localDemo": False},
    )
    assert first_snapshot["sha256"] == second_snapshot["sha256"]
    first_menu_path = app_module.materialize_menu_upload_snapshot(
        first_snapshot
    )
    second_menu_path = app_module.materialize_menu_upload_snapshot(
        second_snapshot
    )
    provider_calls: list[str] = []

    def generate_background(style_id: str, target: Path) -> dict[str, Any]:
        provider_calls.append(style_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (18, 104, 168)).save(
            target,
            format="JPEG",
        )
        return {
            "action": "TokenHubImageV3",
            "promptType": "category_background",
        }

    monkeypatch.setattr(app_module, "tencent_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "tencent_style_background",
        generate_background,
    )
    monkeypatch.setattr(
        app_module,
        "require_generated_output_quality",
        lambda target: {"status": "passed", "quality_score": 100},
    )
    monkeypatch.setattr(
        app_module,
        "persist_ai_generated_asset",
        lambda **kwargs: None,
    )
    item = {
        "row": 2,
        "name": "辣椒炒肉盖码饭",
        "kind": "单品",
        "candidates": [],
    }

    with app_module.customer_preview_menu_path(
        first_menu_path,
        {"userId": first_owner, "localDemo": False},
        first_upload_id,
    ):
        first_candidate = app_module.style_sample_candidate(
            "style-1",
            generate=True,
        )
        first_target = app_module.style_background_target("style-1")
        first_preview_target = app_module.preview_output_target(
            item,
            "style-1",
        )
        _first_final_candidate, first_final_target = (
            app_module.ai_output_candidate(
                item,
                "style-1",
                "standard",
                "generated-final",
            )
        )

    with app_module.customer_preview_menu_path(
        second_menu_path,
        {"userId": second_owner, "localDemo": False},
        second_upload_id,
    ):
        with pytest.raises(
            app_module.PreviewObjectStorageError,
        ) as scope_error:
            app_module.ensure_private_preview_asset(first_target)
        assert scope_error.value.code == "preview_object_scope_mismatch"
        second_target = app_module.style_background_target("style-1")
        second_preview_target = app_module.preview_output_target(
            item,
            "style-1",
        )
        _second_final_candidate, second_final_target = (
            app_module.ai_output_candidate(
                item,
                "style-1",
                "standard",
                "generated-final",
            )
        )
        second_candidate = app_module.style_sample_candidate(
            "style-1",
            generate=False,
        )
        second_relative_name = app_module.private_preview_relative_name(
            second_target
        )
        second_object_key = (
            object_storage_service.private_preview_object_key(
                second_owner,
                second_upload_id,
                second_relative_name,
            )
        )

    assert first_candidate["generationStatus"] == "succeeded"
    assert first_target != second_target
    assert first_preview_target != second_preview_target
    assert first_final_target != second_final_target
    assert second_candidate["generationStatus"] == "pending"
    assert second_candidate["url"] == ""
    assert not second_target.exists()
    assert not storage.exists(second_object_key)
    assert provider_calls == ["style-1"]


def test_formal_generation_restores_selected_background_after_restart(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138015",
        ip="203.0.113.75",
    )
    owner_user_id = str(owner_session["user"]["id"])
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=owner_user_id,
    )
    snapshot = app_module.resolve_menu_upload_snapshot(
        menu_upload_id,
        {"userId": owner_user_id, "localDemo": False},
    )
    menu_path = app_module.materialize_menu_upload_snapshot(snapshot)
    provider_calls: list[str] = []

    def generate_background(style_id: str, target: Path) -> dict[str, Any]:
        provider_calls.append(style_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (32, 98, 166)).save(
            target,
            format="JPEG",
        )
        return {
            "action": "TokenHubImageV3",
            "promptType": "category_background",
        }

    monkeypatch.setattr(app_module, "tencent_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "tencent_style_background",
        generate_background,
    )
    monkeypatch.setattr(
        app_module,
        "require_generated_output_quality",
        lambda target: {"status": "passed", "quality_score": 100},
    )
    monkeypatch.setattr(
        app_module,
        "persist_ai_generated_asset",
        lambda **kwargs: None,
    )
    principal = {"userId": owner_user_id, "localDemo": False}
    with app_module.customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        candidate = app_module.style_sample_candidate(
            "style-1",
            generate=True,
        )
        target = app_module.style_background_target("style-1")
        expected = target.read_bytes()

    target.unlink()
    app_module.ai_output_metadata_path(target).unlink()

    def stop_after_background_recovery(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        raise ValueError("stop-after-background-recovery")

    monkeypatch.setattr(
        app_module,
        "generation_job_id",
        stop_after_background_recovery,
    )
    response = live_security_app.client.post(
        "/api/generation-jobs",
        json={
            "menuUploadId": menu_upload_id,
            "style": "style-1",
            "quality": "standard",
            "backgroundAssetId": candidate["backgroundAssetId"],
            "backgroundSha256": candidate["backgroundSha256"],
        },
        headers=_auth_headers(owner_session),
        environ_base=REMOTE_ADDR,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "stop-after-background-recovery"
    assert target.read_bytes() == expected
    assert provider_calls == ["style-1"]


def test_generated_free_sample_is_reused_from_private_storage_after_restart(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138011",
        ip="203.0.113.71",
    )
    owner_user_id = str(owner_session["user"]["id"])
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=owner_user_id,
    )
    snapshot = app_module.resolve_menu_upload_snapshot(
        menu_upload_id,
        {"userId": owner_user_id, "localDemo": False},
    )
    menu_path = app_module.materialize_menu_upload_snapshot(snapshot)
    provider_calls: list[str] = []

    def generate_sample(
        item: dict[str, Any],
        style_id: str,
        quality: str | None,
        target: Path,
    ) -> dict[str, Any]:
        del quality
        provider_calls.append(f"{style_id}:{item['name']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (210, 72, 45)).save(
            target,
            format="JPEG",
        )
        return {
            "action": "TokenHubImageV3",
            "promptType": "dish_generation",
        }

    monkeypatch.setattr(app_module, "tencent_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "tencent_text_to_image",
        generate_sample,
    )
    monkeypatch.setattr(
        app_module,
        "top_candidates",
        lambda item, library, limit=6, min_score=0.45: [],
    )
    item = {
        "row": 2,
        "name": "辣椒炒肉盖码饭",
        "kind": "单品",
        "candidates": [],
    }
    principal = {"userId": owner_user_id, "localDemo": False}

    with app_module.customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        candidate, generation = app_module.materialize_preview_candidate(
            item,
            "style-1",
        )
        target = app_module.preview_output_target(item, "style-1")
        relative_name = app_module.private_preview_relative_name(target)
        object_key = object_storage_service.private_preview_object_key(
            owner_user_id,
            menu_upload_id,
            relative_name,
        )
        expected = target.read_bytes()
        target.unlink()
        app_module.ai_output_metadata_path(target).unlink()
        recovered = app_module.generated_preview_candidate(
            item,
            "style-1",
        )

    assert candidate is not None
    assert generation["status"] == "succeeded"
    assert recovered is not None
    assert provider_calls == ["style-1:辣椒炒肉盖码饭"]
    assert target.read_bytes() == expected
    assert storage.read_bytes(object_key) == expected


def test_six_parallel_free_samples_keep_owner_context_and_recover(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138013",
        ip="203.0.113.73",
    )
    owner_user_id = str(owner_session["user"]["id"])
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=owner_user_id,
    )
    snapshot = app_module.resolve_menu_upload_snapshot(
        menu_upload_id,
        {"userId": owner_user_id, "localDemo": False},
    )
    menu_path = app_module.materialize_menu_upload_snapshot(snapshot)
    provider_calls: list[str] = []

    def generate_sample(
        item: dict[str, Any],
        style_id: str,
        quality: str | None,
        target: Path,
    ) -> dict[str, Any]:
        del quality
        provider_calls.append(str(item["name"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        color = (40 + int(item["row"]) % 180, 90, 160)
        Image.new("RGB", (64, 48), color).save(
            target,
            format="JPEG",
        )
        return {
            "action": "TokenHubImageV3",
            "promptType": f"dish_generation:{style_id}",
        }

    monkeypatch.setattr(app_module, "FINAL_GENERATION_WORKERS", 3)
    monkeypatch.setattr(app_module, "tencent_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "tencent_text_to_image",
        generate_sample,
    )
    monkeypatch.setattr(
        app_module,
        "top_candidates",
        lambda item, library, limit=6, min_score=0.45: [],
    )
    principal = {"userId": owner_user_id, "localDemo": False}

    with app_module.customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        generated = app_module.preview_samples(
            "style-1",
            generate=True,
        )
        shutil.rmtree(
            live_security_app.library_dir / "_generated_previews"
        )
        recovered = app_module.preview_samples(
            "style-1",
            generate=False,
        )

    assert len(generated["samples"]) == 6
    assert len(recovered["samples"]) == 6
    assert all(
        sample["generation"]["status"] == "succeeded"
        for sample in generated["samples"]
    )
    assert all(
        sample["candidate"] is not None
        for sample in recovered["samples"]
    )
    assert len(provider_calls) == 6
    stored_keys = storage.list_prefix(
        object_storage_service.PRIVATE_PREVIEWS_PREFIX
    )
    image_keys = [
        key for key in stored_keys if not key.endswith(".json")
    ]
    assert len(image_keys) == 6


def test_live_private_preview_storage_outage_never_serves_local_file(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138012",
        ip="203.0.113.72",
    )
    owner_user_id = str(owner_session["user"]["id"])
    principal = {"userId": owner_user_id, "localDemo": False}
    with app_module.customer_preview_menu_path(
        None,
        principal,
        SYNTACTIC_MENU_UPLOAD_ID,
    ):
        target = app_module.generation_cache_root(
            live_security_app.library_dir / "_style_backgrounds",
            menu_key="menu-owner",
        ) / "style-1" / "background.jpg"
        relative_path = app_module.private_preview_relative_name(target)
        expected = _image_bytes(target.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
        app_module.write_ai_output_metadata(
            target,
            {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": "TokenHubImageV3",
            },
        )
        app_module.persist_private_preview_asset(target)
        signed_url = app_module.public_candidate_payload(
            {"url": f"/media/{relative_path}"}
        )["url"]

    class UnavailableStorage:
        def read_bytes_if_exists(self, object_key: str) -> bytes | None:
            del object_key
            raise OSError("remote storage unavailable")

    assert storage.list_prefix(
        object_storage_service.PRIVATE_PREVIEWS_PREFIX
    )
    monkeypatch.setattr(
        app_module.object_storage_service,
        "get_object_storage_service",
        lambda *args, **kwargs: UnavailableStorage(),
    )

    response = live_security_app.client.get(
        signed_url,
        headers=_auth_headers(owner_session),
        environ_base=REMOTE_ADDR,
    )

    assert response.status_code == 503
    assert (
        response.get_json()["code"]
        == "preview_object_storage_unavailable"
    )
    assert response.data != expected


def test_live_background_generation_returns_503_when_persistence_fails(
    live_security_app: LiveSecurityFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_remote_preview_storage(
        live_security_app,
        monkeypatch,
    )
    owner_session = _create_real_customer_session(
        phone="13800138014",
        ip="203.0.113.74",
    )
    owner_user_id = str(owner_session["user"]["id"])
    menu_upload_id = _persist_owned_menu(
        live_security_app,
        owner_user_id=owner_user_id,
    )
    provider_calls: list[str] = []

    def generate_background(style_id: str, target: Path) -> dict[str, Any]:
        provider_calls.append(style_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), (80, 110, 140)).save(
            target,
            format="JPEG",
        )
        return {
            "action": "TokenHubImageV3",
            "promptType": "category_background",
        }

    class PreviewWriteFailure:
        def __getattr__(self, name: str) -> Any:
            return getattr(storage, name)

        def put_bytes(
            self,
            data_or_object_key: Any,
            data: Any = None,
            *,
            object_key: str | None = None,
            **kwargs: Any,
        ) -> str:
            if str(object_key or "").startswith(
                object_storage_service.PRIVATE_PREVIEWS_PREFIX
            ):
                raise OSError("preview object write failed")
            return storage.put_bytes(
                data_or_object_key,
                data,
                object_key=object_key,
                **kwargs,
            )

    monkeypatch.setattr(app_module, "tencent_ready", lambda: True)
    monkeypatch.setattr(
        app_module,
        "tencent_style_background",
        generate_background,
    )
    monkeypatch.setattr(
        app_module,
        "require_generated_output_quality",
        lambda target: {"status": "passed", "quality_score": 100},
    )
    monkeypatch.setattr(
        app_module,
        "persist_ai_generated_asset",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        app_module.object_storage_service,
        "get_object_storage_service",
        lambda *args, **kwargs: PreviewWriteFailure(),
    )

    response = live_security_app.client.get(
        (
            "/api/style-background?style=style-1&generate=1"
            f"&menuUploadId={menu_upload_id}"
        ),
        headers=_auth_headers(owner_session),
        environ_base=REMOTE_ADDR,
    )

    assert provider_calls == ["style-1"]
    assert response.status_code == 503
    assert (
        response.get_json()["code"]
        == "preview_object_storage_unavailable"
    )
