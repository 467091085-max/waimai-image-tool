from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import app as app_module


def jpg_payload() -> str:
    img = Image.new("RGB", (32, 24), (220, 80, 60))
    out = io.BytesIO()
    img.save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode("ascii")


class FakeCosClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Body, Key: str, ContentType: str) -> None:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[f"{Bucket}/{Key}"] = data


class GalleryUploadProxyTest(unittest.TestCase):
    def env(self) -> mock._patch:
        return mock.patch.dict(
            app_module.os.environ,
            {
                "GALLERY_UPLOAD_TOKEN": "secret-token",
                "TENCENTCLOUD_SECRET_ID": "sid",
                "TENCENTCLOUD_SECRET_KEY": "skey",
                "TENCENT_COS_BUCKET": "bucket-123",
                "TENCENT_COS_REGION": "ap-guangzhou",
                "TENCENT_COS_GALLERY_PREFIX": "waimai-gallery-test",
            },
            clear=False,
        )

    def test_gallery_upload_status_and_auth_guard(self) -> None:
        with self.env():
            client = app_module.app.test_client()
            status = client.get("/api/admin/gallery-upload/status").get_json()
            self.assertTrue(status["enabled"])
            self.assertTrue(status["cosReady"])
            self.assertEqual(status["prefix"], "waimai-gallery-test")
            self.assertIn("COS_LIBRARY_INDEX_URL", status["renderEnv"])

            denied = client.post("/api/admin/gallery-upload/batch", json={"session": "s1", "records": []})
            self.assertEqual(denied.status_code, 403)

    def test_gallery_upload_batch_and_publish_index(self) -> None:
        fake = FakeCosClient()
        with tempfile.TemporaryDirectory() as tmp, self.env(), mock.patch.object(app_module, "GALLERY_UPLOAD_DIR", Path(tmp)), mock.patch.object(app_module, "create_cos_client_from_config", return_value=fake):
            client = app_module.app.test_client()
            record = {
                "name": "测试菜",
                "dish": "测试菜",
                "cos_key": "waimai-gallery-test/clean/store/a.jpg",
                "object_key": "waimai-gallery-test/clean/store/a.jpg",
                "reusable": True,
            }
            batch = client.post(
                "/api/admin/gallery-upload/batch",
                json={"session": "s1", "records": [{"record": record, "image": jpg_payload()}]},
                headers={"X-Gallery-Upload-Token": "secret-token"},
            )
            self.assertEqual(batch.status_code, 200)
            payload = batch.get_json()
            self.assertEqual(payload["uploaded"], 1)
            self.assertEqual(payload["sessionRecords"], 1)
            self.assertIn("bucket-123/waimai-gallery-test/clean/store/a.jpg", fake.objects)

            published = client.post(
                "/api/admin/gallery-upload/publish",
                json={"session": "s1"},
                headers={"X-Gallery-Upload-Token": "secret-token"},
            )
            self.assertEqual(published.status_code, 200)
            data = published.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["records"], 1)
            self.assertEqual(data["indexKey"], "waimai-gallery-test/index/library_index.jsonl")
            self.assertIn("bucket-123/waimai-gallery-test/index/library_index.jsonl", fake.objects)


if __name__ == "__main__":
    unittest.main()
