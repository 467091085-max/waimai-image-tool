from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from shared.redis_queue import RedisQueueConfig, RedisTaskQueue
from tests.redis_test_double import RedisTestDouble
from worker.worker import GenerationWorker


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def brpop(self, key: str, timeout: int = 0) -> tuple[str, str] | None:
        values = self.lists.setdefault(key, [])
        if not values:
            return None
        return key, values.pop()

    def rpoplpush(self, source: str, destination: str) -> str | None:
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
        values = self.lists.setdefault(key, [])
        resolved_end = len(values) if end < 0 else end + 1
        return list(values[start:resolved_end])

    def lrem(self, key: str, count: int, value: str) -> int:
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

    def eval(self, script: str, key_count: int, *values: str) -> Any:
        keys = values[:key_count]
        args = values[key_count:]
        if "WAIMAI_CLAIM_RECEIPT" in script:
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
        if "WAIMAI_HEARTBEAT" in script:
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
        if "WAIMAI_FINISH_CLAIM" in script:
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
        if "WAIMAI_RECOVER_RECEIPT" in script:
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
        raise AssertionError("unexpected Lua script")

    def scan_iter(self, match: str, count: int = 10):
        prefix = match.rstrip("*")
        yielded = 0
        for key in list(self.hashes.keys()):
            if key.startswith(prefix):
                yield key
                yielded += 1
                if yielded >= count:
                    return


FakeRedis = RedisTestDouble


