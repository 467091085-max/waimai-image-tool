from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Final


STATUS_QUEUED: Final = "queued"
STATUS_RUNNING: Final = "running"
STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"
STATUS_CANCELED: Final = "canceled"

# Backwards-compatible names for older call sites/tests. The queue now reports
# the canonical "completed" terminal state and keeps failed counts/error details
# on the job payload for partial outcomes.
STATUS_SUCCEEDED: Final = STATUS_COMPLETED
STATUS_PARTIALLY_SUCCEEDED: Final = STATUS_COMPLETED

DEFAULT_WORKER_COUNT: Final = 1
DEFAULT_STALE_AFTER_SECONDS: Final = 5 * 60
DEFAULT_TIMEOUT_SECONDS: Final = 30 * 60

_CANONICAL_JOB_STATUSES: Final = frozenset(
    {
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELED,
    }
)
_LEGACY_STATUS_ALIASES: Final = {
    "succeeded": STATUS_COMPLETED,
    "partially_succeeded": STATUS_COMPLETED,
}
JOB_STATUSES: Final = _CANONICAL_JOB_STATUSES | frozenset(_LEGACY_STATUS_ALIASES)

TERMINAL_JOB_STATUSES: Final = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELED,
    }
)

_ALLOWED_TRANSITIONS: Final = {
    STATUS_QUEUED: frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELED}),
    STATUS_RUNNING: frozenset(
        {
            STATUS_RUNNING,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_CANCELED,
        }
    ),
    STATUS_COMPLETED: frozenset({STATUS_COMPLETED}),
    STATUS_FAILED: frozenset({STATUS_FAILED}),
    STATUS_CANCELED: frozenset({STATUS_CANCELED}),
}


