from __future__ import annotations

from collections import Counter
import re
import unicodedata
from typing import Any

from matching_engine import (
    TAXONOMY_COMBO,
    TAXONOMY_LABELS,
    TAXONOMY_RULES,
    TAXONOMY_UNKNOWN,
    classify_taxonomy,
)


BACKGROUND_PROFILE_VERSION = "2026-07-31.v3"
MIXED_CATEGORY_ID = "mixed"
STYLE_IDS = tuple(f"style-{index}" for index in range(1, 7))

PURE_BACKGROUND_NEGATIVE_PROMPT = (
    "食物，菜品，饮料，水果，蔬菜，香草，食材，原料，牛油果，莓果，"
    "柠檬，橄榄，餐具，餐盘，碗，杯子，餐盒，筷子，刀叉，"
    "文字，价格，菜单，logo，品牌名，水印，人物，手，低清晰度，"
    "模糊，畸变，拼贴，边框，画中画"
)

STYLE_VARIANTS = (
    "明亮自然光，低饱和浅色台面，清爽日间氛围",
    "柔和侧光，深色石材或木质台面，高级餐厅质感",
    "高调漫射光，白灰极简台面，干净大面积留白",
    "品类文化色彩点缀，饱和但不刺眼，画面节奏鲜明",
    "自然手作材质与真实纹理，温暖生活感",
    "冷中性现代台面，均匀商业棚拍光，适合批量统一主图",
)

