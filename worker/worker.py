from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.generator import generate_image
from shared.redis_queue import QueueError, RedisTaskQueue, queue_from_env


LOGGER = logging.getLogger("waimai.worker")


GenerationHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class GenerationWorker:
    def __init__(
        self,
        queue: RedisTaskQueue,
        *,
        handler: GenerationHandler | None = None,
        max_retries: int = 2,
    ) -> None:
        self.queue = queue
        self.handler = handler or generate_image
        self.max_retries = max(0, int(max_retries))

    def process_one(self, *, timeout_seconds: int = 5) -> bool:
        message = self.queue.dequeue(timeout_seconds=timeout_seconds)
        if message is None:
            return False
        task_id = str(message["task_id"])
        payload = dict(message.get("payload") or {})
        payload.setdefault("task_id", task_id)
        attempts = 0
        while attempts <= self.max_retries:
            attempts += 1
            self.queue.mark_running(task_id, attempts=attempts)
            try:
                result = dict(self.handler(payload))
                image_url = str(result.get("image_url") or result.get("imageUrl") or "").strip()
                if not image_url:
                    raise QueueError("worker result missing image_url")
                self.queue.mark_done(task_id, image_url=image_url, result=result)
                return True
            except Exception as exc:  # noqa: BLE001 - worker must capture provider failures
                LOGGER.exception("generation task failed", extra={"task_id": task_id, "attempt": attempts})
                if attempts > self.max_retries:
                    self.queue.mark_failed(task_id, error=str(exc), attempts=attempts)
                    return True
                time.sleep(min(2 ** (attempts - 1), 5))
        return True

    def run_forever(self, *, timeout_seconds: int = 5) -> None:
        while True:
            self.process_one(timeout_seconds=timeout_seconds)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    worker = GenerationWorker(
        queue_from_env(),
        max_retries=int(os.environ.get("WORKER_MAX_RETRIES", "2")),
    )
    worker.run_forever(timeout_seconds=int(os.environ.get("WORKER_BRPOP_TIMEOUT", "5")))


if __name__ == "__main__":
    main()