@dataclass(frozen=True)
class QueueLimits:
    worker_count: int = DEFAULT_WORKER_COUNT
    max_pending_jobs: int = DEFAULT_WORKER_COUNT * 4
    stale_after_seconds: float = float(DEFAULT_STALE_AFTER_SECONDS)
    timeout_seconds: float = float(DEFAULT_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class QueueAdmission:
    allowed: bool
    queued_count: int
    running_count: int
    pending_count: int
    max_pending_jobs: int
    worker_count: int
    reason: str | None = None


def transition_job_status(current: str, target: str) -> str:
    """Return target when a job status transition is allowed."""
    current_status = normalize_job_status(current, "current")
    target_status = normalize_job_status(target, "target")

    if target_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise ValueError(
            f"invalid job status transition: {current_status!r} -> {target_status!r}"
        )

    return target_status


def summarize_job(
    requested: int,
    completed: int,
    failed: int,
    canceled: int = 0,
) -> dict[str, int | str]:
    """Summarize generated item counts into a job status and pending count."""
    requested_count = _non_negative_int(requested, "requested")
    completed_count = _non_negative_int(completed, "completed")
    failed_count = _non_negative_int(failed, "failed")
    canceled_count = _non_negative_int(canceled, "canceled")

    finished_count = completed_count + failed_count + canceled_count
    if finished_count > requested_count:
        raise ValueError("completed, failed, and canceled cannot exceed requested")

    pending = requested_count - finished_count

    if canceled_count > 0:
        status = STATUS_CANCELED
    elif pending > 0:
        status = STATUS_RUNNING
    elif failed_count == requested_count and requested_count > 0:
        status = STATUS_FAILED
    else:
        status = STATUS_COMPLETED

    return {"status": status, "pending": pending}


def normalize_job_status(status: str, name: str = "status") -> str:
    if not isinstance(status, str):
        raise TypeError(f"{name} must be a string")

    normalized = status.strip().lower()
    normalized = _LEGACY_STATUS_ALIASES.get(normalized, normalized)
    if normalized not in _CANONICAL_JOB_STATUSES:
        raise ValueError(f"unsupported job status: {status!r}")

    return normalized


def is_terminal_status(status: str) -> bool:
    return normalize_job_status(status) in TERMINAL_JOB_STATUSES


def resolve_queue_limits(
    *,
    worker_count: int = DEFAULT_WORKER_COUNT,
    max_pending_jobs: int | None = None,
    stale_after_seconds: float = float(DEFAULT_STALE_AFTER_SECONDS),
    timeout_seconds: float = float(DEFAULT_TIMEOUT_SECONDS),
) -> QueueLimits:
    workers = _positive_int(worker_count, "worker_count")
    pending_limit = (
        max(workers * 4, workers)
        if max_pending_jobs is None
        else _positive_int(max_pending_jobs, "max_pending_jobs")
    )
    stale_seconds = _positive_seconds(stale_after_seconds, "stale_after_seconds")
    timeout_seconds_value = _positive_seconds(timeout_seconds, "timeout_seconds")
    if stale_seconds > timeout_seconds_value:
        raise ValueError("stale_after_seconds cannot exceed timeout_seconds")

    return QueueLimits(
        worker_count=workers,
        max_pending_jobs=pending_limit,
        stale_after_seconds=stale_seconds,
        timeout_seconds=timeout_seconds_value,
    )


def evaluate_queue_admission(
    *,
    queued_count: int,
    running_count: int,
    limits: QueueLimits | None = None,
) -> QueueAdmission:
    resolved_limits = limits or resolve_queue_limits()
    queued = _non_negative_int(queued_count, "queued_count")
    running = _non_negative_int(running_count, "running_count")
    pending = queued + running
    if pending >= resolved_limits.max_pending_jobs:
        return QueueAdmission(
            allowed=False,
            queued_count=queued,
            running_count=running,
            pending_count=pending,
            max_pending_jobs=resolved_limits.max_pending_jobs,
            worker_count=resolved_limits.worker_count,
            reason="max_pending_jobs_exceeded",
        )

    return QueueAdmission(
        allowed=True,
        queued_count=queued,
        running_count=running,
        pending_count=pending,
        max_pending_jobs=resolved_limits.max_pending_jobs,
        worker_count=resolved_limits.worker_count,
    )


def inspect_job_timing(
    *,
    status: str,
    created_at: datetime | None,
    updated_at: datetime | None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    now: datetime | None = None,
    limits: QueueLimits | None = None,
    stale_after_seconds: float | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, float | bool | str | None]:
    """Return deterministic stale/timeout metadata for queued or running jobs."""
    canonical_status = normalize_job_status(status)
    resolved_limits = limits or resolve_queue_limits(
        stale_after_seconds=(
            float(DEFAULT_STALE_AFTER_SECONDS)
            if stale_after_seconds is None
            else stale_after_seconds
        ),
        timeout_seconds=(
            float(DEFAULT_TIMEOUT_SECONDS)
            if timeout_seconds is None
            else timeout_seconds
        ),
    )
    stale_seconds = (
        resolved_limits.stale_after_seconds
        if stale_after_seconds is None
        else _positive_seconds(stale_after_seconds, "stale_after_seconds")
    )
    timeout_seconds_value = (
        resolved_limits.timeout_seconds
        if timeout_seconds is None
        else _positive_seconds(timeout_seconds, "timeout_seconds")
    )
    if stale_seconds > timeout_seconds_value:
        raise ValueError("stale_after_seconds cannot exceed timeout_seconds")

    now_value = now or datetime.now(timezone.utc)
    if canonical_status in TERMINAL_JOB_STATUSES:
        elapsed_seconds = _duration_seconds(started_at, finished_at)
        return {
            "status": canonical_status,
            "stale": False,
            "timed_out": False,
            "reason": None,
            "age_seconds": elapsed_seconds,
            "inactive_seconds": 0.0,
            "elapsed_seconds": elapsed_seconds,
            "stale_after_seconds": stale_seconds,
            "timeout_seconds": timeout_seconds_value,
        }

    if canonical_status == STATUS_RUNNING:
        age_anchor = started_at or updated_at or created_at or now_value
        activity_anchor = updated_at or started_at or created_at or now_value
    else:
        age_anchor = created_at or updated_at or now_value
        activity_anchor = updated_at or created_at or now_value

    age_seconds = _duration_seconds(age_anchor, now_value) or 0.0
    inactive_seconds = _duration_seconds(activity_anchor, now_value) or 0.0
    timed_out = age_seconds >= timeout_seconds_value
    stale = timed_out or inactive_seconds >= stale_seconds
    reason = "timeout" if timed_out else "stale" if stale else None

    return {
        "status": canonical_status,
        "stale": stale,
        "timed_out": timed_out,
        "reason": reason,
        "age_seconds": age_seconds,
        "inactive_seconds": inactive_seconds,
        "elapsed_seconds": age_seconds if canonical_status == STATUS_RUNNING else None,
        "stale_after_seconds": stale_seconds,
        "timeout_seconds": timeout_seconds_value,
    }


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _positive_int(value: int, name: str) -> int:
    checked = _non_negative_int(value, name)
    if checked < 1:
        raise ValueError(f"{name} must be at least 1")
    return checked


def _positive_seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    seconds = float(value)
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return seconds


def _duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())
