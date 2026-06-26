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
JOB_SUCCEEDED = "succeeded"
JOB_PARTIAL = "partial"
JOB_COMPLETED = JOB_SUCCEEDED
JOB_PARTIALLY_FAILED = JOB_PARTIAL
JOB_FAILED = "failed"
JOB_REFUNDED = "refunded"
JOB_CANCELLED = "cancelled"
JOB_STATUSES = {
    JOB_CREATED,
    JOB_PAID,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_PARTIAL,
    JOB_PARTIALLY_FAILED,
    JOB_COMPLETED,
    "partially_failed",
    "completed",
    JOB_FAILED,
    JOB_REFUNDED,
    JOB_CANCELLED,
}
JOB_TERMINAL_STATUSES = {JOB_COMPLETED, JOB_FAILED, JOB_PARTIALLY_FAILED, JOB_REFUNDED, JOB_CANCELLED}

JOB_STAGE_MENU_PARSE = "menu_parse"
JOB_STAGE_STYLE_PREPARE = "style_prepare"
JOB_STAGE_PREVIEW_GENERATION = "preview_generation"
JOB_STAGE_FORMAL_GENERATION = "formal_generation"
JOB_STAGE_EXPORT_PACKAGE = "export_package"
JOB_STAGE_DEFINITIONS = (
    {"id": JOB_STAGE_MENU_PARSE, "label": "菜单解析"},
    {"id": JOB_STAGE_STYLE_PREPARE, "label": "风格准备"},
    {"id": JOB_STAGE_PREVIEW_GENERATION, "label": "样图生成"},
    {"id": JOB_STAGE_FORMAL_GENERATION, "label": "正式生图"},
    {"id": JOB_STAGE_EXPORT_PACKAGE, "label": "导出打包"},
)
JOB_STAGE_IDS = {stage["id"] for stage in JOB_STAGE_DEFINITIONS}
STAGE_PENDING = "pending"
STAGE_QUEUED = "queued"
STAGE_RUNNING = "running"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"
STAGE_STATUSES = {STAGE_PENDING, STAGE_QUEUED, STAGE_RUNNING, STAGE_COMPLETED, STAGE_FAILED, STAGE_SKIPPED}

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
    "running",
    "limited",
    "waiting",
    "waiting_for_provider",
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
        'succeeded',
        'partial',
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
    provider_error TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    refund_required INTEGER NOT NULL DEFAULT 0 CHECK (refund_required IN (0, 1)),
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_jobs_order_id_unique
    ON generation_jobs(order_id)
    WHERE order_id IS NOT NULL;
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
    initial_summary = _initial_result_summary(
        total=len(item_list),
        pending=len(item_list),
        source=(request_payload or {}).get("source") if isinstance(request_payload, dict) else None,
    )

    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if clean_order_id:
                existing = _job_row_by_order_id(conn, clean_order_id)
                if existing is not None:
                    conn.commit()
                    payload = _job_payload(conn, str(existing["id"]), include_items=True)
                    payload["idempotent"] = True
                    return payload
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
                    result_summary,
                    created_at,
                    updated_at,
                    paid_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _json_dumps(initial_summary),
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
        payload = _job_payload(conn, clean_job_id, include_items=True)
        payload["idempotent"] = False
        return payload


