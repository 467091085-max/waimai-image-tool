from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any

import job_rules as rules


@dataclass(frozen=True)
class GenerationJob:
    job_id: str
    status: str
    requested: int
    completed: int = 0
    failed: int = 0
    canceled: int = 0
    pending: int = 0
    error: str | None = None
    result: Any = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed: float | None = None

    @property
    def id(self) -> str:
        return self.job_id

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        finished_at = self.finished_at or self.completed_at
        elapsed = self.elapsed
        if elapsed is None:
            elapsed = _elapsed_seconds(self.started_at, finished_at)
        return {
            "id": self.job_id,
            "job_id": self.job_id,
            "status": self.status,
            "requested": self.requested,
            "completed": self.completed,
            "failed": self.failed,
            "canceled": self.canceled,
            "pending": self.pending,
            "error": self.error,
            "result": _copy_value(self.result),
            "metadata": _copy_value(self.metadata or {}),
            "created_at": _format_datetime(self.created_at),
            "updated_at": _format_datetime(self.updated_at),
            "started_at": _format_datetime(self.started_at),
            "finished_at": _format_datetime(finished_at),
            "completed_at": _format_datetime(finished_at),
            "elapsed": elapsed,
            "elapsed_seconds": elapsed,
        }

    def timing(
        self,
        *,
        now: datetime | None = None,
        limits: rules.QueueLimits | None = None,
    ) -> dict[str, float | bool | str | None]:
        return rules.inspect_job_timing(
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at or self.completed_at,
            now=now,
            limits=limits,
        )