def test_api_server_generate_only_enqueues_and_status_reads_task(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    monkeypatch.setenv("PROMPT_API_TOKEN", "prompt-api-test-token")
    api_module.app.config.update(TESTING=True)
    headers = {"Authorization": "Bearer prompt-api-test-token"}

    response = api_module.app.test_client().post(
        "/generate",
        json={"prompt": "牛肉饭商品图"},
        headers=headers,
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert set(payload.keys()) == {"task_id"}
    UUID(payload["task_id"])

    status_response = api_module.app.test_client().get(
        f"/status/{payload['task_id']}",
        headers=headers,
    )
    status_payload = status_response.get_json()
    assert status_response.status_code == 200
    assert set(status_payload.keys()) == {"status", "image_url"}
    assert status_payload["status"] == "pending"
    assert status_payload["image_url"] == ""


def test_api_server_generate_requires_prompt_and_ignores_legacy_fields(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    monkeypatch.setenv("PROMPT_API_TOKEN", "prompt-api-test-token")
    api_module.app.config.update(TESTING=True)

    response = api_module.app.test_client().post(
        "/generate",
        json={"category": "盖饭", "dishName": "牛肉饭"},
        headers={"X-Prompt-API-Token": "prompt-api-test-token"},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_generation_request"
    assert queue.redis.lists == {}


def test_api_server_rejects_oversized_or_non_string_prompt_before_redis(
    monkeypatch,
) -> None:
    queue = RedisTaskQueue(
        FakeRedis(),
        RedisQueueConfig(namespace="test", queue_name="generate"),
    )
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    monkeypatch.setenv("PROMPT_API_TOKEN", "prompt-api-test-token")
    api_module.app.config.update(TESTING=True)
    client = api_module.app.test_client()
    headers = {"Authorization": "Bearer prompt-api-test-token"}

    wrong_type = client.post(
        "/generate",
        json={"prompt": ["牛肉饭", "盖饭"]},
        headers=headers,
    )
    oversized_prompt = client.post(
        "/generate",
        json={"prompt": "x" * 8_001},
        headers=headers,
    )
    oversized_body = client.post(
        "/generate",
        data=b'{"prompt":"' + (b"x" * (64 * 1024)) + b'"}',
        content_type="application/json",
        headers=headers,
    )

    assert wrong_type.status_code == 400
    assert wrong_type.get_json()["code"] == "invalid_generation_request"
    assert oversized_prompt.status_code == 400
    assert (
        oversized_prompt.get_json()["code"]
        == "invalid_generation_request"
    )
    assert oversized_body.status_code == 413
    assert (
        oversized_body.get_json()["code"]
        == "generation_request_too_large"
    )
    assert queue.redis.lists == {}
    assert queue.redis.hashes == {}


def test_api_server_generation_routes_fail_closed_without_valid_token(
    monkeypatch,
) -> None:
    queue = RedisTaskQueue(
        FakeRedis(),
        RedisQueueConfig(namespace="test", queue_name="generate"),
    )
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    api_module.app.config.update(TESTING=True)
    client = api_module.app.test_client()

    monkeypatch.delenv("PROMPT_API_TOKEN", raising=False)
    unavailable = client.post(
        "/generate",
        json={"prompt": "牛肉饭商品图"},
    )
    assert unavailable.status_code == 503
    assert unavailable.get_json()["code"] == "prompt_api_auth_unavailable"

    monkeypatch.setenv("PROMPT_API_TOKEN", "prompt-api-test-token")
    missing = client.post(
        "/generate",
        json={"prompt": "牛肉饭商品图"},
    )
    forbidden = client.post(
        "/generate",
        json={"prompt": "牛肉饭商品图"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    hidden_status = client.get("/status/unknown-task")

    assert missing.status_code == 401
    assert missing.get_json()["code"] == "prompt_api_auth_required"
    assert forbidden.status_code == 403
    assert forbidden.get_json()["code"] == "prompt_api_auth_forbidden"
    assert hidden_status.status_code == 401
    assert hidden_status.get_json()["code"] == "prompt_api_auth_required"
    assert queue.redis.lists == {}


def test_api_server_has_no_generation_provider_imports() -> None:
    source = (Path(__file__).resolve().parents[1] / "api-server" / "app.py").read_text(encoding="utf-8")

    assert "shared.generator" not in source
    assert "generate_image" not in source


def test_worker_processes_task_and_writes_result() -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    task = queue.enqueue({"prompt": "招牌牛肉饭"}, task_id="task-success")
    worker = GenerationWorker(queue, handler=lambda payload: {"image_url": "https://cdn.example/test.jpg"})

    assert task["status"] == "pending"
    assert worker.process_one(timeout_seconds=0) is True

    result = queue.get("task-success")
    assert result["status"] == "done"
    assert result["image_url"] == "https://cdn.example/test.jpg"
    assert result["attempts"] == 1


def test_worker_retries_twice_then_marks_failed(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    queue.enqueue({"prompt": "失败任务"}, task_id="task-failed")
    attempts = {"count": 0}

    def failing_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        attempts["count"] += 1
        raise RuntimeError("provider unavailable")

    import worker.worker as worker_module

    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
    worker = GenerationWorker(queue, handler=failing_handler, max_retries=2)

    assert worker.process_one(timeout_seconds=0) is True
    result = queue.get("task-failed")
    assert attempts["count"] == 3
    assert result["status"] == "failed"
    assert result["attempts"] == 3
    assert "provider unavailable" in result["error"]


def test_worker_deadline_keeps_lease_until_handler_completes() -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    queue.enqueue({"prompt": "超时任务"}, task_id="task-timeout")
    worker = GenerationWorker(
        queue,
        handler=lambda _payload: (time.sleep(0.05) or {"image_url": "https://cdn.example/slow.jpg"}),
        max_retries=0,
        task_timeout_seconds=0.01,
    )

    assert worker.process_one(timeout_seconds=0) is True
    result = queue.get("task-timeout")
    assert result["status"] == "done"
    assert result["attempts"] == 1
    assert result["image_url"] == "https://cdn.example/slow.jpg"
    assert result["error"] == ""


def test_worker_timeout_does_not_start_a_second_provider_call() -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    queue.enqueue({"prompt": "只调用一次"}, task_id="task-timeout-once")
    calls = {"count": 0}

    def slow_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        calls["count"] += 1
        time.sleep(0.05)
        return {"image_url": "https://cdn.example/late.jpg"}

    worker = GenerationWorker(
        queue,
        handler=slow_handler,
        max_retries=2,
        task_timeout_seconds=0.01,
    )

    assert worker.process_one(timeout_seconds=0) is True
    assert calls["count"] == 1
    assert queue.get("task-timeout-once")["status"] == "done"


def test_worker_heartbeats_during_provider_execution(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    queue.enqueue({"prompt": "持续心跳"}, task_id="task-heartbeat")
    heartbeat_count = {"value": 0}
    original_heartbeat = queue.heartbeat

    def counted_heartbeat(*args, **kwargs):
        heartbeat_count["value"] += 1
        return original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(queue, "heartbeat", counted_heartbeat)
    worker = GenerationWorker(
        queue,
        handler=lambda _payload: (
            time.sleep(0.35)
            or {"image_url": "https://cdn.example/heartbeat.jpg"}
        ),
        max_retries=0,
        task_timeout_seconds=1,
    )
    worker.lease_seconds = 0.15

    assert worker.process_one(timeout_seconds=0) is True
    assert heartbeat_count["value"] >= 2
    assert queue.get("task-heartbeat")["status"] == "done"


def test_worker_recovers_stale_running_task_before_dequeue() -> None:
    redis = FakeRedis()
    queue = RedisTaskQueue(redis, RedisQueueConfig(namespace="test", queue_name="generate"))
    queue.enqueue({"prompt": "恢复任务"}, task_id="task-recover")
    queue.dequeue(timeout_seconds=0)
    queue.mark_running("task-recover", attempts=1)
    redis.hashes[queue.config.task_key("task-recover")]["updated_at"] = "1"

    worker = GenerationWorker(
        queue,
        handler=lambda _payload: {"image_url": "https://cdn.example/recovered.jpg"},
        max_retries=2,
        recovery_stale_seconds=0.001,
    )

    assert worker.process_one(timeout_seconds=0) is True
    result = queue.get("task-recover")
    assert result["status"] == "done"
    assert result["image_url"] == "https://cdn.example/recovered.jpg"
    assert result["attempts"] == 2


def test_worker_run_loop_publishes_service_liveness(monkeypatch) -> None:
    queue = RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(namespace="test", queue_name="product-generate"),
    )
    worker = GenerationWorker(
        queue,
        service_id="product-worker",
        service_heartbeat_ttl_seconds=9,
    )

    def stop_after_heartbeat(*, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if queue.service_liveness("product-worker") is not None:
                raise KeyboardInterrupt
            time.sleep(0.01)
        raise AssertionError("worker service heartbeat was not published")

    monkeypatch.setattr(worker, "process_one", stop_after_heartbeat)

    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(timeout_seconds=0)

    live = queue.service_liveness("product-worker")
    assert live is not None
    assert live["instanceId"] == worker.worker_id
    assert live["ttlSeconds"] == 9


def test_worker_run_loop_can_start_ten_independent_queue_consumers(monkeypatch) -> None:
    queue = RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(namespace="test", queue_name="product-revision"),
    )
    worker = GenerationWorker(queue, service_id="revision-worker")
    state_lock = threading.Lock()
    first_wave_ready = threading.Event()
    active = 0
    peak = 0
    interrupt_sent = False

    def observe_consumers(*, timeout_seconds: int) -> bool:
        del timeout_seconds
        nonlocal active, peak, interrupt_sent
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active == 10:
                first_wave_ready.set()
        first_wave_ready.wait(timeout=2)
        time.sleep(0.005)
        should_interrupt = False
        with state_lock:
            active -= 1
            if not interrupt_sent:
                interrupt_sent = True
                should_interrupt = True
        if should_interrupt:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(worker, "process_one", observe_consumers)

    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(timeout_seconds=0, concurrency=10)

    assert peak == 10


def _load_api_server_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "api-server" / "app.py"
    spec = importlib.util.spec_from_file_location("saas_api_server_app", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
