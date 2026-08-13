from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal, Mapping, Sequence

from background_design_contracts import (
    CATEGORY_BACKGROUND_DIRECTIONS,
    EMPTY_SET_STYLE_DIRECTIONS,
    STYLE_BACKGROUND_DIRECTIONS,
    TOP_DOWN_PRODUCT_CATEGORIES,
    UPRIGHT_PRODUCT_CATEGORIES,
)
from matching_engine import normalize_dish


COMPILER_VERSION = "product-image-compiler.v4"
SCENE_CONTRACT_VERSION = "background-scene-contract.v2"
LEGACY_BACKGROUND_PROMPT_VERSION = "style-background.v11"
CURRENT_BACKGROUND_PROMPT_VERSION = "style-background.v12"
BENCHMARKED_BACKGROUND_PROMPT_VERSION = "style-background.v13"
EMPTY_SET_BACKGROUND_PROMPT_VERSION = "style-background.v14"
MAX_COMPILED_PROMPT_CHARS = 900


class PromptCompilationError(ValueError):
    pass


@dataclass(frozen=True)
class CameraSpec:
    pitch_degrees: int
    lens_mm: int
    yaw_degrees: int = 0
    roll_degrees: int = 0


@dataclass(frozen=True)
class SupportPlaneSpec:
    center_x: float
    center_y: float
    safe_left: float
    safe_top: float
    safe_width: float
    safe_height: float
    max_subject_width_ratio: float
    max_subject_height_ratio: float


@dataclass(frozen=True)
class LightingSpec:
    direction: str
    source: str
    color_temperature_k: int


