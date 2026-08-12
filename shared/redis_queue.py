from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4


TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"

TERMINAL_STATUSES = frozenset({TASK_DONE, TASK_FAILED})
DEFAULT_TERMINAL_TTL_SECONDS = 7 * 24 * 60 * 60


class QueueError(RuntimeError):
    pass


class TaskNotFound(QueueError, LookupError):
    pass


class IdempotencyConflict(QueueError):
    def __init__(self, *, task_id: str, request_sha256: str) -> None:
        super().__init__(
            f"idempotency key is already bound to task {task_id} with a different request"
        )
        self.task_id = task_id
        self.request_sha256 = request_sha256


class LeaseLost(QueueError):
    pass


class CancellationRequested(QueueError):
    pass


@dataclass(frozen=True)
class RedisQueueConfig:
    namespace: str = "waimai:saas"
    queue_name: str = "generate"
    terminal_ttl_seconds: int = DEFAULT_TERMINAL_TTL_SECONDS
    idempotency_ttl_seconds: int = DEFAULT_TERMINAL_TTL_SECONDS

    @property
    def slot_tag(self) -> str:
        return hashlib.sha256(
            f"{self.namespace}:{self.queue_name}".encode("utf-8")
        ).hexdigest()[:16]

    @property
    def key_prefix(self) -> str:
        return f"{self.namespace}:{{{self.slot_tag}}}"

    @property
    def queue_key(self) -> str:
        return f"{self.key_prefix}:queue:{self.queue_name}"

    @property
    def processing_key(self) -> str:
        return f"{self.key_prefix}:processing:{self.queue_name}"

    @property
    def dead_letter_key(self) -> str:
        return f"{self.key_prefix}:dead-letter:{self.queue_name}"

    @property
    def task_key_prefix(self) -> str:
        return f"{self.key_prefix}:task:"

    def task_key(self, task_id: str) -> str:
        return f"{self.task_key_prefix}{task_id}"

    def idempotency_claim_key(self, user_id: str, idempotency_key: str) -> str:
        digest = _idempotency_digest(user_id, idempotency_key)
        return f"{self.key_prefix}:idempotency:{digest}"

    def idempotency_claim_key_from_digest(self, digest: str) -> str:
        return f"{self.key_prefix}:idempotency:{_sha256_hex(digest, 'idempotency_digest')}"

    def service_heartbeat_key(self, service_id: str) -> str:
        digest = hashlib.sha256(
            _bounded_text(service_id, "service_id", maximum=200).encode("utf-8")
        ).hexdigest()
        return f"{self.key_prefix}:service-heartbeat:{digest}"


