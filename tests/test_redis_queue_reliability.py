from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from shared.redis_queue import (
    CancellationRequested,
    IdempotencyConflict,
    LeaseLost,
    QueueError,
    RedisQueueConfig,
    RedisTaskQueue,
    public_task_payload,
)
from tests.redis_test_double import RedisTestDouble


class AtomicFakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.lock = threading.RLock()

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        with self.lock:
            self.hashes.setdefault(key, {}).update(
                {str(field): str(value) for field, value in mapping.items()}
            )

    def hgetall(self, key: str) -> dict[str, str]:
        with self.lock:
            return dict(self.hashes.get(key, {}))

    def rpush(self, key: str, value: str) -> None:
        with self.lock:
            self.lists.setdefault(key, []).append(value)

    def rpoplpush(self, source: str, destination: str) -> str | None:
        with self.lock:
            values = self.lists.setdefault(source, [])
            if not values:
                return None
            value = values.pop()
            self.lists.setdefault(destination, []).insert(0, value)
            return value

    def brpoplpush(
        self,
        source: str,
        destination: str,
        timeout: int = 0,
    ) -> str | None:
        return self.rpoplpush(source, destination)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        with self.lock:
            values = self.lists.setdefault(key, [])
            resolved_end = len(values) if end < 0 else end + 1
            return list(values[start:resolved_end])

    def lrem(self, key: str, count: int, value: str) -> int:
        with self.lock:
            values = self.lists.setdefault(key, [])
            removed = 0
            index = 0
            while index < len(values) and (count == 0 or removed < count):
                if values[index] == value:
                    values.pop(index)
                    removed += 1
                else:
                    index += 1
            return removed

    def scan_iter(self, match: str, count: int = 10):
        prefix = match.rstrip("*")
        with self.lock:
            matching = [
                key for key in self.hashes.keys() if key.startswith(prefix)
            ]
        yield from matching[:count]

    def eval(self, _script: str, key_count: int, *values: str) -> list[Any]:
        keys = values[:key_count]
        args = values[key_count:]
        with self.lock:
            if "WAIMAI_CLAIM_RECEIPT" in _script:
                task = self.hashes.get(keys[0])
                if task is None:
                    self.lrem(keys[1], 1, args[0])
                    return [-2, 0]
                if task.get("status") in {"done", "failed"}:
                    self.lrem(keys[1], 1, args[0])
                    return [-1, int(task.get("attempts") or 0)]
                if task.get("status") != "pending":
                    self.lrem(keys[1], 1, args[0])
                    return [-3, int(task.get("attempts") or 0)]
                attempts = int(task.get("attempts") or 0) + 1
                task.update(
                    {
                        "status": "running",
                        "attempts": str(attempts),
                        "worker_id": args[1],
                        "lease_token": args[2],
                        "updated_at": args[3],
                        "lease_expires_at": args[4],
                    }
                )
                if not task.get("started_at"):
                    task["started_at"] = args[3]
                return [1, attempts]
            if "WAIMAI_HEARTBEAT" in _script:
                task = self.hashes.get(keys[0], {})
                if (
                    task.get("status") != "running"
                    or task.get("lease_token") != args[0]
                ):
                    return 0
                task["updated_at"] = args[1]
                task["lease_expires_at"] = args[2]
                if args[3]:
                    task["attempts"] = args[3]
                if args[4]:
                    task["error"] = args[4]
                return 1
            if "WAIMAI_FINISH_CLAIM" in _script:
                task = self.hashes.get(keys[0], {})
                if (
                    task.get("status") != "running"
                    or task.get("lease_token") != args[1]
                ):
                    return 0
                task.update(
                    {
                        "status": args[2],
                        "image_url": args[3],
                        "error": args[4],
                        "result_json": args[5],
                        "updated_at": args[7],
                        "finished_at": args[7],
                        "worker_id": "",
                        "lease_token": "",
                        "lease_expires_at": "",
                    }
                )
                if args[6]:
                    task["attempts"] = args[6]
                self.lrem(keys[1], 1, args[0])
                return 1
            if "WAIMAI_RECOVER_RECEIPT" in _script:
                task = self.hashes.get(keys[0])
                if task is None:
                    self.lrem(keys[1], 1, args[0])
                    return 4
                if task.get("status") in {"done", "failed"}:
                    self.lrem(keys[1], 1, args[0])
                    return 3
                lease_expires_at = int(task.get("lease_expires_at") or 0)
                if task.get("status") == "running" and lease_expires_at > int(
                    args[1]
                ):
                    return 0
                if int(task.get("attempts") or 0) >= int(args[2]):
                    task.update(
                        {
                            "status": "failed",
                            "error": "task lease expired after maximum attempts",
                            "updated_at": args[1],
                            "finished_at": args[1],
                            "worker_id": "",
                            "lease_token": "",
                            "lease_expires_at": "",
                        }
                    )
                    self.lrem(keys[1], 1, args[0])
                    return 2
                task.update(
                    {
                        "status": "pending",
                        "error": "recovered expired task lease",
                        "updated_at": args[1],
                        "worker_id": "",
                        "lease_token": "",
                        "lease_expires_at": "",
                    }
                )
                self.lrem(keys[1], 1, args[0])
                self.rpush(keys[2], args[0])
                return 1
            assert key_count == 3
            task_key, queue_key, claim_key = keys
            task_id = args[0]
            request_sha256 = args[12]
            existing = self.strings.get(claim_key)
            if existing is not None:
                separator = existing.find(":")
                if separator < 0:
                    return [-2, existing]
                existing_sha = existing[:separator]
                existing_task_id = existing[separator + 1 :]
                if existing_sha == request_sha256:
                    return [0, existing_task_id]
                return [-1, existing_task_id]
            if task_key in self.hashes:
                return [-3, task_id]
            fields = (
                "task_id",
                "status",
                "image_url",
                "error",
                "attempts",
                "payload_json",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "owner_user_id",
                "idempotency_key_hash",
                "request_sha256",
            )
            self.hashes[task_key] = {
                field: str(value) for field, value in zip(fields, args[:13])
            }
            self.lists.setdefault(queue_key, []).append(str(args[13]))
            self.strings[claim_key] = f"{request_sha256}:{task_id}"
            return [1, task_id]


