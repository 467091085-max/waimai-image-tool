from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

import app as app_module
import asset_security
import auth_service as auth
import payment_service as payments


def save_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), (220, 120, 80)).save(path)


class SecurityRegressionTests(unittest.TestCase):
    def test_style_preview_sample_rejects_path_traversal_style(self) -> None:
        client = app_module.app.test_client()

        response = client.get("/api/style-preview-sample?style=../../tmp/pwn&index=0")

        self.assertEqual(response.status_code, 400)

    def test_plan_response_strips_candidate_paths(self) -> None:
        plan = {
            "results": [
                {
                    "name": "测试菜",
                    "candidates": [
                        {"imageId": "abc", "url": "/external-media/abc123.jpg", "path": "/private/source.jpg", "styleId": "style-1"}
                    ],
                    "componentMatches": [{"name": "测试", "candidates": [{"path": "/private/component.jpg", "url": "/media/a.jpg"}]}],
                }
            ],
            "styles": [{"id": "style-1", "sample": {"path": "/private/sample.jpg", "url": "/media/sample.jpg"}}],
        }

        payload = app_module.public_plan_payload(plan)

        self.assertNotIn("path", payload["results"][0]["candidates"][0])
        self.assertEqual(payload["results"][0]["candidates"][0]["url"], "")
        self.assertNotIn("path", payload["results"][0]["componentMatches"][0]["candidates"][0])
        self.assertNotIn("path", payload["styles"][0]["sample"])

    def test_public_payload_strips_object_storage_identifiers_recursively(self) -> None:
        plan = {
            "objectKey": "generated/job-1/top.jpg",
            "localPath": "/private/top.jpg",
            "generation": {
                "items": [
                    {
                        "dish": "测试菜",
                        "object_key": "generated/job-1/item.jpg",
                        "local_path": "/private/item.jpg",
                    }
                ]
            },
            "results": [
                {
                    "name": "测试菜",
                    "localObjectKey": "ai-assets/local.jpg",
                    "originalOutputPath": "/private/output.jpg",
                    "candidates": [
                        {
                            "imageId": "abc",
                            "url": "/media/seed_public/public.jpg",
                            "path": "/private/source.jpg",
                            "objectKey": "generated/job-1/dish.jpg",
                            "cosKey": "cos/private/dish.jpg",
                            "tencent": {
                                "safe": "kept",
                                "local_path": "/private/tencent.jpg",
                                "nested": [{"object_key": "generated/nested.jpg"}],
                            },
                        }
                    ],
                    "componentMatches": [
                        {
                            "name": "测试",
                            "candidates": [
                                {
                                    "url": "/media/seed_public/component.jpg",
                                    "path": "/private/component.jpg",
                                    "storageKey": "storage/private/component.jpg",
                                }
                            ],
                        }
                    ],
                }
            ],
            "styles": [
                {
                    "id": "style-1",
                    "sample": {
                        "url": "/media/seed_public/sample.jpg",
                        "path": "/private/sample.jpg",
                        "sourcePath": "/private/source-sample.jpg",
                    },
                }
            ],
        }

        payload = app_module.public_plan_payload(plan)
        raw = json.dumps(payload, ensure_ascii=False)

        for key in (
            "path",
            "localPath",
            "local_path",
            "objectKey",
            "object_key",
            "localObjectKey",
            "originalOutputPath",
            "sourcePath",
            "storageKey",
            "cosKey",
        ):
            self.assertNotIn(f'"{key}"', raw)
        for leaked_value in (
            "/private/",
            "generated/job-1",
            "cos/private",
            "storage/private",
            "ai-assets/local.jpg",
        ):
            self.assertNotIn(leaked_value, raw)
        self.assertEqual(
            payload["results"][0]["candidates"][0]["url"],
            "/media/seed_public/public.jpg",
        )
        self.assertEqual(payload["results"][0]["candidates"][0]["tencent"]["safe"], "kept")

    def test_external_media_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "external" / "菜品.jpg"
            save_image(external)
            image_id = hashlib.sha1(str(external.resolve()).encode()).hexdigest()[:18]

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root / "library"),
                mock.patch.object(app_module, "configured_library_dirs", return_value=[root / "external"]),
                mock.patch.object(app_module, "ensure_demo_data"),
            ):
                app_module.library_images.cache_clear()
                self.addCleanup(app_module.library_images.cache_clear)

                self.assertEqual(app_module.media_url_for_path(external), "")
                self.assertIsNone(app_module.external_image_path(f"{image_id}.jpg"))

    def test_private_media_url_round_trips_reserved_filename_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            target = (
                library
                / "_generated_previews"
                / "menu"
                / "0003_收藏福利#豆泡?加料%优惠.png"
            )
            save_image(target)

            with mock.patch.object(app_module, "LIBRARY_DIR", library):
                url = app_module.media_url_for_path(target)
                relative_name = app_module.library_media_relative_name(url)
                resolved = app_module.safe_library_media_path(
                    relative_name,
                    allowed_prefixes=app_module.PRIVATE_MEDIA_PREFIXES,
                )

            self.assertIn("%23", url)
            self.assertIn("%3F", url)
            self.assertIn("%25", url)
            self.assertEqual(
                relative_name,
                "_generated_previews/menu/0003_收藏福利#豆泡?加料%优惠.png",
            )
            self.assertEqual(resolved, target.resolve())

    def test_style_preview_source_candidates_hide_seed_when_public_candidates_exist(self) -> None:
        candidates = [
            {
                "source": "internal",
                "store": "seed_demo",
                "url": "/media/seed_demo/style-1/菜.jpg",
                "path": "/private/data/library/seed_demo/style-1/菜.jpg",
            },
            {
                "source": "clean",
                "store": "真实门店",
                "url": "https://cdn.example.test/gallery/real.jpg",
                "remoteUrl": "https://cdn.example.test/gallery/real.jpg",
            },
        ]

        visible = app_module.visible_source_candidates(candidates, 3)

        self.assertEqual(visible, [candidates[1]])

    def test_billing_write_routes_require_server_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = app_module.app.test_client()
            with mock.patch.dict(app_module.os.environ, {"BILLING_DB_PATH": str(Path(tmp) / "billing.db")}, clear=False):
                response = client.post("/api/recharge", json={"points": 1000, "userId": "attacker"})

                self.assertEqual(response.status_code, 403)
                self.assertEqual(app_module.billing.get_account("attacker")["balance"], 0)

    def test_remote_request_cannot_use_default_local_demo_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "billing.db"
            client = app_module.app.test_client()
            with mock.patch.dict(
                app_module.os.environ,
                {
                    "BILLING_DB_PATH": str(db_path),
                    "BILLING_API_TOKEN": "",
                    "ADMIN_API_TOKEN": "",
                    "ENABLE_LOCAL_DEMO_BILLING": "true",
                },
                clear=False,
            ):
                response = client.post(
                    "/api/recharge",
                    json={"points": 1000},
                    environ_base={"REMOTE_ADDR": "203.0.113.20"},
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.get_json()["code"],
                    "billing_write_forbidden",
                )
                self.assertEqual(
                    app_module.billing.get_account(
                        app_module.billing.DEFAULT_USER_ID
                    )["balance"],
                    0,
                )

    def test_remote_image_read_stops_at_byte_limit_before_pillow(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * 9

        with (
            mock.patch.object(app_module, "MAX_AI_ASSET_BYTES", 8),
            mock.patch.object(
                app_module.urllib.request,
                "urlopen",
                return_value=response,
            ),
            mock.patch.object(app_module.Image, "open") as image_open,
        ):
            with self.assertRaisesRegex(ValueError, "remote image exceeds limit"):
                app_module.read_remote_pil_image(
                    "https://provider.example.test/result.png"
                )

        response.read.assert_called_once_with(9)
        image_open.assert_not_called()

    def test_tokenhub_json_read_stops_at_limit_before_json_decode(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * 9

        with (
            mock.patch.object(app_module, "MAX_PROVIDER_JSON_BYTES", 8),
            mock.patch.object(
                app_module,
                "tokenhub_config",
                return_value={"api_key": "test-key"},
            ),
            mock.patch.object(app_module, "tokenhub_ready", return_value=True),
            mock.patch.object(
                app_module.urllib.request,
                "urlopen",
                return_value=response,
            ),
            mock.patch.object(app_module.json, "loads") as json_loads,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "TokenHub response exceeds size limit",
            ):
                app_module.tokenhub_http_post(
                    "https://provider.example.test/image",
                    {"prompt": "dish"},
                )

        response.read.assert_called_once_with(9)
        json_loads.assert_not_called()

    def test_generation_watermark_uses_bounded_object_read(self) -> None:
        storage = mock.Mock()
        storage.read_bytes_limited.side_effect = (
            app_module.object_storage_service.ObjectStorageReadLimitExceeded(
                "object exceeds read limit"
            )
        )
        object_key = (
            "originals/watermarks/owner/"
            + ("a" * 64)
            + ".png"
        )

        with mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ):
            with self.assertRaises(app_module.MenuUploadError) as raised:
                app_module.generation_contract_export_watermark(
                    {
                        "watermark": {
                            "enabled": True,
                            "type": "logo",
                            "logoObjectKey": object_key,
                        }
                    }
                )

        self.assertEqual(
            raised.exception.code,
            "generation_watermark_unavailable",
        )
        storage.read_bytes_limited.assert_called_once_with(
            object_key,
            app_module.MAX_LOGO_BYTES,
        )
        storage.read_bytes.assert_not_called()

    def test_image_fingerprint_rejects_declared_pixel_bomb_before_load(
        self,
    ) -> None:
        image = mock.MagicMock()
        image.__enter__.return_value = image
        image.size = (100_000, 100_000)
        image.format = "PNG"

        with mock.patch.object(
            app_module.Image,
            "open",
            return_value=image,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "image dimensions exceed limit",
            ):
                app_module.image_bytes_fingerprint(b"small-image-header")

        image.load.assert_not_called()

    def test_image_fingerprint_rejects_bytes_before_pillow(self) -> None:
        with (
            mock.patch.object(app_module, "MAX_AI_ASSET_BYTES", 8),
            mock.patch.object(app_module.Image, "open") as image_open,
        ):
            with self.assertRaisesRegex(ValueError, "image bytes exceed limit"):
                app_module.image_bytes_fingerprint(b"x" * 9)

        image_open.assert_not_called()

    def test_generate_final_rejects_unauthorized_tencent_generation(self) -> None:
        client = app_module.app.test_client()
        with (
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "materialize_final_images") as materialize,
        ):
            response = client.post("/api/generate-final", json={"style": "style-1"})

        self.assertEqual(response.status_code, 403)
        materialize.assert_not_called()

    def test_auth_request_otp_requires_sms_provider_for_nonlocal_clients_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "storage.sqlite3"
            client = app_module.app.test_client()
            with mock.patch.dict(os.environ, {"STORAGE_DB_PATH": str(db_path)}, clear=False):
                os.environ.pop("AUTH_EXPOSE_MOCK_OTP", None)
                os.environ.pop("SMS_PROVIDER", None)
                response = client.post(
                    "/api/auth/request-otp",
                    json={"phone": "13800138000"},
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["code"], "sms_provider_unavailable")
        self.assertNotIn("mockCode", payload)
        self.assertFalse(db_path.exists())

    def test_growth_write_routes_require_admin_or_session_for_nonlocal_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = app_module.app.test_client()
            with mock.patch.dict(
                os.environ,
                {
                    "STORAGE_DB_PATH": str(Path(tmp) / "storage.sqlite3"),
                    "ADMIN_API_TOKEN": "",
                },
                clear=False,
            ):
                agent_response = client.post(
                    "/api/growth/agents",
                    json={"userId": "attacker"},
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )
                invite_response = client.post(
                    "/api/growth/invites/accept",
                    json={"inviterUserId": "attacker", "inviteeUserId": "default"},
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )

        self.assertIn(agent_response.status_code, {401, 403})
        self.assertIn(invite_response.status_code, {401, 403})

    def test_download_requires_signing_token_for_nonlocal_requests_even_without_secret_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "exports"
            export_dir.mkdir()
            (export_dir / "ok.zip").write_bytes(b"zip-bytes")

            with (
                mock.patch.object(app_module, "EXPORT_DIR", export_dir),
                mock.patch.dict(
                    os.environ,
                    {"DOWNLOAD_SIGNING_SECRET": "", "ASSET_SIGNING_SECRET": ""},
                    clear=False,
                ),
            ):
                client = app_module.app.test_client()
                response = client.get(
                    "/download/ok.zip",
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )

        self.assertIn(response.status_code, {401, 403, 503})
        self.assertNotEqual(response.data, b"zip-bytes")

    def test_download_token_is_bound_to_requested_export_name(self) -> None:
        secret = "download-secret"
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "exports"
            export_dir.mkdir()
            (export_dir / "ok.zip").write_bytes(b"ok-zip")
            (export_dir / "other.zip").write_bytes(b"other-zip")
            token = asset_security.sign_asset_url(
                {
                    "asset_id": "ok.zip",
                    "user_id": "user-1",
                    "order_id": "order-1",
                    "variant": asset_security.EXPORT,
                    "purpose": asset_security.EXPORT,
                    "expires_at": int(time.time()) + 60,
                    "nonce": "nonce-1",
                },
                secret,
            )

            with (
                mock.patch.object(app_module, "EXPORT_DIR", export_dir),
                mock.patch.dict(os.environ, {"DOWNLOAD_SIGNING_SECRET": secret}, clear=False),
            ):
                client = app_module.app.test_client()
                response = client.get(
                    "/download/other.zip",
                    query_string={"token": token},
                    headers={"X-User-Id": "user-1"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "asset_mismatch")
        self.assertNotEqual(response.data, b"other-zip")

    def test_object_sign_route_requires_server_token_for_nonlocal_requests_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = app_module.app.test_client()
            with mock.patch.dict(
                os.environ,
                {
                    "STORAGE_DB_PATH": str(Path(tmp) / "storage.sqlite3"),
                    "OBJECT_SIGNING_SECRET": "object-secret",
                    "OBJECT_API_TOKEN": "",
                    "ADMIN_API_TOKEN": "",
                },
                clear=False,
            ):
                os.environ.pop("ENABLE_LOCAL_DEMO_BILLING", None)
                os.environ.pop("ENABLE_LOCAL_DEMO_OBJECTS", None)
                response = client.post(
                    "/api/objects/sign",
                    json={
                        "objectKey": "generated/job-1/dish.jpg",
                        "purpose": asset_security.PREVIEW,
                        "variant": asset_security.PREVIEW,
                    },
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload["code"], "object_write_forbidden")

    def test_object_sign_route_rejects_path_traversal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = app_module.app.test_client()
            with mock.patch.dict(
                os.environ,
                {
                    "STORAGE_DB_PATH": str(Path(tmp) / "storage.sqlite3"),
                    "OBJECT_SIGNING_SECRET": "object-secret",
                    "OBJECT_API_TOKEN": "object-token",
                    "ADMIN_API_TOKEN": "",
                },
                clear=False,
            ):
                response = client.post(
                    "/api/objects/sign",
                    json={
                        "objectKey": "../secret.txt",
                        "purpose": asset_security.PREVIEW,
                        "variant": asset_security.PREVIEW,
                    },
                    headers={"X-Object-Token": "object-token"},
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "invalid_object_access_request")
        self.assertNotIn("../secret.txt", response.get_data(as_text=True))

    def test_object_sign_route_does_not_return_raw_object_key_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = app_module.app.test_client()
            with mock.patch.dict(
                os.environ,
                {
                    "STORAGE_DB_PATH": str(Path(tmp) / "storage.sqlite3"),
                    "OBJECT_SIGNING_SECRET": "object-secret",
                    "OBJECT_API_TOKEN": "object-token",
                    "ADMIN_API_TOKEN": "",
                },
                clear=False,
            ):
                response = client.post(
                    "/api/objects/sign",
                    json={
                        "objectKey": "generated/job-1/dish.jpg",
                        "purpose": asset_security.PREVIEW,
                        "variant": asset_security.PREVIEW,
                    },
                    headers={"X-Object-Token": "object-token"},
                    environ_base={"REMOTE_ADDR": "203.0.113.10"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["token"])
        self.assertTrue(payload["url"].startswith("/objects/generated/job-1/dish.jpg?token="))
        self.assertNotIn("object_key", payload)
        self.assertNotIn("objectKey", payload)

    def test_object_access_token_is_bound_to_requested_object_key(self) -> None:
        secret = "object-secret"
        with tempfile.TemporaryDirectory() as tmp:
            object_root = Path(tmp) / "objects"
            first = object_root / "generated" / "job-1" / "dish.jpg"
            second = object_root / "generated" / "job-1" / "other.jpg"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"first-image")
            second.write_bytes(b"second-image")
            access = app_module.object_storage_service.create_signed_access(
                "generated/job-1/dish.jpg",
                "user-1",
                asset_security.PREVIEW,
                asset_security.PREVIEW,
                secret,
            )

            client = app_module.app.test_client()
            with mock.patch.dict(
                os.environ,
                {
                    "OBJECT_STORE_DIR": str(object_root),
                    "OBJECT_SIGNING_SECRET": secret,
                    "ASSET_SIGNING_SECRET": "",
                    "DOWNLOAD_SIGNING_SECRET": "",
                },
                clear=False,
            ):
                response = client.get(
                    "/objects/generated/job-1/other.jpg",
                    query_string={"token": access["token"]},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason"], "object_key_mismatch")
        self.assertNotEqual(response.data, b"second-image")

    def test_otp_rejects_invalid_phone_before_creating_challenge_or_user(self) -> None:
        conn = sqlite3.connect(":memory:")
        auth.init_auth_schema(conn)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            bad_numbers = [
                "8613800138000",
                "+8623800138000",
                "1380013800x",
                "1380013800",
                "138001380000",
            ]
            for phone in bad_numbers:
                with self.subTest(phone=phone):
                    with self.assertRaises(auth.AuthError) as error:
                        auth.request_otp(conn, phone, now=now)
                    self.assertEqual(error.exception.code, auth.ERR_INVALID_PHONE)

            self.assertEqual(conn.execute("SELECT COUNT(*) FROM otp_challenges").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        finally:
            conn.close()

    def test_fixed_mock_otp_code_does_not_verify_by_default(self) -> None:
        conn = sqlite3.connect(":memory:")
        auth.init_auth_schema(conn)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            challenge = auth.request_otp(conn, "13800138000", now=now)
            guessed_code = "123456" if challenge["code"] != "123456" else "000000"

            with self.assertRaises(auth.AuthError) as error:
                auth.verify_otp(conn, challenge["challenge_id"], guessed_code, now=now)

            self.assertEqual(error.exception.code, auth.ERR_OTP_MISMATCH)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        finally:
            conn.close()

    def test_payment_callback_requires_signature_secret_configuration(self) -> None:
        conn = sqlite3.connect(":memory:")
        payments.init_payment_schema(conn)
        try:
            order = payments.create_payment_order(
                conn,
                user_id="user-1",
                amount_cents=4900,
                points=500,
                order_id="order-1",
            )

            with self.assertRaises(payments.PaymentSignatureError):
                payments.handle_payment_callback(
                    conn,
                    "fake",
                    order["provider_order_id"],
                    "pay_success",
                    {"event_id": "evt-unsigned"},
                    secret="",
                )

            self.assertEqual(self._payment_order_status(conn, "order-1"), "pending")
            self.assertEqual(self._table_count(conn, "payment_events"), 0)
        finally:
            conn.close()

    def test_payment_callback_route_rejects_unsigned_callback_when_webhook_secret_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage_db_path = Path(tmp) / "storage.sqlite3"
            billing_db_path = Path(tmp) / "billing.sqlite3"
            client = app_module.app.test_client()
            with mock.patch.dict(
                os.environ,
                {
                    "STORAGE_DB_PATH": str(storage_db_path),
                    "BILLING_DB_PATH": str(billing_db_path),
                    "FAKE_PAYMENT_WEBHOOK_SECRET": "",
                    "PAYMENT_WEBHOOK_SECRET": "",
                },
                clear=False,
            ):
                conn = app_module.storage_db.get_conn(storage_db_path)
                try:
                    payments.create_catalog_payment_order(
                        conn,
                        authenticated_user_id="user-1",
                        request={"packageId": "starter-500"},
                        provider="fake",
                        order_id="order-1",
                    )
                finally:
                    conn.close()

                callback_response = client.post(
                    "/api/payments/fake-callback",
                    json={
                        "providerOrderId": "order-1",
                        "eventType": "pay_success",
                        "eventId": "evt-unsigned",
                    },
                )

                self.assertIn(callback_response.status_code, {403, 503})
                self.assertEqual(app_module.billing.get_account("user-1")["balance"], 0)

    def test_payment_repeated_success_with_new_event_id_does_not_credit_twice(self) -> None:
        conn = sqlite3.connect(":memory:")
        payments.init_payment_schema(conn)
        secret = "payment-secret"
        try:
            order = payments.create_payment_order(
                conn,
                user_id="user-1",
                amount_cents=4900,
                points=500,
                order_id="order-1",
            )
            first = payments.handle_payment_callback(
                conn,
                "fake",
                order["provider_order_id"],
                "pay_success",
                self._signed_payment_payload(secret, order["provider_order_id"], "pay_success", "evt-paid-1"),
                secret=secret,
            )
            replay = payments.handle_payment_callback(
                conn,
                "fake",
                order["provider_order_id"],
                "pay_success",
                self._signed_payment_payload(secret, order["provider_order_id"], "pay_success", "evt-paid-2"),
                secret=secret,
            )

            self.assertEqual(first["points_to_credit"], 500)
            self.assertEqual(replay["previous_status"], "paid")
            self.assertEqual(replay["points_to_credit"], 0)
            self.assertEqual(self._payment_order_status(conn, "order-1"), "paid")
            self.assertEqual(self._table_count(conn, "payment_events"), 2)
        finally:
            conn.close()

    def test_admin_mutating_routes_are_not_public_without_admin_token(self) -> None:
        unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}
        admin_routes = [rule.rule for rule in app_module.app.url_map.iter_rules() if rule.rule.startswith("/api/admin")]
        self.assertIn("/api/admin/dashboard", admin_routes)

        client = app_module.app.test_client()
        with mock.patch.dict(os.environ, {"ADMIN_API_TOKEN": "", "ENABLE_LOCAL_ADMIN_WRITES": ""}, clear=False):
            for rule in app_module.app.url_map.iter_rules():
                if not rule.rule.startswith("/api/admin"):
                    continue
                for method in sorted((rule.methods or set()) & unsafe_methods):
                    with self.subTest(route=rule.rule, method=method):
                        response = client.open(
                            self._concrete_route_path(rule.rule),
                            method=method,
                            json={},
                            environ_base={"REMOTE_ADDR": "203.0.113.10"},
                        )
                        self.assertIn(response.status_code, {401, 403})

    def test_invalid_ai_asset_status_checks_authorization_before_input(self) -> None:
        client = app_module.app.test_client()
        with mock.patch.dict(
            os.environ,
            {"ADMIN_API_TOKEN": "", "ENABLE_LOCAL_ADMIN_WRITES": ""},
            clear=False,
        ):
            response = client.post(
                "/api/admin/actions/ai-assets/asset-1/status",
                json={},
                environ_base={"REMOTE_ADDR": "203.0.113.10"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json(silent=True)["code"],
            "admin_permission_forbidden",
        )

    def _signed_payment_payload(
        self,
        secret: str,
        provider_order_id: str,
        event_type: str,
        event_id: str,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"event_id": event_id}
        payload["signature"] = payments.fake_callback_signature(
            "fake",
            provider_order_id,
            event_type,
            payload,
            secret,
        )
        return payload

    def _payment_order_status(self, conn: sqlite3.Connection, order_id: str) -> str:
        row = conn.execute("SELECT status FROM payment_orders WHERE order_id = ?", (order_id,)).fetchone()
        return str(row[0])

    def _table_count(self, conn: sqlite3.Connection, table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def _concrete_route_path(self, rule: str) -> str:
        return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"test-\1", rule)


if __name__ == "__main__":
    unittest.main()
