from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


DIRECT_SCORE = 70.0
REVIEW_SCORE = 45.0
DEFAULT_MIN_SCORE = 0.15

MARKETING_WORDS = (
    "招牌",
    "爆款",
    "热销",
    "人气",
    "福利",
    "特惠",
    "优惠",
    "新品",
    "必点",
    "现炒",
    "现煎",
    "秘制",
    "正宗",
    "经典",
    "老长沙",
)

FORMAT_WORDS = (
    "盖码饭",
    "盖浇饭",
    "盖饭",
    "木桶饭",
    "套餐",
    "组合",
    "单人餐",
    "双人餐",
    "米饭",
    "白饭",
)

COMPONENT_DROP_WORDS = (
    "套餐",
    "组合",
    "单人餐",
    "双人餐",
    "三人餐",
    "盖码饭",
    "盖浇饭",
    "木桶饭",
)

GENERIC_COMPONENTS = {
    "米饭",
    "白米饭",
    "主食",
    "餐具",
    "饮料任选",
    "任选",
    "自选",
}

DRINK_SNACK_WORDS = (
    "可乐",
    "雪碧",
    "芬达",
    "矿泉水",
    "纯净水",
    "王老吉",
    "冰红茶",
    "绿茶",
    "豆浆",
    "果汁",
    "奶茶",
    "酸梅汤",
    "饮品",
    "饮料",
    "小食",
    "小吃",
    "汤",
)

MAIN_FOOD_WORDS = (
    "盖饭",
    "盖码饭",
    "木桶饭",
    "拌饭",
    "炒饭",
    "汤饭",
    "米饭",
    "米粉",
    "米线",
    "面",
    "粥",
    "抄手",
    "饺",
    "包子",
)

COMBO_WORDS = (
    "套餐",
    "组合",
    "双拼",
    "三拼",
    "四拼",
    "拼盘",
    "自选",
    "任选",
    "配菜",
)

SPLIT_RE = re.compile(r"[+＋#&/／、,，|丨;；]+|\s+(?:配|加|和|含)\s+")


def normalize_dish(text: str) -> str:
    """Return a stable key for dish-name comparison."""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,40}[）)]", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|l|克|g|kg|斤|个|只|份|瓶|罐|盒|两)", "", text)
    text = re.sub(r"(买一送一|第二份半价|限时|折扣|满减|赠|送)", "", text)
    text = text.replace("小炒肉", "炒肉")
    text = text.replace("农家一碗香", "一碗香")
    text = text.replace("紫菜鸡蛋汤", "紫菜蛋花汤")
    for word in MARKETING_WORDS + FORMAT_WORDS:
        text = text.replace(word, "")
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).strip()


def grams(text: str) -> set[str]:
    norm = str(text or "")
    if not norm:
        return set()
    out = {norm}
    for size in (2, 3):
        if len(norm) >= size:
            out.update(norm[i : i + size] for i in range(len(norm) - size + 1))
    return out


def _clean_component_label(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"^\s*(含|配|加|赠送?|另附|包含)[:：]?\s*", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|l|克|g|kg|斤|个|只|份|瓶|罐|盒|两)", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -_·:：")
    for word in COMPONENT_DROP_WORDS:
        text = text.replace(word, "")
    return text.strip(" -_·:：")


def split_components(name: str, attrs: str = "") -> list[str]:
    """Split combo/set-meal names into matchable dish components."""
    source = f"{name or ''}+{attrs or ''}" if attrs else str(name or "")
    source = unicodedata.normalize("NFKC", source)
    source = re.sub(r"(套餐内容|规格|内容|包含|含)[:：]", "+", source)
    source = re.sub(r"\s*[xX*]\s*\d+\s*", "+", source)
    raw_parts = SPLIT_RE.split(source)
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_parts:
        label = _clean_component_label(raw)
        norm = normalize_dish(label)
        if len(norm) < 2 or label in GENERIC_COMPONENTS or norm in seen:
            continue
        seen.add(norm)
        out.append(label)
    return out[:8]