def queue() -> RedisTaskQueue:
    return RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(namespace="test", queue_name="generate"),
    )


def test_idempotent_enqueue_reuses_the_original_task() -> None:
    task_queue = queue()
    digest = "a" * 64

    first = task_queue.enqueue_idempotent(
        {"batch": "frozen"},
        user_id="user-1",
        idempotency_key="checkout-123",
        request_sha256=digest,
    )
    second = task_queue.enqueue_idempotent(
        {"batch": "frozen"},
        user_id="user-1",
        idempotency_key="checkout-123",
        request_sha256=digest.upper(),
    )

    assert second["task_id"] == first["task_id"]
    assert second["owner_user_id"] == "user-1"
    assert second["request_sha256"] == digest
    assert len(task_queue.redis.lists[task_queue.config.queue_key]) == 1


def test_idempotency_key_conflict_fails_closed() -> None:
    task_queue = queue()
    original = task_queue.enqueue_idempotent(
        {"batch": "one"},
        user_id="user-1",
        idempotency_key="same-key",
        request_sha256="1" * 64,
    )

    with pytest.raises(IdempotencyConflict) as captured:
        task_queue.enqueue_idempotent(
            {"batch": "two"},
            user_id="user-1",
            idempotency_key="same-key",
            request_sha256="2" * 64,
        )

    assert captured.value.task_id == original["task_id"]
    assert len(task_queue.redis.lists[task_queue.config.queue_key]) == 1


