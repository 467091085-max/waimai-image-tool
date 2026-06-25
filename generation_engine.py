from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


KIND_SINGLE = "single"
KIND_COMBO = "combo"
KIND_OTHER = "other"
QUALITY_NORMAL = "normal"
QUALITY_PREMIUM = "premium"

STRATEGY_REUSE = "reuse"
STRATEGY_REPLACE_BACKGROUND = "replace_background"
STRATEGY_REFERENCE_REDRAW = "reference_redraw"
STRATEGY_TEXT_TO_IMAGE3 = "text_to_image3"
STRATEGY_TEXT_TO_IMAGE_LITE = "text_to_image_lite"
STRATEGY_WAITING_FOR_PROVIDER = "waiting_for_provider"

PROVIDER_TENCENT = "tencent-hunyuan"
PROVIDER_LIBRARY = "library"

STATUS_SUCCEEDED = "succeeded"
STATUS_REUSED = "reused"
STATUS_FAILED = "failed"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PARTIAL = "partial"
PROVIDER_STATUSES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_PARTIAL}

COMBO_MARKERS = ("套餐", "组合", "双人", "单人餐", "多人", "+", "＋", "拼", "任选", "含", "配")
WATERMARK_MARKERS = ("watermark", "watermarkpic", "品牌水印", "水印", "logo", "商标")

STYLE_PROMPT_FALLBACK = "严格贴合所选背景风格，背景、光线、色彩和构图保持一致"
NEGATIVE_IMAGE_PROMPT = "文字，水印，logo，品牌名，价格，人物，手，低清晰度，模糊，变形，裁切主体，脏乱背景"
WAITING_FOR_PROVIDER_ERROR = "waiting_for_provider: 腾讯云生图环境变量未配置完整"
PROMPT_LIMIT = 900
EVIDENCE_KEYS = ("requestId", "jobId", "submitRequestId", "queryRequestId", "endpoint")


@dataclass(frozen=True)
class GenerationRequest:
    dish: str
    kind: str
    style: str
    quality: str
    platforms: tuple[str, ...] = ()
    watermark: bool | dict[str, Any] = False
    source_strategy: str = "auto"
    row: dict[str, Any] = field(default_factory=dict)
    candidates: tuple[dict[str, Any], ...] = ()
    component_matches: tuple[dict[str, Any], ...] = ()
    source_candidate: dict[str, Any] | None = None

    def with_strategy(self, strategy: str, source_candidate: dict[str, Any] | None = None) -> "GenerationRequest":
        return GenerationRequest(
            dish=self.dish,
            kind=self.kind,
            style=self.style,
            quality=self.quality,
            platforms=self.platforms,
            watermark=self.watermark,
            source_strategy=strategy,
            row=self.row,
            candidates=self.candidates,
            component_matches=self.component_matches,
            source_candidate=source_candidate,
        )


@dataclass
class GenerationResult:
    dish: str
    kind: str
    status: str
    source_strategy: str
    provider: str = PROVIDER_TENCENT
    action: str = ""
    prompt_type: str = ""
    candidate: dict[str, Any] | None = None
    path: str = ""
    provider_error: str | None = None
    retryable: bool = False
    refund_required: bool = False
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        evidence = {
            "provider": self.provider,
            "action": self.action,
            "status": self.status,
            "providerStatus": provider_status(self.status),
            "provider_status": provider_status(self.status),
        }
        for key in EVIDENCE_KEYS:
            value = self.metadata.get(key)
            if value:
                evidence[key] = value
        body = {
            "dish": self.dish,
            "kind": self.kind,
            "status": self.status,
            "provider_status": provider_status(self.status),
            "providerStatus": provider_status(self.status),
            "provider": self.provider,
            "action": self.action,
            "promptType": self.prompt_type,
            "source_strategy": self.source_strategy,
            "sourceStrategy": self.source_strategy,
            "retryable": self.retryable,
            "refund_required": self.refund_required,
            "refundRequired": self.refund_required,
            "reason": self.reason,
            "metadata": self.metadata,
            "evidence": evidence,
        }
        for key in EVIDENCE_KEYS:
            value = self.metadata.get(key)
            if value:
                body[key] = value
        if self.candidate is not None:
            body["candidate"] = self.candidate
        if self.path:
            body["path"] = self.path
        if self.provider_error:
            body["provider_error"] = self.provider_error
            body["providerError"] = self.provider_error
            body["error"] = self.provider_error
        return body


