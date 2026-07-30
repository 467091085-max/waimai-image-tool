from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 2
JOB_TYPE = "menu_batch_generation"
PRICING_VERSION = "v1"
QUALITY_POINTS = {"standard": 10, "premium": 20}
WATERMARK_POINTS = 50
EXTRA_PLATFORM_POINTS = 100
PLATFORMS = {
    "meituan": {"id": "meituan", "width": 800, "height": 600, "maxKB": 5120},
    "taobao": {"id": "taobao", "width": 800, "height": 800, "maxKB": 20480},
    "jd": {"id": "jd", "width": 800, "height": 800, "maxKB": 5120},
}
PLATFORM_ORDER = tuple(PLATFORMS)
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
SHA256_RE = re.compile(r"[a-f0-9]{64}")


class BatchContractError(ValueError):
    def __init__(self, code: str, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def freeze_menu_batch_contract(
    *,
    job_id: str,
    user_id: str,
    menu_upload_id: str,
    menu: Mapping[str, Any],
    selected_background: Mapping[str, Any],
    generation_provenance: Mapping[str, Any],
    quality: str,
    image_count: int,
    platforms: Sequence[str],
    watermark: Mapping[str, Any] | None,
    idempotency_key: str,
    pricing_version: str = PRICING_VERSION,
    created_at: str | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_id(job_id, "jobId")
    clean_user_id = _clean_id(user_id, "userId")
    clean_menu_upload_id = _clean_id(menu_upload_id, "menuUploadId")
    clean_menu = _menu_snapshot(menu)
    clean_background = _background_snapshot(selected_background)
    clean_provenance = _generation_provenance(generation_provenance)
    clean_quality = _clean_quality(quality)
    clean_count = _positive_int(image_count, "billing.imageCount")
    clean_platforms = _platform_snapshots(platforms)
    clean_watermark = _watermark_snapshot(watermark or {})
    clean_idempotency_key = _clean_id(idempotency_key, "idempotency.key")
    clean_pricing_version = _clean_id(pricing_version, "billing.pricingVersion")

    generation_points = clean_count * QUALITY_POINTS[clean_quality]
    watermark_points = WATERMARK_POINTS if clean_watermark["enabled"] else 0
    extra_platform_points = max(0, len(clean_platforms) - 1) * EXTRA_PLATFORM_POINTS
    total_points = generation_points + watermark_points + extra_platform_points

    contract = {
        "schemaVersion": SCHEMA_VERSION,
        "jobType": JOB_TYPE,
        "jobId": clean_job_id,
        "menuUploadId": clean_menu_upload_id,
        "userId": clean_user_id,
        "menu": clean_menu,
        "selectedBackground": clean_background,
        "generationProvenance": clean_provenance,
        "quality": {
            "id": clean_quality,
            "pointsPerImage": QUALITY_POINTS[clean_quality],
        },
        "platforms": clean_platforms,
        "watermark": clean_watermark,
        "billing": {
            "snapshotVersion": 1,
            "pricingVersion": clean_pricing_version,
            "imageCount": clean_count,
            "generationPoints": generation_points,
            "watermarkPoints": watermark_points,
            "extraPlatformPoints": extra_platform_points,
            "totalPoints": total_points,
            "debitOrderId": f"gen:{clean_job_id}:debit",
            "refundOrderId": f"gen:{clean_job_id}:refund",
            "state": "pending_debit",
        },
        "idempotency": {
            "key": clean_idempotency_key,
            "requestSha256": "",
        },
        "createdAt": _clean_created_at(created_at),
    }
    contract["idempotency"]["requestSha256"] = request_sha256(contract)
    return contract


def request_sha256(contract: Mapping[str, Any]) -> str:
    basis = {
        "schemaVersion": contract.get("schemaVersion"),
        "jobType": contract.get("jobType"),
        "menuUploadId": contract.get("menuUploadId"),
        "userId": contract.get("userId"),
        "menu": contract.get("menu"),
        "selectedBackground": contract.get("selectedBackground"),
        "generationProvenance": contract.get("generationProvenance"),
        "quality": contract.get("quality"),
        "platforms": contract.get("platforms"),
        "watermark": contract.get("watermark"),
        "billing": _billing_request_basis(contract.get("billing")),
    }
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _menu_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "menu")
    return {
        "objectKey": _object_key(source.get("objectKey"), "menu.objectKey"),
        "sha256": _sha256(source.get("sha256"), "menu.sha256"),
        "parserVersion": _positive_int(source.get("parserVersion", 1), "menu.parserVersion"),
    }


def _background_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "selectedBackground")
    snapshot = {
        "assetId": _clean_id(source.get("assetId"), "selectedBackground.assetId"),
        "styleId": _clean_id(source.get("styleId"), "selectedBackground.styleId"),
        "sha256": _sha256(source.get("sha256"), "selectedBackground.sha256"),
        "objectKey": _object_key(source.get("objectKey"), "selectedBackground.objectKey"),
        "width": _bounded_dimension(source.get("width"), "selectedBackground.width"),
        "height": _bounded_dimension(source.get("height"), "selectedBackground.height"),
    }
    library_asset_id = str(source.get("libraryAssetId") or "").strip()
    if library_asset_id:
        snapshot["libraryAssetId"] = _clean_id(
            library_asset_id,
            "selectedBackground.libraryAssetId",
        )
    return snapshot


