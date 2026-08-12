from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.product_job_store import OutboxClaimLost, ProductJobStore
from shared.postgres_runtime import (
    PostgresRuntimeError,
    connect_postgres,
    postgres_config_from_env,
)
from shared.redis_queue import (
    IdempotencyConflict,
    QueueError,
    RedisTaskQueue,
    product_queue_from_env,
    revision_queue_from_env,
)


LOGGER = logging.getLogger("waimai.product-outbox-dispatcher")

MENU_BATCH_JOB_TYPE = "menu_batch_generation"
REVISION_BATCH_JOB_TYPE = "delivery_asset_revision_batch"
PRODUCT_BATCH_TASK_TYPE = "product_batch"
PRODUCT_REVISION_TASK_TYPE = "product_revision"
SHA256_LENGTH = 64
MAX_BATCH_LIMIT = 100
DISPATCHER_SERVICE_ID = "product-outbox-dispatcher"


class OutboxDispatcherError(RuntimeError):
    pass


class InvalidOutboxClaim(OutboxDispatcherError):
    pass


class UnsupportedProductJobType(OutboxDispatcherError):
    pass


class QueueAcceptanceMismatch(OutboxDispatcherError):
    pass


class OutboxCompensationError(OutboxDispatcherError):
    pass


class DispatchBatchError(OutboxDispatcherError):
    def __init__(
        self,
        *,
        report: Mapping[str, Any],
        failures: list[tuple[str, BaseException]],
    ) -> None:
        self.report = dict(report)
        self.failures = tuple(failures)
        summary = ", ".join(
            f"{outbox_id}:{type(error).__name__}"
            for outbox_id, error in failures
        )
        super().__init__(f"one or more outbox records were not published: {summary}")


@dataclass(frozen=True)
class PreparedDispatch:
    outbox_id: str
    job_id: str
    owner_user_id: str
    request_sha256: str
    idempotency_key: str
    claim_token: str
    fence: int
    task_type: str
    task_payload: dict[str, Any]


def product_job_store_from_env(
    env: Mapping[str, str] | None = None,
) -> ProductJobStore:
    values = os.environ if env is None else env
    try:
        connection = connect_postgres(postgres_config_from_env(values))
    except PostgresRuntimeError as exc:
        raise OutboxDispatcherError(
            f"PostgreSQL unavailable for product outbox dispatcher: {exc.code}"
        ) from exc
    return ProductJobStore(connection)


def prepare_dispatch(claim: Mapping[str, Any]) -> PreparedDispatch:
    source = _mapping(claim, "claim")
    outbox_id = _required_text(source.get("outbox_id"), "outbox_id")
    job_id = _required_text(source.get("job_id"), "job_id")
    owner_user_id = _required_text(
        source.get("owner_user_id"),
        "owner_user_id",
    )
    request_digest = _sha256(source.get("request_sha256"), "request_sha256")
    claim_token = _required_text(source.get("claim_token"), "claim_token")
    fence = _positive_int(source.get("fence"), "fence")

    envelope = _mapping(source.get("payload"), "payload")
    _require_equal(
        envelope.get("jobId"),
        job_id,
        "payload.jobId",
    )
    _require_equal(
        envelope.get("ownerUserId"),
        owner_user_id,
        "payload.ownerUserId",
    )
    _require_digest_equal(
        envelope.get("requestSha256"),
        request_digest,
        "payload.requestSha256",
    )

    request = _json_clone(_mapping(envelope.get("request"), "payload.request"))
    _require_equal(request.get("jobId"), job_id, "payload.request.jobId")
    _require_equal(
        request.get("userId"),
        owner_user_id,
        "payload.request.userId",
    )
    idempotency = _mapping(
        request.get("idempotency"),
        "payload.request.idempotency",
    )
    idempotency_key = _required_text(
        idempotency.get("key"),
        "payload.request.idempotency.key",
    )
    _require_digest_equal(
        idempotency.get("requestSha256"),
        request_digest,
        "payload.request.idempotency.requestSha256",
    )

    job_type = _required_text(
        request.get("jobType"),
        "payload.request.jobType",
    )
    if job_type == MENU_BATCH_JOB_TYPE:
        task_type = PRODUCT_BATCH_TASK_TYPE
        contract_key = "batchContract"
    elif job_type == REVISION_BATCH_JOB_TYPE:
        task_type = PRODUCT_REVISION_TASK_TYPE
        contract_key = "revisionContract"
    else:
        raise UnsupportedProductJobType(
            f"unsupported product outbox job type: {job_type}"
        )

    task_payload = {
        "taskType": task_type,
        contract_key: request,
        "_productJobFence": fence,
        "_productOutboxId": outbox_id,
    }
    return PreparedDispatch(
        outbox_id=outbox_id,
        job_id=job_id,
        owner_user_id=owner_user_id,
        request_sha256=request_digest,
        idempotency_key=idempotency_key,
        claim_token=claim_token,
        fence=fence,
        task_type=task_type,
        task_payload=task_payload,
    )