class RedisTaskQueue:
    """Redis-backed task queue shared by the API server and worker."""

    def __init__(self, redis_client: Any, config: RedisQueueConfig | None = None) -> None:
        self.redis = redis_client
        self.config = config or RedisQueueConfig()

    def enqueue(self, payload: Mapping[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
        clean_payload = _json_object(payload, "payload")
        resolved_task_id = _clean_text(task_id or str(uuid4()), "task_id")
        result = self.redis.eval(
            _ENQUEUE_LUA,
            2,
            self.config.task_key(resolved_task_id),
            self.config.queue_key,
            resolved_task_id,
            TASK_PENDING,
            _json(clean_payload),
            _queue_receipt(resolved_task_id, clean_payload),
        )
        outcome, _unused = _redis_pair(result, "enqueue")
        if outcome == -3:
            raise QueueError(f"task already exists: {resolved_task_id}")
        if outcome == -4:
            raise QueueError("Redis queue key has an incompatible type")
        if outcome != 1:
            raise QueueError(f"unknown Redis enqueue result: {outcome}")
        return self.get(resolved_task_id)

    def enqueue_idempotent(
        self,
        payload: Mapping[str, Any],
        *,
        user_id: str,
        idempotency_key: str,
        request_sha256: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        clean_payload = _json_object(payload, "payload")
        clean_user_id = _bounded_text(user_id, "user_id", maximum=200)
        clean_idempotency_key = _bounded_text(
            idempotency_key,
            "idempotency_key",
            maximum=255,
        )
        clean_request_sha256 = _sha256_hex(request_sha256, "request_sha256")
        resolved_task_id = _clean_text(task_id or str(uuid4()), "task_id")
        idempotency_digest = _idempotency_digest(
            clean_user_id,
            clean_idempotency_key,
        )
        queue_message = _queue_receipt(resolved_task_id, clean_payload)
        result = self.redis.eval(
            _ENQUEUE_IDEMPOTENT_LUA,
            3,
            self.config.task_key(resolved_task_id),
            self.config.queue_key,
            self.config.idempotency_claim_key(
                clean_user_id,
                clean_idempotency_key,
            ),
            resolved_task_id,
            TASK_PENDING,
            "",
            "",
            "0",
            _json(clean_payload),
            "",
            "",
            clean_user_id,
            idempotency_digest,
            clean_request_sha256,
            queue_message,
        )
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            raise QueueError("invalid Redis idempotency result")
        outcome = int(_decode(result[0]))
        bound_task_id = _clean_text(_decode(result[1]), "task_id")
        if outcome in {0, 1}:
            return self.get(bound_task_id)
        if outcome == -1:
            raise IdempotencyConflict(
                task_id=bound_task_id,
                request_sha256=clean_request_sha256,
            )
        if outcome == -2:
            raise QueueError("corrupt Redis idempotency claim")
        if outcome == -3:
            raise QueueError(f"task already exists: {bound_task_id}")
        if outcome == -4:
            raise QueueError("Redis idempotency keys have incompatible types")
        raise QueueError(f"unknown Redis idempotency result: {outcome}")

    def get(self, task_id: str) -> dict[str, Any]:
        clean_task_id = _clean_text(task_id, "task_id")
        raw = self.redis.hgetall(self.config.task_key(clean_task_id))
        if not raw:
            raise TaskNotFound(f"task not found: {clean_task_id}")
        return _decode_task(raw)

    def publish_service_heartbeat(
        self,
        *,
        service_id: str,
        instance_id: str,
        ttl_seconds: int = 30,
    ) -> dict[str, Any]:
        clean_service_id = _bounded_text(service_id, "service_id", maximum=200)
        clean_instance_id = _bounded_text(instance_id, "instance_id", maximum=200)
        clean_ttl_seconds = _positive_int(ttl_seconds, "ttl_seconds")
        payload = {
            "serviceId": clean_service_id,
            "instanceId": clean_instance_id,
            "queueName": self.config.queue_name,
            "reportedAtMs": _now_ms(),
        }
        stored = self.redis.set(
            self.config.service_heartbeat_key(clean_service_id),
            _json(payload),
            ex=clean_ttl_seconds,
        )
        if stored is False:
            raise QueueError(
                f"Redis rejected service heartbeat for {clean_service_id}"
            )
        return dict(payload)

    def service_liveness(self, service_id: str) -> dict[str, Any] | None:
        clean_service_id = _bounded_text(service_id, "service_id", maximum=200)
        key = self.config.service_heartbeat_key(clean_service_id)
        raw = self.redis.get(key)
        if raw is None:
            return None
        payload = _json_loads(_decode(raw))
        if not isinstance(payload, dict):
            raise QueueError(
                f"invalid service heartbeat payload for {clean_service_id}"
            )
        if (
            str(payload.get("serviceId") or "") != clean_service_id
            or not str(payload.get("instanceId") or "").strip()
            or str(payload.get("queueName") or "") != self.config.queue_name
        ):
            raise QueueError(
                f"corrupt service heartbeat payload for {clean_service_id}"
            )
        try:
            reported_at_ms = _positive_int(
                payload.get("reportedAtMs"),
                "reportedAtMs",
            )
        except (TypeError, ValueError) as exc:
            raise QueueError(
                f"corrupt service heartbeat timestamp for {clean_service_id}"
            ) from exc
        ttl_seconds = int(self.redis.ttl(key))
        if ttl_seconds < 0:
            return None
        return {
            **payload,
            "reportedAtMs": reported_at_ms,
            "ageMs": max(0, _now_ms() - reported_at_ms),
            "ttlSeconds": ttl_seconds,
        }

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

    def claim(
        self,
        *,
        worker_id: str,
        lease_ms: int,
        timeout_seconds: float = 5,
    ) -> dict[str, Any] | None:
        clean_worker_id = _bounded_text(worker_id, "worker_id", maximum=200)
        clean_lease_ms = _positive_int(lease_ms, "lease_ms")
        timeout = max(0.0, float(timeout_seconds))
        deadline = time.monotonic() + timeout
        while True:
            lease_token = str(uuid4())
            result = self.redis.eval(
                _CLAIM_NEXT_LUA,
                3,
                self.config.queue_key,
                self.config.processing_key,
                self.config.dead_letter_key,
                self.config.task_key_prefix,
                clean_worker_id,
                lease_token,
                str(clean_lease_ms),
            )
            if not isinstance(result, (list, tuple)) or len(result) < 6:
                raise QueueError("invalid Redis claim result")
            outcome = int(_decode(result[0]))
            if outcome == 1:
                receipt = _decode(result[1])
                message = _queue_message(receipt)
                return {
                    "task_id": message["task_id"],
                    "payload": message["payload"],
                    "receipt": receipt,
                    "worker_id": clean_worker_id,
                    "lease_token": lease_token,
                    "lease_expires_at": int(_decode(result[5])),
                    "attempts": int(_decode(result[3])),
                }
            if outcome == -4:
                raise QueueError("Redis claim keys have incompatible types")
            if outcome != 0:
                raise QueueError(f"unknown Redis claim result: {outcome}")
            if timeout == 0:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.05, remaining))

    def heartbeat(
        self,
        task_id: str,
        *,
        lease_token: str,
        lease_ms: int,
        attempts: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        clean_task_id = _clean_text(task_id, "task_id")
        clean_lease_token = _clean_text(lease_token, "lease_token")
        clean_lease_ms = _positive_int(lease_ms, "lease_ms")
        attempts_value = (
            ""
            if attempts is None
            else str(_non_negative_int(attempts, "attempts"))
        )
        result = self.redis.eval(
            _HEARTBEAT_LUA,
            1,
            self.config.task_key(clean_task_id),
            clean_lease_token,
            str(clean_lease_ms),
            attempts_value,
            str(error or ""),
        )
        outcome = int(_decode(result))
        if outcome != 1:
            raise LeaseLost(f"task lease is no longer owned: {clean_task_id}")
        return self.get(clean_task_id)

    def ack_done(
        self,
        task_id: str,
        *,
        receipt: str,
        lease_token: str,
        image_url: str,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._finish_claim(
            task_id,
            receipt=receipt,
            lease_token=lease_token,
            status=TASK_DONE,
            image_url=_clean_text(image_url, "image_url"),
            error="",
            result=result,
            attempts=None,
        )

    def ack_failed(
        self,
        task_id: str,
        *,
        receipt: str,
        lease_token: str,
        error: str,
        attempts: int,
    ) -> dict[str, Any]:
        return self._finish_claim(
            task_id,
            receipt=receipt,
            lease_token=lease_token,
            status=TASK_FAILED,
            image_url="",
            error=str(error or "worker failed"),
            result=None,
            attempts=_non_negative_int(attempts, "attempts"),
        )

    def ack_canceled(
        self,
        task_id: str,
        *,
        receipt: str,
        lease_token: str,
        attempts: int,
    ) -> dict[str, Any]:
        return self._finish_claim(
            task_id,
            receipt=receipt,
            lease_token=lease_token,
            status=TASK_FAILED,
            image_url="",
            error="canceled",
            result={"canceled": True},
            attempts=_non_negative_int(attempts, "attempts"),
            allow_cancel_requested=True,
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        clean_task_id = _clean_text(task_id, "task_id")
        task = self.get(clean_task_id)
        idempotency_digest = str(task.get("idempotency_key_hash") or "")
        idempotency_key = (
            self.config.idempotency_claim_key_from_digest(idempotency_digest)
            if idempotency_digest
            else self.config.task_key(clean_task_id)
        )
        result = self.redis.eval(
            _CANCEL_TASK_LUA,
            4,
            self.config.task_key(clean_task_id),
            self.config.queue_key,
            self.config.processing_key,
            idempotency_key,
            str(max(0, int(self.config.terminal_ttl_seconds))),
            str(max(0, int(self.config.idempotency_ttl_seconds))),
        )
        outcome = int(_decode(result))
        if outcome == -1:
            raise TaskNotFound(f"task not found: {clean_task_id}")
        if outcome == -4:
            raise QueueError("Redis cancellation keys have incompatible types")
        if outcome not in {1, 2}:
            raise QueueError(f"unknown Redis cancel result: {outcome}")
        return self.get(clean_task_id)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        clean_task_id = _clean_text(task_id, "task_id")
        task = self.get(clean_task_id)
        idempotency_digest = str(task.get("idempotency_key_hash") or "")
        idempotency_key = (
            self.config.idempotency_claim_key_from_digest(idempotency_digest)
            if idempotency_digest
            else self.config.task_key(clean_task_id)
        )
        result = self.redis.eval(
            _REQUEST_CANCEL_LUA,
            4,
            self.config.task_key(clean_task_id),
            self.config.queue_key,
            self.config.processing_key,
            idempotency_key,
            str(max(0, int(self.config.terminal_ttl_seconds))),
            str(max(0, int(self.config.idempotency_ttl_seconds))),
        )
        outcome = int(_decode(result))
        if outcome == -1:
            raise TaskNotFound(f"task not found: {clean_task_id}")
        if outcome == -4:
            raise QueueError("Redis cancellation keys have incompatible types")
        if outcome not in {1, 2, 3}:
            raise QueueError(f"unknown Redis cancellation request result: {outcome}")
        return self.get(clean_task_id)

    def mark_running(self, task_id: str, *, attempts: int) -> dict[str, Any]:
        now = _now_ms()
        return self._update(
            task_id,
            {
                "status": TASK_RUNNING,
                "attempts": str(_non_negative_int(attempts, "attempts")),
                "started_at": str(now),
                "updated_at": str(now),
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
                "finished_at": str(_now_ms()),
            },
        )

    def mark_failed(self, task_id: str, *, error: str, attempts: int) -> dict[str, Any]:
        now = _now_ms()
        return self._update(
            task_id,
            {
                "status": TASK_FAILED,
                "error": str(error or "worker failed"),
                "attempts": str(_non_negative_int(attempts, "attempts")),
                "updated_at": str(now),
                "finished_at": str(now),
            },
        )

    def requeue(self, task_id: str, payload: Mapping[str, Any]) -> None:
        self.redis.lpush(
            self.config.queue_key,
            _queue_receipt(task_id, dict(payload)),
        )

    def recover_stale_running(
        self,
        *,
        stale_after_ms: int,
        max_attempts: int,
        max_recovered: int = 100,
    ) -> dict[str, Any]:
        stale_after_ms = _non_negative_int(stale_after_ms, "stale_after_ms")
        max_attempts = _non_negative_int(max_attempts, "max_attempts")
        max_recovered = _non_negative_int(max_recovered, "max_recovered")
        now = _now_ms()
        recovered: list[str] = []
        failed: list[str] = []
        pattern = self.config.task_key("*")
        for raw_key in self.redis.scan_iter(match=pattern, count=max_recovered):
            if len(recovered) + len(failed) >= max_recovered:
                break
            key = _decode(raw_key)
            raw = self.redis.hgetall(key)
            if not raw:
                continue
            task = _decode_task(raw)
            if task["status"] != TASK_RUNNING:
                continue
            if task.get("lease_token"):
                continue
            updated_at = int(task.get("updated_at") or 0)
            if updated_at and now - updated_at < stale_after_ms:
                continue
            task_id = str(task["task_id"])
            attempts = int(task.get("attempts") or 0)
            if attempts >= max_attempts:
                self.mark_failed(
                    task_id,
                    error="task recovered as failed after worker timeout",
                    attempts=attempts,
                )
                failed.append(task_id)
                continue
            self._update(
                task_id,
                {
                    "status": TASK_PENDING,
                    "error": "recovered stale running task",
                    "updated_at": str(now),
                },
            )
            self.requeue(task_id, task.get("payload") if isinstance(task.get("payload"), dict) else {})
            recovered.append(task_id)
        return {"recovered": recovered, "failed": failed}

    def recover_expired_claims(
        self,
        *,
        max_attempts: int,
        max_recovered: int = 100,
    ) -> dict[str, Any]:
        clean_max_attempts = _non_negative_int(max_attempts, "max_attempts")
        clean_max_recovered = _non_negative_int(max_recovered, "max_recovered")
        if clean_max_recovered == 0:
            return {"recovered": [], "failed": [], "cleaned": []}
        receipts = self.redis.lrange(self.config.processing_key, 0, -1)
        recovered: list[str] = []
        failed: list[str] = []
        cleaned: list[str] = []
        for raw_receipt in receipts:
            if (
                len(recovered) + len(failed) + len(cleaned)
                >= clean_max_recovered
            ):
                break
            receipt = _decode(raw_receipt)
            try:
                message = _queue_message(receipt)
            except QueueError:
                self.redis.lrem(self.config.processing_key, 1, receipt)
                cleaned.append("invalid")
                continue
            task_id = message["task_id"]
            result = self.redis.eval(
                _RECOVER_RECEIPT_LUA,
                3,
                self.config.task_key(task_id),
                self.config.processing_key,
                self.config.queue_key,
                receipt,
                str(clean_max_attempts),
            )
            outcome = int(_decode(result))
            if outcome == 1:
                recovered.append(task_id)
            elif outcome == 2:
                failed.append(task_id)
            elif outcome in {3, 4}:
                cleaned.append(task_id)
            elif outcome != 0:
                raise QueueError(
                    f"unknown Redis recovery result for {task_id}: {outcome}"
                )
        return {
            "recovered": recovered,
            "failed": failed,
            "cleaned": cleaned,
        }

    def _finish_claim(
        self,
        task_id: str,
        *,
        receipt: str,
        lease_token: str,
        status: str,
        image_url: str,
        error: str,
        result: Mapping[str, Any] | None,
        attempts: int | None,
        allow_cancel_requested: bool = False,
    ) -> dict[str, Any]:
        clean_task_id = _clean_text(task_id, "task_id")
        clean_receipt = _clean_text(receipt, "receipt")
        clean_lease_token = _clean_text(lease_token, "lease_token")
        attempts_value = "" if attempts is None else str(attempts)
        task = self.get(clean_task_id)
        idempotency_digest = str(task.get("idempotency_key_hash") or "")
        idempotency_key = (
            self.config.idempotency_claim_key_from_digest(idempotency_digest)
            if idempotency_digest
            else self.config.task_key(clean_task_id)
        )
        result_code = self.redis.eval(
            _FINISH_CLAIM_LUA,
            3,
            self.config.task_key(clean_task_id),
            self.config.processing_key,
            idempotency_key,
            clean_receipt,
            clean_lease_token,
            status,
            image_url,
            error,
            _json(dict(result or {})),
            attempts_value,
            str(max(0, int(self.config.terminal_ttl_seconds))),
            str(max(0, int(self.config.idempotency_ttl_seconds))),
            "1" if allow_cancel_requested else "0",
        )
        outcome = int(_decode(result_code))
        if outcome == -2:
            raise CancellationRequested(
                f"task cancellation was requested: {clean_task_id}"
            )
        if outcome != 1:
            raise LeaseLost(f"task lease is no longer owned: {clean_task_id}")
        return self.get(clean_task_id)

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
    terminal_ttl_seconds = int(
        values.get("REDIS_TERMINAL_TASK_TTL_SECONDS")
        or DEFAULT_TERMINAL_TTL_SECONDS
    )
    idempotency_ttl_seconds = int(
        values.get("REDIS_IDEMPOTENCY_TTL_SECONDS")
        or terminal_ttl_seconds
    )
    return RedisTaskQueue(
        redis_client_from_env(values),
        RedisQueueConfig(
            namespace=namespace,
            queue_name=queue_name,
            terminal_ttl_seconds=max(0, terminal_ttl_seconds),
            idempotency_ttl_seconds=max(0, idempotency_ttl_seconds),
        ),
    )


def product_queue_from_env(env: Mapping[str, str] | None = None) -> RedisTaskQueue:
    values = dict(os.environ if env is None else env)
    values["REDIS_GENERATION_QUEUE"] = (
        str(values.get("REDIS_PRODUCT_QUEUE") or "product-generate").strip()
        or "product-generate"
    )
    return queue_from_env(values)


def revision_queue_from_env(env: Mapping[str, str] | None = None) -> RedisTaskQueue:
    values = dict(os.environ if env is None else env)
    values["REDIS_GENERATION_QUEUE"] = (
        str(values.get("REDIS_REVISION_QUEUE") or "product-revision").strip()
        or "product-revision"
    )
    return queue_from_env(values)


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
        "owner_user_id": decoded.get("owner_user_id", ""),
        "idempotency_key_hash": decoded.get("idempotency_key_hash", ""),
        "request_sha256": decoded.get("request_sha256", ""),
        "worker_id": decoded.get("worker_id", ""),
        "lease_token": decoded.get("lease_token", ""),
        "lease_expires_at": int(decoded.get("lease_expires_at") or 0),
        "cancel_requested": decoded.get("cancel_requested", "") == "1",
        "created_at": int(decoded.get("created_at") or 0),
        "updated_at": int(decoded.get("updated_at") or 0),
        "started_at": int(decoded.get("started_at") or 0),
        "finished_at": int(decoded.get("finished_at") or 0),
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


def _bounded_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = _clean_text(value, name)
    if len(cleaned) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return cleaned


def _sha256_hex(value: str, name: str) -> str:
    cleaned = _clean_text(value, name).lower()
    if re.fullmatch(r"[0-9a-f]{64}", cleaned) is None:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return cleaned


def _idempotency_digest(user_id: str, idempotency_key: str) -> str:
    clean_user_id = _bounded_text(user_id, "user_id", maximum=200)
    clean_key = _bounded_text(idempotency_key, "idempotency_key", maximum=255)
    return hashlib.sha256(
        f"{clean_user_id}\0{clean_key}".encode("utf-8")
    ).hexdigest()


def _positive_int(value: int, name: str) -> int:
    number = _non_negative_int(value, name)
    if number == 0:
        raise ValueError(f"{name} must be positive")
    return number


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


def _queue_message(receipt: str) -> dict[str, Any]:
    message = _json_loads(receipt)
    if not isinstance(message, dict):
        raise QueueError("invalid task queue payload")
    try:
        task_id = _clean_text(
            str(message.get("task_id") or ""),
            "task_id",
        )
    except (TypeError, ValueError) as exc:
        raise QueueError("invalid task queue payload") from exc
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise QueueError("invalid task queue payload")
    return {"task_id": task_id, "payload": payload}


def _queue_receipt(task_id: str, payload: Mapping[str, Any]) -> str:
    return _json(
        {
            "payload": dict(payload),
            "receipt_id": str(uuid4()),
            "task_id": _clean_text(task_id, "task_id"),
        }
    )


def _redis_pair(value: Any, operation: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise QueueError(f"invalid Redis {operation} result")
    return int(_decode(value[0])), int(_decode(value[1]))


def _now_ms() -> int:
    return int(time.time() * 1000)


_ENQUEUE_LUA = """
-- WAIMAI_ENQUEUE
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

local task_type = type_name(KEYS[1])
local queue_type = type_name(KEYS[2])
if task_type ~= "none" then
    return {-3, 0}
end
if queue_type ~= "none" and queue_type ~= "list" then
    return {-4, 0}
end

local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
redis.call(
    "HSET",
    KEYS[1],
    "task_id", ARGV[1],
    "status", ARGV[2],
    "image_url", "",
    "error", "",
    "attempts", "0",
    "payload_json", ARGV[3],
    "queue_receipt", ARGV[4],
    "cancel_requested", "0",
    "created_at", tostring(now_ms),
    "updated_at", tostring(now_ms),
    "started_at", "",
    "finished_at", ""
)
redis.call("LPUSH", KEYS[2], ARGV[4])
return {1, 0}
"""


_ENQUEUE_IDEMPOTENT_LUA = """
-- WAIMAI_ENQUEUE_IDEMPOTENT
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

local task_type = type_name(KEYS[1])
local queue_type = type_name(KEYS[2])
local claim_type = type_name(KEYS[3])
if queue_type ~= "none" and queue_type ~= "list" then
    return {-4, ARGV[1]}
end
if claim_type ~= "none" and claim_type ~= "string" then
    return {-4, ARGV[1]}
end

local existing = redis.call("GET", KEYS[3])
if existing then
    local separator = string.find(existing, ":", 1, true)
    if not separator then
        return {-2, existing}
    end
    local existing_sha = string.sub(existing, 1, separator - 1)
    local existing_task_id = string.sub(existing, separator + 1)
    if existing_sha == ARGV[11] then
        return {0, existing_task_id}
    end
    return {-1, existing_task_id}
end
if task_type ~= "none" then
    return {-3, ARGV[1]}
end

local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
redis.call(
    "HSET",
    KEYS[1],
    "task_id", ARGV[1],
    "status", ARGV[2],
    "image_url", ARGV[3],
    "error", ARGV[4],
    "attempts", ARGV[5],
    "payload_json", ARGV[6],
    "created_at", tostring(now_ms),
    "updated_at", tostring(now_ms),
    "started_at", ARGV[7],
    "finished_at", ARGV[8],
    "owner_user_id", ARGV[9],
    "idempotency_key_hash", ARGV[10],
    "request_sha256", ARGV[11],
    "queue_receipt", ARGV[12],
    "cancel_requested", "0"
)
redis.call("LPUSH", KEYS[2], ARGV[12])
redis.call("SET", KEYS[3], ARGV[11] .. ":" .. ARGV[1])
return {1, ARGV[1]}
"""


_CLAIM_NEXT_LUA = """
-- WAIMAI_CLAIM_NEXT
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

for _, key in ipairs(KEYS) do
    local value_type = type_name(key)
    if value_type ~= "none" and value_type ~= "list" then
        return {-4, "", "", 0, 0, 0}
    end
end

for _ = 1, 100 do
    local receipt = redis.call("RPOPLPUSH", KEYS[1], KEYS[2])
    if not receipt then
        return {0, "", "", 0, 0, 0}
    end
    local decoded_ok, message = pcall(cjson.decode, receipt)
    local task_id = ""
    if decoded_ok and type(message) == "table" then
        task_id = tostring(message["task_id"] or "")
    end
    local task_key = ARGV[1] .. task_id
    local task_type = task_id ~= "" and type_name(task_key) or "none"
    if task_type == "hash" then
        local status = redis.call("HGET", task_key, "status")
        if status == "pending" then
            local redis_time = redis.call("TIME")
            local now_ms = (
                tonumber(redis_time[1]) * 1000
                + math.floor(tonumber(redis_time[2]) / 1000)
            )
            local lease_expires_at = now_ms + tonumber(ARGV[4])
            local attempts = redis.call(
                "HINCRBY",
                task_key,
                "attempts",
                1
            )
            local started_at = redis.call(
                "HGET",
                task_key,
                "started_at"
            )
            if not started_at or started_at == "" then
                redis.call(
                    "HSET",
                    task_key,
                    "started_at",
                    tostring(now_ms)
                )
            end
            redis.call(
                "HSET",
                task_key,
                "status", "running",
                "worker_id", ARGV[2],
                "lease_token", ARGV[3],
                "lease_expires_at", tostring(lease_expires_at),
                "updated_at", tostring(now_ms)
            )
            return {
                1,
                receipt,
                task_id,
                attempts,
                now_ms,
                lease_expires_at
            }
        end
        redis.call("LREM", KEYS[2], 1, receipt)
        if status ~= "done" and status ~= "failed" then
            redis.call("LPUSH", KEYS[3], receipt)
        end
    else
        redis.call("LREM", KEYS[2], 1, receipt)
        redis.call("LPUSH", KEYS[3], receipt)
    end
end
return {0, "", "", 0, 0, 0}
"""


_HEARTBEAT_LUA = """
-- WAIMAI_HEARTBEAT
if redis.call("HGET", KEYS[1], "status") ~= "running" then
    return 0
end
if redis.call("HGET", KEYS[1], "lease_token") ~= ARGV[1] then
    return 0
end
local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
redis.call(
    "HSET",
    KEYS[1],
    "updated_at", tostring(now_ms),
    "lease_expires_at", tostring(now_ms + tonumber(ARGV[2]))
)
if ARGV[3] ~= "" then
    redis.call("HSET", KEYS[1], "attempts", ARGV[3])
end
if ARGV[4] ~= "" then
    redis.call("HSET", KEYS[1], "error", ARGV[4])
end
return 1
"""


_FINISH_CLAIM_LUA = """
-- WAIMAI_FINISH_CLAIM
local processing_type = redis.call("TYPE", KEYS[2])
if type(processing_type) == "table" then
    processing_type = processing_type["ok"]
end
if processing_type ~= "none" and processing_type ~= "list" then
    return -4
end
if redis.call("HGET", KEYS[1], "status") ~= "running" then
    return 0
end
if redis.call("HGET", KEYS[1], "lease_token") ~= ARGV[2] then
    return 0
end
if redis.call("HGET", KEYS[1], "cancel_requested") == "1" and ARGV[10] ~= "1" then
    return -2
end
local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
redis.call(
    "HSET",
    KEYS[1],
    "status", ARGV[3],
    "image_url", ARGV[4],
    "error", ARGV[5],
    "result_json", ARGV[6],
    "updated_at", tostring(now_ms),
    "finished_at", tostring(now_ms),
    "worker_id", "",
    "lease_token", "",
    "lease_expires_at", ""
)
if ARGV[7] ~= "" then
    redis.call("HSET", KEYS[1], "attempts", ARGV[7])
end
redis.call("LREM", KEYS[2], 1, ARGV[1])
if tonumber(ARGV[8]) > 0 then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[8]))
end
if KEYS[3] ~= KEYS[1] and tonumber(ARGV[9]) > 0 then
    redis.call("EXPIRE", KEYS[3], tonumber(ARGV[9]))
end
return 1
"""


_CANCEL_TASK_LUA = """
-- WAIMAI_CANCEL_TASK
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

if type_name(KEYS[1]) == "none" then
    return -1
end
if type_name(KEYS[1]) ~= "hash" then
    return -4
end
for index = 2, 3 do
    local value_type = type_name(KEYS[index])
    if value_type ~= "none" and value_type ~= "list" then
        return -4
    end
end

local status = redis.call("HGET", KEYS[1], "status")
if status == "done" or status == "failed" then
    return 2
end
local receipt = redis.call("HGET", KEYS[1], "queue_receipt")
if receipt and receipt ~= "" then
    redis.call("LREM", KEYS[2], 1, receipt)
    redis.call("LREM", KEYS[3], 1, receipt)
end
local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
redis.call(
    "HSET",
    KEYS[1],
    "status", "failed",
    "image_url", "",
    "error", "canceled",
    "result_json", '{"canceled":true}',
    "updated_at", tostring(now_ms),
    "finished_at", tostring(now_ms),
    "worker_id", "",
    "lease_token", "",
    "lease_expires_at", ""
)
if tonumber(ARGV[1]) > 0 then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[1]))
end
if KEYS[4] ~= KEYS[1] and tonumber(ARGV[2]) > 0 then
    redis.call("EXPIRE", KEYS[4], tonumber(ARGV[2]))
end
return 1
"""


_REQUEST_CANCEL_LUA = """
-- WAIMAI_REQUEST_CANCEL
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

if type_name(KEYS[1]) == "none" then
    return -1
end
if type_name(KEYS[1]) ~= "hash" then
    return -4
end
for index = 2, 3 do
    local value_type = type_name(KEYS[index])
    if value_type ~= "none" and value_type ~= "list" then
        return -4
    end
end

local status = redis.call("HGET", KEYS[1], "status")
if status == "done" or status == "failed" then
    return 2
end
local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
if status == "running" then
    redis.call(
        "HSET",
        KEYS[1],
        "cancel_requested", "1",
        "error", "cancel_requested",
        "updated_at", tostring(now_ms)
    )
    return 3
end

local receipt = redis.call("HGET", KEYS[1], "queue_receipt")
if receipt and receipt ~= "" then
    redis.call("LREM", KEYS[2], 1, receipt)
    redis.call("LREM", KEYS[3], 1, receipt)
end
redis.call(
    "HSET",
    KEYS[1],
    "status", "failed",
    "image_url", "",
    "error", "canceled",
    "result_json", '{"canceled":true}',
    "cancel_requested", "1",
    "updated_at", tostring(now_ms),
    "finished_at", tostring(now_ms),
    "worker_id", "",
    "lease_token", "",
    "lease_expires_at", ""
)
if tonumber(ARGV[1]) > 0 then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[1]))
end
if KEYS[4] ~= KEYS[1] and tonumber(ARGV[2]) > 0 then
    redis.call("EXPIRE", KEYS[4], tonumber(ARGV[2]))
end
return 1
"""


_RECOVER_RECEIPT_LUA = """
-- WAIMAI_RECOVER_RECEIPT
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

local processing_type = type_name(KEYS[2])
local queue_type = type_name(KEYS[3])
if (
    (processing_type ~= "none" and processing_type ~= "list")
    or (queue_type ~= "none" and queue_type ~= "list")
) then
    return -4
end
if redis.call("EXISTS", KEYS[1]) == 0 then
    redis.call("LREM", KEYS[2], 1, ARGV[1])
    return 4
end

local status = redis.call("HGET", KEYS[1], "status")
if status == "done" or status == "failed" then
    redis.call("LREM", KEYS[2], 1, ARGV[1])
    return 3
end
local lease_expires_at = tonumber(
    redis.call("HGET", KEYS[1], "lease_expires_at") or "0"
)
local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
if status == "running" and lease_expires_at > now_ms then
    return 0
end

if redis.call("HGET", KEYS[1], "cancel_requested") == "1" then
    redis.call(
        "HSET",
        KEYS[1],
        "status", "failed",
        "error", "canceled",
        "result_json", '{"canceled":true}',
        "updated_at", tostring(now_ms),
        "finished_at", tostring(now_ms),
        "worker_id", "",
        "lease_token", "",
        "lease_expires_at", ""
    )
    redis.call("LREM", KEYS[2], 1, ARGV[1])
    return 2
end

local attempts = tonumber(redis.call("HGET", KEYS[1], "attempts") or "0")
if attempts >= tonumber(ARGV[2]) then
    redis.call(
        "HSET",
        KEYS[1],
        "status", "failed",
        "error", "task lease expired after maximum attempts",
        "updated_at", tostring(now_ms),
        "finished_at", tostring(now_ms),
        "worker_id", "",
        "lease_token", "",
        "lease_expires_at", ""
    )
    redis.call("LREM", KEYS[2], 1, ARGV[1])
    return 2
end

redis.call(
    "HSET",
    KEYS[1],
    "status", "pending",
    "error", "recovered expired task lease",
    "updated_at", tostring(now_ms),
    "worker_id", "",
    "lease_token", "",
    "lease_expires_at", ""
)
redis.call("LREM", KEYS[2], 1, ARGV[1])
redis.call("RPUSH", KEYS[3], ARGV[1])
return 1
"""
