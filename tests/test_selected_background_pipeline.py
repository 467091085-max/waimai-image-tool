from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw
import pytest

import app as app_module


def save_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def menu_row(row: int = 1, name: str = "招牌牛肉饭") -> dict[str, object]:
    return {
        "row": row,
        "category": "主食",
        "name": name,
        "norm": app_module.normalize(name),
        "kind": "单品",
        "components": [],
        "candidates": [],
        "backgroundAction": "",
        "publicStatus": "",
    }


def selected_background(path: Path) -> app_module.SelectedBackgroundAsset:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(path) as image:
        width, height = image.size
    return app_module.SelectedBackgroundAsset(
        asset_id="bg_test_asset",
        menu_key="menu-test",
        style_id="style-2",
        sha256=digest,
        path=path,
        width=width,
        height=height,
    )


def test_algorithm_intermediate_png_preserves_canonical_rgb_pixels() -> None:
    source = Image.new("RGB", (128, 128), (0, 245, 245))
    ImageDraw.Draw(source).ellipse((24, 18, 104, 116), fill=(190, 55, 35))
    raw = io.BytesIO()
    source.save(raw, "PNG")

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "foreground.png"
        app_module.save_result_image(base64.b64encode(raw.getvalue()).decode(), target)
        with Image.open(target) as persisted:
            assert persisted.format == "PNG"
            assert persisted.convert("RGB").tobytes() == source.tobytes()


def test_provider_image_write_failure_keeps_previous_complete_file() -> None:
    source = Image.new("RGB", (128, 128), (0, 245, 245))
    raw = io.BytesIO()
    source.save(raw, "PNG")

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "foreground.png"
        target.write_bytes(b"previous-complete-file")
        with mock.patch.object(
            Image.Image,
            "save",
            side_effect=OSError("interrupted"),
        ):
            with pytest.raises(OSError, match="interrupted"):
                app_module.save_result_image(
                    base64.b64encode(raw.getvalue()).decode(),
                    target,
                )

        assert target.read_bytes() == b"previous-complete-file"
        assert not list(target.parent.glob(f".{target.name}.*.tmp"))