def dispatch_claim(
    store: Any,
    queue: RedisTaskQueue,
    claim: Mapping[str, Any],
    *,
    prepared: PreparedDispatch | None = None,
) -> dict[str, Any]:
    prepared = prepared or prepare_dispatch(claim)
    recovered_after_error = False
    try:
        task = queue.enqueue_idempotent(
            prepared.task_payload,
            user_id=prepared.owner_user_id,
            idempotency_key=prepared.idempotency_key,
            request_sha256=prepared.request_sha256,
            task_id=prepared.job_id,
        )
    except IdempotencyConflict:
        raise
    except Exception as enqueue_error:  # noqa: BLE001 - Redis client errors vary
        try:
            task = queue.get(prepared.job_id)
        except Exception:  # noqa: BLE001 - acceptance must be positively verified
            raise QueueAcceptanceMismatch(
                "Redis enqueue failed and no matching task could be verified"
            ) from enqueue_error
        if not _accepted_task_matches(task, prepared):
            raise QueueAcceptanceMismatch(
                "Redis enqueue failed and the existing task does not match "
                "the current outbox claim"
            ) from enqueue_error
        recovered_after_error = True

    if not _accepted_task_matches(task, prepared):
        raise QueueAcceptanceMismatch(
            "Redis returned a task that does not match the current outbox claim"
        )

    try:
        published = store.mark_outbox_published(
            outbox_id=prepared.outbox_id,
            claim_token=prepared.claim_token,
        )
    except OutboxClaimLost as claim_error:
        try:
            compensation_status = _cancel_exact_accepted_task(queue, prepared)
        except Exception as compensation_error:
            raise OutboxCompensationError(
                "outbox publish claim was lost after Redis accepted the task, "
                "and compensating cancellation could not be verified"
            ) from compensation_error
        LOGGER.warning(
            "product outbox publish claim was lost after queue acceptance",
            extra={
                "outbox_id": prepared.outbox_id,
                "job_id": prepared.job_id,
                "compensation_status": compensation_status,
                "claim_error_type": type(claim_error).__name__,
            },
        )
        return {
            "outboxId": prepared.outbox_id,
            "jobId": prepared.job_id,
            "status": "publish_claim_lost",
            "claimLost": True,
            "compensationStatus": compensation_status,
            "recoveredAfterQueueError": recovered_after_error,
            "published": None,
        }
    return {
        "outboxId": prepared.outbox_id,
        "jobId": prepared.job_id,
        "status": "published",
        "claimLost": False,
        "recoveredAfterQueueError": recovered_after_error,
        "published": published,
    }


def dispatch_once(
    store: Any,
    queue: RedisTaskQueue,
    *,
    revision_queue: RedisTaskQueue | None = None,
    dispatcher_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
) -> dict[str, Any]:
    clean_dispatcher_id = _required_text(dispatcher_id, "dispatcher_id")
    batch_limit = _bounded_positive_int(
        limit,
        "limit",
        maximum=MAX_BATCH_LIMIT,
    )
    lease = _positive_int(lease_seconds, "lease_seconds")
    claim_token = str(uuid4())
    claims = store.claim_outbox(
        worker_id=clean_dispatcher_id,
        limit=batch_limit,
        lease_seconds=lease,
        claim_token=claim_token,
    )
    if not isinstance(claims, list):
        raise OutboxDispatcherError("claim_outbox must return a list")

    results: list[dict[str, Any]] = []
    failures: list[tuple[str, BaseException]] = []
    for claim in claims:
        outbox_id = (
            str(claim.get("outbox_id") or "")
            if isinstance(claim, Mapping)
            else ""
        )
        try:
            prepared = prepare_dispatch(claim)
            destination = (
                revision_queue
                if (
                    prepared.task_type == PRODUCT_REVISION_TASK_TYPE
                    and revision_queue is not None
                )
                else queue
            )
            results.append(
                dispatch_claim(
                    store,
                    destination,
                    claim,
                    prepared=prepared,
                )
            )
        except (OutboxDispatcherError, QueueError) as exc:
            failures.append((outbox_id or "unknown", exc))
            LOGGER.warning(
                "product outbox record was not published",
                extra={
                    "outbox_id": outbox_id,
                    "error_type": type(exc).__name__,
                },
            )

    report = {
        "claimed": len(claims),
        "published": sum(
            1 for result in results if result.get("status") == "published"
        ),
        "claimLost": sum(
            1 for result in results if bool(result.get("claimLost"))
        ),
        "recovered": sum(
            1
            for result in results
            if bool(result.get("recoveredAfterQueueError"))
        ),
        "results": results,
    }
    if failures:
        raise DispatchBatchError(report=report, failures=failures)
    return report


