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
from shared.product_growth_outbox import ProductGrowthOutbox
from shared.redis_queue import RedisTaskQueue, product_queue_from_env
from worker.growth_event_processor import (
    GrowthEventHandler,
    process_growth_events_once,
)


LOGGER = logging.getLogger("waimai.growth-event-worker")
GROWTH_WORKER_SERVICE_ID = "growth-event-worker"
MAX_BATCH_SIZE = 100
MAX_LEASE_SECONDS = 86400
MAX_RETRY_SECONDS = 604800
MAX_HEARTBEAT_TTL_SECONDS = 3600

ConnectionFactory = Callable[[], Any]
GrowthHandlerFactory = Callable[[], GrowthEventHandler]
QueueFactory = Callable[[], RedisTaskQueue]


class GrowthWorkerError(RuntimeError):
    pass


def growth_connection_from_env(
    env: Mapping[str, str] | None = None,
) -> Any:
    values = os.environ if env is None else env
    try:
        connection = connect_postgres(postgres_config_from_env(values))
    except PostgresRuntimeError as exc:
        raise GrowthWorkerError(
            f"PostgreSQL unavailable for growth event worker: {exc.code}"
        ) from exc
    if bool(getattr(connection, "autocommit", False)):
        _close_connection(connection)
        raise GrowthWorkerError(
            "growth event worker requires autocommit-disabled PostgreSQL"
        )
    return connection


def build_default_growth_handler() -> GrowthEventHandler:
    try:
        from worker.growth_business_handler import (
            build_growth_business_handler,
        )
    except ImportError as exc:
        raise GrowthWorkerError(
            "growth business handler is not available"
        ) from exc
    handler = build_growth_business_handler()
    if not callable(handler):
        raise GrowthWorkerError(
            "growth business handler factory must return a callable"
        )
    return handler


def run_growth_worker_loop(
    *,
    connection_factory: ConnectionFactory,
    queue_factory: QueueFactory,
    handler_factory: GrowthHandlerFactory,
    stop_event: Event,
    worker_id: str,
    batch_limit: int = 10,
    lease_seconds: int = 60,
    retry_in_seconds: int = 30,
    idle_seconds: float = 1.0,
    initial_backoff_seconds: float = 0.5,
    max_backoff_seconds: float = 30.0,
    heartbeat_ttl_seconds: int = 30,
    wait: Callable[[float], bool] | None = None,
) -> None:
    clean_worker_id = _required_text(worker_id, "worker_id")
    clean_batch_limit = _bounded_positive_int(
        batch_limit,
        "batch_limit",
        maximum=MAX_BATCH_SIZE,
    )
    clean_lease_seconds = _bounded_positive_int(
        lease_seconds,
        "lease_seconds",
        maximum=MAX_LEASE_SECONDS,
    )
    clean_retry_seconds = _bounded_nonnegative_int(
        retry_in_seconds,
        "retry_in_seconds",
        maximum=MAX_RETRY_SECONDS,
    )
    idle_delay = _positive_float(idle_seconds, "idle_seconds")
    initial_backoff = _positive_float(
        initial_backoff_seconds,
        "initial_backoff_seconds",
    )
    max_backoff = max(
        initial_backoff,
        _positive_float(max_backoff_seconds, "max_backoff_seconds"),
    )
    heartbeat_ttl = _bounded_positive_int(
        heartbeat_ttl_seconds,
        "heartbeat_ttl_seconds",
        maximum=MAX_HEARTBEAT_TTL_SECONDS,
    )
    wait_for_stop = wait or stop_event.wait

    handler: GrowthEventHandler | None = None
    connection: Any | None = None
    outbox: ProductGrowthOutbox | None = None
    queue: RedisTaskQueue | None = None
    consecutive_failures = 0
    try:
        while not stop_event.is_set():
            try:
                if handler is None:
                    handler = handler_factory()
                    if not callable(handler):
                        raise GrowthWorkerError(
                            "growth handler factory must return a callable"
                        )
                if connection is None:
                    connection = connection_factory()
                    if bool(getattr(connection, "autocommit", False)):
                        raise GrowthWorkerError(
                            "growth event worker requires "
                            "autocommit-disabled PostgreSQL"
                        )
                    outbox = ProductGrowthOutbox(connection)
                if queue is None:
                    queue = queue_factory()

                report = process_growth_events_once(
                    outbox,
                    connection,
                    handler=handler,
                    worker_id=clean_worker_id,
                    limit=clean_batch_limit,
                    lease_seconds=clean_lease_seconds,
                    retry_in_seconds=clean_retry_seconds,
                )
                queue.publish_service_heartbeat(
                    service_id=GROWTH_WORKER_SERVICE_ID,
                    instance_id=clean_worker_id,
                    ttl_seconds=heartbeat_ttl,
                )
                consecutive_failures = 0
                if int(report.get("claimed") or 0) == 0:
                    wait_for_stop(idle_delay)
            except Exception:  # noqa: BLE001 - process boundary
                consecutive_failures += 1
                LOGGER.exception(
                    "growth event worker cycle failed",
                    extra={
                        "consecutive_failures": consecutive_failures,
                    },
                )
                _close_connection(connection)
                connection = None
                outbox = None
                queue = None
                delay = min(
                    max_backoff,
                    initial_backoff * (2 ** (consecutive_failures - 1)),
                )
                wait_for_stop(delay)
    finally:
        _close_connection(connection)