def classify_kind(name: str, attrs: str = "", category: str = "") -> str:
    """Classify a menu item as single dish, combo, or snack/drink."""
    text = unicodedata.normalize("NFKC", f"{category or ''} {name or ''} {attrs or ''}")
    components = split_components(name, attrs)
    has_separator = bool(re.search(r"[+＋#&/／、,，|丨;；]", text))
    if any(word in text for word in COMBO_WORDS) or (has_separator and len(components) >= 2):
        return "套餐/组合"
    if any(word in text for word in MAIN_FOOD_WORDS):
        return "单品"
    if any(word in text for word in DRINK_SNACK_WORDS):
        return "饮品/小食"
    return "单品"


def similarity(
    menu_name: str,
    image_name: str,
    menu_norm: str | None = None,
    image_norm: str | None = None,
    menu_grams: set[str] | None = None,
    image_grams: set[str] | None = None,
) -> float:
    """Score two dish names in the range 0.0-1.0."""
    left = menu_norm if menu_norm is not None else normalize_dish(menu_name)
    right = image_norm if image_norm is not None else normalize_dish(image_name)
    if not left or not right:
        return 0.0
    left_grams = menu_grams if menu_grams is not None else grams(left)
    right_grams = image_grams if image_grams is not None else grams(right)
    seq = SequenceMatcher(None, left, right).ratio()
    jac = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
    contains = 0.22 if left in right or right in left else 0.0
    prefix = 0.06 if left[:2] == right[:2] else 0.0
    length_gap = abs(len(left) - len(right)) / max(len(left), len(right), 1)
    score = seq * 0.48 + jac * 0.40 + contains + prefix - length_gap * 0.08
    return max(0.0, min(1.0, score))


def _value(record: Any, *keys: str, default: Any = "") -> Any:
    for key in keys:
        if isinstance(record, Mapping) and key in record:
            value = record[key]
            if value is not None:
                return value
        if hasattr(record, key):
            value = getattr(record, key)
            if value is not None:
                return value
    return default


def _path_stem(record: Any) -> str:
    path = _value(record, "path", "file", default="")
    if path:
        return Path(str(path)).stem
    return ""


def _record_name(record: Any) -> str:
    return str(
        _value(
            record,
            "dishName",
            "dish_name",
            "dish",
            "name",
            "title",
            default=_path_stem(record),
        )
        or ""
    )


def _record_norm(record: Any) -> str:
    return str(_value(record, "norm", "normalized", "canonical", default="") or normalize_dish(_record_name(record)))


def _record_style(record: Any) -> str:
    return str(_value(record, "styleId", "style_id", "style", default="style-upload") or "style-upload")


def _record_source(record: Any) -> str:
    return str(_value(record, "source", "store", "provider", "batch", default="library") or "library")


def _record_id(record: Any, name: str, style_id: str) -> str:
    value = _value(record, "imageId", "image_id", "id", default="")
    if value:
        return str(value)
    seed = "|".join([str(_value(record, "path", "url", default="")), name, style_id])
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:18]


def _candidate(record: Any, score: float, matched_name: str, match_type: str, component: str = "") -> dict[str, Any]:
    dish_name = _record_name(record)
    style_id = _record_style(record)
    source = _record_source(record)
    path = _value(record, "path", default="")
    url = _value(record, "url", "publicUrl", "public_url", default="")
    candidate = {
        "imageId": _record_id(record, dish_name, style_id),
        "score": round(score * 100, 1),
        "dishName": dish_name,
        "styleId": style_id,
        "source": source,
        "store": source,
        "matchType": match_type,
        "matchedName": matched_name,
    }
    if component:
        candidate["component"] = component
    if url:
        candidate["url"] = str(url)
    if path:
        candidate["path"] = str(path)
    return candidate


def _prepared_records(records: Sequence[Any]) -> list[dict[str, Any]]:
    prepared = []
    for record in records:
        name = _record_name(record)
        norm = _record_norm(record)
        if not norm:
            continue
        prepared.append({"record": record, "name": name, "norm": norm, "grams": grams(norm)})
    return prepared


