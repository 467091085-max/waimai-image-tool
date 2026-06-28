from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

import job_rules as rules


class JobRulesTests(unittest.TestCase):
    def test_allows_expected_status_transitions(self) -> None:
        transitions = [
            (rules.STATUS_QUEUED, rules.STATUS_QUEUED),
            (rules.STATUS_QUEUED, rules.STATUS_RUNNING),
            (rules.STATUS_QUEUED, rules.STATUS_CANCELED),
            (rules.STATUS_RUNNING, rules.STATUS_RUNNING),
            (rules.STATUS_RUNNING, rules.STATUS_COMPLETED),
            (rules.STATUS_RUNNING, rules.STATUS_FAILED),
            (rules.STATUS_RUNNING, rules.STATUS_CANCELED),
            (rules.STATUS_COMPLETED, rules.STATUS_COMPLETED),
            (rules.STATUS_FAILED, rules.STATUS_FAILED),
            (rules.STATUS_CANCELED, rules.STATUS_CANCELED),
        ]

        for current, target in transitions:
            with self.subTest(current=current, target=target):
                self.assertEqual(rules.transition_job_status(current, target), target)

    def test_rejects_invalid_status_transitions(self) -> None:
        transitions = [
            (rules.STATUS_QUEUED, rules.STATUS_COMPLETED),
            (rules.STATUS_QUEUED, rules.STATUS_FAILED),
            (rules.STATUS_COMPLETED, rules.STATUS_RUNNING),
            (rules.STATUS_FAILED, rules.STATUS_RUNNING),
            (rules.STATUS_CANCELED, rules.STATUS_RUNNING),
        ]

        for current, target in transitions:
            with self.subTest(current=current, target=target):
                with self.assertRaises(ValueError):
                    rules.transition_job_status(current, target)

    def test_rejects_unknown_statuses(self) -> None:
        with self.assertRaises(ValueError):
            rules.transition_job_status("missing", rules.STATUS_RUNNING)

        with self.assertRaises(ValueError):
            rules.transition_job_status(rules.STATUS_QUEUED, "missing")

    def test_normalizes_legacy_success_status_names(self) -> None:
        self.assertEqual(rules.STATUS_SUCCEEDED, rules.STATUS_COMPLETED)
        self.assertEqual(rules.STATUS_PARTIALLY_SUCCEEDED, rules.STATUS_COMPLETED)
        self.assertEqual(rules.normalize_job_status("succeeded"), rules.STATUS_COMPLETED)
        self.assertEqual(rules.normalize_job_status("partially_succeeded"), rules.STATUS_COMPLETED)
        self.assertEqual(
            rules.transition_job_status(rules.STATUS_RUNNING, "succeeded"),
            rules.STATUS_COMPLETED,
        )

    def test_summarizes_partially_succeeded_job(self) -> None:
        self.assertEqual(
            rules.summarize_job(requested=5, completed=3, failed=2),
            {"status": rules.STATUS_COMPLETED, "pending": 0},
        )

    def test_summarizes_fully_succeeded_job(self) -> None:
        self.assertEqual(
            rules.summarize_job(requested=3, completed=3, failed=0),
            {"status": rules.STATUS_COMPLETED, "pending": 0},
        )

    def test_summarizes_fully_failed_job(self) -> None:
        self.assertEqual(
            rules.summarize_job(requested=3, completed=0, failed=3),
            {"status": rules.STATUS_FAILED, "pending": 0},
        )

    def test_summarizes_running_job_and_pending_count(self) -> None:
        self.assertEqual(
            rules.summarize_job(requested=5, completed=2, failed=1),
            {"status": rules.STATUS_RUNNING, "pending": 2},
        )

    def test_summarizes_canceled_job(self) -> None:
        self.assertEqual(
            rules.summarize_job(requested=5, completed=2, failed=0, canceled=3),
            {"status": rules.STATUS_CANCELED, "pending": 0},
        )

    def test_rejects_invalid_summary_counts(self) -> None:
        with self.assertRaises(ValueError):
            rules.summarize_job(requested=2, completed=2, failed=1)

        with self.assertRaises(ValueError):
            rules.summarize_job(requested=-1, completed=0, failed=0)

        with self.assertRaises(TypeError):
            rules.summarize_job(requested=1, completed=True, failed=0)

    def test_resolves_queue_limits_and_admission(self) -> None:
        limits = rules.resolve_queue_limits(
            worker_count=3,
            max_pending_jobs=5,
            stale_after_seconds=10,
            timeout_seconds=30,
        )

        self.assertEqual(limits.worker_count, 3)
        self.assertEqual(limits.max_pending_jobs, 5)

        allowed = rules.evaluate_queue_admission(
            queued_count=2,
            running_count=2,
            limits=limits,
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.pending_count, 4)

        denied = rules.evaluate_queue_admission(
            queued_count=3,
            running_count=2,
            limits=limits,
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "max_pending_jobs_exceeded")

    def test_rejects_invalid_queue_limits(self) -> None:
        with self.assertRaises(ValueError):
            rules.resolve_queue_limits(worker_count=0)

        with self.assertRaises(ValueError):
            rules.resolve_queue_limits(stale_after_seconds=31, timeout_seconds=30)

    def test_inspects_running_job_stale_and_timeout(self) -> None:
        started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        updated_at = started_at + timedelta(seconds=3)
        limits = rules.resolve_queue_limits(
            stale_after_seconds=5,
            timeout_seconds=20,
        )

        stale = rules.inspect_job_timing(
            status=rules.STATUS_RUNNING,
            created_at=started_at,
            started_at=started_at,
            updated_at=updated_at,
            now=started_at + timedelta(seconds=9),
            limits=limits,
        )
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["timed_out"])
        self.assertEqual(stale["reason"], "stale")

        timed_out = rules.inspect_job_timing(
            status=rules.STATUS_RUNNING,
            created_at=started_at,
            started_at=started_at,
            updated_at=updated_at,
            now=started_at + timedelta(seconds=21),
            limits=limits,
        )
        self.assertTrue(timed_out["timed_out"])
        self.assertEqual(timed_out["reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
