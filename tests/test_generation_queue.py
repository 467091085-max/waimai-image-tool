from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import patch

import job_rules as rules
from generation_queue import InMemoryGenerationQueue, JobStore


class GenerationQueueTests(unittest.TestCase):
    def make_queue(self, worker_count: int = 2) -> InMemoryGenerationQueue:
        queue = InMemoryGenerationQueue(worker_count=worker_count)
        self.addCleanup(queue.shutdown)
        return queue

    def test_successful_job_records_counts_and_result(self) -> None:
        queue = self.make_queue()

        queued = queue.enqueue(
            "job-success",
            lambda: {"object_key": "generated/menu/one.jpg"},
            requested=1,
        )
        self.assertEqual(queued.status, rules.STATUS_QUEUED)

        queue.join(timeout=2)

        job = queue.get("job-success")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.status, rules.STATUS_COMPLETED)
        self.assertEqual(job.requested, 1)
        self.assertEqual(job.completed, 1)
        self.assertEqual(job.failed, 0)
        self.assertEqual(job.pending, 0)
        self.assertIsNone(job.error)
        self.assertEqual(job.result, {"object_key": "generated/menu/one.jpg"})
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertIsNotNone(job.elapsed)
        data = job.to_dict()
        self.assertEqual(data["status"], rules.STATUS_COMPLETED)
        self.assertIsNotNone(data["finished_at"])
        self.assertEqual(data["completed_at"], data["finished_at"])
        self.assertIsNotNone(data["elapsed"])
        self.assertEqual(queue.list(status=rules.STATUS_COMPLETED), [job])
        self.assertEqual(queue.list(status="succeeded"), [job])

    def test_partially_failed_job_records_progress_and_error(self) -> None:
        queue = self.make_queue()

        def task() -> None:
            queue.progress("job-partial", completed=2)
            raise RuntimeError("provider quota exhausted")

        queue.enqueue("job-partial", task, requested=3)
        queue.join(timeout=2)

        job = queue.get("job-partial")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.status, rules.STATUS_COMPLETED)
        self.assertEqual(job.requested, 3)
        self.assertEqual(job.completed, 2)
        self.assertEqual(job.failed, 1)
        self.assertEqual(job.pending, 0)
        self.assertIn("RuntimeError: provider quota exhausted", job.error or "")

    def test_cancel_pending_job_prevents_execution(self) -> None:
        queue = self.make_queue(worker_count=1)
        first_started = threading.Event()
        release_first = threading.Event()
        executed: list[str] = []

        def slow_task() -> str:
            first_started.set()
            self.assertTrue(release_first.wait(timeout=2))
            return "done"

        def should_not_run() -> str:
            executed.append("cancel-me")
            return "ran"

        queue.enqueue("slow", slow_task)
        self.assertTrue(first_started.wait(timeout=2))
        queue.enqueue("cancel-me", should_not_run)

        canceled = queue.cancel("cancel-me", error="user canceled")
        self.assertEqual(canceled.status, rules.STATUS_CANCELED)
        self.assertEqual(canceled.pending, 0)

        release_first.set()
        queue.join(timeout=2)

        self.assertEqual(executed, [])
        job = queue.get("cancel-me")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.status, rules.STATUS_CANCELED)
        self.assertEqual(job.error, "user canceled")

    def test_cancel_completed_job_does_not_mutate_terminal_job(self) -> None:
        queue = self.make_queue(worker_count=1)

        queue.enqueue("done", lambda: "finished")
        queue.join(timeout=2)

        completed = queue.get("done")
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.status, rules.STATUS_COMPLETED)

        after_cancel = queue.cancel("done", error="user canceled")

        self.assertEqual(after_cancel.status, rules.STATUS_COMPLETED)
        self.assertEqual(after_cancel.result, "finished")
        self.assertEqual(after_cancel.canceled, 0)
        self.assertIsNone(after_cancel.error)
        self.assertEqual(queue.get("done"), completed)

    def test_enqueue_is_idempotent_for_same_job_id(self) -> None:
        queue = self.make_queue(worker_count=1)
        release = threading.Event()
        calls: list[str] = []

        def first_task() -> str:
            calls.append("first")
            self.assertTrue(release.wait(timeout=2))
            return "first-result"

        def duplicate_task() -> str:
            calls.append("duplicate")
            return "duplicate-result"

        queue.enqueue("same-job", first_task)
        duplicate = queue.enqueue("same-job", duplicate_task)
        self.assertEqual(duplicate.job_id, "same-job")
        self.assertEqual(len(queue.list()), 1)

        release.set()
        queue.join(timeout=2)

        job = queue.get("same-job")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(calls, ["first"])
        self.assertEqual(job.status, rules.STATUS_COMPLETED)
        self.assertEqual(job.result, "first-result")

    def test_worker_count_runs_jobs_concurrently(self) -> None:
        queue = self.make_queue(worker_count=2)
        lock = threading.Lock()
        release = threading.Event()
        two_active = threading.Event()
        active = 0
        max_active = 0

        def concurrent_task(name: str) -> str:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    two_active.set()

            self.assertTrue(release.wait(timeout=2))

            with lock:
                active -= 1
            return name

        queue.enqueue("job-a", concurrent_task, "a")
        queue.enqueue("job-b", concurrent_task, "b")

        self.assertTrue(two_active.wait(timeout=2))
        release.set()
        queue.join(timeout=2)

        self.assertEqual(max_active, 2)
        self.assertEqual(queue.get("job-a").status, rules.STATUS_COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(queue.get("job-b").status, rules.STATUS_COMPLETED)  # type: ignore[union-attr]

    def test_queue_rejects_new_jobs_when_pending_limit_is_full(self) -> None:
        queue = InMemoryGenerationQueue(worker_count=1, max_pending_jobs=1)
        self.addCleanup(queue.shutdown)
        started = threading.Event()
        release = threading.Event()

        def slow_task() -> str:
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return "done"

        queue.enqueue("slow", slow_task)
        self.assertTrue(started.wait(timeout=2))

        with self.assertRaisesRegex(RuntimeError, "max_pending_jobs_exceeded"):
            queue.enqueue("blocked", lambda: "blocked")

        release.set()
        queue.join(timeout=2)

    def test_timed_out_running_job_stays_failed_after_late_task_return(self) -> None:
        queue = InMemoryGenerationQueue(
            worker_count=1,
            stale_after_seconds=1,
            timeout_seconds=2,
        )
        self.addCleanup(queue.shutdown)
        started = threading.Event()
        release = threading.Event()

        def slow_task() -> str:
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return "late-result"

        queue.enqueue("too-slow", slow_task, requested=1)
        self.assertTrue(started.wait(timeout=2))
        running = queue.get("too-slow")
        self.assertIsNotNone(running)
        assert running is not None
        assert running.started_at is not None

        check_at = running.started_at + timedelta(seconds=3)
        timing = queue.inspect("too-slow", now=check_at)
        self.assertIsNotNone(timing)
        assert timing is not None
        self.assertTrue(timing["timed_out"])

        failed = queue.fail_timed_out(now=check_at, error="provider timeout")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].status, rules.STATUS_FAILED)
        self.assertEqual(failed[0].failed, 1)

        release.set()
        queue.join(timeout=2)

        final = queue.get("too-slow")
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.status, rules.STATUS_FAILED)
        self.assertEqual(final.result, None)
        self.assertEqual(final.error, "provider timeout")

    def test_snapshot_reports_counts_limits_ages_stale_timeout_and_closed(self) -> None:
        base_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        limits = rules.resolve_queue_limits(
            worker_count=3,
            max_pending_jobs=9,
            stale_after_seconds=5,
            timeout_seconds=20,
        )
        store = JobStore()

        with patch(
            "generation_queue._utc_now",
            return_value=base_time - timedelta(seconds=12),
        ):
            store.enqueue("queued-stale")
        with patch(
            "generation_queue._utc_now",
            return_value=base_time - timedelta(seconds=3),
        ):
            store.enqueue("queued-fresh")
        with patch(
            "generation_queue._utc_now",
            return_value=base_time - timedelta(seconds=25),
        ):
            store.enqueue("running-timeout")
            store.start("running-timeout")
        with patch(
            "generation_queue._utc_now",
            return_value=base_time - timedelta(seconds=4),
        ):
            store.enqueue("running-fresh")
            store.start("running-fresh")
        with patch(
            "generation_queue._utc_now",
            return_value=base_time - timedelta(seconds=2),
        ):
            store.enqueue("completed")
            store.start("completed")
            store.complete("completed")

        queue = InMemoryGenerationQueue(limits=limits, store=store)
        self.addCleanup(queue.shutdown, wait_for_jobs=False)

        snapshot = queue.snapshot(now=base_time)

        self.assertEqual(
            snapshot["countsByStatus"],
            {
                rules.STATUS_QUEUED: 2,
                rules.STATUS_RUNNING: 2,
                rules.STATUS_COMPLETED: 1,
                rules.STATUS_FAILED: 0,
                rules.STATUS_CANCELED: 0,
            },
        )
        self.assertEqual(snapshot["workerCount"], 3)
        self.assertEqual(
            snapshot["limits"],
            {
                "workerCount": 3,
                "maxPendingJobs": 9,
                "staleAfterSeconds": 5.0,
                "timeoutSeconds": 20.0,
            },
        )
        self.assertEqual(snapshot["oldestQueuedAgeSeconds"], 12.0)
        self.assertEqual(snapshot["oldestRunningAgeSeconds"], 25.0)
        self.assertEqual(snapshot["staleCount"], 2)
        self.assertEqual(snapshot["timedOutCount"], 1)
        self.assertFalse(snapshot["closed"])

        running = store.get("running-timeout")
        self.assertIsNotNone(running)
        assert running is not None
        self.assertEqual(running.status, rules.STATUS_RUNNING)

        queue.shutdown(wait_for_jobs=False)
        closed_snapshot = queue.snapshot(now=base_time)
        self.assertTrue(closed_snapshot["closed"])
        self.assertEqual(closed_snapshot["countsByStatus"], snapshot["countsByStatus"])


