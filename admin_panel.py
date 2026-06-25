from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import billing
import generation_jobs
from flask import Blueprint, Response, jsonify, render_template, request

MENU_EXTS = {".xls", ".xlsx"}
REQUIRED_SOURCES = ("clean", "watermark", "internal")
CLEANING_LOW_QUALITY_SCORE = 0.7


@dataclass(frozen=True)
class AdminDependencies:
    library_images: Callable[[], Sequence[Any]]
    media_url_for_path: Callable[[Path], str]
    current_menu_path: Callable[[], Path | None]
    parse_menu: Callable[[Path | None], Mapping[str, Any]]
    upload_dir: Path
    billing_db_path: Path | str | None = None
    jobs_db_path: Path | str | None = None


def create_admin_blueprint(deps: AdminDependencies) -> Blueprint:
    blueprint = Blueprint("admin", __name__)

    @blueprint.get("/admin")
    def admin_home() -> Response | str:
        response = render_template("admin.html")
        return response

    @blueprint.get("/api/admin/library-sample")
    def admin_library_sample():
        limit = _bounded_int(request.args.get("limit"), default=18, minimum=1, maximum=48)
        payload = library_sample_payload(deps, limit)
        return jsonify(payload)

    @blueprint.get("/api/admin/menu-audit")
    def admin_menu_audit():
        limit = _bounded_int(request.args.get("limit"), default=40, minimum=1, maximum=100)
        payload = menu_audit_payload(deps, limit)
        return jsonify(payload)

    @blueprint.get("/api/admin/operations")
    def admin_operations():
        limit = _bounded_int(request.args.get("limit"), default=40, minimum=1, maximum=100)
        payload = operations_payload(deps, limit)
        status = 200 if payload.get("ok", False) else 500
        return jsonify(payload), status

    @blueprint.after_request
    def add_admin_headers(response: Response) -> Response:
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    return blueprint


