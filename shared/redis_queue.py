from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4


TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"

TERMINAL_STATUSES = frozenset({TASK_DONE, TASK_FAILED})


class QueueError(RuntimeError):
    pass


class TaskNotFound(QueueError, LookupError):
    pass


@dataclass(frozen=True)
class RedisQueueConfig:
    namespace: str = "waimai:saas"
    queue_name: str = "generate"

    @property
    def queue_key(self) -> str:
        return f"{self.namespace}:queue:{self.queue_name}"

    def task_key(self, task_id: str) -> str:
        return f"{self.namespace}:task:{task_id}"


class RedisTaskQueue:
    """Redis-backed task queue shared by the API server and worker."""

    def __init__(self, redis_client: Any, config: RedisQueueConfig | None = None) -> None:
        self.redis = redis_client
        self.config = config or RedisQueueConfig()

    def enqueue(self, payload: Mapping[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
        clean_payload = _json_object(payload, "payload")
        resolved_task_id = _clean_text(task_id or f"task_{uuid4().hex}", "task_id")
        now = _now_ms()
        task = {
            "task_id": resolved_task_id,
            "status": TASK_PENDING,
            "image_url": "",
            "error": "",
            "attempts": "0",
            "payload_json": _json(clean_payload),
            "created_at": str(now),
            "updated_at": str(now),
        }
        self.redis.hset(self.config.task_key(resolved_task_id), mapping=task)
        self.redis.rpush(self.config.queue_key, _json({"task_id": resolved_task_id, "payload": clean_payload}))
        return self.get(resolved_task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        clean_task_id = _clean_text(task_id, "task_id")
        raw = self.redis.hgetall(self.config.task_key(clean_task_id))
        if not raw:
            raise TaskNotFound(f"task not found: {clean_task_id}")
        return _decode_task(raw)

    def dequeue(self, timeout_seconds: int = 5) -> dict[str, Any] | None:
        item = self.redis.brpop(self.config.queue_key, timeout=timeout_seconds)
        if item is None:
            return None
        _queue_key, raw_payload = item
        message = _json_loads(_decode(raw_payload))
        if not isinstance(message, dict):
            raise QueueError("invalid task queue payload")
        task_id = _clean_text(str(message.get("task_id") or ""), "task_id")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        return {"task_id": task_id, "payload": payload}

    def mark_running(self, task_id: str, *, attempts: int) -> dict[str, Any]:
        return self._update(
            task_id,
            {
                "status": TASK_RUNNING,
                "attempts": str(_non_negative_int(attempts, "attempts")),
                "updated_at": str(_now_ms()),
            },
        )

    def mark_done(self, task_id: str, *, image_url: str, result: Mapping[str, Any] | None = None) -> dict[str, Any]:
        clean_url = _clean_text(image_url, "image_url")
        return self._update(
            task_id,
            {
                "status": TASK_DONE,
                "image_url": clean_url,
                "error": "",
                "result_json": _json(dict(result or {})),
                "updated_at": str(_now_ms()),
            },
        )

    def mark_failed(self, task_id: str, *, error: str, attempts: int) -> dict[str, Any]:
        return self._update(
            task_id,
            {
                "status": TASK_FAILED,
                "error": str(error or "worker failed"),
                "attempts": str(_non_negative_int(attempts, "attempts")),
                "updated_at": str(_now_ms()),
            },
        )

    def requeue(self, task_id: str, payload: Mapping[str, Any]) -> None:
        self.redis.rpush(self.config.queue_key, _json({"task_id": task_id, "payload": dict(payload)}))

    def _update(self, task_id: str, mapping: Mapping[str, Any]) -> dict[str, Any]:
        self.get(task_id)
        self.redis.hset(self.config.task_key(task_id), mapping={key: str(value) for key, value in mapping.items()})
        return self.get(task_id)


def redis_client_from_env(env: Mapping[str, str] | None = None) -> Any:
    values = os.environ if env is None else env
    redis_url = str(values.get("REDIS_URL") or "").strip()
    if not redis_url:
        raise QueueError("REDIS_URL is required")
    import redis

    return redis.Redis.from_url(redis_url, decode_responses=False)


def queue_from_env(env: Mapping[str, str] | None = None) -> RedisTaskQueue:
    values = os.environ if env is None else env
    namespace = str(values.get("REDIS_NAMESPACE") or "waimai:saas").strip() or "waimai:saas"
    queue_name = str(values.get("REDIS_GENERATION_QUEUE") or "generate").strip() or "generate"
    return RedisTaskQueue(redis_client_from_env(values), RedisQueueConfig(namespace=namespace, queue_name=queue_name))


def public_task_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(task.get("task_id") or ""),
        "status": str(task.get("status") or TASK_PENDING),
        "image_url": str(task.get("image_url") or ""),
        "error": str(task.get("error") or ""),
        "attempts": int(task.get("attempts") or 0),
        "result": dict(task.get("result") or {}),
    }


def _decode_task(raw: Mapping[Any, Any]) -> dict[str, Any]:
    decoded = {_decode(key): _decode(value) for key, value in raw.items()}
    payload = _json_loads(decoded.get("payload_json") or "{}")
    result = _json_loads(decoded.get("result_json") or "{}")
    return {
        "task_id": decoded.get("task_id", ""),
        "status": decoded.get("status", TASK_PENDING),
        "image_url": decoded.get("image_url", ""),
        "error": decoded.get("error", ""),
        "attempts": int(decoded.get("attempts") or 0),
        "payload": payload if isinstance(payload, dict) else {},
        "result": result if isinstance(result, dict) else {},
        "created_at": int(decoded.get("created_at") or 0),
        "updated_at": int(decoded.get("updated_at") or 0),
    }


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return dict(value)


def _clean_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must be non-empty")
    return cleaned


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    number = int(value)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _now_ms() -> int:
    return int(time.time() * 1000)