class JobStoreTests(unittest.TestCase):
    def test_manual_lifecycle_methods_update_queryable_state(self) -> None:
        store = JobStore()

        created = store.enqueue("manual", requested=2, metadata={"style": "style-1"})
        self.assertEqual(created.pending, 2)

        running = store.start("manual")
        self.assertEqual(running.status, rules.STATUS_RUNNING)

        progressed = store.progress("manual", completed=1)
        self.assertEqual(progressed.completed, 1)
        self.assertEqual(progressed.pending, 1)

        completed = store.complete("manual", result={"generated": 2})
        self.assertEqual(completed.status, rules.STATUS_COMPLETED)
        self.assertEqual(completed.completed, 2)
        self.assertEqual(completed.pending, 0)
        self.assertEqual(completed.result, {"generated": 2})
        self.assertIsNotNone(completed.finished_at)
        self.assertIsNotNone(completed.elapsed)
        self.assertEqual(store.get("manual"), completed)
        self.assertEqual(store.list(status=rules.STATUS_COMPLETED), [completed])

    def test_store_reports_and_fails_timed_out_job_without_sleeping(self) -> None:
        store = JobStore()
        store.enqueue("timeout", requested=2)
        running = store.start("timeout")
        self.assertIsNotNone(running.started_at)
        assert running.started_at is not None
        limits = rules.resolve_queue_limits(stale_after_seconds=5, timeout_seconds=20)

        stale_at = running.started_at + timedelta(seconds=6)
        timing = store.inspect_timing("timeout", now=stale_at, limits=limits)
        self.assertTrue(timing["stale"])
        self.assertFalse(timing["timed_out"])
        self.assertEqual(store.list_stale(now=stale_at, limits=limits), [store.get("timeout")])

        timeout_at = running.started_at + timedelta(seconds=21)
        timed_out = store.fail_timed_out(
            now=timeout_at,
            limits=limits,
            error="generation timeout",
        )

        self.assertEqual(len(timed_out), 1)
        failed = timed_out[0]
        self.assertEqual(failed.status, rules.STATUS_FAILED)
        self.assertEqual(failed.failed, 2)
        self.assertEqual(failed.pending, 0)
        self.assertEqual(failed.error, "generation timeout")
        self.assertEqual(failed.finished_at, timeout_at)
        self.assertEqual(failed.elapsed, 21.0)


if __name__ == "__main__":
    unittest.main()
