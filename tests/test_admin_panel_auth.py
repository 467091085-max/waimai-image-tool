from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from flask import Flask, Request

from admin_panel import (
    ADMIN_AI_ASSETS_READ_SCOPE,
    ADMIN_AI_ASSETS_WRITE_SCOPE,
    ADMIN_LIST_RESOURCES,
    ADMIN_LIST_RESOURCE_SCOPES,
    AdminAuthorization,
    AdminDependencies,
    AdminRequestAuthorizer,
    create_admin_blueprint,
)


ROOT = Path(__file__).resolve().parents[1]


def make_app(
    upload_dir: Path,
    authorizer: AdminRequestAuthorizer | None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.register_blueprint(
        create_admin_blueprint(
            AdminDependencies(
                library_images=lambda: [],
                media_url_for_path=lambda path: f"/media/{path.name}",
                current_menu_path=lambda: None,
                parse_menu=lambda _path=None: {
                    "file": "",
                    "store": "",
                    "count": 0,
                    "kindCounts": {},
                    "sheets": [],
                    "errors": [],
                    "demo": True,
                },
                upload_dir=upload_dir,
                db_path=upload_dir / "admin-auth.db",
                ai_asset_manifest_path=upload_dir / "missing-ai-assets.jsonl",
                request_authorizer=authorizer,
            )
        )
    )
    return app


def authorization(authenticated: bool, allowed: bool) -> AdminRequestAuthorizer:
    def authorize(_request: Request, _scope: str) -> AdminAuthorization:
        return AdminAuthorization(authenticated=authenticated, allowed=allowed)

    return authorize


def admin_get_paths() -> list[str]:
    return [
        "/admin",
        "/api/admin/library-sample",
        "/api/admin/menu-audit",
        "/api/admin/dashboard",
        "/api/admin/ai-assets",
        *(f"/api/admin/lists/{resource}" for resource in ADMIN_LIST_RESOURCES),
    ]


class AdminPanelAuthorizationTests(unittest.TestCase):
    def test_missing_authorizer_fails_closed_for_every_admin_get_and_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp), None).test_client()

            for path in admin_get_paths():
                with self.subTest(method="GET", path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.get_json()["code"], "admin_authentication_required")
                    self.assertIn("WWW-Authenticate", response.headers)

            response = client.post(
                "/api/admin/actions/ai-assets/asset-1/status",
                json={"status": "approved"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "admin_authentication_required")

    def test_authenticated_non_admin_is_forbidden_for_every_admin_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp), authorization(authenticated=True, allowed=False)).test_client()

            for path in admin_get_paths():
                with self.subTest(method="GET", path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.get_json()["code"], "admin_permission_required")

            response = client.post(
                "/api/admin/actions/ai-assets/asset-1/status",
                json={"status": "approved"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "admin_permission_required")

    def test_admin_is_allowed_for_every_get_and_list_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp), authorization(authenticated=True, allowed=True)).test_client()

            for path in admin_get_paths():
                with self.subTest(method="GET", path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)

            response = client.post(
                "/api/admin/actions/ai-assets/missing/status",
                json={"status": "approved"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "ai_asset_not_found")

    def test_head_requests_use_the_same_fail_closed_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            anonymous = make_app(Path(tmp), None).test_client()
            for path in admin_get_paths():
                with self.subTest(identity="anonymous", path=path):
                    self.assertEqual(anonymous.head(path).status_code, 401)

        with tempfile.TemporaryDirectory() as tmp:
            admin = make_app(Path(tmp), authorization(authenticated=True, allowed=True)).test_client()
            for path in admin_get_paths():
                with self.subTest(identity="admin", path=path):
                    response = admin.head(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_data(), b"")

    def test_authorizer_receives_minimum_scope_for_every_list_resource(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def authorize(flask_request: Request, scope: str) -> AdminAuthorization:
            calls.append((flask_request.method, flask_request.path, scope))
            return AdminAuthorization(authenticated=True, allowed=True)

        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp), authorize).test_client()
            for resource in ADMIN_LIST_RESOURCES:
                response = client.get(f"/api/admin/lists/{resource}")
                self.assertEqual(response.status_code, 200)

            self.assertEqual(
                {
                    path.rsplit("/", 1)[-1]: scope
                    for method, path, scope in calls
                    if method == "GET" and "/api/admin/lists/" in path
                },
                ADMIN_LIST_RESOURCE_SCOPES,
            )

            calls.clear()
            self.assertEqual(client.get("/api/admin/ai-assets").status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/admin/actions/ai-assets/missing/status",
                    json={"status": "approved"},
                ).status_code,
                404,
            )

        self.assertEqual(
            calls,
            [
                ("GET", "/api/admin/ai-assets", ADMIN_AI_ASSETS_READ_SCOPE),
                (
                    "POST",
                    "/api/admin/actions/ai-assets/missing/status",
                    ADMIN_AI_ASSETS_WRITE_SCOPE,
                ),
            ],
        )

    def test_authorizer_failure_is_generic_and_fails_closed(self) -> None:
        def broken_authorizer(_request: Request, _scope: str) -> AdminAuthorization:
            raise RuntimeError("secret-auth-backend-detail")

        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp), broken_authorizer).test_client()
            response = client.get("/api/admin/lists/users")

        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertEqual(data["code"], "admin_authorization_unavailable")
        payload = response.get_data(as_text=True).lower()
        self.assertNotIn("secret-auth-backend-detail", payload)
        self.assertNotIn("runtimeerror", payload)

    def test_malformed_authorizer_result_fails_closed(self) -> None:
        def malformed_authorizer(_request: Request, _scope: str) -> Any:
            return True

        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp), malformed_authorizer).test_client()
            response = client.get("/admin")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "admin_authorization_unavailable")


if __name__ == "__main__":
    unittest.main()