def test_same_idempotency_key_is_scoped_to_authenticated_user() -> None:
    task_queue = queue()
    first = task_queue.enqueue_idempotent(
        {"batch": "one"},
        user_id="user-1",
        idempotency_key="same-key",
        request_sha256="1" * 64,
    )
    second = task_queue.enqueue_idempotent(
        {"batch": "two"},
        user_id="user-2",
        idempotency_key="same-key",
        request_sha256="2" * 64,
    )

    assert first["task_id"] != second["task_id"]
    assert len(task_queue.redis.lists[task_queue.config.queue_key]) == 2


def test_concurrent_idempotent_enqueue_creates_one_task() -> None:
    task_queue = queue()

    def enqueue_once(_index: int) -> str:
        return task_queue.enqueue_idempotent(
            {"batch": "concurrent"},
            user_id="user-1",
            idempotency_key="concurrent-key",
            request_sha256="c" * 64,
        )["task_id"]

    with ThreadPoolExecutor(max_workers=12) as executor:
        task_ids = list(executor.map(enqueue_once, range(24)))

    assert len(set(task_ids)) == 1
    assert len(task_queue.redis.lists[task_queue.config.queue_key]) == 1
    assert len(task_queue.redis.hashes) == 1


def test_concurrent_claim_has_one_lease_owner() -> None:
    task_queue = queue()
    task_queue.enqueue({"batch": "claim-race"}, task_id="task-claim-race")

    def claim_once(worker_index: int):
        return task_queue.claim(
            worker_id=f"worker-{worker_index}",
            lease_ms=10_000,
            timeout_seconds=0,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(claim_once, range(8)))

    owned = [claim for claim in claims if claim is not None]
    assert len(owned) == 1
    assert owned[0]["task_id"] == "task-claim-race"
    assert len(task_queue.redis.lists[task_queue.config.processing_key]) == 1
    assert task_queue.redis.lists[task_queue.config.queue_key] == []


def test_queue_is_fifo() -> None:
    task_queue = queue()
    task_queue.enqueue({"sequence": 1}, task_id="task-first")
    task_queue.enqueue({"sequence": 2}, task_id="task-second")

    first = task_queue.claim(
        worker_id="worker-a",
        lease_ms=10_000,
        timeout_seconds=0,
    )
    assert first is not None
    task_queue.ack_done(
        first["task_id"],
        receipt=first["receipt"],
        lease_token=first["lease_token"],
        image_url="https://cdn.example/first.png",
    )
    second = task_queue.claim(
        worker_id="worker-a",
        lease_ms=10_000,
        timeout_seconds=0,
    )

    assert first["task_id"] == "task-first"
    assert second is not None
    assert second["task_id"] == "task-second"


def test_all_queue_keys_share_a_redis_cluster_hash_tag() -> None:
    config = RedisQueueConfig(namespace="test", queue_name="generate")
    keys = {
        config.queue_key,
        config.processing_key,
        config.dead_letter_key,
        config.task_key("task-1"),
        config.idempotency_claim_key("user-1", "request-1"),
    }

    tags = {
        key[key.index("{") + 1 : key.index("}")]
        for key in keys
    }

    assert len(tags) == 1


def test_wrong_type_queue_fails_before_task_creation() -> None:
    task_queue = queue()
    task_queue.redis.strings[task_queue.config.queue_key] = "wrong-type"

    with pytest.raises(QueueError):
        task_queue.enqueue({"batch": "must-not-partially-write"})

    assert task_queue.redis.hashes == {}


def test_wrong_type_idempotency_claim_fails_before_task_creation() -> None:
    task_queue = queue()
    claim_key = task_queue.config.idempotency_claim_key(
        "user-1",
        "request-1",
    )
    task_queue.redis.hashes[claim_key] = {"wrong": "type"}

    with pytest.raises(QueueError):
        task_queue.enqueue_idempotent(
            {"batch": "must-not-partially-write"},
            user_id="user-1",
            idempotency_key="request-1",
            request_sha256="a" * 64,
        )

    assert len(task_queue.redis.hashes) == 1
    assert claim_key in task_queue.redis.hashes


