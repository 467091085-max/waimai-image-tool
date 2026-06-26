from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops, ImageDraw

import app as app_module
from image_pipeline import (
    PLATFORMS,
    apply_watermark,
    export_delivery_zip,
    platform_extra_points,
    prepare_platform_image,
    require_platforms,
)


def png_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


class ExportWatermarkPlatformsTest(unittest.TestCase):
    def test_platform_pricing_treats_any_single_platform_as_free(self) -> None:
        self.assertEqual(platform_extra_points(["meituan"]), 0)
        self.assertEqual(platform_extra_points(["taobao"]), 0)
        self.assertEqual(platform_extra_points(["jd"]), 0)
        self.assertEqual(platform_extra_points(["taobao", "jd"]), 100)
        self.assertEqual(platform_extra_points(["jd", "taobao", "meituan"]), 200)

        with self.assertRaisesRegex(ValueError, "至少选择一个"):
            require_platforms([])

    def test_export_api_rejects_empty_platforms_before_building_plan(self) -> None:
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()

        with mock.patch.object(app_module, "build_plan") as build_plan:
            response = client.post("/api/export", json={"style": "style-1", "platforms": []})

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body["code"], "platform_required")
        self.assertIn("至少选择一个", body["error"])
        build_plan.assert_not_called()

    def test_zip_uses_excel_dish_names_selected_platforms_and_requested_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            src = Image.new("RGBA", (1000, 760), (244, 241, 235, 255))
            draw = ImageDraw.Draw(src)
            draw.ellipse((260, 170, 760, 700), fill=(210, 70, 58, 255))
            src.save(source)

            result = export_delivery_zip(
                [
                    {
                        "id": "dish-1",
                        "row": 1,
                        "name": "招牌/辣椒炒肉盖码饭",
                        "category": "热销",
                        "kind": "单品",
                        "points": 100,
                        "backgroundAction": "背景一致，直接复用",
                        "candidates": [{"path": str(source)}],
                    }
                ],
                root / "exports",
                platforms=["taobao", "jd"],
                image_format="png",
                watermark={"enabled": True, "type": "text", "text": "蒜头", "color": "white", "position": "bottom-right"},
                run_name="selected_platforms",
            )

            self.assertEqual(result["images"], 2)
            self.assertEqual(result["platforms"], ["taobao", "jd"])
            self.assertEqual(result["extraPlatformPoints"], 100)

            zip_path = root / "exports" / result["download"].split("/download/", 1)[1]
            with zipfile.ZipFile(zip_path) as zf:
                image_names = sorted(name for name in zf.namelist() if name.startswith("images/"))
                self.assertEqual(len(image_names), 2)
                self.assertTrue(any(name.endswith("/招牌_辣椒炒肉盖码饭.png") for name in image_names))
                self.assertEqual({name.split("/")[1].split("_", 1)[0] for name in image_names}, {"taobao", "jd"})
                for name in image_names:
                    platform_id = name.split("/")[1].split("_", 1)[0]
                    spec = PLATFORMS[platform_id]
                    payload = zf.read(name)
                    image = Image.open(io.BytesIO(payload))
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.size, (spec["width"], spec["height"]))
                    self.assertLessEqual(len(payload), spec["maxKB"] * 1024)

    def test_text_and_transparent_logo_watermarks_stay_inside_each_platform_safe_area(self) -> None:
        base = Image.new("RGBA", (980, 680), (235, 232, 226, 255))
        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle((160, 90, 820, 620), radius=60, fill=(218, 80, 60, 255))
        logo = Image.new("RGBA", (120, 72), (0, 0, 0, 0))
        logo_draw = ImageDraw.Draw(logo)
        logo_draw.rectangle((18, 14, 102, 58), fill=(32, 112, 220, 210))
        logo_url = png_data_url(logo)

        watermark_cases = [
            {"enabled": True, "type": "text", "text": "蒜头", "color": "black", "position": "top-left"},
            {"enabled": True, "type": "text", "text": "蒜头", "color": "white", "position": "bottom-right"},
            {"enabled": True, "type": "logo", "logoData": logo_url, "position": "top-right"},
        ]

        for platform_id in PLATFORMS:
            clean = prepare_platform_image(base, platform_id)
            margin = max(24, clean.width // 34)
            for watermark in watermark_cases:
                with self.subTest(platform=platform_id, watermark=watermark["type"], position=watermark["position"]):
                    marked = apply_watermark(clean, watermark)
                    bbox = ImageChops.difference(clean.convert("RGB"), marked.convert("RGB")).getbbox()
                    self.assertIsNotNone(bbox)
                    assert bbox is not None
                    left, top, right, bottom = bbox
                    self.assertGreaterEqual(left, margin)
                    self.assertGreaterEqual(top, margin)
                    self.assertLessEqual(right, clean.width - margin)
                    self.assertLessEqual(bottom, clean.height - margin)


if __name__ == "__main__":
    unittest.main()
