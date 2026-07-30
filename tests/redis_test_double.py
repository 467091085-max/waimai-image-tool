from __future__ import annotations

import json
import threading
import time
from typing import Any


class RedisTestDouble:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.lock = threading.RLock()

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        with self.lock:
            self.hashes.setdefault(key, {}).update(
                {str(field): str(value) for field, value in mapping.items()}
            )

    def hgetall(self, key: str) -> dict[str, str]:
        with self.lock:
            return dict(self.hashes.get(key, {}))

    def lpush(self, key: str, value: str) -> None:
        with self.lock:
            self.lists.setdefault(key, []).insert(0, value)

    def rpush(self, key: str, value: str) -> None:
        with self.lock:
            self.lists.setdefault(key, []).append(value)

    def brpop(self, key: str, timeout: int = 0) -> tuple[str, str] | None:
        with self.lock:
            values = self.lists.setdefault(key, [])
            if not values:
                return None
            return key, values.pop()

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

    def expire(self, key: str, seconds: int) -> bool:
        with self.lock:
            if self._key_type(key) == "none":
                return False
            self.expirations[key] = int(seconds)
            return True

    def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | None = None,
    ) -> bool:
        with self.lock:
            if self._key_type(key) not in {"none", "string"}:
                raise TypeError("WRONGTYPE")
            self.strings[key] = str(value)
            if ex is not None:
                self.expirations[key] = int(ex)
            else:
                self.expirations.pop(key, None)
            return True

    def get(self, key: str) -> str | None:
        with self.lock:
            if self._key_type(key) not in {"none", "string"}:
                raise TypeError("WRONGTYPE")
            return self.strings.get(key)

    def ttl(self, key: str) -> int:
        with self.lock:
            if self._key_type(key) == "none":
                return -2
            return self.expirations.get(key, -1)

    def eval(self, script: str, key_count: int, *values: str) -> Any:
        keys = values[:key_count]
        args = values[key_count:]
        with self.lock:
            if "WAIMAI_ENQUEUE_IDEMPOTENT" in script:
                return self._enqueue_idempotent(keys, args)
            if "WAIMAI_ENQUEUE" in script:
                return self._enqueue(keys, args)
            if "WAIMAI_CLAIM_NEXT" in script:
                return self._claim_next(keys, args)
            if "WAIMAI_HEARTBEAT" in script:
                return self._heartbeat(keys, args)
            if "WAIMAI_CANCEL_TASK" in script:
                return self._cancel(keys, args)
            if "WAIMAI_REQUEST_CANCEL" in script:
                return self._request_cancel(keys, args)
            if "WAIMAI_FINISH_CLAIM" in script:
                return self._finish(keys, args)
            if "WAIMAI_RECOVER_RECEIPT" in script:
                return self._recover(keys, args)
        raise AssertionError("unexpected Lua script")

    def _enqueue(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> list[Any]:
        task_key, queue_key = keys
        if self._key_type(task_key) != "none":
            return [-3, 0]
        if self._key_type(queue_key) not in {"none", "list"}:
            return [-4, 0]
        now = str(self._now_ms())
        self.hashes[task_key] = {
            "task_id": args[0],
            "status": args[1],
            "image_url": "",
            "error": "",
            "attempts": "0",
            "payload_json": args[2],
            "queue_receipt": args[3],
            "cancel_requested": "0",
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "finished_at": "",
        }
        self.lpush(queue_key, args[3])
        return [1, 0]

    def _enqueue_idempotent(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> list[Any]:
        task_key, queue_key, claim_key = keys
        if self._key_type(queue_key) not in {"none", "list"}:
            return [-4, args[0]]
        if self._key_type(claim_key) not in {"none", "string"}:
            return [-4, args[0]]
        existing = self.strings.get(claim_key)
        if existing is not None:
            separator = existing.find(":")
            if separator < 0:
                return [-2, existing]
            existing_sha = existing[:separator]
            existing_task_id = existing[separator + 1 :]
            if existing_sha == args[10]:
                return [0, existing_task_id]
            return [-1, existing_task_id]
        if self._key_type(task_key) != "none":
            return [-3, args[0]]
        now = str(self._now_ms())
        fields = (
            "task_id",
            "status",
            "image_url",
            "error",
            "attempts",
            "payload_json",
        )
        task = {
            field: str(value)
            for field, value in zip(fields, args[:6])
        }
        task.update(
            {
                "created_at": now,
                "updated_at": now,
                "started_at": args[6],
                "finished_at": args[7],
                "owner_user_id": args[8],
                "idempotency_key_hash": args[9],
                "request_sha256": args[10],
                "queue_receipt": args[11],
                "cancel_requested": "0",
            }
        )
        self.hashes[task_key] = task
        self.lpush(queue_key, args[11])
        self.strings[claim_key] = f"{args[10]}:{args[0]}"
        return [1, args[0]]

    def _claim_next(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> list[Any]:
        queue_key, processing_key, dead_letter_key = keys
        if any(
            self._key_type(key) not in {"none", "list"} for key in keys
        ):
            return [-4, "", "", 0, 0, 0]
        for _index in range(100):
            receipt = self.rpoplpush(queue_key, processing_key)
            if receipt is None:
                return [0, "", "", 0, 0, 0]
            try:
                message = json.loads(receipt)
            except json.JSONDecodeError:
                message = {}
            task_id = str(message.get("task_id") or "")
            task = self.hashes.get(f"{args[0]}{task_id}")
            if task is not None and task.get("status") == "pending":
                attempts = int(task.get("attempts") or 0) + 1
                now = self._now_ms()
                expires = now + int(args[3])
                task.update(
                    {
                        "status": "running",
                        "attempts": str(attempts),
                        "worker_id": args[1],
                        "lease_token": args[2],
                        "updated_at": str(now),
                        "lease_expires_at": str(expires),
                    }
                )
                if not task.get("started_at"):
                    task["started_at"] = str(now)
                return [1, receipt, task_id, attempts, now, expires]
            self.lrem(processing_key, 1, receipt)
            if task is None or task.get("status") not in {"done", "failed"}:
                self.lpush(dead_letter_key, receipt)
        return [0, "", "", 0, 0, 0]

    def _heartbeat(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> int:
        task = self.hashes.get(keys[0], {})
        if (
            task.get("status") != "running"
            or task.get("lease_token") != args[0]
        ):
            return 0
        now = self._now_ms()
        task["updated_at"] = str(now)
        task["lease_expires_at"] = str(now + int(args[1]))
        if args[2]:
            task["attempts"] = args[2]
        if args[3]:
            task["error"] = args[3]
        return 1

    def _finish(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> int:
        if self._key_type(keys[1]) not in {"none", "list"}:
            return -4
        task = self.hashes.get(keys[0], {})
        if (
            task.get("status") != "running"
            or task.get("lease_token") != args[1]
        ):
            return 0
        if task.get("cancel_requested") == "1" and args[9] != "1":
            return -2
        now = str(self._now_ms())
        task.update(
            {
                "status": args[2],
                "image_url": args[3],
                "error": args[4],
                "result_json": args[5],
                "updated_at": now,
                "finished_at": now,
                "worker_id": "",
                "lease_token": "",
                "lease_expires_at": "",
            }
        )
        if args[6]:
            task["attempts"] = args[6]
        self.lrem(keys[1], 1, args[0])
        if int(args[7]) > 0:
            self.expire(keys[0], int(args[7]))
        if keys[2] != keys[0] and int(args[8]) > 0:
            self.expire(keys[2], int(args[8]))
        return 1

    def _request_cancel(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> int:
        task = self.hashes.get(keys[0])
        if task is None:
            return -1
        if any(self._key_type(key) not in {"none", "list"} for key in keys[1:3]):
            return -4
        if task.get("status") in {"done", "failed"}:
            return 2
        now = str(self._now_ms())
        if task.get("status") == "running":
            task.update(
                {
                    "cancel_requested": "1",
                    "error": "cancel_requested",
                    "updated_at": now,
                }
            )
            return 3
        receipt = task.get("queue_receipt", "")
        if receipt:
            self.lrem(keys[1], 1, receipt)
            self.lrem(keys[2], 1, receipt)
        task.update(
            {
                "status": "failed",
                "image_url": "",
                "error": "canceled",
                "result_json": '{"canceled":true}',
                "cancel_requested": "1",
                "updated_at": now,
                "finished_at": now,
                "worker_id": "",
                "lease_token": "",
                "lease_expires_at": "",
            }
        )
        if int(args[0]) > 0:
            self.expire(keys[0], int(args[0]))
        if keys[3] != keys[0] and int(args[1]) > 0:
            self.expire(keys[3], int(args[1]))
        return 1

    def _cancel(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> int:
        task = self.hashes.get(keys[0])
        if task is None:
            return -1
        if any(self._key_type(key) not in {"none", "list"} for key in keys[1:3]):
            return -4
        if task.get("status") in {"done", "failed"}:
            return 2
        receipt = task.get("queue_receipt", "")
        if receipt:
            self.lrem(keys[1], 1, receipt)
            self.lrem(keys[2], 1, receipt)
        now = str(self._now_ms())
        task.update(
            {
                "status": "failed",
                "image_url": "",
                "error": "canceled",
                "result_json": '{"canceled":true}',
                "updated_at": now,
                "finished_at": now,
                "worker_id": "",
                "lease_token": "",
                "lease_expires_at": "",
            }
        )
        if int(args[0]) > 0:
            self.expire(keys[0], int(args[0]))
        if keys[3] != keys[0] and int(args[1]) > 0:
            self.expire(keys[3], int(args[1]))
        return 1

    def _recover(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> int:
        if (
            self._key_type(keys[1]) not in {"none", "list"}
            or self._key_type(keys[2]) not in {"none", "list"}
        ):
            return -4
        task = self.hashes.get(keys[0])
        if task is None:
            self.lrem(keys[1], 1, args[0])
            return 4
        if task.get("status") in {"done", "failed"}:
            self.lrem(keys[1], 1, args[0])
            return 3
        now = self._now_ms()
        lease_expires_at = int(task.get("lease_expires_at") or 0)
        if task.get("status") == "running" and lease_expires_at > now:
            return 0
        if task.get("cancel_requested") == "1":
            task.update(
                {
                    "status": "failed",
                    "error": "canceled",
                    "result_json": '{"canceled":true}',
                    "updated_at": str(now),
                    "finished_at": str(now),
                    "worker_id": "",
                    "lease_token": "",
                    "lease_expires_at": "",
                }
            )
            self.lrem(keys[1], 1, args[0])
            return 2
        if int(task.get("attempts") or 0) >= int(args[1]):
            task.update(
                {
                    "status": "failed",
                    "error": "task lease expired after maximum attempts",
                    "updated_at": str(now),
                    "finished_at": str(now),
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
                "updated_at": str(now),
                "worker_id": "",
                "lease_token": "",
                "lease_expires_at": "",
            }
        )
        self.lrem(keys[1], 1, args[0])
        self.rpush(keys[2], args[0])
        return 1

    def _key_type(self, key: str) -> str:
        if key in self.hashes:
            return "hash"
        if key in self.lists:
            return "list"
        if key in self.strings:
            return "string"
        return "none"

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
