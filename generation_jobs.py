from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "generation_jobs.db"

JOB_CREATED = "created"
JOB_PAID = "paid"
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_PARTIALLY_FAILED = "partially_failed"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_REFUNDED = "refunded"
JOB_CANCELLED = "cancelled"
JOB_STATUSES = {
    JOB_CREATED,
    JOB_PAID,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_PARTIALLY_FAILED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_REFUNDED,
    JOB_CANCELLED,
}
JOB_TERMINAL_STATUSES = {JOB_COMPLETED, JOB_FAILED, JOB_PARTIALLY_FAILED, JOB_REFUNDED, JOB_CANCELLED}

ITEM_PENDING = "pending"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_COMPLETED = "completed"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"
ITEM_STATUSES = {ITEM_PENDING, ITEM_QUEUED, ITEM_RUNNING, ITEM_COMPLETED, ITEM_FAILED, ITEM_SKIPPED}
ITEM_DONE_STATUSES = {ITEM_COMPLETED, ITEM_SKIPPED}
ITEM_TERMINAL_STATUSES = ITEM_DONE_STATUSES | {ITEM_FAILED}
RUNNABLE_ITEM_STATUSES = {ITEM_PENDING, ITEM_QUEUED}

SUCCESS_RESULT_STATUSES = {
    "completed",
    "succeeded",
    "cached",
    "reused",
    "fallback",
    "skipped",
}
DEFERRED_RESULT_STATUSES = {
    "created",
    "pending",
    "queued",
    "limited",
    "waiting",
}
FAILED_RESULT_STATUSES = {"failed", "error"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'created',
        'paid',
        'queued',
        'running',
        'partially_failed',
        'completed',
        'failed',
        'refunded',
        'cancelled'
    )),
    style TEXT NOT NULL,
    quality TEXT NOT NULL,
    total_items INTEGER NOT NULL DEFAULT 0 CHECK (total_items >= 0),
    completed_items INTEGER NOT NULL DEFAULT 0 CHECK (completed_items >= 0),
    failed_items INTEGER NOT NULL DEFAULT 0 CHECK (failed_items >= 0),
    pending_items INTEGER NOT NULL DEFAULT 0 CHECK (pending_items >= 0),
    points INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    order_id TEXT,
    error TEXT,
    request_payload TEXT NOT NULL DEFAULT '{}',
    plan_snapshot TEXT NOT NULL DEFAULT '{}',
    result_summary TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    queued_at TEXT,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS generation_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    item_index INTEGER NOT NULL CHECK (item_index > 0),
    row_no INTEGER,
    dish TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN (
        'pending',
        'queued',
        'running',
        'completed',
        'failed',
        'skipped'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    provider TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    reason TEXT,
    error TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (job_id) REFERENCES generation_jobs(id) ON DELETE CASCADE,
    UNIQUE (job_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_generation_jobs_user_created ON generation_jobs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status);
CREATE INDEX IF NOT EXISTS idx_generation_job_items_job_status ON generation_job_items(job_id, status, item_index);
"""


class GenerationJobError(Exception):
    code = "generation_job_error"
    status_code = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}


class JobNotFound(GenerationJobError):
    code = "job_not_found"
    status_code = 404


class InvalidJobInput(GenerationJobError):
    code = "invalid_job_input"


def resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    configured = os.environ.get("GENERATION_JOBS_DB_PATH")
    return Path(configured) if configured else DEFAULT_DB_PATH


@contextmanager
def open_db(db_path: str | os.PathLike[str] | None = None) -> Iterable[sqlite3.Connection]:
    path = resolve_db_path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | os.PathLike[str] | None = None) -> Path:
    path = resolve_db_path(db_path)
    with open_db(path) as conn:
        _ensure_schema(conn)
        conn.commit()
    return path


def create_job(
    *,
    user_id: str,
    style: str,
    quality: str = "standard",
    items: Sequence[dict[str, Any]],
    request_payload: dict[str, Any] | None = None,
    plan_snapshot: dict[str, Any] | None = None,
    points: int = 0,
    order_id: str | None = None,
    mark_paid: bool = False,
    job_id: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_user_id = _clean_required(user_id, "user_id")
    clean_style = _clean_required(style, "style")
    clean_quality = _clean_required(quality, "quality")
    clean_job_id = _clean_required(job_id or uuid.uuid4().hex, "job_id")
    clean_order_id = _clean_optional(order_id)
    clean_points = _non_negative_int(points, "points")
    now = _now()
    status = JOB_PAID if mark_paid or clean_order_id else JOB_CREATED
    paid_at = now if status == JOB_PAID else None
    item_list = list(items)

    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO generation_jobs (
                    id,
                    user_id,
                    status,
                    style,
                    quality,
                    total_items,
                    completed_items,
                    failed_items,
                    pending_items,
                    points,
                    order_id,
                    request_payload,
                    plan_snapshot,
                    created_at,
                    updated_at,
                    paid_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_job_id,
                    clean_user_id,
                    status,
                    clean_style,
                    clean_quality,
                    len(item_list),
                    len(item_list),
                    clean_points,
                    clean_order_id,
                    _json_dumps(request_payload or {}),
                    _json_dumps(plan_snapshot or {}),
                    now,
                    now,
                    paid_at,
                ),
            )
            for index, item in enumerate(item_list, start=1):
                _insert_item(conn, clean_job_id, index, item, now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _job_payload(conn, clean_job_id, include_items=True)


def get_job(
    job_id: str,
    *,
    include_items: bool = True,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        return _job_payload(conn, _clean_required(job_id, "job_id"), include_items=include_items)


def mark_paid(
    job_id: str,
    *,
    order_id: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    clean_order_id = _clean_optional(order_id)
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _job_row(conn, clean_job_id)
            status = str(row["status"])
            next_status = JOB_PAID if status == JOB_CREATED else status
            conn.execute(
                """
                UPDATE generation_jobs
                SET status = ?,
                    order_id = COALESCE(?, order_id),
                    paid_at = COALESCE(paid_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (next_status, clean_order_id, now, now, clean_job_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _job_payload(conn, clean_job_id, include_items=True)


def mark_queued(
    job_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    return _mark_job_status(job_id, JOB_QUEUED, timestamp_column="queued_at", db_path=db_path)


def mark_running(
    job_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    return _mark_job_status(job_id, JOB_RUNNING, timestamp_column="started_at", db_path=db_path)


def mark_completed(
    job_id: str,
    *,
    result_summary: dict[str, Any] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    return _mark_job_status(
        job_id,
        JOB_COMPLETED,
        timestamp_column="completed_at",
        result_summary=result_summary,
        db_path=db_path,
    )


def mark_failed(
    job_id: str,
    error: str,
    *,
    result_summary: dict[str, Any] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    return _mark_job_status(
        job_id,
        JOB_FAILED,
        timestamp_column="completed_at",
        error=str(error),
        result_summary=result_summary,
        db_path=db_path,
    )


def record_item_status(
    job_id: str,
    item_index: int,
    status: str,
    *,
    provider: str | None = None,
    action: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    result: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    increment_attempt: bool = False,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    clean_status = _clean_item_status(status)
    clean_index = _positive_int(item_index, "item_index")
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _job_row(conn, clean_job_id)
            item = _item_row(conn, clean_job_id, clean_index)
            next_provider = str(provider) if provider is not None else str(item["provider"] or "")
            next_action = str(action) if action is not None else str(item["action"] or "")
            next_reason = str(reason) if reason is not None else item["reason"]
            next_error = str(error) if error is not None else (item["error"] if clean_status == ITEM_FAILED else None)
            next_result = _json_dumps(result if result is not None else _json_loads(item["result"], {}))
            next_payload = _json_dumps(payload if payload is not None else _json_loads(item["payload"], {}))
            started_at = item["started_at"]
            completed_at = item["completed_at"]
            if clean_status == ITEM_RUNNING:
                started_at = started_at or now
                completed_at = None
            elif clean_status in ITEM_TERMINAL_STATUSES:
                completed_at = now
            elif clean_status in {ITEM_PENDING, ITEM_QUEUED}:
                completed_at = None

            conn.execute(
                """
                UPDATE generation_job_items
                SET status = ?,
                    attempts = attempts + ?,
                    provider = ?,
                    action = ?,
                    reason = ?,
                    error = ?,
                    payload = ?,
                    result = ?,
                    updated_at = ?,
                    started_at = ?,
                    completed_at = ?
                WHERE job_id = ? AND item_index = ?
                """,
                (
                    clean_status,
                    1 if increment_attempt else 0,
                    next_provider,
                    next_action,
                    next_reason,
                    next_error,
                    next_payload,
                    next_result,
                    now,
                    started_at,
                    completed_at,
                    clean_job_id,
                    clean_index,
                ),
            )
            _refresh_job_status(conn, clean_job_id, now=now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _job_payload(conn, clean_job_id, include_items=True)


def runnable_items(
    job_id: str,
    *,
    limit: int | None = None,
    statuses: Iterable[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    clean_job_id = _clean_required(job_id, "job_id")
    clean_statuses = tuple(_clean_item_status(status) for status in (statuses or RUNNABLE_ITEM_STATUSES))
    clean_limit = _optional_positive_int(limit, "limit")
    if not clean_statuses:
        return []
    placeholders = ",".join("?" for _ in clean_statuses)
    query = f"""
        SELECT *
        FROM generation_job_items
        WHERE job_id = ? AND status IN ({placeholders})
        ORDER BY item_index ASC
    """
    params: list[Any] = [clean_job_id, *clean_statuses]
    if clean_limit is not None:
        query += " LIMIT ?"
        params.append(clean_limit)
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        _job_row(conn, clean_job_id)
        return [_item_payload(row) for row in conn.execute(query, params).fetchall()]


def retry_failed_items(
    job_id: str,
    *,
    item_indexes: Sequence[int] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    indexes = [_positive_int(index, "item_index") for index in item_indexes] if item_indexes is not None else None
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _job_row(conn, clean_job_id)
            if indexes:
                placeholders = ",".join("?" for _ in indexes)
                conn.execute(
                    f"""
                    UPDATE generation_job_items
                    SET status = ?,
                        error = NULL,
                        updated_at = ?,
                        completed_at = NULL
                    WHERE job_id = ? AND status = ? AND item_index IN ({placeholders})
                    """,
                    (ITEM_QUEUED, now, clean_job_id, ITEM_FAILED, *indexes),
                )
            else:
                conn.execute(
                    """
                    UPDATE generation_job_items
                    SET status = ?,
                        error = NULL,
                        updated_at = ?,
                        completed_at = NULL
                    WHERE job_id = ? AND status = ?
                    """,
                    (ITEM_QUEUED, now, clean_job_id, ITEM_FAILED),
                )
            _set_job_status(conn, clean_job_id, JOB_QUEUED, now=now, timestamp_column="queued_at")
            _refresh_job_status(conn, clean_job_id, now=now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _job_payload(conn, clean_job_id, include_items=True)


def run_job(
    job_id: str,
    runner: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    limit: int | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    clean_limit = _optional_positive_int(limit, "limit")
    job = mark_queued(clean_job_id, db_path=db_path)
    items = runnable_items(clean_job_id, limit=clean_limit, db_path=db_path)
    run_summary = {
        "selected": len(items),
        "completed": 0,
        "failed": 0,
        "deferred": 0,
        "errors": [],
    }
    if not items:
        refreshed = refresh_job_status(clean_job_id, db_path=db_path)
        refreshed["lastRun"] = run_summary
        return refreshed

    mark_running(clean_job_id, db_path=db_path)
    for item in items:
        index = int(item["index"])
        record_item_status(clean_job_id, index, ITEM_RUNNING, increment_attempt=True, db_path=db_path)
        try:
            result = runner(item) or {}
        except Exception as exc:
            message = str(exc)[:500]
            record_item_status(
                clean_job_id,
                index,
                ITEM_FAILED,
                error=message,
                result={"status": "failed", "error": message},
                db_path=db_path,
            )
            run_summary["failed"] += 1
            run_summary["errors"].append({"index": index, "dish": item.get("dish"), "error": message})
            continue

        item_status, error = _item_status_from_runner_result(result)
        if item_status == ITEM_FAILED:
            record_item_status(
                clean_job_id,
                index,
                ITEM_FAILED,
                provider=_optional_text(result.get("provider")),
                action=_optional_text(result.get("action")),
                reason=_optional_text(result.get("reason")),
                error=error or _optional_text(result.get("error")),
                result=result,
                payload=result.get("payload") if isinstance(result.get("payload"), dict) else None,
                db_path=db_path,
            )
            run_summary["failed"] += 1
            run_summary["errors"].append({"index": index, "dish": item.get("dish"), "error": error or result.get("error")})
        elif item_status == ITEM_QUEUED:
            record_item_status(
                clean_job_id,
                index,
                ITEM_QUEUED,
                provider=_optional_text(result.get("provider")),
                action=_optional_text(result.get("action")),
                reason=_optional_text(result.get("reason")),
                result=result,
                payload=result.get("payload") if isinstance(result.get("payload"), dict) else None,
                db_path=db_path,
            )
            run_summary["deferred"] += 1
        else:
            record_item_status(
                clean_job_id,
                index,
                item_status,
                provider=_optional_text(result.get("provider")),
                action=_optional_text(result.get("action")),
                reason=_optional_text(result.get("reason")),
                result=result,
                payload=result.get("payload") if isinstance(result.get("payload"), dict) else None,
                db_path=db_path,
            )
            run_summary["completed"] += 1

    job = refresh_job_status(clean_job_id, db_path=db_path)
    job["lastRun"] = run_summary
    return job


def refresh_job_status(
    job_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _job_row(conn, clean_job_id)
            _refresh_job_status(conn, clean_job_id, now=now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _job_payload(conn, clean_job_id, include_items=True)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _insert_item(conn: sqlite3.Connection, job_id: str, index: int, item: dict[str, Any], now: str) -> None:
    row_no = _optional_int(item.get("row"), "row")
    dish = str(item.get("name") or item.get("dish") or item.get("dishName") or "")
    category = str(item.get("category") or "")
    kind = str(item.get("kind") or "")
    conn.execute(
        """
        INSERT INTO generation_job_items (
            id,
            job_id,
            item_index,
            row_no,
            dish,
            category,
            kind,
            status,
            payload,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{job_id}:{index}",
            job_id,
            index,
            row_no,
            dish,
            category,
            kind,
            ITEM_PENDING,
            _json_dumps(item),
            now,
            now,
        ),
    )


def _mark_job_status(
    job_id: str,
    status: str,
    *,
    timestamp_column: str | None = None,
    error: str | None = None,
    result_summary: dict[str, Any] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    clean_status = _clean_job_status(status)
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _job_row(conn, clean_job_id)
            _set_job_status(
                conn,
                clean_job_id,
                clean_status,
                now=now,
                timestamp_column=timestamp_column,
                error=error,
                result_summary=result_summary,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _job_payload(conn, clean_job_id, include_items=True)


def _set_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    now: str,
    timestamp_column: str | None = None,
    error: str | None = None,
    result_summary: dict[str, Any] | None = None,
) -> None:
    row = _job_row(conn, job_id)
    current_status = str(row["status"])
    if current_status in {JOB_REFUNDED, JOB_CANCELLED} and status not in {JOB_REFUNDED, JOB_CANCELLED}:
        return
    values: list[Any] = [status, now]
    assignments = ["status = ?", "updated_at = ?"]
    if timestamp_column:
        if timestamp_column not in {"paid_at", "queued_at", "started_at", "completed_at"}:
            raise InvalidJobInput("Unsupported job timestamp column", column=timestamp_column)
        assignments.append(f"{timestamp_column} = COALESCE({timestamp_column}, ?)")
        values.append(now)
    if error is not None:
        assignments.append("error = ?")
        values.append(str(error)[:1000])
    elif status not in {JOB_FAILED, JOB_PARTIALLY_FAILED}:
        assignments.append("error = NULL")
    if result_summary is not None:
        assignments.append("result_summary = ?")
        values.append(_json_dumps(result_summary))
    values.append(job_id)
    conn.execute(f"UPDATE generation_jobs SET {', '.join(assignments)} WHERE id = ?", values)


def _refresh_job_status(conn: sqlite3.Connection, job_id: str, *, now: str) -> None:
    job = _job_row(conn, job_id)
    current_status = str(job["status"])
    if current_status in {JOB_REFUNDED, JOB_CANCELLED}:
        return
    counts = _item_counts(conn, job_id)
    total = int(job["total_items"])
    completed = int(counts.get(ITEM_COMPLETED, 0)) + int(counts.get(ITEM_SKIPPED, 0))
    failed = int(counts.get(ITEM_FAILED, 0))
    running = int(counts.get(ITEM_RUNNING, 0))
    queued = int(counts.get(ITEM_QUEUED, 0))
    pending = int(counts.get(ITEM_PENDING, 0))
    remaining = max(total - completed - failed, 0)

    next_status = current_status
    completed_at: str | None = job["completed_at"]
    if total == 0:
        next_status = JOB_COMPLETED
        completed_at = completed_at or now
    elif running > 0:
        next_status = JOB_RUNNING
        completed_at = None
    elif remaining > 0:
        if current_status not in {JOB_CREATED, JOB_PAID}:
            next_status = JOB_QUEUED
        completed_at = None
    elif failed == 0:
        next_status = JOB_COMPLETED
        completed_at = completed_at or now
    elif completed == 0:
        next_status = JOB_FAILED
        completed_at = completed_at or now
    else:
        next_status = JOB_PARTIALLY_FAILED
        completed_at = completed_at or now

    summary = {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "queued": queued,
        "running": running,
        "remaining": remaining,
    }
    conn.execute(
        """
        UPDATE generation_jobs
        SET status = ?,
            completed_items = ?,
            failed_items = ?,
            pending_items = ?,
            result_summary = ?,
            updated_at = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (next_status, completed, failed, remaining, _json_dumps(summary), now, completed_at, job_id),
    )


def _item_counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM generation_job_items
        WHERE job_id = ?
        GROUP BY status
        """,
        (job_id,),
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _job_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM generation_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound("Generation job not found", jobId=job_id)
    return row


def _item_row(conn: sqlite3.Connection, job_id: str, item_index: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM generation_job_items WHERE job_id = ? AND item_index = ?",
        (job_id, item_index),
    ).fetchone()
    if row is None:
        raise JobNotFound("Generation job item not found", jobId=job_id, itemIndex=item_index)
    return row


def _job_payload(conn: sqlite3.Connection, job_id: str, *, include_items: bool = True) -> dict[str, Any]:
    row = _job_row(conn, job_id)
    items = [
        _item_payload(item_row)
        for item_row in conn.execute(
            "SELECT * FROM generation_job_items WHERE job_id = ? ORDER BY item_index ASC",
            (job_id,),
        ).fetchall()
    ] if include_items else []
    counts = _item_counts(conn, job_id)
    total = int(row["total_items"])
    completed = int(row["completed_items"])
    failed = int(row["failed_items"])
    pending = int(row["pending_items"])
    running = int(counts.get(ITEM_RUNNING, 0))
    queued = int(counts.get(ITEM_QUEUED, 0))
    percent = 100 if total == 0 else round((completed + failed) / total * 100, 1)
    payload = {
        "id": row["id"],
        "userId": row["user_id"],
        "status": row["status"],
        "style": row["style"],
        "quality": row["quality"],
        "totalItems": total,
        "completedItems": completed,
        "failedItems": failed,
        "pendingItems": pending,
        "points": int(row["points"]),
        "orderId": row["order_id"],
        "error": row["error"],
        "request": _json_loads(row["request_payload"], {}),
        "planSnapshot": _json_loads(row["plan_snapshot"], {}),
        "resultSummary": _json_loads(row["result_summary"], {}),
        "progress": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "queued": queued,
            "running": running,
            "percent": percent,
        },
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "paidAt": row["paid_at"],
        "queuedAt": row["queued_at"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
    }
    if include_items:
        payload["items"] = items
    return payload


def _item_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "jobId": row["job_id"],
        "index": int(row["item_index"]),
        "row": row["row_no"],
        "dish": row["dish"],
        "category": row["category"],
        "kind": row["kind"],
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "provider": row["provider"],
        "action": row["action"],
        "reason": row["reason"],
        "error": row["error"],
        "payload": _json_loads(row["payload"], {}),
        "result": _json_loads(row["result"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
    }


def _item_status_from_runner_result(result: dict[str, Any]) -> tuple[str, str | None]:
    explicit = str(result.get("itemStatus") or "").strip().lower()
    if explicit:
        if explicit in ITEM_STATUSES:
            return explicit, _optional_text(result.get("error"))
        raise InvalidJobInput("Unsupported runner item status", status=explicit)
    status = str(result.get("status") or "").strip().lower()
    generation = result.get("generation")
    if not status and isinstance(generation, dict):
        status = str(generation.get("status") or "").strip().lower()
    if status in FAILED_RESULT_STATUSES:
        return ITEM_FAILED, _optional_text(result.get("error")) or _optional_text((generation or {}).get("error") if isinstance(generation, dict) else None)
    if status in DEFERRED_RESULT_STATUSES:
        return ITEM_QUEUED, _optional_text(result.get("error"))
    if status in SUCCESS_RESULT_STATUSES or not status:
        return ITEM_COMPLETED, None
    return ITEM_FAILED, _optional_text(result.get("error")) or f"Unsupported generation result status: {status}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean_required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidJobInput(f"{name} is required", field=name)
    if len(text) > 200:
        raise InvalidJobInput(f"{name} is too long", field=name, maxLength=200)
    return text


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_job_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean not in JOB_STATUSES:
        raise InvalidJobInput("Unsupported job status", status=status)
    return clean


def _clean_item_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean not in ITEM_STATUSES:
        raise InvalidJobInput("Unsupported item status", status=status)
    return clean


def _positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except Exception as exc:
        raise InvalidJobInput(f"{name} must be an integer", field=name) from exc
    if number <= 0:
        raise InvalidJobInput(f"{name} must be positive", field=name, value=value)
    return number


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, name)


def _non_negative_int(value: Any, name: str) -> int:
    try:
        number = int(value or 0)
    except Exception as exc:
        raise InvalidJobInput(f"{name} must be an integer", field=name) from exc
    if number < 0:
        raise InvalidJobInput(f"{name} must be non-negative", field=name, value=value)
    return number


def _optional_int(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception as exc:
        raise InvalidJobInput(f"{name} must be an integer", field=name, value=value) from exc


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
