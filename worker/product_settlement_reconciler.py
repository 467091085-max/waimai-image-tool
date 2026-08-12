from __future__ import annotations

import logging
import os
import signal
import socket
import sys
from pathlib import Path
from threading import Event
from typing import Any, Callable, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.postgres_runtime import (
    PostgresRuntimeError,
    connect_postgres,
    postgres_config_from_env,
)
from shared.product_generation_settlement import (
    ProductGenerationSettlementError,
    apply_generation_completion,
    completion_from_redis_task,
    settle_terminal_job,
)
from shared.product_job_store import (
    ProductJobStore,
    ProductJobStoreError,
)
from shared.product_revision_settlement import (
    revision_completion_from_redis_task,
)
from shared.redis_queue import (
    QueueError,
    RedisTaskQueue,
    TaskNotFound,
    product_queue_from_env,
    revision_queue_from_env,
)


LOGGER = logging.getLogger("waimai.product-settlement-reconciler")
RECONCILER_SERVICE_ID = "product-settlement-reconciler"
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}
MENU_BATCH_JOB_TYPE = "menu_batch_generation"
REVISION_BATCH_JOB_TYPE = "delivery_asset_revision_batch"


class ProductSettlementReconcilerError(RuntimeError):
    pass


def product_job_store_from_env(
    env: Mapping[str, str] | None = None,
) -> ProductJobStore:
    values = os.environ if env is None else env
    try:
        connection = connect_postgres(postgres_config_from_env(values))
    except PostgresRuntimeError as exc:
        raise ProductSettlementReconcilerError(
            f"PostgreSQL unavailable for settlement reconciler: {exc.code}"
        ) from exc
    return ProductJobStore(connection)


def reconcile_once(
    store: Any,
    queue: RedisTaskQueue,
    *,
    revision_queue: RedisTaskQueue | None = None,
    reconciler_id: str,
    limit: int = 100,
    settlement_reclaim_after_seconds: int = 300,
    object_store: Any | None = None,
) -> dict[str, Any]:
    clean_reconciler_id = _required_text(reconciler_id, "reconciler_id")
    batch_limit = _bounded_positive_int(limit, "limit", maximum=1000)
    reclaim_after = _positive_int(
        settlement_reclaim_after_seconds,
        "settlement_reclaim_after_seconds",
    )
    candidates = store.list_reconciliation_candidates(limit=batch_limit)
    if not isinstance(candidates, list):
        raise ProductSettlementReconcilerError(
            "list_reconciliation_candidates must return a list"
        )

    report: dict[str, Any] = {
        "candidates": len(candidates),
        "completed": 0,
        "settled": 0,
        "active": 0,
        "waitingOutbox": 0,
        "requeuedOutbox": 0,
        "deferred": 0,
        "failed": 0,
        "failures": [],
    }
    for raw_candidate in candidates:
        try:
            candidate = _candidate(raw_candidate)
            if candidate["job_status"] in TERMINAL_JOB_STATUSES:
                settle_terminal_job(
                    store,
                    job_id=candidate["job_id"],
                    owner_user_id=candidate["owner_user_id"],
                    reconciler_id=clean_reconciler_id,
                    reclaim_after_seconds=reclaim_after,
                )
                report["settled"] += 1
                continue

            destination = (
                revision_queue
                if (
                    candidate["job_type"] == REVISION_BATCH_JOB_TYPE
                    and revision_queue is not None
                )
                else queue
            )
            try:
                task = destination.get(candidate["job_id"])
            except TaskNotFound:
                if (
                    candidate["job_status"] == "running"
                    and candidate["outbox_status"] == "published"
                ):
                    requeued = (
                        store.requeue_published_outbox_if_task_missing(
                            job_id=candidate["job_id"],
                            expected_fence=candidate["fence"],
                        )
                    )
                    if requeued is None:
                        report["deferred"] += 1
                    else:
                        report["requeuedOutbox"] += 1
                    continue
                if candidate["outbox_status"] in {"pending", "claimed"}:
                    report["waitingOutbox"] += 1
                    continue
                raise ProductSettlementReconcilerError(
                    "nonterminal PostgreSQL job has no recoverable Redis task"
                )

            completion = completion_for_redis_task(
                task,
                object_store=object_store,
            )
            if completion is None:
                report["active"] += 1
                continue
            if (
                completion.job_id != candidate["job_id"]
                or completion.owner_user_id != candidate["owner_user_id"]
                or completion.request_sha256
                != candidate["request_sha256"]
                or completion.fence != candidate["fence"]
            ):
                raise ProductSettlementReconcilerError(
                    "Redis completion does not match the PostgreSQL candidate"
                )
            apply_generation_completion(
                store,
                completion,
                reconciler_id=clean_reconciler_id,
                settlement_reclaim_after_seconds=reclaim_after,
            )
            report["completed"] += 1
            report["settled"] += 1
        except (
            ProductGenerationSettlementError,
            ProductJobStoreError,
            ProductSettlementReconcilerError,
            QueueError,
            TypeError,
            ValueError,
        ) as exc:
            job_id = (
                str(raw_candidate.get("job_id") or "")
                if isinstance(raw_candidate, Mapping)
                else ""
            )
            report["failed"] += 1
            report["failures"].append(
                {
                    "jobId": job_id,
                    "errorType": type(exc).__name__,
                }
            )
            LOGGER.exception(
                "product settlement reconciliation failed",
                extra={
                    "job_id": job_id,
                    "error_type": type(exc).__name__,
                },
            )
    return report


