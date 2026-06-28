from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import asset_security
import storage_db


class DownloadRouteTests(unittest.TestCase):
    def test_download_without_signing_secret_allows_local_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")

            with self._download_env("", ""), mock.patch.object(app_module, "EXPORT_DIR", export_dir):
                client = app_module.app.test_client()

                response = client.get("/download/ok.zip")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b"zip-bytes")
                response.close()

    def test_download_with_signing_secret_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")

            with self._download_env("download-secret", ""), mock.patch.object(app_module, "EXPORT_DIR", export_dir):
                client = app_module.app.test_client()

                response = client.get("/download/ok.zip", headers={"X-User-Id": "user-1"})

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["reason"], "missing_token")
                self.assertNotIn(str(export_dir), response.get_data(as_text=True))
                response.close()

    def test_download_with_valid_export_token_allows_download(self) -> None:
        secret = "download-secret"
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")
            token = self._token(secret, "ok.zip", "user-1")

            with self._download_env(secret, ""), mock.patch.object(app_module, "EXPORT_DIR", export_dir):
                client = app_module.app.test_client()

                response = client.get(
                    "/download/ok.zip",
                    query_string={"token": token},
                    headers={"X-User-Id": "user-1"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b"zip-bytes")
                response.close()

    def test_download_with_wrong_user_token_is_forbidden(self) -> None:
        secret = "download-secret"
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")
            token = self._token(secret, "ok.zip", "user-2")

            with self._download_env(secret, ""), mock.patch.object(app_module, "EXPORT_DIR", export_dir):
                client = app_module.app.test_client()

                response = client.get(
                    "/download/ok.zip",
                    query_string={"token": token},
                    headers={"X-User-Id": "user-1"},
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["reason"], "user_mismatch")
                self.assertNotIn(str(export_dir), response.get_data(as_text=True))
                response.close()

    def test_download_with_invalid_token_is_forbidden(self) -> None:
        secret = "download-secret"
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")

            with self._download_env(secret, ""), mock.patch.object(app_module, "EXPORT_DIR", export_dir):
                client = app_module.app.test_client()

                response = client.get(
                    "/download/ok.zip",
                    query_string={"token": "not-a-token"},
                    headers={"X-User-Id": "user-1"},
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["reason"], "invalid_token")
                self.assertNotIn(str(export_dir), response.get_data(as_text=True))
                response.close()

    def test_download_serves_only_export_directory_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")
            secret = root / "secret.txt"
            secret.write_text("do not serve", encoding="utf-8")

            with self._download_env("", ""), mock.patch.object(app_module, "EXPORT_DIR", export_dir):
                client = app_module.app.test_client()

                traversal = client.get("/download/../secret.txt")
                self.assertEqual(traversal.status_code, 404)
                self.assertNotIn("do not serve", traversal.get_data(as_text=True))
                traversal.close()

                encoded_traversal = client.get("/download/%2e%2e/secret.txt")
                self.assertEqual(encoded_traversal.status_code, 404)
                self.assertNotIn("do not serve", encoded_traversal.get_data(as_text=True))
                encoded_traversal.close()

    def test_download_records_allowed_and_denied_asset_access_audit(self) -> None:
        secret = "download-secret"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_dir = self._export_dir(tmp)
            db_path = root / "storage.sqlite3"
            self._write_export(export_dir, "ok.zip", b"zip-bytes")
            token = self._token(secret, "ok.zip", "user-1")

            with (
                self._download_env(secret, ""),
                mock.patch.dict(os.environ, {"STORAGE_DB_PATH": str(db_path)}, clear=False),
                mock.patch.object(app_module, "EXPORT_DIR", export_dir),
            ):
                client = app_module.app.test_client()
                denied = client.get("/download/ok.zip", headers={"X-User-Id": "user-1"})
                self.assertEqual(denied.status_code, 403)
                denied.close()

                allowed = client.get(
                    "/download/ok.zip",
                    query_string={"token": token},
                    headers={"X-User-Id": "user-1"},
                )
                self.assertEqual(allowed.status_code, 200)
                allowed.close()

            rows = self._asset_access_rows(db_path)
            self.assertEqual([(row["asset_id"], row["action"], row["allowed"], row["deny_reason"]) for row in rows], [
                ("ok.zip", "export", 0, "missing_token"),
                ("ok.zip", "export", 1, ""),
            ])

    def test_export_api_returns_signed_download_url_when_secret_configured(self) -> None:
        secret = "download-secret"
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = self._export_dir(tmp)
            self._write_export(export_dir, "ok.zip", b"zip-bytes")

            with (
                self._download_env(secret, ""),
                mock.patch.object(app_module, "EXPORT_DIR", export_dir),
                mock.patch.object(app_module, "build_plan", return_value={"results": []}),
                mock.patch.object(app_module, "prepare_results_for_export", return_value=[]),
                mock.patch.object(
                    app_module,
                    "export_delivery_zip",
                    return_value={"rows": 0, "images": 0, "platforms": ["meituan"], "download": "/download/ok.zip"},
                ),
            ):
                client = app_module.app.test_client()
                response = client.post("/api/export", json={"style": ""}, headers={"X-User-Id": "user-1"})
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertIn("token=", payload["download"])

                download = client.get(payload["download"], headers={"X-User-Id": "user-1"})
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.data, b"zip-bytes")
                download.close()

    def _download_env(self, download_secret: str, asset_secret: str):
        return mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_SIGNING_SECRET": download_secret,
                "ASSET_SIGNING_SECRET": asset_secret,
            },
        )

    def _export_dir(self, root: str) -> Path:
        export_dir = Path(root) / "exports"
        export_dir.mkdir()
        return export_dir

    def _write_export(self, export_dir: Path, name: str, data: bytes) -> None:
        target = export_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _token(self, secret: str, asset_id: str, user_id: str) -> str:
        return asset_security.sign_asset_url(
            {
                "asset_id": asset_id,
                "user_id": user_id,
                "order_id": "order-1",
                "variant": asset_security.EXPORT,
                "purpose": asset_security.EXPORT,
                "expires_at": int(time.time()) + 60,
                "nonce": "nonce-1",
            },
            secret,
        )

    def _asset_access_rows(self, db_path: Path):
        conn = storage_db.get_conn(db_path)
        try:
            return conn.execute(
                """
                SELECT asset_id, action, allowed, deny_reason
                FROM asset_access_logs
                ORDER BY rowid ASC
                """
            ).fetchall()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
