from __future__ import annotations

import json
import tempfile
import urllib.parse
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


class RemoteGalleryRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        app_module.library_images.cache_clear()

    def test_cos_library_index_url_loads_remote_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            library_dir.mkdir()
            index_url = "https://cos.example.test/library_index.jsonl"
            image_url = "https://cdn.example.test/library/云端湘菜馆/红烧牛肉.jpg"
            raw = json.dumps(
                {
                    "id": "remote-url-001",
                    "source": "clean",
                    "store": "云端湘菜馆",
                    "dish": "红烧牛肉",
                    "remote_url": image_url,
                    "style_id": "remote-url-style",
                    "reusable": True,
                },
                ensure_ascii=False,
            ).encode("utf-8")

            class FakeResponse:
                def __enter__(self) -> "FakeResponse":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

                def read(self) -> bytes:
                    return raw + b"\n"

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "COS_LIBRARY_INDEX_URL": index_url,
                        "LIBRARY_INDEX_URL": "",
                        "LIBRARY_INDEX_PATH": "",
                        "LIBRARY_SOURCE_DIRS": str(root / "missing-source"),
                    },
                ),
                mock.patch.object(app_module, "LIBRARY_DIR", library_dir),
                mock.patch.object(app_module, "ensure_demo_data"),
                mock.patch.object(app_module.urllib.request, "urlopen", return_value=FakeResponse()),
            ):
                app_module.library_images.cache_clear()
                library = app_module.library_images()
                self.assertEqual(len(library), 1)
                self.assertEqual(library[0].remote_url, image_url)

                status = app_module.app.test_client().get("/api/library-status").get_json()
                self.assertTrue(status["remoteIndex"])
                self.assertEqual(status["remoteImages"], 1)
                self.assertEqual(status["indexSource"], index_url)
                self.assertEqual(status["indexError"], "")

    def test_local_jsonl_index_adds_remote_images_to_matches_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            library_dir.mkdir()
            index_path = root / "library_index.jsonl"
            clean_url = "https://cdn.example.test/library/云端湘菜馆/辣椒炒肉.jpg"
            watermark_url = "https://cdn.example.test/library/水印店/小炒黄牛肉.jpg"
            write_jsonl(
                index_path,
                [
                    {
                        "id": "remote-clean-001",
                        "source": "clean",
                        "store": "云端湘菜馆",
                        "dish": "老长沙辣椒炒肉盖码饭",
                        "style_id": "cos-style-stable",
                        "remote_url": clean_url,
                        "cos_key": "library/云端湘菜馆/辣椒炒肉.jpg",
                        "reusable": True,
                    },
                    {
                        "id": "remote-watermark-001",
                        "source": "watermark",
                        "store": "水印店",
                        "dish": "小炒黄牛肉盖码饭",
                        "url": watermark_url,
                        "cos_key": "library/水印店/小炒黄牛肉.jpg",
                        "reusable": True,
                    },
                ],
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "LIBRARY_INDEX_PATH": str(index_path),
                        "LIBRARY_SOURCE_DIRS": str(root / "missing-source"),
                    },
                ),
                mock.patch.object(app_module, "LIBRARY_DIR", library_dir),
                mock.patch.object(app_module, "ensure_demo_data"),
            ):
                app_module.library_images.cache_clear()
                library = app_module.library_images()

                remote = next(image for image in library if image.image_id == "remote-clean-001")
                self.assertEqual(remote.remote_url, clean_url)
                self.assertEqual(remote.cos_key, "library/云端湘菜馆/辣椒炒肉.jpg")
                self.assertEqual(remote.style_id, "cos-style-stable")

                watermark = next(image for image in library if image.image_id == "remote-watermark-001")
                self.assertFalse(watermark.reusable)
                self.assertTrue(watermark.reference_only)
                self.assertEqual(watermark.style_id, app_module.stable_style_id("水印店", "watermark"))

                item = {
                    "row": 1,
                    "name": "老长沙辣椒炒肉盖码饭",
                    "norm": app_module.normalize("老长沙辣椒炒肉盖码饭"),
                }
                candidates = app_module.top_candidates(item, library)
                self.assertEqual(candidates[0]["imageId"], "remote-clean-001")
                self.assertEqual(candidates[0]["url"], clean_url)
                self.assertNotIn("/external-media/", candidates[0]["url"])

                candidate = app_module.candidate_from_library_image(remote)
                self.assertEqual(candidate["url"], clean_url)
                self.assertEqual(candidate["remoteUrl"], clean_url)

                response = app_module.app.test_client().get("/api/library-status")
                self.assertEqual(response.status_code, 200)
                status = response.get_json()
                self.assertEqual(status["remoteImages"], 2)
                self.assertEqual(status["indexImages"], 2)
                self.assertEqual(status["indexSource"], str(index_path))
                self.assertEqual(status["indexError"], "")
                self.assertEqual(status["sources"]["clean"], 1)
                self.assertEqual(status["sources"]["watermark"], 1)

    def test_cos_key_bucket_region_are_enough_for_remote_gallery_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            library_dir.mkdir()
            index_path = root / "library_index.jsonl"
            cos_key = "waimai-gallery/clean/云端湘菜馆/abcdef1234567890.jpg"
            expected_url = (
                "https://demo-gallery-1250000000.cos.ap-guangzhou.myqcloud.com/"
                f"{urllib.parse.quote(cos_key, safe='/%')}"
            )
            write_jsonl(
                index_path,
                [
                    {
                        "id": "remote-cos-key-001",
                        "source": "clean",
                        "store": "云端湘菜馆",
                        "dish": "红烧牛肉",
                        "canonical_norm": "红烧牛肉",
                        "match_family": "meat",
                        "match_kind": "单品",
                        "match_category": "single",
                        "style_id": "cos-style-key-only",
                        "cos_key": cos_key,
                        "cos_bucket": "demo-gallery-1250000000",
                        "cos_region": "ap-guangzhou",
                        "reusable": True,
                        "watermark": "none",
                    },
                ],
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "LIBRARY_INDEX_PATH": str(index_path),
                        "LIBRARY_SOURCE_DIRS": str(root / "missing-source"),
                    },
                ),
                mock.patch.object(app_module, "LIBRARY_DIR", library_dir),
                mock.patch.object(app_module, "ensure_demo_data"),
            ):
                app_module.library_images.cache_clear()
                library = app_module.library_images()
                remote = next(image for image in library if image.image_id == "remote-cos-key-001")

                self.assertEqual(remote.remote_url, expected_url)
                self.assertEqual(remote.cos_key, cos_key)
                self.assertTrue(remote.reusable)

                status = app_module.app.test_client().get("/api/library-status").get_json()
                self.assertEqual(status["remoteImages"], 1)
                self.assertEqual(status["indexImages"], 1)

    def test_index_failure_keeps_status_endpoint_alive_with_seed_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            missing_index = root / "missing.jsonl"
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "LIBRARY_INDEX_PATH": str(missing_index),
                        "LIBRARY_SOURCE_DIRS": str(root / "missing-source"),
                    },
                ),
                mock.patch.object(app_module, "LIBRARY_DIR", library_dir),
            ):
                app_module.library_images.cache_clear()
                client = app_module.app.test_client()
                home = client.get("/")
                response = client.get("/api/library-status")

                self.assertEqual(home.status_code, 200)
                self.assertEqual(response.status_code, 200)
                status = response.get_json()
                self.assertGreater(status["total"], 0)
                self.assertEqual(status["remoteImages"], 0)
                self.assertIn("FileNotFoundError", status["indexError"])
                self.assertEqual(status["indexSource"], str(missing_index))
                self.assertGreater(status["sources"]["internal"], 0)


if __name__ == "__main__":
    unittest.main()