class GenerationProvider(Protocol):
    configured: bool
    allow_lite_fallback: bool

    def reuse(self, request: GenerationRequest) -> dict[str, Any]:
        ...

    def replace_background(self, request: GenerationRequest) -> dict[str, Any]:
        ...

    def reference_redraw(self, request: GenerationRequest) -> dict[str, Any]:
        ...

    def text_to_image(self, request: GenerationRequest) -> dict[str, Any]:
        ...


def normalize_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {KIND_SINGLE, "单品", "菜品", "single_dish"}:
        return KIND_SINGLE
    if text in {KIND_COMBO, "套餐", "套餐/组合", "组合", "combo", "set"}:
        return KIND_COMBO
    if any(marker in text for marker in COMBO_MARKERS):
        return KIND_COMBO
    return KIND_OTHER


def normalize_quality(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"premium", "精修", "refined"}:
        return QUALITY_PREMIUM
    return QUALITY_NORMAL


def request_from_row(
    row: dict[str, Any],
    *,
    style: str,
    quality: str | None = None,
    platforms: list[str] | tuple[str, ...] | None = None,
    watermark: bool | dict[str, Any] = False,
) -> GenerationRequest:
    candidates = tuple(c for c in row.get("candidates") or [] if isinstance(c, dict))
    component_matches = tuple(c for c in row.get("componentMatches") or [] if isinstance(c, dict))
    return GenerationRequest(
        dish=str(row.get("name") or row.get("dish") or row.get("dishName") or ""),
        kind=normalize_kind(row.get("kind")),
        style=str(style or ""),
        quality=normalize_quality(quality),
        platforms=tuple(str(platform) for platform in (platforms or ()) if str(platform or "").strip()),
        watermark=watermark,
        row=row,
        candidates=candidates,
        component_matches=component_matches,
    )


def select_generation_request(request: GenerationRequest) -> GenerationRequest:
    same_style = _same_style_reusable_candidate(request)
    if same_style:
        return request.with_strategy(STRATEGY_REUSE, same_style)

    if request.kind == KIND_COMBO:
        combo_source = _combo_reference_candidate(request)
        if combo_source:
            if candidate_has_brand_watermark(combo_source) or not combo_source.get("reusable", True):
                return request.with_strategy(STRATEGY_REFERENCE_REDRAW, combo_source)
            return request.with_strategy(STRATEGY_REPLACE_BACKGROUND, combo_source)
        return request.with_strategy(STRATEGY_TEXT_TO_IMAGE3, None)

    clean_source = _reusable_source_candidate(request)
    if clean_source:
        return request.with_strategy(STRATEGY_REPLACE_BACKGROUND, clean_source)

    reference = _reference_candidate(request)
    if reference:
        return request.with_strategy(STRATEGY_REFERENCE_REDRAW, reference)

    return request.with_strategy(STRATEGY_TEXT_TO_IMAGE3, None)


