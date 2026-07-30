from __future__ import annotations

import signal
import sys
from threading import Event
from types import ModuleType
from typing import Any

import pytest

from shared.redis_queue import RedisQueueConfig, RedisTaskQueue
from tests.redis_test_double import RedisTestDouble
from worker import growth_worker


class FakeConnection:
    def __init__(self, *, autocommit: bool = False) -> None:
        self.autocommit = autocommit
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeQueue:
    def __init__(self, *, heartbeat_error: Exception | None = None) -> None:
        self.heartbeat_error = heartbeat_error
        self.heartbeats: list[dict[str, Any]] = []

    def publish_service_heartbeat(self, **kwargs: Any) -> dict[str, Any]:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.heartbeats.append(kwargs)
        return dict(kwargs)


def handler(_cursor: Any, _event: dict[str, Any]) -> dict[str, Any]:
    return {}


def test_growth_connection_uses_autocommit_disabled_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    captured: dict[str, Any] = {}

    def connector(*args: Any, **kwargs: Any) -> FakeConnection:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(
        growth_worker,
        "connect_postgres",
        lambda config: connector(
            config.database_url,
            connect_timeout=config.connect_timeout_seconds,
            application_name=config.application_name,
            autocommit=False,
        ),
    )

    resolved = growth_worker.growth_connection_from_env(
        {
            "PRODUCT_POSTGRES_ENABLED": "true",
            "DATABASE_URL": "postgresql://localhost/test",
        }
    )

    assert resolved is connection
    assert captured["kwargs"]["autocommit"] is False


def test_default_handler_factory_is_lazy_and_validates_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("worker.growth_business_handler")
    module.build_growth_business_handler = lambda: handler  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "worker.growth_business_handler",
        module,
    )

    assert growth_worker.build_default_growth_handler() is handler

    module.build_growth_business_handler = lambda: None  # type: ignore[attr-defined]
    with pytest.raises(
        growth_worker.GrowthWorkerError,
        match="must return a callable",
    ):
        growth_worker.build_default_growth_handler()


def test_loop_processes_bounded_batches_and_heartbeats_when_busy_and_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    queue = FakeQueue()
    stop_event = Event()
    calls: list[dict[str, Any]] = []
    reports = iter([{"claimed": 2}, {"claimed": 0}])
    waits: list[float] = []

    def process(*args: Any, **kwargs: Any) -> dict[str, int]:
        calls.append({"args": args, "kwargs": kwargs})
        return next(reports)

    def wait(delay: float) -> bool:
        waits.append(delay)
        stop_event.set()
        return True

    monkeypatch.setattr(
        growth_worker,
        "process_growth_events_once",
        process,
    )

    growth_worker.run_growth_worker_loop(
        connection_factory=lambda: connection,
        queue_factory=lambda: queue,
        handler_factory=lambda: handler,
        stop_event=stop_event,
        worker_id="growth-instance-1",
        batch_limit=7,
        lease_seconds=45,
        retry_in_seconds=12,
        idle_seconds=1.25,
        heartbeat_ttl_seconds=33,
        wait=wait,
    )

    assert len(calls) == 2
    assert calls[0]["args"][1] is connection
    assert calls[0]["kwargs"] == {
        "handler": handler,
        "worker_id": "growth-instance-1",
        "limit": 7,
        "lease_seconds": 45,
        "retry_in_seconds": 12,
    }
    assert queue.heartbeats == [
        {
            "service_id": "growth-event-worker",
            "instance_id": "growth-instance-1",
            "ttl_seconds": 33,
        },
        {
            "service_id": "growth-event-worker",
            "instance_id": "growth-instance-1",
            "ttl_seconds": 33,
        },
    ]
    assert waits == [1.25]
    assert connection.closed is True


