from __future__ import annotations

from collections import Counter
import re
import unicodedata
from typing import Any

import background_catalog
from matching_engine import (
    TAXONOMY_COMBO,
    TAXONOMY_LABELS,
    TAXONOMY_RULES,
    TAXONOMY_UNKNOWN,
    classify_taxonomy,
)


BACKGROUND_PROFILE_VERSION = "2026-08-01.v12"
MIXED_CATEGORY_ID = "mixed"
STYLE_IDS = background_catalog.STYLE_IDS

PURE_BACKGROUND_NEGATIVE_PROMPT = (
    "食物，菜品，饮料，水果，蔬菜，香草，食材，原料，植物，叶片，花，"
    "枝条，装饰物，道具，花瓶，玻璃器皿，餐具，餐盘，碗，杯子，餐盒，"
    "筷子，刀叉，布料，桌布，餐巾，纸张，木板，砧板，托盘，展示台，"
    "展台，底座，台座，方台，圆台，台阶，层板，垫板，边框，画框，"
    "可见桌沿，桌面厚度，第二层桌面，独立水平面，矩形留白区域，白色矩形，"
    "色卡，色块，几何块，"
    "文字，价格，菜单，logo，品牌名，水印，人物，手，低清晰度，"
    "模糊，畸变，拼贴，画中画，中央小图，背景虚化，暗角，强景深虚化，"
    "无来源阴影，悬浮阴影，不存在物体产生的投影"
)

STYLE_VARIANTS = tuple(slot.prompt for slot in background_catalog.STYLE_SLOTS)

BACKGROUND_SCENES = {
    "topped_rice": "盖饭盖码饭场景，暖白、赤陶与少量炭灰配色，平整耐热浅石材纹理",
    "mixed_rice": "炒饭拌饭场景，暖灰、琥珀与深棕配色，细腻哑光石材纹理",
    "porridge_soup_rice": "粥汤饭场景，象牙白、燕麦色与浅木色配色，温润低对比材质",
    "rice_noodles": "米粉米线场景，暖白、陶土红与青灰配色，清爽细石材纹理",
    "wheat_noodles": "面食场景，麦芽色、暖灰与深木色配色，利落耐热材质",
    "dumpling_wonton": "饺子馄饨场景，米白、竹木色与淡青灰配色，细腻温润纹理",
    "buns_dim_sum": "包子点心场景，奶油白、浅木与淡金配色，明净柔和材质",
    "chinese_wraps": "中式卷饼场景，麦黄、暖白与陶红配色，轻快细木纹材质",
    "malatang_maocai": "麻辣烫冒菜场景，朱红、炭黑与暖灰配色，耐热哑光石材纹理",
    "hotpot_skewers": "火锅串串场景，深红、黑灰与少量黄铜色配色，厚实耐热材质",
    "barbecue": "烧烤场景，炭灰、焦褐与暗红配色，粗细适中的深色石材纹理",
    "fried_chicken": "炸鸡场景，芥末黄、番茄红与暖白配色，年轻明快哑光材质",
    "burger_hotdog": "汉堡热狗场景，番茄红、芥末黄与深灰配色，现代快餐哑光材质",
    "pizza": "披萨场景，赤陶、奶油白与橄榄灰配色，温暖意式木石纹理",
    "sandwich_bagel": "三明治贝果场景，燕麦白、浅木与鼠尾草灰配色，明亮细腻材质",
    "light_food": "轻食沙拉场景，鼠尾草绿、暖白与浅灰配色，清新低饱和矿物材质",
    "pasta_steak": "意面牛排场景，象牙白、酒红与炭灰配色，精致哑光石材纹理",
    "japanese": "日料场景，暖灰、原木与墨黑配色，克制细木纹和哑光材质",
    "korean": "韩餐场景，暖白、石墨灰与克制朱红配色，现代细石材纹理",
    "southeast_asian": "东南亚菜场景，青绿、珊瑚橙与暖白配色，清爽热带感矿物纹理",
    "sichuan_hunan": "川湘菜场景，朱红、胡桃木与炭灰配色，热烈耐热木石纹理",
    "cantonese_roast": "粤式烧味场景，蜜糖棕、暖白与墨绿灰配色，明亮细石材纹理",
    "jiangzhe": "江浙菜场景，月白、黛青与浅木配色，雅致低对比矿物纹理",
    "northeast_chinese": "东北菜场景，暖木、砖红与深灰配色，厚实自然木石纹理",
    "northwest_xinjiang": "西北新疆菜场景，沙岩色、靛青与炭灰配色，粗粝但平整的石材纹理",
    "northern_lu": "北方鲁菜场景，枣红、暖白与深木色配色，端正沉稳木石纹理",
    "fujian_taiwan": "闽台菜场景，海盐白、浅青与原木色配色，清爽细腻材质",
    "home_stir_fry": "家常小炒场景，暖木、米白与陶红配色，真实温暖细木纹材质",
    "fish_seafood": "鱼海鲜场景，海盐白、浅蓝灰与冷石色配色，清凉细石材纹理",
    "beef_lamb_pot": "牛羊锅场景，焦糖棕、炭灰与暗红配色，厚重耐热哑光材质",
    "braised_cooked_food": "卤味熟食凉菜场景，卤褐、暗红与深木色配色，克制温暖木纹材质",
    "soup_stew": "汤羹炖品场景，米白、浅杏与灰褐配色，温润低对比材质",
    "steamed_claypot": "蒸菜煲仔场景，陶土、暖灰与深木色配色，耐热平整木石纹理",
    "milk_fruit_tea": "奶茶果茶场景，奶油白、淡桃与浅青绿配色，清透明亮哑光材质",
    "coffee_cocoa": "咖啡可可场景，可可棕、奶油白与石墨灰配色，现代细木石纹理",
    "bottled_drinks": "瓶装酒水饮料场景，冷白、金属灰与深蓝灰配色，简洁零售棚拍材质",
    "fresh_drinks": "鲜榨饮品场景，暖白、青柠绿与淡橙配色，清新明亮哑光材质",
    "dessert_bakery": "甜品烘焙场景，奶油白、浅粉与焦糖色配色，柔和细腻哑光材质",
    "fried_snacks": "炸物小食场景，金黄、暖红与炭灰配色，明快休闲哑光材质",
    "fruit": "水果果切场景，冷白、清水蓝与嫩绿配色，明亮清凉矿物材质",
}

