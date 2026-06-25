from __future__ import annotations

import builtins
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from scripts.sync_gallery_to_cos import (
    SyncConfig,
    cos_key_for_record,
    prepare_jpeg,
    summary_path_for,
    sync_gallery,
)


def make_image(path: Path, size: tuple[int, int] = (160, 120), mode: str = "RGB") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "RGBA":
        Image.new("RGBA", size, (220, 40, 30, 120)).save(path)
    else:
        Image.new("RGB", size, (220, 40, 30)).save(path)


class CosGallerySyncTest(unittest.TestCase):
    def test_dry_run_writes_index_without_cos_sdk_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_dir = root / "cleanpic"
            watermark_dir = root / "watermarkpic"
            output = root / "library_index.jsonl"
            make_image(clean_dir / "测试门店" / "招牌牛肉.png", size=(200, 100), mode="RGBA")
            make_image(watermark_dir / "品牌门店" / "小炒黄牛肉.jpg", size=(90, 180))

            real_import = builtins.__import__

            def guarded_import(name, *args, **kwargs):
                if name == "qcloud_cos":
                    raise AssertionError("dry-run must not import qcloud_cos")
                return real_import(name, *args, **kwargs)

            with mock.patch("builtins.__import__", side_effect=guarded_import):
                summary = sync_gallery(
                    SyncConfig(
                        clean_dir=clean_dir,
                        watermark_dir=watermark_dir,
                        bucket="demo-gallery-1250000000",
                        region="ap-guangzhou",
                        prefix="waimai-gallery",
                        dry_run=True,
                        output=output,
                        max_side=80,
                        quality=80,
                    )
                )

            self.assertTrue(summary["ok"])
            self.assertTrue(summary["dryRun"])
            self.assertEqual(summary["indexedTotal"], 2)
            self.assertEqual(summary["uploadedImages"], 0)
            self.assertEqual(summary["wouldUploadImages"], 2)
            self.assertTrue(output.exists())
            self.assertTrue(summary_path_for(output).exists())

            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            required = {
                "id",
                "dish",
                "norm",
                "store",
                "source",
                "reusable",
                "reference_only",
                "style_id",
                "relative_path",
                "cos_key",
                "public_url",
                "width",
                "height",
                "sha1",
                "tags",
                "quality_score",
                "has_brand_watermark",
                "has_dish_text_watermark",
                "avoid_as_style_card",
                "avoid_as_match_primary",
            }
            for record in records:
                self.assertGreaterEqual(record.keys(), required)
                self.assertTrue(record["public_url"].startswith("https://demo-gallery-1250000000.cos.ap-guangzhou.myqcloud.com/"))

            clean = next(record for record in records if record["source"] == "clean")
            self.assertEqual((clean["width"], clean["height"]), (80, 40))
            self.assertEqual(clean["cos_key"], f"waimai-gallery/clean/测试门店/{clean['sha1']}.jpg")
            self.assertFalse(clean["reference_only"])
            self.assertTrue(clean["reusable"])

            watermark = next(record for record in records if record["source"] == "watermark")
            self.assertEqual(watermark["cos_key"], f"waimai-gallery/watermark/品牌门店/{watermark['sha1']}.jpg")
            self.assertTrue(watermark["reference_only"])
            self.assertFalse(watermark["reusable"])
            self.assertTrue(watermark["has_brand_watermark"])

    def test_cos_key_naming_is_stable(self) -> None:
        record = {"source": "clean", "store": "A 店", "sha1": "a" * 40}

        first = cos_key_for_record(record, "/waimai-gallery/")
        second = cos_key_for_record(dict(record), "waimai-gallery")

        self.assertEqual(first, second)
        self.assertEqual(first, f"waimai-gallery/clean/A_店/{'a' * 40}.jpg")

    def test_prepare_jpeg_converts_to_rgb_and_limits_long_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "rgba.png"
            make_image(source, size=(120, 60), mode="RGBA")

            prepared = prepare_jpeg(source, max_side=32, quality=78)
            with Image.open(io.BytesIO(prepared.data)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(max(image.size), 32)


if __name__ == "__main__":
    unittest.main()