class SelectedBackgroundPipelineTests(unittest.TestCase):
    def test_exact_product_identity_keeps_similar_combo_names_separate(self) -> None:
        first = menu_row(1, "【霸气任选】 三拼饭+赠品五选一")
        second = menu_row(2, "【超值自选】 双拼饭+赠品五选一")
        first["kind"] = second["kind"] = "套餐/组合"
        first["components"] = second["components"] = ["饭", "赠品五选一"]

        self.assertNotEqual(
            app_module.exact_product_identity(first),
            app_module.exact_product_identity(second),
        )

        duplicate = dict(first)
        duplicate["row"] = 99
        duplicate["name"] = "【霸气任选】   三拼饭+赠品五选一"
        self.assertEqual(
            app_module.exact_product_identity(first),
            app_module.exact_product_identity(duplicate),
        )

    def test_exact_duplicate_rows_share_one_foreground_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (800, 600), (35, 90, 145))
            background = selected_background(background_path)
            rows = [menu_row(1), menu_row(99)]
            calls = {"foreground": 0, "mask": 0}

            def fake_foreground(
                item: dict[str, object],
                style_id: str,
                quality: str | None,
                target: Path,
                selected: app_module.SelectedBackgroundAsset,
            ) -> dict[str, object]:
                calls["foreground"] += 1
                time.sleep(0.05)
                save_image(target, (640, 480), (190, 55, 35))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "TokenHubImageV3",
                    "promptType": "text_to_image",
                    "requestId": "foreground-shared",
                    "referenceConditioned": True,
                    "backgroundIdentityVerified": False,
                }

            def fake_mask(
                item: dict[str, object],
                foreground: Path,
                target: Path,
            ) -> dict[str, object]:
                calls["mask"] += 1
                mask = Image.new("L", (640, 480), 0)
                ImageDraw.Draw(mask).ellipse((120, 70, 520, 440), fill=255)
                target.parent.mkdir(parents=True, exist_ok=True)
                mask.save(target)
                return {
                    "provider": "tencent-hunyuan",
                    "action": "ReplaceBackgroundMask",
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(
                    app_module,
                    "current_menu_cache_key",
                    return_value="menu-test",
                ),
                mock.patch.object(
                    app_module,
                    "chroma_foreground_fast_path_enabled",
                    return_value=False,
                ),
                mock.patch.object(
                    app_module,
                    "tencent_text_to_image",
                    side_effect=fake_foreground,
                ),
                mock.patch.object(
                    app_module,
                    "tencent_extract_foreground_mask",
                    side_effect=fake_mask,
                ),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            app_module.tencent_exact_background_image,
                            row,
                            background,
                            "standard",
                            root / f"output-{index}.png",
                        )
                        for index, row in enumerate(rows)
                    ]
                    results = [future.result() for future in futures]

            self.assertEqual(calls, {"foreground": 1, "mask": 1})
            self.assertEqual(
                sorted(
                    result["composition"]["foregroundCached"]
                    for result in results
                ),
                [False, True],
            )

    def test_exact_duplicate_failure_is_shared_without_second_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (800, 600), (35, 90, 145))
            background = selected_background(background_path)
            rows = [menu_row(1), menu_row(99)]

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(
                    app_module,
                    "current_menu_cache_key",
                    return_value="menu-test",
                ),
                mock.patch.object(app_module, "FINAL_GENERATION_WORKERS", 2),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 5),
                mock.patch.object(
                    app_module,
                    "tencent_status_payload",
                    return_value={
                        "provider": "tencent-hunyuan",
                        "configured": True,
                    },
                ),
                mock.patch.object(
                    app_module,
                    "chroma_foreground_fast_path_enabled",
                    return_value=False,
                ),
                mock.patch.object(
                    app_module,
                    "tencent_text_to_image",
                    side_effect=RuntimeError("provider failed"),
                ) as foreground,
                mock.patch.object(
                    app_module,
                    "ai_asset_library_enabled",
                    return_value=False,
                ),
            ):
                generation = app_module.materialize_final_images(
                    {"results": rows},
                    "style-2",
                    "standard",
                    background,
                )

            foreground.assert_called_once()
            self.assertEqual(generation["succeeded"], 0)
            self.assertEqual(generation["failed"], 2)
            self.assertEqual(generation["pending"], 2)

    def test_failed_quality_gate_deletes_placeholder_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "placeholder.jpg"
            save_image(target, (800, 600), (240, 240, 240))

            with self.assertRaises(app_module.SelectedBackgroundError) as raised:
                app_module.require_generated_output_quality(target)

            self.assertEqual(raised.exception.code, "generated_image_quality_failed")
            self.assertFalse(target.exists())

    def test_style_background_placeholder_is_not_exposed_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def fake_style(style_id: str, target: Path) -> dict[str, object]:
                save_image(target, (800, 600), (230, 230, 230))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "TokenHubImageV3",
                    "promptType": "style_background",
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_style_background", side_effect=fake_style),
                mock.patch.object(app_module, "local_background_fallback_enabled", return_value=False),
            ):
                candidate = app_module.style_sample_candidate("style-2")
                target = app_module.style_background_target("style-2")

            self.assertEqual(candidate["url"], "")
            self.assertEqual(candidate["generationStatus"], "failed")
            self.assertEqual(candidate["generationAction"], "ProviderError")
            self.assertFalse(target.exists())

    def test_background_asset_identity_changes_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "background.jpg"
            save_image(target, (120, 90), (20, 70, 130))
            with (
                mock.patch.object(app_module, "LIBRARY_DIR", Path(tmp) / "library"),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
            ):
                first = app_module.build_selected_background_asset("style-2", target)
                save_image(target, (120, 90), (180, 40, 30))
                second = app_module.build_selected_background_asset("style-2", target)

            self.assertNotEqual(first.sha256, second.sha256)
            self.assertNotEqual(first.asset_id, second.asset_id)
            self.assertNotEqual(first.path, target)
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(hashlib.sha256(first.path.read_bytes()).hexdigest(), first.sha256)

    def test_exact_pipeline_caches_foreground_and_mask_and_verifies_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (800, 600), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()
            calls = {"foreground": 0, "mask": 0}

            def fake_foreground(
                item: dict[str, object],
                style_id: str,
                quality: str | None,
                target: Path,
                selected: app_module.SelectedBackgroundAsset,
            ) -> dict[str, object]:
                calls["foreground"] += 1
                save_image(target, (640, 480), (190, 55, 35))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "TokenHubImageV3",
                    "promptType": "text_to_image",
                    "requestId": "foreground-1",
                    "referenceConditioned": True,
                    "backgroundIdentityVerified": False,
                }

            def fake_mask(item: dict[str, object], foreground: Path, target: Path) -> dict[str, object]:
                calls["mask"] += 1
                mask = Image.new("L", (640, 480), 0)
                ImageDraw.Draw(mask).ellipse((120, 70, 520, 440), fill=255)
                target.parent.mkdir(parents=True, exist_ok=True)
                mask.save(target)
                return {"provider": "tencent-hunyuan", "action": "ReplaceBackgroundMask"}

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "tencent_text_to_image", side_effect=fake_foreground),
                mock.patch.object(app_module, "tencent_extract_foreground_mask", side_effect=fake_mask),
            ):
                first_target = root / "first.png"
                first = app_module.tencent_exact_background_image(row, background, "standard", first_target)
                second_target = root / "second.png"
                second = app_module.tencent_exact_background_image(row, background, "standard", second_target)
                _, mask_target = app_module.foreground_cache_targets(row, background, "standard")
                Image.new("L", (640, 480), 255).save(mask_target)
                third_target = root / "third.png"
                third = app_module.tencent_exact_background_image(row, background, "standard", third_target)
                with mock.patch.object(
                    app_module,
                    "EXACT_BACKGROUND_MASK_CACHE_VERSION",
                    2,
                ):
                    fourth_target = root / "fourth.png"
                    fourth = app_module.tencent_exact_background_image(
                        row,
                        background,
                        "standard",
                        fourth_target,
                    )
                with mock.patch.object(
                    app_module,
                    "DISH_GENERATION_PROMPT_VERSION",
                    999,
                ):
                    fifth_target = root / "fifth.png"
                    fifth = app_module.tencent_exact_background_image(
                        row,
                        background,
                        "standard",
                        fifth_target,
                    )

            self.assertEqual(calls, {"foreground": 2, "mask": 4})
            self.assertTrue(first["backgroundIdentityVerified"])
            self.assertTrue(first["persistedOutputBackgroundVerified"])
            self.assertEqual(first_target.suffix, ".png")
            self.assertTrue(first["composition"]["outsideMaskPixelsPreserved"])
            self.assertEqual(first["composition"]["backgroundAssetId"], background.asset_id)
            self.assertFalse(first["composition"]["foregroundCached"])
            self.assertTrue(second["composition"]["foregroundCached"])
            self.assertTrue(second["composition"]["maskCached"])
            self.assertTrue(third["composition"]["foregroundCached"])
            self.assertFalse(third["composition"]["maskCached"])
            self.assertTrue(fourth["composition"]["foregroundCached"])
            self.assertFalse(fourth["composition"]["maskCached"])
            self.assertFalse(fifth["composition"]["foregroundCached"])
            self.assertFalse(fifth["composition"]["maskCached"])
            with Image.open(second_target) as result:
                self.assertEqual(result.size, (800, 600))

    def test_preview_with_selected_background_uses_exact_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (120, 90), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()

            def fake_exact(
                item: dict[str, object],
                selected: app_module.SelectedBackgroundAsset,
                quality: str | None,
                target: Path,
                **_kwargs: object,
            ) -> dict[str, object]:
                save_image(target, (120, 90), (80, 120, 60))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "DeterministicBackgroundComposite",
                    "promptType": "text_to_image",
                    "backgroundIdentityVerified": True,
                    "persistedOutputBackgroundVerified": True,
                    "pipelineVersion": app_module.EXACT_BACKGROUND_PIPELINE_VERSION,
                    "outputSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(app_module, "tencent_exact_background_image", side_effect=fake_exact) as exact,
                mock.patch.object(app_module, "render_local_composed_image") as local_fallback,
            ):
                candidate, generation = app_module.materialize_preview_candidate(
                    row,
                    "style-2",
                    "standard",
                    background,
                )

            exact.assert_called_once()
            local_fallback.assert_not_called()
            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertEqual(generation["action"], "DeterministicBackgroundComposite")
            self.assertEqual(candidate["backgroundAssetId"], background.asset_id)
            self.assertEqual(candidate["backgroundSha256"], background.sha256)
            self.assertTrue(candidate["backgroundIdentityVerified"])
            self.assertEqual(Path(str(candidate["path"])).suffix, ".png")

    def test_exact_pipeline_uses_local_chroma_mask_without_cloud_mask_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (800, 600), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()

            def fake_chroma(
                item: dict[str, object],
                quality: str | None,
                target: Path,
            ) -> dict[str, object]:
                image = Image.new("RGB", (640, 480), (0, 245, 245))
                ImageDraw.Draw(image).ellipse((120, 70, 520, 440), fill=(190, 55, 35))
                target.parent.mkdir(parents=True, exist_ok=True)
                image.save(target)
                return {
                    "provider": "tencent-hunyuan",
                    "action": "TokenHubImageV3",
                    "promptType": "chroma_foreground",
                    "requestId": "foreground-fast-1",
                    "referenceConditioned": False,
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "chroma_foreground_fast_path_enabled", return_value=True),
                mock.patch.object(app_module, "tencent_chroma_foreground", side_effect=fake_chroma) as chroma,
                mock.patch.object(app_module, "tencent_text_to_image") as reference_generation,
                mock.patch.object(app_module, "tencent_extract_foreground_mask") as cloud_mask,
            ):
                target = root / "fast.png"
                detail = app_module.tencent_exact_background_image(
                    row,
                    background,
                    "standard",
                    target,
                )

            chroma.assert_called_once()
            reference_generation.assert_not_called()
            cloud_mask.assert_not_called()
            self.assertEqual(detail["maskExtraction"]["action"], "LocalChromaKeyMask")
            self.assertTrue(detail["backgroundIdentityVerified"])
            self.assertTrue(detail["persistedOutputBackgroundVerified"])
            self.assertFalse(detail["referenceConditioned"])

    def test_exact_pipeline_falls_back_to_cloud_mask_when_chroma_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (800, 600), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()

            def fake_chroma(
                item: dict[str, object],
                quality: str | None,
                target: Path,
            ) -> dict[str, object]:
                image = Image.new("RGB", (640, 480), (0, 245, 245))
                draw = ImageDraw.Draw(image)
                draw.ellipse((110, 290, 530, 450), fill=(55, 155, 158))
                draw.ellipse((140, 70, 500, 410), fill=(190, 55, 35))
                target.parent.mkdir(parents=True, exist_ok=True)
                image.save(target)
                return {
                    "provider": "tencent-hunyuan",
                    "action": "TokenHubImageV3",
                    "promptType": "chroma_foreground",
                    "requestId": "foreground-fast-2",
                    "referenceConditioned": False,
                }

            def fake_cloud_mask(
                item: dict[str, object],
                foreground: Path,
                target: Path,
            ) -> dict[str, object]:
                mask = Image.new("L", (640, 480), 0)
                ImageDraw.Draw(mask).ellipse((120, 70, 520, 440), fill=255)
                target.parent.mkdir(parents=True, exist_ok=True)
                mask.save(target)
                return {
                    "provider": "tencent-hunyuan",
                    "action": "ReplaceBackgroundMask",
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "chroma_foreground_fast_path_enabled", return_value=True),
                mock.patch.object(app_module, "tencent_chroma_foreground", side_effect=fake_chroma),
                mock.patch.object(app_module, "tencent_extract_foreground_mask", side_effect=fake_cloud_mask) as cloud_mask,
            ):
                target = root / "fallback.png"
                detail = app_module.tencent_exact_background_image(
                    row,
                    background,
                    "standard",
                    target,
                )

            cloud_mask.assert_called_once()
            self.assertEqual(
                detail["maskExtraction"]["fallbackReasonCode"],
                "chroma_spill_too_large",
            )
            self.assertEqual(
                detail["maskExtraction"]["fallbackFrom"],
                "local-chroma-key",
            )
            self.assertTrue(detail["persistedOutputBackgroundVerified"])

    def test_preview_mask_failure_never_falls_back_to_unverified_local_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (120, 90), (35, 90, 145))
            background = selected_background(background_path)

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(
                    app_module,
                    "tencent_exact_background_image",
                    side_effect=app_module.SelectedBackgroundError(
                        "foreground_mask_missing",
                        "Mask service returned no mask",
                    ),
                ),
                mock.patch.object(app_module, "local_preview_fallback_enabled", return_value=True),
                mock.patch.object(app_module, "render_local_composed_image") as local_fallback,
            ):
                candidate, generation = app_module.materialize_preview_candidate(
                    menu_row(),
                    "style-2",
                    "standard",
                    background,
                )

            self.assertIsNone(candidate)
            self.assertEqual(generation["status"], "failed")
            local_fallback.assert_not_called()

    def test_formal_generation_with_selected_background_uses_exact_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (120, 90), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()

            def fake_exact(
                item: dict[str, object],
                selected: app_module.SelectedBackgroundAsset,
                quality: str | None,
                target: Path,
                **_kwargs: object,
            ) -> dict[str, object]:
                save_image(target, (120, 90), (80, 120, 60))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "DeterministicBackgroundComposite",
                    "promptType": "text_to_image",
                    "backgroundIdentityVerified": True,
                    "persistedOutputBackgroundVerified": True,
                    "pipelineVersion": app_module.EXACT_BACKGROUND_PIPELINE_VERSION,
                    "outputSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 5),
                mock.patch.object(
                    app_module,
                    "tencent_status_payload",
                    return_value={"provider": "tencent-hunyuan", "configured": True},
                ),
                mock.patch.object(app_module, "tencent_exact_background_image", side_effect=fake_exact) as exact,
                mock.patch.object(app_module, "ai_asset_library_enabled", return_value=False),
                mock.patch.object(app_module, "render_local_composed_image") as local_fallback,
            ):
                generation = app_module.materialize_final_images(
                    {"results": [row]},
                    "style-2",
                    "standard",
                    background,
                )

            exact.assert_called_once()
            local_fallback.assert_not_called()
            self.assertEqual(generation["succeeded"], 1)
            self.assertEqual(row["generation"]["action"], "DeterministicBackgroundComposite")
            self.assertEqual(row["candidates"][0]["backgroundAssetId"], background.asset_id)
            self.assertTrue(row["candidates"][0]["backgroundIdentityVerified"])
            self.assertEqual(Path(str(row["candidates"][0]["path"])).suffix, ".png")

    def test_formal_generation_reports_progress_after_each_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (120, 90), (35, 90, 145))
            background = selected_background(background_path)
            rows = [menu_row(1, "招牌牛肉饭"), menu_row(2, "香辣鸡肉饭")]
            progress: list[tuple[int, int, int]] = []

            def fake_exact(
                item: dict[str, object],
                selected: app_module.SelectedBackgroundAsset,
                quality: str | None,
                target: Path,
                **_kwargs: object,
            ) -> dict[str, object]:
                save_image(target, (120, 90), (80, 120, 60))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "DeterministicBackgroundComposite",
                    "promptType": "text_to_image",
                    "backgroundIdentityVerified": True,
                    "persistedOutputBackgroundVerified": True,
                    "pipelineVersion": app_module.EXACT_BACKGROUND_PIPELINE_VERSION,
                    "outputSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(
                    app_module,
                    "current_menu_cache_key",
                    return_value="menu-test",
                ),
                mock.patch.object(app_module, "FINAL_GENERATION_WORKERS", 1),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 5),
                mock.patch.object(
                    app_module,
                    "tencent_status_payload",
                    return_value={
                        "provider": "tencent-hunyuan",
                        "configured": True,
                    },
                ),
                mock.patch.object(
                    app_module,
                    "tencent_exact_background_image",
                    side_effect=fake_exact,
                ),
                mock.patch.object(
                    app_module,
                    "ai_asset_library_enabled",
                    return_value=False,
                ),
            ):
                app_module.materialize_final_images(
                    {"results": rows},
                    "style-2",
                    "standard",
                    background,
                    progress_callback=lambda completed, failed, pending: (
                        progress.append((completed, failed, pending))
                    ),
                )

            self.assertEqual(progress, [(0, 0, 2), (1, 0, 1), (2, 0, 0)])

    def test_formal_standard_reuses_verified_free_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (120, 90), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()

            def fake_exact(
                item: dict[str, object],
                selected: app_module.SelectedBackgroundAsset,
                quality: str | None,
                target: Path,
                **_kwargs: object,
            ) -> dict[str, object]:
                save_image(target, (120, 90), (80, 120, 60))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "DeterministicBackgroundComposite",
                    "promptType": "text_to_image",
                    "backgroundIdentityVerified": True,
                    "persistedOutputBackgroundVerified": True,
                    "pipelineVersion": app_module.EXACT_BACKGROUND_PIPELINE_VERSION,
                    "outputSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 5),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(
                    app_module,
                    "tencent_status_payload",
                    return_value={"provider": "tencent-hunyuan", "configured": True},
                ),
                mock.patch.object(app_module, "tencent_exact_background_image", side_effect=fake_exact) as exact,
                mock.patch.object(app_module, "ai_asset_library_enabled", return_value=False),
            ):
                preview, generation = app_module.materialize_preview_candidate(
                    row,
                    "style-2",
                    "standard",
                    background,
                )
                assert preview is not None
                preview_bytes = Path(str(preview["path"])).read_bytes()
                self.assertEqual(generation["status"], "succeeded")
                exact.reset_mock()

                final_generation = app_module.materialize_final_images(
                    {"results": [row]},
                    "style-2",
                    "standard",
                    background,
                )
                exact.assert_not_called()

                exact.reset_mock()
                premium_row = menu_row()
                premium_generation = app_module.materialize_final_images(
                    {"results": [premium_row]},
                    "style-2",
                    "premium",
                    background,
                )

            exact.assert_called_once()
            self.assertEqual(final_generation["succeeded"], 1)
            self.assertEqual(final_generation["cached"], 1)
            self.assertEqual(final_generation["actions"], {"PreviewReuse": 1})
            self.assertEqual(premium_generation["succeeded"], 1)
            self.assertEqual(premium_generation["cached"], 0)
            self.assertEqual(row["generation"]["action"], "PreviewReuse")
            final_path = Path(str(row["candidates"][0]["path"]))
            self.assertEqual(final_path.read_bytes(), preview_bytes)
            final_metadata = app_module.load_ai_output_metadata(final_path)
            self.assertTrue(
                app_module.verified_exact_output_metadata(
                    final_metadata,
                    final_path,
                    background,
                )
            )

    def test_formal_generation_falls_back_when_preview_cache_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            save_image(background_path, (120, 90), (35, 90, 145))
            background = selected_background(background_path)
            row = menu_row()

            def fake_exact(
                item: dict[str, object],
                selected: app_module.SelectedBackgroundAsset,
                quality: str | None,
                target: Path,
                **_kwargs: object,
            ) -> dict[str, object]:
                save_image(target, (120, 90), (80, 120, 60))
                return {
                    "provider": "tencent-hunyuan",
                    "action": "DeterministicBackgroundComposite",
                    "promptType": "text_to_image",
                    "backgroundIdentityVerified": True,
                    "persistedOutputBackgroundVerified": True,
                    "pipelineVersion": app_module.EXACT_BACKGROUND_PIPELINE_VERSION,
                    "outputSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(
                    app_module,
                    "generated_preview_candidate",
                    side_effect=app_module.PreviewObjectStorageError(
                        "preview_object_storage_unavailable",
                        "temporary failure",
                    ),
                ),
                mock.patch.object(app_module, "TENCENT_SYNC_LIMIT", 5),
                mock.patch.object(app_module, "tencent_ready", return_value=True),
                mock.patch.object(
                    app_module,
                    "tencent_status_payload",
                    return_value={"provider": "tencent-hunyuan", "configured": True},
                ),
                mock.patch.object(
                    app_module,
                    "tencent_exact_background_image",
                    side_effect=fake_exact,
                ) as exact,
                mock.patch.object(app_module, "ai_asset_library_enabled", return_value=False),
            ):
                generation = app_module.materialize_final_images(
                    {"results": [row]},
                    "style-2",
                    "standard",
                    background,
                )

            exact.assert_called_once()
            self.assertEqual(generation["succeeded"], 1)
            self.assertEqual(generation["cached"], 0)
            self.assertEqual(
                row["generation"]["action"],
                "DeterministicBackgroundComposite",
            )

    def test_tampered_exact_output_fails_metadata_and_candidate_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background_path = root / "background.jpg"
            output_path = root / "output.png"
            save_image(background_path, (800, 600), (35, 90, 145))
            save_image(output_path, (800, 600), (80, 120, 60))
            background = selected_background(background_path)
            output_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "backgroundIdentityVerified": True,
                "persistedOutputBackgroundVerified": True,
                "pipelineVersion": app_module.EXACT_BACKGROUND_PIPELINE_VERSION,
                "dishPromptVersion": app_module.DISH_GENERATION_PROMPT_VERSION,
                "outputSha256": output_sha,
                **app_module.selected_background_metadata(background),
            }
            candidate = {
                **metadata,
                "path": str(output_path),
            }

            self.assertTrue(
                app_module.verified_exact_output_metadata(metadata, output_path, background)
            )
            self.assertTrue(app_module.verified_exact_candidate(candidate, background))

            with mock.patch.object(
                app_module,
                "DISH_GENERATION_PROMPT_VERSION",
                999,
            ):
                self.assertFalse(
                    app_module.verified_exact_output_metadata(
                        metadata,
                        output_path,
                        background,
                    )
                )
                self.assertFalse(
                    app_module.verified_exact_candidate(
                        candidate,
                        background,
                    )
                )

            save_image(output_path, (800, 600), (170, 40, 35))

            self.assertFalse(
                app_module.verified_exact_output_metadata(metadata, output_path, background)
            )
            self.assertFalse(app_module.verified_exact_candidate(candidate, background))

    def test_live_preview_requires_complete_selected_background_identity(self) -> None:
        client = app_module.app.test_client()
        with (
            mock.patch.dict(
                app_module.os.environ,
                {"REQUIRE_SELECTED_BACKGROUND_IDENTITY": "true"},
                clear=False,
            ),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-2"}),
            mock.patch.object(app_module, "preview_samples") as preview,
        ):
            missing = client.get("/api/style-preview?style=style-2")
            incomplete = client.get(
                "/api/style-preview?style=style-2&backgroundAssetId=bg_test_asset"
            )

        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.get_json()["code"], "selected_background_identity_required")
        self.assertEqual(incomplete.status_code, 409)
        self.assertEqual(incomplete.get_json()["code"], "selected_background_identity_incomplete")
        preview.assert_not_called()

    def test_changed_background_identity_is_rejected_before_generation_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "_style_backgrounds" / "menu-test" / "style-2" / "背景风格样图.jpg"
            save_image(source, (120, 90), (35, 90, 145))
            client = app_module.app.test_client()

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "current_menu_path", return_value=None),
                mock.patch.object(app_module, "public_style_ids", return_value={"style-2"}),
                mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
            ):
                original = app_module.build_selected_background_asset("style-2", source)
                save_image(source, (120, 90), (190, 45, 35))
                with mock.patch.object(app_module.generation_queue, "enqueue") as enqueue:
                    response = client.post(
                        "/api/generation-jobs",
                        json={
                            "style": "style-2",
                            "quality": "standard",
                            "backgroundAssetId": original.asset_id,
                            "backgroundSha256": original.sha256,
                        },
                    )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["code"], "selected_background_changed")
            enqueue.assert_not_called()

    def test_valid_preview_identity_is_resolved_and_passed_to_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "_style_backgrounds" / "menu-test" / "style-2" / "背景风格样图.jpg"
            save_image(source, (120, 90), (35, 90, 145))
            client = app_module.app.test_client()
            preview_payload = {
                "style": "style-2",
                "samples": [],
                "previewFreeImages": app_module.PREVIEW_SAMPLE_COUNT,
            }

            with (
                mock.patch.object(app_module, "LIBRARY_DIR", root),
                mock.patch.object(app_module, "current_menu_cache_key", return_value="menu-test"),
                mock.patch.object(app_module, "public_style_ids", return_value={"style-2"}),
            ):
                identity = app_module.build_selected_background_asset("style-2", source)
                with mock.patch.object(
                    app_module,
                    "preview_samples",
                    return_value=preview_payload,
                ) as preview:
                    response = client.get(
                        "/api/style-preview",
                        query_string={
                            "style": "style-2",
                            "backgroundAssetId": identity.asset_id,
                            "backgroundSha256": identity.sha256,
                        },
                    )

            self.assertEqual(response.status_code, 200)
            preview.assert_called_once()
            resolved = preview.call_args.kwargs["selected_background"]
            self.assertEqual(resolved.asset_id, identity.asset_id)
            self.assertEqual(resolved.sha256, identity.sha256)
            self.assertEqual(resolved.path, identity.path)


if __name__ == "__main__":
    unittest.main()
