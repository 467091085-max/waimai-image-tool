from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw

from image_pipeline import (
    MAX_LOGO_DATA_URL_CHARS,
    PLATFORMS,
    REPORT_COLUMNS,
    assess_generated_asset_quality,
    apply_watermark,
    export_delivery_zip,
    fit_to_platform,
    make_logo_watermark,
    make_text_watermark,
)


def png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class ImagePipelineTest(unittest.TestCase):
    def test_platform_specs_and_contain_fit_keep_subject_edges(self) -> None:
        self.assertEqual((PLATFORMS["meituan"]["width"], PLATFORMS["meituan"]["height"], PLATFORMS["meituan"]["maxKB"]), (800, 600, 5120))
        self.assertEqual((PLATFORMS["taobao"]["width"], PLATFORMS["taobao"]["height"], PLATFORMS["taobao"]["maxKB"]), (800, 800, 20480))
        self.assertEqual((PLATFORMS["jd"]["width"], PLATFORMS["jd"]["height"], PLATFORMS["jd"]["maxKB"]), (800, 800, 5120))

        src = Image.new("RGB", (400, 200), (230, 230, 230))
        draw = ImageDraw.Draw(src)
        draw.rectangle((0, 0, 79, 199), fill=(10, 220, 40))
        draw.rectangle((320, 0, 399, 199), fill=(20, 60, 235))
        draw.ellipse((150, 40, 250, 160), fill=(235, 70, 50))

        fitted = fit_to_platform(src, "taobao")

        self.assertEqual(fitted.size, (800, 800))
        self.assertGreater(fitted.getpixel((20, 400))[1], 180)
        self.assertGreater(fitted.getpixel((780, 400))[2], 180)
        self.assertEqual(fitted.getpixel((400, 40)), fitted.getpixel((400, 760)))

    def test_text_and_png_logo_watermarks_keep_transparent_backgrounds(self) -> None:
        text_mark = make_text_watermark("测试品牌", 800)
        self.assertEqual(text_mark.mode, "RGBA")
        self.assertEqual(text_mark.getpixel((0, 0))[3], 0)

        logo = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        draw = ImageDraw.Draw(logo)
        draw.rectangle((10, 10, 29, 29), fill=(255, 0, 0, 220))
        logo_url = png_data_url(logo)
        logo_mark = make_logo_watermark(logo_url, 800)

        self.assertIsNotNone(logo_mark)
        assert logo_mark is not None
        self.assertEqual(logo_mark.getpixel((0, 0))[3], 0)

        base = Image.new("RGBA", (200, 160), (100, 120, 140, 255))
        out = apply_watermark(base, {"enabled": True, "type": "logo", "logoData": logo_url, "position": "top-left"})

        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((24, 24)), base.getpixel((24, 24)))
        self.assertNotEqual(out.getpixel((44, 44)), base.getpixel((44, 44)))

    def test_generated_asset_quality_passes_full_composed_food_image(self) -> None:
        img = Image.new("RGB", (800, 600), (242, 238, 232))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 799, 599), outline=(10, 80, 60), width=20)
        draw.ellipse((100, 80, 710, 560), fill=(210, 70, 50))
        draw.rectangle((170, 140, 650, 500), fill=(45, 130, 70))
        draw.ellipse((280, 180, 520, 450), fill=(250, 230, 80))

        report = assess_generated_asset_quality(img)

        self.assertTrue(report["passed"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["reasons"], [])
        self.assertGreaterEqual(report["score"], 0.65)

    def test_generated_asset_quality_rejects_low_quality_reusable_assets(self) -> None:
        solid = Image.new("RGB", (800, 600), (240, 240, 240))
        transparent = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
        center_frame = Image.new("RGB", (800, 600), (245, 245, 245))
        draw = ImageDraw.Draw(center_frame)
        draw.rectangle((300, 225, 500, 375), fill=(50, 80, 150))
        draw.ellipse((330, 240, 470, 360), fill=(220, 60, 50))

        solid_report = assess_generated_asset_quality(solid)
        transparent_report = assess_generated_asset_quality(transparent)
        frame_report = assess_generated_asset_quality(center_frame)

        self.assertFalse(solid_report["passed"])
        self.assertIn("solid_or_placeholder", solid_report["reasons"])
        self.assertFalse(transparent_report["passed"])
        self.assertIn("background_not_filled", transparent_report["reasons"])
        self.assertFalse(frame_report["passed"])
        self.assertIn("small_center_frame", frame_report["reasons"])

    def test_export_zip_outputs_rgb_jpgs_under_limits_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            src = Image.new("RGBA", (1200, 700), (242, 238, 232, 255))
            draw = ImageDraw.Draw(src)
            draw.rectangle((40, 40, 1160, 660), outline=(12, 36, 78, 255), width=24)
            draw.ellipse((330, 120, 870, 650), fill=(214, 72, 58, 255))
            src.save(source)

            plan_results = [
                {
                    "name": "招牌测试菜",
                    "category": "热销",
                    "kind": "单品",
                    "points": 10,
                    "backgroundAction": "背景一致，直接复用",
                    "candidates": [{"path": str(source)}],
                },
                {
                    "name": "缺图菜",
                    "category": "新品",
                    "kind": "单品",
                    "points": 20,
                    "backgroundAction": "智能补图",
                    "candidates": [],
                },
            ]

            result = export_delivery_zip(
                plan_results,
                root / "exports",
                platforms=["meituan", "taobao", "jd"],
                watermark={"enabled": True, "type": "text", "text": "测试品牌", "position": "bottom-right"},
                run_name="case",
            )

            self.assertEqual(result["images"], 3)
            self.assertEqual(result["rows"], 4)
            zip_path = root / "exports" / result["download"].split("/download/", 1)[1]
            self.assertTrue(zip_path.exists())

            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                image_names = sorted(name for name in names if name.startswith("images/") and name.endswith(".jpg"))

                self.assertIn("delivery_report.xlsx", names)
                self.assertEqual(len(image_names), 3)
                self.assertEqual({name.split("/")[1].split("_", 1)[0] for name in image_names}, {"meituan", "taobao", "jd"})

                report = pd.read_excel(io.BytesIO(zf.read("delivery_report.xlsx")))
                self.assertEqual(list(report.columns), REPORT_COLUMNS)
                self.assertEqual(len(report), 4)
                self.assertIn("待补图", set(report["图片状态"]))

                for name in image_names:
                    platform_id = name.split("/")[1].split("_", 1)[0]
                    spec = PLATFORMS[platform_id]
                    payload = zf.read(name)
                    img = Image.open(io.BytesIO(payload))
                    self.assertEqual(img.format, "JPEG")
                    self.assertEqual(img.mode, "RGB")
                    self.assertEqual(img.size, (spec["width"], spec["height"]))
                    self.assertLessEqual(len(payload), spec["maxKB"] * 1024)

    def test_logo_watermark_rejects_oversized_base64_payload(self) -> None:
        self.assertIsNone(make_logo_watermark("a" * (MAX_LOGO_DATA_URL_CHARS + 1), 800))

    def test_export_zip_skips_corrupt_candidate_image_without_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corrupt = root / "corrupt.jpg"
            corrupt.write_bytes(b"not an image")
            plan_results = [
                {
                    "name": "坏图菜",
                    "category": "测试",
                    "kind": "单品",
                    "points": 10,
                    "backgroundAction": "背景一致，直接复用",
                    "candidates": [{"path": str(corrupt)}],
                }
            ]

            result = export_delivery_zip(plan_results, root / "exports", run_name="corrupt-case")

            self.assertEqual(result["images"], 0)
            zip_path = root / "exports" / result["download"].split("/download/", 1)[1]
            with zipfile.ZipFile(zip_path) as zf:
                report = pd.read_excel(io.BytesIO(zf.read("delivery_report.xlsx")))
            self.assertEqual(report.loc[0, "图片状态"], "待补图")


if __name__ == "__main__":
    unittest.main()