def execute_generation_request(request: GenerationRequest, provider: GenerationProvider) -> GenerationResult:
    routed = request if request.source_strategy != "auto" else select_generation_request(request)
    if routed.source_strategy != STRATEGY_REUSE and not provider.configured:
        return GenerationResult(
            dish=routed.dish,
            kind=routed.kind,
            status=STATUS_QUEUED,
            source_strategy=STRATEGY_WAITING_FOR_PROVIDER,
            action="WaitingForProvider",
            provider_error=WAITING_FOR_PROVIDER_ERROR,
            retryable=True,
            refund_required=False,
            reason=STRATEGY_WAITING_FOR_PROVIDER,
        )
    try:
        if routed.source_strategy == STRATEGY_REUSE:
            detail = provider.reuse(routed)
        elif routed.source_strategy == STRATEGY_REPLACE_BACKGROUND:
            detail = provider.replace_background(routed)
        elif routed.source_strategy == STRATEGY_REFERENCE_REDRAW:
            detail = provider.reference_redraw(routed)
        elif routed.source_strategy in {STRATEGY_TEXT_TO_IMAGE3, STRATEGY_TEXT_TO_IMAGE_LITE}:
            detail = provider.text_to_image(routed)
        else:
            raise RuntimeError(f"Unsupported generation strategy: {routed.source_strategy}")
    except Exception as exc:
        return failed_result(routed, exc)

    action = str(detail.get("action") or _action_for_strategy(routed.source_strategy))
    strategy = _strategy_after_provider(routed.source_strategy, detail)
    status = _status_after_provider(routed.source_strategy, detail)
    provider_name = str(detail.get("provider") or (PROVIDER_LIBRARY if routed.source_strategy == STRATEGY_REUSE else PROVIDER_TENCENT))
    return GenerationResult(
        dish=routed.dish,
        kind=routed.kind,
        status=status,
        source_strategy=strategy,
        provider=provider_name,
        action=action,
        prompt_type=str(detail.get("promptType") or prompt_type_for_strategy(strategy, routed.kind)),
        candidate=detail.get("candidate") if isinstance(detail.get("candidate"), dict) else None,
        path=str(detail.get("path") or ""),
        provider_error=str(detail.get("provider_error") or detail.get("providerError") or "") or None,
        retryable=bool(detail.get("retryable")),
        refund_required=bool(detail.get("refund_required") if "refund_required" in detail else detail.get("refundRequired")),
        reason=str(detail.get("reason") or "") or None,
        metadata={key: value for key, value in detail.items() if key not in {"candidate", "path", "provider", "action", "promptType", "reason"}},
    )


def failed_result(request: GenerationRequest, exc: Exception) -> GenerationResult:
    message = str(exc)[:1000] or exc.__class__.__name__
    return GenerationResult(
        dish=request.dish,
        kind=request.kind,
        status=STATUS_FAILED,
        source_strategy=request.source_strategy,
        provider=PROVIDER_TENCENT,
        action=_action_for_strategy(request.source_strategy),
        prompt_type=prompt_type_for_strategy(request.source_strategy, request.kind),
        provider_error=message,
        retryable=is_retryable_provider_error(message),
        refund_required=True,
        reason="provider_error",
    )


def is_retryable_provider_error(message: str) -> bool:
    lowered = str(message or "").lower()
    permanent_markers = ("invalidparameter", "illegal", "敏感", "审核", "policy", "not found", "公网 url")
    if any(marker in lowered for marker in permanent_markers):
        return False
    retry_markers = (
        "timeout",
        "timed out",
        "temporarily",
        "resourceinsufficient",
        "resource insufficient",
        "quota",
        "rate",
        "throttl",
        "http 429",
        "http 5",
        "资源不足",
        "超时",
        "限频",
        "额度",
    )
    if any(marker in lowered for marker in retry_markers):
        return True
    return True


def provider_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean in PROVIDER_STATUSES:
        return clean
    if clean in {"reused", "cached", "completed", "skipped"}:
        return STATUS_SUCCEEDED
    if clean == "fallback":
        return STATUS_PARTIAL
    if clean in {"pending", "waiting", "limited", STRATEGY_WAITING_FOR_PROVIDER}:
        return STATUS_QUEUED
    if clean in {"partially_failed"}:
        return STATUS_PARTIAL
    if clean in {"error"}:
        return STATUS_FAILED
    return STATUS_FAILED if clean else STATUS_QUEUED


