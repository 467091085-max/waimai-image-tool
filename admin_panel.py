from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from flask import Blueprint, Response, jsonify, render_template, request

MENU_EXTS = {".xls", ".xlsx"}
REQUIRED_SOURCES = ("clean", "watermark", "internal")
DEFAULT_AI_ASSET_MANIFEST_PATH = Path("data/library/_ai_asset_library/manifest.jsonl")
ADMIN_LIST_RESOURCES = {
    "users": "list_users",
    "stores": "list_stores",
    "orders": "list_orders",
    "generation-tasks": "list_generation_tasks",
    "asset-access": "list_asset_access_logs",
    "risk-events": "list_risk_events",
    "commission-settlements": "list_commission_settlements",
    "withdrawals": "list_withdrawals",
}


@dataclass(frozen=True)
class AdminDependencies:
    library_images: Callable[[], Sequence[Any]]
    media_url_for_path: Callable[[Path], str]
    current_menu_path: Callable[[], Path | None]
    parse_menu: Callable[[Path | None], Mapping[str, Any]]
    upload_dir: Path
    db_path: Path | str | None = None
    ai_asset_manifest_path: Path | str | None = None


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

    @blueprint.get("/api/admin/dashboard")
    def admin_dashboard():
        payload = admin_dashboard_payload(deps)
        return jsonify(payload)

    @blueprint.get("/api/admin/ai-assets")
    def admin_ai_assets():
        payload = ai_assets_payload(deps)
        return jsonify(payload)

    @blueprint.post("/api/admin/actions/ai-assets/<asset_id>/status")
    def admin_ai_asset_status_action(asset_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, Mapping):
            payload = {}
        body, status = ai_asset_status_action_payload(deps, asset_id, payload, request.headers)
        return jsonify(body), status

    @blueprint.get("/api/admin/lists/<resource>")
    def admin_list_resource(resource: str):
        payload, status = admin_list_payload(deps, resource, request.args)
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
    styles = {str(_field(image, "style_id", "")) for image in images if _field(image, "style_id", "")}
    reusable = sum(1 for image in images if bool(_field(image, "reusable", False)))

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
            "reusable": reusable,
            "referenceOnly": max(0, len(images) - reusable),
            "stores": len(stores),
            "styles": len(styles),
        },
        "sources": dict(sorted(sources.items())),
        "samples": samples[:limit],
        "bySource": by_source,
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


def admin_dashboard_payload(deps: AdminDependencies) -> dict[str, Any]:
    try:
        import admin_data
        import storage_db
    except Exception as exc:
        return _empty_dashboard_payload(f"{type(exc).__name__}: {exc}")

    try:
        conn = storage_db.init_db(deps.db_path)
        should_close = True
    except Exception as exc:
        return _empty_dashboard_payload(f"{type(exc).__name__}: {exc}")

    try:
        return {
            "ok": True,
            "summary": admin_data.dashboard_summary(conn),
            "recentJobs": admin_data.recent_jobs(conn, limit=12),
            "commissions": admin_data.commission_summary(conn),
            "risk": admin_data.risk_summary(conn),
            "assetAccess": admin_data.asset_access_summary(conn),
            "generatedAt": storage_db.utc_now(),
        }
    except Exception as exc:
        return _empty_dashboard_payload(f"{type(exc).__name__}: {exc}")
    finally:
        if should_close:
            conn.close()


def ai_assets_payload(deps: AdminDependencies, limit: int = 50) -> dict[str, Any]:
    max_assets = max(0, min(int(limit), 50))
    try:
        import ai_asset_repository

        repository_cls = getattr(ai_asset_repository, "AiAssetRepository", None) or getattr(ai_asset_repository, "AIAssetRepository")
        manifest_path = deps.ai_asset_manifest_path or DEFAULT_AI_ASSET_MANIFEST_PATH
        records = repository_cls(manifest_path).list_assets()
    except Exception:
        records = []

    statuses = Counter(_asset_admin_status(record) for record in records)
    by_kind = Counter(str(record.get("kind") or "unknown") for record in records)
    by_category = Counter(str(record.get("category") or "未分类") for record in records)
    pending = statuses.get("pending", 0)

    return {
        "ok": True,
        "summary": {
            "total": len(records),
            "approved": statuses.get("approved", 0),
            "rejected": statuses.get("rejected", 0),
            "disabled": statuses.get("disabled", 0),
            "pending": pending,
            "byKind": dict(sorted(by_kind.items())),
            "byCategory": dict(sorted(by_category.items())),
        },
        "assets": [_ai_asset_admin_view(record) for record in records[:max_assets]],
    }


