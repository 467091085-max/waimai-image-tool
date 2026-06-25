from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import generation_engine
import generation_jobs


def menu_item(row: int, name: str, *, points: int = 10) -> dict[str, object]:
    return {
        "row": row,
        "category": "测试分类",
        "name": name,
        "kind": "单品",
        "points": points,
        "candidates": [],
        "backgroundAction": "智能补图",
        "publicStatus": "待正式生成",
    }


class GenerationJobStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def create_job(self, count: int = 2) -> dict[str, object]:
        return generation_jobs.create_job(
            user_id="u1",
            style="style-1",
            quality="standard",
            items=[menu_item(index, f"菜品{index}") for index in range(1, count + 1)],
            points=count * 10,
            db_path=self.db_path,
        )

    def test_create_job_persists_items_and_initial_progress(self) -> None:
        job = self.create_job(2)

        self.assertEqual(job["status"], generation_jobs.JOB_CREATED)
        self.assertEqual(job["totalItems"], 2)
        self.assertEqual(job["pendingItems"], 2)
        self.assertEqual(job["progress"]["percent"], 0)
        self.assertEqual([item["status"] for item in job["items"]], [generation_jobs.ITEM_PENDING, generation_jobs.ITEM_PENDING])
        self.assertEqual(job["items"][0]["payload"]["name"], "菜品1")

        reloaded = generation_jobs.get_job(str(job["id"]), db_path=self.db_path)
        self.assertEqual(reloaded["items"][1]["dish"], "菜品2")
        self.assertEqual(reloaded["points"], 20)

    def test_mark_paid_running_completed_failed_and_retry_failed_items(self) -> None:
        job = self.create_job(2)

        paid = generation_jobs.mark_paid(str(job["id"]), order_id="debit-1", db_path=self.db_path)
        self.assertEqual(paid["status"], generation_jobs.JOB_PAID)
        self.assertEqual(paid["orderId"], "debit-1")
        self.assertIsNotNone(paid["paidAt"])

        self.assertEqual(generation_jobs.mark_queued(str(job["id"]), db_path=self.db_path)["status"], generation_jobs.JOB_QUEUED)
        self.assertEqual(generation_jobs.mark_running(str(job["id"]), db_path=self.db_path)["status"], generation_jobs.JOB_RUNNING)
        generation_jobs.record_item_status(
            str(job["id"]),
            1,
            generation_jobs.ITEM_COMPLETED,
            provider="fake",
            action="TextToImageLite",
            result={"status": "succeeded"},
            db_path=self.db_path,
        )
        partially_failed = generation_jobs.record_item_status(
            str(job["id"]),
            2,
            generation_jobs.ITEM_FAILED,
            error="model timeout",
            result={"status": "failed", "error": "model timeout"},
            db_path=self.db_path,
        )

        self.assertEqual(partially_failed["status"], generation_jobs.JOB_PARTIALLY_FAILED)
        self.assertEqual(partially_failed["completedItems"], 1)
        self.assertEqual(partially_failed["failedItems"], 1)
        self.assertEqual(partially_failed["items"][1]["error"], "model timeout")

        retry = generation_jobs.retry_failed_items(str(job["id"]), db_path=self.db_path)
        self.assertEqual(retry["status"], generation_jobs.JOB_QUEUED)
        self.assertEqual(retry["completedItems"], 1)
        self.assertEqual(retry["failedItems"], 0)
        self.assertEqual(retry["pendingItems"], 1)
        self.assertEqual(retry["items"][1]["status"], generation_jobs.ITEM_QUEUED)
        self.assertIsNone(retry["items"][1]["error"])

    def test_run_job_is_idempotent_across_repeated_runs(self) -> None:
        job = self.create_job(3)
        calls: list[int] = []

        def runner(item: dict[str, object]) -> dict[str, object]:
            calls.append(int(item["index"]))
            payload = dict(item["payload"])
            payload["generated"] = True
            return {"status": "succeeded", "provider": "fake", "action": "Generate", "payload": payload}

        first = generation_jobs.run_job(str(job["id"]), runner, limit=1, db_path=self.db_path)
        self.assertEqual(calls, [1])
        self.assertEqual(first["status"], generation_jobs.JOB_QUEUED)
        self.assertEqual(first["completedItems"], 1)
        self.assertEqual(first["pendingItems"], 2)
        self.assertEqual(first["lastRun"]["selected"], 1)

        second = generation_jobs.run_job(str(job["id"]), runner, limit=5, db_path=self.db_path)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(second["status"], generation_jobs.JOB_COMPLETED)
        self.assertEqual(second["completedItems"], 3)
        self.assertEqual([item["attempts"] for item in second["items"]], [1, 1, 1])

        third = generation_jobs.run_job(str(job["id"]), runner, limit=5, db_path=self.db_path)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(third["status"], generation_jobs.JOB_COMPLETED)
        self.assertEqual(third["lastRun"]["selected"], 0)
        self.assertEqual([item["attempts"] for item in third["items"]], [1, 1, 1])

    def test_failed_item_does_not_block_rest_of_batch(self) -> None:
        job = self.create_job(3)

        def runner(item: dict[str, object]) -> dict[str, object]:
            if int(item["index"]) == 2:
                raise RuntimeError("single image failed")
            return {"status": "succeeded", "provider": "fake", "action": "Generate"}

        result = generation_jobs.run_job(str(job["id"]), runner, limit=10, db_path=self.db_path)

        self.assertEqual(result["status"], generation_jobs.JOB_PARTIALLY_FAILED)
        self.assertEqual(result["completedItems"], 2)
        self.assertEqual(result["failedItems"], 1)
        self.assertEqual(result["lastRun"]["completed"], 2)
        self.assertEqual(result["lastRun"]["failed"], 1)
        self.assertEqual(
            [item["status"] for item in result["items"]],
            [generation_jobs.ITEM_COMPLETED, generation_jobs.ITEM_FAILED, generation_jobs.ITEM_COMPLETED],
        )
        self.assertIn("single image failed", result["items"][1]["error"])
        self.assertEqual(result["items"][1]["providerError"], "single image failed")
        self.assertTrue(result["items"][1]["retryable"])
        self.assertTrue(result["items"][1]["refundRequired"])
        self.assertTrue(result["refundRequired"])

    def test_runner_failure_result_records_provider_error_retry_and_refund_hook(self) -> None:
        job = self.create_job(2)

        def runner(item: dict[str, object]) -> dict[str, object]:
            if int(item["index"]) == 1:
                return {
                    "status": "failed",
                    "provider": "tencent-hunyuan",
                    "action": "SubmitTextToImageJob",
                    "provider_error": "hunyuan.tencentcloudapi.com ResourceInsufficient: 资源不足",
                    "retryable": True,
                    "refund_required": True,
                }
            return {"status": "succeeded", "provider": "library", "action": "Reuse"}

        result = generation_jobs.run_job(str(job["id"]), runner, limit=10, db_path=self.db_path)

        failed = result["items"][0]
        self.assertEqual(result["status"], generation_jobs.JOB_PARTIAL)
        self.assertEqual(failed["provider"], "tencent-hunyuan")
        self.assertIn("ResourceInsufficient", failed["providerError"])
        self.assertTrue(failed["retryable"])
        self.assertTrue(failed["refundRequired"])