def _score_candidates(
    query_name: str,
    prepared_records: list[dict[str, Any]],
    *,
    limit: int,
    min_score: float,
    match_type: str,
    component: str = "",
) -> list[dict[str, Any]]:
    query_norm = normalize_dish(query_name)
    query_grams = grams(query_norm)
    scored: list[tuple[float, Any]] = []
    for prepared in prepared_records:
        score = similarity(query_name, prepared["name"], query_norm, prepared["norm"], query_grams, prepared["grams"])
        if score >= min_score:
            scored.append((score, prepared["record"]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_candidate(record, score, query_name, match_type, component) for score, record in scored[:limit]]


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.get("imageId") or candidate.get("path") or candidate.get("url") or candidate.get("dishName"))
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _sort_candidates(candidates: list[dict[str, Any]], selected_style: str = "") -> list[dict[str, Any]]:
    if selected_style:
        return sorted(candidates, key=lambda c: (c.get("styleId") == selected_style, float(c.get("score") or 0)), reverse=True)
    return sorted(candidates, key=lambda c: float(c.get("score") or 0), reverse=True)


def _status_for(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "未找到"
    score = float(candidates[0].get("score") or 0)
    if score >= DIRECT_SCORE:
        return "直接可用"
    if score >= REVIEW_SCORE:
        return "需人工确认"
    return "弱匹配"


def _background_action(candidates: list[dict[str, Any]], selected_style: str = "") -> str:
    if not candidates:
        return "需要定制/生成"
    chosen = candidates[0]
    score = float(chosen.get("score") or 0)
    if selected_style:
        if chosen.get("styleId") == selected_style and score >= DIRECT_SCORE:
            return "背景一致，直接复用"
        if chosen.get("styleId") == selected_style and score >= REVIEW_SCORE:
            return "需人工确认"
        if score >= REVIEW_SCORE:
            return "需抠图换背景"
        return "智能补图"
    if score >= DIRECT_SCORE:
        return "优先复用图库图"
    if score >= REVIEW_SCORE:
        return "需人工确认"
    return "智能补图"


def _item_value(item: Any, *keys: str, default: Any = "") -> Any:
    return _value(item, *keys, default=default)


def match_menu_to_library(
    items: Sequence[Any],
    records: Sequence[Any],
    *,
    selected_style: str = "",
    limit: int = 6,
    component_limit: int = 3,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Match menu items to image-library records.

    Each returned row contains dish candidates, per-component candidates for combo
    meals, score/status fields, style/source metadata, and a background action
    recommendation.
    """
    prepared = _prepared_records(records)
    results: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        name = str(_item_value(item, "name", "dishName", "dish", "title", default=item if isinstance(item, str) else "") or "")
        attrs = str(_item_value(item, "attrs", "attributes", "spec", "description", default="") or "")
        category = str(_item_value(item, "category", "cat", default="") or "")
        row = _item_value(item, "row", "index", default=idx)
        components = list(_item_value(item, "components", default=[]) or split_components(name, attrs))
        kind = str(_item_value(item, "kind", "type", default="") or classify_kind(name, attrs, category))
        norm = str(_item_value(item, "norm", "normalized", default="") or normalize_dish(name))

        dish_candidates = _score_candidates(name, prepared, limit=limit, min_score=min_score, match_type="dish")
        component_matches = []
        component_candidates: list[dict[str, Any]] = []
        if kind == "套餐/组合" and components:
            for component in components:
                matches = _score_candidates(
                    component,
                    prepared,
                    limit=component_limit,
                    min_score=min_score,
                    match_type="component",
                    component=component,
                )
                component_matches.append(
                    {
                        "name": component,
                        "norm": normalize_dish(component),
                        "status": _status_for(matches),
                        "candidates": matches,
                    }
                )
                component_candidates.extend(matches)

        candidates = _dedupe_candidates(dish_candidates + component_candidates)
        candidates = _sort_candidates(candidates, selected_style)[:limit]
        status = _status_for(candidates)
        background_action = _background_action(candidates, selected_style)
        results.append(
            {
                "row": row,
                "category": category,
                "name": name,
                "kind": kind,
                "norm": norm,
                "components": components,
                "status": status,
                "candidates": candidates,
                "componentMatches": component_matches,
                "backgroundAction": background_action,
                "selectedStyle": selected_style,
            }
        )
    return results


def _style_ids_from_results(results: Sequence[Mapping[str, Any]]) -> list[str]:
    style_ids = {
        str(candidate.get("styleId"))
        for row in results
        for candidate in row.get("candidates", [])
        if candidate.get("styleId")
    }
    return sorted(style_ids)


def _best_candidate_for_style(row: Mapping[str, Any], style_id: str) -> dict[str, Any] | None:
    candidates = [c for c in row.get("candidates", []) if c.get("styleId") == style_id]
    if not candidates:
        return None
    return max(candidates, key=lambda c: float(c.get("score") or 0))


def _component_style_status(row: Mapping[str, Any], style_id: str) -> str | None:
    components = row.get("componentMatches") or []
    if not components:
        return None
    best_scores = []
    for component in components:
        same_style = [c for c in component.get("candidates", []) if c.get("styleId") == style_id]
        if not same_style:
            return "bgReplace"
        best_scores.append(max(float(c.get("score") or 0) for c in same_style))
    if best_scores and all(score >= DIRECT_SCORE for score in best_scores):
        return "direct"
    if best_scores and all(score >= REVIEW_SCORE for score in best_scores):
        return "review"
    return "custom"


def style_coverage(
    items_or_matches: Sequence[Any],
    records: Sequence[Any] | None = None,
    *,
    limit: int = 6,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Summarize how well each style covers the matched menu."""
    if records is None:
        results = list(items_or_matches)
    else:
        results = match_menu_to_library(items_or_matches, records, limit=limit, min_score=min_score)
    total = max(1, len(results))
    options = []
    for style_id in _style_ids_from_results(results):
        direct = review = bg_replace = custom = count = 0
        sample = None
        for row in results:
            component_status = _component_style_status(row, style_id)
            same = _best_candidate_for_style(row, style_id)
            sample = sample or same
            if same:
                count += 1
            if component_status == "direct":
                direct += 1
                continue
            if component_status == "review":
                review += 1
                continue
            if component_status == "bgReplace":
                bg_replace += 1
                continue
            if component_status == "custom":
                custom += 1
                continue
            if same:
                score = float(same.get("score") or 0)
                if score >= DIRECT_SCORE:
                    direct += 1
                elif score >= REVIEW_SCORE:
                    review += 1
                else:
                    custom += 1
            elif row.get("candidates"):
                bg_replace += 1
            else:
                custom += 1
        options.append(
            {
                "id": style_id,
                "styleId": style_id,
                "count": count,
                "sample": sample,
                "direct": direct,
                "review": review,
                "bgReplace": bg_replace,
                "custom": custom,
                "directRate": round(direct / total * 100, 1),
                "processingRate": round((review + bg_replace) / total * 100, 1),
                "customRate": round(custom / total * 100, 1),
            }
        )
    return sorted(options, key=lambda item: (item["direct"], item["review"], item["count"]), reverse=True)


SAMPLE_MENU_ITEMS = [
    {"row": 1, "category": "热销", "name": "老长沙辣椒炒肉盖码饭", "price": "19.8"},
    {"row": 2, "category": "套餐", "name": "辣椒炒肉+茄子肉末盖码饭", "price": "24.8"},
    {"row": 3, "category": "饮品", "name": "康师傅冰红茶", "price": "4.0"},
]

SAMPLE_LIBRARY_RECORDS = [
    {"imageId": "sample-1", "dishName": "辣椒小炒肉盖饭", "styleId": "style-1", "source": "sample"},
    {"imageId": "sample-2", "dishName": "茄子肉末盖码饭", "styleId": "style-1", "source": "sample"},
    {"imageId": "sample-3", "dishName": "康师傅冰红茶", "styleId": "style-2", "source": "sample"},
]


def run_builtin_selftest() -> dict[str, Any]:
    matches = match_menu_to_library(SAMPLE_MENU_ITEMS, SAMPLE_LIBRARY_RECORDS, selected_style="style-1")
    coverage = style_coverage(matches)
    combo = next(row for row in matches if row["kind"] == "套餐/组合")
    ok = bool(
        normalize_dish("【热销】老长沙辣椒炒肉盖码饭") == "辣椒炒肉"
        and len(combo["componentMatches"]) >= 2
        and all(component["candidates"] for component in combo["componentMatches"][:2])
        and coverage
    )
    return {"ok": ok, "matches": matches, "coverage": coverage}


if __name__ == "__main__":
    result = run_builtin_selftest()
    print("ok" if result["ok"] else "failed")