def test_internal_idempotency_fields_are_not_public() -> None:
    task_queue = queue()
    task = task_queue.enqueue_idempotent(
        {"batch": "private"},
        user_id="customer@example.com",
        idempotency_key="private-browser-key",
        request_sha256="f" * 64,
    )

    payload = public_task_payload(task)

    assert "owner_user_id" not in payload
    assert "idempotency_key_hash" not in payload
    assert "request_sha256" not in payload
    redis_keys = " ".join(task_queue.redis.strings.keys())
    assert "customer@example.com" not in redis_keys
    assert "private-browser-key" not in redis_keys


def test_claim_heartbeat_and_ack_require_the_lease_owner() -> None:
    task_queue = queue()
    task_queue.enqueue({"prompt": "牛肉饭"}, task_id="task-lease")

    claim = task_queue.claim(
        worker_id="worker-a",
        lease_ms=1_000,
        timeout_seconds=0,
    )

    assert claim is not None
    assert claim["attempts"] == 1
    assert task_queue.get("task-lease")["status"] == "running"
    assert len(task_queue.redis.lists[task_queue.config.processing_key]) == 1
    with pytest.raises(LeaseLost):
        task_queue.heartbeat(
            "task-lease",
            lease_token="wrong-token",
            lease_ms=1_000,
        )
    heartbeat = task_queue.heartbeat(
        "task-lease",
        lease_token=claim["lease_token"],
        lease_ms=2_000,
    )
    assert heartbeat["worker_id"] == "worker-a"

    done = task_queue.ack_done(
        "task-lease",
        receipt=claim["receipt"],
        lease_token=claim["lease_token"],
        image_url="https://cdn.example/task-lease.png",
    )

    assert done["status"] == "done"
    assert done["image_url"].endswith("task-lease.png")
    assert task_queue.redis.lists[task_queue.config.processing_key] == []
    with pytest.raises(LeaseLost):
        task_queue.ack_failed(
            "task-lease",
            receipt=claim["receipt"],
            lease_token=claim["lease_token"],
            error="late worker",
            attempts=1,
        )
    task_key = task_queue.config.task_key("task-lease")
    assert task_queue.redis.expirations[task_key] == task_queue.config.terminal_ttl_seconds


def test_cancel_pending_task_removes_receipt_and_expires_idempotency_claim() -> None:
    task_queue = queue()
    task = task_queue.enqueue_idempotent(
        {"taskType": "product_batch"},
        user_id="user-1",
        idempotency_key="cancel-pending",
        request_sha256="a" * 64,
        task_id="task-cancel-pending",
    )

    canceled = task_queue.cancel(task["task_id"])

    assert canceled["status"] == "failed"
    assert canceled["error"] == "canceled"
    assert canceled["result"] == {"canceled": True}
    assert task_queue.redis.lists[task_queue.config.queue_key] == []
    task_key = task_queue.config.task_key(task["task_id"])
    claim_key = task_queue.config.idempotency_claim_key(
        "user-1",
        "cancel-pending",
    )
    assert task_queue.redis.expirations[task_key] == task_queue.config.terminal_ttl_seconds
    assert task_queue.redis.expirations[claim_key] == task_queue.config.idempotency_ttl_seconds


def test_cancel_running_task_removes_processing_receipt_and_fences_worker() -> None:
    task_queue = queue()
    task_queue.enqueue({"taskType": "product_batch"}, task_id="task-cancel-running")
    claim = task_queue.claim(
        worker_id="worker-a",
        lease_ms=10_000,
        timeout_seconds=0,
    )
    assert claim is not None

    canceled = task_queue.cancel("task-cancel-running")

    assert canceled["status"] == "failed"
    assert canceled["result"] == {"canceled": True}
    assert task_queue.redis.lists[task_queue.config.processing_key] == []
    with pytest.raises(LeaseLost):
        task_queue.heartbeat(
            "task-cancel-running",
            lease_token=claim["lease_token"],
            lease_ms=10_000,
        )
    with pytest.raises(LeaseLost):
        task_queue.ack_done(
            "task-cancel-running",
            receipt=claim["receipt"],
            lease_token=claim["lease_token"],
            image_url="https://cdn.example/late.png",
        )