def completion_for_redis_task(
    task: Mapping[str, Any],
    *,
    object_store: Any | None,
):
    payload = task.get("payload")
    task_type = (
        str(payload.get("taskType") or "").strip()
        if isinstance(payload, Mapping)
        else ""
    )
    if task_type == "product_revision":
        return revision_completion_from_redis_task(
            task,
            object_store=object_store,
        )
    return completion_from_redis_task(
        task,
        object_store=object_store,
    )


def run_reconciler_loop(
    *,
    store_factory: Callable[[], Any],
    queue_factory: Callable[[], RedisTaskQueue],
    revision_queue_factory: Callable[[], RedisTaskQueue] | None = None,
    stop_event: Event,
    reconciler_id: str,
    batch_limit: int = 100,
    idle_seconds: float = 1.0,
    initial_backoff_seconds: float = 0.5,
    max_backoff_seconds: float = 30.0,
    heartbeat_ttl_seconds: int = 30,
    settlement_reclaim_after_seconds: int = 300,
    object_store: Any | None = None,
    wait: Callable[[float], bool] | None = None,
) -> None:
    idle_delay = _positive_float(idle_seconds, "idle_seconds")
    initial_backoff = _positive_float(
        initial_backoff_seconds,
        "initial_backoff_seconds",
    )
    max_backoff = max(
        initial_backoff,
        _positive_float(max_backoff_seconds, "max_backoff_seconds"),
    )
    heartbeat_ttl = _positive_int(
        heartbeat_ttl_seconds,
        "heartbeat_ttl_seconds",
    )
    wait_for_stop = wait or stop_event.wait
    store: Any | None = None
    queue: RedisTaskQueue | None = None
    revision_queue: RedisTaskQueue | None = None
    failures = 0
    try:
        while not stop_event.is_set():
            try:
                if store is None:
                    store = store_factory()
                if queue is None:
                    queue = queue_factory()
                if revision_queue is None:
                    revision_queue = (
                        revision_queue_factory()
                        if revision_queue_factory is not None
                        else queue
                    )
                report = reconcile_once(
                    store,
                    queue,
                    revision_queue=revision_queue,
                    reconciler_id=reconciler_id,
                    limit=batch_limit,
                    settlement_reclaim_after_seconds=(
                        settlement_reclaim_after_seconds
                    ),
                    object_store=object_store,
                )
                queue.publish_service_heartbeat(
                    service_id=RECONCILER_SERVICE_ID,
                    instance_id=reconciler_id,
                    ttl_seconds=heartbeat_ttl,
                )
                failures = 0
                if int(report["candidates"]) == 0:
                    wait_for_stop(idle_delay)
            except Exception:  # noqa: BLE001 - process boundary
                failures += 1
                LOGGER.exception(
                    "product settlement reconciliation cycle failed",
                    extra={"consecutive_failures": failures},
                )
                _close_store(store)
                store = None
                queue = None
                revision_queue = None
                delay = min(
                    max_backoff,
                    initial_backoff * (2 ** (failures - 1)),
                )
                wait_for_stop(delay)
    finally:
        _close_store(store)


