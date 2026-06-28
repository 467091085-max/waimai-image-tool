from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import asset_security
from object_storage_service import (
    BUCKET_PREFIXES,
    GENERATED_PREFIX,
    MENUS_PREFIX,
    ORIGINALS_PREFIX,
    ObjectStorageService,
    assess_object_storage_readiness,
    create_signed_access,
    verify_signed_access,
)


class ObjectStorageServiceTests(unittest.TestCase):
    def test_put_read_list_stat_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ObjectStorageService(Path(tmp) / "objects")
            key = "menus/store-1/menu.json"

            stored_key = service.put_bytes(b'{"name":"demo"}', object_key=key)

            self.assertEqual(stored_key, key)
            self.assertTrue(service.exists(key))
            self.assertEqual(service.read_bytes(key), b'{"name":"demo"}')
            self.assertEqual(service.list_prefix(MENUS_PREFIX), [key])
            self.assertEqual(service.list_prefix("menus/store-1"), [key])
            self.assertEqual(service.stat(key)["size"], len(b'{"name":"demo"}'))
            self.assertEqual(service.stat(key)["bucket"], MENUS_PREFIX)

            self.assertTrue(service.delete(key))
            self.assertFalse(service.exists(key))
            self.assertFalse(service.delete(key))
            self.assertEqual(service.list_prefix(MENUS_PREFIX), [])

    def test_put_file_uses_bucket_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "dish.jpg"
            source.write_bytes(b"jpeg-bytes")
            service = ObjectStorageService(Path(tmp) / "objects")

            key = service.put_file(source, prefix=ORIGINALS_PREFIX, filename="dish.jpg")

            self.assertTrue(key.startswith(ORIGINALS_PREFIX))
            self.assertEqual(service.read_bytes(key), b"jpeg-bytes")
            self.assertEqual(service.stat(key)["bucket"], ORIGINALS_PREFIX)
            self.assertIn(GENERATED_PREFIX, BUCKET_PREFIXES)

    def test_rejects_path_traversal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ObjectStorageService(Path(tmp) / "objects")
            bad_keys = [
                "",
                "../secret.txt",
                "menus/../../secret.txt",
                "/absolute/secret.txt",
                "menus\\..\\secret.txt",
                "menus/./secret.txt",
            ]

            for bad_key in bad_keys:
                with self.subTest(bad_key=bad_key):
                    with self.assertRaises((TypeError, ValueError)):
                        service.put_bytes(b"secret", object_key=bad_key)
                    with self.assertRaises((TypeError, ValueError)):
                        service.read_bytes(bad_key)

    def test_signed_access_token_round_trip(self) -> None:
        now = 1_800_000_000
        secret = "test-secret"
        key = "generated/job-1/dish.jpg"

        access = create_signed_access(
            key,
            "user-1",
            asset_security.PREVIEW,
            asset_security.PREVIEW,
            secret,
            expires_in=60,
            now=now,
        )
        payload = verify_signed_access(access["token"], secret, now=now)

        self.assertEqual(access["object_key"], key)
        self.assertIn("/objects/generated/job-1/dish.jpg?token=", access["url"])
        self.assertEqual(payload["object_key"], key)
        self.assertEqual(payload["asset_id"], key)
        self.assertEqual(payload["user_id"], "user-1")
        self.assertEqual(payload["purpose"], asset_security.PREVIEW)
        self.assertEqual(payload["variant"], asset_security.PREVIEW)
        self.assertEqual(payload["expires_at"], now + 60)

    def test_expired_signed_access_token_fails(self) -> None:
        now = 1_800_000_000
        secret = "test-secret"
        access = create_signed_access(
            "exports/job-1/package.zip",
            "user-1",
            asset_security.EXPORT,
            asset_security.EXPORT,
            secret,
            expires_in=1,
            now=now,
        )

        with self.assertRaises(asset_security.ExpiredAssetTokenError):
            verify_signed_access(access["token"], secret, now=now + 2)

    def test_local_object_storage_is_demo_ready_with_warning(self) -> None:
        readiness = assess_object_storage_readiness({})

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["provider"], "local")
        self.assertEqual(readiness["mode"], "local_demo")
        self.assertEqual(readiness["blockingIssues"], [])
        self.assertIn("local_object_storage_is_for_development_only", readiness["warnings"])

    def test_production_local_object_storage_is_not_ready(self) -> None:
        readiness = assess_object_storage_readiness({"APP_ENV": "production"})

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "local")
        self.assertEqual(readiness["mode"], "local_demo")
        self.assertIn(
            "private_remote_object_storage_provider_required",
            readiness["blockingIssues"],
        )
        self.assertIn("object_signing_secret_required", readiness["blockingIssues"])

    def test_disabled_local_demo_requires_remote_provider_and_signing_secret(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "ENABLE_LOCAL_DEMO_STORAGE": "false",
                "OBJECT_SIGNING_SECRET": "secret",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "local")
        self.assertIn(
            "private_remote_object_storage_provider_required",
            readiness["blockingIssues"],
        )
        self.assertNotIn("object_signing_secret_required", readiness["blockingIssues"])

    def test_remote_private_provider_is_ready_when_bucket_and_secret_are_configured(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "APP_ENV": "production",
                "OBJECT_STORAGE_PROVIDER": "cos",
                "OBJECT_STORAGE_BUCKET": "waimai-assets-prod",
                "OBJECT_SIGNING_SECRET": "secret",
                "ENABLE_LOCAL_DEMO_STORAGE": "false",
            }
        )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["provider"], "cos")
        self.assertEqual(readiness["mode"], "remote_private")
        self.assertEqual(readiness["blockingIssues"], [])
        self.assertIn("remote_provider_sdk_not_initialized_by_readiness_check", readiness["warnings"])

    def test_remote_provider_requires_signing_secret(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "APP_ENV": "production",
                "OBJECT_STORAGE_PROVIDER": "oss",
                "OBJECT_STORAGE_BUCKET": "waimai-assets-prod",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "oss")
        self.assertEqual(readiness["mode"], "remote_private")
        self.assertIn("object_signing_secret_required", readiness["blockingIssues"])

    def test_remote_provider_rejects_public_read_storage(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "OBJECT_STORAGE_PROVIDER": "r2",
                "OBJECT_STORAGE_BUCKET": "waimai-assets-prod",
                "OBJECT_SIGNING_SECRET": "secret",
                "OBJECT_STORAGE_PUBLIC_READ": "true",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertIn("private_object_storage_required", readiness["blockingIssues"])

    def test_unknown_provider_is_not_ready(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "OBJECT_STORAGE_PROVIDER": "ftp",
                "OBJECT_SIGNING_SECRET": "secret",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "ftp")
        self.assertEqual(readiness["mode"], "unknown")
        self.assertIn("unsupported_object_storage_provider", readiness["blockingIssues"])


if __name__ == "__main__":
    unittest.main()
