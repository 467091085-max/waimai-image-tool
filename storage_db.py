from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "storage.sqlite3"
DEFAULT_OBJECT_STORE_DIR = DATA_DIR / "object_store"
SCHEMA_VERSION = 2

REQUIRED_TABLES = {
    "menus",
    "menu_items",
    "menu_uploads",
    "library_images",
    "generation_jobs",
    "generation_job_items",
    "generation_results",
    "exports",
    "generated_images",
    "export_packages",
    "point_ledger",
}

MENU_STATUSES = {"uploaded", "parsed", "failed", "archived"}
JOB_STATUSES = {"queued", "running", "succeeded", "failed", "canceled"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}
VALID_JOB_TRANSITIONS = {
    "queued": {"queued", "running", "failed", "canceled"},
    "running": {"running", "succeeded", "failed", "canceled"},
    "succeeded": {"succeeded"},
    "failed": {"failed"},
    "canceled": {"canceled"},
}
JOB_ITEM_STATUSES = {"queued", "running", "succeeded", "failed", "canceled", "skipped"}
TERMINAL_JOB_ITEM_STATUSES = {"succeeded", "failed", "canceled", "skipped"}
VALID_JOB_ITEM_TRANSITIONS = {
    "queued": {"queued", "running", "succeeded", "failed", "canceled", "skipped"},
    "running": {"running", "succeeded", "failed", "canceled", "skipped"},
    "succeeded": {"succeeded"},
    "failed": {"failed"},
    "canceled": {"canceled"},
    "skipped": {"skipped"},
}
GENERATION_RESULT_STATUSES = {"generated", "failed", "rejected"}
EXPORT_STATUSES = {"ready", "failed", "expired"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS menus (
    id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    object_key TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'upload',
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded', 'parsed', 'failed', 'archived')),
    parsed_summary_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_items (
    id TEXT PRIMARY KEY,
    menu_id TEXT NOT NULL REFERENCES menus (id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL DEFAULT 0,
    sheet_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    components_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_uploads (
    id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL,
    object_key TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded', 'parsed', 'failed', 'archived')),
    parsed_summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_images (
    id TEXT PRIMARY KEY,
    object_key TEXT NOT NULL UNIQUE,
    thumbnail_object_key TEXT NOT NULL DEFAULT '',
    file_url TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'library',
    store_name TEXT NOT NULL DEFAULT '',
    dish_name TEXT NOT NULL,
    normalized_dish TEXT NOT NULL DEFAULT '',
    canonical_dish_id TEXT NOT NULL DEFAULT '',
    category_path TEXT NOT NULL DEFAULT '',
    style_id TEXT NOT NULL DEFAULT 'style-upload',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    reusable INTEGER NOT NULL DEFAULT 1 CHECK (reusable IN (0, 1)),
    has_brand_watermark INTEGER NOT NULL DEFAULT 0 CHECK (has_brand_watermark IN (0, 1)),
    has_dish_text INTEGER NOT NULL DEFAULT 0 CHECK (has_dish_text IN (0, 1)),
    quality_score REAL NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_jobs (
    id TEXT PRIMARY KEY,
    menu_id TEXT REFERENCES menus (id) ON DELETE SET NULL,
    menu_upload_id TEXT REFERENCES menu_uploads (id) ON DELETE SET NULL,
    style_id TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT 'standard',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    requested_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS generation_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES generation_jobs (id) ON DELETE CASCADE,
    menu_item_id TEXT REFERENCES menu_items (id) ON DELETE SET NULL,
    menu_row INTEGER NOT NULL DEFAULT 0,
    dish_name TEXT NOT NULL DEFAULT '',
    normalized_dish TEXT NOT NULL DEFAULT '',
    item_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled', 'skipped')),
    action TEXT NOT NULL DEFAULT '',
    library_image_id TEXT REFERENCES library_images (id) ON DELETE SET NULL,
    prompt TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS generation_results (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES generation_jobs (id) ON DELETE CASCADE,
    job_item_id TEXT REFERENCES generation_job_items (id) ON DELETE SET NULL,
    menu_item_id TEXT REFERENCES menu_items (id) ON DELETE SET NULL,
    menu_row INTEGER NOT NULL DEFAULT 0,
    dish_name TEXT NOT NULL DEFAULT '',
    library_image_id TEXT REFERENCES library_images (id) ON DELETE SET NULL,
    object_key TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    prompt TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'generated'
        CHECK (status IN ('generated', 'failed', 'rejected')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_images (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES generation_jobs (id) ON DELETE CASCADE,
    menu_row INTEGER NOT NULL DEFAULT 0,
    dish_name TEXT NOT NULL DEFAULT '',
    library_image_id TEXT REFERENCES library_images (id) ON DELETE SET NULL,
    object_key TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    prompt TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'generated'
        CHECK (status IN ('generated', 'failed', 'rejected')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES generation_jobs (id) ON DELETE SET NULL,
    object_key TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    image_count INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'failed', 'expired')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_packages (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES generation_jobs (id) ON DELETE SET NULL,
    object_key TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    image_count INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'failed', 'expired')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_ledger (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT 'demo',
    job_id TEXT REFERENCES generation_jobs (id) ON DELETE SET NULL,
    amount INTEGER NOT NULL CHECK (amount <> 0),
    balance_after INTEGER,
    reason TEXT NOT NULL,
    reference_type TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_menus_store_created
    ON menus (store_name, created_at);

CREATE INDEX IF NOT EXISTS idx_menu_items_menu_sort
    ON menu_items (menu_id, sort_order, row_number);

CREATE INDEX IF NOT EXISTS idx_menu_items_normalized_name
    ON menu_items (normalized_name);

CREATE INDEX IF NOT EXISTS idx_menu_uploads_store_created
    ON menu_uploads (store_name, created_at);

CREATE INDEX IF NOT EXISTS idx_library_images_normalized_dish
    ON library_images (normalized_dish);

CREATE INDEX IF NOT EXISTS idx_library_images_style_source
    ON library_images (style_id, source);

CREATE INDEX IF NOT EXISTS idx_library_images_reusable
    ON library_images (reusable);

CREATE INDEX IF NOT EXISTS idx_library_images_canonical_dish
    ON library_images (canonical_dish_id);

CREATE INDEX IF NOT EXISTS idx_generation_jobs_status_created
    ON generation_jobs (status, created_at);

CREATE INDEX IF NOT EXISTS idx_generation_jobs_menu
    ON generation_jobs (menu_id);

CREATE INDEX IF NOT EXISTS idx_generation_jobs_menu_upload
    ON generation_jobs (menu_upload_id);

CREATE INDEX IF NOT EXISTS idx_generation_job_items_job_sort
    ON generation_job_items (job_id, sort_order, menu_row);

CREATE INDEX IF NOT EXISTS idx_generation_job_items_status
    ON generation_job_items (status);

CREATE INDEX IF NOT EXISTS idx_generation_results_job
    ON generation_results (job_id);

CREATE INDEX IF NOT EXISTS idx_generation_results_job_item
    ON generation_results (job_item_id);

CREATE INDEX IF NOT EXISTS idx_generation_results_object_key
    ON generation_results (object_key);

CREATE INDEX IF NOT EXISTS idx_generated_images_job
    ON generated_images (job_id);

CREATE INDEX IF NOT EXISTS idx_generated_images_object_key
    ON generated_images (object_key);

CREATE INDEX IF NOT EXISTS idx_exports_job
    ON exports (job_id);

CREATE INDEX IF NOT EXISTS idx_export_packages_job
    ON export_packages (job_id);

CREATE INDEX IF NOT EXISTS idx_point_ledger_account_created
    ON point_ledger (account_id, created_at);

CREATE INDEX IF NOT EXISTS idx_point_ledger_job
    ON point_ledger (job_id);
"""

MIGRATION_COLUMNS = {
    "library_images": [
        ("file_url", "TEXT NOT NULL DEFAULT ''"),
        ("canonical_dish_id", "TEXT NOT NULL DEFAULT ''"),
        ("has_brand_watermark", "INTEGER NOT NULL DEFAULT 0 CHECK (has_brand_watermark IN (0, 1))"),
        ("has_dish_text", "INTEGER NOT NULL DEFAULT 0 CHECK (has_dish_text IN (0, 1))"),
        ("quality_score", "REAL NOT NULL DEFAULT 0"),
    ],
    "generation_jobs": [
        ("menu_id", "TEXT REFERENCES menus (id) ON DELETE SET NULL"),
    ],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def json_dumps(value: Any) -> str:
    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def normalize_dish_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,40}[）)]", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|l|克|g|kg|斤|个|只|份|瓶|罐|盒|两)", "", text)
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).strip()


def _resolve_db_path(db_path: str | os.PathLike[str] | None) -> str | Path:
    if db_path is None:
        configured = os.environ.get("STORAGE_DB_PATH")
        db_path = configured if configured else DEFAULT_DB_PATH
    if str(db_path) == ":memory:":
        return ":memory:"
    return Path(db_path).expanduser()


def get_conn(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    resolved = _resolve_db_path(db_path)
    if isinstance(resolved, Path):
        resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_existing_tables(conn: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    for table, columns in MIGRATION_COLUMNS.items():
        if table not in tables:
            continue
        existing = _table_columns(conn, table)
        for column, definition in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(conn_or_path: sqlite3.Connection | str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    conn = conn_or_path if isinstance(conn_or_path, sqlite3.Connection) else get_conn(conn_or_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_existing_tables(conn)
        conn.executescript(INDEX_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _fetch_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def _require_row(conn: sqlite3.Connection, table: str, record_id: str) -> sqlite3.Row:
    row = _fetch_one(conn, f"SELECT * FROM {table} WHERE id = ?", (record_id,))
    if row is None:
        raise KeyError(f"{table} record not found: {record_id}")
    return row


def _menu_from_row(conn: sqlite3.Connection, row: sqlite3.Row, *, include_items: bool = True) -> dict[str, Any]:
    data = dict(row)
    data["parsed_summary"] = json_loads(data.pop("parsed_summary_json", None), {})
    data["metadata"] = json_loads(data.pop("metadata_json", None), {})
    if include_items:
        data["items"] = list_menu_items(conn, data["id"])
    return data


def _menu_item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["components"] = json_loads(data.pop("components_json", None), [])
    data["raw"] = json_loads(data.pop("raw_json", None), {})
    return data


def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["request"] = json_loads(data.pop("request_json", None), {})
    data["result"] = json_loads(data.pop("result_json", None), {})
    return data


def _job_item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["result"] = json_loads(data.pop("result_json", None), {})
    return data


def _generation_result_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json_loads(data.pop("metadata_json", None), {})
    return data


def _export_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = json_loads(data.pop("metadata_json", None), {})
    return data


def _library_image_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["reusable"] = bool(data["reusable"])
    data["has_brand_watermark"] = bool(data.get("has_brand_watermark", 0))
    data["has_dish_text"] = bool(data.get("has_dish_text", 0))
    data["tags"] = json_loads(data.pop("tags_json", None), [])
    data["metadata"] = json_loads(data.pop("metadata_json", None), {})
    return data


def _validate_status(value: str, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {label} status: {value}")


def create_menu(
    conn: sqlite3.Connection,
    *,
    store_name: str = "",
    original_filename: str = "",
    object_key: str = "",
    content_type: str = "",
    file_size: int = 0,
    sha256: str = "",
    source: str = "upload",
    status: str = "uploaded",
    parsed_summary: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    items: Sequence[dict[str, Any]] | None = None,
    menu_id: str | None = None,
) -> dict[str, Any]:
    _validate_status(status, MENU_STATUSES, "menu")
    now = utc_now()
    menu_id = menu_id or new_id("menu")
    with conn:
        conn.execute(
            """
            INSERT INTO menus (
                id, store_name, original_filename, object_key, content_type,
                file_size, sha256, source, status, parsed_summary_json,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                menu_id,
                store_name,
                original_filename,
                object_key,
                content_type,
                int(file_size),
                sha256,
                source,
                status,
                json_dumps(parsed_summary),
                json_dumps(metadata),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO menu_uploads (
                id, store_name, original_filename, object_key, content_type,
                file_size, sha256, status, parsed_summary_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                menu_id,
                store_name,
                original_filename,
                object_key,
                content_type,
                int(file_size),
                sha256,
                status,
                json_dumps(parsed_summary),
                now,
                now,
            ),
        )
        for index, item in enumerate(items or [], start=1):
            _insert_menu_item(conn, menu_id=menu_id, sort_order=index, item=item, now=now)
    return get_menu(conn, menu_id)


def _insert_menu_item(
    conn: sqlite3.Connection,
    *,
    menu_id: str,
    sort_order: int,
    item: dict[str, Any],
    now: str,
    item_id: str | None = None,
) -> str:
    item_id = item_id or str(item.get("id") or new_id("mitem"))
    name = str(item.get("name") or item.get("dish_name") or "")
    normalized_name = str(item.get("normalized_name") or item.get("norm") or normalize_dish_name(name))
    row_number = int(item.get("row_number") or item.get("row") or 0)
    sheet_name = str(item.get("sheet_name") or item.get("sheet") or "")
    components = item.get("components")
    conn.execute(
        """
        INSERT INTO menu_items (
            id, menu_id, row_number, sheet_name, category, name, normalized_name,
            kind, price, components_json, raw_json, sort_order, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            menu_id,
            row_number,
            sheet_name,
            str(item.get("category") or ""),
            name,
            normalized_name,
            str(item.get("kind") or ""),
            str(item.get("price") or ""),
            json_dumps(list(components or [])),
            json_dumps(item.get("raw") or item),
            int(item.get("sort_order") or sort_order),
            now,
            now,
        ),
    )
    return item_id


def create_menu_item(
    conn: sqlite3.Connection,
    menu_id: str,
    *,
    row_number: int = 0,
    sheet_name: str = "",
    category: str = "",
    name: str,
    normalized_name: str | None = None,
    kind: str = "",
    price: str = "",
    components: Sequence[str] | None = None,
    raw: dict[str, Any] | None = None,
    sort_order: int = 0,
    item_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    item = {
        "id": item_id,
        "row_number": row_number,
        "sheet_name": sheet_name,
        "category": category,
        "name": name,
        "normalized_name": normalized_name if normalized_name is not None else normalize_dish_name(name),
        "kind": kind,
        "price": price,
        "components": list(components or []),
        "raw": raw or {},
        "sort_order": sort_order,
    }
    with conn:
        saved_id = _insert_menu_item(conn, menu_id=menu_id, sort_order=sort_order, item=item, now=now, item_id=item_id)
        conn.execute("UPDATE menus SET updated_at = ? WHERE id = ?", (now, menu_id))
    return get_menu_item(conn, saved_id)


def get_menu(conn: sqlite3.Connection, menu_id: str, *, include_items: bool = True) -> dict[str, Any]:
    return _menu_from_row(conn, _require_row(conn, "menus", menu_id), include_items=include_items)


def list_menus(
    conn: sqlite3.Connection,
    *,
    store_name: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if store_name is not None:
        clauses.append("store_name = ?")
        params.append(store_name)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        SELECT * FROM menus
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_menu_from_row(conn, row, include_items=False) for row in rows]


def get_menu_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    return _menu_item_from_row(_require_row(conn, "menu_items", item_id))


def list_menu_items(conn: sqlite3.Connection, menu_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM menu_items
        WHERE menu_id = ?
        ORDER BY sort_order ASC, row_number ASC, id ASC
        """,
        (menu_id,),
    ).fetchall()
    return [_menu_item_from_row(row) for row in rows]


def create_generation_job(
    conn: sqlite3.Connection,
    *,
    menu_id: str | None = None,
    menu_upload_id: str | None = None,
    style_id: str = "",
    quality: str = "standard",
    requested_count: int = 0,
    request: dict[str, Any] | None = None,
    status: str = "queued",
    items: Sequence[dict[str, Any]] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    _validate_status(status, JOB_STATUSES, "job")
    now = utc_now()
    started_at = now if status == "running" else None
    completed_at = now if status in TERMINAL_JOB_STATUSES else None
    job_id = job_id or new_id("job")
    if menu_upload_id is None and menu_id is not None:
        legacy_row = _fetch_one(conn, "SELECT id FROM menu_uploads WHERE id = ?", (menu_id,))
        menu_upload_id = legacy_row["id"] if legacy_row is not None else None
    with conn:
        conn.execute(
            """
            INSERT INTO generation_jobs (
                id, menu_id, menu_upload_id, style_id, quality, status, requested_count,
                request_json, created_at, updated_at, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                menu_id,
                menu_upload_id,
                style_id,
                quality,
                status,
                int(requested_count),
                json_dumps(request),
                now,
                now,
                started_at,
                completed_at,
            ),
        )
        for index, item in enumerate(items or [], start=1):
            _insert_generation_job_item(conn, job_id=job_id, sort_order=index, item=item, now=now)
        if items:
            _refresh_generation_job_counts(conn, job_id, now=now)
    return get_generation_job(conn, job_id)


def get_generation_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    include_items: bool = False,
) -> dict[str, Any]:
    data = _job_from_row(_require_row(conn, "generation_jobs", job_id))
    if include_items:
        data["items"] = list_generation_job_items(conn, job_id=job_id)
    return data


def list_generation_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    menu_id: str | None = None,
    menu_upload_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if menu_id is not None:
        clauses.append("menu_id = ?")
        params.append(menu_id)
    if menu_upload_id is not None:
        clauses.append("menu_upload_id = ?")
        params.append(menu_upload_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        SELECT * FROM generation_jobs
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_job_from_row(row) for row in rows]


def update_generation_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    requested_count: int | None = None,
    completed_count: int | None = None,
    failed_count: int | None = None,
    result: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    current = get_generation_job(conn, job_id)
    updates: list[str] = []
    params: list[Any] = []
    now = utc_now()

    if status is not None:
        _validate_status(status, JOB_STATUSES, "job")
        allowed = VALID_JOB_TRANSITIONS[current["status"]]
        if status not in allowed:
            raise ValueError(f"invalid job status transition: {current['status']} -> {status}")
        updates.append("status = ?")
        params.append(status)
        if status == "running" and current["started_at"] is None:
            updates.append("started_at = ?")
            params.append(now)
        if status in TERMINAL_JOB_STATUSES and current["completed_at"] is None:
            updates.append("completed_at = ?")
            params.append(now)

    if requested_count is not None:
        updates.append("requested_count = ?")
        params.append(int(requested_count))
    if completed_count is not None:
        updates.append("completed_count = ?")
        params.append(int(completed_count))
    if failed_count is not None:
        updates.append("failed_count = ?")
        params.append(int(failed_count))
    if result is not None:
        updates.append("result_json = ?")
        params.append(json_dumps(result))
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)
    if not updates:
        return current

    updates.append("updated_at = ?")
    params.append(now)
    params.append(job_id)
    with conn:
        conn.execute(f"UPDATE generation_jobs SET {', '.join(updates)} WHERE id = ?", tuple(params))
    return get_generation_job(conn, job_id)


def _insert_generation_job_item(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    sort_order: int,
    item: dict[str, Any],
    now: str,
    item_id: str | None = None,
) -> str:
    item_id = item_id or str(item.get("job_item_id") or new_id("jitem"))
    status = str(item.get("status") or "queued")
    _validate_status(status, JOB_ITEM_STATUSES, "job item")
    dish_name = str(item.get("dish_name") or item.get("name") or "")
    normalized_dish = str(item.get("normalized_dish") or item.get("norm") or normalize_dish_name(dish_name))
    started_at = now if status == "running" else None
    completed_at = now if status in TERMINAL_JOB_ITEM_STATUSES else None
    conn.execute(
        """
        INSERT INTO generation_job_items (
            id, job_id, menu_item_id, menu_row, dish_name, normalized_dish,
            item_kind, status, action, library_image_id, prompt, result_json,
            error_message, sort_order, created_at, updated_at, started_at, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            job_id,
            item.get("menu_item_id"),
            int(item.get("menu_row") or item.get("row") or 0),
            dish_name,
            normalized_dish,
            str(item.get("item_kind") or item.get("kind") or ""),
            status,
            str(item.get("action") or ""),
            item.get("library_image_id"),
            str(item.get("prompt") or ""),
            json_dumps(item.get("result")),
            str(item.get("error_message") or ""),
            int(item.get("sort_order") or sort_order),
            now,
            now,
            started_at,
            completed_at,
        ),
    )
    return item_id


def _refresh_generation_job_counts(conn: sqlite3.Connection, job_id: str, *, now: str | None = None) -> None:
    now = now or utc_now()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS requested_count,
            SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
        FROM generation_job_items
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    conn.execute(
        """
        UPDATE generation_jobs
        SET requested_count = ?, completed_count = ?, failed_count = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            int(row["requested_count"] or 0),
            int(row["completed_count"] or 0),
            int(row["failed_count"] or 0),
            now,
            job_id,
        ),
    )


def create_generation_job_item(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    menu_item_id: str | None = None,
    menu_row: int = 0,
    dish_name: str = "",
    normalized_dish: str | None = None,
    item_kind: str = "",
    status: str = "queued",
    action: str = "",
    library_image_id: str | None = None,
    prompt: str = "",
    result: dict[str, Any] | None = None,
    error_message: str = "",
    sort_order: int = 0,
    item_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    item = {
        "id": item_id,
        "menu_item_id": menu_item_id,
        "menu_row": menu_row,
        "dish_name": dish_name,
        "normalized_dish": normalized_dish if normalized_dish is not None else normalize_dish_name(dish_name),
        "item_kind": item_kind,
        "status": status,
        "action": action,
        "library_image_id": library_image_id,
        "prompt": prompt,
        "result": result,
        "error_message": error_message,
        "sort_order": sort_order,
    }
    with conn:
        saved_id = _insert_generation_job_item(conn, job_id=job_id, sort_order=sort_order, item=item, now=now, item_id=item_id)
        _refresh_generation_job_counts(conn, job_id, now=now)
    return get_generation_job_item(conn, saved_id)


def get_generation_job_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    return _job_item_from_row(_require_row(conn, "generation_job_items", item_id))


def list_generation_job_items(
    conn: sqlite3.Connection,
    *,
    job_id: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id = ?")
        params.append(job_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        SELECT * FROM generation_job_items
        {where}
        ORDER BY sort_order ASC, menu_row ASC, id ASC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_job_item_from_row(row) for row in rows]


def update_generation_job_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    status: str | None = None,
    action: str | None = None,
    library_image_id: str | None = None,
    prompt: str | None = None,
    result: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    current = get_generation_job_item(conn, item_id)
    updates: list[str] = []
    params: list[Any] = []
    now = utc_now()

    if status is not None:
        _validate_status(status, JOB_ITEM_STATUSES, "job item")
        allowed = VALID_JOB_ITEM_TRANSITIONS[current["status"]]
        if status not in allowed:
            raise ValueError(f"invalid job item status transition: {current['status']} -> {status}")
        updates.append("status = ?")
        params.append(status)
        if status == "running" and current["started_at"] is None:
            updates.append("started_at = ?")
            params.append(now)
        if status in TERMINAL_JOB_ITEM_STATUSES and current["completed_at"] is None:
            updates.append("completed_at = ?")
            params.append(now)
    if action is not None:
        updates.append("action = ?")
        params.append(action)
    if library_image_id is not None:
        updates.append("library_image_id = ?")
        params.append(library_image_id)
    if prompt is not None:
        updates.append("prompt = ?")
        params.append(prompt)
    if result is not None:
        updates.append("result_json = ?")
        params.append(json_dumps(result))
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)
    if not updates:
        return current

    updates.append("updated_at = ?")
    params.append(now)
    params.append(item_id)
    with conn:
        conn.execute(f"UPDATE generation_job_items SET {', '.join(updates)} WHERE id = ?", tuple(params))
        _refresh_generation_job_counts(conn, current["job_id"], now=now)
    return get_generation_job_item(conn, item_id)


def create_library_image(
    conn: sqlite3.Connection,
    *,
    object_key: str,
    dish_name: str,
    store_name: str = "",
    style_id: str = "style-upload",
    source: str = "library",
    thumbnail_object_key: str = "",
    file_url: str = "",
    sha256: str = "",
    normalized_dish: str | None = None,
    canonical_dish_id: str = "",
    category_path: str = "",
    width: int = 0,
    height: int = 0,
    file_size: int = 0,
    reusable: bool = True,
    has_brand_watermark: bool = False,
    has_dish_text: bool = False,
    quality_score: float = 0,
    tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
    image_id: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    image_id = image_id or new_id("libimg")
    with conn:
        conn.execute(
            """
            INSERT INTO library_images (
                id, object_key, thumbnail_object_key, file_url, sha256, source,
                store_name, dish_name, normalized_dish, canonical_dish_id,
                category_path, style_id, width, height, file_size, reusable,
                has_brand_watermark, has_dish_text, quality_score, tags_json,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                object_key,
                thumbnail_object_key,
                file_url,
                sha256,
                source,
                store_name,
                dish_name,
                normalized_dish if normalized_dish is not None else normalize_dish_name(dish_name),
                canonical_dish_id,
                category_path,
                style_id,
                int(width),
                int(height),
                int(file_size),
                1 if reusable else 0,
                1 if has_brand_watermark else 0,
                1 if has_dish_text else 0,
                float(quality_score),
                json_dumps(list(tags or [])),
                json_dumps(metadata),
                now,
                now,
            ),
        )
    return get_library_image(conn, image_id)


def get_library_image(conn: sqlite3.Connection, image_id: str) -> dict[str, Any]:
    return _library_image_from_row(_require_row(conn, "library_images", image_id))


def list_library_images(
    conn: sqlite3.Connection,
    *,
    dish_query: str | None = None,
    style_id: str | None = None,
    source: str | None = None,
    reusable: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if dish_query:
        clauses.append("(normalized_dish LIKE ? OR dish_name LIKE ?)")
        norm = f"%{normalize_dish_name(dish_query)}%"
        params.extend([norm, f"%{dish_query}%"])
    if style_id is not None:
        clauses.append("style_id = ?")
        params.append(style_id)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if reusable is not None:
        clauses.append("reusable = ?")
        params.append(1 if reusable else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        SELECT * FROM library_images
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_library_image_from_row(row) for row in rows]


def update_library_image(
    conn: sqlite3.Connection,
    image_id: str,
    *,
    object_key: str | None = None,
    thumbnail_object_key: str | None = None,
    file_url: str | None = None,
    sha256: str | None = None,
    source: str | None = None,
    store_name: str | None = None,
    dish_name: str | None = None,
    normalized_dish: str | None = None,
    canonical_dish_id: str | None = None,
    category_path: str | None = None,
    style_id: str | None = None,
    width: int | None = None,
    height: int | None = None,
    file_size: int | None = None,
    reusable: bool | None = None,
    has_brand_watermark: bool | None = None,
    has_dish_text: bool | None = None,
    quality_score: float | None = None,
    tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_row(conn, "library_images", image_id)
    updates: list[str] = []
    params: list[Any] = []

    field_values = {
        "object_key": object_key,
        "thumbnail_object_key": thumbnail_object_key,
        "file_url": file_url,
        "sha256": sha256,
        "source": source,
        "store_name": store_name,
        "dish_name": dish_name,
        "canonical_dish_id": canonical_dish_id,
        "category_path": category_path,
        "style_id": style_id,
    }
    for field, value in field_values.items():
        if value is not None:
            updates.append(f"{field} = ?")
            params.append(value)

    if normalized_dish is not None:
        updates.append("normalized_dish = ?")
        params.append(normalized_dish)
    elif dish_name is not None:
        updates.append("normalized_dish = ?")
        params.append(normalize_dish_name(dish_name))

    numeric_fields = {"width": width, "height": height, "file_size": file_size}
    for field, value in numeric_fields.items():
        if value is not None:
            updates.append(f"{field} = ?")
            params.append(int(value))
    if reusable is not None:
        updates.append("reusable = ?")
        params.append(1 if reusable else 0)
    if has_brand_watermark is not None:
        updates.append("has_brand_watermark = ?")
        params.append(1 if has_brand_watermark else 0)
    if has_dish_text is not None:
        updates.append("has_dish_text = ?")
        params.append(1 if has_dish_text else 0)
    if quality_score is not None:
        updates.append("quality_score = ?")
        params.append(float(quality_score))
    if tags is not None:
        updates.append("tags_json = ?")
        params.append(json_dumps(list(tags)))
    if metadata is not None:
        updates.append("metadata_json = ?")
        params.append(json_dumps(metadata))
    if not updates:
        return get_library_image(conn, image_id)

    updates.append("updated_at = ?")
    params.append(utc_now())
    params.append(image_id)
    with conn:
        conn.execute(f"UPDATE library_images SET {', '.join(updates)} WHERE id = ?", tuple(params))
    return get_library_image(conn, image_id)


def create_generation_result(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    object_key: str,
    job_item_id: str | None = None,
    menu_item_id: str | None = None,
    menu_row: int = 0,
    dish_name: str = "",
    library_image_id: str | None = None,
    platform: str = "",
    width: int = 0,
    height: int = 0,
    file_size: int = 0,
    prompt: str = "",
    seed: str = "",
    status: str = "generated",
    metadata: dict[str, Any] | None = None,
    result_id: str | None = None,
) -> dict[str, Any]:
    _validate_status(status, GENERATION_RESULT_STATUSES, "generation result")
    if job_item_id is not None:
        job_item = get_generation_job_item(conn, job_item_id)
        if job_item["job_id"] != job_id:
            raise ValueError("generation result job_id does not match job item")
        menu_item_id = menu_item_id if menu_item_id is not None else job_item["menu_item_id"]
        menu_row = menu_row or int(job_item["menu_row"] or 0)
        dish_name = dish_name or job_item["dish_name"]
        library_image_id = library_image_id if library_image_id is not None else job_item["library_image_id"]
        prompt = prompt or job_item["prompt"]
    now = utc_now()
    result_id = result_id or new_id("genres")
    with conn:
        conn.execute(
            """
            INSERT INTO generation_results (
                id, job_id, job_item_id, menu_item_id, menu_row, dish_name,
                library_image_id, object_key, platform, width, height, file_size,
                prompt, seed, status, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                job_id,
                job_item_id,
                menu_item_id,
                int(menu_row),
                dish_name,
                library_image_id,
                object_key,
                platform,
                int(width),
                int(height),
                int(file_size),
                prompt,
                seed,
                status,
                json_dumps(metadata),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO generated_images (
                id, job_id, menu_row, dish_name, library_image_id, object_key,
                platform, width, height, file_size, prompt, seed, status,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                job_id,
                int(menu_row),
                dish_name,
                library_image_id,
                object_key,
                platform,
                int(width),
                int(height),
                int(file_size),
                prompt,
                seed,
                status,
                json_dumps(metadata),
                now,
                now,
            ),
        )
    return get_generation_result(conn, result_id)


def get_generation_result(conn: sqlite3.Connection, result_id: str) -> dict[str, Any]:
    return _generation_result_from_row(_require_row(conn, "generation_results", result_id))


def list_generation_results(
    conn: sqlite3.Connection,
    *,
    job_id: str | None = None,
    job_item_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id = ?")
        params.append(job_id)
    if job_item_id is not None:
        clauses.append("job_item_id = ?")
        params.append(job_item_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        SELECT * FROM generation_results
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_generation_result_from_row(row) for row in rows]


def create_export(
    conn: sqlite3.Connection,
    *,
    object_key: str,
    job_id: str | None = None,
    platform: str = "",
    scope: str = "",
    image_count: int = 0,
    file_size: int = 0,
    status: str = "ready",
    metadata: dict[str, Any] | None = None,
    export_id: str | None = None,
) -> dict[str, Any]:
    _validate_status(status, EXPORT_STATUSES, "export")
    now = utc_now()
    export_id = export_id or new_id("export")
    with conn:
        conn.execute(
            """
            INSERT INTO exports (
                id, job_id, object_key, platform, scope, image_count, file_size,
                status, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id,
                job_id,
                object_key,
                platform,
                scope,
                int(image_count),
                int(file_size),
                status,
                json_dumps(metadata),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO export_packages (
                id, job_id, object_key, platform, scope, image_count, file_size,
                status, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id,
                job_id,
                object_key,
                platform,
                scope,
                int(image_count),
                int(file_size),
                status,
                json_dumps(metadata),
                now,
                now,
            ),
        )
    return get_export(conn, export_id)


def create_export_record(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    return create_export(conn, **kwargs)


def create_export_package(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    return create_export(conn, **kwargs)


def get_export(conn: sqlite3.Connection, export_id: str) -> dict[str, Any]:
    return _export_from_row(_require_row(conn, "exports", export_id))


def list_exports(
    conn: sqlite3.Connection,
    *,
    job_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id = ?")
        params.append(job_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        SELECT * FROM exports
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_export_from_row(row) for row in rows]


class ObjectStorage(Protocol):
    def put_bytes(self, data: bytes, *, prefix: str = "objects", filename: str | None = None) -> str:
        """Persist bytes and return an object key."""

    def put_file(self, source: str | os.PathLike[str], *, prefix: str = "objects", filename: str | None = None) -> str:
        """Persist a local file and return an object key."""

    def read_bytes(self, object_key: str) -> bytes:
        """Read a previously stored object."""


def safe_object_filename(filename: str | None) -> str:
    name = unicodedata.normalize("NFKC", filename or "object.bin")
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name).strip("._-")
    return name[:120] or "object.bin"


def _safe_key_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\-/]+", "-", value.strip().strip("/"))
    parts = [part for part in cleaned.split("/") if part and part not in {".", ".."}]
    return "/".join(parts) or "objects"


class LocalObjectStorage:
    def __init__(self, root: str | os.PathLike[str] = DEFAULT_OBJECT_STORE_DIR) -> None:
        self.root = Path(root).expanduser()

    def put_bytes(self, data: bytes, *, prefix: str = "objects", filename: str | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        stored_name = f"{digest[:16]}_{safe_object_filename(filename)}"
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        object_key = f"{_safe_key_part(prefix)}/{day}/{stored_name}"
        target = self.path_for_key(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return object_key

    def put_file(self, source: str | os.PathLike[str], *, prefix: str = "objects", filename: str | None = None) -> str:
        source_path = Path(source).expanduser()
        return self.put_bytes(source_path.read_bytes(), prefix=prefix, filename=filename or source_path.name)

    def put_stream(self, stream: BinaryIO, *, prefix: str = "objects", filename: str | None = None) -> str:
        return self.put_bytes(stream.read(), prefix=prefix, filename=filename)

    def read_bytes(self, object_key: str) -> bytes:
        return self.path_for_key(object_key).read_bytes()

    def copy_to(self, object_key: str, target: str | os.PathLike[str]) -> Path:
        target_path = Path(target).expanduser()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path_for_key(object_key), target_path)
        return target_path

    def path_for_key(self, object_key: str) -> Path:
        raw_key = object_key.strip()
        if not raw_key or "\\" in raw_key:
            raise ValueError(f"invalid object key: {object_key!r}")
        key = PurePosixPath(raw_key.lstrip("/"))
        if not key.parts or any(part in {"", ".", ".."} for part in key.parts):
            raise ValueError(f"invalid object key: {object_key!r}")
        root = self.root.resolve()
        target = (root / Path(*key.parts)).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"object key escapes store root: {object_key!r}")
        return target


def get_object_store(root: str | os.PathLike[str] | None = None) -> LocalObjectStorage:
    configured = os.environ.get("OBJECT_STORE_DIR")
    return LocalObjectStorage(root or configured or DEFAULT_OBJECT_STORE_DIR)
