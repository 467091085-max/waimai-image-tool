from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from shared.product_generation_settlement import GenerationCompletion
from shared.redis_queue import TaskNotFound
from worker import product_settlement_reconciler as reconciler


DIGEST = "a" * 64


def candidate(**overrides: Any) -> dict[str, Any]:
    value = {
        "job_id": "job-1",
        "owner_user_id": "user-1",
        "request_sha256": DIGEST,
        "job_status": "running",
        "fence": 3,
        "cancel_requested": False,
        "settlement_status": "pending",
        "settlement_version": 0,
        "outbox_status": "published",
    }
    value.update(overrides)
    return value


def completion() -> GenerationCompletion:
    return GenerationCompletion(
        job_id="job-1",
        owner_user_id="user-1",
        request_sha256=DIGEST,
        fence=3,
        terminal_status="succeeded",
        requested_count=3,
        completed_count=2,
        failed_count=1,
        refund_target_points=10,
        manifest_ref="generated/manifests/job-1/manifest.json",
        manifest_sha256="b" * 64,
        error_message="",
    )


class FakeStore:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = candidates
        self.requeued: list[dict[str, Any]] = []

    def list_reconciliation_candidates(self, *, limit: int) -> list[dict[str, Any]]:
        assert limit > 0
        return self.candidates

    def requeue_published_outbox_if_task_missing(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.requeued.append(kwargs)
        return {"status": "pending"}


class FakeQueue:
    def __init__(self, tasks: dict[str, dict[str, Any]] | None = None) -> None:
        self.tasks = tasks or {}
        self.heartbeats: list[dict[str, Any]] = []

    def get(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.tasks:
            raise TaskNotFound(job_id)
        return self.tasks[job_id]

    def publish_service_heartbeat(self, **kwargs: Any) -> None:
        self.heartbeats.append(kwargs)


def test_terminal_redis_task_is_completed_and_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([candidate()])
    queue = FakeQueue({"job-1": {"status": "done"}})
    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reconciler,
        "completion_from_redis_task",
        lambda _task, object_store=None: completion(),
    )
    monkeypatch.setattr(
        reconciler,
        "apply_generation_completion",
        lambda _store, value, **_kwargs: applied.append(
            (value.job_id, value.owner_user_id)
        ),
    )

    report = reconciler.reconcile_once(
        store,
        queue,
        reconciler_id="reconciler-1",
    )

    assert report["completed"] == 1
    assert report["settled"] == 1
    assert report["failed"] == 0
    assert applied == [("job-1", "user-1")]


def test_revision_task_uses_revision_completion_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([candidate()])
    queue = FakeQueue(
        {
            "job-1": {
                "status": "done",
                "payload": {"taskType": "product_revision"},
            }
        }
    )
    applied: list[str] = []
    monkeypatch.setattr(
        reconciler,
        "revision_completion_from_redis_task",
        lambda _task, object_store=None: completion(),
    )
    monkeypatch.setattr(
        reconciler,
        "completion_from_redis_task",
        lambda *_args, **_kwargs: pytest.fail(
            "revision task must not use batch parser"
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "apply_generation_completion",
        lambda _store, value, **_kwargs: applied.append(value.job_id),
    )

    report = reconciler.reconcile_once(
        store,
        queue,
        reconciler_id="reconciler-1",
    )

    assert report["completed"] == 1
    assert report["failed"] == 0
    assert applied == ["job-1"]


def test_active_task_is_left_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([candidate()])
    queue = FakeQueue({"job-1": {"status": "running"}})
    monkeypatch.setattr(
        reconciler,
        "completion_from_redis_task",
        lambda _task, object_store=None: None,
    )

    report = reconciler.reconcile_once(
        store,
        queue,
        reconciler_id="reconciler-1",
    )

    assert report["active"] == 1
    assert report["completed"] == 0


def test_published_job_with_missing_task_requeues_exact_fence() -> None:
    store = FakeStore([candidate()])

    report = reconciler.reconcile_once(
        store,
        FakeQueue(),
        reconciler_id="reconciler-1",
    )

    assert report["requeuedOutbox"] == 1
    assert store.requeued == [
        {
            "job_id": "job-1",
            "expected_fence": 3,
        }
    ]


def test_pending_outbox_without_task_waits_for_dispatcher() -> None:
    store = FakeStore(
        [
            candidate(
                job_status="queued",
                fence=0,
                outbox_status="pending",
            )
        ]
    )

    report = reconciler.reconcile_once(
        store,
        FakeQueue(),
        reconciler_id="reconciler-1",
    )

    assert report["waitingOutbox"] == 1
    assert report["failed"] == 0


def test_terminal_database_job_settles_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore(
        [
            candidate(
                job_status="canceled",
                fence=0,
                outbox_status="canceled",
            )
        ]
    )
    settled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reconciler,
        "settle_terminal_job",
        lambda _store, **kwargs: settled.append(
            (kwargs["job_id"], kwargs["owner_user_id"])
        ),
    )

    report = reconciler.reconcile_once(
        store,
        FakeQueue(),
        reconciler_id="reconciler-1",
    )

    assert report["settled"] == 1
    assert settled == [("job-1", "user-1")]


def test_one_bad_candidate_does_not_block_other_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore(
        [
            candidate(job_id="bad", request_sha256="invalid"),
            candidate(job_id="job-1"),
        ]
    )
    queue = FakeQueue({"job-1": {"status": "done"}})
    monkeypatch.setattr(
        reconciler,
        "completion_from_redis_task",
        lambda _task, object_store=None: completion(),
    )
    monkeypatch.setattr(
        reconciler,
        "apply_generation_completion",
        lambda *_args, **_kwargs: None,
    )

    report = reconciler.reconcile_once(
        store,
        queue,
        reconciler_id="reconciler-1",
    )

    assert report["failed"] == 1
    assert report["completed"] == 1
    assert report["failures"] == [
        {
            "jobId": "bad",
            "errorType": "ProductSettlementReconcilerError",
        }
    ]


def test_loop_publishes_liveness_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore([])
    queue = FakeQueue()
    stop_event = Event()
    waits: list[float] = []

    def wait(delay: float) -> bool:
        waits.append(delay)
        stop_event.set()
        return True

    reconciler.run_reconciler_loop(
        store_factory=lambda: store,
        queue_factory=lambda: queue,
        stop_event=stop_event,
        reconciler_id="reconciler-1",
        wait=wait,
    )

    assert waits == [1.0]
    assert queue.heartbeats == [
        {
            "service_id": reconciler.RECONCILER_SERVICE_ID,
            "instance_id": "reconciler-1",
            "ttl_seconds": 30,
        }
    ]


def test_store_factory_wraps_postgres_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconciler,
        "postgres_config_from_env",
        lambda _env: SimpleNamespace(),
    )
    monkeypatch.setattr(
        reconciler,
        "connect_postgres",
        lambda _config: (_ for _ in ()).throw(
            reconciler.PostgresRuntimeError("postgres_connection_failed")
        ),
    )

    with pytest.raises(reconciler.ProductSettlementReconcilerError):
        reconciler.product_job_store_from_env(
            {"DATABASE_URL": "postgresql://redacted"}
        )
