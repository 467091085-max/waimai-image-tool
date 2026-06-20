from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
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

            def fake_materialize(plan_arg: dict[str, object], style: str, quality: str) -> dict[str, object]:
                row = plan_arg["results"][0]
                row["generation"] = {
                    "row": row["row"],
                    "dish": row["name"],
                    "status": "succeeded",
                    "provider": "fake",
                    "action": "TextToImageLite",
                    "succeeded": True,
                }
                row["publicStatus"] = "已生成"
                return {"provider": "fake", "items": [row["generation"]], "succeeded": 1, "failed": 0}

            with (
                mock.patch.dict(os.environ, {"GENERATION_JOBS_DB_PATH": str(db_path)}),
                mock.patch.object(app_module, "build_plan", return_value=plan),
                mock.patch.object(app_module, "materialize_final_images", side_effect=fake_materialize),
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
                self.assertEqual(polled.get_json()["job"]["items"][0]["result"]["generation"]["action"], "TextToImageLite")


if __name__ == "__main__":
    unittest.main()
