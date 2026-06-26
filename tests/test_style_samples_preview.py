from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import app as app_module


def save_image(path: Path, color: tuple[int, int, int] = (220, 90, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 72), color).save(path)


def menu_row(row: int, name: str, kind: str = "单品", candidates: list[dict[str, object]] | None = None) -> dict[str, object]:
    norm = app_module.normalize(name)
    return {
        "row": row,
        "category": "测试",
        "name": name,
        "norm": norm,
        "grams": app_module.grams(norm),
        "price": "",
        "kind": kind,
        "components": [],
        "candidates": candidates or [],
        "status": "直接可用" if candidates else "未找到",
    }


def candidate(path: Path, dish: str, style_id: str) -> dict[str, object]:
    return {
        "imageId": path.stem,
        "score": 92.0,
        "confidence": 92.0,
        "dishName": dish,
        "store": "测试图库",
        "styleId": style_id,
        "styleName": style_id,
        "source": "clean",
        "reusable": True,
        "url": f"/media/{path.name}",
        "path": str(path),
        "generated": False,
    }


class StyleSamplesPreviewTests(unittest.TestCase):
    def tearDown(self) -> None:
        app_module.background_signature_for_path.cache_clear()
        app_module.image_hash_signature_for_path.cache_clear()
        app_module.library_images.cache_clear()

    def test_style_options_show_six_numbered_backgrounds_and_hunyuan_fills_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "真店A" / "style-a" / "辣椒炒肉盖码饭.jpg"
            duplicate = root / "真店B" / "style-b" / "小炒黄牛肉盖码饭.jpg"
            save_image(first, (190, 120, 80))
            save_image(duplicate, (190, 120, 80))
            library = [
                app_module.LibraryImage("real-a", first, "真店A", "辣椒炒肉盖码饭", app_module.normalize("辣椒炒肉盖码饭"), app_module.grams(app_module.normalize("辣椒炒肉盖码饭")), "real-style-a", "clean", True),
                app_module.LibraryImage("real-b", duplicate, "真店B", "小炒黄牛肉盖码饭", app_module.normalize("小炒黄牛肉盖码饭"), app_module.grams(app_module.normalize("小炒黄牛肉盖码饭")), "real-style-b", "clean", True),
            ]
            rows = [
                menu_row(1, "辣椒炒肉盖码饭", candidates=[candidate(first, "辣椒炒肉盖码饭", "real-style-a")]),
                menu_row(2, "小炒黄牛肉盖码饭", candidates=[candidate(duplicate, "小炒黄牛肉盖码饭", "real-style-b")]),
            ]
            generated_colors = [(40, 70, 120), (80, 160, 90), (210, 80, 80), (120, 80, 190), (60, 150, 180), (225, 170, 80)]
            generated_calls: list[str] = []

            def fake_style_background(style_id: str, target: Path) -> dict[str, object]:
                generated_calls.append(style_id)
                save_image(target, generated_colors[len(generated_calls) % len(generated_colors)])
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "style_background", "requestId": f"style-{len(generated_calls)}"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "library_images", return_value=library),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_style_background", side_effect=fake_style_background),
            ):
                styles = app_module.style_options(rows)

            self.assertEqual(len(styles), 6)
            self.assertEqual([style["name"] for style in styles], ["一号背景", "二号背景", "三号背景", "四号背景", "五号背景", "六号背景"])
            self.assertEqual(len({style["dedupeSignature"] for style in styles}), 6)
            self.assertTrue(generated_calls)
            self.assertEqual(styles[0]["source"], "real")
            self.assertTrue(any(style["needsGeneratedBackground"] for style in styles))

    def test_preview_samples_only_use_six_single_dishes(self) -> None:
        menu = {
            "items": [
                menu_row(1, "百事可乐", "单品"),
                menu_row(2, "红烧肉套餐", "套餐/组合"),
                menu_row(3, "辣椒炒肉盖码饭", "单品"),
                menu_row(4, "小炒黄牛肉盖码饭", "单品"),
            ]
        }

        with (
            mock.patch.object(app_module, "parse_menu", return_value=menu),
            mock.patch.object(app_module, "library_images", return_value=[]),
            mock.patch.object(app_module, "materialize_preview_candidate", return_value=(None, app_module.queued_generation_payload())) as materialize,
        ):
            manifest = app_module.preview_samples("style-1")

        self.assertEqual(len(manifest["samples"]), 6)
        self.assertEqual(materialize.call_count, 6)
        names = [sample["name"] for sample in manifest["samples"]]
        self.assertIn("辣椒炒肉盖码饭", names)
        self.assertIn("小炒黄牛肉盖码饭", names)
        self.assertFalse(any("可乐" in name or "套餐" in name for name in names))
        self.assertTrue(all(sample["kind"] == "单品" for sample in manifest["samples"]))

    def test_preview_generation_failure_returns_real_error_without_local_fake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品")

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "allow_local_image_fallback", return_value=True),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan quota exhausted")),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            draw_demo.assert_not_called()
            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "failed")
            self.assertEqual(generation["action"], "TextToImage")
            self.assertIn("hunyuan quota exhausted", generation["error"])


if __name__ == "__main__":
    unittest.main()
