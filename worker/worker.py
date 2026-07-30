from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.generator import generate_image
from shared.redis_queue import (
    CancellationRequested,
    LeaseLost,
    QueueError,
    RedisTaskQueue,
    product_queue_from_env,
    queue_from_env,
)
from worker.product_batch_handler import (
    NonRetryableProductBatchError,
    PRODUCT_BATCH_TASK_TYPE,
    ProductBatchCancellationRequested,
    handle_product_batch,
)
from worker.product_revision_handler import (
    PRODUCT_REVISION_TASK_TYPE,
    handle_product_revision,
)


LOGGER = logging.getLogger("waimai.worker")


GenerationHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def dispatch_generation(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    task_type = str(payload.get("taskType") or "")
    if task_type == PRODUCT_BATCH_TASK_TYPE:
        return handle_product_batch(payload)
    if task_type == PRODUCT_REVISION_TASK_TYPE:
        return handle_product_revision(payload)
    if task_type in {"", "prompt_generation"} and str(
        payload.get("prompt") or ""
    ).strip():
        return generate_image(payload)
    raise QueueError(f"unsupported generation task type: {task_type or 'missing'}")


class GenerationWorker:
    def __init__(
        self,
        queue: RedisTaskQueue,
        *,
        handler: GenerationHandler | None = None,
        max_retries: int = 2,
        task_timeout_seconds: float = 60,
        product_batch_timeout_seconds: float = 3600,
        recovery_stale_seconds: float = 60,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        service_id: str = "generation-worker",
        service_heartbeat_ttl_seconds: int = 30,
    ) -> None:
        self.queue = queue
        self.handler = handler or dispatch_generation
        self.max_retries = max(0, int(max_retries))
        self.task_timeout_seconds = max(0.001, float(task_timeout_seconds))
        self.product_batch_timeout_seconds = max(
            self.task_timeout_seconds,
            float(product_batch_timeout_seconds),
        )
        self.recovery_stale_seconds = max(0.001, float(recovery_stale_seconds))
        self.worker_id = str(worker_id or f"worker-{uuid4()}").strip()
        self.service_id = str(service_id or "generation-worker").strip()
        self.service_heartbeat_ttl_seconds = max(
            3,
            int(service_heartbeat_ttl_seconds),
        )
        self.lease_seconds = max(
            self.task_timeout_seconds + 5,
            float(
                lease_seconds
                if lease_seconds is not None
                else max(
                    self.recovery_stale_seconds,
                    self.task_timeout_seconds + 5,
                )
            ),
        )

    def process_one(self, *, timeout_seconds: int = 5) -> bool:
        self.recover_stale_tasks()
        message = self.queue.claim(
            worker_id=self.worker_id,
            lease_ms=int(self.lease_seconds * 1000),
            timeout_seconds=timeout_seconds,
        )
        if message is None:
            return False
        task_id = str(message["task_id"])
        payload = dict(message.get("payload") or {})
        payload.setdefault("task_id", task_id)
        receipt = str(message["receipt"])
        lease_token = str(message["lease_token"])
        attempts = int(message["attempts"])
        if str(payload.get("taskType") or "") in {
            PRODUCT_BATCH_TASK_TYPE,
            PRODUCT_REVISION_TASK_TYPE,
        }:
            payload["_executionGuard"] = lambda: self._guard_product_execution(
                task_id,
                lease_token,
            )
        while True:
            try:
                result = dict(
                    self._run_handler_with_lease(
                        payload,
                        task_id=task_id,
                        lease_token=lease_token,
                        attempts=attempts,
                    )
                )
                image_url = str(result.get("image_url") or result.get("imageUrl") or "").strip()
                if not image_url:
                    raise QueueError("worker result missing image_url")
            except LeaseLost:
                LOGGER.warning(
                    "generation task lease lost during provider execution",
                    extra={"task_id": task_id},
                )
                return True
            except ProductBatchCancellationRequested:
                LOGGER.info(
                    "product batch cancellation acknowledged",
                    extra={"task_id": task_id, "attempt": attempts},
                )
                try:
                    self.queue.ack_canceled(
                        task_id,
                        receipt=receipt,
                        lease_token=lease_token,
                        attempts=attempts,
                    )
                except LeaseLost:
                    LOGGER.warning(
                        "generation task lease lost before cancellation acknowledgement",
                        extra={"task_id": task_id},
                    )
                return True
            except TimeoutError as exc:
                LOGGER.exception(
                    "generation task timed out",
                    extra={"task_id": task_id, "attempt": attempts},
                )
                try:
                    self.queue.ack_failed(
                        task_id,
                        receipt=receipt,
                        lease_token=lease_token,
                        error=str(exc),
                        attempts=attempts,
                    )
                except LeaseLost:
                    LOGGER.warning(
                        "generation task lease lost before timeout acknowledgement",
                        extra={"task_id": task_id},
                    )
                return True
            except NonRetryableProductBatchError as exc:
                LOGGER.exception(
                    "product batch failed permanently",
                    extra={"task_id": task_id, "attempt": attempts},
                )
                try:
                    self.queue.ack_failed(
                        task_id,
                        receipt=receipt,
                        lease_token=lease_token,
                        error=str(exc),
                        attempts=attempts,
                    )
                except LeaseLost:
                    LOGGER.warning(
                        "generation task lease lost before permanent failure acknowledgement",
                        extra={"task_id": task_id},
                    )
                return True
            except Exception as exc:  # noqa: BLE001 - worker must capture provider failures
                LOGGER.exception("generation task failed", extra={"task_id": task_id, "attempt": attempts})
                if attempts > self.max_retries:
                    try:
                        self.queue.ack_failed(
                            task_id,
                            receipt=receipt,
                            lease_token=lease_token,
                            error=str(exc),
                            attempts=attempts,
                        )
                    except LeaseLost:
                        LOGGER.warning(
                            "generation task lease lost before failure acknowledgement",
                            extra={"task_id": task_id},
                        )
                    return True
                attempts += 1
                try:
                    self.queue.heartbeat(
                        task_id,
                        lease_token=lease_token,
                        lease_ms=int(self.lease_seconds * 1000),
                        attempts=attempts,
                        error=str(exc),
                    )
                except LeaseLost:
                    LOGGER.warning(
                        "generation task lease lost before retry",
                        extra={"task_id": task_id},
                    )
                    return True
                time.sleep(min(2 ** (attempts - 2), 5))
                continue
            try:
                self.queue.ack_done(
                    task_id,
                    receipt=receipt,
                    lease_token=lease_token,
                    image_url=image_url,
                    result=result,
                )
            except CancellationRequested:
                try:
                    self.queue.ack_canceled(
                        task_id,
                        receipt=receipt,
                        lease_token=lease_token,
                        attempts=attempts,
                    )
                except LeaseLost:
                    LOGGER.warning(
                        "generation task lease lost before cancellation acknowledgement",
                        extra={"task_id": task_id},
                    )
            except LeaseLost:
                LOGGER.warning(
                    "generation task lease lost before success acknowledgement",
                    extra={"task_id": task_id},
                )
            return True

    def _guard_product_execution(
        self,
        task_id: str,
        lease_token: str,
    ) -> None:
        task = self.queue.get(task_id)
        if (
            str(task.get("status") or "") != "running"
            or str(task.get("lease_token") or "") != lease_token
        ):
            raise LeaseLost(f"product batch lease is no longer owned: {task_id}")
        if task.get("cancel_requested") is True:
            raise ProductBatchCancellationRequested(
                f"product batch cancellation requested: {task_id}"
            )

    def recover_stale_tasks(self) -> dict[str, Any]:
        claimed = self.queue.recover_expired_claims(
            max_attempts=self.max_retries + 1,
        )
        legacy = self.queue.recover_stale_running(
            stale_after_ms=int(self.recovery_stale_seconds * 1000),
            max_attempts=self.max_retries + 1,
        )
        return {
            "recovered": claimed["recovered"] + legacy["recovered"],
            "failed": claimed["failed"] + legacy["failed"],
            "cleaned": claimed["cleaned"],
        }

    def _run_handler_with_timeout(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        timeout_seconds = (
            self.product_batch_timeout_seconds
            if str(payload.get("taskType") or "") in {
                PRODUCT_BATCH_TASK_TYPE,
                PRODUCT_REVISION_TASK_TYPE,
            }
            else self.task_timeout_seconds
        )
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="generation-provider")
        future = executor.submit(self.handler, payload)
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            LOGGER.error(
                "generation task exceeded its alert deadline; "
                "retaining the lease until the provider call exits",
                extra={"timeout_seconds": timeout_seconds},
            )
            return future.result()
        finally:
            executor.shutdown(wait=True, cancel_futures=False)

    def _run_handler_with_lease(
        self,
        payload: Mapping[str, Any],
        *,
        task_id: str,
        lease_token: str,
        attempts: int,
    ) -> Mapping[str, Any]:
        stop = Event()
        lease_lost = Event()
        heartbeat_error: list[BaseException] = []
        heartbeat_interval = max(
            0.1,
            min(self.lease_seconds / 3, 30),
        )

        def heartbeat_loop() -> None:
            while not stop.wait(heartbeat_interval):
                try:
                    self.queue.heartbeat(
                        task_id,
                        lease_token=lease_token,
                        lease_ms=int(self.lease_seconds * 1000),
                        attempts=attempts,
                    )
                except BaseException as exc:  # noqa: BLE001 - thread hands error to owner
                    heartbeat_error.append(exc)
                    lease_lost.set()
                    return

        heartbeat_thread = Thread(
            target=heartbeat_loop,
            name=f"lease-heartbeat-{task_id[:20]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = self._run_handler_with_timeout(payload)
        finally:
            stop.set()
            heartbeat_thread.join(timeout=1)
        if lease_lost.is_set():
            reason = heartbeat_error[0] if heartbeat_error else "heartbeat failed"
            raise LeaseLost(f"task lease heartbeat failed: {reason}")
        return result

    def publish_service_heartbeat(self) -> dict[str, Any]:
        return self.queue.publish_service_heartbeat(
            service_id=self.service_id,
            instance_id=self.worker_id,
            ttl_seconds=self.service_heartbeat_ttl_seconds,
        )

    def run_forever(self, *, timeout_seconds: int = 5) -> None:
        heartbeat_stop = Event()
        heartbeat_interval = max(
            1.0,
            self.service_heartbeat_ttl_seconds / 3,
        )

        def service_heartbeat_loop() -> None:
            while not heartbeat_stop.is_set():
                try:
                    self.publish_service_heartbeat()
                except Exception:  # noqa: BLE001 - readiness will expose heartbeat loss
                    LOGGER.exception("worker service heartbeat failed")
                heartbeat_stop.wait(heartbeat_interval)

        heartbeat_thread = Thread(
            target=service_heartbeat_loop,
            name=f"service-heartbeat-{self.service_id[:30]}",
            daemon=True,
        )
        heartbeat_thread.start()
        retry_delay = 0.5
        try:
            while True:
                try:
                    self.process_one(timeout_seconds=timeout_seconds)
                    retry_delay = 0.5
                except KeyboardInterrupt:
                    raise
                except Exception:  # noqa: BLE001 - transient Redis errors must not stop Worker
                    LOGGER.exception("worker loop failed; retrying")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    task_mode = str(os.environ.get("WORKER_TASK_MODE") or "prompt").strip().lower()
    queue = (
        product_queue_from_env()
        if task_mode == "product"
        else queue_from_env()
    )
    worker = GenerationWorker(
        queue,
        max_retries=int(os.environ.get("WORKER_MAX_RETRIES", "2")),
        task_timeout_seconds=float(os.environ.get("WORKER_TASK_TIMEOUT", "60")),
        product_batch_timeout_seconds=float(
            os.environ.get("WORKER_PRODUCT_BATCH_TIMEOUT", "3600")
        ),
        recovery_stale_seconds=float(os.environ.get("WORKER_RECOVERY_STALE_SECONDS", "60")),
        worker_id=os.environ.get("WORKER_ID"),
        lease_seconds=float(os.environ.get("WORKER_LEASE_SECONDS", "0")) or None,
        service_id=(
            str(os.environ.get("PRODUCT_WORKER_SERVICE_ID") or "product-worker")
            if task_mode == "product"
            else str(
                os.environ.get("GENERATION_WORKER_SERVICE_ID")
                or "generation-worker"
            )
        ),
        service_heartbeat_ttl_seconds=int(
            os.environ.get("WORKER_SERVICE_HEARTBEAT_TTL_SECONDS", "30")
        ),
    )
    worker.run_forever(timeout_seconds=int(os.environ.get("WORKER_BRPOP_TIMEOUT", "5")))


if __name__ == "__main__":
    main()
