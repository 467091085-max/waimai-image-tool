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
                if action == "SubmitTextToImageJob":
                    return {"JobId": "job-image3", "RequestId": "req-submit", "_Endpoint": app_module.TENCENT_AIART_HOST}
                if action == "QueryTextToImageJob":
                    return {
                        "JobStatusCode": "5",
                        "JobStatusMsg": "处理完成",
                        "ResultImage": ["https://cdn.example.test/result.jpg"],
                        "ResultDetails": ["Success"],
                        "RequestId": "req-query",
                        "_Endpoint": app_module.TENCENT_AIART_HOST,
                    }
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

            self.assertEqual(text_detail["action"], "SubmitTextToImageJob")
            self.assertEqual(text_detail["queryAction"], "QueryTextToImageJob")
            self.assertEqual(text_detail["jobId"], "job-image3")
            self.assertEqual(combo_detail["action"], "ReplaceBackground")
            text_prompt = str(payloads[0][1]["Prompt"])
            combo_prompt = str(payloads[2][1]["Prompt"])
            for required in ["纯文生图", "外卖平台主图", "主体完整", "背景必须跟所选背景一致", "不要出现任何文字", "logo", "水印"]:
                self.assertIn(required, text_prompt)
            for required in ["套餐组合外卖主图", "牛肉饭", "鸡腿", "外卖平台主图", "主体完整", "背景必须跟所选背景一致", "不要出现任何文字", "logo", "水印"]:
                self.assertIn(required, combo_prompt)
            self.assertEqual(payloads[0][0], "SubmitTextToImageJob")
            self.assertEqual(payloads[1][0], "QueryTextToImageJob")
            self.assertEqual(payloads[0][1]["LogoAdd"], 0)
            self.assertEqual(payloads[0][1]["Revise"], 1)
            self.assertNotIn("NegativePrompt", payloads[0][1])
            self.assertEqual(payloads[2][1]["ProductUrl"], source["url"])

    def test_text_to_image3_can_fallback_to_lite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.jpg"
            calls: list[str] = []

            def fake_api(action: str, payload: dict[str, object], timeout: int = 70) -> dict[str, object]:
                calls.append(action)
                if action == "SubmitTextToImageJob":
                    raise RuntimeError("3.0 quota not ready")
                if action == "TextToImageLite":
                    return {"ResultImage": "https://cdn.example.test/result.jpg", "RequestId": "req-lite", "Seed": 456}
                raise AssertionError(action)

            with (
                mock.patch.object(app_module, "tencent_api_request", side_effect=fake_api),
                mock.patch.object(app_module, "save_result_image"),
            ):
                detail = app_module.tencent_text_to_image(menu_row(1, "招牌牛肉饭", "单品", []), "style-4", "standard", target)

            self.assertEqual(calls, ["SubmitTextToImageJob", "TextToImageLite"])
            self.assertEqual(detail["action"], "TextToImageLite")
            self.assertEqual(detail["fallbackFrom"], "SubmitTextToImageJob")

    def test_text_to_image_tries_aiart_before_hunyuan_and_aggregates_resource_errors(self) -> None:
        calls: list[str] = []

        def fake_cloud(action: str, payload: dict[str, object], host: str, service: str, version: str, timeout: int = 70) -> dict[str, object]:
            calls.append(host)
            raise RuntimeError(f"{host} ResourceInsufficient: 资源不足")

        with mock.patch.object(app_module, "tencent_cloud_api_request", side_effect=fake_cloud):
            with self.assertRaisesRegex(RuntimeError, "aiart.tencentcloudapi.com.*hunyuan.tencentcloudapi.com"):
                app_module.tencent_api_request("TextToImageLite", {"Prompt": "测试"})

        self.assertEqual(calls, [app_module.TENCENT_AIART_HOST, app_module.TENCENT_HUNYUAN_HOST])

    def test_preview_keeps_replace_background_error_when_local_fallback_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])

            with (
                mock.patch.dict("os.environ", {"ALLOW_LOCAL_IMAGE_FALLBACK": "true"}),
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=RuntimeError("aiart not open")),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan no quota")),
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "fallback")
            self.assertEqual(generation["fallbackFrom"], "tencent-hunyuan")
            self.assertIn("商品背景生成失败", generation["error"])
            self.assertIn("文生图兜底失败", generation["error"])

    def test_preview_model_failure_does_not_use_local_fake_image_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=RuntimeError("aiart not open")),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan no quota")),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "failed")
            self.assertIn("商品背景生成失败", generation["error"])
            draw_demo.assert_not_called()

    def test_preview_reuses_same_style_candidate_without_tencent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "same_style.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background") as replace,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-2", "standard")

            replace.assert_not_called()
            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "reused")
            self.assertEqual(preview_candidate["styleId"], "style-2")

    def test_preview_sample_materializes_with_tencent_instead_of_local_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])
            calls: list[str] = []

            def fake_replace(row_arg: dict[str, object], source_candidate: dict[str, object], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, object]:
                calls.append("ReplaceBackground")
                save_image(target, (80, 130, 180))
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "replace_background", "requestId": "preview-rb"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=fake_replace),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "succeeded")
            self.assertEqual(calls, ["ReplaceBackground"])
            self.assertEqual(preview_candidate["aiProvider"], "tencent-hunyuan")
            draw_demo.assert_not_called()

    def test_style_background_sample_uses_tencent_when_ready_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_style(style_id: str, target: Path) -> dict:
                save_image(target, (80, 120, 160))
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "style_background", "requestId": "style-bg"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_style_background", side_effect=fake_style) as tencent_style,
            ):
                style_candidate = app_module.style_sample_candidate("style-6")

            tencent_style.assert_called_once()
            self.assertEqual(style_candidate["source"], "tencent-style-sample")
            self.assertEqual(style_candidate["aiProvider"], "tencent-hunyuan")
            self.assertTrue((root / "_style_backgrounds" / "style-6" / "背景风格样图.jpg").exists())

    def test_style_background_sample_prefers_real_library_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "seed_real_store" / "style-6" / "招牌辣椒炒肉盖码饭.jpg"
            fallback = root / "_style_backgrounds" / "style-6" / "背景风格样图.jpg"
            save_image(real, (220, 80, 60))
            save_image(fallback, (80, 110, 140))
            app_module.library_images.cache_clear()

            try:
                with (
                    mock.patch.object(app_module, "LIBRARY_DIR", root),
                    mock.patch.object(app_module, "ensure_demo_data"),
                    mock.patch.object(app_module, "tencent_ready", return_value=True),
                    mock.patch.object(app_module, "tencent_style_background") as tencent_style,
                    mock.patch.object(app_module, "draw_demo_image") as draw_demo,
                ):
                    style_candidate = app_module.style_sample_candidate("style-6")
            finally:
                app_module.library_images.cache_clear()

            self.assertEqual(style_candidate["dishName"], "招牌辣椒炒肉盖码饭")
            self.assertEqual(style_candidate["source"], "internal")
            self.assertEqual(style_candidate["styleSampleSource"], "library")
            self.assertIn("seed_real_store", style_candidate["url"])
            tencent_style.assert_not_called()
            draw_demo.assert_not_called()

    def test_style_options_prioritize_real_library_styles_over_builtin_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "真店A" / "辣椒炒肉盖码饭.jpg"
            second = root / "真店B" / "小炒黄牛肉盖码饭.jpg"
            save_image(first, (210, 88, 60))
            save_image(second, (80, 150, 95))
            library = [
                app_module.LibraryImage("real-a", first, "真店A", "辣椒炒肉盖码饭", app_module.normalize("辣椒炒肉盖码饭"), app_module.grams(app_module.normalize("辣椒炒肉盖码饭")), "real-style-a", "clean", True),
                app_module.LibraryImage("real-b", second, "真店B", "小炒黄牛肉盖码饭", app_module.normalize("小炒黄牛肉盖码饭"), app_module.grams(app_module.normalize("小炒黄牛肉盖码饭")), "real-style-b", "clean", True),
            ]
            rows = [
                menu_row(1, "完全未匹配菜品A", "单品", []),
                menu_row(2, "完全未匹配菜品B", "单品", []),
            ]

            with mock.patch.object(app_module, "library_images", return_value=library):
                styles = app_module.style_options(rows)

            self.assertEqual(len(styles), app_module.PREVIEW_SAMPLE_COUNT)
            self.assertEqual({styles[0]["id"], styles[1]["id"]}, {"real-style-a", "real-style-b"})
            self.assertEqual(styles[0]["sample"]["styleSampleSource"], "library")
            self.assertEqual(styles[1]["sample"]["styleSampleSource"], "library")
            self.assertNotEqual(styles[0]["sample"]["source"], "generated-style-sample")
            self.assertNotEqual(styles[1]["sample"]["source"], "generated-style-sample")

    def test_style_options_replace_duplicate_background_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "真店A" / "style-a" / "辣椒炒肉盖码饭.jpg"
            second = root / "真店B" / "style-b" / "小炒黄牛肉盖码饭.jpg"
            save_image(first, (190, 120, 80))
            save_image(second, (190, 120, 80))
            library = [
                app_module.LibraryImage("real-a", first, "真店A", "辣椒炒肉盖码饭", app_module.normalize("辣椒炒肉盖码饭"), app_module.grams(app_module.normalize("辣椒炒肉盖码饭")), "real-style-a", "clean", True),
                app_module.LibraryImage("real-b", second, "真店B", "小炒黄牛肉盖码饭", app_module.normalize("小炒黄牛肉盖码饭"), app_module.grams(app_module.normalize("小炒黄牛肉盖码饭")), "real-style-b", "clean", True),
            ]
            rows = [
                menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(first, "辣椒炒肉盖码饭", "real-style-a")]),
                menu_row(2, "小炒黄牛肉盖码饭", "单品", [candidate(second, "小炒黄牛肉盖码饭", "real-style-b")]),
            ]

            app_module.background_signature_for_path.cache_clear()
            try:
                with (
                    mock.patch.object(app_module, "LIBRARY_DIR", root),
                    mock.patch.object(app_module, "library_images", return_value=library),
                    mock.patch.object(app_module, "tencent_ready", return_value=False),
                ):
                    styles = app_module.style_options(rows)
            finally:
                app_module.background_signature_for_path.cache_clear()

            signatures = [app_module.candidate_background_signature(style["sample"]) for style in styles]
            self.assertEqual(len(styles), app_module.PREVIEW_SAMPLE_COUNT)
            self.assertEqual(len(signatures), len(set(signatures)))
            self.assertIn("generated-style-sample", {style["sample"]["source"] for style in styles})

    def test_style_background_sample_can_use_tencent_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[str] = []

            def fake_style_background(style_id: str, target: Path) -> dict[str, object]:
                calls.append(style_id)
                save_image(target, (120, 150, 190))
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "style_background", "requestId": "style-bg"}

            with (
                mock.patch.dict("os.environ", {"GENERATE_STYLE_BACKGROUNDS_WITH_TENCENT": "true"}),
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_style_background", side_effect=fake_style_background),
            ):
                style_candidate = app_module.style_sample_candidate("style-6")

            self.assertEqual(calls, ["style-6"])
            self.assertEqual(style_candidate["aiProvider"], "tencent-hunyuan")
            self.assertEqual(style_candidate["generationAction"], "ReplaceBackground")

    def test_style_background_generation_uses_replace_background_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "seed.jpg"
            save_image(source)
            target = root / "style.jpg"
            payloads: list[tuple[str, dict[str, object]]] = []

            def fake_api(action: str, payload: dict[str, object], timeout: int = app_module.TENCENT_REQUEST_TIMEOUT) -> dict[str, object]:
                payloads.append((action, payload))
                return {"ResultImage": "https://cdn.example.test/style.jpg", "RequestId": "style-rb"}

            with (
                mock.patch.object(app_module, "style_background_seed_candidate", return_value=candidate(source, "辣椒炒肉", "style-1")),
                mock.patch.object(app_module, "candidate_public_url", return_value="https://cdn.example.test/seed.jpg"),
                mock.patch.object(app_module, "tencent_api_request", side_effect=fake_api),
                mock.patch.object(app_module, "save_result_image"),
            ):
                detail = app_module.tencent_style_background("style-2", target)

            self.assertEqual(detail["action"], "ReplaceBackground")
            self.assertEqual(payloads[0][0], "ReplaceBackground")
            self.assertEqual(payloads[0][1]["ProductUrl"], "https://cdn.example.test/seed.jpg")
            self.assertIn("背景风格样图", str(payloads[0][1]["Prompt"]))

    def test_candidate_public_url_encodes_chinese_paths(self) -> None:
        with app_module.app.test_request_context(base_url="https://waimai.example.test"):
            url = app_module.candidate_public_url({"url": "/media/门店/style-1/辣椒炒肉(热销).jpg"})

        self.assertIn("%E9%97%A8%E5%BA%97", url)
        self.assertIn("%E8%BE%A3%E6%A4%92%E7%82%92%E8%82%89", url)
        self.assertTrue(url.startswith("https://waimai.example.test/media/"))

    def test_model_input_public_url_copies_to_ascii_public_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "中文菜名.jpg"
            save_image(source)
            with (
                app_module.app.test_request_context(base_url="https://waimai.example.test"),
                mock.patch.object(app_module, "MODEL_INPUT_DIR", root / "model_inputs"),
            ):
                url = app_module.model_input_public_url({"path": str(source), "url": "/media/中文菜名.jpg"})

            self.assertRegex(url, r"^https://waimai\.example\.test/model-inputs/[a-f0-9]{24}\.jpg$")
            self.assertTrue((root / "model_inputs").exists())

    def test_export_api_rejects_missing_style_and_empty_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            missing_style = client.post("/api/export", json={"platforms": ["meituan"]})
            self.assertEqual(missing_style.status_code, 400)
            self.assertIn("请先选择风格", missing_style.get_json()["error"])

            plan = {"results": [menu_row(1, "待生成菜品", "单品", [])]}
            with (
                mock.patch.object(app_module, "EXPORT_DIR", root / "exports"),
                mock.patch.object(app_module, "build_plan", return_value=plan),
            ):
                empty = client.post("/api/export", json={"style": "style-1", "platforms": ["meituan"]})

            self.assertEqual(empty.status_code, 400)
            body = empty.get_json()
            self.assertIn("没有可导出的成图", body["error"])
            self.assertEqual(body["export"]["images"], 0)

    def test_export_api_returns_zip_when_images_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉盖码饭", "style-1")])
            row["backgroundAction"] = "背景一致，直接复用"
            row["publicStatus"] = "已生成"
            plan = {"results": [row]}
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            with (
                mock.patch.object(app_module, "EXPORT_DIR", root / "exports"),
                mock.patch.object(app_module, "build_plan", return_value=plan),
            ):
                response = client.post("/api/export", json={"style": "style-1", "platforms": ["meituan", "jd"], "format": "jpg"})

            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertEqual(body["images"], 2)
            self.assertEqual(body["platforms"], ["meituan", "jd"])
            self.assertRegex(body["download"], r"^/download/export_.*?/result\.zip$")

    def test_style_preview_manifest_does_not_generate_synchronously(self) -> None:
        with (
            mock.patch.object(app_module, "parse_menu", return_value={"items": [menu_row(1, "辣椒炒肉盖码饭", "单品", [])]}),
            mock.patch.object(app_module, "library_images", return_value=[]),
            mock.patch.object(app_module, "materialize_preview_candidate") as materialize,
            mock.patch.object(app_module, "generated_preview_candidate", return_value=None),
        ):
            manifest = app_module.preview_samples("style-1", generate=False)

        materialize.assert_not_called()
        self.assertEqual(manifest["previewFreeImages"], app_module.PREVIEW_SAMPLE_COUNT)
        self.assertEqual(manifest["samples"][0]["generation"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
