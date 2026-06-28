from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

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


def test_api_server_generate_only_enqueues_and_status_reads_task(monkeypatch) -> None:
    queue = RedisTaskQueue(FakeRedis(), RedisQueueConfig(namespace="test", queue_name="generate"))
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    api_module.app.config.update(TESTING=True)

    response = api_module.app.test_client().post("/generate", json={"prompt": "牛肉饭商品图"})

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["task_id"].startswith("task_")
    assert payload["status"] == "pending"
    assert payload["status_url"] == f"/status/{payload['task_id']}"

    status_response = api_module.app.test_client().get(payload["status_url"])
    status_payload = status_response.get_json()
    assert status_response.status_code == 200
    assert status_payload["status"] == "pending"
    assert status_payload["image_url"] == ""


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


def _load_api_server_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "api-server" / "app.py"
    spec = importlib.util.spec_from_file_location("saas_api_server_app", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
