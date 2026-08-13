from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

import job_rules as rules
from generation_queue import InMemoryGenerationQueue

import app as app_module


TEST_ATTESTATION_SECRET = "generation-queue-signing-secret-32-bytes-minimum"


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

    def test_local_batch_progress_callback_keeps_terminal_counts_uncommitted(self) -> None:
        queue = self.make_queue()
        queue.store.reserve("batch-progress", requested=3)
        queue.store.start("batch-progress")

        with mock.patch.object(app_module, "generation_queue", queue):
            progress = app_module.local_generation_progress_callback(
                "batch-progress"
            )
            progress(1, 0, 2)
            first = queue.get("batch-progress")
            progress(2, 1, 0)
            final = queue.get("batch-progress")

        assert first is not None
        assert final is not None
        self.assertEqual(
            (first.completed, first.failed, first.pending),
            (0, 0, 3),
        )
        self.assertEqual(
            (final.completed, final.failed, final.pending),
            (0, 0, 3),
        )
        self.assertEqual(
            first.result["rowProgress"],
            {
                "processed": 1,
                "succeeded": 1,
                "failed": 0,
                "pending": 2,
            },
        )
        self.assertEqual(
            final.result["rowProgress"],
            {
                "processed": 3,
                "succeeded": 2,
                "failed": 1,
                "pending": 0,
            },
        )

        failed_job = queue.fail("batch-progress", "manifest write failed")
        self.assertEqual(failed_job.status, rules.STATUS_FAILED)
        self.assertEqual(
            (failed_job.completed, failed_job.failed, failed_job.pending),
            (0, 3, 0),
        )

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
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=True),
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

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "auth_required")
        self.assertEqual(queue.list(), [])
        build_plan.assert_not_called()

    def test_authenticated_session_owns_job_and_ignores_spoofed_user_header(self) -> None:
        queue = self.make_queue()
        client = app_module.app.test_client()
        selected_background = app_module.SelectedBackgroundAsset(
            asset_id="bg_test",
            menu_key="1" * 12,
            style_id="style-1",
            sha256="2" * 64,
            path=Path("/tmp/bg-test.image"),
            width=1024,
            height=768,
        )
        menu_snapshot = {
            "id": "menu_" + ("a" * 32),
            "objectKey": "menus/menu.xlsx",
            "sha256": "1" * 64,
            "parserVersion": 1,
            "ownerUserId": "server-user",
            "originalFilename": "menu.xlsx",
            "summary": {"count": 3},
        }
        background_snapshot = {
            "assetId": selected_background.asset_id,
            "styleId": selected_background.style_id,
            "sha256": selected_background.sha256,
            "objectKey": "generated/selected-backgrounds/bg_test/image",
            "width": selected_background.width,
            "height": selected_background.height,
        }

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
            mock.patch.object(app_module, "tencent_ready", return_value=True),
            mock.patch.object(app_module, "generation_write_authorized", return_value=False),
            mock.patch.object(app_module, "local_demo_generation_allowed", return_value=False),
            mock.patch.object(
                app_module,
                "object_access_signing_secret",
                return_value=TEST_ATTESTATION_SECRET,
            ),
            mock.patch.object(
                app_module,
                "require_authenticated_session",
                return_value=({"user_id": "server-user"}, None),
            ),
            mock.patch.object(app_module, "resolve_menu_upload_snapshot", return_value=menu_snapshot),
            mock.patch.object(app_module, "materialize_menu_upload_snapshot", return_value=Path("/tmp/menu.xlsx")),
            mock.patch.object(app_module, "requested_selected_background", return_value=selected_background),
            mock.patch.object(app_module, "selected_background_batch_snapshot", return_value=background_snapshot),
            mock.patch.object(app_module, "batch_watermark_snapshot", return_value={"enabled": False}),
            mock.patch.object(
                app_module.billing,
                "debit_account",
                return_value={"idempotent": False, "balance": 970},
            ) as debit_account,
            mock.patch.object(
                app_module,
                "persist_generation_batch_contract",
                return_value=({"status": "queued"}, True),
            ),
            mock.patch.object(app_module, "run_generation_batch_job", return_value={"generation": {}}),
            mock.patch.object(app_module, "account_payload", return_value={"balance": 970}),
        ):
            response = client.post(
                "/api/generation-jobs",
                headers={
                    "Authorization": "Bearer valid-session",
                    "X-User-Id": "attacker-user",
                },
                json={
                    "style": "style-1",
                    "quality": "standard",
                    "jobId": "browser-idempotency-key",
                    "menuUploadId": menu_snapshot["id"],
                    "platforms": ["meituan"],
                    "watermark": {"enabled": False},
                    "imageCount": 9999,
                },
            )
            queue.join(timeout=2)

        self.assertEqual(response.status_code, 200)
        created = response.get_json()
        self.assertTrue(created["jobId"].startswith("generation-"))
        self.assertNotEqual(created["jobId"], "browser-idempotency-key")
        jobs = queue.list()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].metadata["userId"], "server-user")
        self.assertNotEqual(jobs[0].metadata["userId"], "attacker-user")
        self.assertEqual(jobs[0].metadata["menuUploadId"], menu_snapshot["id"])
        self.assertEqual(len(jobs[0].metadata["requestSha256"]), 64)
        debit_account.assert_called_once()
        self.assertEqual(debit_account.call_args.args[0], "server-user")
        self.assertEqual(debit_account.call_args.args[2], 30)

    def test_generation_job_status_and_cancel_hide_other_users_job(self) -> None:
        queue = self.make_queue()
        queue.store.reserve(
            "owned-job",
            requested=1,
            metadata={"style": "style-1", "userId": "owner-user"},
        )
        client = app_module.app.test_client()

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(
                app_module,
                "generation_request_principal",
                return_value=(
                    {
                        "userId": "other-user",
                        "internal": False,
                        "localDemo": False,
                    },
                    None,
                ),
            ),
        ):
            hidden_status = client.get("/api/generation-jobs/owned-job")
            hidden_cancel = client.post(
                "/api/generation-jobs/owned-job/cancel"
            )

        self.assertEqual(hidden_status.status_code, 404)
        self.assertEqual(hidden_cancel.status_code, 404)
        self.assertEqual(queue.get("owned-job").status, rules.STATUS_QUEUED)

        with (
            mock.patch.object(app_module, "generation_queue", queue),
            mock.patch.object(
                app_module,
                "generation_request_principal",
                return_value=(
                    {
                        "userId": "owner-user",
                        "internal": False,
                        "localDemo": False,
                    },
                    None,
                ),
            ),
        ):
            visible = client.get("/api/generation-jobs/owned-job")

        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.get_json()["jobId"], "owned-job")


if __name__ == "__main__":
    unittest.main()
