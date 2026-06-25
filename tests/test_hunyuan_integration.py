from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import app as app_module
import generation_engine
import generation_jobs
from scripts import smoke_hunyuan_live


def save_image(path: Path, color: tuple[int, int, int] = (210, 80, 60)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color).save(path)


def menu_row(row: int = 1, name: str = "招牌辣椒炒肉") -> dict[str, object]:
    return {
        "row": row,
        "category": "smoke",
        "name": name,
        "kind": "单品",
        "components": [],
        "candidates": [],
        "backgroundAction": "智能补图",
        "publicStatus": "待正式生成",
        "points": 0,
    }


class HunyuanLiveIntegrationTests(unittest.TestCase):
    def test_formal_runner_waits_for_provider_without_local_fake_when_tencent_missing(self) -> None:
        row = menu_row()

        with (
            mock.patch.object(app_module, "tencent_ready", return_value=False),
            mock.patch.object(app_module, "draw_demo_image") as draw_demo,
        ):
            output = app_module.run_formal_generation_item(row, style="style-1", quality="standard")

        draw_demo.assert_not_called()
        self.assertEqual(output["result"]["status"], generation_engine.STATUS_QUEUED)
        self.assertEqual(output["result"]["providerStatus"], generation_engine.STATUS_QUEUED)
        self.assertEqual(output["result"]["sourceStrategy"], generation_engine.STRATEGY_WAITING_FOR_PROVIDER)
        self.assertEqual(output["result"]["provider"], generation_engine.PROVIDER_TENCENT)
        self.assertIn("waiting_for_provider", output["result"]["provider_error"])
        self.assertTrue(output["result"]["retryable"])
        self.assertFalse(output["result"]["refund_required"])
        self.assertEqual(row["generationStatus"], generation_engine.STATUS_QUEUED)

    def test_model_input_prefers_remote_url_over_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "local.jpg"
            save_image(source)
            candidate = {"url": "https://cdn.example.test/远程菜品.jpg", "path": str(source)}

            with mock.patch.object(app_module, "upload_model_input_to_cos", side_effect=AssertionError("should not upload")):
                url = app_module.model_input_public_url(candidate)

        self.assertEqual(url, "https://cdn.example.test/%E8%BF%9C%E7%A8%8B%E8%8F%9C%E5%93%81.jpg")

    def test_model_input_uploads_local_path_to_cos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "中文菜名.jpg"
            save_image(source)

            with (
                mock.patch.object(app_module, "MODEL_INPUT_DIR", root / "model_inputs"),
                mock.patch.object(app_module, "upload_model_input_to_cos", return_value="https://cos.example.test/model-input.jpg") as upload,
            ):
                url = app_module.model_input_public_url({"path": str(source)})

            self.assertEqual(url, "https://cos.example.test/model-input.jpg")
            upload.assert_called_once()
            uploaded_path = upload.call_args.args[0]
            self.assertRegex(uploaded_path.name, r"^[a-f0-9]{24}\.jpg$")
            self.assertTrue(uploaded_path.exists())

    def test_job_api_direct_items_create_and_run_waiting_provider_without_build_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            client = app_module.app.test_client()

            with (
                mock.patch.dict(os.environ, {"GENERATION_JOBS_DB_PATH": str(db_path)}),
                mock.patch.object(app_module, "build_plan", side_effect=AssertionError("direct items must not build plan")),
                mock.patch.object(app_module, "tencent_ready", return_value=False),
                mock.patch.object(app_module, "draw_demo_image") as draw_demo,
            ):
                created = client.post(
                    "/api/jobs",
                    json={"style": "style-1", "quality": "standard", "paid": True, "items": [menu_row()]},
                )
                self.assertEqual(created.status_code, 201)
                created_body = created.get_json()
                job_id = created_body["job"]["id"]
                self.assertEqual(created_body["job"]["totalItems"], 1)

                run = client.post(f"/api/jobs/{job_id}/run", json={"limit": 1})
                self.assertEqual(run.status_code, 200)
                body = run.get_json()

            draw_demo.assert_not_called()
            job = body["job"]
            item = job["items"][0]
            generation = item["result"]["generationResult"]
            self.assertEqual(job["status"], generation_jobs.JOB_QUEUED)
            self.assertEqual(item["status"], generation_jobs.ITEM_QUEUED)
            self.assertEqual(item["provider"], generation_engine.PROVIDER_TENCENT)
            self.assertEqual(generation["sourceStrategy"], generation_engine.STRATEGY_WAITING_FOR_PROVIDER)
            self.assertEqual(generation["providerStatus"], generation_engine.STATUS_QUEUED)
            self.assertIn("waiting_for_provider", generation["provider_error"])
            self.assertFalse(item["refundRequired"])

    def test_smoke_dry_run_checks_status_without_creating_or_running_job(self) -> None:
        args = argparse.Namespace(
            base_url="",
            live=False,
            limit=0,
            style="style-1",
            quality="standard",
            dish="招牌辣椒炒肉",
        )

        with (
            mock.patch.object(app_module, "tencent_ready", return_value=False),
            mock.patch.object(smoke_hunyuan_live.SmokeClient, "post_json", side_effect=AssertionError("dry-run must not post")),
        ):
            result = smoke_hunyuan_live.run_smoke(args)

        self.assertEqual(result["mode"], "dry-run")
        self.assertFalse(result["willRunProvider"])
        self.assertEqual(result["fixedDish"], "招牌辣椒炒肉")
        self.assertIn("--live --limit 1", result["liveCommandRequired"])
        self.assertEqual(result["status"], generation_engine.STATUS_QUEUED)


if __name__ == "__main__":
    unittest.main()