def dispatcher_heartbeat_key(
    queue: RedisTaskQueue,
    dispatcher_id: str,
) -> str:
    _required_text(dispatcher_id, "dispatcher_id")
    return queue.config.service_heartbeat_key(DISPATCHER_SERVICE_ID)


def write_dispatcher_heartbeat(
    queue: RedisTaskQueue,
    *,
    dispatcher_id: str,
    status: str,
    ttl_seconds: int,
    report: Mapping[str, Any] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    ttl = _positive_int(ttl_seconds, "heartbeat_ttl_seconds")
    document = {
        "schemaVersion": 1,
        "role": "product-outbox-dispatcher",
        "dispatcherId": _required_text(dispatcher_id, "dispatcher_id"),
        "serviceId": DISPATCHER_SERVICE_ID,
        "instanceId": _required_text(dispatcher_id, "dispatcher_id"),
        "queueName": queue.config.queue_name,
        "status": _required_text(status, "status"),
        "observedAtUnix": int(now()),
        "reportedAtMs": int(now() * 1000),
        "claimed": int((report or {}).get("claimed") or 0),
        "published": int((report or {}).get("published") or 0),
        "recovered": int((report or {}).get("recovered") or 0),
    }
    key = dispatcher_heartbeat_key(queue, dispatcher_id)
    written = queue.redis.set(
        key,
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        ex=ttl,
    )
    if written is False:
        raise QueueError("Redis rejected dispatcher heartbeat write")
    return document


def read_dispatcher_heartbeat(
    queue: RedisTaskQueue,
    dispatcher_id: str,
) -> dict[str, Any] | None:
    clean_dispatcher_id = _required_text(dispatcher_id, "dispatcher_id")
    key = dispatcher_heartbeat_key(queue, clean_dispatcher_id)
    raw = queue.redis.get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        document = json.loads(str(raw))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise QueueError("invalid dispatcher heartbeat document") from exc
    if not isinstance(document, dict):
        raise QueueError("invalid dispatcher heartbeat document")
    if (
        str(document.get("serviceId") or "") != DISPATCHER_SERVICE_ID
        or str(document.get("instanceId") or "") != clean_dispatcher_id
        or str(document.get("queueName") or "") != queue.config.queue_name
    ):
        raise QueueError("invalid dispatcher heartbeat identity")
    ttl = int(queue.redis.ttl(key))
    if ttl < 0:
        return None
    document["ttlSeconds"] = ttl
    return document


def run_dispatch_loop(
    *,
    store_factory: Callable[[], Any],
    queue_factory: Callable[[], RedisTaskQueue],
    revision_queue_factory: Callable[[], RedisTaskQueue] | None = None,
    stop_event: Event,
    dispatcher_id: str,
    batch_limit: int = 10,
    lease_seconds: int = 60,
    idle_seconds: float = 1.0,
    initial_backoff_seconds: float = 0.5,
    max_backoff_seconds: float = 30.0,
    heartbeat_ttl_seconds: int = 30,
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
    wait_for_stop = wait or stop_event.wait
    store: Any | None = None
    queue: RedisTaskQueue | None = None
    revision_queue: RedisTaskQueue | None = None
    consecutive_failures = 0

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
                report = dispatch_once(
                    store,
                    queue,
                    revision_queue=revision_queue,
                    dispatcher_id=dispatcher_id,
                    limit=batch_limit,
                    lease_seconds=lease_seconds,
                )
                consecutive_failures = 0
                if heartbeat_ttl_seconds > 0:
                    write_dispatcher_heartbeat(
                        queue,
                        dispatcher_id=dispatcher_id,
                        status="running",
                        ttl_seconds=heartbeat_ttl_seconds,
                        report=report,
                    )
                if int(report["claimed"]) == 0 and idle_delay > 0:
                    wait_for_stop(idle_delay)
            except Exception as exc:  # noqa: BLE001 - process boundary
                consecutive_failures += 1
                LOGGER.exception(
                    "product outbox dispatch cycle failed",
                    extra={"consecutive_failures": consecutive_failures},
                )
                _close_store(store)
                store = None
                queue = None
                revision_queue = None
                delay = min(
                    max_backoff,
                    initial_backoff * (2 ** (consecutive_failures - 1)),
                )
                wait_for_stop(delay)
    finally:
        if queue is not None and heartbeat_ttl_seconds > 0:
            try:
                write_dispatcher_heartbeat(
                    queue,
                    dispatcher_id=dispatcher_id,
                    status="stopping",
                    ttl_seconds=heartbeat_ttl_seconds,
                )
            except Exception:  # noqa: BLE001 - shutdown remains best effort
                LOGGER.warning(
                    "failed to write dispatcher shutdown heartbeat",
                    exc_info=True,
                )
        _close_store(store)


def main() -> int:
    logging.basicConfig(
        level=str(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    values = dict(os.environ)
    stop_event = Event()
    _install_signal_handlers(stop_event)
    dispatcher_id = (
        str(values.get("PRODUCT_OUTBOX_DISPATCHER_ID") or "").strip()
        or (
            f"outbox-{socket.gethostname()}-{os.getpid()}-"
            f"{uuid4().hex[:8]}"
        )
    )
    run_dispatch_loop(
        store_factory=lambda: product_job_store_from_env(values),
        queue_factory=lambda: product_queue_from_env(values),
        revision_queue_factory=lambda: revision_queue_from_env(values),
        stop_event=stop_event,
        dispatcher_id=dispatcher_id,
        batch_limit=_env_positive_int(
            values,
            "PRODUCT_OUTBOX_BATCH_SIZE",
            10,
        ),
        lease_seconds=_env_positive_int(
            values,
            "PRODUCT_OUTBOX_LEASE_SECONDS",
            60,
        ),
        idle_seconds=_env_positive_float(
            values,
            "PRODUCT_OUTBOX_IDLE_SECONDS",
            1.0,
        ),
        initial_backoff_seconds=_env_positive_float(
            values,
            "PRODUCT_OUTBOX_INITIAL_BACKOFF_SECONDS",
            0.5,
        ),
        max_backoff_seconds=_env_positive_float(
            values,
            "PRODUCT_OUTBOX_MAX_BACKOFF_SECONDS",
            30.0,
        ),
        heartbeat_ttl_seconds=_env_positive_int(
            values,
            "PRODUCT_OUTBOX_HEARTBEAT_TTL_SECONDS",
            30,
        ),
    )
    return 0


def _accepted_task_matches(
    task: Any,
    prepared: PreparedDispatch,
) -> bool:
    if not isinstance(task, Mapping):
        return False
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return (
        _same_text(task.get("task_id"), prepared.job_id)
        and _same_text(
            task.get("owner_user_id"),
            prepared.owner_user_id,
        )
        and _same_digest(
            task.get("request_sha256"),
            prepared.request_sha256,
        )
        and _canonical_json(payload)
        == _canonical_json(prepared.task_payload)
    )


def _cancel_exact_accepted_task(
    queue: RedisTaskQueue,
    prepared: PreparedDispatch,
) -> str:
    result = queue.redis.eval(
        _CANCEL_EXACT_ACCEPTED_TASK_LUA,
        4,
        queue.config.task_key(prepared.job_id),
        queue.config.queue_key,
        queue.config.processing_key,
        queue.config.idempotency_claim_key(
            prepared.owner_user_id,
            prepared.idempotency_key,
        ),
        prepared.job_id,
        prepared.owner_user_id,
        prepared.request_sha256,
        _canonical_json(prepared.task_payload),
        str(max(0, int(queue.config.terminal_ttl_seconds))),
        str(max(0, int(queue.config.idempotency_ttl_seconds))),
    )
    try:
        outcome = int(
            result.decode("utf-8") if isinstance(result, bytes) else result
        )
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise QueueError(
            "invalid Redis exact cancellation result"
        ) from exc
    statuses = {
        -1: "missing",
        0: "identity_mismatch",
        1: "canceled",
        2: "already_terminal",
        3: "cancel_requested",
    }
    if outcome == -4:
        raise QueueError(
            "Redis exact cancellation keys have incompatible types"
        )
    if outcome == -5:
        raise QueueError(
            "Redis exact cancellation found an unsupported task state"
        )
    status = statuses.get(outcome)
    if status is None:
        raise QueueError(f"unknown Redis exact cancellation result: {outcome}")
    return status


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
        except Exception:  # noqa: BLE001 - cleanup must not mask root cause
            LOGGER.warning("failed to close PostgreSQL connection", exc_info=True)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidOutboxClaim(f"{field} must be an object")
    return value


def _required_text(value: Any, field: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or len(clean) > 512 or any(ord(char) < 32 for char in clean):
        raise InvalidOutboxClaim(f"{field} must be a non-empty bounded string")
    return clean


def _sha256(value: Any, field: str) -> str:
    clean = str(value or "").strip().lower()
    if (
        len(clean) != SHA256_LENGTH
        or any(char not in "0123456789abcdef" for char in clean)
    ):
        raise InvalidOutboxClaim(
            f"{field} must be a 64-character SHA-256 digest"
        )
    return clean


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidOutboxClaim(f"{field} must be a positive integer")
    return value


def _bounded_positive_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    number = _positive_int(value, field)
    if number > maximum:
        raise InvalidOutboxClaim(f"{field} must not exceed {maximum}")
    return number


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise OutboxDispatcherError(f"{field} must be positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OutboxDispatcherError(f"{field} must be positive") from exc
    if number <= 0:
        raise OutboxDispatcherError(f"{field} must be positive")
    return number


def _require_equal(value: Any, expected: str, field: str) -> None:
    actual = _required_text(value, field)
    if not _same_text(actual, expected):
        raise InvalidOutboxClaim(f"{field} does not match the claimed job")


def _require_digest_equal(value: Any, expected: str, field: str) -> None:
    actual = _sha256(value, field)
    if not hmac.compare_digest(actual, expected):
        raise InvalidOutboxClaim(f"{field} does not match the claimed request")


def _same_text(value: Any, expected: str) -> bool:
    actual = str(value or "")
    return hmac.compare_digest(actual, expected)


def _same_digest(value: Any, expected: str) -> bool:
    actual = str(value or "").strip().lower()
    return (
        len(actual) == SHA256_LENGTH
        and hmac.compare_digest(actual, expected)
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidOutboxClaim("outbox request must be JSON serializable") from exc


def _json_clone(value: Mapping[str, Any]) -> dict[str, Any]:
    cloned = json.loads(_canonical_json(value))
    if not isinstance(cloned, dict):
        raise InvalidOutboxClaim("payload.request must be an object")
    return cloned


def _env_positive_int(
    values: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = str(values.get(key) or default).strip()
    try:
        number = int(raw)
    except ValueError as exc:
        raise OutboxDispatcherError(f"{key} must be a positive integer") from exc
    if number <= 0:
        raise OutboxDispatcherError(f"{key} must be a positive integer")
    return number


def _env_positive_float(
    values: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    return _positive_float(
        str(values.get(key) or default).strip(),
        key,
    )


_CANCEL_EXACT_ACCEPTED_TASK_LUA = """
-- WAIMAI_CANCEL_EXACT_ACCEPTED_TASK
local function type_name(key)
    local value = redis.call("TYPE", key)
    if type(value) == "table" then
        return value["ok"]
    end
    return value
end

local task_type = type_name(KEYS[1])
if task_type == "none" then
    return -1
end
if task_type ~= "hash" then
    return -4
end
for index = 2, 3 do
    local value_type = type_name(KEYS[index])
    if value_type ~= "none" and value_type ~= "list" then
        return -4
    end
end
local idempotency_type = type_name(KEYS[4])
if idempotency_type ~= "none" and idempotency_type ~= "string" then
    return -4
end

if redis.call("HGET", KEYS[1], "task_id") ~= ARGV[1]
    or redis.call("HGET", KEYS[1], "owner_user_id") ~= ARGV[2]
    or redis.call("HGET", KEYS[1], "request_sha256") ~= ARGV[3]
    or redis.call("HGET", KEYS[1], "payload_json") ~= ARGV[4] then
    return 0
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
if status ~= "pending" then
    return -5
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
if tonumber(ARGV[5]) > 0 then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[5]))
end
if KEYS[4] ~= KEYS[1] and tonumber(ARGV[6]) > 0 then
    redis.call("EXPIRE", KEYS[4], tonumber(ARGV[6]))
end
return 1
"""


if __name__ == "__main__":
    raise SystemExit(main())