@dataclass(frozen=True)
class PlacementSpec:
    center_x: float
    center_y: float
    max_subject_width_ratio: float
    max_subject_height_ratio: float
    shadow_offset_x_ratio: float
    shadow_offset_y_ratio: float
    shadow_blur_ratio: float
    shadow_opacity: float

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackgroundSceneContract:
    category_id: str
    style_id: str
    style_name: str
    scene_type: str
    scene_description: str
    surface_description: str
    camera: CameraSpec
    support: SupportPlaneSpec
    lighting: LightingSpec
    placement: PlacementSpec
    background_prompt_version: str = CURRENT_BACKGROUND_PROMPT_VERSION
    asset_id: str = ""
    asset_sha256: str = ""
    version: str = SCENE_CONTRACT_VERSION

    def basis_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "categoryId": self.category_id,
            "styleId": self.style_id,
            "styleName": self.style_name,
            "sceneType": self.scene_type,
            "sceneDescription": self.scene_description,
            "surfaceDescription": self.surface_description,
            "camera": asdict(self.camera),
            "support": asdict(self.support),
            "lighting": asdict(self.lighting),
            "placement": self.placement.payload(),
            "backgroundPromptVersion": self.background_prompt_version,
            "assetId": self.asset_id,
            "assetSha256": self.asset_sha256,
        }

    @property
    def contract_sha256(self) -> str:
        return _digest(self.basis_payload())

    def payload(self) -> dict[str, Any]:
        return {
            **self.basis_payload(),
            "contractSha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class DishSpec:
    raw_name: str
    resolved_name: str
    visual_name: str
    kind: str
    components: tuple[str, ...]
    required_components: tuple[str, ...]
    choice_groups: tuple[Mapping[str, Any], ...]
    flavor_modifiers: tuple[str, ...]
    selected_choices: tuple[str, ...]
    rejected_choices: tuple[str, ...]
    container: str
    semantic_requirements: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "rawName": self.raw_name,
            "resolvedName": self.resolved_name,
            "visualName": self.visual_name,
            "kind": self.kind,
            "components": list(self.components),
            "requiredComponents": list(self.required_components),
            "choiceGroups": [dict(group) for group in self.choice_groups],
            "flavorModifiers": list(self.flavor_modifiers),
            "selectedChoices": list(self.selected_choices),
            "rejectedChoices": list(self.rejected_choices),
            "container": self.container,
            "semanticRequirements": list(self.semantic_requirements),
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    model: str
    supports_seed: bool = True
    supports_revise: bool = True
    supports_negative_prompt: bool = False
    max_prompt_chars: int = MAX_COMPILED_PROMPT_CHARS


@dataclass(frozen=True)
class CompiledGeneration:
    prompt: str
    negative_prompt: str | None
    provider_payload: dict[str, Any]
    seed: int
    compile_digest: str
    prompt_sha256: str
    provider_payload_sha256: str
    scene_contract_sha256: str
    placement: PlacementSpec
    dish: DishSpec
    audit: dict[str, Any]


@dataclass(frozen=True)
class _SceneTemplate:
    style_name: str
    scene_type: str
    scene_description: str
    surface_description: str
    camera: CameraSpec
    support: SupportPlaneSpec
    lighting: LightingSpec
    placement: PlacementSpec


_SCENE_TEMPLATES: dict[str, _SceneTemplate] = {
    "style-1": _SceneTemplate(
        "暖象牙纯色无缝棚拍",
        "seamless-surface",
        "高调暖象牙色商业棚拍，满幅连续承托面，无墙桌分界和可见边缘",
        "单一暖象牙色哑光细颗粒微水泥",
        CameraSpec(62, 55),
        SupportPlaneSpec(0.50, 0.55, 0.10, 0.16, 0.80, 0.74, 0.82, 0.74),
        LightingSpec("左上至右下", "大型柔光箱", 4300),
        PlacementSpec(0.50, 0.55, 0.82, 0.74, 0.010, 0.012, 0.018, 0.16),
    ),
    "style-2": _SceneTemplate(
        "陶土红纯色食欲棚拍",
        "seamless-surface",
        "温暖陶土红商业棚拍，满幅连续承托面，无洞穴、墙面或可见边缘",
        "单一低饱和陶土红哑光矿物涂层",
        CameraSpec(58, 50),
        SupportPlaneSpec(0.50, 0.56, 0.11, 0.17, 0.78, 0.72, 0.80, 0.72),
        LightingSpec("右上至左下", "柔和侧顶光加弱填充", 4500),
        PlacementSpec(0.50, 0.56, 0.80, 0.72, -0.010, 0.012, 0.018, 0.17),
    ),
    "style-3": _SceneTemplate(
        "晨光浅洞石餐桌",
        "full-frame-tabletop",
        "明亮自然窗边餐桌，桌面覆盖完整画幅，桌沿、厚度和墙面都在画外",
        "一整块纹理连续的浅色洞石桌面",
        CameraSpec(56, 52),
        SupportPlaneSpec(0.52, 0.56, 0.12, 0.18, 0.78, 0.72, 0.80, 0.72),
        LightingSpec("左上窗光", "扩散日光", 5000),
        PlacementSpec(0.52, 0.56, 0.80, 0.72, 0.010, 0.013, 0.017, 0.17),
    ),
    "style-4": _SceneTemplate(
        "暖胡桃木家常餐桌",
        "full-frame-tabletop",
        "温暖真实的家庭用餐环境，桌面覆盖完整画幅，桌板轮廓和墙面都在画外",
        "一整块连续胡桃木纹餐桌",
        CameraSpec(54, 50),
        SupportPlaneSpec(0.48, 0.57, 0.10, 0.19, 0.80, 0.71, 0.82, 0.71),
        LightingSpec("左侧至右下", "自然窗光加顶部填充", 4600),
        PlacementSpec(0.48, 0.57, 0.82, 0.71, 0.011, 0.013, 0.018, 0.18),
    ),
    "style-5": _SceneTemplate(
        "鼠尾草绿现代餐桌",
        "full-frame-tabletop",
        "清爽现代餐饮摄影，桌面满幅连续，无墙桌交界、拼色区或中央石板",
        "单一低饱和鼠尾草绿哑光陶瓷质感桌面",
        CameraSpec(60, 55),
        SupportPlaneSpec(0.50, 0.54, 0.11, 0.15, 0.79, 0.75, 0.81, 0.75),
        LightingSpec("左上均匀顶光", "大型漫射天幕", 5200),
        PlacementSpec(0.50, 0.54, 0.81, 0.75, 0.008, 0.011, 0.017, 0.15),
    ),
    "style-6": _SceneTemplate(
        "炭灰餐厅质感桌面",
        "full-frame-tabletop",
        "克制高级的晚餐厅摄影，桌面覆盖完整画幅，无桌沿、墙面和舞台感",
        "一整块炭灰细纹板岩桌面",
        CameraSpec(52, 55),
        SupportPlaneSpec(0.51, 0.57, 0.11, 0.19, 0.79, 0.70, 0.81, 0.70),
        LightingSpec("右上至左下", "暖柔光窗加中性填充", 4200),
        PlacementSpec(0.51, 0.57, 0.81, 0.70, -0.011, 0.014, 0.019, 0.19),
    ),
}


_LEGACY_SCENE_TEMPLATES: dict[str, _SceneTemplate] = {
    style_id: _SceneTemplate(
        style_name=template.style_name,
        scene_type=template.scene_type,
        scene_description=template.scene_description,
        surface_description=template.surface_description,
        camera=CameraSpec(25, template.camera.lens_mm),
        support=template.support,
        lighting=template.lighting,
        placement=template.placement,
    )
    for style_id, template in _SCENE_TEMPLATES.items()
}


def _benchmarked_scene_template(
    style_id: str,
    category_id: str,
    *,
    prompt_version: str,
) -> _SceneTemplate:
    try:
        base = _SCENE_TEMPLATES[style_id]
        direction = CATEGORY_BACKGROUND_DIRECTIONS[category_id]
        empty_style = (
            EMPTY_SET_STYLE_DIRECTIONS[style_id]
            if prompt_version == EMPTY_SET_BACKGROUND_PROMPT_VERSION
            else None
        )
        if empty_style is None:
            style_name, surface_field, geometry = STYLE_BACKGROUND_DIRECTIONS[
                style_id
            ]
        else:
            style_name = empty_style.name
            surface_field = empty_style.surface_field
            geometry = (
                "单一平整桌面从四边延伸画外，无墙面、地平线、桌沿、"
                "厚度、桌腿、桌下空间或第二层平面"
            )
    except KeyError as exc:
        raise PromptCompilationError(
            f"unknown benchmarked background contract: {category_id}/{style_id}"
        ) from exc

    if category_id in UPRIGHT_PRODUCT_CATEGORIES:
        pitch = {
            "style-1": 15,
            "style-2": 15,
            "style-3": 18,
            "style-4": 18,
            "style-5": 16,
            "style-6": 14,
        }[style_id]
        support = SupportPlaneSpec(
            0.50, 0.58, 0.20, 0.10, 0.60, 0.80, 0.60, 0.80
        )
        placement = PlacementSpec(
            0.50, 0.58, 0.58, 0.80, 0.008, 0.012, 0.017, 0.15
        )
    elif category_id in TOP_DOWN_PRODUCT_CATEGORIES:
        pitch = {
            "style-1": 60,
            "style-2": 58,
            "style-3": 62,
            "style-4": 58,
            "style-5": 60,
            "style-6": 56,
        }[style_id]
        support = SupportPlaneSpec(
            0.50, 0.53, 0.07, 0.12, 0.86, 0.78, 0.88, 0.78
        )
        placement = PlacementSpec(
            0.50, 0.53, 0.88, 0.78, 0.009, 0.011, 0.017, 0.15
        )
    else:
        pitch = {
            "style-1": 42,
            "style-2": 40,
            "style-3": 46,
            "style-4": 42,
            "style-5": 44,
            "style-6": 38,
        }[style_id]
        support = SupportPlaneSpec(
            0.50, 0.55, 0.08, 0.14, 0.84, 0.76, 0.84, 0.76
        )
        placement = PlacementSpec(
            0.50, 0.55, 0.84, 0.76, 0.010, 0.012, 0.018, 0.16
        )

    surface = str(getattr(direction, surface_field))
    if empty_style is not None:
        pitch = empty_style.camera_pitch(category_id)
        scene_type = empty_style.scene_type
        lens_mm = empty_style.lens_mm
        light_direction = empty_style.light_direction
        color_temperature_k = empty_style.color_temperature_k
    else:
        scene_type = base.scene_type
        lens_mm = base.camera.lens_mm
        light_direction = base.lighting.direction
        color_temperature_k = base.lighting.color_temperature_k
    return replace(
        base,
        style_name=style_name,
        scene_type=scene_type,
        scene_description=(
            f"原创{style_name}，{geometry}，中央承托区连续、真实、没有舞台感"
        ),
        surface_description=surface,
        camera=CameraSpec(pitch, lens_mm),
        support=support,
        lighting=LightingSpec(
            light_direction,
            direction.lighting_mood,
            color_temperature_k,
        ),
        placement=placement,
    )


_CHOICE_COUNT_RE = re.compile(r"(?P<count>[二三四五六七八九十\d]+)选(?:一|1)")
_CHOICE_SEPARATOR_RE = re.compile(
    r"(?:[/／、,，|丨｜]|或者|或|(?i:(?<![A-Za-z])or(?![A-Za-z])))"
)
_CHOICE_BOUNDARY_RE = re.compile(r"[+＋;；:：【】\[\]()（）]")
_CHOICE_COUNTS = {
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_MARKETING_TERMS_RE = re.compile(
    r"(?:超值|爆款|招牌|必点|人气|特惠|限时|推荐|店长推荐|镇店|新品|豪华|霸气)"
)


def scene_contract_for(
    style_id: str,
    category_id: str,
    *,
    asset_id: str = "",
    asset_sha256: str = "",
    prompt_version: str = CURRENT_BACKGROUND_PROMPT_VERSION,
) -> BackgroundSceneContract:
    resolved_prompt_version = str(prompt_version or "").strip()
    if resolved_prompt_version == CURRENT_BACKGROUND_PROMPT_VERSION:
        templates = _SCENE_TEMPLATES
    elif resolved_prompt_version == LEGACY_BACKGROUND_PROMPT_VERSION:
        templates = _LEGACY_SCENE_TEMPLATES
    elif resolved_prompt_version in {
        BENCHMARKED_BACKGROUND_PROMPT_VERSION,
        EMPTY_SET_BACKGROUND_PROMPT_VERSION,
    }:
        templates = {
            str(style_id).strip(): _benchmarked_scene_template(
                str(style_id).strip(),
                str(category_id or "unknown").strip() or "unknown",
                prompt_version=resolved_prompt_version,
            )
        }
    else:
        raise PromptCompilationError(
            f"unsupported background prompt version: {prompt_version}"
        )
    try:
        template = templates[str(style_id).strip()]
    except KeyError as exc:
        raise PromptCompilationError(f"unknown background style: {style_id}") from exc
    digest = str(asset_sha256 or "").strip().lower()
    if digest and not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise PromptCompilationError("background asset sha256 is invalid")
    return BackgroundSceneContract(
        category_id=str(category_id or "unknown").strip() or "unknown",
        style_id=str(style_id).strip(),
        style_name=template.style_name,
        scene_type=template.scene_type,
        scene_description=template.scene_description,
        surface_description=template.surface_description,
        camera=template.camera,
        support=template.support,
        lighting=template.lighting,
        placement=template.placement,
        background_prompt_version=resolved_prompt_version,
        asset_id=str(asset_id or "").strip(),
        asset_sha256=digest,
    )


def scene_contract_from_payload(
    payload: Mapping[str, Any],
    *,
    expected_asset_id: str = "",
    expected_asset_sha256: str = "",
) -> BackgroundSceneContract:
    if not isinstance(payload, Mapping):
        raise PromptCompilationError("background scene contract must be an object")
    contract = scene_contract_for(
        str(payload.get("styleId") or ""),
        str(payload.get("categoryId") or "unknown"),
        asset_id=str(payload.get("assetId") or ""),
        asset_sha256=str(payload.get("assetSha256") or ""),
        prompt_version=str(payload.get("backgroundPromptVersion") or ""),
    )
    if payload.get("version") != SCENE_CONTRACT_VERSION:
        raise PromptCompilationError("background scene contract version is invalid")
    expected_payload = contract.payload()
    if dict(payload) != expected_payload:
        raise PromptCompilationError("background scene contract does not match its style template")
    if expected_asset_id and contract.asset_id != expected_asset_id:
        raise PromptCompilationError("background scene contract asset id mismatch")
    if expected_asset_sha256 and contract.asset_sha256 != expected_asset_sha256:
        raise PromptCompilationError("background scene contract asset sha256 mismatch")
    return contract


def resolve_explicit_choices(
    dish_name: str,
) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    normalized = unicodedata.normalize("NFKC", str(dish_name or ""))
    choices: list[tuple[str, tuple[str, ...]]] = []
    replacements: list[tuple[int, int, str]] = []
    for count_match in _CHOICE_COUNT_RE.finditer(normalized):
        count_text = count_match.group("count")
        count = int(count_text) if count_text.isdigit() else _CHOICE_COUNTS.get(count_text, 0)
        if count < 2:
            continue
        preceding = normalized[: count_match.start()]
        boundary = None
        for candidate in _CHOICE_BOUNDARY_RE.finditer(preceding):
            boundary = candidate
        segment_start = boundary.end() if boundary is not None else 0
        segment = normalized[segment_start : count_match.start()]
        parts: list[tuple[str, int]] = []
        cursor = 0
        for separator in _CHOICE_SEPARATOR_RE.finditer(segment):
            raw = segment[cursor : separator.start()]
            stripped = raw.strip()
            if stripped:
                parts.append((stripped, cursor + len(raw) - len(raw.lstrip())))
            cursor = separator.end()
        raw = segment[cursor:]
        stripped = raw.strip()
        if stripped:
            parts.append((stripped, cursor + len(raw) - len(raw.lstrip())))
        if len(parts) < count:
            continue
        selected_parts = parts[-count:]
        options = tuple(part[0] for part in selected_parts)
        selected = options[0]
        choice_start = segment_start + selected_parts[0][1]
        choices.append((selected, options))
        replacements.append((choice_start, count_match.end(), selected))

    resolved = normalized
    for start, end, selected in reversed(replacements):
        resolved = resolved[:start] + selected + resolved[end:]
    return resolved, tuple(choices)


def generation_components(
    row: Mapping[str, Any],
    choices: Sequence[tuple[str, tuple[str, ...]]],
) -> list[str]:
    excluded = {
        normalize_dish(option)
        for _selected, options in choices
        for option in options[1:]
        if normalize_dish(option)
    }
    values: list[str] = []
    seen: set[str] = set()
    for raw_value in row.get("components") or []:
        value = str(raw_value).strip()
        if not value:
            continue
        value, _component_choices = resolve_explicit_choices(value)
        value = re.sub(
            r"\s*[二三四五六七八九十\d]+选(?:一|1)\s*$",
            "",
            unicodedata.normalize("NFKC", value),
        )
        norm = normalize_dish(value)
        if not norm or norm in excluded or norm in seen:
            continue
        seen.add(norm)
        values.append(value)
    return values


def _structured_choice_groups(row: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_groups = row.get("choiceGroups")
    if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, (str, bytes)):
        return ()
    groups: list[dict[str, Any]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, Mapping):
            continue
        options = tuple(
            dict.fromkeys(
                str(option).strip()
                for option in raw_group.get("options") or []
                if str(option).strip()
            )
        )
        if not options:
            continue
        selected = str(raw_group.get("selected") or options[0]).strip()
        selected = next(
            (
                option
                for option in options
                if normalize_dish(option) == normalize_dish(selected)
            ),
            options[0],
        )
        groups.append(
            {
                "name": str(raw_group.get("name") or "自选项").strip(),
                "role": str(raw_group.get("role") or "side").strip(),
                "choose": 1,
                "options": list(options),
                "selected": selected,
            }
        )
    return tuple(groups)


def _deduped_values(values: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value).strip()
        norm = normalize_dish(value)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(value)
    return tuple(out)


def _is_drink_component(value: str) -> bool:
    normalized = normalize_dish(value)
    return any(
        term in normalized
        for term in (
            "饮品",
            "饮料",
            "奶茶",
            "果汁",
            "咖啡",
            "可乐",
            "雪碧",
            "冰红茶",
            "绿茶",
            "矿泉水",
            "纯净水",
            "豆浆",
            "酸梅汤",
        )
    )


def analyze_dish(row: Mapping[str, Any], menu_taxonomy_id: str) -> DishSpec:
    raw_name = str(row.get("name") or "外卖菜品").strip()
    resolved_name, legacy_choices = resolve_explicit_choices(raw_name)
    choice_groups = _structured_choice_groups(row)
    if choice_groups:
        selected_choices = _deduped_values(
            tuple(str(group["selected"]) for group in choice_groups)
        )
        selected_norms = {normalize_dish(value) for value in selected_choices}
        rejected_choices = _deduped_values(
            tuple(
                str(option)
                for group in choice_groups
                for option in group["options"]
                if normalize_dish(option) not in selected_norms
            )
        )
    else:
        selected_choices = _deduped_values(
            tuple(choice[0] for choice in legacy_choices)
        )
        selected_norms = {normalize_dish(value) for value in selected_choices}
        rejected_choices = _deduped_values(
            tuple(
                option
                for _selected, options in legacy_choices
                for option in options[1:]
                if normalize_dish(option) not in selected_norms
            )
        )
    raw_required = row.get("requiredComponents")
    if isinstance(raw_required, Sequence) and not isinstance(raw_required, (str, bytes)):
        required_components = _deduped_values(tuple(str(value) for value in raw_required))
    else:
        required_components = _deduped_values(
            tuple(generation_components(row, legacy_choices))
        )
    components = _deduped_values((*required_components, *selected_choices))
    raw_flavors = row.get("flavorModifiers")
    flavor_modifiers = (
        _deduped_values(tuple(str(value) for value in raw_flavors))
        if isinstance(raw_flavors, Sequence) and not isinstance(raw_flavors, (str, bytes))
        else ()
    )
    kind = str(row.get("kind") or "菜品").strip() or "菜品"
    visual_source = resolved_name
    if choice_groups and components:
        visual_source = "+".join(components)
        if kind == "套餐/组合":
            visual_source += "套餐"
    visual_name = _MARKETING_TERMS_RE.sub("", visual_source)
    visual_name = re.sub(r"\s+", " ", visual_name).strip(" -_｜|") or resolved_name
    semantic_identity = visual_source if choice_groups else resolved_name
    semantic_source = " ".join((semantic_identity, *components))
    requirements: list[str] = []
    if menu_taxonomy_id in {"mixed_rice", "topped_rice"} or any(
        word in semantic_source for word in ("拌饭", "盖饭", "盖码饭", "烤肉饭")
    ):
        requirements.append(
            "这是中式外卖米饭餐，必须清楚出现白米饭，餐盘以白米饭和中式熟食为主体"
        )
    if "烤肉" in semantic_source:
        requirements.append("烤肉画成切片中式蜜汁猪肉")
    if "烤排" in semantic_source:
        requirements.append(
            "烤排画成全熟浅棕色中式黑椒无骨猪肉排，切成6片整齐排列，切面没有粉红色"
        )
    if "鸡排" in semantic_source:
        requirements.append("鸡排画成全熟金黄色完整鸡排")
    if "腿排" in semantic_source:
        requirements.append("腿排画成全熟金黄色去骨鸡腿排")
    if "猪排" in semantic_source:
        requirements.append("猪排画成全熟浅棕色中式猪排薄片")
    portion_match = re.search(r"([双三四])拼", resolved_name)
    if portion_match:
        count = {"双": 2, "三": 3, "四": 4}[portion_match.group(1)]
        requirements.append(f"{portion_match.group(1)}拼必须呈现{count}种不同肉类")
    if selected_choices:
        requirements.append(
            f"备选项已固定，本图只呈现{'、'.join(selected_choices)}，每个固定项只出现一份"
        )
    elif re.search(r"(?:[二三四五六七八九十\d]+选一|任选|自选|可选)", semantic_source):
        requirements.append("标注选一、任选或自选的配菜只出现其中一种")
    selected_drink_group = any(
        str(group.get("role") or "").strip().lower() == "drink"
        and str(group.get("selected") or "").strip()
        for group in choice_groups
    )
    if selected_drink_group or any(
        _is_drink_component(component) for component in components
    ):
        requirements.append("饮品只放一杯，杯身纯色无品牌无文字")
    if kind == "套餐/组合":
        requirements.append("非备选的套餐核心食材必须分别可辨，不得漏项或替换")
    return DishSpec(
        raw_name=raw_name,
        resolved_name=resolved_name,
        visual_name=visual_name,
        kind=kind,
        components=components,
        required_components=required_components,
        choice_groups=choice_groups,
        flavor_modifiers=flavor_modifiers,
        selected_choices=selected_choices,
        rejected_choices=rejected_choices,
        container=_infer_container(semantic_source, menu_taxonomy_id, kind),
        semantic_requirements=tuple(dict.fromkeys(requirements)),
    )


def compile_product_image(
    row: Mapping[str, Any],
    *,
    menu_taxonomy_id: str,
    background: BackgroundSceneContract,
    quality: str,
    mode: Literal["chroma_foreground", "reference", "replace"],
    provider: ProviderCapabilities | None = None,
) -> CompiledGeneration:
    if mode not in {"chroma_foreground", "reference", "replace"}:
        raise PromptCompilationError(f"unsupported generation mode: {mode}")
    capabilities = provider or ProviderCapabilities("hy-image-v3.0")
    dish = analyze_dish(row, menu_taxonomy_id)
    compile_basis = {
        "compilerVersion": COMPILER_VERSION,
        "dish": dish.payload(),
        "menuTaxonomyId": menu_taxonomy_id,
        "sceneContractSha256": background.contract_sha256,
        "quality": quality,
        "mode": mode,
        "providerModel": capabilities.model,
    }
    compile_digest = _digest(compile_basis)
    seed = int.from_bytes(bytes.fromhex(compile_digest)[:4], "big") or 1
    prompt = _compile_prompt(
        dish,
        background,
        quality=quality,
        mode=mode,
        max_chars=capabilities.max_prompt_chars,
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    negative_prompt = (
        "文字，数字，水印，logo，品牌，人物，手，畸形器皿，竖立餐盘，悬浮餐盘，"
        "错误透视，错误阴影，边框，小图，拼贴"
        if capabilities.supports_negative_prompt
        else None
    )
    provider_payload: dict[str, Any] = {
        "Prompt": prompt,
        "LogoAdd": 0,
    }
    if capabilities.supports_revise:
        provider_payload["Revise"] = 0
    if capabilities.supports_seed:
        provider_payload["Seed"] = seed
    if negative_prompt:
        provider_payload["NegativePrompt"] = negative_prompt
    provider_payload_sha256 = _digest(provider_payload)
    audit = _prompt_audit(
        prompt,
        dish,
        background,
        max_chars=capabilities.max_prompt_chars,
    )
    return CompiledGeneration(
        prompt=prompt,
        negative_prompt=negative_prompt,
        provider_payload=provider_payload,
        seed=seed,
        compile_digest=compile_digest,
        prompt_sha256=prompt_sha256,
        provider_payload_sha256=provider_payload_sha256,
        scene_contract_sha256=background.contract_sha256,
        placement=background.placement,
        dish=dish,
        audit=audit,
    )


def _compile_prompt(
    dish: DishSpec,
    background: BackgroundSceneContract,
    *,
    quality: str,
    mode: str,
    max_chars: int,
) -> str:
    components = "、".join(dish.components[:8])
    if dish.kind == "套餐/组合":
        generation_label = "套餐组合外卖主图，外卖平台主图"
    elif mode == "replace":
        generation_label = "仅替换背景的外卖平台主图"
    else:
        generation_label = "纯文生图，外卖平台主图"
    fixed_choice = (
        f"备选已固定为{'、'.join(dish.selected_choices)}，不得出现其他备选项。"
        if dish.selected_choices
        else ""
    )
    if mode == "chroma_foreground":
        scene_requirement = (
            f"{generation_label}，当前步骤只生成供程序抠取的菜品前景，主体必须完整。"
            f"严格生成“{dish.visual_name}”，类型为{dish.kind}，使用{dish.container}。"
            + (f"套餐构成：{components}。" if components and dish.kind == "套餐/组合" else "")
            + fixed_choice
            + (
                "背景必须是完全均匀的纯青色抠图幕布（RGB 0,255,255），"
                "不得画出所选背景或任何真实场景，没有桌面、墙面、地平线、渐变、"
                "反射或道具；青幕上不要生成投影。餐盘、餐盒、杯子、碗、托盘和"
                "其他器皿只能使用白色、黑色或暖中性色，绝不能使用青色、蓝绿色、"
                "湖蓝色、薄荷绿色或任何接近幕布的颜色，器皿边缘不得染上幕布颜色。"
            )
        )
    else:
        scene_requirement = (
            f"{generation_label}，主体完整，背景必须跟所选背景一致。"
            f"严格生成“{dish.visual_name}”，类型为{dish.kind}，使用{dish.container}。"
            + (f"套餐构成：{components}。" if components and dish.kind == "套餐/组合" else "")
            + fixed_choice
        )
    mandatory = [
        (
            "最高优先级：画面绝对不能包含汉字、字母、数字或其他可读符号；"
            "容器不要出现任何文字，所有容器必须纯色无印刷，"
            "不能有标签、品牌、logo、水印、价格、人物或手。"
        ),
        scene_requirement,
        "菜品语义：" + "；".join(dish.semantic_requirements) + "。"
        if dish.semantic_requirements
        else "菜名和实际食材必须一一对应，不得用相似但错误的菜品替代。",
        (
            f"机位必须继承所选背景：相机从水平面上方{background.camera.pitch_degrees}度俯拍，"
            f"使用约{background.camera.lens_mm}mm标准镜头；器皿底面与承托面平行，"
            "盘沿椭圆透视必须符合该俯角，器皿绝不能竖立、倾斜、悬浮或陷入桌面。"
        ),
        (
            f"主光从{background.lighting.direction}照射，使用{background.lighting.source}，"
            "菜品高光与阴影方向必须一致。"
        ),
        (
            f"外卖商品图大主体构图，主体宽度约{round(background.placement.max_subject_width_ratio * 100)}%，"
            "可见面积约46%至58%，完整清楚、真实有食欲，不使用小图、相框、边框或画中画。"
        ),
    ]
    if mode != "chroma_foreground":
        mandatory.append(
            f"背景必须遵循“{background.style_name}”：{background.scene_description}，"
            f"承托材质为{background.surface_description}，菜品自然落在同一平面。"
        )
    optional = [
        "菜品使用真实食材纹理、自然熟度和克制油润感，避免塑料质感。",
        "高清商业餐饮摄影，焦点落在主要食材，边缘清楚，颜色自然。"
        if quality == "premium"
        else "清晰真实的商业餐饮摄影，主要食材完整可辨。",
        "餐具结构必须合理，筷子、勺子、杯子和餐盒不得变形或重复。",
    ]
    return _fit_complete_segments(mandatory, optional, max_chars=max_chars)


def _fit_complete_segments(
    mandatory: Sequence[str],
    optional: Sequence[str],
    *,
    max_chars: int,
) -> str:
    prompt = "".join(segment for segment in mandatory if segment)
    if len(prompt) > max_chars:
        raise PromptCompilationError(
            f"mandatory prompt contract exceeds provider limit: {len(prompt)} > {max_chars}"
        )
    for segment in optional:
        if len(prompt) + len(segment) <= max_chars:
            prompt += segment
    return prompt


def _prompt_audit(
    prompt: str,
    dish: DishSpec,
    background: BackgroundSceneContract,
    *,
    max_chars: int,
) -> dict[str, Any]:
    checks = {
        "withinProviderLimit": len(prompt) <= max_chars,
        "resolvedDishPresent": dish.visual_name in prompt,
        "cameraInherited": (
            str(background.camera.pitch_degrees) in prompt
            and str(background.camera.lens_mm) in prompt
        ),
        "horizontalSupportRequired": "器皿底面与承托面平行" in prompt,
        "noReadableTextRequired": "画面绝对不能包含" in prompt,
        "rejectedChoicesAbsent": all(option not in prompt for option in dish.rejected_choices),
    }
    if not all(checks.values()):
        failed = ",".join(name for name, passed in checks.items() if not passed)
        raise PromptCompilationError(f"compiled prompt failed QA: {failed}")
    return {
        "passed": True,
        "checks": checks,
        "promptChars": len(prompt),
        "selectedChoices": list(dish.selected_choices),
        "rejectedChoices": list(dish.rejected_choices),
    }


def _infer_container(semantic_source: str, category_id: str, kind: str) -> str:
    if kind == "套餐/组合":
        return "一个水平放置的宽大低矮纯色分格餐盘"
    if any(word in semantic_source for word in ("奶茶", "果汁", "咖啡", "可乐", "饮品")):
        return "一只低矮纯色无字饮料杯"
    if any(word in semantic_source for word in ("汤", "粥", "面", "粉", "馄饨", "饺子")):
        return "一只宽口低矮陶瓷碗"
    if category_id in {"pizza"} or "披萨" in semantic_source:
        return "一个水平放置的完整圆形餐盘"
    if category_id in {"mixed_rice", "topped_rice", "fried_rice"} or "饭" in semantic_source:
        return "一个水平放置的宽口浅圆陶瓷餐盘"
    return "一个水平放置的低矮纯色餐盘"


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BENCHMARKED_BACKGROUND_PROMPT_VERSION",
    "EMPTY_SET_BACKGROUND_PROMPT_VERSION",
    "BackgroundSceneContract",
    "COMPILER_VERSION",
    "CompiledGeneration",
    "MAX_COMPILED_PROMPT_CHARS",
    "PlacementSpec",
    "PromptCompilationError",
    "ProviderCapabilities",
    "SCENE_CONTRACT_VERSION",
    "analyze_dish",
    "compile_product_image",
    "generation_components",
    "resolve_explicit_choices",
    "scene_contract_for",
    "scene_contract_from_payload",
]