def main() -> int:
    logging.basicConfig(
        level=str(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    values = dict(os.environ)
    stop_event = Event()
    _install_signal_handlers(stop_event)
    worker_id = (
        str(values.get("GROWTH_WORKER_ID") or "").strip()
        or (
            f"growth-{socket.gethostname()}-{os.getpid()}-"
            f"{uuid4().hex[:8]}"
        )
    )
    run_growth_worker_loop(
        connection_factory=lambda: growth_connection_from_env(values),
        queue_factory=lambda: product_queue_from_env(values),
        handler_factory=build_default_growth_handler,
        stop_event=stop_event,
        worker_id=worker_id,
        batch_limit=_env_bounded_positive_int(
            values,
            "GROWTH_WORKER_BATCH_SIZE",
            10,
            maximum=MAX_BATCH_SIZE,
        ),
        lease_seconds=_env_bounded_positive_int(
            values,
            "GROWTH_WORKER_LEASE_SECONDS",
            60,
            maximum=MAX_LEASE_SECONDS,
        ),
        retry_in_seconds=_env_bounded_nonnegative_int(
            values,
            "GROWTH_WORKER_RETRY_SECONDS",
            30,
            maximum=MAX_RETRY_SECONDS,
        ),
        idle_seconds=_env_positive_float(
            values,
            "GROWTH_WORKER_IDLE_SECONDS",
            1.0,
        ),
        initial_backoff_seconds=_env_positive_float(
            values,
            "GROWTH_WORKER_INITIAL_BACKOFF_SECONDS",
            0.5,
        ),
        max_backoff_seconds=_env_positive_float(
            values,
            "GROWTH_WORKER_MAX_BACKOFF_SECONDS",
            30.0,
        ),
        heartbeat_ttl_seconds=_env_bounded_positive_int(
            values,
            "GROWTH_WORKER_HEARTBEAT_TTL_SECONDS",
            30,
            maximum=MAX_HEARTBEAT_TTL_SECONDS,
        ),
    )
    return 0


def _install_signal_handlers(stop_event: Event) -> None:
    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _close_connection(connection: Any | None) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup remains best effort
            LOGGER.warning(
                "failed to close growth worker PostgreSQL connection",
                exc_info=True,
            )


def _required_text(value: Any, field: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or len(clean) > 200 or any(ord(char) < 32 for char in clean):
        raise GrowthWorkerError(
            f"{field} must be a non-empty bounded string"
        )
    return clean


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise GrowthWorkerError(f"{field} must be positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GrowthWorkerError(f"{field} must be positive") from exc
    if number <= 0:
        raise GrowthWorkerError(f"{field} must be positive")
    return number


def _bounded_positive_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise GrowthWorkerError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise GrowthWorkerError(
            f"{field} must be a positive integer"
        ) from exc
    if number <= 0 or number > maximum:
        raise GrowthWorkerError(
            f"{field} must be between 1 and {maximum}"
        )
    return number


def _bounded_nonnegative_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise GrowthWorkerError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise GrowthWorkerError(
            f"{field} must be a non-negative integer"
        ) from exc
    if number < 0 or number > maximum:
        raise GrowthWorkerError(
            f"{field} must be between 0 and {maximum}"
        )
    return number


def _env_bounded_positive_int(
    values: Mapping[str, str],
    key: str,
    default: int,
    *,
    maximum: int,
) -> int:
    return _bounded_positive_int(
        str(values.get(key) or default).strip(),
        key,
        maximum=maximum,
    )


def _env_bounded_nonnegative_int(
    values: Mapping[str, str],
    key: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = values.get(key)
    return _bounded_nonnegative_int(
        str(default if raw is None or str(raw).strip() == "" else raw).strip(),
        key,
        maximum=maximum,
    )


def _env_positive_float(
    values: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    return _positive_float(
        str(values.get(key) or default).strip(),
        key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
