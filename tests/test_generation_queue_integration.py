from __future__ import annotations

import threading
import unittest
from unittest import mock

import job_rules as rules
from generation_queue import InMemoryGenerationQueue

import app as app_module


class GenerationQueueIntegrationTests(unittest.TestCase):
    def make_queue(self, worker_count: int = 1) -> InMemoryGenerationQueue:
        queue = InMemoryGenerationQueue(worker_count=worker_count)
        self.addCleanup(queue.shutdown)
        return queue

    def test_post_enqueues_job_and_get_returns_result(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()
        plan = {"selectedStyle": "style-1", "styles": [], "results": [], "summary": {}}
        generation = {"status": "succeeded", "succeeded": 0, "failed": 0, "pending": 0}

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
            mock.patch.object(app_module, "build_plan", return_value=plan),
            mock.patch.object(app_module, "materialize_final_images", return_value=generation),
        ):
            response = client.post("/api/generation-jobs", json={"style": "style-1", "quality": "standard"})
            self.assertEqual(response.status_code, 200)
            created = response.get_json()
            self.assertIn(created["status"], {rules.STATUS_QUEUED, rules.STATUS_RUNNING, rules.STATUS_COMPLETED})
            self.assertTrue(created["jobId"].startswith("generation-"))

            lookup = client.get(f"/api/generation-jobs/{created['jobId']}")
            self.assertEqual(lookup.status_code, 200)
            self.assertEqual(lookup.get_json()["jobId"], created["jobId"])

            queue.join(timeout=2)
            completed = client.get(f"/api/generation-jobs/{created['jobId']}").get_json()

        self.assertEqual(completed["status"], rules.STATUS_COMPLETED)
        self.assertEqual(completed["pending"], 0)
        self.assertEqual(completed["completed"], 1)
        self.assertEqual(completed["failed"], 0)
        self.assertIsNone(completed["error"])
        self.assertEqual(completed["result"]["generation"], generation)
        self.assertIn("finishedAt", completed)
        self.assertIn("elapsedSeconds", completed)
        self.assertIn("timedOut", completed)
        self.assertFalse(completed["timedOut"])
        self.assertFalse(completed["stale"])

    def test_get_refreshes_timeout_and_returns_explicit_timeout_payload(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()
        queue.store.reserve("slow-job", requested=1, metadata={"style": "style-1"})
        queue.store.start("slow-job")

        def fail_timeout(**kwargs):
            return [queue.fail("slow-job", app_module.GENERATION_JOB_TIMEOUT_ERROR)]

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(queue, "fail_timed_out", side_effect=fail_timeout) as fail_timed_out,
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
        ):
            response = client.get("/api/generation-jobs/slow-job")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(fail_timed_out.called)
        self.assertEqual(payload["status"], rules.STATUS_FAILED)
        self.assertEqual(payload["error"], app_module.GENERATION_JOB_TIMEOUT_ERROR)
        self.assertTrue(payload["timedOut"])
        self.assertEqual(payload["timingReason"], "timeout")
        self.assertEqual(payload["pending"], 0)
        self.assertEqual(payload["failed"], 1)
        self.assertIsNotNone(payload["finishedAt"])
        self.assertIsNotNone(payload["elapsedSeconds"])

    def test_cancel_endpoint_cancels_queued_job_and_prevents_execution(self) -> None:
        queue = self.make_queue(worker_count=1)
        client = app_module.app.test_client()
        first_started = threading.Event()
        release_first = threading.Event()
        executed: list[str] = []

        def slow_task() -> str:
            first_started.set()
            self.assertTrue(release_first.wait(timeout=2))
            return "done"

        def should_not_run() -> str:
            executed.append("cancel-api")
            return "ran"

        queue.enqueue("slow", slow_task)
        self.assertTrue(first_started.wait(timeout=2))
        queue.enqueue("cancel-api", should_not_run, requested=1, metadata={"style": "style-1"})

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
        ):
            response = client.post("/api/generation-jobs/cancel-api/cancel")

        release_first.set()
        queue.join(timeout=2)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["jobId"], "cancel-api")
        self.assertEqual(payload["status"], rules.STATUS_CANCELED)
        self.assertEqual(payload["pending"], 0)
        self.assertEqual(payload["canceled"], 1)
        self.assertEqual(executed, [])

    def test_cancel_endpoint_does_not_cancel_completed_job(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()
        queue.store.reserve("already-done", requested=1, metadata={"style": "style-1"})
        queue.store.complete("already-done", result={"ok": True})

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
        ):
            response = client.post("/api/generation-jobs/already-done/cancel")

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["code"], "generation_job_already_finished")
        self.assertEqual(payload["job"]["status"], rules.STATUS_COMPLETED)
        job = queue.get("already-done")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.status, rules.STATUS_COMPLETED)
        self.assertEqual(job.result, {"ok": True})
        self.assertEqual(job.canceled, 0)

    def test_invalid_style_returns_400_without_crashing_or_enqueueing(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "build_plan") as build_plan,
        ):
            response = client.post("/api/generation-jobs", json={"style": "missing-style", "quality": "standard"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(queue.list(), [])
        build_plan.assert_not_called()

    def test_post_returns_429_when_generation_queue_is_full(self) -> None:
        queue = mock.Mock()
        queue.fail_timed_out.return_value = []
        queue.enqueue.side_effect = RuntimeError(
            "generation queue admission denied: max_pending_jobs_exceeded"
        )
        client = app_module.app.test_client()

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
            mock.patch.object(app_module, "build_plan") as build_plan,
        ):
            response = client.post("/api/generation-jobs", json={"style": "style-1", "quality": "standard"})

        self.assertEqual(response.status_code, 429)
        payload = response.get_json()
        self.assertEqual(payload["code"], "generation_queue_full")
        self.assertIn("队列已满", payload["error"])
        self.assertIn("max_pending_jobs_exceeded", payload["reason"])
        queue.enqueue.assert_called_once()
        build_plan.assert_not_called()

    def test_post_returns_503_when_generation_queue_is_shut_down(self) -> None:
        queue = mock.Mock()
        queue.fail_timed_out.return_value = []
        queue.enqueue.side_effect = RuntimeError("generation queue is shut down")
        client = app_module.app.test_client()

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
            mock.patch.object(app_module, "build_plan") as build_plan,
        ):
            response = client.post("/api/generation-jobs", json={"style": "style-1", "quality": "standard"})

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["code"], "generation_queue_unavailable")
        self.assertIn("队列暂不可用", payload["error"])
        self.assertIn("shut down", payload["reason"])
        queue.enqueue.assert_called_once()
        build_plan.assert_not_called()

    def test_post_is_idempotent_for_same_style_quality_default_job_id(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()
        release = threading.Event()
        build_calls: list[tuple[str, str]] = []

        def slow_build_plan(style: str, quality: str) -> dict[str, object]:
            build_calls.append((style, quality))
            self.assertTrue(release.wait(timeout=2))
            return {"selectedStyle": style, "styles": [], "results": [], "summary": {}}

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
            mock.patch.object(app_module, "build_plan", side_effect=slow_build_plan),
            mock.patch.object(app_module, "materialize_final_images", return_value={"status": "succeeded"}),
        ):
            first = client.post("/api/generation-jobs", json={"style": "style-1", "quality": "premium"}).get_json()
            second = client.post("/api/generation-jobs", json={"style": "style-1", "quality": "premium"}).get_json()
            release.set()
            queue.join(timeout=2)

        self.assertEqual(first["jobId"], second["jobId"])
        self.assertEqual(len(queue.list()), 1)
        self.assertEqual(build_calls, [("style-1", "premium")])

    def test_tencent_generation_requires_generation_token_or_local_demo(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=False),
            mock.patch.object(app_module, "build_plan") as build_plan,
        ):
            response = client.post("/api/generation-jobs", json={"style": "style-1", "quality": "standard"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(queue.list(), [])
        build_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
