from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import app as app_module


def save_image(path: Path, color: tuple[int, int, int] = (220, 90, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path)


def candidate(path: Path, dish: str, style_id: str, reusable: bool = True, generated: bool = False) -> dict[str, object]:
    return {
        "imageId": path.stem,
        "score": 92.0,
        "dishName": dish,
        "store": "测试图库",
        "styleId": style_id,
        "styleName": "测试风格",
        "source": "generated-preview" if generated else "internal",
        "reusable": reusable,
        "url": f"https://cdn.example.test/{path.name}",
        "path": str(path),
        "generated": generated,
    }


def menu_row(row: int, name: str, kind: str, candidates: list[dict[str, object]], components: list[str] | None = None) -> dict[str, object]:
    return {
        "row": row,
        "category": "测试",
        "name": name,
        "kind": kind,
        "components": components or [],
        "candidates": candidates,
        "backgroundAction": "",
        "publicStatus": "",
    }


class AppGenerationTests(unittest.TestCase):
    def test_materialize_routes_required_rows_to_replace_or_text_and_reuses_same_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / f"source_{idx}.jpg" for idx in range(4)]
            for idx, path in enumerate(sources):
                save_image(path, (180 + idx, 80, 60))

            rows = [
                menu_row(1, "红烧肉", "单品", [candidate(sources[0], "红烧肉", "style-2")]),
                menu_row(2, "新品汤", "单品", []),
                menu_row(3, "清炒时蔬", "单品", [candidate(sources[1], "清炒时蔬", "style-1")]),
                menu_row(4, "红烧肉+青菜套餐", "套餐/组合", [candidate(sources[2], "红烧肉+青菜套餐", "style-1")], ["红烧肉", "青菜"]),
            ]
            plan = {"results": rows}
            calls: list[tuple[str, str]] = []

            def fake_replace(row: dict[str, object], source_candidate: dict[str, object], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, object]:
                calls.append(("ReplaceBackground", str(row["name"])))
                save_image(target, (20, 120, 80))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "ReplaceBackground",
                    "promptType": "combo" if row.get("kind") == "套餐/组合" else "replace_background",
                    "requestId": f"rb-{row['row']}",
                }

            def fake_text(row: dict[str, object], style_id: str, quality: str | None, target: Path) -> dict[str, object]:
                calls.append(("TextToImageLite", str(row["name"])))
                save_image(target, (40, 80, 180))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "TextToImageLite",
                    "promptType": "combo" if row.get("kind") == "套餐/组合" else "text_to_image",
                    "requestId": f"txt-{row['row']}",
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 10),
                mock.patch.object(app_module, "tencent_status_payload", return_value={"provider": "tencent-hunyuan", "configured": True}),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=fake_replace),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text),
            ):
                generation = app_module.materialize_final_images(plan, "style-1", "standard")

            self.assertEqual(generation["attempted"], 3)
            self.assertEqual(generation["succeeded"], 3)
            self.assertEqual(generation["fallback"], 0)
            self.assertEqual(generation["skipped"], 1)
            self.assertEqual(generation["actions"], {"ReplaceBackground": 2, "Reuse": 1, "TextToImageLite": 1})
            self.assertEqual(calls, [("ReplaceBackground", "红烧肉"), ("TextToImageLite", "新品汤"), ("ReplaceBackground", "红烧肉+青菜套餐")])
            self.assertEqual(rows[2]["generation"]["status"], "reused")
            self.assertEqual(rows[3]["generation"]["promptType"], "combo")
            self.assertTrue(str(rows[0]["candidates"][0]["source"]).startswith("tencent-ReplaceBackground"))
            self.assertTrue(str(rows[1]["candidates"][0]["source"]).startswith("tencent-TextToImageLite"))

    def test_sync_limit_marks_unattempted_rows_without_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / f"limit_source_{idx}.jpg" for idx in range(3)]
            for path in sources:
                save_image(path)

            rows = [
                menu_row(1, "菜品A", "单品", [candidate(sources[0], "菜品A", "style-2")]),
                menu_row(2, "菜品B", "单品", [candidate(sources[1], "菜品B", "style-2")]),
                menu_row(3, "菜品C", "单品", [candidate(sources[2], "菜品C", "style-2")]),
            ]
            plan = {"results": rows}

            def fake_replace(row: dict[str, object], source_candidate: dict[str, object], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, object]:
                save_image(target, (10, 160, 90))
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "replace_background", "requestId": "one"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 1),
                mock.patch.object(app_module, "tencent_status_payload", return_value={"provider": "tencent-hunyuan", "configured": True}),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=fake_replace),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                generation = app_module.materialize_final_images(plan, "style-1", "standard")

            self.assertEqual(generation["attempted"], 1)
            self.assertEqual(generation["succeeded"], 1)
            self.assertEqual(generation["limited"], 2)
            self.assertEqual(generation["pending"], 2)
            self.assertEqual(generation["fallback"], 0)
            draw_demo.assert_not_called()
            self.assertEqual(rows[1]["generation"]["status"], "limited")
            self.assertEqual(rows[1]["publicStatus"], "待正式生成")
            self.assertEqual(rows[1]["generationStatus"], "limited")
            self.assertEqual(rows[1]["candidates"][0]["source"], "internal")

    def test_tencent_prompts_have_fixed_constraints_for_text_replace_and_combo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "out.jpg"
            payloads: list[tuple[str, dict[str, object]]] = []

            def fake_api(action: str, payload: dict[str, object], timeout: int = 70) -> dict[str, object]:
                payloads.append((action, payload))
                return {"ResultImage": "https://cdn.example.test/result.jpg", "RequestId": f"req-{action}", "Seed": 123}

            single = menu_row(1, "招牌牛肉饭", "单品", [])
            combo = menu_row(2, "牛肉饭+鸡腿套餐", "套餐/组合", [], ["牛肉饭", "鸡腿"])
            source = candidate(root / "source.jpg", "牛肉饭+鸡腿套餐", "style-2")

            with (
                mock.patch.object(app_module, "tencent_api_request", side_effect=fake_api),
                mock.patch.object(app_module, "save_result_image"),
            ):
                text_detail = app_module.tencent_text_to_image(single, "style-4", "premium", target)
                combo_detail = app_module.tencent_replace_background(combo, source, "style-4", target)

            self.assertEqual(text_detail["action"], "TextToImageLite")
            self.assertEqual(combo_detail["action"], "ReplaceBackground")
            text_prompt = str(payloads[0][1]["Prompt"])
            combo_prompt = str(payloads[1][1]["Prompt"])
            for required in ["纯文生图", "外卖平台主图", "主体完整", "背景必须跟所选背景一致", "不要出现任何文字", "logo", "水印"]:
                self.assertIn(required, text_prompt)
            for required in ["套餐组合外卖主图", "牛肉饭", "鸡腿", "外卖平台主图", "主体完整", "背景必须跟所选背景一致", "不要出现任何文字", "logo", "水印"]:
                self.assertIn(required, combo_prompt)
            self.assertEqual(payloads[0][1]["NegativePrompt"], app_module.NEGATIVE_IMAGE_PROMPT)
            self.assertEqual(payloads[1][1]["ProductUrl"], source["url"])


if __name__ == "__main__":
    unittest.main()
