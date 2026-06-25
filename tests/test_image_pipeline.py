from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd
from PIL import Image, ImageChops, ImageDraw

from image_pipeline import (
    PLATFORMS,
    REPORT_COLUMNS,
    apply_watermark,
    export_delivery_zip,
    fit_to_platform,
    make_logo_watermark,
    make_text_watermark,
    normalize_image_format,
    platform_extra_points,
    prepare_platform_image,
    safe_filename,
)
from scripts.smoke_export import run_smoke_export


def png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def jpg_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


class ImagePipelineTest(unittest.TestCase):
    def test_platform_specs_and_contain_fit_keep_subject_edges(self) -> None:
        self.assertEqual((PLATFORMS["meituan"]["width"], PLATFORMS["meituan"]["height"], PLATFORMS["meituan"]["maxKB"]), (800, 600, 5120))
        self.assertEqual((PLATFORMS["taobao"]["width"], PLATFORMS["taobao"]["height"], PLATFORMS["taobao"]["maxKB"]), (800, 800, 20480))
        self.assertEqual((PLATFORMS["jd"]["width"], PLATFORMS["jd"]["height"], PLATFORMS["jd"]["maxKB"]), (800, 800, 5120))
        self.assertEqual(PLATFORMS["meituan"]["aspect"], "4:3")
        self.assertEqual(PLATFORMS["taobao"]["aspect"], "1:1")
        self.assertEqual(PLATFORMS["jd"]["aspect"], "1:1")
        self.assertEqual(PLATFORMS["meituan"]["defaultFormat"], "jpg")
        self.assertEqual(PLATFORMS["taobao"]["defaultFormat"], "jpg")
        self.assertEqual(PLATFORMS["jd"]["defaultFormat"], "jpg")
        self.assertEqual(set(PLATFORMS["meituan"]["formats"]), {"jpg", "png"})
        self.assertEqual(set(PLATFORMS["taobao"]["formats"]), {"jpg", "png"})
        self.assertEqual(set(PLATFORMS["jd"]["formats"]), {"jpg", "jpeg", "png"})

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

        prepared = prepare_platform_image(src, "meituan", {"enabled": True, "type": "text", "text": "很长的测试品牌水印" * 10})
        self.assertEqual(prepared.size, (800, 600))

    def test_format_normalization_defaults_and_jpeg_alias(self) -> None:
        self.assertEqual(normalize_image_format(None, "meituan"), ("jpg", ".jpg", "JPEG"))
        self.assertEqual(normalize_image_format("png", "taobao"), ("png", ".png", "PNG"))
        self.assertEqual(normalize_image_format("jpeg", "jd"), ("jpeg", ".jpeg", "JPEG"))
        self.assertEqual(normalize_image_format("jpeg", "meituan"), ("jpg", ".jpg", "JPEG"))
        self.assertEqual(normalize_image_format("webp", "jd"), ("jpg", ".jpg", "JPEG"))

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

        jpg_logo = Image.new("RGB", (80, 50), (245, 245, 245))
        draw = ImageDraw.Draw(jpg_logo)
        draw.rectangle((8, 8, 71, 41), fill=(20, 80, 220))
        jpg_mark = make_logo_watermark(jpg_data_url(jpg_logo), 800)
        self.assertIsNotNone(jpg_mark)
        assert jpg_mark is not None
        self.assertEqual(jpg_mark.mode, "RGBA")
        self.assertEqual(jpg_mark.getpixel((0, 0))[3], 255)
        self.assertLessEqual(jpg_mark.width, 160)
        self.assertLessEqual(jpg_mark.height, 100)

    def test_corner_text_watermarks_stay_inside_safe_area_without_background_panel(self) -> None:
        base = Image.new("RGBA", (320, 240), (214, 220, 224, 255))
        margin = max(24, base.width // 34)
        positions = {
            "top-left": ("left", "top"),
            "top-right": ("right", "top"),
            "bottom-left": ("left", "bottom"),
            "bottom-right": ("right", "bottom"),
        }

        mark = make_text_watermark("WM", base.width, "white")
        self.assertEqual(mark.getpixel((0, 0))[3], 0)

        for position, edges in positions.items():
            with self.subTest(position=position):
                out = apply_watermark(base, {"enabled": True, "type": "text", "text": "WM", "color": "white", "position": position})
                bbox = ImageChops.difference(base.convert("RGB"), out.convert("RGB")).getbbox()
                self.assertIsNotNone(bbox)
                assert bbox is not None
                left, top, right, bottom = bbox
                self.assertGreaterEqual(left, margin)
                self.assertGreaterEqual(top, margin)
                self.assertLessEqual(right, base.width - margin)
                self.assertLessEqual(bottom, base.height - margin)
                if "left" in edges:
                    self.assertLess(left, base.width // 2)
                if "right" in edges:
                    self.assertGreater(right, base.width // 2)
                if "top" in edges:
                    self.assertLess(top, base.height // 2)
                if "bottom" in edges:
                    self.assertGreater(bottom, base.height // 2)

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

    def test_export_supports_jpeg_option_and_jpg_logo_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            src = Image.new("RGB", (900, 700), (242, 238, 232))
            draw = ImageDraw.Draw(src)
            draw.rectangle((120, 120, 780, 600), fill=(214, 72, 58))
            src.save(source, "JPEG", quality=95)

            logo = Image.new("RGB", (90, 54), (250, 250, 250))
            draw = ImageDraw.Draw(logo)
            draw.rectangle((10, 10, 80, 44), fill=(20, 80, 220))

            result = export_delivery_zip(
                [
                    {
                        "name": "JPEG选项测试菜",
                        "category": "热销",
                        "kind": "单品",
                        "points": 0,
                        "backgroundAction": "背景一致，直接复用",
                        "candidates": [{"path": str(source)}],
                    }
                ],
                root / "exports",
                image_format="jpeg",
                platforms=["meituan", "jd"],
                watermark={"enabled": True, "type": "logo", "logoData": jpg_data_url(logo), "position": "top-left"},
                run_name="jpeg_logo",
            )

            self.assertEqual(result["images"], 2)
            zip_path = root / "exports" / result["download"].split("/download/", 1)[1]
            with zipfile.ZipFile(zip_path) as zf:
                image_names = sorted(name for name in zf.namelist() if name.startswith("images/"))
                self.assertEqual(len(image_names), 2)
                self.assertTrue(any("/meituan_" in name and name.endswith(".jpg") for name in image_names))
                self.assertTrue(any("/jd_" in name and name.endswith(".jpeg") for name in image_names))
                for name in image_names:
                    platform_id = name.split("/")[1].split("_", 1)[0]
                    spec = PLATFORMS[platform_id]
                    payload = zf.read(name)
                    img = Image.open(io.BytesIO(payload))
                    self.assertEqual(img.format, "JPEG")
                    self.assertEqual(img.mode, "RGB")
                    self.assertEqual(img.size, (spec["width"], spec["height"]))
                    self.assertLessEqual(len(payload), spec["maxKB"] * 1024)

    def test_export_supports_selected_ids_other_scope_png_and_unique_dish_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_a = root / "a.png"
            source_b = root / "b.png"
            source_c = root / "c.png"
            for index, source in enumerate([source_a, source_b, source_c], start=1):
                img = Image.new("RGBA", (420 + index * 80, 360), (246, 244, 238, 255))
                draw = ImageDraw.Draw(img)
                draw.rectangle((80, 60, img.width - 80, 300), fill=(180, 70 + index * 30, 60, 255))
                img.save(source)

            plan_results = [
                {"id": "dish-single", "row": 101, "name": "招牌/测试菜", "category": "热销", "kind": "单品", "points": 0, "backgroundAction": "背景一致，直接复用", "candidates": [{"imageId": "img-a", "path": str(source_a)}]},
                {"id": "dish-combo", "row": 102, "name": "招牌/测试菜", "category": "套餐", "kind": "套餐/组合", "points": 0, "backgroundAction": "背景一致，直接复用", "candidates": [{"imageId": "img-b", "path": str(source_b)}]},
                {"id": "dish-other", "row": 103, "name": "饮品:酸梅汤", "category": "饮品", "kind": "其他", "points": 0, "backgroundAction": "背景一致，直接复用", "candidates": [{"imageId": "img-c", "path": str(source_c)}]},
            ]

            selected = export_delivery_zip(
                plan_results,
                root / "exports",
                scope="selected",
                selected_ids=["dish-single", "dish-combo"],
                platforms=["jd"],
                image_format="png",
                run_name="selected_ids",
            )
            self.assertEqual(selected["images"], 2)
            zip_path = root / "exports" / selected["download"].split("/download/", 1)[1]

            with zipfile.ZipFile(zip_path) as zf:
                image_names = sorted(name for name in zf.namelist() if name.startswith("images/") and name.endswith(".png"))
                self.assertEqual(len(image_names), 2)
                self.assertTrue(any(name.endswith("/招牌_测试菜.png") for name in image_names))
                self.assertTrue(any(name.endswith("/招牌_测试菜_2.png") for name in image_names))
                for name in image_names:
                    img = Image.open(io.BytesIO(zf.read(name)))
                    self.assertEqual(img.format, "PNG")
                    self.assertEqual(img.mode, "RGB")
                    self.assertEqual(img.size, (800, 800))

            other = export_delivery_zip(
                plan_results,
                root / "exports",
                scope="other",
                platforms=["meituan"],
                run_name="other_scope",
            )
            self.assertEqual(other["images"], 1)
            other_zip_path = root / "exports" / other["download"].split("/download/", 1)[1]
            with zipfile.ZipFile(other_zip_path) as zf:
                report = pd.read_excel(io.BytesIO(zf.read("delivery_report.xlsx")))
                self.assertEqual(report.iloc[0]["类型"], "其他")
                self.assertTrue(any(name.endswith("/饮品_酸梅汤.jpg") for name in zf.namelist()))

    def test_safe_filename_and_platform_extra_points(self) -> None:
        self.assertEqual(safe_filename("  招牌/测试:菜*?  "), "招牌_测试_菜_")
        self.assertEqual(platform_extra_points(["meituan"]), 0)
        self.assertEqual(platform_extra_points(["meituan", "jd"]), 100)
        self.assertEqual(platform_extra_points(["meituan", "taobao", "jd"]), 200)

    def test_smoke_export_script_builds_placeholder_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_smoke_export(Path(tmp) / "exports")
            self.assertEqual(result["images"], 9)
            self.assertEqual(result["rows"], 9)
            zip_path = Path(result["zipPath"])
            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path) as zf:
                self.assertIn("delivery_report.xlsx", zf.namelist())
                self.assertEqual(len([name for name in zf.namelist() if name.startswith("images/")]), 9)


if __name__ == "__main__":
    unittest.main()
