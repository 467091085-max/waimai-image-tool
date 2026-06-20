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
            self.assertTrue(Path(record["thumb_path"]).exists())

    def test_write_index_outputs_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "library_index.jsonl"
            write_index([{"dish": "酸梅汤", "source": "clean"}], output)

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), {"dish": "酸梅汤", "source": "clean"})


if __name__ == "__main__":
    unittest.main()