def library_sample_payload(deps: AdminDependencies, limit: int = 18) -> dict[str, Any]:
    images = list(deps.library_images())
    sources = Counter(str(_field(image, "source", "unknown") or "unknown") for image in images)
    for source in REQUIRED_SOURCES:
        sources.setdefault(source, 0)

    stores = {str(_field(image, "store", "")) for image in images if _field(image, "store", "")}
    styles = {
        str(_first_field(image, "style_id", "styleId", "style", default=""))
        for image in images
        if _first_field(image, "style_id", "styleId", "style", default="")
    }
    cleaning = library_cleaning_summary(images)

    by_source: dict[str, list[dict[str, Any]]] = {source: [] for source in sorted(sources)}
    sample_pool: dict[str, list[dict[str, Any]]] = {source: [] for source in sorted(sources)}
    per_source_cap = max(1, min(8, limit // max(1, len(by_source)) + 1))
    for image in images:
        source = str(_field(image, "source", "unknown") or "unknown")
        sample = _image_sample(image, deps)
        if len(by_source.setdefault(source, [])) < per_source_cap:
            by_source[source].append(sample)
        if len(sample_pool.setdefault(source, [])) < limit:
            sample_pool[source].append(sample)
    samples = _balanced_samples(sample_pool, limit)

    return {
        "ok": True,
        "summary": {
            "total": len(images),
            "reusable": cleaning["reusable"],
            "referenceOnly": max(0, len(images) - cleaning["reusable"]),
            "stores": len(stores),
            "styles": len(styles),
            "cleaning": cleaning,
        },
        "cleaningSummary": cleaning,
        "sources": dict(sorted(sources.items())),
        "samples": samples[:limit],
        "bySource": by_source,
    }


def library_cleaning_summary(images: Sequence[Any]) -> dict[str, int]:
    reusable = 0
    watermark_risk = 0
    needs_review = 0
    low_quality = 0

    for image in images:
        if _as_bool(_field(image, "reusable", False)):
            reusable += 1

        has_watermark = _image_has_watermark_risk(image)
        has_dish_text = _as_bool(_first_field(image, "has_dish_text", "hasDishText", default=False))
        quality_score = _quality_score(image)
        reasons = _review_reasons(image)

        if has_watermark:
            watermark_risk += 1
        if quality_score is not None and quality_score < CLEANING_LOW_QUALITY_SCORE:
            low_quality += 1
        if has_watermark or has_dish_text or reasons or (quality_score is not None and quality_score < CLEANING_LOW_QUALITY_SCORE):
            needs_review += 1

    return {
        "reusable": reusable,
        "watermarkRisk": watermark_risk,
        "needsReview": needs_review,
        "lowQuality": low_quality,
    }


def menu_audit_payload(deps: AdminDependencies, limit: int = 40) -> dict[str, Any]:
    current = _parse_current_menu(deps)
    audit = _audit_upload_dir(deps, limit)
    return {
        "ok": True,
        "current": current,
        "audit": audit,
        "parser": {
            "supportedExtensions": sorted(MENU_EXTS),
            "source": "uploads",
        },
    }


def operations_payload(deps: AdminDependencies, limit: int = 40) -> dict[str, Any]:
    billing_payload = billing.admin_billing_payload(deps.billing_db_path, limit=limit)
    generation_payload = generation_status_payload(deps.jobs_db_path, limit=limit)
    billing_summary = billing_payload.get("summary", {}) if isinstance(billing_payload, Mapping) else {}
    generation_summary = generation_payload.get("summary", {}) if isinstance(generation_payload, Mapping) else {}
    ok = bool(billing_payload.get("ok")) and bool(generation_payload.get("ok"))
    return {
        "ok": ok,
        "summary": {
            "totalBalance": int(billing_summary.get("totalBalance", 0) or 0),
            "orderCount": int(billing_summary.get("orderCount", 0) or 0),
            "ledgerCount": int(billing_summary.get("ledgerCount", 0) or 0),
            "refundCount": int(billing_summary.get("refundCount", 0) or 0),
            "refundPoints": int(billing_summary.get("refundPoints", 0) or 0),
            "generationJobCount": int(generation_summary.get("jobCount", 0) or 0),
            "runningJobs": int(generation_summary.get("runningJobs", 0) or 0),
            "failedJobs": int(generation_summary.get("failedJobs", 0) or 0),
        },
        "billing": billing_payload,
        "generation": generation_payload,
    }


def generation_status_payload(
    db_path: str | Path | None = None,
    *,
    limit: int = 40,
) -> dict[str, Any]:
    capped_limit = max(1, min(100, int(limit)))
    path = generation_jobs.resolve_db_path(db_path)
    if str(path) != ":memory:" and not path.exists():
        return _empty_generation_payload()
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, "generation_jobs"):
                return _empty_generation_payload()
            jobs = [
                _generation_job_payload(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM generation_jobs
                    ORDER BY updated_at DESC, created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (capped_limit,),
                ).fetchall()
            ]
            status_counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM generation_jobs GROUP BY status"
                ).fetchall()
            }
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS jobs,
                    COALESCE(SUM(total_items), 0) AS total_items,
                    COALESCE(SUM(completed_items), 0) AS completed_items,
                    COALESCE(SUM(failed_items), 0) AS failed_items,
                    COALESCE(SUM(pending_items), 0) AS pending_items,
                    COALESCE(SUM(points), 0) AS points
                FROM generation_jobs
                """
            ).fetchone()
    except sqlite3.Error as exc:
        payload = _empty_generation_payload()
        payload.update(
            {
                "ok": False,
                "error": "Generation status unavailable",
                "code": "generation_status_unavailable",
                "details": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
        return payload

    running_jobs = sum(status_counts.get(status, 0) for status in ("queued", "running", "paid"))
    failed_jobs = sum(status_counts.get(status, 0) for status in ("failed", "partially_failed"))
    return {
        "ok": True,
        "summary": {
            "jobCount": int(totals["jobs"]),
            "totalItems": int(totals["total_items"]),
            "completedItems": int(totals["completed_items"]),
            "failedItems": int(totals["failed_items"]),
            "pendingItems": int(totals["pending_items"]),
            "points": int(totals["points"]),
            "runningJobs": int(running_jobs),
            "failedJobs": int(failed_jobs),
            "statusCounts": status_counts,
        },
        "jobs": jobs,
    }


def _parse_current_menu(deps: AdminDependencies) -> dict[str, Any]:
    path = deps.current_menu_path()
    try:
        menu = deps.parse_menu(path)
    except Exception as exc:
        return {
            "available": False,
            "file": path.name if path else "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"available": True, **_menu_summary(menu)}


def _audit_upload_dir(deps: AdminDependencies, limit: int) -> dict[str, Any]:
    upload_dir = deps.upload_dir
    files = sorted(
        (path for path in upload_dir.iterdir() if path.is_file() and path.suffix.lower() in MENU_EXTS),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if upload_dir.exists() else []
    selected_files = files[:limit]
    records = []
    failures = []
    totals = Counter({"single": 0, "combo": 0, "snack": 0, "total": 0})

    for path in selected_files:
        try:
            menu = deps.parse_menu(path)
        except Exception as exc:
            failures.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        summary = _menu_summary(menu)
        records.append(summary)
        totals.update({key: int(summary.get("kindCounts", {}).get(key, 0) or 0) for key in totals})

    return {
        "directory": upload_dir.name,
        "files": len(files),
        "scanned": len(selected_files),
        "parsed": len(records),
        "failed": len(failures),
        "totalItems": sum(int(record.get("count", 0) or 0) for record in records),
        "kindCounts": dict(totals),
        "menus": records,
        "errors": failures,
    }


def _menu_summary(menu: Mapping[str, Any]) -> dict[str, Any]:
    kind_counts = menu.get("kindCounts") if isinstance(menu.get("kindCounts"), Mapping) else {}
    sheets = menu.get("sheets") if isinstance(menu.get("sheets"), list) else []
    errors = menu.get("errors") if isinstance(menu.get("errors"), list) else []
    return {
        "file": str(menu.get("file") or ""),
        "store": str(menu.get("store") or ""),
        "count": int(menu.get("count") or 0),
        "kindCounts": {
            "single": int(kind_counts.get("single", 0) or 0),
            "combo": int(kind_counts.get("combo", 0) or 0),
            "snack": int(kind_counts.get("snack", 0) or 0),
            "total": int(kind_counts.get("total", menu.get("count", 0)) or 0),
        },
        "sheets": [
            {
                "sheet": str(sheet.get("sheet") or ""),
                "headerRow": int(sheet.get("headerRow") or 0),
                "items": int(sheet.get("items") or 0),
                "score": float(sheet.get("score") or 0),
            }
            for sheet in sheets[:8]
            if isinstance(sheet, Mapping)
        ],
        "errors": [
            {
                "sheet": str(error.get("sheet") or ""),
                "message": str(error.get("message") or error.get("error") or ""),
            }
            for error in errors[:8]
            if isinstance(error, Mapping)
        ],
        "demo": bool(menu.get("demo", False)),
    }


def _empty_generation_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "summary": {
            "jobCount": 0,
            "totalItems": 0,
            "completedItems": 0,
            "failedItems": 0,
            "pendingItems": 0,
            "points": 0,
            "runningJobs": 0,
            "failedJobs": 0,
            "statusCounts": {},
        },
        "jobs": [],
    }


def _generation_job_payload(row: sqlite3.Row) -> dict[str, Any]:
    total = int(row["total_items"])
    completed = int(row["completed_items"])
    failed = int(row["failed_items"])
    percent = 100 if total == 0 else round((completed + failed) / total * 100, 1)
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "status": row["status"],
        "style": row["style"],
        "quality": row["quality"],
        "totalItems": total,
        "completedItems": completed,
        "failedItems": failed,
        "pendingItems": int(row["pending_items"]),
        "points": int(row["points"]),
        "orderId": row["order_id"],
        "error": row["error"],
        "progress": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": int(row["pending_items"]),
            "percent": percent,
        },
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "paidAt": row["paid_at"],
        "queuedAt": row["queued_at"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _balanced_samples(by_source: Mapping[str, Sequence[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    seen_ids = set()
    max_depth = max((len(items) for items in by_source.values()), default=0)
    for index in range(max_depth):
        for source in sorted(by_source):
            items = by_source[source]
            if index >= len(items):
                continue
            sample = items[index]
            sample_id = sample.get("imageId")
            if sample_id in seen_ids:
                continue
            samples.append(sample)
            seen_ids.add(sample_id)
            if len(samples) >= limit:
                return samples
    return samples


def _image_sample(image: Any, deps: AdminDependencies) -> dict[str, Any]:
    path_value = _field(image, "path", "")
    path = Path(path_value) if path_value else Path()
    style_id = str(_first_field(image, "style_id", "styleId", "style", default=""))
    return {
        "imageId": str(_first_field(image, "image_id", "imageId", "id", default="")),
        "dishName": str(_first_field(image, "dish", "dishName", "name", default="")),
        "store": str(_field(image, "store", "")),
        "source": str(_field(image, "source", "unknown") or "unknown"),
        "styleId": style_id,
        "styleName": _style_label(style_id),
        "reusable": _as_bool(_field(image, "reusable", False)),
        "hasBrandWatermark": _image_has_watermark_risk(image),
        "hasDishText": _as_bool(_first_field(image, "has_dish_text", "hasDishText", default=False)),
        "qualityScore": _quality_score(image),
        "reviewReasons": _review_reasons(image),
        "url": deps.media_url_for_path(path) if path_value else "",
    }


def _style_label(style_id: str) -> str:
    if style_id.startswith("style-"):
        return style_id.replace("-", " ").title()
    if style_id:
        return style_id
    return "未标注风格"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first_field(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        item = _field(value, name, None)
        if item is not None:
            return item
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "none", "null"}
    return bool(value)


def _quality_score(image: Any) -> float | None:
    value = _first_field(image, "quality_score", "qualityScore", default=None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _review_reasons(image: Any) -> list[str]:
    value = _first_field(image, "review_reasons", "reviewReasons", default=[])
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value if str(item)]
    return []


def _image_has_watermark_risk(image: Any) -> bool:
    explicit = _first_field(image, "has_brand_watermark", "hasBrandWatermark", default=None)
    source = str(_field(image, "source", "") or "").lower()
    path_value = str(_field(image, "path", "") or "").lower()
    inferred = source == "watermark" or "watermarkpic" in source or "watermarkpic" in path_value
    return (explicit is not None and _as_bool(explicit)) or inferred


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))
