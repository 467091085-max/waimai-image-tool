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
SCHEMA_VERSION = 1

REQUIRED_TABLES = {
    "menu_uploads",
    "library_images",
    "generation_jobs",
    "generated_images",
    "export_packages",
    "point_ledger",
}

JOB_STATUSES = {"queued", "running", "succeeded", "failed", "canceled"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}
VALID_JOB_TRANSITIONS = {
    "queued": {"queued", "running", "failed", "canceled"},
    "running": {"running", "succeeded", "failed", "canceled"},
    "succeeded": {"succeeded"},
    "failed": {"failed"},
    "canceled": {"canceled"},
}

SCHEMA_SQL = """
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

CREATE INDEX IF NOT EXISTS idx_menu_uploads_store_created
    ON menu_uploads (store_name, created_at);

CREATE TABLE IF NOT EXISTS library_images (
    id TEXT PRIMARY KEY,
    object_key TEXT NOT NULL UNIQUE,
    thumbnail_object_key TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'library',
    store_name TEXT NOT NULL DEFAULT '',
    dish_name TEXT NOT NULL,
    normalized_dish TEXT NOT NULL DEFAULT '',
    category_path TEXT NOT NULL DEFAULT '',
    style_id TEXT NOT NULL DEFAULT 'style-upload',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    reusable INTEGER NOT NULL DEFAULT 1 CHECK (reusable IN (0, 1)),
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_images_normalized_dish
    ON library_images (normalized_dish);

CREATE INDEX IF NOT EXISTS idx_library_images_style_source
    ON library_images (style_id, source);

CREATE INDEX IF NOT EXISTS idx_library_images_reusable
    ON library_images (reusable);

CREATE TABLE IF NOT EXISTS generation_jobs (
    id TEXT PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_generation_jobs_status_created
    ON generation_jobs (status, created_at);

CREATE INDEX IF NOT EXISTS idx_generation_jobs_menu_upload
    ON generation_jobs (menu_upload_id);

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

CREATE INDEX IF NOT EXISTS idx_generated_images_job
    ON generated_images (job_id);

CREATE INDEX IF NOT EXISTS idx_generated_images_object_key
    ON generated_images (object_key);

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

CREATE INDEX IF NOT EXISTS idx_export_packages_job
    ON export_packages (job_id);

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

CREATE INDEX IF NOT EXISTS idx_point_ledger_account_created
    ON point_ledger (account_id, created_at);

CREATE INDEX IF NOT EXISTS idx_point_ledger_job
    ON point_ledger (job_id);
"""


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


def init_db(conn_or_path: sqlite3.Connection | str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    conn = conn_or_path if isinstance(conn_or_path, sqlite3.Connection) else get_conn(conn_or_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _fetch_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def _require_row(conn: sqlite3.Connection, table: str, record_id: str) -> sqlite3.Row:
    row = _fetch_one(conn, f"SELECT * FROM {table} WHERE id = ?", (record_id,))
    if row is None:
        raise KeyError(f"{table} record not found: {record_id}")
    return row


def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["request"] = json_loads(data.pop("request_json", None), {})
    data["result"] = json_loads(data.pop("result_json", None), {})
    return data


def _library_image_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["reusable"] = bool(data["reusable"])
    data["tags"] = json_loads(data.pop("tags_json", None), [])
    data["metadata"] = json_loads(data.pop("metadata_json", None), {})
    return data


def create_generation_job(
    conn: sqlite3.Connection,
    *,
    menu_upload_id: str | None = None,
    style_id: str = "",
    quality: str = "standard",
    requested_count: int = 0,
    request: dict[str, Any] | None = None,
    status: str = "queued",
    job_id: str | None = None,
) -> dict[str, Any]:
    if status not in JOB_STATUSES:
        raise ValueError(f"invalid job status: {status}")
    now = utc_now()
    started_at = now if status == "running" else None
    completed_at = now if status in TERMINAL_JOB_STATUSES else None
    job_id = job_id or new_id("job")
    with conn:
        conn.execute(
            """
            INSERT INTO generation_jobs (
                id, menu_upload_id, style_id, quality, status, requested_count,
                request_json, created_at, updated_at, started_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
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
    return get_generation_job(conn, job_id)


def get_generation_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    return _job_from_row(_require_row(conn, "generation_jobs", job_id))


def list_generation_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    menu_upload_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
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
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid job status: {status}")
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


def create_library_image(
    conn: sqlite3.Connection,
    *,
    object_key: str,
    dish_name: str,
    store_name: str = "",
    style_id: str = "style-upload",
    source: str = "library",
    thumbnail_object_key: str = "",
    sha256: str = "",
    normalized_dish: str | None = None,
    category_path: str = "",
    width: int = 0,
    height: int = 0,
    file_size: int = 0,
    reusable: bool = True,
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
                id, object_key, thumbnail_object_key, sha256, source, store_name,
                dish_name, normalized_dish, category_path, style_id, width, height,
                file_size, reusable, tags_json, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                object_key,
                thumbnail_object_key,
                sha256,
                source,
                store_name,
                dish_name,
                normalized_dish if normalized_dish is not None else normalize_dish_name(dish_name),
                category_path,
                style_id,
                int(width),
                int(height),
                int(file_size),
                1 if reusable else 0,
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
    sha256: str | None = None,
    source: str | None = None,
    store_name: str | None = None,
    dish_name: str | None = None,
    normalized_dish: str | None = None,
    category_path: str | None = None,
    style_id: str | None = None,
    width: int | None = None,
    height: int | None = None,
    file_size: int | None = None,
    reusable: bool | None = None,
    tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_row(conn, "library_images", image_id)
    updates: list[str] = []
    params: list[Any] = []

    field_values = {
        "object_key": object_key,
        "thumbnail_object_key": thumbnail_object_key,
        "sha256": sha256,
        "source": source,
        "store_name": store_name,
        "dish_name": dish_name,
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
        key = PurePosixPath(object_key.strip().lstrip("/"))
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
