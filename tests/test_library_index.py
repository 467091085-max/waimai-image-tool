from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from library_index import scan_library, write_index


class LibraryIndexTest(unittest.TestCase):
    def test_scan_library_extracts_metadata_tags_and_thumbnails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_dir = root / "clean"
            watermark_dir = root / "watermark"
            image_path = clean_dir / "测试门店" / "套餐" / "招牌双拼套餐+可乐.jpg"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (640, 480), (220, 80, 40)).save(image_path)
            watermark_dir.mkdir()
            (watermark_dir / "broken.jpg").write_text("not an image", encoding="utf-8")

            thumb_dir = root / "thumbs"
            result = scan_library(
                roots={"clean": clean_dir, "watermark": watermark_dir},
                thumb_dir=thumb_dir,
                thumb_size=(96, 96),
            )

            self.assertEqual(result.total, 1)
            self.assertEqual(len(result.errors), 1)
            record = result.records[0]
            self.assertEqual(record["source"], "clean")
            self.assertEqual(record["store"], "测试门店")
            self.assertEqual(record["category_path"], "套餐")
            self.assertEqual(record["dish"], "招牌双拼套餐+可乐")
            self.assertEqual(record["suffix"], ".jpg")
            self.assertEqual(record["width"], 640)
            self.assertEqual(record["height"], 480)
            self.assertTrue(record["is_combo"])
            self.assertTrue(record["is_drink"])
            self.assertIn("combo", record["tags"])
            self.assertIn("drink", record["tags"])
            self.assertGreaterEqual(record.keys(), {"reusable", "has_brand_watermark", "has_dish_text", "quality_score", "review_reasons"})
            self.assertTrue(Path(record["thumb_path"]).exists())

    def test_scan_library_marks_cleanpic_and_watermarkpic_reuse_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_dir = root / "cleanpic"
            watermark_dir = root / "watermarkpic"
            normal_clean = clean_dir / "测试门店" / "红烧牛肉.jpg"
            prompt_clean = clean_dir / "测试门店" / "勿点提示背景米饭.jpg"
            brand_clean = clean_dir / "测试门店" / "可口可乐活动电话13800138000.jpg"
            watermark_image = watermark_dir / "品牌门店" / "小炒黄牛肉.jpg"
            for path in [normal_clean, prompt_clean, brand_clean, watermark_image]:
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (320, 240), (80, 120, 160)).save(path)

            result = scan_library(
                roots={"clean": clean_dir, "watermark": watermark_dir},
                thumb_dir=None,
                make_thumbs=False,
            )

            self.assertEqual(result.total, 4)
            by_dish = {record["dish"]: record for record in result.records}

            clean_record = by_dish["红烧牛肉"]
            self.assertTrue(clean_record["reusable"])
            self.assertFalse(clean_record["has_brand_watermark"])
            self.assertFalse(clean_record["has_dish_text"])
            self.assertEqual(clean_record["quality_score"], 1.0)
            self.assertEqual(clean_record["review_reasons"], [])

            watermark_record = by_dish["小炒黄牛肉"]
            self.assertFalse(watermark_record["reusable"])
            self.assertTrue(watermark_record["has_brand_watermark"])
            self.assertFalse(watermark_record["has_dish_text"])
            self.assertLess(watermark_record["quality_score"], 1.0)
            self.assertTrue(any("品牌水印风险" in reason for reason in watermark_record["review_reasons"]))

            prompt_record = by_dish["勿点提示背景米饭"]
            self.assertFalse(prompt_record["reusable"])
            self.assertTrue(prompt_record["has_dish_text"])
            self.assertLess(prompt_record["quality_score"], 0.7)
            self.assertTrue(any("低复用图" in reason for reason in prompt_record["review_reasons"]))

            brand_record = by_dish["可口可乐活动电话13800138000"]
            self.assertFalse(brand_record["reusable"])
            self.assertTrue(brand_record["has_dish_text"])
            self.assertTrue(any("明显品牌词" in reason for reason in brand_record["review_reasons"]))
            self.assertTrue(any("电话" in reason for reason in brand_record["review_reasons"]))
            self.assertTrue(any("活动词" in reason for reason in brand_record["review_reasons"]))

            summary = result.summary()["cleaning"]
            self.assertEqual(summary["reusable"], 1)
            self.assertEqual(summary["watermarkRisk"], 1)
            self.assertEqual(summary["needsReview"], 3)
            self.assertEqual(summary["lowQuality"], 2)

    def test_write_index_outputs_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "library_index.jsonl"
            write_index([{"dish": "酸梅汤", "source": "clean"}], output)

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), {"dish": "酸梅汤", "source": "clean"})


if __name__ == "__main__":
    unittest.main()