def prompt_for_generation(
    row: dict[str, Any],
    style_id: str,
    quality: str | None = "standard",
    prompt_type: str = "text_to_image",
    *,
    style_prompt: Callable[[str], str] | None = None,
) -> str:
    style = style_prompt(style_id) if style_prompt else STYLE_PROMPT_FALLBACK
    detail = "高清真实餐饮商业摄影，菜品细节清楚，主体完整居中"
    if normalize_quality(quality) == QUALITY_PREMIUM:
        detail += "，专业布光，食材纹理精细，成图更有质感"
    dish = str(row.get("name") or row.get("dish") or row.get("dishName") or "外卖菜品")
    kind = normalize_kind(row.get("kind"))
    safe_area = _watermark_safe_area(row)
    forbidden = "不要出现任何文字、价格、非用户指定logo、水印、品牌名、人物、手、包装袋，不要裁切菜品主体。"
    crop = "适合美团/饿了么/京东外卖平台裁切，四周保留干净安全边距，主体占画面约70%。"
    common = (
        f"外卖主图，外卖平台主图，干净统一背景，主体完整，背景必须跟所选背景一致：{style}。"
        f"{detail}。{crop}{safe_area}{forbidden}"
    )
    if prompt_type == "reuse":
        return _clip_prompt(f"A 同菜同风格：图库已有「{dish}」且背景风格一致，直接使用原图，不调用模型生成。", 160)
    if prompt_type == "replace_background":
        return _clip_prompt(
            _short_replace_prompt(dish, style, watermark=bool(row.get("watermark"))),
            240,
        )
    if prompt_type in {"reference_redraw", "watermark_redraw", "debrand_redraw"}:
        return _clip_prompt(
            _short_replace_prompt(dish, style, watermark=bool(row.get("watermark")), debrand=True),
            240,
        )
    if prompt_type == "combo_replace_background":
        return _clip_prompt(
            f"B 同菜不同背景套餐/组合换背景：保留参考图中「{dish}」全部菜品、份量、餐具、摆盘、相对位置和主体比例，"
            f"包含：{row_components_text(row)}。必须把原背景完整替换为{style}，不要保留原桌面、墙面、场景、杂物。"
            "真实外卖平台套餐主图，主体完整居中，多菜品协调同框，无文字水印logo价格。",
            320,
        )
    if kind == KIND_COMBO or prompt_type == "combo":
        return _clip_prompt(
            f"C 图库没有该套餐/组合，按菜品名生成新套餐：「{dish}」，套餐组合外卖主图，包含：{row_components_text(row)}。"
            f"{common}多菜品协调摆放，主次清楚，所有组件真实同框，不要有单品图拼贴痕迹。"
        )
    return _clip_prompt(
        f"C 图库没有该菜，按菜品名生成新菜：「{dish}」菜品单品图，纯文生图。"
        f"{common}菜品自然可食用，画面干净，不生成菜单页、海报、包装或插画。"
    )


def prompt_for_style_background(
    style_id: str,
    *,
    style_prompt: Callable[[str], str] | None = None,
    variant_prompt: str = "",
) -> str:
    style = style_prompt(style_id) if style_prompt else STYLE_PROMPT_FALLBACK
    variant = f"{variant_prompt}。" if variant_prompt else ""
    return _clip_prompt(
        "背景生成任务：生成一张外卖菜品主图背景风格样图。"
        f"背景风格：{style}。{variant}"
        "画面中可以有一份普通中式菜品作为尺度参考，但重点展示背景、桌面、光影和色调。"
        "必须和其他背景方案明显不同，干净高级，主体完整居中，适合平台裁切。"
        "四周保留安全边距，右下角留出后续水印安全区。"
        "不要出现文字、价格、logo、水印、品牌名、人物、手、包装袋。"
    )


def _short_replace_prompt(dish: str, style: str, *, watermark: bool = False, debrand: bool = False) -> str:
    style_text = _clip_prompt(style, 34)
    dish_text = _clip_prompt(dish or "菜品", 22)
    safe = "右下角留水印安全区，" if watermark else ""
    if debrand:
        return (
            f"B 同菜不同背景去品牌水印重绘：保持菜品种类、菜量、餐具、摆盘和主体比例，菜品为{dish_text}。"
            f"必须把原背景完整替换为{style_text}，不要保留原桌面、墙面、场景、杂物。"
            f"主体完整居中，{safe}生成干净可交付成图，无文字水印logo价格。"
        )
    return (
        f"B 同菜不同背景换背景：保留{dish_text}主体、菜量、餐具、摆盘和主体比例，"
        f"必须把原背景完整替换为{style_text}，不要保留原桌面、墙面、场景、杂物。"
        f"真实外卖主图，主体完整居中，{safe}干净背景，无文字水印logo价格。"
    )


def row_components_text(row: dict[str, Any]) -> str:
    components = [str(value).strip() for value in row.get("components") or [] if str(value).strip()]
    if not components:
        for component in row.get("componentMatches") or []:
            name = str(component.get("name") or component.get("dish") or "").strip()
            if name:
                components.append(name)
    return "、".join(components[:8]) if components else str(row.get("name") or "套餐组合")