def test_failed_cycles_back_off_reconnect_and_do_not_publish_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeConnection()
    second = FakeConnection()
    third = FakeConnection()
    connections = iter([first, second, third])
    queues = [FakeQueue(), FakeQueue(), FakeQueue()]
    queue_iter = iter(queues)
    stop_event = Event()
    waits: list[float] = []
    outcomes: list[Any] = [
        ConnectionError("database read failed"),
        RuntimeError("processor failed again"),
        {"claimed": 0},
    ]

    def process(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def wait(delay: float) -> bool:
        waits.append(delay)
        if len(waits) == 3:
            stop_event.set()
        return stop_event.is_set()

    monkeypatch.setattr(
        growth_worker,
        "process_growth_events_once",
        process,
    )

    growth_worker.run_growth_worker_loop(
        connection_factory=lambda: next(connections),
        queue_factory=lambda: next(queue_iter),
        handler_factory=lambda: handler,
        stop_event=stop_event,
        worker_id="growth-instance-2",
        idle_seconds=1.0,
        initial_backoff_seconds=0.2,
        max_backoff_seconds=1.0,
        wait=wait,
    )

    assert waits == [0.2, 0.4, 1.0]
    assert queues[0].heartbeats == []
    assert queues[1].heartbeats == []
    assert len(queues[2].heartbeats) == 1
    assert first.closed is True
    assert second.closed is True
    assert third.closed is True


def test_redis_heartbeat_failure_is_a_failed_cycle_without_extra_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    queue = FakeQueue(heartbeat_error=ConnectionError("redis unavailable"))
    stop_event = Event()
    waits: list[float] = []

    monkeypatch.setattr(
        growth_worker,
        "process_growth_events_once",
        lambda *_args, **_kwargs: {"claimed": 1},
    )

    def wait(delay: float) -> bool:
        waits.append(delay)
        stop_event.set()
        return True

    growth_worker.run_growth_worker_loop(
        connection_factory=lambda: connection,
        queue_factory=lambda: queue,
        handler_factory=lambda: handler,
        stop_event=stop_event,
        worker_id="growth-instance-3",
        initial_backoff_seconds=0.3,
        wait=wait,
    )

    assert queue.heartbeats == []
    assert waits == [0.3]
    assert connection.closed is True


def test_loop_publishes_product_queue_heartbeat_with_real_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    redis = RedisTestDouble()
    queue = RedisTaskQueue(
        redis,
        RedisQueueConfig(
            namespace="waimai:test",
            queue_name="product-generate",
        ),
    )
    stop_event = Event()
    monkeypatch.setattr(
        growth_worker,
        "process_growth_events_once",
        lambda *_args, **_kwargs: {"claimed": 0},
    )

    growth_worker.run_growth_worker_loop(
        connection_factory=lambda: connection,
        queue_factory=lambda: queue,
        handler_factory=lambda: handler,
        stop_event=stop_event,
        worker_id="growth-instance-ttl",
        heartbeat_ttl_seconds=27,
        wait=lambda _delay: stop_event.set() or True,
    )

    live = queue.service_liveness("growth-event-worker")
    assert live is not None
    assert live["instanceId"] == "growth-instance-ttl"
    assert live["queueName"] == "product-generate"
    assert live["ttlSeconds"] == 27
    heartbeat_key = queue.config.service_heartbeat_key(
        "growth-event-worker"
    )
    assert redis.expirations[heartbeat_key] == 27


def test_handler_factory_failure_backs_off_without_false_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    queue = FakeQueue()
    stop_event = Event()
    waits: list[float] = []
    factories: list[Any] = [
        RuntimeError("handler unavailable"),
        handler,
    ]

    def handler_factory() -> Any:
        result = factories.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        growth_worker,
        "process_growth_events_once",
        lambda *_args, **_kwargs: {"claimed": 0},
    )

    def wait(delay: float) -> bool:
        waits.append(delay)
        if len(waits) == 2:
            stop_event.set()
        return stop_event.is_set()

    growth_worker.run_growth_worker_loop(
        connection_factory=lambda: connection,
        queue_factory=lambda: queue,
        handler_factory=handler_factory,
        stop_event=stop_event,
        worker_id="growth-instance-handler",
        initial_backoff_seconds=0.2,
        idle_seconds=1.0,
        wait=wait,
    )

    assert waits == [0.2, 1.0]
    assert len(queue.heartbeats) == 1
    assert connection.closed is True


def test_loop_rejects_autocommit_connection_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(autocommit=True)
    stop_event = Event()
    process_calls: list[bool] = []

    monkeypatch.setattr(
        growth_worker,
        "process_growth_events_once",
        lambda *_args, **_kwargs: process_calls.append(True),
    )

    growth_worker.run_growth_worker_loop(
        connection_factory=lambda: connection,
        queue_factory=FakeQueue,
        handler_factory=lambda: handler,
        stop_event=stop_event,
        worker_id="growth-instance-4",
        wait=lambda _delay: stop_event.set() or True,
    )

    assert process_calls == []
    assert connection.closed is True


def test_signal_handlers_request_cooperative_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, Any] = {}
    stop_event = Event()
    monkeypatch.setattr(
        growth_worker.signal,
        "signal",
        lambda signum, callback: handlers.__setitem__(signum, callback),
    )

    growth_worker._install_signal_handlers(stop_event)

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stop_event.is_set()


def test_main_builds_independent_worker_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("GROWTH_WORKER_ID", "render-growth-1")
    monkeypatch.setenv("GROWTH_WORKER_BATCH_SIZE", "8")
    monkeypatch.setenv("GROWTH_WORKER_LEASE_SECONDS", "90")
    monkeypatch.setenv("GROWTH_WORKER_RETRY_SECONDS", "15")
    monkeypatch.setenv("GROWTH_WORKER_IDLE_SECONDS", "2.5")
    monkeypatch.setenv("GROWTH_WORKER_INITIAL_BACKOFF_SECONDS", "0.25")
    monkeypatch.setenv("GROWTH_WORKER_MAX_BACKOFF_SECONDS", "4")
    monkeypatch.setenv("GROWTH_WORKER_HEARTBEAT_TTL_SECONDS", "40")
    monkeypatch.setattr(
        growth_worker,
        "_install_signal_handlers",
        lambda stop_event: captured.setdefault("stop_event", stop_event),
    )
    monkeypatch.setattr(
        growth_worker,
        "run_growth_worker_loop",
        lambda **kwargs: captured.update(kwargs),
    )

    assert growth_worker.main() == 0

    assert captured["worker_id"] == "render-growth-1"
    assert captured["batch_limit"] == 8
    assert captured["lease_seconds"] == 90
    assert captured["retry_in_seconds"] == 15
    assert captured["idle_seconds"] == 2.5
    assert captured["initial_backoff_seconds"] == 0.25
    assert captured["max_backoff_seconds"] == 4.0
    assert captured["heartbeat_ttl_seconds"] == 40
    assert captured["handler_factory"] is (
        growth_worker.build_default_growth_handler
    )
    assert callable(captured["connection_factory"])
    assert callable(captured["queue_factory"])
    assert isinstance(captured["stop_event"], Event)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GROWTH_WORKER_BATCH_SIZE", "101"),
        ("GROWTH_WORKER_LEASE_SECONDS", "86401"),
        ("GROWTH_WORKER_RETRY_SECONDS", "604801"),
        ("GROWTH_WORKER_HEARTBEAT_TTL_SECONDS", "3601"),
    ],
)
def test_main_rejects_unbounded_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setattr(
        growth_worker,
        "_install_signal_handlers",
        lambda _stop_event: None,
    )

    with pytest.raises(growth_worker.GrowthWorkerError):
        growth_worker.main()