def test_request_cancel_running_preserves_lease_until_worker_acknowledges() -> None:
    task_queue = queue()
    task_queue.enqueue({"taskType": "product_batch"}, task_id="task-request-cancel")
    claim = task_queue.claim(
        worker_id="worker-a",
        lease_ms=10_000,
        timeout_seconds=0,
    )
    assert claim is not None

    requested = task_queue.request_cancel("task-request-cancel")

    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    assert requested["lease_token"] == claim["lease_token"]
    assert task_queue.redis.lists[task_queue.config.processing_key] == [
        claim["receipt"]
    ]
    heartbeat = task_queue.heartbeat(
        "task-request-cancel",
        lease_token=claim["lease_token"],
        lease_ms=10_000,
    )
    assert heartbeat["status"] == "running"
    with pytest.raises(CancellationRequested):
        task_queue.ack_done(
            "task-request-cancel",
            receipt=claim["receipt"],
            lease_token=claim["lease_token"],
            image_url="https://cdn.example/late.png",
        )

    canceled = task_queue.ack_canceled(
        "task-request-cancel",
        receipt=claim["receipt"],
        lease_token=claim["lease_token"],
        attempts=1,
    )
    assert canceled["status"] == "failed"
    assert canceled["result"] == {"canceled": True}
    assert task_queue.redis.lists[task_queue.config.processing_key] == []


def test_requested_cancel_becomes_terminal_when_lease_expires() -> None:
    task_queue = queue()
    task_queue.enqueue({"taskType": "product_batch"}, task_id="task-cancel-expired")
    claim = task_queue.claim(
        worker_id="worker-a",
        lease_ms=1_000,
        timeout_seconds=0,
    )
    assert claim is not None
    task_queue.request_cancel("task-cancel-expired")
    task_queue.redis.hashes[
        task_queue.config.task_key("task-cancel-expired")
    ]["lease_expires_at"] = "1"

    recovered = task_queue.recover_expired_claims(max_attempts=3)

    assert recovered["failed"] == ["task-cancel-expired"]
    task = task_queue.get("task-cancel-expired")
    assert task["status"] == "failed"
    assert task["result"] == {"canceled": True}
    assert task_queue.redis.lists[task_queue.config.processing_key] == []


def test_orphaned_processing_receipt_is_requeued() -> None:
    task_queue = queue()
    task_queue.enqueue({"prompt": "孤儿任务"}, task_id="task-orphan")
    receipt = task_queue.redis.rpoplpush(
        task_queue.config.queue_key,
        task_queue.config.processing_key,
    )
    assert receipt is not None

    recovered = task_queue.recover_expired_claims(max_attempts=3)

    assert recovered["recovered"] == ["task-orphan"]
    assert task_queue.get("task-orphan")["status"] == "pending"
    assert task_queue.redis.lists[task_queue.config.processing_key] == []
    assert task_queue.redis.lists[task_queue.config.queue_key] == [receipt]


def test_expired_claim_is_requeued_for_another_worker() -> None:
    task_queue = queue()
    task_queue.enqueue({"prompt": "恢复任务"}, task_id="task-recover")
    first = task_queue.claim(
        worker_id="worker-a",
        lease_ms=1_000,
        timeout_seconds=0,
    )
    assert first is not None
    task_queue.redis.hashes[
        task_queue.config.task_key("task-recover")
    ]["lease_expires_at"] = "1"

    recovered = task_queue.recover_expired_claims(max_attempts=3)
    second = task_queue.claim(
        worker_id="worker-b",
        lease_ms=1_000,
        timeout_seconds=0,
    )

    assert recovered["recovered"] == ["task-recover"]
    assert second is not None
    assert second["attempts"] == 2
    assert second["lease_token"] != first["lease_token"]
    with pytest.raises(LeaseLost):
        task_queue.ack_done(
            "task-recover",
            receipt=first["receipt"],
            lease_token=first["lease_token"],
            image_url="https://cdn.example/stale.png",
        )
    task_queue.ack_done(
        "task-recover",
        receipt=second["receipt"],
        lease_token=second["lease_token"],
        image_url="https://cdn.example/recovered.png",
    )
    assert task_queue.get("task-recover")["image_url"].endswith(
        "recovered.png"
    )