SEAMLESS_SOLID_COLOR_ALIASES = {
    "浅木": "浅暖米色",
    "浅木色": "浅暖米色",
    "原木": "浅焦糖棕",
    "原木色": "浅焦糖棕",
    "暖木": "暖焦糖棕",
    "胡桃木": "深焦糖棕",
    "竹木色": "浅麦芽棕",
    "深木色": "深焦糖棕",
    "金属灰": "中性冷灰",
    "冷石色": "浅冷灰",
    "沙岩色": "浅沙金色",
}

# These v11 prompt bytes already back hash-locked approved COS manifests.
FROZEN_V11_PROMPT_CATEGORIES = frozenset(
    {"light_food", "topped_rice", "mixed_rice"}
)
FROZEN_PROFILE_V10_PROMPT_CATEGORIES = frozenset(
    {
        "porridge_soup_rice",
        "rice_noodles",
        "wheat_noodles",
        "dumpling_wonton",
        "buns_dim_sum",
        "chinese_wraps",
        "malatang_maocai",
        "hotpot_skewers",
        "barbecue",
    }
)
FROZEN_PROFILE_V11_SINGLE_HUE_PROMPT_CATEGORIES = frozenset(
    {
        "fried_chicken",
        "burger_hotdog",
        "pizza",
        "pasta_steak",
        "korean",
        "southeast_asian",
        "sandwich_bagel",
        "japanese",
        "sichuan_hunan",
        "cantonese_roast",
        "jiangzhe",
        "northeast_chinese",
        "northwest_xinjiang",
        "northern_lu",
        "fujian_taiwan",
        "home_stir_fry",
        "fish_seafood",
        "beef_lamb_pot",
    }
)
FROZEN_PROFILE_V12_NORMALIZED_PROMPT_CATEGORIES = frozenset(
    {
        "sandwich_bagel",
        "japanese",
        "sichuan_hunan",
        "cantonese_roast",
        "jiangzhe",
        "northeast_chinese",
        "northwest_xinjiang",
        "northern_lu",
        "fujian_taiwan",
        "home_stir_fry",
        "fish_seafood",
        "beef_lamb_pot",
    }
)
HASH_LOCKED_PROMPT_CATEGORIES = (
    FROZEN_V11_PROMPT_CATEGORIES
    | FROZEN_PROFILE_V10_PROMPT_CATEGORIES
    | FROZEN_PROFILE_V11_SINGLE_HUE_PROMPT_CATEGORIES
)

