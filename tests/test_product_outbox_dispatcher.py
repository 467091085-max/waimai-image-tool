"""Deterministic fake-store/queue tests; these are not real PG/Redis integration."""

from __future__ import annotations

import copy
import json
from threading import Event
from typing import Any

import pytest

from shared.batch_contract import menu_batch_contract_attestation
from shared.product_job_store import OutboxClaimLost
from shared.refinement_contract import revision_contract_attestation
from shared.redis_queue import (
    IdempotencyConflict,
    QueueError,
    RedisQueueConfig,
)
from worker.outbox_dispatcher import (
    DispatchBatchError,
    InvalidOutboxClaim,
    OutboxDispatcherError,
    dispatcher_heartbeat_key,
    dispatch_once,
    read_dispatcher_heartbeat,
    run_dispatch_loop,
    write_dispatcher_heartbeat,
)
from worker.worker import GenerationWorker


DIGEST = "a" * 64
TEST_ATTESTATION_SECRET = "outbox-dispatch-signing-secret-32-bytes-minimum"


@pytest.fixture(autouse=True)
def _contract_attestation_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECT_SIGNING_SECRET", TEST_ATTESTATION_SECRET)


def frozen_request(
    job_type: str = "menu_batch_generation",
) -> dict[str, Any]:
    request = {
        "schemaVersion": 1,
        "jobType": job_type,
        "jobId": "job-1",
        "userId": "user-1",
        "idempotency": {
            "key": "idem-1",
            "requestSha256": DIGEST,
        },
        "frozen": {"dish": "牛肉饭"},
    }
    attestation = (
        menu_batch_contract_attestation
        if job_type == "menu_batch_generation"
        else revision_contract_attestation
    )
    request["contractAttestation"] = attestation(
        request,
        TEST_ATTESTATION_SECRET,
    )
    return request


