from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from shared.redis_queue import RedisQueueConfig, RedisTaskQueue
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

    def scan_iter(self, match: str, count: int = 10):
        prefix = match.rstrip("*")
        yielded = 0
        for key in list(self.hashes.keys()):
            if key.startswith(prefix):
                yield key
                yielded += 1
                if yielded >= count:
                    return


def test_api_server_generate_only_enqueues_and_status_reads_task(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    api_module.app.config.update(TESTING=True)

    response = api_module.app.test_client().post("/generate", json={"prompt": "牛肉饭商品图"})

    assert response.status_code == 202
    payload = response.get_json()
    assert set(payload.keys()) == {"task_id"}
    UUID(payload["task_id"])

    status_response = api_module.app.test_client().get(f"/status/{payload['task_id']}")
    status_payload = status_response.get_json()
    assert status_response.status_code == 200
    assert set(status_payload.keys()) == {"status", "image_url"}
    assert status_payload["status"] == "pending"
    assert status_payload["image_url"] == ""


def test_api_server_generate_requires_prompt_and_ignores_legacy_fields(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    api_module.app.config.update(TESTING=True)

    response = api_module.app.test_client().post("/generate", json={"category": "盖饭", "dishName": "牛肉饭"})

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_generation_request"
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


def test_worker_times_out_handler_and_marks_failed() -> None:
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
    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert "timed out" in result["error"]


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


def _load_api_server_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "api-server" / "app.py"
    spec = importlib.util.spec_from_file_location("saas_api_server_app", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