class JobStore:
    """Thread-safe local job state store with a backend-friendly surface."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = RLock()

    def reserve(
        self,
        job_id: str,
        *,
        requested: int = 1,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[GenerationJob, bool]:
        normalized_job_id = _normalize_job_id(job_id)
        requested_count = _non_negative_int(requested, "requested")

        with self._lock:
            existing = self._jobs.get(normalized_job_id)
            if existing is not None:
                return _snapshot(existing), False

            now = _utc_now()
            job = GenerationJob(
                job_id=normalized_job_id,
                status=rules.STATUS_QUEUED,
                requested=requested_count,
                pending=requested_count,
                metadata=dict(metadata or {}),
                created_at=now,
                updated_at=now,
            )
            self._jobs[normalized_job_id] = job
            return _snapshot(job), True

    def enqueue(
        self,
        job_id: str,
        *,
        requested: int = 1,
        metadata: Mapping[str, Any] | None = None,
    ) -> GenerationJob:
        job, _created = self.reserve(job_id, requested=requested, metadata=metadata)
        return job

    def start(self, job_id: str) -> GenerationJob:
        with self._lock:
            current = self._require(job_id)
            if current.status in rules.TERMINAL_JOB_STATUSES:
                return _snapshot(current)

            now = _utc_now()
            started_at = current.started_at or now
            status = rules.transition_job_status(current.status, rules.STATUS_RUNNING)
            job = replace(
                current,
                status=status,
                pending=_pending_for(
                    current.requested,
                    current.completed,
                    current.failed,
                    current.canceled,
                ),
                started_at=started_at,
                updated_at=now,
                elapsed=_elapsed_seconds(started_at, now),
            )
            self._jobs[job.job_id] = job
            return _snapshot(job)

    def progress(
        self,
        job_id: str,
        *,
        completed: int | None = None,
        failed: int | None = None,
        pending: int | None = None,
        result: Any = None,
    ) -> GenerationJob:
        with self._lock:
            current = self._require(job_id)
            if current.status in rules.TERMINAL_JOB_STATUSES:
                return _snapshot(current)

            completed_count = current.completed if completed is None else _non_negative_int(completed, "completed")
            failed_count = current.failed if failed is None else _non_negative_int(failed, "failed")
            if completed_count < current.completed:
                raise ValueError("completed cannot decrease")
            if failed_count < current.failed:
                raise ValueError("failed cannot decrease")

            if pending is None:
                requested_count = current.requested
                pending_count = _pending_for(
                    requested_count,
                    completed_count,
                    failed_count,
                    current.canceled,
                )
            else:
                pending_count = _non_negative_int(pending, "pending")
                requested_count = completed_count + failed_count + current.canceled + pending_count

            now = _utc_now()
            started_at = current.started_at or now
            status = rules.transition_job_status(current.status, rules.STATUS_RUNNING)
            job = replace(
                current,
                status=status,
                requested=requested_count,
                completed=completed_count,
                failed=failed_count,
                pending=pending_count,
                result=_copy_value(result) if result is not None else current.result,
                started_at=started_at,
                updated_at=now,
                elapsed=_elapsed_seconds(started_at, now),
            )
            self._jobs[job.job_id] = job
            return _snapshot(job)

    def complete(
        self,
        job_id: str,
        *,
        result: Any = None,
        completed: int | None = None,
    ) -> GenerationJob:
        with self._lock:
            current = self._require(job_id)
            if current.status in rules.TERMINAL_JOB_STATUSES:
                return _snapshot(current)

            completed_count = (
                current.requested - current.failed - current.canceled
                if completed is None
                else _non_negative_int(completed, "completed")
            )
            if completed_count < current.completed:
                raise ValueError("completed cannot decrease")

            _validate_final_counts(
                current.requested,
                completed_count,
                current.failed,
                current.canceled,
            )
            summary = rules.summarize_job(
                requested=current.requested,
                completed=completed_count,
                failed=current.failed,
                canceled=current.canceled,
            )
            status = str(summary["status"])
            _ensure_transition(current.status, status)

            now = _utc_now()
            started_at = current.started_at or now
            job = replace(
                current,
                status=status,
                completed=completed_count,
                pending=int(summary["pending"]),
                error=None,
                result=_copy_value(result),
                started_at=started_at,
                finished_at=current.finished_at or current.completed_at or now,
                completed_at=current.completed_at or current.finished_at or now,
                updated_at=now,
                elapsed=_elapsed_seconds(started_at, current.finished_at or current.completed_at or now),
            )
            self._jobs[job.job_id] = job
            return _snapshot(job)

    def fail(
        self,
        job_id: str,
        error: BaseException | str,
        *,
        failed: int | None = None,
        result: Any = None,
    ) -> GenerationJob:
        with self._lock:
            current = self._require(job_id)
            if current.status in rules.TERMINAL_JOB_STATUSES:
                return _snapshot(current)

            requested_count = current.requested
            if failed is None:
                remaining = requested_count - current.completed - current.failed - current.canceled
                if requested_count == 0 and current.completed == 0 and current.failed == 0:
                    requested_count = 1
                    failed_count = 1
                else:
                    failed_count = current.failed + max(0, remaining)
            else:
                failed_count = _non_negative_int(failed, "failed")

            if failed_count < current.failed:
                raise ValueError("failed cannot decrease")

            _validate_final_counts(
                requested_count,
                current.completed,
                failed_count,
                current.canceled,
            )
            summary = rules.summarize_job(
                requested=requested_count,
                completed=current.completed,
                failed=failed_count,
                canceled=current.canceled,
            )
            status = str(summary["status"])
            _ensure_transition(current.status, status)

            now = _utc_now()
            started_at = current.started_at or now
            finished_at = current.finished_at or current.completed_at or now
            job = replace(
                current,
                status=status,
                requested=requested_count,
                failed=failed_count,
                pending=int(summary["pending"]),
                error=_error_text(error),
                result=_copy_value(result) if result is not None else current.result,
                started_at=started_at,
                finished_at=finished_at,
                completed_at=current.completed_at or current.finished_at or now,
                updated_at=now,
                elapsed=_elapsed_seconds(started_at, finished_at),
            )
            self._jobs[job.job_id] = job
            return _snapshot(job)

    def cancel(self, job_id: str, *, error: str | None = None) -> GenerationJob:
        with self._lock:
            current = self._require(job_id)
            if current.status in rules.TERMINAL_JOB_STATUSES:
                return _snapshot(current)

            canceled_count = current.requested - current.completed - current.failed
            if canceled_count < 0:
                canceled_count = 0

            status = rules.transition_job_status(current.status, rules.STATUS_CANCELED)
            now = _utc_now()
            finished_at = current.finished_at or current.completed_at or now
            job = replace(
                current,
                status=status,
                canceled=canceled_count,
                pending=0,
                error=error,
                finished_at=finished_at,
                completed_at=current.completed_at or current.finished_at or now,
                updated_at=now,
                elapsed=_elapsed_seconds(current.started_at, finished_at),
            )
            self._jobs[job.job_id] = job
            return _snapshot(job)

    def get(self, job_id: str) -> GenerationJob | None:
        normalized_job_id = _normalize_job_id(job_id)
        with self._lock:
            job = self._jobs.get(normalized_job_id)
            return _snapshot(job) if job is not None else None

    def list(self, *, status: str | None = None) -> list[GenerationJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status is not None:
            normalized_status = _normalize_status(status)
            jobs = [job for job in jobs if job.status == normalized_status]
        return [_snapshot(job) for job in jobs]

    def inspect_timing(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        limits: rules.QueueLimits | None = None,
    ) -> dict[str, float | bool | str | None]:
        with self._lock:
            current = self._require(job_id)
            return current.timing(now=now, limits=limits)

    def list_stale(
        self,
        *,
        now: datetime | None = None,
        limits: rules.QueueLimits | None = None,
        timed_out_only: bool = False,
    ) -> list[GenerationJob]:
        now_value = now or _utc_now()
        stale_jobs: list[GenerationJob] = []
        with self._lock:
            jobs = list(self._jobs.values())
            for job in jobs:
                timing = job.timing(now=now_value, limits=limits)
                if timed_out_only:
                    stale = bool(timing["timed_out"])
                else:
                    stale = bool(timing["stale"])
                if stale:
                    stale_jobs.append(_snapshot(job))
        return stale_jobs

    def snapshot(
        self,
        *,
        now: datetime | None = None,
        limits: rules.QueueLimits | None = None,
        closed: bool = False,
    ) -> dict[str, Any]:
        now_value = now or _utc_now()
        resolved_limits = limits or rules.resolve_queue_limits()
        counts = _empty_status_counts()
        oldest_queued_age: float | None = None
        oldest_running_age: float | None = None
        stale_count = 0
        timed_out_count = 0

        with self._lock:
            jobs = list(self._jobs.values())

        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
            timing = job.timing(now=now_value, limits=resolved_limits)
            if bool(timing["stale"]):
                stale_count += 1
            if bool(timing["timed_out"]):
                timed_out_count += 1

            age_seconds = timing["age_seconds"]
            age = float(age_seconds) if age_seconds is not None else None
            if job.status == rules.STATUS_QUEUED and age is not None:
                oldest_queued_age = (
                    age if oldest_queued_age is None else max(oldest_queued_age, age)
                )
            if job.status == rules.STATUS_RUNNING and age is not None:
                oldest_running_age = (
                    age if oldest_running_age is None else max(oldest_running_age, age)
                )

        return {
            "countsByStatus": counts,
            "workerCount": resolved_limits.worker_count,
            "limits": _limits_payload(resolved_limits),
            "oldestQueuedAgeSeconds": oldest_queued_age,
            "oldestRunningAgeSeconds": oldest_running_age,
            "staleCount": stale_count,
            "timedOutCount": timed_out_count,
            "closed": bool(closed),
        }

    def fail_timed_out(
        self,
        *,
        now: datetime | None = None,
        limits: rules.QueueLimits | None = None,
        error: str = "generation job timed out",
    ) -> list[GenerationJob]:
        now_value = now or _utc_now()
        timed_out_jobs: list[GenerationJob] = []
        with self._lock:
            jobs = list(self._jobs.values())
            for current in jobs:
                timing = current.timing(now=now_value, limits=limits)
                if not bool(timing["timed_out"]):
                    continue
                if current.status in rules.TERMINAL_JOB_STATUSES:
                    continue

                requested_count = current.requested
                remaining = requested_count - current.completed - current.failed - current.canceled
                if requested_count == 0 and current.completed == 0 and current.failed == 0:
                    requested_count = 1
                    failed_count = 1
                else:
                    failed_count = current.failed + max(0, remaining)

                _validate_final_counts(
                    requested_count,
                    current.completed,
                    failed_count,
                    current.canceled,
                )
                _ensure_transition(current.status, rules.STATUS_FAILED)
                started_at = current.started_at or now_value
                job = replace(
                    current,
                    status=rules.STATUS_FAILED,
                    requested=requested_count,
                    failed=failed_count,
                    pending=0,
                    error=error,
                    started_at=started_at,
                    finished_at=current.finished_at or current.completed_at or now_value,
                    completed_at=current.completed_at or current.finished_at or now_value,
                    updated_at=now_value,
                    elapsed=_elapsed_seconds(started_at, current.finished_at or current.completed_at or now_value),
                )
                self._jobs[job.job_id] = job
                timed_out_jobs.append(_snapshot(job))
        return timed_out_jobs

    def _require(self, job_id: str) -> GenerationJob:
        normalized_job_id = _normalize_job_id(job_id)
        job = self._jobs.get(normalized_job_id)
        if job is None:
            raise KeyError(f"generation job not found: {normalized_job_id}")
        return job


class InMemoryGenerationQueue:
    """Local background generation queue backed by threads and JobStore."""

    def __init__(
        self,
        *,
        worker_count: int = 1,
        store: JobStore | None = None,
        limits: rules.QueueLimits | None = None,
        max_pending_jobs: int | None = None,
        stale_after_seconds: float = float(rules.DEFAULT_STALE_AFTER_SECONDS),
        timeout_seconds: float = float(rules.DEFAULT_TIMEOUT_SECONDS),
    ) -> None:
        self.limits = limits or rules.resolve_queue_limits(
            worker_count=worker_count,
            max_pending_jobs=max_pending_jobs,
            stale_after_seconds=stale_after_seconds,
            timeout_seconds=timeout_seconds,
        )
        self.worker_count = self.limits.worker_count
        self.store = store or JobStore()
        self._executor = ThreadPoolExecutor(max_workers=self.worker_count, thread_name_prefix="generation")
        self._futures: dict[str, Future[None]] = {}
        self._lock = RLock()
        self._closed = False

    def enqueue(
        self,
        job_id: str,
        task: Callable[..., Any],
        *args: Any,
        requested: int = 1,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        if not callable(task):
            raise TypeError("task must be callable")

        with self._lock:
            if self._closed:
                raise RuntimeError("generation queue is shut down")
            existing = self.store.get(job_id)
            if existing is not None:
                return existing
            admission = rules.evaluate_queue_admission(
                queued_count=len(self.store.list(status=rules.STATUS_QUEUED)),
                running_count=len(self.store.list(status=rules.STATUS_RUNNING)),
                limits=self.limits,
            )
            if not admission.allowed:
                raise RuntimeError(
                    f"generation queue admission denied: {admission.reason}"
                )
            job, created = self.store.reserve(job_id, requested=requested, metadata=metadata)
            if not created:
                return job
            if self._closed:
                self.store.cancel(job.job_id, error="queue shut down before execution")
                raise RuntimeError("generation queue is shut down")
            future = self._executor.submit(self._execute, job.job_id, task, args, kwargs)
            self._futures[job.job_id] = future
        return job

    def start(self, job_id: str) -> GenerationJob:
        return self.store.start(job_id)

    def progress(self, job_id: str, **kwargs: Any) -> GenerationJob:
        return self.store.progress(job_id, **kwargs)

    def complete(self, job_id: str, **kwargs: Any) -> GenerationJob:
        return self.store.complete(job_id, **kwargs)

    def fail(self, job_id: str, error: BaseException | str, **kwargs: Any) -> GenerationJob:
        return self.store.fail(job_id, error, **kwargs)

    def cancel(self, job_id: str, *, error: str | None = None) -> GenerationJob:
        job = self.store.cancel(job_id, error=error)
        with self._lock:
            future = self._futures.get(job.job_id)
            if future is not None:
                future.cancel()
        return job

    def get(self, job_id: str) -> GenerationJob | None:
        return self.store.get(job_id)

    def list(self, *, status: str | None = None) -> list[GenerationJob]:
        return self.store.list(status=status)

    def inspect(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, float | bool | str | None] | None:
        try:
            return self.store.inspect_timing(job_id, now=now, limits=self.limits)
        except KeyError:
            return None

    def list_stale(
        self,
        *,
        now: datetime | None = None,
        timed_out_only: bool = False,
    ) -> list[GenerationJob]:
        return self.store.list_stale(
            now=now,
            limits=self.limits,
            timed_out_only=timed_out_only,
        )

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            closed = self._closed
        return self.store.snapshot(now=now, limits=self.limits, closed=closed)

    def fail_timed_out(
        self,
        *,
        now: datetime | None = None,
        error: str = "generation job timed out",
    ) -> list[GenerationJob]:
        jobs = self.store.fail_timed_out(now=now, limits=self.limits, error=error)
        with self._lock:
            for job in jobs:
                future = self._futures.get(job.job_id)
                if future is not None:
                    future.cancel()
        return jobs

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            futures = tuple(self._futures.values())
        if not futures:
            return

        done, pending = wait(futures, timeout=timeout)
        if pending:
            raise TimeoutError(f"{len(pending)} generation job(s) still running")
        for future in done:
            try:
                future.result()
            except CancelledError:
                pass

    def shutdown(self, *, wait_for_jobs: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait_for_jobs, cancel_futures=not wait_for_jobs)

    def _execute(
        self,
        job_id: str,
        task: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        started = self.store.start(job_id)
        if started.status == rules.STATUS_CANCELED:
            return

        try:
            result = task(*args, **kwargs)
        except Exception as exc:
            self.store.fail(job_id, exc)
            return

        current = self.store.get(job_id)
        if current is not None and current.status in rules.TERMINAL_JOB_STATUSES:
            return
        self.store.complete(job_id, result=result)

    def __enter__(self) -> InMemoryGenerationQueue:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.shutdown()


def _ensure_transition(current_status: str, target_status: str) -> None:
    if current_status == rules.STATUS_QUEUED and target_status in rules.TERMINAL_JOB_STATUSES:
        rules.transition_job_status(current_status, rules.STATUS_RUNNING)
        rules.transition_job_status(rules.STATUS_RUNNING, target_status)
        return
    rules.transition_job_status(current_status, target_status)


def _validate_final_counts(requested: int, completed: int, failed: int, canceled: int) -> None:
    _pending_for(requested, completed, failed, canceled)


def _pending_for(requested: int, completed: int, failed: int, canceled: int) -> int:
    requested_count = _non_negative_int(requested, "requested")
    completed_count = _non_negative_int(completed, "completed")
    failed_count = _non_negative_int(failed, "failed")
    canceled_count = _non_negative_int(canceled, "canceled")
    finished = completed_count + failed_count + canceled_count
    if finished > requested_count:
        raise ValueError("completed, failed, and canceled cannot exceed requested")
    return requested_count - finished


def _normalize_job_id(job_id: str) -> str:
    if not isinstance(job_id, str):
        raise TypeError("job_id must be a string")
    normalized = job_id.strip()
    if not normalized:
        raise ValueError("job_id must not be empty")
    return normalized


def _normalize_status(status: str) -> str:
    return rules.normalize_job_status(status)


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _error_text(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        message = str(error)
        return f"{type(error).__name__}: {message}" if message else type(error).__name__
    return str(error)


def _snapshot(job: GenerationJob) -> GenerationJob:
    return replace(
        job,
        result=_copy_value(job.result),
        metadata=_copy_value(job.metadata or {}),
    )


def _empty_status_counts() -> dict[str, int]:
    return {
        rules.STATUS_QUEUED: 0,
        rules.STATUS_RUNNING: 0,
        rules.STATUS_COMPLETED: 0,
        rules.STATUS_FAILED: 0,
        rules.STATUS_CANCELED: 0,
    }


def _limits_payload(limits: rules.QueueLimits) -> dict[str, int | float]:
    return {
        "workerCount": limits.worker_count,
        "maxPendingJobs": limits.max_pending_jobs,
        "staleAfterSeconds": limits.stale_after_seconds,
        "timeoutSeconds": limits.timeout_seconds,
    }


def _copy_value(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _elapsed_seconds(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    if started_at is None or finished_at is None:
        return None
    return max(0.0, (finished_at - started_at).total_seconds())


__all__ = [
    "GenerationJob",
    "InMemoryGenerationQueue",
    "JobStore",
]