class GenerationEngineRoutingTests(unittest.TestCase):
    def test_generation_request_structure_and_strategy_order(self) -> None:
        same_style = {"dishName": "红烧肉", "styleId": "style-1", "reusable": True, "path": "/tmp/a.jpg"}
        diff_style = {"dishName": "红烧肉", "styleId": "style-2", "reusable": True, "path": "/tmp/b.jpg"}
        watermark = {"dishName": "红烧肉", "styleId": "style-2", "reusable": False, "source": "watermarkpic", "path": "/tmp/c.jpg"}

        reuse_request = generation_engine.request_from_row(
            {**menu_item(1, "红烧肉"), "candidates": [same_style, diff_style]},
            style="style-1",
            quality="standard",
            platforms=["meituan"],
            watermark={"enabled": True},
        )
        self.assertEqual(reuse_request.dish, "红烧肉")
        self.assertEqual(reuse_request.kind, generation_engine.KIND_SINGLE)
        self.assertEqual(reuse_request.quality, generation_engine.QUALITY_NORMAL)
        self.assertEqual(reuse_request.platforms, ("meituan",))
        self.assertEqual(generation_engine.select_generation_request(reuse_request).source_strategy, generation_engine.STRATEGY_REPLACE_BACKGROUND)

        cached_same_style = {**same_style, "generated": True, "aiProvider": "tencent-hunyuan", "generationStatus": "succeeded"}
        cached_request = generation_engine.request_from_row({**menu_item(10, "红烧肉"), "candidates": [cached_same_style]}, style="style-1")
        self.assertEqual(generation_engine.select_generation_request(cached_request).source_strategy, generation_engine.STRATEGY_REUSE)

        replace_request = generation_engine.request_from_row({**menu_item(2, "红烧肉"), "candidates": [diff_style]}, style="style-1")
        self.assertEqual(generation_engine.select_generation_request(replace_request).source_strategy, generation_engine.STRATEGY_REPLACE_BACKGROUND)

        redraw_request = generation_engine.request_from_row({**menu_item(3, "红烧肉"), "candidates": [watermark]}, style="style-1")
        self.assertEqual(generation_engine.select_generation_request(redraw_request).source_strategy, generation_engine.STRATEGY_REFERENCE_REDRAW)

        text_request = generation_engine.request_from_row(menu_item(4, "新品汤"), style="style-1")
        self.assertEqual(generation_engine.select_generation_request(text_request).source_strategy, generation_engine.STRATEGY_TEXT_TO_IMAGE3)

    def test_combo_without_combo_reference_does_not_use_single_candidate(self) -> None:
        single_candidate = {"dishName": "红烧肉", "styleId": "style-2", "reusable": True, "path": "/tmp/single.jpg"}
        combo_row = {
            **menu_item(5, "红烧肉+青菜套餐"),
            "kind": "套餐/组合",
            "components": ["红烧肉", "青菜"],
            "candidates": [single_candidate],
        }

        routed = generation_engine.select_generation_request(generation_engine.request_from_row(combo_row, style="style-1"))

        self.assertEqual(routed.kind, generation_engine.KIND_COMBO)
        self.assertEqual(routed.source_strategy, generation_engine.STRATEGY_TEXT_TO_IMAGE3)
        self.assertIsNone(routed.source_candidate)


