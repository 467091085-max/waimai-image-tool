from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import app as app_module


def save_image(path: Path, color: tuple[int, int, int] = (220, 90, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path)


def save_quality_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (800, 600), (238, 232, 218))
    draw = ImageDraw.Draw(image)
    for index in range(18):
        x0 = 20 + index * 42
        y0 = 30 + (index % 5) * 96
        color = (80 + (index * 37) % 150, 60 + (index * 23) % 150, 50 + (index * 19) % 150)
        draw.rectangle((x0, y0, x0 + 110, y0 + 80), fill=color)
    draw.ellipse((230, 140, 590, 500), fill=(76, 132, 78), outline=(245, 245, 245), width=18)
    draw.ellipse((310, 210, 510, 410), fill=(226, 176, 86))
    image.save(path)


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
        "norm": app_module.normalize(name),
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
                mock.patch.object(app_module, "ai_first_generation_enabled", return_value=False),
                mock.patch.object(app_module, "ai_asset_library_enabled", return_value=False),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=fake_replace),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text),
            ):
                generation = app_module.materialize_final_images(plan, "style-1", "standard")

            self.assertEqual(generation["attempted"], 3)
            self.assertEqual(generation["succeeded"], 3)
            self.assertEqual(generation["fallback"], 0)
            self.assertEqual(generation["skipped"], 1)
            self.assertEqual(generation["actions"], {"ReplaceBackground": 2, "Reuse": 1, "TextToImageLite": 1})
            self.assertCountEqual(calls, [("ReplaceBackground", "红烧肉"), ("TextToImageLite", "新品汤"), ("ReplaceBackground", "红烧肉+青菜套餐")])
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
                mock.patch.object(app_module, "ai_first_generation_enabled", return_value=False),
                mock.patch.object(app_module, "ai_asset_library_enabled", return_value=False),
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

    def test_ai_first_generation_uses_text_to_image_instead_of_gallery_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            rows = [menu_row(1, "牛油果鸡胸沙拉", "单品", [candidate(source, "相似沙拉", "style-2")])]
            plan = {"results": rows}
            text_calls: list[str] = []

            def fake_text(row: dict[str, object], style_id: str, quality: str | None, target: Path) -> dict[str, object]:
                text_calls.append(str(row["name"]))
                save_image(target, (40, 150, 90))
                return {"provider": "tencent-hunyuan", "action": "TextToImageLite", "promptType": "text_to_image", "requestId": "txt"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 10),
                mock.patch.object(app_module, "tencent_status_payload", return_value={"provider": "tencent-hunyuan", "configured": True}),
                mock.patch.object(app_module, "ai_asset_library_enabled", return_value=False),
                mock.patch.object(app_module, "tencent_replace_background") as replace,
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_text),
            ):
                generation = app_module.materialize_final_images(plan, "style-1", "standard")

            replace.assert_not_called()
            self.assertEqual(text_calls, ["牛油果鸡胸沙拉"])
            self.assertEqual(generation["succeeded"], 1)
            self.assertEqual(rows[0]["generation"]["action"], "TextToImageLite")

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

    def test_tencent_status_accepts_tokenhub_key_without_legacy_secret_pair(self) -> None:
        with mock.patch.dict(
            app_module.os.environ,
            {
                "TENCENT_TOKENHUB_API_KEY": "tokenhub-test-key",
                "TENCENT_TOKENHUB_IMAGE_MODEL": "hy-image-v3.0",
            },
            clear=True,
        ):
            status = app_module.tencent_status_payload()

        self.assertTrue(status["configured"])
        self.assertTrue(status["tokenhubReady"])
        self.assertFalse(status["cloudApiReady"])
        self.assertEqual(status["tokenhubModel"], "hy-image-v3.0")
        self.assertEqual(status["missing"], [])

    def test_text_to_image_prefers_tokenhub_when_key_is_configured(self) -> None:
        with (
            mock.patch.dict(
                app_module.os.environ,
                {
                    "TENCENT_TOKENHUB_API_KEY": "tokenhub-test-key",
                    "TENCENT_TOKENHUB_IMAGE_MODEL": "hy-image-v3.0",
                },
                clear=True,
            ),
            mock.patch.object(app_module, "tokenhub_image_request", return_value={
                "ResultImage": "https://cdn.example.test/tokenhub.jpg",
                "RequestId": "job-1",
                "_Endpoint": "tokenhub.tencentmaas.com",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
            }) as tokenhub,
            mock.patch.object(app_module, "tencent_cloud_api_request") as legacy,
        ):
            response = app_module.tencent_api_request("TextToImageLite", {"Prompt": "测试"})

        self.assertEqual(response["_Action"], "TokenHubImageV3")
        tokenhub.assert_called_once()
        legacy.assert_not_called()

    def test_tokenhub_failure_strips_v3_only_fields_before_legacy_fallback(self) -> None:
        cloud_payloads: list[dict[str, object]] = []

        def fake_cloud(
            _action: str,
            payload: dict[str, object],
            host: str,
            _service: str,
            _version: str,
            _timeout: int = 70,
        ) -> dict[str, object]:
            cloud_payloads.append(dict(payload))
            raise RuntimeError(f"{host} ResourceInsufficient")

        with (
            mock.patch.dict(
                app_module.os.environ,
                {
                    "TENCENT_TOKENHUB_API_KEY": "tokenhub-test-key",
                    "TENCENT_TOKENHUB_IMAGE_MODEL": "hy-image-v3.0",
                },
                clear=True,
            ),
            mock.patch.object(
                app_module,
                "tokenhub_image_request",
                side_effect=RuntimeError("ImageIllegalDetected"),
            ),
            mock.patch.object(
                app_module,
                "tencent_cloud_api_request",
                side_effect=fake_cloud,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "ImageIllegalDetected"):
                app_module.tencent_api_request(
                    "TextToImageLite",
                    {
                        "Prompt": "测试",
                        "Images": ["https://example.test/reference.jpg"],
                        "Revise": 0,
                        "Seed": 123,
                    },
                )

        self.assertEqual(len(cloud_payloads), 2)
        for payload in cloud_payloads:
            self.assertNotIn("Images", payload)
            self.assertNotIn("Revise", payload)
            self.assertNotIn("Seed", payload)

    def test_tokenhub_generation_is_serialized_within_the_service_process(self) -> None:
        state_lock = threading.Lock()
        in_flight = 0
        max_in_flight = 0

        def fake_tokenhub(
            payload: dict[str, object],
            timeout: int = 70,
        ) -> dict[str, object]:
            nonlocal in_flight, max_in_flight
            with state_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.03)
            with state_lock:
                in_flight -= 1
            return {
                "ResultImage": "https://cdn.example.test/tokenhub.jpg",
                "RequestId": str(payload.get("Prompt") or "job"),
                "_Endpoint": "tokenhub.tencentmaas.com",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
            }

        with (
            mock.patch.dict(
                app_module.os.environ,
                {
                    "TENCENT_TOKENHUB_API_KEY": "tokenhub-test-key",
                    "TENCENT_TOKENHUB_IMAGE_MODEL": "hy-image-v3.0",
                },
                clear=True,
            ),
            mock.patch.object(app_module, "tokenhub_image_request", side_effect=fake_tokenhub),
            mock.patch.object(app_module, "tencent_cloud_api_request") as legacy,
        ):
            with ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(
                    executor.map(
                        lambda index: app_module.tencent_api_request(
                            "TextToImageLite",
                            {"Prompt": f"测试-{index}"},
                        ),
                        range(4),
                    )
                )

        self.assertEqual(len(responses), 4)
        self.assertEqual(max_in_flight, 1)
        legacy.assert_not_called()

    def test_tokenhub_payload_maps_legacy_text_to_image_fields(self) -> None:
        with mock.patch.dict(
            app_module.os.environ,
            {
                "TENCENT_TOKENHUB_API_KEY": "tokenhub-test-key",
                "TENCENT_TOKENHUB_IMAGE_MODEL": "hy-image-lite",
            },
            clear=True,
        ):
            payload = app_module.tokenhub_image_payload({
                "Prompt": "辣椒炒肉外卖主图",
                "NegativePrompt": "文字 水印",
                "Resolution": "1024:768",
                "RspImgType": "url",
                "LogoAdd": 0,
                "Images": [
                    "https://cdn.example.test/background.jpg",
                    "https://cdn.example.test/reference.jpg",
                ],
            })

        self.assertEqual(payload["model"], "hy-image-lite")
        self.assertEqual(payload["prompt"], "辣椒炒肉外卖主图")
        self.assertEqual(payload["negative_prompt"], "文字 水印")
        self.assertEqual(payload["resolution"], "1024:768")
        self.assertEqual(payload["rsp_img_type"], "url")
        self.assertEqual(payload["logo_add"], 0)
        self.assertEqual(
            payload["images"],
            [
                "https://cdn.example.test/background.jpg",
                "https://cdn.example.test/reference.jpg",
            ],
        )

    def test_tokenhub_v3_payload_preserves_revise_and_seed_contract(self) -> None:
        with mock.patch.dict(
            app_module.os.environ,
            {
                "TENCENT_TOKENHUB_API_KEY": "tokenhub-test-key",
                "TENCENT_TOKENHUB_IMAGE_MODEL": "hy-image-v3.0",
            },
            clear=True,
        ):
            payload = app_module.tokenhub_image_payload({
                "Prompt": "单一连续背景",
                "NegativePrompt": "展台 纸卷",
                "Resolution": "1024:768",
                "LogoAdd": 0,
                "Revise": 0,
                "Seed": 148237123,
            })

        self.assertEqual(payload["model"], "hy-image-v3.0")
        self.assertEqual(payload["revise"], 0)
        self.assertEqual(payload["seed"], 148237123)
        self.assertNotIn("negative_prompt", payload)

    def test_text_to_image_tries_aiart_before_hunyuan_and_aggregates_resource_errors(self) -> None:
        calls: list[str] = []

        def fake_cloud(action: str, payload: dict[str, object], host: str, service: str, version: str, timeout: int = 70) -> dict[str, object]:
            calls.append(host)
            raise RuntimeError(f"{host} ResourceInsufficient: 资源不足")

        with (
            mock.patch.dict(app_module.os.environ, {"TENCENT_TOKENHUB_ENABLED": "false"}, clear=False),
            mock.patch.object(app_module, "tencent_cloud_api_request", side_effect=fake_cloud),
        ):
            with self.assertRaisesRegex(RuntimeError, "aiart.tencentcloudapi.com.*hunyuan.tencentcloudapi.com"):
                app_module.tencent_api_request("TextToImageLite", {"Prompt": "测试"})

        self.assertEqual(calls, [app_module.TENCENT_AIART_HOST, app_module.TENCENT_HUNYUAN_HOST])

    def test_preview_preserves_configured_provider_failure_without_claiming_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=RuntimeError("aiart not open")),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("hunyuan no quota")),
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "failed")
            self.assertEqual(generation["action"], "ProviderError")
            self.assertEqual(generation["errorCode"], "provider_quota")
            self.assertFalse(generation["retryable"])
            self.assertNotIn("未配置", generation["error"])

    def test_preview_marks_transient_provider_failure_retryable_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = menu_row(1, "牛油果鸡胸沙拉", "单品", [])
            error = RuntimeError(
                "TokenHub HTTP 504: gateway timeout "
                "Authorization: Bearer secret-value "
                "Credential=secret-id/2026-07-29 Signature=secret-signature"
            )

            with self.assertLogs(app_module.app.logger, level="WARNING") as captured:
                with (
                    mock.patch.object(app_module, "LIBRARY_DIR", root),
                    mock.patch.object(app_module, "tencent_ready", return_value=True),
                    mock.patch.object(app_module, "tencent_replace_background", side_effect=error),
                    mock.patch.object(app_module, "tencent_text_to_image", side_effect=error),
                ):
                    preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "failed")
            self.assertEqual(generation["action"], "ProviderError")
            self.assertEqual(generation["errorCode"], "provider_transient")
            self.assertTrue(generation["retryable"])
            self.assertNotIn("[redacted]", generation["error"])
            self.assertNotIn("secret-value", generation["error"])
            log_output = "\n".join(captured.output)
            self.assertIn("[redacted]", log_output)
            self.assertNotIn("secret-value", log_output)
            self.assertNotIn("secret-id", log_output)
            self.assertNotIn("secret-signature", log_output)

    def test_preview_provider_failure_classifies_documented_error_codes(self) -> None:
        cases = (
            ("FailedOperation.RequestTimeout", "provider_transient", True),
            ("FailedOperation.RpcFail", "provider_transient", True),
            ("FailedOperation.InnerError", "provider_transient", True),
            ("RequestLimitExceeded.JobNumExceed", "provider_transient", True),
            ("ResourceUnavailable.LowBalance", "provider_quota", False),
            ("ResourceUnavailable.InArrears", "provider_quota", False),
            ("AuthFailure.UnauthorizedOperation", "provider_auth", False),
            ("InvalidParameterValue.ParameterValueError", "provider_error", False),
        )

        for raw_error, error_code, retryable in cases:
            with self.subTest(raw_error=raw_error):
                failure = app_module.preview_provider_failure(RuntimeError(raw_error))
                self.assertEqual(failure["errorCode"], error_code)
                self.assertEqual(failure["retryable"], retryable)

    def test_preview_retry_reuses_cached_generation_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = menu_row(1, "牛油果鸡胸沙拉", "单品", [])
            target = root / "_generated_previews" / "menu-cache" / "style-1" / "0001_牛油果鸡胸沙拉.jpg"
            save_image(target)
            app_module.write_ai_output_metadata(
                target,
                {"status": "succeeded", "provider": "tencent-hunyuan", "action": "TokenHubImageV3"},
            )

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-cache"),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_text_to_image") as text_to_image,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            text_to_image.assert_not_called()
            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "cached")

    def test_preview_waits_for_configuration_only_when_provider_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = menu_row(1, "牛油果鸡胸沙拉", "单品", [])

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=False),
                mock.patch.object(app_module, "local_preview_fallback_enabled", return_value=False),
                mock.patch.object(app_module, "tencent_replace_background") as replace,
                mock.patch.object(app_module, "tencent_text_to_image") as text_to_image,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNone(preview_candidate)
            self.assertEqual(generation["status"], "pending")
            self.assertEqual(generation["action"], "WaitingForModelConfig")
            replace.assert_not_called()
            text_to_image.assert_not_called()

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

    def test_preview_sample_uses_category_fallback_without_color_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            save_image(source)
            row = menu_row(1, "辣椒炒肉盖码饭", "单品", [candidate(source, "辣椒炒肉", "style-2")])
            image = app_module.LibraryImage("img", source, "测试店", "辣椒炒肉", "辣椒炒肉", {"辣椒", "炒肉"}, "style-x", "clean", True)

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "library_images", return_value=[image]),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "local_preview_fallback_enabled", return_value=True),
                mock.patch.object(app_module, "tencent_replace_background", side_effect=RuntimeError("provider unavailable")) as replace,
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=RuntimeError("provider unavailable")),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                preview_candidate, generation = app_module.materialize_preview_candidate(row, "style-1", "standard")

            self.assertIsNotNone(preview_candidate)
            self.assertEqual(generation["status"], "fallback")
            self.assertEqual(preview_candidate["aiProvider"], "local-category")
            replace.assert_not_called()
            draw_demo.assert_not_called()

    def test_style_background_sample_uses_category_fallback_without_tencent_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "light-food.jpg"
            save_image(source)
            image = app_module.LibraryImage("img", source, "轻食店", "鸡胸沙拉", "鸡胸沙拉", {"鸡胸", "沙拉"}, "style-x", "clean", True)

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "library_images", return_value=[image]),
                mock.patch.object(app_module, "tencent_ready", return_value=False),
                mock.patch.object(app_module, "local_background_fallback_enabled", return_value=True),
                mock.patch.object(app_module, "tencent_style_background") as style_background,
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                style_candidate = app_module.style_sample_candidate("style-6")

            style_background.assert_not_called()
            self.assertEqual(style_candidate["aiProvider"], "local-category")
            self.assertEqual(style_candidate["generationAction"], "LocalCategoryBackground")
            draw_demo.assert_not_called()

    def test_style_background_sample_waits_for_provider_without_tencent_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "tencent_ready", return_value=False),
                mock.patch.object(app_module, "local_background_fallback_enabled", return_value=False),
                mock.patch.object(app_module, "tencent_style_background") as style_background,
            ):
                style_candidate = app_module.style_sample_candidate("style-3")

            style_background.assert_not_called()
            self.assertEqual(style_candidate["url"], "")
            self.assertEqual(style_candidate["aiProvider"], "tencent-hunyuan")
            self.assertEqual(style_candidate["generationStatus"], "pending")
            self.assertEqual(style_candidate["generationAction"], "WaitingForProvider")

    def test_build_plan_does_not_generate_style_backgrounds_synchronously(self) -> None:
        generate_flags: list[bool] = []

        def fake_style_sample(style_id: str, generate: bool = True) -> dict[str, object]:
            generate_flags.append(generate)
            return {
                "imageId": f"{style_id}-pending",
                "url": "",
                "styleId": style_id,
                "dishName": "背景风格样图",
                "generationProvider": "tencent-hunyuan",
                "generationStatus": "pending",
                "generationAction": "PendingGeneration",
            }

        with (
            mock.patch.object(app_module, "parse_menu", return_value={"items": [menu_row(1, "牛油果鸡胸沙拉", "单品", [])], "store": "测试店"}),
            mock.patch.object(app_module, "library_images", return_value=[]),
            mock.patch.object(app_module, "public_library_images", return_value=[]),
            mock.patch.object(app_module, "style_sample_candidate", side_effect=fake_style_sample),
        ):
            plan = app_module.build_plan("", "standard")

        self.assertEqual(len(plan["styles"]), app_module.PREVIEW_SAMPLE_COUNT)
        self.assertEqual(generate_flags, [False] * app_module.PREVIEW_SAMPLE_COUNT)

    def test_style_background_api_generates_one_requested_style(self) -> None:
        calls: list[tuple[str, bool]] = []

        def fake_style_sample(style_id: str, generate: bool = True) -> dict[str, object]:
            calls.append((style_id, generate))
            return {
                "imageId": f"{style_id}-done",
                "url": f"/media/demo_store/{style_id}/background.jpg",
                "styleId": style_id,
                "dishName": "背景风格样图",
                "generationProvider": "tencent-hunyuan",
                "generationStatus": "succeeded",
                "generationAction": "TextToImageLite",
            }

        with mock.patch.object(app_module, "style_sample_candidate", side_effect=fake_style_sample):
            response = app_module.app.test_client().get("/api/style-background?style=style-1&generate=1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(calls, [("style-1", True)])
        self.assertEqual(
            payload["sample"]["url"],
            "/media/demo_store/style-1/background.jpg",
        )
        self.assertEqual(payload["sample"]["generationAction"], "TextToImageLite")

    def test_persist_hunyuan_product_asset_writes_matchable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset_root = root / "_ai_asset_library"
            output = root / "outputs" / "salad.jpg"
            save_quality_image(output)
            row = menu_row(8, "牛油果鸡胸沙拉", "单品", [], ["牛油果", "鸡胸"])
            menu = {
                "store": "轻食测试店",
                "items": [row],
            }
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": "TextToImageLite",
                "promptType": "text_to_image",
                "category": "轻食健康餐",
            }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "AI_ASSET_DIR", asset_root),
                mock.patch.object(app_module, "parse_menu", return_value=menu),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu123"),
                mock.patch.object(app_module, "current_menu_path", return_value=None),
            ):
                record = app_module.persist_ai_generated_asset(kind="product_image", source_path=output, style_id="style-1", metadata=metadata, row=row, quality="standard")

            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["kind"], "product_image")
            self.assertEqual(record["category"], "轻食健康餐")
            self.assertEqual(record["productName"], "牛油果鸡胸沙拉")
            self.assertEqual(record["status"], "approved")
            self.assertEqual(record["qualityStatus"], "passed")
            self.assertIn("牛油果鸡胸沙拉", record["matchNames"])
            self.assertIn("牛油果", record["keywords"])
            self.assertTrue((asset_root / "manifest.jsonl").exists())
            self.assertTrue(Path(record["localPath"]).exists())

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "AI_ASSET_DIR", asset_root),
                mock.patch.object(app_module, "ensure_demo_data"),
                mock.patch.object(app_module, "configured_library_dirs", return_value=[]),
            ):
                app_module.library_images.cache_clear()
                self.addCleanup(app_module.library_images.cache_clear)
                images = app_module.library_images()

            self.assertTrue(any(image.source == "hunyuan-product" and image.dish == "牛油果鸡胸沙拉" for image in images))

    def test_persist_hunyuan_asset_rejects_low_quality_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset_root = root / "_ai_asset_library"
            output = root / "outputs" / "solid.jpg"
            save_image(output, (245, 245, 245))
            row = menu_row(9, "牛油果鸡胸沙拉", "单品", [], ["牛油果", "鸡胸"])
            menu = {"store": "轻食测试店", "items": [row]}
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": "TextToImageLite",
                "promptType": "text_to_image",
                "category": "轻食健康餐",
            }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "AI_ASSET_DIR", asset_root),
                mock.patch.object(app_module, "parse_menu", return_value=menu),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu123"),
                mock.patch.object(app_module, "current_menu_path", return_value=None),
            ):
                record = app_module.persist_ai_generated_asset(kind="product_image", source_path=output, style_id="style-1", metadata=metadata, row=row, quality="standard")

            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["qualityStatus"], "failed")
            self.assertIn("too_small", record["qualityReasons"])

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "AI_ASSET_DIR", asset_root),
                mock.patch.object(app_module, "ensure_demo_data"),
                mock.patch.object(app_module, "configured_library_dirs", return_value=[]),
            ):
                app_module.library_images.cache_clear()
                self.addCleanup(app_module.library_images.cache_clear)
                images = app_module.library_images()

            self.assertFalse(any(image.source == "hunyuan-product" for image in images))

    def test_style_background_generation_uses_text_to_image_in_ai_first_mode(self) -> None:
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
                mock.patch.object(app_module, "style_background_seed_candidate", return_value=candidate(source, "辣椒炒肉", "style-1")) as seed_candidate,
                mock.patch.object(app_module, "candidate_public_url", return_value="https://cdn.example.test/seed.jpg") as public_url,
                mock.patch.object(app_module, "tencent_api_request", side_effect=fake_api),
                mock.patch.object(app_module, "save_result_image"),
            ):
                detail = app_module.tencent_style_background("style-2", target)

            self.assertEqual(detail["action"], "TextToImageLite")
            self.assertEqual(payloads[0][0], "TextToImageLite")
            self.assertNotIn("ProductUrl", payloads[0][1])
            self.assertIn("真实空景摄影", str(payloads[0][1]["Prompt"]))
            self.assertIn("禁止菜品", str(payloads[0][1]["Prompt"]))
            seed_candidate.assert_not_called()
            public_url.assert_not_called()

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

    def test_preview_samples_prioritize_category_dishes_and_combos_over_announcements(self) -> None:
        def item(
            row: int,
            name: str,
            kind: str,
            taxonomy: str,
            components: list[str] | None = None,
        ) -> dict[str, object]:
            return {
                "row": row,
                "category": "测试",
                "name": name,
                "norm": app_module.normalize(name),
                "kind": kind,
                "taxonomy": taxonomy,
                "components": components or [],
            }

        menu = {
            "items": [
                item(1, "祝顾客马年行大运,万事皆顺意", "单品", "unknown"),
                item(2, "0.01元热狗肠1根", "单品", "burger_hotdog"),
                item(3, "蜜汁烤肉拌饭+黑椒烤排套餐", "套餐/组合", "combo", ["蜜汁烤肉拌饭", "黑椒烤排"]),
                item(4, "黑椒烤排拌饭+时蔬套餐", "套餐/组合", "combo", ["黑椒烤排拌饭", "时蔬"]),
                item(5, "招牌蜜汁烤肉拌饭", "单品", "mixed_rice"),
                item(6, "双椒鸡排拌饭", "单品", "mixed_rice"),
                item(9, "招牌蜜汁烤肉拌饭", "单品", "mixed_rice"),
                item(7, "烤肉拌饭双拼套餐", "套餐/组合", "combo", ["鸡排"]),
                item(8, "黑椒烤排三拼套餐", "套餐/组合", "combo", ["烤排"]),
            ]
        }

        with mock.patch.object(
            app_module,
            "category_report",
            return_value={"taxonomyId": "mixed_rice"},
        ):
            selected = app_module.select_preview_sample_items(menu)

        self.assertEqual(
            [row["name"] for row in selected],
            [
                "蜜汁烤肉拌饭+黑椒烤排套餐",
                "黑椒烤排拌饭+时蔬套餐",
                "招牌蜜汁烤肉拌饭",
                "双椒鸡排拌饭",
                "烤肉拌饭双拼套餐",
                "黑椒烤排三拼套餐",
            ],
        )
        self.assertNotIn("祝顾客马年行大运,万事皆顺意", [row["name"] for row in selected])
        self.assertNotIn("0.01元热狗肠1根", [row["name"] for row in selected])

    def test_style_preview_batch_preserves_successes_when_one_sample_raises(self) -> None:
        entries = [
            {"item": menu_row(index + 1, f"菜品{index + 1}", "单品", []), "candidates": []}
            for index in range(app_module.PREVIEW_SAMPLE_COUNT)
        ]

        def generate_sample(style: str, entry: dict[str, object], generate: bool = True) -> dict[str, object]:
            row = int(entry["item"]["row"])
            if row == 3:
                raise RuntimeError("TokenHub HTTP 504: gateway timeout")
            return {
                **entry["item"],
                "candidate": {"url": f"/media/sample-{row}.jpg"},
                "generation": {"status": "succeeded", "provider": "tencent-hunyuan", "action": "TextToImage"},
                "points": 0,
                "publicStatus": "免费样图",
            }

        with (
            mock.patch.object(app_module, "preview_sample_entries", return_value=entries),
            mock.patch.object(app_module, "preview_sample_payload_from_entry", side_effect=generate_sample),
            mock.patch.object(app_module, "FINAL_GENERATION_WORKERS", 3),
        ):
            preview = app_module.preview_samples("style-1", generate=True)

        self.assertEqual(len(preview["samples"]), app_module.PREVIEW_SAMPLE_COUNT)
        self.assertEqual([sample["row"] for sample in preview["samples"]], list(range(1, 7)))
        self.assertEqual(preview["samples"][2]["generation"]["status"], "failed")
        self.assertEqual(preview["samples"][2]["generation"]["action"], "ProviderError")
        self.assertTrue(preview["samples"][2]["generation"]["retryable"])
        self.assertEqual(
            [sample["candidate"]["url"] for sample in preview["samples"] if sample["candidate"]],
            ["/media/sample-1.jpg", "/media/sample-2.jpg", "/media/sample-4.jpg", "/media/sample-5.jpg", "/media/sample-6.jpg"],
        )

    def test_style_preview_sample_api_validates_index_and_generates_only_requested_sample(self) -> None:
        sample = {
            **menu_row(3, "菜品3", "单品", []),
            "candidate": {
                "url": "/media/demo_store/style-1/sample-3.jpg"
            },
            "generation": {"status": "succeeded", "provider": "tencent-hunyuan", "action": "TextToImage"},
            "points": 0,
            "publicStatus": "免费样图",
        }
        client = app_module.app.test_client()

        with mock.patch.object(app_module, "preview_sample_payload") as generate:
            invalid_urls = (
                "/api/style-preview-sample?style=style-1",
                "/api/style-preview-sample?style=style-1&index=",
                "/api/style-preview-sample?style=style-1&index=abc",
                "/api/style-preview-sample?style=style-1&index=1.5",
            )
            for url in invalid_urls:
                with self.subTest(url=url):
                    self.assertEqual(client.get(url).status_code, 400)
            generate.assert_not_called()

        entries = [
            {"item": menu_row(index + 1, f"菜品{index + 1}", "单品", []), "candidates": []}
            for index in range(app_module.PREVIEW_SAMPLE_COUNT)
        ]
        with mock.patch.object(app_module, "preview_sample_entries", return_value=entries):
            self.assertEqual(client.get("/api/style-preview-sample?style=style-1&index=-1").status_code, 404)
            self.assertEqual(client.get("/api/style-preview-sample?style=style-1&index=6").status_code, 404)

        with mock.patch.object(app_module, "preview_sample_payload", return_value=sample) as generate:
            response = client.get("/api/style-preview-sample?style=style-1&index=2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["index"], 2)
        self.assertEqual(
            response.get_json()["sample"]["candidate"]["url"],
            "/media/demo_store/style-1/sample-3.jpg",
        )
        generate.assert_called_once_with("style-1", 2, generate=True)


if __name__ == "__main__":
    unittest.main()