def main() -> int:
    logging.basicConfig(
        level=str(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    values = dict(os.environ)
    stop_event = Event()
    _install_signal_handlers(stop_event)
    reconciler_id = (
        str(values.get("PRODUCT_SETTLEMENT_RECONCILER_ID") or "").strip()
        or (
            f"settlement-{socket.gethostname()}-{os.getpid()}-"
            f"{uuid4().hex[:8]}"
        )
    )
    run_reconciler_loop(
        store_factory=lambda: product_job_store_from_env(values),
        queue_factory=lambda: product_queue_from_env(values),
        revision_queue_factory=lambda: revision_queue_from_env(values),
        stop_event=stop_event,
        reconciler_id=reconciler_id,
        batch_limit=_env_positive_int(
            values,
            "PRODUCT_SETTLEMENT_BATCH_SIZE",
            100,
        ),
        idle_seconds=_env_positive_float(
            values,
            "PRODUCT_SETTLEMENT_IDLE_SECONDS",
            1.0,
        ),
        initial_backoff_seconds=_env_positive_float(
            values,
            "PRODUCT_SETTLEMENT_INITIAL_BACKOFF_SECONDS",
            0.5,
        ),
        max_backoff_seconds=_env_positive_float(
            values,
            "PRODUCT_SETTLEMENT_MAX_BACKOFF_SECONDS",
            30.0,
        ),
        heartbeat_ttl_seconds=_env_positive_int(
            values,
            "PRODUCT_SETTLEMENT_HEARTBEAT_TTL_SECONDS",
            30,
        ),
        settlement_reclaim_after_seconds=_env_positive_int(
            values,
            "PRODUCT_SETTLEMENT_RECLAIM_SECONDS",
            300,
        ),
    )
    return 0


def _candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductSettlementReconcilerError(
            "reconciliation candidate must be an object"
        )
    job_status = _required_text(value.get("job_status"), "job_status")
    if job_status not in {
        "queued",
        "running",
        "succeeded",
        "failed",
        "canceled",
    }:
        raise ProductSettlementReconcilerError(
            f"unsupported PostgreSQL job status: {job_status}"
        )
    outbox_status = str(value.get("outbox_status") or "").strip()
    if outbox_status not in {
        "pending",
        "claimed",
        "published",
        "canceled",
    }:
        raise ProductSettlementReconcilerError(
            f"unsupported PostgreSQL outbox status: {outbox_status or 'missing'}"
        )
    fence = _nonnegative_int(value.get("fence"), "fence")
    if job_status == "running" and fence <= 0:
        raise ProductSettlementReconcilerError(
            "running PostgreSQL job must have a positive fence"
        )
    job_type = _required_text(value.get("job_type"), "job_type")
    if job_type not in {MENU_BATCH_JOB_TYPE, REVISION_BATCH_JOB_TYPE}:
        raise ProductSettlementReconcilerError(
            f"unsupported PostgreSQL job type: {job_type}"
        )
    return {
        "job_id": _required_text(value.get("job_id"), "job_id"),
        "owner_user_id": _required_text(
            value.get("owner_user_id"),
            "owner_user_id",
        ),
        "request_sha256": _sha256(
            value.get("request_sha256"),
            "request_sha256",
        ),
        "job_type": job_type,
        "job_status": job_status,
        "fence": fence,
        "outbox_status": outbox_status,
    }


def _install_signal_handlers(stop_event: Event) -> None:
    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _close_store(store: Any | None) -> None:
    connection = getattr(store, "connection", None)
    close = getattr(connection, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup remains best effort
            LOGGER.warning(
                "failed to close PostgreSQL connection",
                exc_info=True,
            )


def _required_text(value: Any, field: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or len(clean) > 512 or any(ord(char) < 32 for char in clean):
        raise ProductSettlementReconcilerError(
            f"{field} must be a non-empty bounded string"
        )
    return clean


def _sha256(value: Any, field: str) -> str:
    clean = str(value or "").strip().lower()
    if len(clean) != 64 or any(
        character not in "0123456789abcdef" for character in clean
    ):
        raise ProductSettlementReconcilerError(
            f"{field} must be a SHA-256 digest"
        )
    return clean


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProductSettlementReconcilerError(
            f"{field} must be a positive integer"
        )
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductSettlementReconcilerError(
            f"{field} must be a positive integer"
        ) from exc
    if number <= 0:
        raise ProductSettlementReconcilerError(
            f"{field} must be a positive integer"
        )
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProductSettlementReconcilerError(
            f"{field} must be a non-negative integer"
        )
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductSettlementReconcilerError(
            f"{field} must be a non-negative integer"
        ) from exc
    if number < 0:
        raise ProductSettlementReconcilerError(
            f"{field} must be a non-negative integer"
        )
    return number


def _bounded_positive_int(value: Any, field: str, *, maximum: int) -> int:
    number = _positive_int(value, field)
    if number > maximum:
        raise ProductSettlementReconcilerError(
            f"{field} must not exceed {maximum}"
        )
    return number


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ProductSettlementReconcilerError(f"{field} must be positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductSettlementReconcilerError(
            f"{field} must be positive"
        ) from exc
    if number <= 0:
        raise ProductSettlementReconcilerError(
            f"{field} must be positive"
        )
    return number


def _env_positive_int(
    values: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    return _positive_int(str(values.get(key) or default).strip(), key)


def _env_positive_float(
    values: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    return _positive_float(str(values.get(key) or default).strip(), key)


if __name__ == "__main__":
    raise SystemExit(main())