def _generation_provenance(
    value: Mapping[str, Any],
) -> dict[str, str]:
    source = _mapping(value, "generationProvenance")
    return {
        "taxonomyVersion": _clean_id(
            source.get("taxonomyVersion"),
            "generationProvenance.taxonomyVersion",
        ),
        "dishPromptVersion": _clean_id(
            source.get("dishPromptVersion"),
            "generationProvenance.dishPromptVersion",
        ),
        "pipelineVersion": _clean_id(
            source.get("pipelineVersion"),
            "generationProvenance.pipelineVersion",
        ),
        "provider": _clean_id(
            source.get("provider"),
            "generationProvenance.provider",
        ),
        "providerMode": _clean_id(
            source.get("providerMode"),
            "generationProvenance.providerMode",
        ),
        "modelName": _clean_id(
            source.get("modelName"),
            "generationProvenance.modelName",
        ),
        "modelVersion": _clean_id(
            source.get("modelVersion"),
            "generationProvenance.modelVersion",
        ),
    }


def _platform_snapshots(values: Sequence[str]) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise BatchContractError(
            "invalid_platforms",
            "platforms must be a non-empty array",
            field="platforms",
        )
    requested = [str(value).strip() for value in values]
    if not requested:
        raise BatchContractError(
            "invalid_platforms",
            "at least one platform is required",
            field="platforms",
        )
    if len(set(requested)) != len(requested):
        raise BatchContractError(
            "duplicate_platform",
            "platforms must not contain duplicates",
            field="platforms",
        )
    unknown = [platform_id for platform_id in requested if platform_id not in PLATFORMS]
    if unknown:
        raise BatchContractError(
            "unsupported_platform",
            f"unsupported platform: {unknown[0]}",
            field="platforms",
        )
    selected = set(requested)
    return [dict(PLATFORMS[platform_id]) for platform_id in PLATFORM_ORDER if platform_id in selected]