MIXED_SCENE = "复合餐饮菜单场景，中性商业摄影台面，兼容深碗、浅盘和餐盒轮廓"

_RULES_BY_ID = {
    taxonomy_id: tuple(keywords)
    for taxonomy_id, _label, keywords in TAXONOMY_RULES
}
_LABEL_TO_ID = {
    label: taxonomy_id
    for taxonomy_id, label in TAXONOMY_LABELS.items()
}
_EXPECTED_TAXONOMIES = set(_RULES_BY_ID)
if set(BACKGROUND_SCENES) != _EXPECTED_TAXONOMIES:
    missing = sorted(_EXPECTED_TAXONOMIES - set(BACKGROUND_SCENES))
    extra = sorted(set(BACKGROUND_SCENES) - _EXPECTED_TAXONOMIES)
    raise RuntimeError(
        f"background taxonomy profile mismatch: missing={missing}, extra={extra}"
    )


def normalize_category_id(value: Any) -> str:
    text = str(value or "").strip()
    if text in BACKGROUND_SCENES or text == MIXED_CATEGORY_ID:
        return text
    return _LABEL_TO_ID.get(text, MIXED_CATEGORY_ID)


def profile_keywords(category_id: str) -> tuple[str, ...]:
    return _RULES_BY_ID.get(normalize_category_id(category_id), ())


def style_prompt(category_id: str, style_id: str) -> str:
    normalized_category = normalize_category_id(category_id)
    try:
        style_index = STYLE_IDS.index(str(style_id or ""))
    except ValueError:
        style_index = 0
    scene = BACKGROUND_SCENES.get(normalized_category, MIXED_SCENE)
    return f"{scene}；{STYLE_VARIANTS[style_index]}"


def pure_background_style_prompt(category_id: str, style_id: str) -> str:
    normalized_category = normalize_category_id(category_id)
    slot = background_catalog.style_slot(style_id)
    scene = BACKGROUND_SCENES.get(normalized_category, MIXED_SCENE)
    match = re.search(r"场景，(.+?)配色，", scene)
    palette = re.split(r"[、与和]", match.group(1)) if match else []
    colors = [color.strip() for color in palette if color.strip()]
    if slot.scene_type == "seamless-solid":
        if style_id == "style-1":
            color_index = 0
        elif normalized_category in FROZEN_V11_PROMPT_CATEGORIES:
            color_index = -1
        else:
            color_index = 1 if len(colors) > 1 else -1
        color = colors[color_index] if colors else "低饱和中性色"
        if (
            normalized_category not in HASH_LOCKED_PROMPT_CATEGORIES
            or normalized_category
            in FROZEN_PROFILE_V12_NORMALIZED_PROMPT_CATEGORIES
        ):
            color = SEAMLESS_SOLID_COLOR_ALIASES.get(color, color)
        if (
            style_id == "style-2"
            and (
                normalized_category not in HASH_LOCKED_PROMPT_CATEGORIES
                or normalized_category
                in FROZEN_PROFILE_V11_SINGLE_HUE_PROMPT_CATEGORIES
            )
        ):
            single_color_prompt = slot.prompt.replace(
                "单一低饱和辅色",
                "同一色相的单一低饱和纯色",
            )
            return (
                f"仅以{color}为唯一主色；墙面、建筑圆弧和地面必须全部使用"
                f"{color}同一色相，仅允许自然明暗变化，地面不得变成另一色相，"
                f"禁止任何第二色地面；{single_color_prompt}"
            )
        return f"仅以{color}为唯一主色；{slot.prompt}"
    if normalized_category not in FROZEN_V11_PROMPT_CATEGORIES:
        wall_color = colors[0] if colors else "低饱和中性色"
        return (
            f"背景墙仅以{wall_color}为主色；ONE MATERIAL, ONE COLOR "
            "TABLETOP. NO PATCHWORK, INLAY OR COLOR BLOCKING. "
            "桌面禁止拼色、拼花、镶嵌、分区或混合材质；"
            f"{slot.prompt}"
        )
    prompt = style_prompt(normalized_category, style_id)
    prompt = re.sub(r"^[^，；。]+场景，", "", prompt)
    prompt = re.sub(r"(?:适合|兼容)[^，；。]+轮廓，?", "", prompt)
    return prompt.replace("点缀", "配色").replace("细节", "纹理").strip("，；。")


