from __future__ import annotations

import tempfile
import unittest
import zipfile
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
    def test_materialize_routes_required_rows_to_reuse_replace_or_text(self) -> None:
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

            self.assertEqual(generation["attempted"], 2)
            self.assertEqual(generation["succeeded"], 2)
            self.assertEqual(generation["fallback"], 0)
            self.assertEqual(generation["skipped"], 2)
            self.assertEqual(generation["actions"], {"ReplaceBackground": 1, "TextToImageLite": 1, "Reuse": 2})
            self.assertEqual(calls, [("ReplaceBackground", "红烧肉"), ("TextToImageLite", "新品汤")])
            self.assertEqual(rows[2]["generation"]["status"], "reused")
            self.assertEqual(rows[2]["generation"]["reason"], "same_dish_same_style")
            self.assertEqual(rows[3]["generation"]["status"], "reused")
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
            for required in ["C 图库没有该菜", "纯文生图", "外卖平台主图", "主体完整", "背景必须跟所选背景一致", "不要出现任何文字", "logo", "水印"]:
                self.assertIn(required, text_prompt)
            for required in ["B 同菜不同背景套餐/组合换背景", "保留参考图", "牛肉饭", "鸡腿", "外卖平台套餐主图", "主体完整", "必须把原背景完整替换", "不要保留原桌面", "logo", "水印"]:
                self.assertIn(required, combo_prompt)
            redraw_prompt = app_module.prompt_for_generation(single, "style-4", "premium", "watermark_redraw")
            for required in ["B 同菜不同背景去品牌水印重绘", "保持菜品种类", "必须把原背景完整替换", "品牌水印", "生成干净可交付成图"]:
                self.assertIn(required, redraw_prompt)
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

    def test_formal_runner_combo_without_combo_reference_uses_ai_combo_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            single_source = root / "single.jpg"
            save_image(single_source)
            row = menu_row(
                1,
                "红烧肉+青菜套餐",
                "套餐/组合",
                [candidate(single_source, "红烧肉", "style-2")],
                ["红烧肉", "青菜"],
            )
            calls: list[str] = []

            def fake_text(row_arg: dict[str, object], style_id: str, quality: str | None, target: Path) -> dict[str, object]:
                calls.append(f"text:{row_arg['name']}")
                save_image(target, (60, 120, 180))
                return {"provider": "tencent-hunyuan", "action": "SubmitTextToImageJob", "promptType": "combo", "requestId": "combo-text"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text),
                mock.patch.object(app_module, "tencent_replace_background") as replace,
            ):
                output = app_module.run_formal_generation_item(row, style="style-1", quality="standard")

            replace.assert_not_called()
            self.assertEqual(calls, ["text:红烧肉+青菜套餐"])
            self.assertEqual(output["result"]["status"], "succeeded")
            self.assertEqual(output["result"]["kind"], "combo")
            self.assertEqual(output["result"]["sourceStrategy"], "text_to_image3")
            self.assertEqual(row["generation"]["promptType"], "combo")

    def test_formal_runner_uses_reference_redraw_for_watermarked_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "watermarked.jpg"
            save_image(source)
            row = menu_row(
                1,
                "招牌牛肉饭",
                "单品",
                [{**candidate(source, "招牌牛肉饭", "style-2", reusable=False), "source": "watermarkpic"}],
            )
            calls: list[str] = []

            def fake_redraw(row_arg: dict[str, object], source_candidate: dict[str, object], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, object]:
                calls.append(str(source_candidate["source"]))
                save_image(target, (160, 80, 120))
                return {"provider": "tencent-hunyuan", "action": "ReferenceRedraw", "promptType": "watermark_redraw", "requestId": "redraw"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_reference_redraw", side_effect=fake_redraw),
                mock.patch.object(app_module, "tencent_text_to_image") as text,
            ):
                output = app_module.run_formal_generation_item(row, style="style-1", quality="premium")

            text.assert_not_called()
            self.assertEqual(calls, ["watermarkpic"])
            self.assertEqual(output["result"]["sourceStrategy"], "reference_redraw")
            self.assertEqual(output["result"]["action"], "ReferenceRedraw")
            self.assertEqual(row["generationStatus"], "succeeded")

    def test_formal_runner_provider_failure_returns_refund_hook_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = menu_row(1, "新品汤", "单品", [])

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan.tencentcloudapi.com ResourceInsufficient: 资源不足")),
            ):
                output = app_module.run_formal_generation_item(row, style="style-1", quality="standard")

            self.assertEqual(output["result"]["status"], "failed")
            self.assertIn("ResourceInsufficient", output["result"]["providerError"])
            self.assertTrue(output["result"]["retryable"])
            self.assertTrue(output["result"]["refundRequired"])
            self.assertEqual(row["generationStatus"], "failed")

    def test_formal_runner_abc_routes_and_evidence_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            same_source = root / "same.jpg"
            diff_source = root / "diff.jpg"
            save_image(same_source)
            save_image(diff_source)
            same_row = menu_row(1, "红烧肉", "单品", [candidate(same_source, "红烧肉", "style-1")])
            diff_row = menu_row(2, "红烧肉", "单品", [candidate(diff_source, "红烧肉", "style-2")])
            missing_row = menu_row(3, "新品汤", "单品", [])
            calls: list[tuple[str, str]] = []

            def fake_replace(row_arg: dict[str, object], source_candidate: dict[str, object], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, object]:
                calls.append(("replace", str(row_arg["name"])))
                save_image(target, (20, 120, 80))
                return {
                    "status": "succeeded",
                    "provider": "tencent-hunyuan",
                    "action": "ReplaceBackground",
                    "promptType": "replace_background",
                    "requestId": "rb-real",
                    "endpoint": "aiart.tencentcloudapi.com",
                }

            def fake_text(row_arg: dict[str, object], style_id: str, quality: str | None, target: Path) -> dict[str, object]:
                calls.append(("text", str(row_arg["name"])))
                save_image(target, (40, 80, 180))
                return {
                    "status": "succeeded",
                    "provider": "tencent-hunyuan",
                    "action": "SubmitTextToImageJob",
                    "promptType": "text_to_image",
                    "requestId": "txt-real",
                    "jobId": "job-real",
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=fake_replace),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text),
                mock.patch.object(app_module, "tencent_cloud_api_request", side_effect=AssertionError("network disabled")),
            ):
                same_output = app_module.run_formal_generation_item(same_row, style="style-1", quality="standard")
                diff_output = app_module.run_formal_generation_item(diff_row, style="style-1", quality="standard")
                missing_output = app_module.run_formal_generation_item(missing_row, style="style-1", quality="standard")

            self.assertEqual(calls, [("replace", "红烧肉"), ("text", "新品汤")])
            self.assertEqual(same_output["result"]["status"], "reused")
            self.assertEqual(same_output["result"]["provider"], "library")
            self.assertEqual(same_output["result"]["action"], "Reuse")
            self.assertEqual(same_output["result"]["evidence"], {"provider": "library", "action": "Reuse", "status": "reused", "providerStatus": "succeeded", "provider_status": "succeeded"})

            self.assertEqual(diff_output["result"]["sourceStrategy"], "replace_background")
            self.assertEqual(diff_output["result"]["requestId"], "rb-real")
            self.assertEqual(diff_output["result"]["evidence"]["provider"], "tencent-hunyuan")
            self.assertEqual(diff_output["result"]["evidence"]["action"], "ReplaceBackground")
            self.assertEqual(diff_output["result"]["evidence"]["requestId"], "rb-real")
            self.assertEqual(diff_output["result"]["evidence"]["status"], "succeeded")

            self.assertEqual(missing_output["result"]["sourceStrategy"], "text_to_image3")
            self.assertEqual(missing_output["result"]["action"], "SubmitTextToImageJob")
            self.assertEqual(missing_output["result"]["evidence"]["requestId"], "txt-real")
            self.assertEqual(missing_output["result"]["evidence"]["jobId"], "job-real")

    def test_preview_keeps_replace_background_error_without_local_fake_when_fallback_enabled(self) -> None:
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
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan no quota")) as text,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "failed")
            self.assertEqual(generation["provider"], "tencent-hunyuan")
            self.assertEqual(generation["action"], "ReplaceBackground")
            self.assertIn("aiart not open", generation["error"])
            text.assert_not_called()

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
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan no quota")) as text,
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "failed")
            self.assertEqual(generation["action"], "ReplaceBackground")
            self.assertIn("aiart not open", generation["providerError"])
            text.assert_not_called()
            draw_demo.assert_not_called()

    def test_preview_unifies_same_style_candidate_with_tencent_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "same_style.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])

            def fake_replace(row_arg: dict[str, object], source_candidate: dict[str, object], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, object]:
                save_image(target, (90, 130, 170))
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "replace_background", "requestId": "same-preview"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=fake_replace) as replace,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-2", "standard")

            replace.assert_called_once()
            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "succeeded")
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
            job = app_module.style_background_job(style_candidate)
            self.assertEqual(job["evidence"]["provider"], "tencent-hunyuan")
            self.assertEqual(job["evidence"]["action"], "ReplaceBackground")
            self.assertEqual(job["evidence"]["requestId"], "style-bg")
            self.assertEqual(job["evidence"]["status"], "succeeded")

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

    def test_style_options_return_six_fixed_labels_unique_signatures_and_api_fields(self) -> None:
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
            app_module.image_hash_signature_for_path.cache_clear()
            try:
                with (
                    mock.patch.object(app_module, "LIBRARY_DIR", root),
                    mock.patch.object(app_module, "library_images", return_value=library),
                    mock.patch.object(app_module, "tencent_ready", return_value=False),
                ):
                    styles = app_module.style_options(rows)
            finally:
                app_module.background_signature_for_path.cache_clear()
                app_module.image_hash_signature_for_path.cache_clear()

            self.assertEqual(len(styles), app_module.PREVIEW_SAMPLE_COUNT)
            self.assertEqual([style["label"] for style in styles], list(app_module.BACKGROUND_LABELS))
            self.assertEqual([style["name"] for style in styles], list(app_module.BACKGROUND_LABELS))
            self.assertEqual(len({style["dedupeSignature"] for style in styles}), app_module.PREVIEW_SAMPLE_COUNT)
            for style in styles:
                self.assertIn(style["source"], {"real", "generated", "cache"})
                self.assertIn("imageUrl", style)
                self.assertIn("backgroundJob", style)
                self.assertEqual(style["needs_generated_background"], style["needsGeneratedBackground"])
                if style["needsGeneratedBackground"]:
                    self.assertIsNotNone(style["aiFillManifest"])
                    self.assertEqual(style["aiFillManifest"]["type"], "style_background")
                    self.assertEqual(style["generationManifest"], style["aiFillManifest"])
            self.assertTrue(any(style["source"] == "real" for style in styles))
            self.assertTrue(any(style["needsGeneratedBackground"] for style in styles))

    def test_style_background_generation_failure_does_not_return_local_fake_when_tencent_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_style_background", side_effect=RuntimeError("hunyuan quota exhausted")),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                style_candidate = app_module.style_background_candidate("style-5")

            draw_demo.assert_not_called()
            self.assertEqual(style_candidate["generationStatus"], "failed")
            self.assertEqual(style_candidate["generationProvider"], "tencent-hunyuan")
            self.assertTrue(style_candidate["needs_generated_background"])
            self.assertEqual(style_candidate["url"], "")
            self.assertEqual(style_candidate["path"], "")
            self.assertIn("hunyuan quota exhausted", style_candidate["error"])

    def test_style_background_sample_can_use_tencent_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[str] = []
            app_module.library_images.cache_clear()

            def fake_style_background(style_id: str, target: Path) -> dict[str, object]:
                calls.append(style_id)
                save_image(target, (120, 150, 190))
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": "style_background", "requestId": "style-bg"}

            try:
                with (
                    mock.patch.dict("os.environ", {"GENERATE_STYLE_BACKGROUNDS_WITH_TENCENT": "true"}),
                    mock.patch.object(app_module, "LIBRARY_DIR", root),
                    mock.patch.object(app_module, "tencent_ready", return_value=True),
                    mock.patch.object(app_module, "tencent_style_background", side_effect=fake_style_background),
                ):
                    style_candidate = app_module.style_sample_candidate("style-6")
            finally:
                app_module.library_images.cache_clear()

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

    def test_model_input_public_url_uploads_local_path_to_cos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "中文菜名.jpg"
            save_image(source)
            with (
                mock.patch.object(app_module, "MODEL_INPUT_DIR", root / "model_inputs"),
                mock.patch.object(app_module, "upload_model_input_to_cos", return_value="https://cos.example.test/model-input.jpg"),
            ):
                url = app_module.model_input_public_url({"path": str(source), "url": "/media/中文菜名.jpg"})

            self.assertEqual(url, "https://cos.example.test/model-input.jpg")
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
                response = client.post("/api/export", json={"style": "style-1", "platforms": ["meituan", "jd"], "imageFormat": "jpeg"})
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                download = client.get(body["download"])

            self.assertEqual(body["images"], 2)
            self.assertEqual(body["platforms"], ["meituan", "jd"])
            self.assertRegex(body["download"], r"^/download/export_.*?/result\.zip$")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.headers["Content-Disposition"].split("filename=", 1)[1], "result.zip")
            zip_path = root / "exports" / body["download"].split("/download/", 1)[1]
            with zipfile.ZipFile(zip_path) as zf:
                image_names = sorted(name for name in zf.namelist() if name.startswith("images/"))
                self.assertTrue(any("/meituan_" in name and name.endswith(".jpg") for name in image_names))
                self.assertTrue(any("/jd_" in name and name.endswith(".jpeg") for name in image_names))

    def test_export_api_returns_stable_json_when_zip_fails(self) -> None:
        plan = {"results": [menu_row(1, "辣椒炒肉盖码饭", "单品", [])]}
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()

        with (
            mock.patch.object(app_module, "build_plan", return_value=plan),
            mock.patch.object(app_module, "export_delivery_zip", side_effect=RuntimeError("is not valid")),
            mock.patch.object(app_module.app.logger, "exception"),
        ):
            response = client.post("/api/export", json={"style": "style-1", "platforms": ["meituan"], "format": "jpg"})

        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["code"], "export_failed")
        self.assertIn("导出失败", body["error"])
        self.assertNotIn("is not valid", body["error"])

    def test_download_returns_json_for_missing_or_invalid_export_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "exports").mkdir()
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            with mock.patch.object(app_module, "EXPORT_DIR", root / "exports"):
                missing = client.get("/download/missing/result.zip")
                traversal = client.get("/download/../secret.zip")

            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.get_json()["code"], "download_not_found")
            self.assertEqual(traversal.status_code, 404)
            self.assertEqual(traversal.get_json()["code"], "download_not_found")

    def test_style_preview_default_generates_six_preview_jobs(self) -> None:
        with (
            mock.patch.object(app_module, "parse_menu", return_value={"items": [menu_row(1, "辣椒炒肉盖码饭", "单品", [])]}),
            mock.patch.object(app_module, "library_images", return_value=[]),
            mock.patch.object(app_module, "materialize_preview_candidate", return_value=(None, app_module.queued_generation_payload())) as materialize,
            mock.patch.object(app_module, "generated_preview_candidate", return_value=None),
        ):
            manifest = app_module.preview_samples("style-1")

        self.assertEqual(materialize.call_count, app_module.PREVIEW_SAMPLE_COUNT)
        self.assertEqual(manifest["previewFreeImages"], app_module.PREVIEW_SAMPLE_COUNT)
        self.assertEqual(manifest["samples"][0]["generation"]["status"], "queued")
        self.assertIn("waiting_for_provider", manifest["samples"][0]["error"])

    def test_style_preview_api_returns_manifest_without_sync_generation(self) -> None:
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()
        with (
            mock.patch.object(app_module, "parse_menu", return_value={"items": [menu_row(1, "辣椒炒肉盖码饭", "单品", [])]}),
            mock.patch.object(app_module, "library_images", return_value=[]),
            mock.patch.object(app_module, "materialize_preview_candidate") as materialize,
            mock.patch.object(app_module, "generated_preview_candidate", return_value=None),
        ):
            response = client.get("/api/style-preview?style=style-1")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["previewFreeImages"], app_module.PREVIEW_SAMPLE_COUNT)
        self.assertEqual(len(body["samples"]), app_module.PREVIEW_SAMPLE_COUNT)
        self.assertIn(body["samples"][0]["generation"]["status"], {"pending", "queued"})
        materialize.assert_not_called()

    def test_preview_sample_entries_filter_drinks_sides_rice_prompts_and_combos(self) -> None:
        items = [
            menu_row(1, "百事可乐", "单品", []),
            menu_row(2, "一碗米饭", "单品", []),
            menu_row(3, "辣椒包", "单品", []),
            menu_row(4, "温馨提示勿点", "单品", []),
            menu_row(5, "红烧肉套餐", "套餐/组合", []),
            menu_row(6, "辣椒炒肉盖码饭", "单品", []),
            menu_row(7, "小炒黄牛肉盖码饭", "单品", []),
        ]
        for item in items:
            item["norm"] = app_module.normalize(str(item["name"]))

        with (
            mock.patch.object(app_module, "parse_menu", return_value={"items": items}),
            mock.patch.object(app_module, "library_images", return_value=[]),
        ):
            entries = app_module.preview_sample_entries()

        names = [entry["item"]["name"] for entry in entries]
        self.assertEqual(len(entries), app_module.PREVIEW_SAMPLE_COUNT)
        self.assertIn("辣椒炒肉盖码饭", names)
        self.assertIn("小炒黄牛肉盖码饭", names)
        for banned in ("可乐", "米饭", "辣椒包", "提示", "套餐"):
            self.assertFalse(any(banned in name for name in names))
        self.assertTrue(all(app_module.preview_sample_item_allowed(entry["item"]) for entry in entries))

    def test_preview_generation_uses_text_to_image_when_source_candidate_does_not_match_dish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_source = root / "wrong.jpg"
            save_image(wrong_source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(wrong_source, "百事可乐", "style-2")])
            row["norm"] = app_module.normalize(str(row["name"]))

            def fake_text(row_arg: dict[str, object], style_id: str, quality: str | None, target: Path) -> dict[str, object]:
                save_image(target, (90, 140, 180))
                return {"provider": "tencent-hunyuan", "action": "TextToImageLite", "promptType": "text_to_image", "requestId": "txt-preview"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background") as replace,
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text) as text_to_image,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            replace.assert_not_called()
            text_to_image.assert_called_once()
            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "succeeded")
            self.assertEqual(generation["action"], "TextToImageLite")
            self.assertEqual(preview_candidate["dishName"], "辣椒炒肉盖码饭")

    def test_formal_generation_uses_text_to_image_when_source_candidate_does_not_match_dish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_source = root / "wrong.jpg"
            save_image(wrong_source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(wrong_source, "百事可乐", "style-2")])
            row["norm"] = app_module.normalize(str(row["name"]))

            def fake_text(row_arg: dict[str, object], style_id: str, quality: str | None, target: Path) -> dict[str, object]:
                save_image(target, (90, 140, 180))
                return {"provider": "tencent-hunyuan", "action": "TextToImageLite", "promptType": "text_to_image", "requestId": "txt-final"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background") as replace,
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text) as text_to_image,
            ):
                output = app_module.run_formal_generation_item(row, style="style-1", quality="standard")

            replace.assert_not_called()
            text_to_image.assert_called_once()
            self.assertEqual(output["row"]["generation"]["status"], "succeeded")
            self.assertEqual(output["row"]["generation"]["action"], "TextToImageLite")


if __name__ == "__main__":
    unittest.main()