def _watermark_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "watermark")
    enabled = _bool(source.get("enabled", False), "watermark.enabled")
    watermark_type = str(source.get("type") or "text").strip().lower()
    if watermark_type not in {"text", "logo"}:
        raise BatchContractError(
            "invalid_watermark_type",
            "watermark.type must be text or logo",
            field="watermark.type",
        )
    color = str(source.get("color") or "black").strip().lower()
    if color not in {"black", "white"}:
        raise BatchContractError(
            "invalid_watermark_color",
            "watermark.color must be black or white",
            field="watermark.color",
        )
    position = str(source.get("position") or "bottom-right").strip().lower()
    if position not in {"top-left", "top-right", "bottom-left", "bottom-right", "center"}:
        raise BatchContractError(
            "invalid_watermark_position",
            "unsupported watermark position",
            field="watermark.position",
        )
    pattern = str(source.get("pattern") or "corner").strip().lower()
    if pattern not in {"corner", "tile"}:
        raise BatchContractError(
            "invalid_watermark_pattern",
            "watermark.pattern must be corner or tile",
            field="watermark.pattern",
        )
    text = str(source.get("text") or "").strip()[:64]
    logo_key = str(source.get("logoObjectKey") or "").strip()
    if not enabled:
        logo_key = ""
    elif watermark_type == "logo":
        logo_key = _object_key(logo_key, "watermark.logoObjectKey")
    else:
        logo_key = ""
    return {
        "enabled": enabled,
        "type": watermark_type,
        "text": text,
        "logoObjectKey": logo_key,
        "color": color,
        "position": position,
        "pattern": pattern,
    }


def _billing_request_basis(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source.get(key)
        for key in (
            "snapshotVersion",
            "pricingVersion",
            "imageCount",
            "generationPoints",
            "watermarkPoints",
            "extraPlatformPoints",
            "totalPoints",
        )
    }


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BatchContractError(
            "invalid_object",
            f"{field} must be an object",
            field=field,
        )
    return value


def _clean_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise BatchContractError(
            "invalid_identifier",
            f"{field} has an invalid format",
            field=field,
        )
    clean = value.strip()
    if not ID_RE.fullmatch(clean):
        raise BatchContractError(
            "invalid_identifier",
            f"{field} has an invalid format",
            field=field,
        )
    return clean


def _object_key(value: Any, field: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if (
        not clean
        or len(clean) > 512
        or clean.startswith(("/", "\\"))
        or "\\" in clean
        or "://" in clean
        or any(part in {"", ".", ".."} for part in clean.split("/"))
        or any(ord(char) < 32 for char in clean)
    ):
        raise BatchContractError(
            "invalid_object_key",
            f"{field} must be a private relative object key",
            field=field,
        )
    return clean


def _sha256(value: Any, field: str) -> str:
    clean = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(clean):
        raise BatchContractError(
            "invalid_sha256",
            f"{field} must be a 64-character SHA-256 digest",
            field=field,
        )
    return clean


def _clean_quality(value: Any) -> str:
    clean = str(value or "").strip().lower()
    if clean not in QUALITY_POINTS:
        raise BatchContractError(
            "invalid_quality",
            "quality must be standard or premium",
            field="quality",
        )
    return clean


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BatchContractError("invalid_integer", f"{field} must be an integer", field=field)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BatchContractError("invalid_integer", f"{field} must be an integer", field=field) from exc
    if number <= 0:
        raise BatchContractError("invalid_integer", f"{field} must be positive", field=field)
    return number


def _bounded_dimension(value: Any, field: str) -> int:
    number = _positive_int(value, field)
    if number > 12_000:
        raise BatchContractError(
            "invalid_dimension",
            f"{field} exceeds the supported image limit",
            field=field,
        )
    return number


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise BatchContractError(
            "invalid_boolean",
            f"{field} must be a boolean",
            field=field,
        )
    return value


def _clean_created_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    clean = str(value).strip()
    if not clean or len(clean) > 40:
        raise BatchContractError(
            "invalid_timestamp",
            "createdAt has an invalid format",
            field="createdAt",
        )
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BatchContractError(
            "invalid_timestamp",
            "createdAt has an invalid format",
            field="createdAt",
        ) from exc
    if parsed.tzinfo is None:
        raise BatchContractError(
            "invalid_timestamp",
            "createdAt must include a timezone",
            field="createdAt",
        )
    return clean
