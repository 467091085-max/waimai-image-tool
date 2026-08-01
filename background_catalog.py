from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata
from typing import Any, Iterable

from matching_engine import TAXONOMY_RULES, TAXONOMY_VERSION


CATALOG_SCHEMA_VERSION = 1
CATALOG_VERSION = "background-catalog.v1"
REVIEW_STATUSES = {"pending", "approved", "rejected", "disabled"}
SHA256_RE = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class BackgroundStyleSlot:
    style_id: str
    slot_id: str
    name: str
    scene_type: str
    prompt: str

    def public_payload(self) -> dict[str, str]:
        return asdict(self)


STYLE_SLOTS = (
    BackgroundStyleSlot(
        "style-1",
        "solid-warm",
        "暖色纯色棚拍",
        "seamless-solid",
        "暖色单色无缝摄影棚弧面，地面与背景连续，无可见接缝、折角或地平线，柔和高调漫射光",
    ),
    BackgroundStyleSlot(
        "style-2",
        "solid-cool",
        "冷色纯色棚拍",
        "seamless-solid",
        "冷色单色无缝摄影棚弧面，地面与背景连续，无可见接缝、折角或地平线，克制均匀棚拍光",
    ),
    BackgroundStyleSlot(
        "style-3",
        "table-light-stone",
        "明亮浅石桌面",
        "flat-table",
        "平整浅色石材桌面从左右与下边缘连续铺满，简洁墙面，约25度轻俯视，明亮自然日光",
    ),
    BackgroundStyleSlot(
        "style-4",
        "table-dark-stone",
        "深色石材桌面",
        "flat-table",
        "平整深色哑光石材桌面从左右与下边缘连续铺满，深中性墙面，约25度轻俯视，柔和侧光",
    ),
    BackgroundStyleSlot(
        "style-5",
        "table-natural-wood",
        "温暖木质桌面",
        "flat-table",
        "平整天然木纹桌面从左右与下边缘连续铺满，素净墙面，约25度轻俯视，温暖自然光",
    ),
    BackgroundStyleSlot(
        "style-6",
        "table-commercial-neutral",
        "冷中性商业桌面",
        "flat-table",
        "平整冷中性商业摄影桌面从左右与下边缘连续铺满，无缝简洁背景墙，约25度轻俯视，均匀棚拍光",
    ),
)

STYLE_IDS = tuple(slot.style_id for slot in STYLE_SLOTS)
STYLE_SLOT_BY_ID = {slot.style_id: slot for slot in STYLE_SLOTS}
CATEGORY_LABELS = {
    category_id: label
    for category_id, label, _keywords in TAXONOMY_RULES
}
CATEGORY_IDS = tuple(CATEGORY_LABELS)

if len(CATEGORY_IDS) != 40 or len(set(CATEGORY_IDS)) != 40:
    raise RuntimeError("background catalog requires 40 unique taxonomies")
if len(STYLE_IDS) != 6 or len(set(STYLE_IDS)) != 6:
    raise RuntimeError("background catalog requires six unique style slots")


def style_slot(style_id: Any) -> BackgroundStyleSlot:
    try:
        return STYLE_SLOT_BY_ID[str(style_id or "").strip()]
    except KeyError as exc:
        raise ValueError(f"unknown background style slot: {style_id}") from exc


def normalize_category_id(value: Any) -> str:
    text = str(value or "").strip()
    if text in CATEGORY_LABELS:
        return text
    for category_id, label in CATEGORY_LABELS.items():
        if text == label:
            return category_id
    raise ValueError(f"unknown background category: {value}")


def category_label(category_id: Any) -> str:
    return CATEGORY_LABELS[normalize_category_id(category_id)]


def expected_catalog_pairs() -> tuple[tuple[str, str], ...]:
    return tuple(
        (category_id, style_id)
        for category_id in CATEGORY_IDS
        for style_id in STYLE_IDS
    )