BACKGROUND_SCENES = {
    "topped_rice": "中式现炒盖饭场景，适合大碗或圆盘轮廓，宽阔耐热台面",
    "mixed_rice": "炒饭拌饭场景，适合圆盘或石锅轮廓，整洁餐厅桌面",
    "porridge_soup_rice": "粥和汤饭场景，适合深碗轮廓，温润晨间餐桌",
    "rice_noodles": "米粉米线场景，适合深口碗轮廓，清爽地方小吃店材质",
    "wheat_noodles": "面食场景，适合宽口面碗轮廓，利落面馆台面",
    "dumpling_wonton": "饺子馄饨场景，适合浅盘或汤碗轮廓，细腻蒸笼纹理点缀",
    "buns_dim_sum": "包子点心场景，适合蒸笼或小盘轮廓，明净早茶台面",
    "chinese_wraps": "中式卷饼场景，适合长盘或纸托轮廓，轻快早餐档口质感",
    "malatang_maocai": "麻辣烫冒菜场景，适合深碗轮廓，红油暖色与耐热石材",
    "hotpot_skewers": "火锅串串场景，适合锅具中心轮廓，深色耐热桌面与金属细节",
    "barbecue": "烧烤场景，适合长盘或烤盘轮廓，炭火质感深色桌面",
    "fried_chicken": "炸鸡场景，适合分享桶或宽盘轮廓，年轻明快快餐质感",
    "burger_hotdog": "汉堡热狗场景，适合纸托或餐盘轮廓，现代快餐台面",
    "pizza": "披萨场景，适合大圆盘轮廓，意式餐厅木石材质",
    "sandwich_bagel": "三明治贝果场景，适合长方盘轮廓，明亮咖啡馆台面",
    "light_food": "轻食沙拉场景，适合大浅碗轮廓，清新自然材质与绿色点缀",
    "pasta_steak": "意面牛排场景，适合西式宽盘轮廓，精致餐厅桌面",
    "japanese": "日料场景，适合漆器或陶盘轮廓，克制日式木纹与和纸质感",
    "korean": "韩餐场景，适合石锅或多格餐盘轮廓，现代韩式餐厅材质",
    "southeast_asian": "东南亚菜场景，适合宽碗或圆盘轮廓，热带清新色彩与藤编细节",
    "sichuan_hunan": "川湘菜场景，适合大圆盘轮廓，热辣暖色与中式木石材质",
    "cantonese_roast": "粤式烧味场景，适合长盘轮廓，明亮港式餐厅台面",
    "jiangzhe": "江浙菜场景，适合雅致圆盘轮廓，水墨感浅色中式材质",
    "northeast_chinese": "东北菜场景，适合大盘轮廓，厚实木桌与温暖家宴质感",
    "northwest_xinjiang": "西北新疆菜场景，适合大盘或长盘轮廓，粗粝木石与织物纹理",
    "northern_lu": "北方鲁菜场景，适合大圆盘轮廓，端正中式宴席材质",
    "fujian_taiwan": "闽台菜场景，适合汤碗或小盘轮廓，清爽沿海餐厅材质",
    "home_stir_fry": "家常小炒场景，适合家常圆盘轮廓，温暖木桌与真实烟火感",
    "fish_seafood": "鱼和海鲜场景，适合长盘或大圆盘轮廓，清凉沿海石材质感",
    "beef_lamb_pot": "牛羊锅场景，适合砂锅或深锅轮廓，厚重耐热桌面与暖光",
    "braised_cooked_food": "卤味熟食凉菜场景，适合拼盘轮廓，中式木纹与克制暖色",
    "soup_stew": "汤羹炖品场景，适合深碗或炖盅轮廓，温润素雅桌面",
    "steamed_claypot": "蒸菜煲仔场景，适合砂锅或蒸笼轮廓，耐热木石台面",
    "milk_fruit_tea": "奶茶果茶场景，适合高杯轮廓，清透明亮饮品店台面",
    "coffee_cocoa": "咖啡可可场景，适合杯具轮廓，现代咖啡馆木石台面",
    "bottled_drinks": "瓶装酒水饮料场景，适合瓶罐轮廓，简洁零售冷柜或吧台质感",
    "fresh_drinks": "鲜榨饮品场景，适合透明杯轮廓，清新水果吧台材质",
    "dessert_bakery": "甜品烘焙场景，适合小盘或蛋糕托轮廓，柔和烘焙店台面",
    "fried_snacks": "炸物小食场景，适合纸托或小篮轮廓，明快休闲餐饮台面",
    "fruit": "水果果切场景，适合浅盘或透明盒轮廓，明亮清凉自然台面",
}

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
    prompt = style_prompt(category_id, style_id)
    prompt = re.sub(r"^[^，；。]+场景，", "", prompt)
    return re.sub(r"(?:适合|兼容)[^，；。]+轮廓，?", "", prompt).strip("，；。")


def pure_background_prompt(category_id: str, style_id: str) -> str:
    normalized_category = normalize_category_id(category_id)
    label = TAXONOMY_LABELS.get(normalized_category, "复合餐饮")
    return (
        "纯背景场景商业摄影。EMPTY SET ONLY. NO FOOD, EDIBLE PROPS OR "
        "CONTAINERS. 禁止菜品、饮料、果蔬、香草、食材、餐具、容器、文字、"
        "菜单、logo、水印、人物或手。只允许空置台面、背景墙和非食用材质；"
        f"中央与边缘都必须完全空置。供后期合成{label}商品；"
        "仅用材质、配色和光线暗示品类；"
        f"{pure_background_style_prompt(normalized_category, style_id)}；"
        "中央保留完整宽阔摆放区，真实光影，高分辨率。"
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

    store_taxonomy = classify_taxonomy(
        _signal_text(menu.get("store") or "")
    )
    if store_taxonomy in BACKGROUND_SCENES:
        primary = store_taxonomy
        selection_reason = "store_taxonomy"
    else:
        ranked = sorted(
            BACKGROUND_SCENES,
            key=lambda taxonomy_id: (
                -scores[taxonomy_id],
                -item_counts[taxonomy_id],
                taxonomy_id,
            ),
        )
        primary = ranked[0] if ranked and scores[ranked[0]] > 0 else MIXED_CATEGORY_ID
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
