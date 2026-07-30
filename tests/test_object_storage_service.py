from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import asset_security
from object_storage_service import (
    BUCKET_PREFIXES,
    GENERATED_PREFIX,
    MENUS_PREFIX,
    ORIGINALS_PREFIX,
    ObjectStorageReadLimitExceeded,
    ObjectStorageService,
    TencentCOSObjectStorageService,
    assess_object_storage_readiness,
    create_signed_access,
    download_object_file_limited,
    private_preview_object_key,
    put_object_file_limited,
    read_file_bytes_limited,
    verify_signed_access,
)


class FakeCOSBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def get_raw_stream(self) -> io.BytesIO:
        return io.BytesIO(self.payload)


class FakeCOSClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def put_object(self, *, Bucket: str, Body: object, Key: str, ContentType: str) -> None:
        payload = Body.read() if hasattr(Body, "read") else Body
        assert isinstance(payload, bytes)
        self.objects[Key] = payload
        self.content_types[Key] = ContentType

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise FileNotFoundError(Key)
        return {"Body": FakeCOSBody(self.objects[Key])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise FileNotFoundError(Key)
        return {"Content-Length": str(len(self.objects[Key])), "Last-Modified": "Mon, 29 Jun 2026 00:00:00 GMT"}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)

    def list_objects(self, *, Bucket: str, Prefix: str) -> dict[str, object]:
        return {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}


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

    def test_local_limited_upload_rejects_before_replacing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "dish.png"
            source.write_bytes(b"new-oversized-image")
            service = ObjectStorageService(Path(tmp) / "objects")
            key = service.put_bytes(
                b"existing-image",
                object_key="ai-assets/tenant/asset/original.png",
            )

            with self.assertRaises(ObjectStorageReadLimitExceeded):
                put_object_file_limited(
                    service,
                    source,
                    object_key=key,
                    max_bytes=len(b"new-oversized-image") - 1,
                )

            self.assertEqual(service.read_bytes(key), b"existing-image")
            stored = put_object_file_limited(
                service,
                source,
                object_key=key,
                max_bytes=len(b"new-oversized-image"),
            )
            self.assertEqual(stored, key)
            self.assertEqual(
                service.read_bytes(key),
                b"new-oversized-image",
            )

    def test_local_limited_read_rejects_oversized_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ObjectStorageService(Path(tmp) / "objects")
            key = service.put_bytes(
                b"nine-byte",
                object_key="generated/job/image.png",
            )

            with self.assertRaises(ObjectStorageReadLimitExceeded):
                service.read_bytes_limited(key, 8)

            self.assertEqual(service.read_bytes_limited(key, 9), b"nine-byte")

    def test_local_file_read_is_bounded_by_actual_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "growing-image.bin"
            source.write_bytes(b"123456789")

            real_stat = Path.stat

            def stale_stat(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> object:
                if path == source:
                    return mock.Mock(st_size=8)
                return real_stat(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "stat", stale_stat),
                self.assertRaises(ObjectStorageReadLimitExceeded),
            ):
                read_file_bytes_limited(source, 8)

            self.assertEqual(
                read_file_bytes_limited(source, 9),
                b"123456789",
            )

    def test_explicit_file_upload_and_download_stream_through_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "package.zip"
            destination = Path(tmp) / "downloaded.zip"
            source.write_bytes(b"streamed-export-package")
            service = ObjectStorageService(Path(tmp) / "objects")

            key = service.put_file(
                source,
                object_key="exports/job-1/package.zip",
            )
            returned = service.download_file(key, destination)

            self.assertEqual(returned, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_local_limited_download_rejects_before_copying_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ObjectStorageService(Path(tmp) / "objects")
            key = service.put_bytes(
                b"oversized-object",
                object_key="generated/job/image.png",
            )
            destination = Path(tmp) / "limited.png"

            with self.assertRaises(ObjectStorageReadLimitExceeded):
                download_object_file_limited(
                    service,
                    key,
                    destination,
                    len(b"oversized-object") - 1,
                )

            self.assertFalse(destination.exists())
            returned = download_object_file_limited(
                service,
                key,
                destination,
                len(b"oversized-object"),
            )
            self.assertEqual(returned, destination)
            self.assertEqual(destination.read_bytes(), b"oversized-object")

    def test_limited_download_fails_closed_without_bounded_adapter(self) -> None:
        storage = mock.Mock()
        storage.download_file_limited = None

        with self.assertRaisesRegex(
            RuntimeError,
            "bounded streaming download is unavailable",
        ):
            download_object_file_limited(
                storage,
                "generated/job/image.png",
                "/tmp/must-not-be-written.png",
                8,
            )

        storage.download_file.assert_not_called()

    def test_private_preview_key_is_owner_and_menu_scoped(self) -> None:
        relative_name = (
            "_generated_previews/menu-key/style-1/0001_dish.png"
        )

        first = private_preview_object_key(
            "user-1",
            "menu-upload-1",
            relative_name,
        )
        replay = private_preview_object_key(
            "user-1",
            "menu-upload-1",
            relative_name,
        )
        other_owner = private_preview_object_key(
            "user-2",
            "menu-upload-1",
            relative_name,
        )
        other_menu = private_preview_object_key(
            "user-1",
            "menu-upload-2",
            relative_name,
        )

        self.assertEqual(first, replay)
        self.assertNotEqual(first, other_owner)
        self.assertNotEqual(first, other_menu)
        self.assertNotIn("user-1", first)
        self.assertNotIn("menu-upload-1", first)
        self.assertTrue(first.startswith("generated/customer-previews/v1/"))

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

    def test_signed_access_preserves_non_overriding_extra_claims(self) -> None:
        now = 1_800_000_000
        secret = "test-secret"
        access = create_signed_access(
            "exports/job-1/package.zip",
            "user-1",
            asset_security.EXPORT,
            asset_security.EXPORT,
            secret,
            now=now,
            extra_claims={"export_id": "export-1"},
        )

        payload = verify_signed_access(access["token"], secret, now=now)

        self.assertEqual(payload["export_id"], "export-1")

    def test_signed_access_rejects_extra_claim_override(self) -> None:
        with self.assertRaises(ValueError):
            create_signed_access(
                "exports/job-1/package.zip",
                "user-1",
                asset_security.EXPORT,
                asset_security.EXPORT,
                "test-secret",
                extra_claims={"user_id": "attacker"},
            )

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
        self.assertEqual(readiness["appEnv"], "production")
        self.assertIn(
            "private_remote_object_storage_provider_required",
            readiness["blockingIssues"],
        )
        self.assertIn("object_signing_secret_required", readiness["blockingIssues"])

    def test_render_runtime_local_object_storage_is_not_ready(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "PUBLIC_BASE_URL": "https://waimai-image-tool-1.onrender.com",
                "OBJECT_SIGNING_SECRET": "secret",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "local")
        self.assertEqual(readiness["mode"], "local_demo")
        self.assertEqual(readiness["appEnv"], "render")
        self.assertIn(
            "private_remote_object_storage_provider_required",
            readiness["blockingIssues"],
        )
        self.assertNotIn("object_signing_secret_required", readiness["blockingIssues"])

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

    def test_cos_provider_is_ready_when_runtime_credentials_are_configured(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "APP_ENV": "production",
                "OBJECT_STORAGE_PROVIDER": "cos",
                "OBJECT_STORAGE_BUCKET": "waimai-assets-prod",
                "OBJECT_STORAGE_REGION": "ap-guangzhou",
                "OBJECT_STORAGE_SECRET_ID": "sid",
                "OBJECT_STORAGE_SECRET_KEY": "skey",
                "OBJECT_SIGNING_SECRET": "secret",
                "ENABLE_LOCAL_DEMO_STORAGE": "false",
            }
        )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["provider"], "cos")
        self.assertEqual(readiness["mode"], "remote_private")
        self.assertEqual(readiness["blockingIssues"], [])
        self.assertIn("cos_runtime_adapter_enabled", readiness["warnings"])

    def test_cos_provider_requires_runtime_credentials(self) -> None:
        readiness = assess_object_storage_readiness(
            {
                "APP_ENV": "production",
                "OBJECT_STORAGE_PROVIDER": "cos",
                "OBJECT_STORAGE_BUCKET": "waimai-assets-prod",
                "OBJECT_SIGNING_SECRET": "secret",
                "ENABLE_LOCAL_DEMO_STORAGE": "false",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "cos")
        self.assertIn("cos_runtime_credentials_required", readiness["blockingIssues"])

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
        self.assertIn("remote_provider_runtime_adapter_not_implemented", readiness["blockingIssues"])

    def test_cos_backend_uses_logical_keys_with_optional_remote_prefix(self) -> None:
        client = FakeCOSClient()
        service = TencentCOSObjectStorageService(
            bucket="waimai-assets-prod",
            region="ap-guangzhou",
            secret_id="sid",
            secret_key="skey",
            prefix="tenant-a",
            client=client,
        )

        key = service.put_bytes(b"image-bytes", object_key="generated/job-1/dish.jpg")

        self.assertEqual(key, "generated/job-1/dish.jpg")
        self.assertIn("tenant-a/generated/job-1/dish.jpg", client.objects)
        self.assertEqual(client.content_types["tenant-a/generated/job-1/dish.jpg"], "image/jpeg")
        self.assertTrue(service.exists(key))
        self.assertEqual(service.read_bytes(key), b"image-bytes")
        with self.assertRaises(ObjectStorageReadLimitExceeded):
            service.read_bytes_limited(key, len(b"image-bytes") - 1)
        self.assertEqual(
            service.read_bytes_limited(key, len(b"image-bytes")),
            b"image-bytes",
        )
        self.assertEqual(service.stat(key)["remote_key"], "tenant-a/generated/job-1/dish.jpg")
        self.assertEqual(service.list_prefix("generated/job-1"), [key])
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "package.zip"
            destination = Path(tmp) / "downloaded.zip"
            source.write_bytes(b"cos-streamed-export")
            export_key = service.put_file(
                source,
                object_key="exports/job-1/package.zip",
            )
            service.download_file(export_key, destination)
            self.assertEqual(
                destination.read_bytes(),
                b"cos-streamed-export",
            )
            limited_destination = Path(tmp) / "limited.zip"
            with self.assertRaises(ObjectStorageReadLimitExceeded):
                service.download_file_limited(
                    export_key,
                    limited_destination,
                    len(b"cos-streamed-export") - 1,
                )
            self.assertFalse(limited_destination.exists())
            service.download_file_limited(
                export_key,
                limited_destination,
                len(b"cos-streamed-export"),
            )
            self.assertEqual(
                limited_destination.read_bytes(),
                b"cos-streamed-export",
            )
            oversized_source = Path(tmp) / "oversized.png"
            oversized_source.write_bytes(b"cos-bounded-upload")
            bounded_key = "ai-assets/tenant/asset/original.png"
            with self.assertRaises(ObjectStorageReadLimitExceeded):
                service.put_file_limited(
                    oversized_source,
                    object_key=bounded_key,
                    max_bytes=len(b"cos-bounded-upload") - 1,
                )
            self.assertNotIn(
                "tenant-a/" + bounded_key,
                client.objects,
            )
            service.put_file_limited(
                oversized_source,
                object_key=bounded_key,
                max_bytes=len(b"cos-bounded-upload"),
            )
            self.assertEqual(
                client.objects["tenant-a/" + bounded_key],
                b"cos-bounded-upload",
            )
        self.assertTrue(service.delete(key))
        self.assertFalse(service.exists(key))

    def test_optional_cos_read_distinguishes_missing_from_outage(self) -> None:
        client = FakeCOSClient()
        service = TencentCOSObjectStorageService(
            bucket="waimai-assets-prod",
            region="ap-guangzhou",
            secret_id="sid",
            secret_key="skey",
            client=client,
        )

        self.assertIsNone(
            service.read_bytes_if_exists(
                "generated/customer-previews/v1/missing.jpg"
            )
        )

        client.get_object = mock.Mock(
            side_effect=RuntimeError("cos network unavailable")
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "cos network unavailable",
        ):
            service.read_bytes_if_exists(
                "generated/customer-previews/v1/outage.jpg"
            )

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