def outbox_claim(
    job_type: str = "menu_batch_generation",
    **overrides: Any,
) -> dict[str, Any]:
    request = frozen_request(job_type)
    claim = {
        "outbox_id": "outbox-1",
        "job_id": "job-1",
        "payload": {
            "jobId": "job-1",
            "ownerUserId": "user-1",
            "requestSha256": DIGEST,
            "request": request,
        },
        "owner_user_id": "user-1",
        "request_sha256": DIGEST,
        "fence": 7,
        "claim_token": "claim-1",
    }
    claim.update(overrides)
    return claim


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(
        self,
        claims: list[dict[str, Any]] | None = None,
        *,
        claim_error: BaseException | None = None,
        publish_error: BaseException | None = None,
    ) -> None:
        self.claims = list(claims or [])
        self.claim_error = claim_error
        self.publish_error = publish_error
        self.claim_calls: list[dict[str, Any]] = []
        self.published: list[dict[str, str]] = []
        self.connection = FakeConnection()

    def claim_outbox(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.claim_calls.append(dict(kwargs))
        if self.claim_error is not None:
            raise self.claim_error
        claims = self.claims
        self.claims = []
        return claims

    def mark_outbox_published(
        self,
        *,
        outbox_id: str,
        claim_token: str,
    ) -> dict[str, str]:
        if self.publish_error is not None:
            raise self.publish_error
        published = {
            "id": outbox_id,
            "status": "published",
            "claim_token": claim_token,
        }
        self.published.append(
            {"outbox_id": outbox_id, "claim_token": claim_token}
        )
        return published


class FakeHeartbeatRedis:
    def __init__(
        self,
        *,
        compensation_task: dict[str, Any] | None = None,
    ) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.compensation_task = copy.deepcopy(compensation_task)
        self.compensation_calls: list[dict[str, Any]] = []
        self.provider_queued = compensation_task is not None

    def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        self.ttls[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def ttl(self, key: str) -> int:
        if key not in self.values:
            return -2
        return self.ttls.get(key, -1)

    def eval(self, script: str, numkeys: int, *values: Any) -> int:
        assert "WAIMAI_CANCEL_EXACT_ACCEPTED_TASK" in script
        assert numkeys == 4
        keys = list(values[:numkeys])
        arguments = [str(value) for value in values[numkeys:]]
        self.compensation_calls.append(
            {"keys": keys, "arguments": arguments}
        )
        task = self.compensation_task
        if task is None:
            return -1
        expected_payload = json.dumps(
            task.get("payload") or {},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if (
            str(task.get("task_id") or "") != arguments[0]
            or str(task.get("owner_user_id") or "") != arguments[1]
            or str(task.get("request_sha256") or "") != arguments[2]
            or expected_payload != arguments[3]
        ):
            return 0
        status = str(task.get("status") or "")
        if status in {"done", "failed"}:
            return 2
        if status == "running":
            task["cancel_requested"] = True
            task["error"] = "cancel_requested"
            return 3
        if status != "pending":
            return -5
        task["status"] = "failed"
        task["cancel_requested"] = True
        task["error"] = "canceled"
        task["result"] = {"canceled": True}
        self.provider_queued = False
        return 1

    def provider_ready(self) -> bool:
        task = self.compensation_task or {}
        return (
            self.provider_queued
            and task.get("status") == "pending"
            and task.get("cancel_requested") is not True
        )


class FakeQueue:
    def __init__(
        self,
        *,
        enqueue_error: BaseException | None = None,
        enqueue_task: dict[str, Any] | None = None,
        get_task: dict[str, Any] | None = None,
        get_error: BaseException | None = None,
        compensation_task: dict[str, Any] | None = None,
    ) -> None:
        self.enqueue_error = enqueue_error
        self.enqueue_task = enqueue_task
        self.get_task = get_task
        self.get_error = get_error
        self.compensation_task_overridden = compensation_task is not None
        self.enqueue_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.redis = FakeHeartbeatRedis(
            compensation_task=compensation_task,
        )
        self.config = RedisQueueConfig(queue_name="product-generate")

    def enqueue_idempotent(
        self,
        payload: dict[str, Any],
        *,
        user_id: str,
        idempotency_key: str,
        request_sha256: str,
        task_id: str,
    ) -> dict[str, Any]:
        call = {
            "payload": copy.deepcopy(payload),
            "user_id": user_id,
            "idempotency_key": idempotency_key,
            "request_sha256": request_sha256,
            "task_id": task_id,
        }
        self.enqueue_calls.append(call)
        if self.enqueue_error is not None:
            raise self.enqueue_error
        task = self.enqueue_task or accepted_task(call)
        if not self.compensation_task_overridden:
            self.redis.compensation_task = copy.deepcopy(task)
            self.redis.provider_queued = True
        return task

    def get(self, task_id: str) -> dict[str, Any]:
        self.get_calls.append(task_id)
        if self.get_error is not None:
            raise self.get_error
        if self.get_task is None:
            raise QueueError("missing fake task")
        return copy.deepcopy(self.get_task)

    def recover_expired_claims(self, **_kwargs: Any) -> dict[str, int]:
        return {"recovered": 0, "failed": 0, "cleaned": 0}

    def recover_stale_running(self, **_kwargs: Any) -> dict[str, int]:
        return {"recovered": 0, "failed": 0}

    def claim(self, **_kwargs: Any) -> None:
        if self.redis.provider_ready():
            raise AssertionError("canceled task remained provider-ready")
        return None


def accepted_task(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": call["task_id"],
        "owner_user_id": call["user_id"],
        "request_sha256": call["request_sha256"],
        "payload": copy.deepcopy(call["payload"]),
        "status": "pending",
    }


def expected_task(
    *,
    job_type: str = "menu_batch_generation",
    fence: int = 7,
    outbox_id: str = "outbox-1",
) -> dict[str, Any]:
    contract_key = (
        "batchContract"
        if job_type == "menu_batch_generation"
        else "revisionContract"
    )
    task_type = (
        "product_batch"
        if job_type == "menu_batch_generation"
        else "product_revision"
    )
    return {
        "task_id": "job-1",
        "owner_user_id": "user-1",
        "request_sha256": DIGEST,
        "payload": {
            "taskType": task_type,
            contract_key: frozen_request(job_type),
            "_productJobFence": fence,
            "_productOutboxId": outbox_id,
        },
        "status": "pending",
    }


@pytest.mark.parametrize(
    ("job_type", "task_type", "contract_key"),
    [
        ("menu_batch_generation", "product_batch", "batchContract"),
        (
            "delivery_asset_revision_batch",
            "product_revision",
            "revisionContract",
        ),
    ],
)
def test_dispatches_supported_contract_without_mutating_digest(
    job_type: str,
    task_type: str,
    contract_key: str,
) -> None:
    claim = outbox_claim(job_type)
    original_request = copy.deepcopy(claim["payload"]["request"])
    store = FakeStore([claim])
    queue = FakeQueue()

    report = dispatch_once(
        store,
        queue,
        dispatcher_id="dispatcher-1",
        limit=5,
        lease_seconds=90,
    )

    assert report["claimed"] == 1
    assert report["published"] == 1
    assert report["recovered"] == 0
    assert store.published == [
        {"outbox_id": "outbox-1", "claim_token": "claim-1"}
    ]
    call = queue.enqueue_calls[0]
    assert call["user_id"] == "user-1"
    assert call["idempotency_key"] == "idem-1"
    assert call["request_sha256"] == DIGEST
    assert call["task_id"] == "job-1"
    assert call["payload"]["taskType"] == task_type
    assert call["payload"][contract_key] == original_request
    assert call["payload"]["_productJobFence"] == 7
    assert call["payload"]["_productOutboxId"] == "outbox-1"
    assert claim["payload"]["request"] == original_request
    assert store.claim_calls[0]["limit"] == 5
    assert store.claim_calls[0]["lease_seconds"] == 90
    assert store.claim_calls[0]["claim_token"]


def test_revision_outbox_is_published_only_to_dedicated_revision_queue() -> None:
    store = FakeStore([outbox_claim("delivery_asset_revision_batch")])
    product_queue = FakeQueue()
    revision_queue = FakeQueue()
    revision_queue.config = RedisQueueConfig(queue_name="product-revision")

    report = dispatch_once(
        store,
        product_queue,
        revision_queue=revision_queue,
        dispatcher_id="dispatcher-1",
    )

    assert report["published"] == 1
    assert product_queue.enqueue_calls == []
    assert len(revision_queue.enqueue_calls) == 1
    assert (
        revision_queue.enqueue_calls[0]["payload"]["taskType"]
        == "product_revision"
    )


def test_unknown_job_type_fails_closed_without_enqueue_or_publish() -> None:
    store = FakeStore([outbox_claim("unknown_batch")])
    queue = FakeQueue()

    with pytest.raises(DispatchBatchError) as raised:
        dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert raised.value.report["published"] == 0
    assert queue.enqueue_calls == []
    assert store.published == []


@pytest.mark.parametrize(
    "job_type",
    ["menu_batch_generation", "delivery_asset_revision_batch"],
)
def test_tampered_attestation_never_reaches_redis_or_publish(
    job_type: str,
) -> None:
    claim = outbox_claim(job_type)
    request = claim["payload"]["request"]
    request["frozen"]["dish"] = "伪造菜品"
    forged_digest = "b" * 64
    request["idempotency"]["requestSha256"] = forged_digest
    claim["payload"]["requestSha256"] = forged_digest
    claim["request_sha256"] = forged_digest
    store = FakeStore([claim])
    queue = FakeQueue()

    with pytest.raises(DispatchBatchError) as raised:
        dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert raised.value.report["published"] == 0
    assert queue.enqueue_calls == []
    assert store.published == []


def test_dispatch_batch_limit_is_strictly_bounded() -> None:
    store = FakeStore([])
    queue = FakeQueue()

    with pytest.raises(InvalidOutboxClaim, match="must not exceed 100"):
        dispatch_once(
            store,
            queue,
            dispatcher_id="dispatcher-1",
            limit=101,
        )

    assert store.claim_calls == []


def test_timeout_after_success_verifies_exact_task_then_publishes() -> None:
    store = FakeStore([outbox_claim()])
    queue = FakeQueue(
        enqueue_error=QueueError("timeout after Redis accepted the script"),
        get_task=expected_task(),
    )

    report = dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert report["published"] == 1
    assert report["recovered"] == 1
    assert queue.get_calls == ["job-1"]
    assert store.published[0]["claim_token"] == "claim-1"
    assert queue.redis.compensation_calls == []


def test_raw_redis_timeout_after_success_is_also_verified() -> None:
    class RawRedisTimeout(Exception):
        pass

    store = FakeStore([outbox_claim()])
    queue = FakeQueue(
        enqueue_error=RawRedisTimeout("socket timed out after EVAL"),
        get_task=expected_task(),
    )

    report = dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert report["published"] == 1
    assert report["recovered"] == 1
    assert queue.get_calls == ["job-1"]


@pytest.mark.parametrize(
    "existing_task",
    [
        expected_task(fence=6),
        expected_task(fence=8),
        expected_task(outbox_id="outbox-old"),
    ],
)
def test_timeout_recovery_rejects_forged_or_old_fence_and_outbox(
    existing_task: dict[str, Any],
) -> None:
    store = FakeStore([outbox_claim()])
    queue = FakeQueue(
        enqueue_error=QueueError("timeout"),
        get_task=existing_task,
    )

    with pytest.raises(DispatchBatchError) as raised:
        dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert raised.value.report["published"] == 0
    assert store.published == []


def test_idempotency_conflict_never_checks_other_task_or_publishes() -> None:
    store = FakeStore([outbox_claim()])
    queue = FakeQueue(
        enqueue_error=IdempotencyConflict(
            task_id="other-job",
            request_sha256=DIGEST,
        )
    )

    with pytest.raises(DispatchBatchError):
        dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert queue.get_calls == []
    assert store.published == []


def test_claim_lost_after_enqueue_cancels_exact_task_and_isolates_record() -> None:
    store = FakeStore(
        [outbox_claim()],
        publish_error=OutboxClaimLost("claim expired"),
    )
    queue = FakeQueue()

    report = dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert report["claimed"] == 1
    assert report["published"] == 0
    assert report["claimLost"] == 1
    assert report["results"][0]["status"] == "publish_claim_lost"
    assert report["results"][0]["compensationStatus"] == "canceled"
    assert store.published == []
    assert len(queue.redis.compensation_calls) == 1
    assert queue.redis.compensation_task is not None
    assert queue.redis.compensation_task["status"] == "failed"
    assert queue.redis.compensation_task["cancel_requested"] is True
    assert queue.redis.provider_ready() is False
    provider_calls: list[dict[str, Any]] = []
    worker = GenerationWorker(
        queue,
        handler=lambda payload: provider_calls.append(dict(payload)) or {},
    )
    assert worker.process_one(timeout_seconds=0) is False
    assert provider_calls == []


@pytest.mark.parametrize(
    "different_task",
    [
        {**expected_task(), "task_id": "other-job"},
        {**expected_task(), "owner_user_id": "other-user"},
        {**expected_task(), "request_sha256": "b" * 64},
        expected_task(fence=8),
    ],
    ids=["job-id", "owner", "request-sha", "fence"],
)
def test_claim_lost_never_cancels_nonmatching_task(
    different_task: dict[str, Any],
) -> None:
    store = FakeStore(
        [outbox_claim()],
        publish_error=OutboxClaimLost("job canceled"),
    )
    queue = FakeQueue(compensation_task=different_task)
    before = copy.deepcopy(queue.redis.compensation_task)

    report = dispatch_once(store, queue, dispatcher_id="dispatcher-1")

    assert report["published"] == 0
    assert report["claimLost"] == 1
    assert report["results"][0]["compensationStatus"] == "identity_mismatch"
    assert queue.redis.compensation_task == before
    assert queue.redis.provider_ready() is True


def test_dispatcher_heartbeat_uses_product_hash_tag_ttl_and_read_contract() -> None:
    queue = FakeQueue()

    document = write_dispatcher_heartbeat(
        queue,
        dispatcher_id="dispatcher-1",
        status="running",
        ttl_seconds=45,
        report={"claimed": 2, "published": 1, "recovered": 1},
        now=lambda: 1234.9,
    )

    key = dispatcher_heartbeat_key(queue, "dispatcher-1")
    assert key == queue.config.service_heartbeat_key(
        "product-outbox-dispatcher"
    )
    assert queue.redis.ttls[key] == 45
    assert document["role"] == "product-outbox-dispatcher"
    assert document["serviceId"] == "product-outbox-dispatcher"
    assert document["instanceId"] == "dispatcher-1"
    assert document["queueName"] == "product-generate"
    assert document["observedAtUnix"] == 1234
    assert read_dispatcher_heartbeat(queue, "dispatcher-1") == {
        **document,
        "ttlSeconds": 45,
    }


def test_loop_reconnects_with_exponential_backoff_and_stops_cooperatively() -> None:
    first = FakeStore(claim_error=ConnectionError("database unavailable"))
    second = FakeStore(claim_error=ConnectionError("database unavailable"))
    third = FakeStore([])
    stores = iter([first, second, third])
    queues: list[FakeQueue] = []
    stop_event = Event()
    waits: list[float] = []

    def queue_factory() -> FakeQueue:
        queue = FakeQueue()
        queues.append(queue)
        return queue

    def wait(delay: float) -> bool:
        waits.append(delay)
        if len(waits) == 3:
            stop_event.set()
        return stop_event.is_set()

    run_dispatch_loop(
        store_factory=lambda: next(stores),
        queue_factory=queue_factory,
        stop_event=stop_event,
        dispatcher_id="dispatcher-loop",
        idle_seconds=1.25,
        initial_backoff_seconds=0.2,
        max_backoff_seconds=2.0,
        heartbeat_ttl_seconds=0,
        wait=wait,
    )

    assert waits == [0.2, 0.4, 1.25]
    assert len(queues) == 3
    assert first.connection.closed is True
    assert second.connection.closed is True
    assert third.connection.closed is True


def test_loop_rejects_zero_idle_delay_instead_of_busy_polling() -> None:
    with pytest.raises(
        OutboxDispatcherError,
        match="idle_seconds must be positive",
    ):
        run_dispatch_loop(
            store_factory=lambda: FakeStore([]),
            queue_factory=FakeQueue,
            stop_event=Event(),
            dispatcher_id="dispatcher-loop",
            idle_seconds=0,
            heartbeat_ttl_seconds=0,
        )
