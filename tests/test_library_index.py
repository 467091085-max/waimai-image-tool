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
            self.assertEqual(record["match_category"], "package")
            self.assertEqual(record["match_family"], "combo")
            self.assertEqual(record["match_kind"], "套餐/组合")
            self.assertEqual(record["suffix"], ".jpg")
            self.assertEqual(record["width"], 640)
            self.assertEqual(record["height"], 480)
            self.assertTrue(record["is_combo"])
            self.assertTrue(record["is_package"])
            self.assertTrue(record["is_drink"])
            self.assertIn("combo", record["tags"])
            self.assertIn("package", record["tags"])
            self.assertIn("drink", record["tags"])
            self.assertGreaterEqual(
                record.keys(),
                {
                    "reusable",
                    "reference_only",
                    "direct_delivery_allowed",
                    "canonical",
                    "name",
                    "category",
                    "style",
                    "background",
                    "local_path",
                    "watermark",
                    "has_brand_watermark",
                    "has_dish_text",
                    "has_dish_text_watermark",
                    "quality_score",
                    "style_weight",
                    "match_weight",
                    "review_reasons",
                },
            )
            self.assertTrue(record["reusable"])
            self.assertFalse(record["reference_only"])
            self.assertEqual(record["canonical"], "招牌双拼套餐可乐")
            self.assertEqual(record["name"], "招牌双拼套餐+可乐")
            self.assertEqual(record["category"], "套餐")
            self.assertEqual(record["watermark"], "none")
            self.assertTrue(record["avoid_as_style_card"])
            self.assertLess(record["style_weight"], 1.0)
            self.assertTrue(Path(record["thumb_path"]).exists())
            summary = result.summary()
            self.assertEqual(summary["packageImages"], 1)
            self.assertEqual(summary["formalImages"], 1)
            self.assertEqual(summary["matchCategories"]["package"], 1)

    def test_scan_library_marks_cleanpic_and_watermarkpic_reuse_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_dir = root / "cleanpic"
            watermark_dir = root / "watermarkpic"
            normal_clean = clean_dir / "测试门店" / "红烧牛肉.jpg"
            dish_text_clean = clean_dir / "测试门店" / "干锅牛肉_带字水印.jpg"
            prompt_clean = clean_dir / "测试门店" / "勿点提示背景米饭.jpg"
            brand_clean = clean_dir / "测试门店" / "可口可乐活动电话13800138000.jpg"
            watermark_image = watermark_dir / "品牌门店" / "小炒黄牛肉.jpg"
            for path in [normal_clean, dish_text_clean, prompt_clean, brand_clean, watermark_image]:
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (640, 480), (80, 120, 160)).save(path)

            result = scan_library(
                roots={"clean": clean_dir, "watermark": watermark_dir},
                thumb_dir=None,
                make_thumbs=False,
            )

            self.assertEqual(result.total, 5)
            by_dish = {record["dish"]: record for record in result.records}

            clean_record = by_dish["红烧牛肉"]
            self.assertTrue(clean_record["reusable"])
            self.assertFalse(clean_record["reference_only"])
            self.assertFalse(clean_record["has_brand_watermark"])
            self.assertFalse(clean_record["has_dish_text_watermark"])
            self.assertEqual(clean_record["quality_score"], 1.0)
            self.assertEqual(clean_record["review_reasons"], [])

            dish_text_record = by_dish["干锅牛肉_带字水印"]
            self.assertTrue(dish_text_record["reusable"])
            self.assertFalse(dish_text_record["reference_only"])
            self.assertTrue(dish_text_record["has_dish_text"])
            self.assertTrue(dish_text_record["has_dish_text_watermark"])
            self.assertFalse(dish_text_record["has_brand_watermark"])
            self.assertLess(dish_text_record["style_weight"], 1.0)
            self.assertTrue(any("菜品名文字水印" in reason for reason in dish_text_record["review_reasons"]))

            watermark_record = by_dish["小炒黄牛肉"]
            self.assertFalse(watermark_record["reusable"])
            self.assertTrue(watermark_record["reference_only"])
            self.assertFalse(watermark_record["direct_delivery_allowed"])
            self.assertTrue(watermark_record["has_brand_watermark"])
            self.assertIn("brand_watermark", watermark_record["delivery_blockers"])
            self.assertLess(watermark_record["quality_score"], 1.0)
            self.assertTrue(any("品牌水印风险" in reason for reason in watermark_record["review_reasons"]))

            prompt_record = by_dish["勿点提示背景米饭"]
            self.assertTrue(prompt_record["reusable"])
            self.assertFalse(prompt_record["reference_only"])
            self.assertFalse(prompt_record["has_dish_text_watermark"])
            self.assertLess(prompt_record["quality_score"], 0.7)
            self.assertTrue(prompt_record["avoid_as_style_card"])
            self.assertTrue(prompt_record["avoid_as_match_primary"])
            self.assertIn("generic", prompt_record["tags"])
            self.assertTrue(any("低质/泛图" in reason for reason in prompt_record["review_reasons"]))

            brand_record = by_dish["可口可乐活动电话13800138000"]
            self.assertTrue(brand_record["reusable"])
            self.assertTrue(brand_record["suspected_watermark"])
            self.assertTrue(brand_record["has_dish_text"])
            self.assertTrue(any("明显品牌词" in reason for reason in brand_record["review_reasons"]))
            self.assertTrue(any("电话" in reason for reason in brand_record["review_reasons"]))
            self.assertTrue(any("活动词" in reason for reason in brand_record["review_reasons"]))

            full_summary = result.summary()
            self.assertEqual(full_summary["total"], 5)
            self.assertEqual(full_summary["clean"], 4)
            self.assertEqual(full_summary["watermark"], 1)
            self.assertGreaterEqual(full_summary["singleImages"], 2)
            self.assertGreaterEqual(full_summary["snackDrinkImages"], 1)
            self.assertEqual(full_summary["reusable"], 4)
            self.assertEqual(full_summary["referenceOnly"], 1)

            summary = full_summary["cleaning"]
            self.assertEqual(summary["reusable"], 4)
            self.assertEqual(summary["referenceOnly"], 1)
            self.assertEqual(summary["watermarkRisk"], 1)
            self.assertEqual(summary["dishTextWatermark"], 1)
            self.assertEqual(summary["lowQuality"], 2)
            self.assertGreaterEqual(summary["downranked"], 2)

    def test_scan_library_marks_sha1_duplicate_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_dir = root / "clean"
            watermark_dir = root / "watermark"
            clean_image = clean_dir / "店A" / "红烧肉.png"
            watermark_image = watermark_dir / "店B" / "红烧肉参考.png"
            for path in [clean_image, watermark_image]:
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (360, 360), (120, 30, 40)).save(path)

            result = scan_library(
                roots={"clean": clean_dir, "watermark": watermark_dir},
                thumb_dir=None,
                make_thumbs=False,
            )

            self.assertEqual(result.total, 2)
            summary = result.summary()
            self.assertEqual(summary["sha1Deduped"], 1)
            self.assertEqual(summary["sha1Duplicates"], 1)
            self.assertEqual(summary["sha1"]["duplicateGroups"], 1)

            primary = next(record for record in result.records if record["sha1_primary"])
            duplicate = next(record for record in result.records if record["sha1_duplicate"])
            self.assertEqual(primary["source"], "clean")
            self.assertEqual(primary["sha1_group_size"], 2)
            self.assertEqual(duplicate["sha1_primary_path"], primary["path"])

    def test_write_index_outputs_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "library_index.jsonl"
            write_index([{"dish": "酸梅汤", "source": "clean"}], output)

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), {"dish": "酸梅汤", "source": "clean"})


if __name__ == "__main__":
    unittest.main()