def prompt_type_for_strategy(strategy: str, kind: str) -> str:
    if strategy == STRATEGY_REUSE:
        return "reuse"
    if strategy == STRATEGY_REPLACE_BACKGROUND:
        return "combo_replace_background" if kind == KIND_COMBO else "replace_background"
    if strategy == STRATEGY_REFERENCE_REDRAW:
        return "watermark_redraw"
    if kind == KIND_COMBO:
        return "combo"
    return "text_to_image"


def candidate_has_brand_watermark(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    if candidate.get("has_brand_watermark") or candidate.get("hasBrandWatermark"):
        return True
    if candidate.get("reusable") is False:
        return True
    source = " ".join(str(candidate.get(key) or "") for key in ("source", "path", "url", "reviewReason", "reviewReasons"))
    return any(marker.lower() in source.lower() for marker in WATERMARK_MARKERS)


def candidate_is_model_output(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    provider = str(candidate.get("aiProvider") or candidate.get("generationProvider") or "")
    source = str(candidate.get("source") or "")
    status = str(candidate.get("generationStatus") or "").lower()
    return bool(
        provider == PROVIDER_TENCENT
        or source.startswith("tencent")
        or (candidate.get("generated") and status in {"succeeded", "cached"})
    )


def candidate_kind(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return KIND_OTHER
    text = " ".join(str(candidate.get(key) or "") for key in ("kind", "dishName", "name", "path", "url"))
    return normalize_kind(text)


def _same_style_reusable_candidate(request: GenerationRequest) -> dict[str, Any] | None:
    for candidate in request.candidates:
        if not candidate.get("reusable", True) or candidate_has_brand_watermark(candidate):
            continue
        if str(candidate.get("styleId") or "") != request.style:
            continue
        if request.kind == KIND_COMBO and candidate_kind(candidate) != KIND_COMBO:
            continue
        return candidate
    return None


def _reusable_source_candidate(request: GenerationRequest) -> dict[str, Any] | None:
    for candidate in request.candidates:
        if candidate_is_model_output(candidate):
            continue
        if not candidate.get("reusable", True) or candidate_has_brand_watermark(candidate):
            continue
        if request.kind == KIND_COMBO and candidate_kind(candidate) != KIND_COMBO:
            continue
        return candidate
    return None


def _reference_candidate(request: GenerationRequest) -> dict[str, Any] | None:
    for candidate in request.candidates:
        if candidate_has_brand_watermark(candidate) or candidate.get("path") or candidate.get("url"):
            return candidate
    return None


def _combo_reference_candidate(request: GenerationRequest) -> dict[str, Any] | None:
    for candidate in request.candidates:
        if candidate_kind(candidate) == KIND_COMBO:
            return candidate
    return None


def _action_for_strategy(strategy: str) -> str:
    return {
        STRATEGY_REUSE: "Reuse",
        STRATEGY_REPLACE_BACKGROUND: "ReplaceBackground",
        STRATEGY_REFERENCE_REDRAW: "ReferenceRedraw",
        STRATEGY_TEXT_TO_IMAGE3: "SubmitTextToImageJob",
        STRATEGY_TEXT_TO_IMAGE_LITE: "TextToImageLite",
    }.get(strategy, "Generate")


def _strategy_after_provider(strategy: str, detail: dict[str, Any]) -> str:
    action = str(detail.get("action") or "")
    if action == "TextToImageLite" or detail.get("fallbackFrom") == "SubmitTextToImageJob":
        return STRATEGY_TEXT_TO_IMAGE_LITE
    return strategy


def _status_after_provider(strategy: str, detail: dict[str, Any]) -> str:
    raw_status = str(detail.get("status") or "").strip().lower()
    if raw_status in PROVIDER_STATUSES:
        return raw_status
    if strategy == STRATEGY_REUSE:
        return STATUS_REUSED
    return STATUS_SUCCEEDED


def _watermark_safe_area(row: dict[str, Any]) -> str:
    watermark = row.get("watermark")
    if isinstance(watermark, dict) and watermark.get("enabled"):
        return "右下角保留干净安全区，便于后续添加用户指定品牌水印；"
    return "画面中不主动生成任何品牌水印；"


def _clip_prompt(value: str, limit: int = PROMPT_LIMIT) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def output_path_text(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value or "")