def pure_background_prompt(category_id: str, style_id: str) -> str:
    normalized_category = normalize_category_id(category_id)
    slot = background_catalog.style_slot(style_id)
    if slot.scene_type == "seamless-solid":
        geometry = (
            "ONE CONTINUOUS ARCHITECTURAL SURFACE ONLY. 只允许墙地一体的"
            "建筑无缝弧面，底部和中央同高；不得创建纸卷、幕布、水平矩形、"
            "独立平面或任何有边缘和厚度的形状。"
        )
    else:
        geometry = (
            "ONE ORDINARY TABLETOP ONLY. TABLETOP FRONT EDGE OUTSIDE FRAME. "
            "只允许一张普通桌面的连续纹理从左、右、下三边延伸到画外；"
            "桌沿和厚度必须在画幅下方不可见，不得出现第二层表面或中央矩形。"
        )
    return (
        "真实空景摄影，4:3横图。EMPTY SET ONLY. NO FOOD, PROPS, "
        "PODIUMS, PLINTHS, RISERS OR DISPLAY SURFACES. 禁止菜品、饮料、"
        "食材、植物、布料、餐具、容器、托盘、砧板、展示台、台座、垫板、"
        "桌垫、纸张、文字、logo、水印、人物或手。"
        f"{geometry}"
        "中央、边缘、前景和后景全部无物；"
        f"{pure_background_style_prompt(normalized_category, style_id)}；"
        "镜头约25度轻俯视，透视自然；中央约60%区域只保持连续材质和安静留白，"
        "不能为了放商品而创造任何矩形、平台、垫板或单独亮区；只允许一个明确"
        "主光方向，不能出现"
        "无来源阴影或不存在物体产生的投影，真实商业摄影光影。"
    )


def _signal_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"(粉丝|宠粉|关注)[^\s，,。；;]{0,6}福利", "福利", text)
    text = re.sub(r"(火锅|烧烤|咖喱|麻辣烫)味", "口味", text)
    return text


def _resolved_item_taxonomies(item: dict[str, Any]) -> list[str]:
    taxonomy = str(item.get("taxonomy") or "").strip()
    if taxonomy == TAXONOMY_COMBO or str(item.get("kind") or "") == "套餐/组合":
        resolved = []
        for component in item.get("components") or []:
            component_taxonomy = classify_taxonomy(str(component))
            if component_taxonomy in BACKGROUND_SCENES:
                resolved.append(component_taxonomy)
        return resolved
    if taxonomy not in BACKGROUND_SCENES:
        taxonomy = classify_taxonomy(
            _signal_text(item.get("name") or ""),
            "",
            _signal_text(item.get("category") or ""),
        )
    return [taxonomy] if taxonomy in BACKGROUND_SCENES else []


def _keyword_scores(menu: dict[str, Any]) -> Counter[str]:
    parts = [menu.get("store") or "", menu.get("file") or ""]
    for item in menu.get("items") or []:
        parts.extend(
            (
                item.get("category") or "",
                item.get("name") or "",
                " ".join(str(value) for value in item.get("components") or []),
            )
        )
    text = _signal_text(" ".join(str(value) for value in parts))
    scores: Counter[str] = Counter()
    for taxonomy_id, keywords in _RULES_BY_ID.items():
        scores[taxonomy_id] = sum(
            text.count(keyword.lower()) * max(1, len(keyword))
            for keyword in keywords
        )
    return scores


