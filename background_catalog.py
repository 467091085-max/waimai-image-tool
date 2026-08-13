from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

from matching_engine import TAXONOMY_RULES, TAXONOMY_VERSION


CATALOG_SCHEMA_VERSION = 1
CATALOG_VERSION = "background-catalog.v1"
EMPTY_SET_PROMPT_VERSION = "style-background.v14"
REVIEW_STATUSES = {"pending", "approved", "rejected", "disabled"}
SHA256_RE = re.compile(r"[a-f0-9]{64}")


def generation_evidence_payload(entry: dict[str, Any]) -> dict[str, Any]:
    def first(*names: str) -> Any:
        for name in names:
            value = entry.get(name)
            if value not in (None, ""):
                return value
        return None

    payload = {
        "generationProvider": first(
            "generationProvider",
            "generation_provider",
            "source_provider",
            "provider",
        ),
        "providerAction": first("providerAction", "provider_action", "action"),
        "model": first("model", "model_name"),
        "seed": first("seed", "applied_seed"),
        "requestedSeed": first("requestedSeed", "requested_seed"),
        "seedApplied": first("seedApplied", "seed_applied"),
        "promptRevisionEnabled": first(
            "promptRevisionEnabled",
            "prompt_revision_enabled",
        ),
        "promptRevisionControlApplied": first(
            "promptRevisionControlApplied",
            "prompt_revision_control_applied",
        ),
    }
    optional_fields = {
        "seedEvidenceSource": first(
            "seedEvidenceSource",
            "seed_evidence_source",
        ),
        "providerSeedEchoed": first(
            "providerSeedEchoed",
            "provider_seed_echoed",
        ),
        "providerSeedPresent": first(
            "providerSeedPresent",
            "provider_seed_present",
        ),
        "seedControlSubmitted": first(
            "seedControlSubmitted",
            "seed_control_submitted",
        ),
        "promptRevisionControlSubmitted": first(
            "promptRevisionControlSubmitted",
            "prompt_revision_control_submitted",
        ),
    }
    payload.update(
        {
            key: value
            for key, value in optional_fields.items()
            if value is not None
        }
    )
    return payload


def generation_evidence_valid(
    entry: dict[str, Any],
    *,
    prompt_version: Any = None,
) -> bool:
    version = str(
        prompt_version
        if prompt_version not in (None, "")
        else entry.get("promptVersion") or entry.get("prompt_version") or ""
    ).strip()
    if version != EMPTY_SET_PROMPT_VERSION:
        return True
    evidence = generation_evidence_payload(entry)
    action = str(evidence.get("providerAction") or "").strip()
    expected_model = {
        "TokenHubImageV3": "hy-image-v3.0",
        "TokenHubHyImageV3": "hy-image-v3",
    }.get(action)
    requested_seed = evidence.get("requestedSeed")
    applied_seed = evidence.get("seed")
    seed_source = str(evidence.get("seedEvidenceSource") or "").strip()
    provider_echoed = evidence.get("providerSeedEchoed")
    provider_seed_present = evidence.get("providerSeedPresent")
    provider_presence_valid = (
        provider_seed_present is None
        or type(provider_seed_present) is bool
    )
    submitted_request_evidence = bool(
        seed_source == "submitted-request"
        and evidence.get("seedControlSubmitted") is True
        and evidence.get("promptRevisionControlSubmitted") is True
        and evidence.get("promptRevisionControlApplied") is False
        and (
            (
                provider_echoed is False
                and provider_seed_present is False
                and applied_seed is None
                and evidence.get("seedApplied") is False
            )
            or (
                provider_echoed is True
                and provider_seed_present is True
                and type(applied_seed) is int
                and applied_seed == requested_seed
                and evidence.get("seedApplied") is True
            )
        )
    )
    provider_response_evidence = bool(
        seed_source in {"", "provider-response"}
        and provider_echoed in {None, True}
        and type(applied_seed) is int
        and applied_seed == requested_seed
        and evidence.get("seedApplied") is True
        and evidence.get("promptRevisionControlApplied") is True
    )
    return bool(
        str(evidence.get("generationProvider") or "").strip()
        == "tencent-hunyuan"
        and expected_model is not None
        and str(evidence.get("model") or "").strip().lower()
        == expected_model
        and evidence.get("promptRevisionEnabled") is False
        and type(requested_seed) is int
        and 1 <= requested_seed <= 4_294_967_295
        and provider_presence_valid
        and (submitted_request_evidence or provider_response_evidence)
    )


def frozen_generation_evidence(
    entry: dict[str, Any],
    *,
    prompt_version: Any,
    asset_sha256: Any,
) -> dict[str, Any]:
    payload = generation_evidence_payload(entry)
    payload["promptVersion"] = str(prompt_version or "").strip()
    payload["assetSha256"] = str(asset_sha256 or "").strip().lower()
    return payload


def generation_evidence_sha256(evidence: dict[str, Any]) -> str:
    raw = json.dumps(
        evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
        "单一低饱和主色的哑光微水泥无缝空间，墙面与地面由建筑圆弧连续过渡，"
        "全画面只有一种主色及自然明暗，无纸卷、幕布、板材、接缝、独立平面、"
        "前沿、厚度或几何块，柔和高调漫射光",
    ),
    BackgroundStyleSlot(
        "style-2",
        "solid-cool",
        "冷色纯色棚拍",
        "seamless-solid",
        "单一低饱和辅色的哑光微水泥无缝空间，墙面与地面由建筑圆弧连续过渡，"
        "全画面只有一种主色及自然明暗，无纸卷、幕布、板材、接缝、独立平面、"
        "前沿、厚度或几何块，克制均匀棚拍光",
    ),
    BackgroundStyleSlot(
        "style-3",
        "table-light-stone",
        "明亮浅石桌面",
        "flat-table",
        "一张普通浅色石材餐桌的连续桌面从左右与下边缘铺满，桌面前沿和厚度"
        "位于画幅下方不可见，石纹连续穿过中央，无第二层表面，简洁墙面，"
        "约25度轻俯视，明亮自然日光",
    ),
    BackgroundStyleSlot(
        "style-4",
        "table-dark-stone",
        "深色石材桌面",
        "flat-table",
        "一张普通深色哑光石材餐桌的连续桌面从左右与下边缘铺满，桌面前沿和"
        "厚度位于画幅下方不可见，石纹连续穿过中央，无第二层表面，深中性墙面，"
        "约25度轻俯视，柔和侧光",
    ),
    BackgroundStyleSlot(
        "style-5",
        "table-natural-wood",
        "温暖木质桌面",
        "flat-table",
        "一张裸露天然木纹餐桌的连续桌面从左右与下边缘铺满，桌面前沿和厚度"
        "位于画幅下方不可见，木纹连续穿过中央，不放桌垫、纸张或砧板，"
        "无第二层表面，素净墙面，约25度轻俯视，温暖自然光",
    ),
    BackgroundStyleSlot(
        "style-6",
        "table-commercial-neutral",
        "冷中性商业桌面",
        "flat-table",
        "一张普通冷中性哑光餐桌的连续桌面从左右与下边缘铺满，桌面前沿和厚度"
        "位于画幅下方不可见，细腻纹理连续穿过中央，无第二层表面，简洁背景墙，"
        "约25度轻俯视，均匀自然光",
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