def test_expired_claim_fails_after_maximum_attempts() -> None:
    task_queue = queue()
    task_queue.enqueue({"prompt": "永久失败"}, task_id="task-max")
    claim = task_queue.claim(
        worker_id="worker-a",
        lease_ms=1_000,
        timeout_seconds=0,
    )
    assert claim is not None
    task_queue.redis.hashes[
        task_queue.config.task_key("task-max")
    ]["lease_expires_at"] = "1"

    recovered = task_queue.recover_expired_claims(max_attempts=1)

    assert recovered["failed"] == ["task-max"]
    task = task_queue.get("task-max")
    assert task["status"] == "failed"
    assert "maximum attempts" in task["error"]
    assert task_queue.redis.lists[task_queue.config.processing_key] == []


def test_legacy_recovery_does_not_steal_an_active_lease() -> None:
    task_queue = queue()
    task_queue.enqueue({"prompt": "运行中"}, task_id="task-active")
    claim = task_queue.claim(
        worker_id="worker-a",
        lease_ms=60_000,
        timeout_seconds=0,
    )
    assert claim is not None
    task_queue.redis.hashes[
        task_queue.config.task_key("task-active")
    ]["updated_at"] = "1"

    result = task_queue.recover_stale_running(
        stale_after_ms=0,
        max_attempts=3,
    )

    assert result == {"recovered": [], "failed": []}
    assert task_queue.get("task-active")["status"] == "running"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", ""),
        ("idempotency_key", ""),
        ("request_sha256", "not-a-digest"),
    ],
)
def test_idempotent_enqueue_rejects_invalid_identity(
    field: str,
    value: str,
) -> None:
    task_queue = queue()
    arguments = {
        "user_id": "user-1",
        "idempotency_key": "request-1",
        "request_sha256": "a" * 64,
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        task_queue.enqueue_idempotent({"batch": "frozen"}, **arguments)

    assert task_queue.redis.hashes == {}
    assert task_queue.redis.lists == {}


def test_service_heartbeat_has_ttl_and_round_trips_liveness() -> None:
    task_queue = queue()

    published = task_queue.publish_service_heartbeat(
        service_id="product-worker",
        instance_id="worker-a",
        ttl_seconds=17,
    )
    live = task_queue.service_liveness("product-worker")

    assert published["serviceId"] == "product-worker"
    assert published["instanceId"] == "worker-a"
    assert live is not None
    assert live["queueName"] == "generate"
    assert live["ttlSeconds"] == 17
    assert live["ageMs"] >= 0
    key = task_queue.config.service_heartbeat_key("product-worker")
    assert task_queue.redis.expirations[key] == 17


def test_service_liveness_rejects_corrupt_or_non_expiring_heartbeat() -> None:
    task_queue = queue()
    key = task_queue.config.service_heartbeat_key("product-worker")
    task_queue.redis.set(key, '{"serviceId":"other"}', ex=10)

    with pytest.raises(QueueError, match="corrupt service heartbeat"):
        task_queue.service_liveness("product-worker")

    task_queue.redis.set(
        key,
        (
            '{"instanceId":"worker-a","queueName":"generate",'
            '"reportedAtMs":1,"serviceId":"product-worker"}'
        ),
    )
    assert task_queue.service_liveness("product-worker") is None