def _version_token(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", text):
        raise ValueError(f"invalid background catalog version: {value}")
    return text


def _object_prefix(value: Any) -> str:
    parts = [part for part in str(value or "").strip("/").split("/") if part]
    if not parts or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part)
        for part in parts
    ):
        raise ValueError("invalid background catalog object prefix")
    return "/".join(parts)


def catalog_object_key(
    *,
    category_id: Any,
    style_id: Any,
    prompt_version: Any,
    prompt_sha256: str,
    asset_sha256: str,
    suffix: str = ".jpg",
    tenant_id: str = "waimai-shared",
    prefix: str = "ai-assets",
) -> str:
    category = normalize_category_id(category_id)
    style = style_slot(style_id).style_id
    prompt_version_token = _version_token(prompt_version)
    prompt_digest = str(prompt_sha256 or "").strip().lower()
    asset_digest = str(asset_sha256 or "").strip().lower()
    if not SHA256_RE.fullmatch(prompt_digest):
        raise ValueError("invalid background prompt sha256")
    if not SHA256_RE.fullmatch(asset_digest):
        raise ValueError("invalid background asset sha256")
    clean_suffix = str(suffix or "").lower()
    if clean_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("invalid background asset suffix")
    tenant = _version_token(tenant_id)
    return "/".join(
        (
            _object_prefix(prefix),
            tenant,
            "background-catalog",
            CATALOG_VERSION,
            TAXONOMY_VERSION,
            category,
            style,
            prompt_version_token,
            f"prompt-{prompt_digest[:16]}",
            f"{asset_digest}{clean_suffix}",
        )
    )


def catalog_manifest_key(
    category_id: Any,
    prompt_version: Any,
    *,
    tenant_id: str = "waimai-shared",
    prefix: str = "ai-assets",
) -> str:
    return "/".join(
        (
            _object_prefix(prefix),
            _version_token(tenant_id),
            "background-catalog",
            CATALOG_VERSION,
            TAXONOMY_VERSION,
            normalize_category_id(category_id),
            _version_token(prompt_version),
            "manifest.json",
        )
    )


def category_manifest_status(
    records: Iterable[dict[str, Any]],
    *,
    category_id: Any,
    prompt_version: Any,
) -> dict[str, Any]:
    category = normalize_category_id(category_id)
    expected_prompt_version = _version_token(prompt_version)
    approved_by_style: dict[str, list[dict[str, Any]]] = {
        style_id: [] for style_id in STYLE_IDS
    }
    pending = 0
    rejected = 0
    for raw in records:
        if str(raw.get("catalogVersion") or "") != CATALOG_VERSION:
            continue
        if str(raw.get("taxonomyVersion") or "") != TAXONOMY_VERSION:
            continue
        if str(raw.get("categoryId") or "") != category:
            continue
        if str(raw.get("promptVersion") or "") != expected_prompt_version:
            continue
        style_id = str(raw.get("styleId") or "")
        if style_id not in approved_by_style:
            continue
        status = str(raw.get("reviewStatus") or "pending").lower()
        if status == "approved":
            approved_by_style[style_id].append(raw)
        elif status == "rejected":
            rejected += 1
        else:
            pending += 1

    missing = [
        style_id
        for style_id, matches in approved_by_style.items()
        if not matches
    ]
    duplicates = [
        style_id
        for style_id, matches in approved_by_style.items()
        if len(matches) > 1
    ]
    ready = not missing and not duplicates
    return {
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "catalogVersion": CATALOG_VERSION,
        "taxonomyVersion": TAXONOMY_VERSION,
        "categoryId": category,
        "categoryName": CATEGORY_LABELS[category],
        "promptVersion": expected_prompt_version,
        "status": "ready" if ready else "incomplete",
        "ready": ready,
        "approvedCount": sum(
            1 for matches in approved_by_style.values() if len(matches) == 1
        ),
        "missingStyleIds": missing,
        "duplicateStyleIds": duplicates,
        "pendingCount": pending,
        "rejectedCount": rejected,
        "assets": [
            approved_by_style[style_id][0]
            for style_id in STYLE_IDS
            if len(approved_by_style[style_id]) == 1
        ],
    }