class GenerationJobApiTests(unittest.TestCase):
    def test_job_api_create_run_and_query_returns_pollable_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.db"
            plan = {
                "menu": {"count": 2},
                "summary": {"total": 2, "points": 20},
                "selectedStyle": "style-1",
                "quality": {"id": "standard"},
                "pricing": {},
                "pipeline": {},
                "results": [menu_item(1, "辣椒炒肉"), menu_item(2, "小炒黄牛肉")],
            }

            def fake_run_formal(row: dict[str, object], *, style: str, quality: str | None = "standard", platforms=None, watermark=False) -> dict[str, object]:
                row["generation"] = {
                    "row": row["row"],
                    "dish": row["name"],
                    "status": "succeeded",
                    "provider": "fake",
                    "action": "SubmitTextToImageJob",
                    "sourceStrategy": "text_to_image3",
                    "succeeded": True,
                }
                row["publicStatus"] = "已生成"
                return {"result": row["generation"], "row": row}

            with (
                mock.patch.dict(os.environ, {"GENERATION_JOBS_DB_PATH": str(db_path)}),
                mock.patch.object(app_module, "build_plan", return_value=plan),
                mock.patch.object(app_module, "run_formal_generation_item", side_effect=fake_run_formal),
            ):
                client = app_module.app.test_client()
                created = client.post("/api/jobs", json={"style": "style-1", "quality": "standard", "orderId": "debit-1"})
                self.assertEqual(created.status_code, 201)
                created_body = created.get_json()
                job_id = created_body["job"]["id"]
                self.assertEqual(created_body["job"]["status"], generation_jobs.JOB_PAID)
                self.assertEqual(created_body["poll"]["url"], f"/api/jobs/{job_id}")

                run = client.post(f"/api/jobs/{job_id}/run", json={"limit": 2})
                self.assertEqual(run.status_code, 200)
                run_body = run.get_json()
                self.assertEqual(run_body["job"]["status"], generation_jobs.JOB_COMPLETED)
                self.assertEqual(run_body["job"]["progress"]["completed"], 2)

                polled = client.get(f"/api/jobs/{job_id}")
                self.assertEqual(polled.status_code, 200)
                item = polled.get_json()["job"]["items"][0]
                self.assertEqual(item["result"]["generation"]["action"], "SubmitTextToImageJob")
                self.assertFalse(item["refundRequired"])


if __name__ == "__main__":
    unittest.main()