def get_job_by_order_id(
    order_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    clean_order_id = _clean_required(order_id, "order_id")
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        row = _job_row_by_order_id(conn, clean_order_id)
        if row is None:
            return None
        payload = _job_payload(conn, str(row["id"]), include_items=True)
        payload["idempotent"] = True
        return payload


def set_job_stage(
    job_id: str,
    stage_id: str,
    status: str,
    *,
    detail: str | None = None,
    error: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_required(job_id, "job_id")
    clean_stage_id = _clean_stage_id(stage_id)
    clean_status = _clean_stage_status(status)
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _job_row(conn, clean_job_id)
            request_payload = _json_loads(row["request_payload"], {})
            source = request_payload.get("source") if isinstance(request_payload, dict) else None
            summary = _json_loads(row["result_summary"], {})
            if not isinstance(summary, dict):
                summary = {}
            stages = _job_stages_from_summary(summary, source=source)
            stages = _set_stage_in_list(stages, clean_stage_id, clean_status, now=now, detail=detail, error=error)
            active_stage = next((stage for stage in stages if stage["id"] == clean_stage_id), None)
            summary["stages"] = stages
            summary["stage"] = active_stage
            if error:
                summary["error"] = str(error)[:1000]
            assignments = ["result_summary = ?", "updated_at = ?"]
            values: list[Any] = [_json_dumps(summary), now]
            if clean_status == STAGE_FAILED and error:
                assignments.append("error = ?")
                values.append(str(error)[:1000])
            values.append(clean_job_id)
            conn.execute(f"UPDATE generation_jobs SET {', '.join(assignments)} WHERE id = ?", values)
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
    provider_error: str | None = None,
    retryable: bool | None = None,
    refund_required: bool | None = None,
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
            next_result_value = result if result is not None else _json_loads(item["result"], {})
            next_result = _json_dumps(next_result_value)
            next_payload = _json_dumps(payload if payload is not None else _json_loads(item["payload"], {}))
            result_provider_error = None
            result_retryable = None
            result_refund_required = None
            if isinstance(next_result_value, dict):
                result_provider_error = next_result_value.get("provider_error") or next_result_value.get("providerError")
                result_retryable = next_result_value.get("retryable")
                result_refund_required = (
                    next_result_value.get("refund_required")
                    if "refund_required" in next_result_value
                    else next_result_value.get("refundRequired")
                )
            next_provider_error = (
                str(provider_error)
                if provider_error is not None
                else str(result_provider_error)
                if result_provider_error is not None
                else (item["provider_error"] if clean_status == ITEM_FAILED else None)
            )
            next_retryable = _bool_int(retryable if retryable is not None else result_retryable)
            next_refund_required = _bool_int(refund_required if refund_required is not None else result_refund_required)
            started_at = item["started_at"]
            completed_at = item["completed_at"]
            if clean_status == ITEM_RUNNING:
                started_at = started_at or now
                completed_at = None
                next_provider_error = None
                next_retryable = 0
                next_refund_required = 0
            elif clean_status in ITEM_TERMINAL_STATUSES:
                completed_at = now
            elif clean_status in {ITEM_PENDING, ITEM_QUEUED}:
                completed_at = None
                next_provider_error = None if clean_status == ITEM_QUEUED else next_provider_error
                next_retryable = 0 if clean_status == ITEM_QUEUED else next_retryable
                next_refund_required = 0 if clean_status == ITEM_QUEUED else next_refund_required

            conn.execute(
                """
                UPDATE generation_job_items
                SET status = ?,
                    attempts = attempts + ?,
                    provider = ?,
                    action = ?,
                    reason = ?,
                    error = ?,
                    provider_error = ?,
                    retryable = ?,
                    refund_required = ?,
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
                    next_provider_error,
                    next_retryable,
                    next_refund_required,
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
                        provider_error = NULL,
                        retryable = 0,
                        refund_required = 0,
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
                        provider_error = NULL,
                        retryable = 0,
                        refund_required = 0,
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
    set_job_stage(clean_job_id, JOB_STAGE_FORMAL_GENERATION, STAGE_QUEUED, detail="正式生图任务已排队", db_path=db_path)
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
        _record_run_summary(clean_job_id, run_summary, db_path=db_path)
        refreshed_stage = _formal_stage_status_from_job(refreshed)
        refreshed = set_job_stage(
            clean_job_id,
            JOB_STAGE_FORMAL_GENERATION,
            refreshed_stage,
            detail="没有待生成菜品" if refreshed_stage == STAGE_COMPLETED else "没有可启动的待生成菜品",
            error=refreshed.get("error") if refreshed_stage == STAGE_FAILED else None,
            db_path=db_path,
        )
        refreshed["lastRun"] = run_summary
        return refreshed

    mark_running(clean_job_id, db_path=db_path)
    set_job_stage(clean_job_id, JOB_STAGE_FORMAL_GENERATION, STAGE_RUNNING, detail=f"正在生成 {len(items)} 张正式图", db_path=db_path)
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
                provider_error=message,
                retryable=True,
                refund_required=True,
                result={
                    "status": "failed",
                    "error": message,
                    "provider_error": message,
                    "providerError": message,
                    "retryable": True,
                    "refund_required": True,
                    "refundRequired": True,
                },
                db_path=db_path,
            )
            run_summary["failed"] += 1
            run_summary["errors"].append({"index": index, "dish": item.get("dish"), "error": message})
            continue

        item_status, error = _item_status_from_runner_result(result)
        if item_status == ITEM_FAILED:
            provider_error = _optional_text(result.get("provider_error") or result.get("providerError") or error or result.get("error"))
            record_item_status(
                clean_job_id,
                index,
                ITEM_FAILED,
                provider=_optional_text(result.get("provider")),
                action=_optional_text(result.get("action")),
                reason=_optional_text(result.get("reason")),
                error=error or _optional_text(result.get("error")),
                provider_error=provider_error,
                retryable=_optional_bool(result.get("retryable")),
                refund_required=_optional_bool(result.get("refund_required") if "refund_required" in result else result.get("refundRequired")),
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
                provider_error=_optional_text(result.get("provider_error") or result.get("providerError")),
                retryable=_optional_bool(result.get("retryable")),
                refund_required=_optional_bool(result.get("refund_required") if "refund_required" in result else result.get("refundRequired")),
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
                provider_error=_optional_text(result.get("provider_error") or result.get("providerError")),
                retryable=_optional_bool(result.get("retryable")),
                refund_required=_optional_bool(result.get("refund_required") if "refund_required" in result else result.get("refundRequired")),
                result=result,
                payload=result.get("payload") if isinstance(result.get("payload"), dict) else None,
                db_path=db_path,
            )
            run_summary["completed"] += 1

    job = refresh_job_status(clean_job_id, db_path=db_path)
    _record_run_summary(clean_job_id, run_summary, db_path=db_path)
    stage_status = _formal_stage_status_from_job(job)
    detail = f"本批完成 {run_summary['completed']} 张，失败 {run_summary['failed']} 张，延后 {run_summary['deferred']} 张"
    job = set_job_stage(
        clean_job_id,
        JOB_STAGE_FORMAL_GENERATION,
        stage_status,
        detail=detail,
        error=job.get("error") if stage_status == STAGE_FAILED else None,
        db_path=db_path,
    )
    job["lastRun"] = run_summary
    return job


def _record_run_summary(
    job_id: str,
    run_summary: dict[str, Any],
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> None:
    clean_job_id = _clean_required(job_id, "job_id")
    now = _now()
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _job_row(conn, clean_job_id)
            summary = _json_loads(row["result_summary"], {})
            if not isinstance(summary, dict):
                summary = {}
            summary["lastRun"] = run_summary
            conn.execute(
                "UPDATE generation_jobs SET result_summary = ?, updated_at = ? WHERE id = ?",
                (_json_dumps(summary), now, clean_job_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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
    _ensure_column(conn, "generation_job_items", "provider_error", "TEXT")
    _ensure_column(conn, "generation_job_items", "retryable", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "generation_job_items", "refund_required", "INTEGER NOT NULL DEFAULT 0")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    current_status = _canonical_job_status(row["status"])
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
    current_status = _canonical_job_status(job["status"])
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

    existing_summary = _json_loads(job["result_summary"], {})
    summary = existing_summary if isinstance(existing_summary, dict) else {}
    summary.update({
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "queued": queued,
        "running": running,
        "remaining": remaining,
    })
    if next_status in {JOB_FAILED, JOB_PARTIALLY_FAILED}:
        errors = _failed_item_errors(conn, job_id)
        if errors:
            summary["errors"] = errors
    error_value = job["error"]
    if next_status == JOB_FAILED:
        error_value = error_value or _first_failed_item_error(conn, job_id) or "Generation job failed"
    elif next_status not in {JOB_FAILED, JOB_PARTIALLY_FAILED}:
        error_value = None
    conn.execute(
        """
        UPDATE generation_jobs
        SET status = ?,
            completed_items = ?,
            failed_items = ?,
            pending_items = ?,
            error = ?,
            result_summary = ?,
            updated_at = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (next_status, completed, failed, remaining, error_value, _json_dumps(summary), now, completed_at, job_id),
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


def _job_row_by_order_id(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM generation_jobs
        WHERE order_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (order_id,),
    ).fetchone()


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
    refund_required = any(bool(item.get("refundRequired")) for item in items)
    retryable_failed = any(item.get("status") == ITEM_FAILED and bool(item.get("retryable")) for item in items)
    percent = 100 if total == 0 else round((completed + failed) / total * 100, 1)
    request_payload = _json_loads(row["request_payload"], {})
    result_summary = _json_loads(row["result_summary"], {})
    if not isinstance(result_summary, dict):
        result_summary = {}
    source = request_payload.get("source") if isinstance(request_payload, dict) else None
    stages = _reconciled_job_stages(row, result_summary, source=source)
    stage = _active_stage_from_stages(stages, result_summary)
    payload = {
        "id": row["id"],
        "userId": row["user_id"],
        "status": _canonical_job_status(row["status"]),
        "style": row["style"],
        "quality": row["quality"],
        "totalItems": total,
        "completedItems": completed,
        "failedItems": failed,
        "pendingItems": pending,
        "points": int(row["points"]),
        "orderId": row["order_id"],
        "error": row["error"],
        "refundRequired": refund_required,
        "refund_required": refund_required,
        "retryableFailed": retryable_failed,
        "request": request_payload,
        "planSnapshot": _json_loads(row["plan_snapshot"], {}),
        "resultSummary": result_summary,
        "stage": stage,
        "phase": stage,
        "stages": stages,
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
    if isinstance(result_summary.get("lastRun"), dict):
        payload["lastRun"] = result_summary["lastRun"]
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
        "providerError": row["provider_error"],
        "provider_error": row["provider_error"],
        "retryable": bool(row["retryable"]),
        "refundRequired": bool(row["refund_required"]),
        "refund_required": bool(row["refund_required"]),
        "payload": _json_loads(row["payload"], {}),
        "result": _json_loads(row["result"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
    }


def _initial_result_summary(total: int, pending: int, source: Any = None) -> dict[str, Any]:
    stages = _default_job_stages(source=source)
    return {
        "total": total,
        "completed": 0,
        "failed": 0,
        "pending": pending,
        "queued": 0,
        "running": 0,
        "remaining": pending,
        "stage": next(stage for stage in stages if stage["id"] == JOB_STAGE_FORMAL_GENERATION),
        "stages": stages,
    }


def _default_job_stages(source: Any = None) -> list[dict[str, Any]]:
    preview_status = STAGE_SKIPPED if str(source or "") == "direct_items" else STAGE_COMPLETED
    initial_statuses = {
        JOB_STAGE_MENU_PARSE: STAGE_COMPLETED,
        JOB_STAGE_STYLE_PREPARE: STAGE_COMPLETED,
        JOB_STAGE_PREVIEW_GENERATION: preview_status,
        JOB_STAGE_FORMAL_GENERATION: STAGE_PENDING,
        JOB_STAGE_EXPORT_PACKAGE: STAGE_PENDING,
    }
    return [
        {
            **definition,
            "status": initial_statuses[str(definition["id"])],
            "detail": "",
            "error": "",
        }
        for definition in JOB_STAGE_DEFINITIONS
    ]


def _job_stages_from_summary(summary: dict[str, Any], *, source: Any = None) -> list[dict[str, Any]]:
    existing = summary.get("stages") if isinstance(summary, dict) else None
    by_id = {
        str(stage.get("id")): stage
        for stage in existing
        if isinstance(existing, list) and isinstance(stage, dict) and str(stage.get("id")) in JOB_STAGE_IDS
    } if isinstance(existing, list) else {}
    stages = []
    for default in _default_job_stages(source=source):
        merged = {**default, **by_id.get(str(default["id"]), {})}
        merged["id"] = str(default["id"])
        merged["label"] = str(default["label"])
        try:
            merged["status"] = _clean_stage_status(merged.get("status") or default["status"])
        except InvalidJobInput:
            merged["status"] = str(default["status"])
        merged["detail"] = str(merged.get("detail") or "")
        merged["error"] = str(merged.get("error") or "")
        stages.append(merged)
    return stages


def _reconciled_job_stages(row: sqlite3.Row, summary: dict[str, Any], *, source: Any = None) -> list[dict[str, Any]]:
    stages = _job_stages_from_summary(summary, source=source)
    status = _canonical_job_status(row["status"])
    formal_status = _stage_status_from_job_status(status)
    stages = _set_stage_in_list(
        stages,
        JOB_STAGE_FORMAL_GENERATION,
        formal_status,
        now=str(row["updated_at"] or ""),
        detail=None,
        error=str(row["error"] or "") if formal_status == STAGE_FAILED else None,
        preserve_existing=True,
    )
    return stages


def _active_stage_from_stages(stages: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    summary_stage = summary.get("stage") if isinstance(summary, dict) else None
    active_id = str(summary_stage.get("id") or "") if isinstance(summary_stage, dict) else ""
    active = next((stage for stage in stages if stage["id"] == active_id and stage["status"] in {STAGE_QUEUED, STAGE_RUNNING, STAGE_FAILED}), None)
    if active:
        return active
    for status in (STAGE_RUNNING, STAGE_QUEUED, STAGE_FAILED, STAGE_PENDING):
        active = next((stage for stage in stages if stage["status"] == status), None)
        if active:
            return active
    return stages[-1] if stages else {"id": "", "label": "", "status": STAGE_PENDING, "detail": "", "error": ""}


def _set_stage_in_list(
    stages: list[dict[str, Any]],
    stage_id: str,
    status: str,
    *,
    now: str,
    detail: str | None = None,
    error: str | None = None,
    preserve_existing: bool = False,
) -> list[dict[str, Any]]:
    clean_stage_id = _clean_stage_id(stage_id)
    clean_status = _clean_stage_status(status)
    output = []
    for stage in stages:
        if stage["id"] != clean_stage_id:
            output.append(stage)
            continue
        next_stage = dict(stage)
        if not preserve_existing or stage.get("status") != clean_status:
            next_stage["status"] = clean_status
        if detail is not None:
            next_stage["detail"] = str(detail)[:500]
        if error is not None:
            next_stage["error"] = str(error)[:1000]
        elif clean_status != STAGE_FAILED and not preserve_existing:
            next_stage["error"] = ""
        if clean_status in {STAGE_QUEUED, STAGE_RUNNING}:
            next_stage.setdefault("startedAt", now)
            next_stage.pop("completedAt", None)
        if clean_status in {STAGE_COMPLETED, STAGE_FAILED, STAGE_SKIPPED}:
            next_stage.setdefault("startedAt", now)
            next_stage["completedAt"] = now
        output.append(next_stage)
    return output


def _stage_status_from_job_status(status: str) -> str:
    clean = _canonical_job_status(status)
    if clean == JOB_RUNNING:
        return STAGE_RUNNING
    if clean == JOB_QUEUED:
        return STAGE_QUEUED
    if clean in {JOB_COMPLETED}:
        return STAGE_COMPLETED
    if clean in {JOB_FAILED, JOB_PARTIALLY_FAILED}:
        return STAGE_FAILED
    return STAGE_PENDING


def _formal_stage_status_from_job(job: dict[str, Any]) -> str:
    return _stage_status_from_job_status(str(job.get("status") or ""))


def _failed_item_errors(conn: sqlite3.Connection, job_id: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT item_index, dish, error, provider_error
        FROM generation_job_items
        WHERE job_id = ? AND status = ?
        ORDER BY item_index ASC
        LIMIT ?
        """,
        (job_id, ITEM_FAILED, limit),
    ).fetchall()
    return [
        {
            "index": int(row["item_index"]),
            "dish": row["dish"],
            "message": str(row["provider_error"] or row["error"] or "生成失败"),
        }
        for row in rows
    ]


def _first_failed_item_error(conn: sqlite3.Connection, job_id: str) -> str | None:
    errors = _failed_item_errors(conn, job_id, limit=1)
    return str(errors[0]["message"]) if errors else None


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
    clean = _canonical_job_status(clean)
    if clean not in JOB_STATUSES:
        raise InvalidJobInput("Unsupported job status", status=status)
    return clean


def _clean_stage_id(stage_id: Any) -> str:
    clean = str(stage_id or "").strip()
    if clean not in JOB_STAGE_IDS:
        raise InvalidJobInput("Unsupported job stage", stage=stage_id)
    return clean


def _clean_stage_status(status: Any) -> str:
    clean = str(status or "").strip().lower()
    if clean not in STAGE_STATUSES:
        raise InvalidJobInput("Unsupported job stage status", status=status)
    return clean


def _canonical_job_status(status: Any) -> str:
    clean = str(status or "").strip().lower()
    if clean == "completed":
        return JOB_SUCCEEDED
    if clean == "partially_failed":
        return JOB_PARTIAL
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


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return bool(text)


def _bool_int(value: Any) -> int:
    parsed = _optional_bool(value)
    return 1 if parsed else 0