def menu_background_context(menu: dict[str, Any]) -> dict[str, Any]:
    items = [
        item
        for item in (menu.get("items") or [])
        if isinstance(item, dict)
    ]
    item_counts: Counter[str] = Counter()
    for item in items:
        item_counts.update(_resolved_item_taxonomies(item))

    keyword_scores = _keyword_scores(menu)
    scores: Counter[str] = Counter()
    for taxonomy_id in BACKGROUND_SCENES:
        scores[taxonomy_id] = (
            item_counts[taxonomy_id] * 10
            + keyword_scores[taxonomy_id]
        )

    ranked = sorted(
        BACKGROUND_SCENES,
        key=lambda taxonomy_id: (
            -scores[taxonomy_id],
            -item_counts[taxonomy_id],
            taxonomy_id,
        ),
    )
    evidence_primary = (
        ranked[0]
        if ranked and scores[ranked[0]] > 0
        else MIXED_CATEGORY_ID
    )
    store_taxonomy = classify_taxonomy(
        _signal_text(menu.get("store") or "")
    )
    file_taxonomy = classify_taxonomy(
        _signal_text(menu.get("file") or "")
    )
    dominant_item = (
        item_counts.most_common(1)[0]
        if item_counts
        else (MIXED_CATEGORY_ID, 0)
    )
    store_item_count = item_counts[store_taxonomy]
    store_conflicts_with_menu = bool(
        store_taxonomy in BACKGROUND_SCENES
        and dominant_item[0] != store_taxonomy
        and dominant_item[1] >= 2
        and dominant_item[1] > (store_item_count * 2)
    )
    store_is_corroborated = bool(
        file_taxonomy == store_taxonomy
        or not store_conflicts_with_menu
    )
    if store_taxonomy in BACKGROUND_SCENES and store_is_corroborated:
        primary = store_taxonomy
        selection_reason = "store_taxonomy"
    elif store_taxonomy in BACKGROUND_SCENES:
        primary = MIXED_CATEGORY_ID
        selection_reason = "insufficient_or_conflicting_evidence"
    else:
        primary = evidence_primary
        selection_reason = (
            "menu_taxonomy_evidence"
            if primary != MIXED_CATEGORY_ID
            else "insufficient_evidence"
        )

    ranked_candidates = sorted(
        (
            taxonomy_id
            for taxonomy_id in BACKGROUND_SCENES
            if scores[taxonomy_id] > 0
        ),
        key=lambda taxonomy_id: (
            -scores[taxonomy_id],
            -item_counts[taxonomy_id],
            taxonomy_id,
        ),
    )[:5]
    if primary in BACKGROUND_SCENES and primary not in ranked_candidates:
        ranked_candidates.insert(0, primary)
        ranked_candidates = ranked_candidates[:5]

    top_score = scores[ranked_candidates[0]] if ranked_candidates else 0
    second_score = (
        scores[ranked_candidates[1]]
        if len(ranked_candidates) > 1
        else 0
    )
    known_signals = sum(item_counts.values())
    coverage = min(1.0, known_signals / max(1, len(items)))
    margin = (
        max(0.0, (top_score - second_score) / top_score)
        if top_score
        else 0.0
    )
    if (
        selection_reason != "store_taxonomy"
        and primary != MIXED_CATEGORY_ID
        and (
            known_signals < 2
            or (second_score > 0 and margin < 0.12)
        )
    ):
        primary = MIXED_CATEGORY_ID
        selection_reason = "insufficient_or_conflicting_evidence"
    if primary == MIXED_CATEGORY_ID:
        confidence = 35
    elif selection_reason == "store_taxonomy":
        support = item_counts[primary] + keyword_scores[primary]
        confidence = min(96, 78 + min(18, support))
    else:
        confidence = min(
            95,
            max(45, round(50 + (coverage * 20) + (margin * 25))),
        )

    category_label = TAXONOMY_LABELS.get(primary, "复合餐饮")
    return {
        "category": category_label,
        "taxonomyId": primary,
        "primaryCategoryId": primary,
        "confidence": confidence,
        "selectionReason": selection_reason,
        "storeTaxonomyId": (
            store_taxonomy
            if store_taxonomy in BACKGROUND_SCENES
            else ""
        ),
        "fileTaxonomyId": (
            file_taxonomy
            if file_taxonomy in BACKGROUND_SCENES
            else ""
        ),
        "profileVersion": BACKGROUND_PROFILE_VERSION,
        "knownSignalCount": known_signals,
        "menuItemCount": len(items),
        "candidates": [
            {
                "name": TAXONOMY_LABELS[taxonomy_id],
                "taxonomyId": taxonomy_id,
                "score": int(scores[taxonomy_id]),
                "itemCount": int(item_counts[taxonomy_id]),
            }
            for taxonomy_id in ranked_candidates
        ],
    }