def ai_asset_status_action_payload(
    deps: AdminDependencies,
    asset_id: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    status = _payload_text(payload, "status", "action")
    reason = _payload_text(payload, "qualityNote", "quality_note", "note", "reason")
    actor = (
        _payload_text(payload, "actorUserId", "actor_user_id", "actor")
        or _arg(headers or {}, "X-Admin-User")
        or "admin"
    )

    conn = None
    try:
        import admin_actions
        import ai_asset_repository
        import storage_db

        repository_cls = getattr(ai_asset_repository, "AiAssetRepository", None) or getattr(ai_asset_repository, "AIAssetRepository")
        manifest_path = deps.ai_asset_manifest_path or DEFAULT_AI_ASSET_MANIFEST_PATH
        repo = repository_cls(manifest_path)
        previous = repo.get(asset_id)
        if previous is None:
            return _admin_action_error("ai_asset_not_found", f"AI asset not found: {asset_id}", 404)

        conn = storage_db.init_db(deps.db_path)
        updated = admin_actions.mark_ai_asset_status(repo, asset_id, status, quality_note=reason)
        audit = admin_actions.admin_audit_event(
            conn,
            actor,
            "ai_asset_status_updated",
            "ai_asset",
            str(updated.get("asset_id") or asset_id),
            metadata={
                "fromStatus": _asset_admin_status(previous),
                "toStatus": _asset_admin_status(updated),
                "qualityNote": reason,
                "assetKind": str(updated.get("kind") or ""),
                "category": str(updated.get("category") or ""),
                "styleId": str(updated.get("style_id") or ""),
                "productName": str(updated.get("product_name") or ""),
            },
            reason=reason,
        )
        return {
            "ok": True,
            "asset": _ai_asset_admin_view(updated),
            "audit": {
                "id": audit["id"],
                "action": audit["action"],
                "target": audit["target"],
                "status": audit["status"],
                "createdAt": audit["createdAt"],
            },
        }, 200
    except ValueError as exc:
        return _admin_action_error("invalid_ai_asset_status_action", str(exc), 400)
    except KeyError as exc:
        return _admin_action_error("ai_asset_not_found", str(exc), 404)
    except Exception as exc:
        return _admin_action_error("ai_asset_status_action_failed", f"{type(exc).__name__}: {exc}", 500)
    finally:
        if conn is not None:
            conn.close()


def admin_list_payload(deps: AdminDependencies, resource: str, args: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    normalized_resource = _normalize_resource(resource)
    function_name = ADMIN_LIST_RESOURCES.get(normalized_resource)
    if not function_name:
        return {
            "ok": False,
            "error": "unsupported admin list resource",
            "code": "unsupported_admin_list_resource",
            "resource": normalized_resource,
        }, 404

    conn = None
    try:
        admin_data, storage_db, conn = _admin_data_conn(deps)
        list_fn = getattr(admin_data, function_name)
        page = list_fn(conn, **_admin_list_args(normalized_resource, args))
        return {
            "ok": True,
            "resource": normalized_resource,
            **page,
            "generatedAt": storage_db.utc_now(),
        }, 200
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "code": "admin_list_failed",
            "resource": normalized_resource,
            "items": [],
            "total": 0,
        }, 500
    finally:
        if conn is not None:
            conn.close()


def _empty_dashboard_payload(error: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "error": error,
        "summary": {
            "jobs": {"total": 0, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "canceled": 0},
            "images": {"total": 0, "generated": 0, "failed": 0, "rejected": 0},
            "exports": {"total": 0, "ready": 0, "failed": 0, "expired": 0},
            "points": {"credits": 0, "debits": 0, "net": 0},
            "commissions": {"pendingAmount": 0, "eligibleAmount": 0, "settledAmount": 0, "orderCount": 0},
            "risk": {"total": 0, "review": 0, "denied": 0},
            "assetAccess": {"total": 0, "allowed": 0, "denied": 0},
        },
        "recentJobs": [],
        "commissions": {"byStatus": {}, "pendingAmount": 0, "eligibleAmount": 0, "settledAmount": 0},
        "risk": {"byDecision": {}, "byLevel": {}, "highestLevel": "info"},
        "assetAccess": {"byAction": {}, "allowed": 0, "denied": 0, "topDenyReason": ""},
        "generatedAt": "",
    }


def _admin_data_conn(deps: AdminDependencies):
    import admin_actions
    import admin_data
    import auth_service
    import payment_service
    import storage_db

    conn = storage_db.init_db(deps.db_path)
    auth_service.init_auth_schema(conn)
    payment_service.init_payment_schema(conn)
    admin_actions.init_admin_actions_schema(conn)
    return admin_data, storage_db, conn


def _admin_list_args(resource: str, args: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "limit": _bounded_int(_arg(args, "limit"), default=50, minimum=0, maximum=200),
        "offset": _bounded_int(_arg(args, "offset"), default=0, minimum=0, maximum=100_000),
        "sort": _arg(args, "sort") or "created_at",
        "order": _arg(args, "order") or "desc",
    }
    for source, target in (
        ("search", "search"),
        ("createdFrom", "created_from"),
        ("created_from", "created_from"),
        ("createdTo", "created_to"),
        ("created_to", "created_to"),
    ):
        value = _arg(args, source)
        if value and target not in payload:
            payload[target] = value

    filters: dict[str, tuple[str, ...]] = {
        "users": ("status",),
        "stores": ("status", "createdByUserId", "created_by_user_id"),
        "orders": ("status", "userId", "user_id", "provider"),
        "generation-tasks": ("status", "menuUploadId", "menu_upload_id", "storeName", "store_name", "styleId", "style_id"),
        "asset-access": ("status", "action", "assetType", "asset_type", "userId", "user_id", "agentId", "agent_id", "assetId", "asset_id"),
        "risk-events": ("decision", "riskLevel", "risk_level", "eventType", "event_type", "userId", "user_id"),
        "commission-settlements": ("status", "agentId", "agent_id"),
        "withdrawals": ("status", "agentId", "agent_id"),
    }
    aliases = {
        "createdByUserId": "created_by_user_id",
        "userId": "user_id",
        "menuUploadId": "menu_upload_id",
        "storeName": "store_name",
        "styleId": "style_id",
        "assetType": "asset_type",
        "agentId": "agent_id",
        "assetId": "asset_id",
        "riskLevel": "risk_level",
        "eventType": "event_type",
    }
    for name in filters.get(resource, ()):
        value = _arg(args, name)
        if value:
            payload[aliases.get(name, name)] = value

    if resource == "asset-access":
        allowed = _optional_bool(_arg(args, "allowed"))
        if allowed is not None:
            payload["allowed"] = allowed

    return payload


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


def _ai_asset_admin_view(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "assetId": str(record.get("asset_id") or ""),
        "kind": str(record.get("kind") or ""),
        "category": str(record.get("category") or ""),
        "styleId": str(record.get("style_id") or ""),
        "productName": str(record.get("product_name") or ""),
        "qualityScore": float(record.get("quality_score") or 0),
        "qualityReasons": [str(reason) for reason in record.get("quality_reasons", []) if str(reason or "").strip()],
        "status": _asset_admin_status(record),
        "createdAt": str(record.get("created_at") or ""),
    }


def _asset_admin_status(record: Mapping[str, Any]) -> str:
    status = str(record.get("status") or "").strip().lower()
    if status in {"approved", "rejected", "disabled"}:
        return status
    return "pending"


def _image_sample(image: Any, deps: AdminDependencies) -> dict[str, Any]:
    path_value = _field(image, "path", "")
    path = Path(path_value) if path_value else Path()
    return {
        "imageId": str(_field(image, "image_id", "")),
        "dishName": str(_field(image, "dish", "")),
        "store": str(_field(image, "store", "")),
        "source": str(_field(image, "source", "unknown") or "unknown"),
        "styleId": str(_field(image, "style_id", "")),
        "styleName": _style_label(str(_field(image, "style_id", ""))),
        "reusable": bool(_field(image, "reusable", False)),
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


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _normalize_resource(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _arg(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name) if hasattr(args, "get") else None
    if value is None:
        return ""
    return str(value).strip()


def _payload_text(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name) if hasattr(payload, "get") else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _admin_action_error(code: str, message: str, status: int) -> tuple[dict[str, Any], int]:
    return {
        "ok": False,
        "code": code,
        "error": message,
    }, status


def _optional_bool(value: str) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "allowed"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "denied"}:
        return False
    return None
