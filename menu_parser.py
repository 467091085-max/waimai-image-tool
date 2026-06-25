from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

MENU_EXTS = {".xls", ".xlsx"}
DEFAULT_MENU_DIR = Path.home() / "Documents" / "menus"

KIND_SINGLE = "单品"
KIND_COMBO = "套餐/组合"
KIND_SNACK = "饮品/小食"
KIND_OTHER = "其他"

BASIC_COMBO = "套餐"
BASIC_BEVERAGE = "饮品"
BASIC_ADDON = "小料"
BASIC_STAPLE = "主食"
BASIC_RICE_NOODLE = "米粉/米线"
BASIC_NOODLE = "面食/抄手"
BASIC_PORRIDGE = "粥/汤饭"
BASIC_MAIN = "炒菜/盖饭"
BASIC_SNACK = "小吃"
BASIC_OTHER = "其他"

NAME_HEADERS = {
    "菜单名",
    "菜品名",
    "菜品名称",
    "菜名",
    "餐品名",
    "餐品名称",
    "商品名",
    "商品名称",
    "产品名",
    "产品名称",
    "名称",
    "品名",
    "门店菜品",
}
NAME_HEADER_PRIORITY = {
    "菜单名": 120,
    "商品名称": 118,
    "商品名": 116,
    "菜品名称": 114,
    "菜品名": 112,
    "餐品名称": 110,
    "餐品名": 108,
    "产品名称": 106,
    "产品名": 104,
    "菜名": 102,
    "品名": 96,
    "名称": 90,
    "门店菜品": 78,
}
NAME_EXTRA_HEADERS = {"名称调整", "调整名称", "展示名称", "商品别名", "菜品别名"}

CATEGORY_HEADERS = {
    "一级分类",
    "二级分类",
    "分类",
    "分类名",
    "分类名称",
    "商品分类",
    "菜单分类",
    "分组",
    "品类",
}
CATEGORY_IGNORE_VALUES = {"全部", "所有", "未分类", "默认", "无", "-"}

PRICE_HEADER_PRIORITY = {
    "活动价": 130,
    "折扣价": 126,
    "优惠价": 124,
    "现价": 122,
    "售价": 120,
    "销售价": 118,
    "价格": 116,
    "门店价格": 112,
    "会员价": 110,
    "打包价": 96,
    "折扣": 72,
    "原价": 64,
    "堂食售卖价格": 56,
}

ATTRIBUTE_HEADERS = {
    "属性",
    "规格",
    "规格名",
    "套餐内容",
    "内容",
    "备注",
    "原料",
}

NOISE_SHEET_WORDS = {
    "活动",
    "注意",
    "须知",
    "说明",
    "商圈",
    "调研",
    "竞品",
    "成本",
    "成本表",
    "库存",
}
PRIMARY_SHEET_WORDS = {"菜单", "菜品", "商品", "调研结果", "menu"}
DISCOURAGED_NAME_PARTS = {
    "商家名称",
    "店铺名称",
    "门店名称",
    "月销量",
    "图片链接",
    "配送",
}

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
    "推荐",
    "现炒",
    "现煎",
    "手打",
    "手作",
    "手工",
    "入店必点",
    "秘制",
    "正宗",
    "经典",
    "老长沙",
    "严选",
    "超值",
    "精品",
    "厨师",
    "收藏",
    "宠粉",
    "粉丝",
    "会员",
    "专享",
)

PLATFORM_WORDS = (
    "美团",
    "饿了么",
    "大众点评",
    "外卖",
    "堂食",
    "到店",
    "门店",
    "下单",
    "打包",
    "配送",
)

SPEC_WORDS = (
    "大份",
    "中份",
    "小份",
    "超大份",
    "标准份",
    "一份",
    "单份",
    "堂食份量",
    "堂食分量",
    "加量",
    "双倍加量",
    "微辣",
    "中辣",
    "重辣",
    "特辣",
    "少辣",
    "免辣",
    "不辣",
    "加辣",
    "常温",
    "冰镇",
    "去冰",
    "少冰",
    "正常冰",
    "咸鲜",
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
)

COMBO_WORDS = (
    "套餐",
    "组合",
    "套饭",
    "套餐饭",
    "双拼",
    "三拼",
    "四拼",
    "多拼",
    "拼盘",
    "自选",
    "任选",
    "搭配",
    "搭子",
    "多人餐",
    "单人餐",
    "双人餐",
    "三人餐",
    "四人餐",
    "亲子餐",
    "分享餐",
    "大礼包",
    "全家桶",
)

BEVERAGE_WORDS = (
    "可乐",
    "雪碧",
    "芬达",
    "王老吉",
    "冰红茶",
    "绿茶",
    "矿泉水",
    "纯净水",
    "椰子水",
    "豆浆",
    "果汁",
    "柠檬水",
    "柠檬茶",
    "金桔",
    "酸梅汤",
    "奶茶",
    "咖啡",
    "酸奶",
    "冰沙",
    "饮品",
    "饮料",
)

ADDON_WORDS = (
    "小料",
    "加料",
    "配料",
    "蘸料",
    "蘸水",
    "蘸碟",
    "调料",
    "酱汁",
    "料汁",
    "辣椒包",
    "生抽包",
    "醋包",
    "白糖包",
    "香菜",
    "葱花",
    "蒜粒",
    "泡菜",
    "沙拉汁",
)

ADDON_EXACT_WORDS = {
    "加鸡蛋",
    "加煎蛋",
    "加荷包蛋",
    "加卤蛋",
    "加茶叶蛋",
    "加肠",
    "加火腿",
    "加肉",
    "加粉",
    "加面",
}

SERVICE_WORDS = (
    "餐具",
    "发票",
    "纸巾",
    "打包盒",
    "包装费",
    "配送费",
    "补差价",
    "差价",
    "勿点",
    "勿拍",
    "不要点",
    "不要拍",
    "请勿下单",
    "温馨提示",
    "提示",
    "说明",
    "公告",
    "收藏",
    "福利",
    "满减",
    "红包",
)

PLAIN_RICE_WORDS = ("米饭", "白米饭", "白饭", "珍珠饭", "杂粮饭", "糙米饭", "主食")
RICE_DISH_WORDS = ("炒饭", "盖饭", "盖码饭", "盖浇饭", "拌饭", "汤饭", "煲仔饭", "木桶饭")
RICE_NOODLE_WORDS = ("螺蛳粉", "米粉", "米线", "酸辣粉", "河粉", "粉丝", "土豆粉")
NOODLE_WORDS = ("面", "抄手", "馄饨", "水饺", "饺子", "包子", "烧麦", "肠粉", "云吞")
PORRIDGE_WORDS = ("粥", "豆汤饭", "汤饭")
SNACK_WORDS = (
    "小食",
    "小吃",
    "甜品",
    "茶叶蛋",
    "溏心蛋",
    "煎蛋",
    "荷包蛋",
    "卤蛋",
    "锅贴",
    "汤圆",
    "凉菜",
    "卤味",
    "花生米",
)


@dataclass
class TableCandidate:
    sheet_name: str
    sheet_index: int
    header_row: int
    headers: list[str]
    name_col: int
    price_col: int | None
    category_cols: list[int]
    attribute_cols: list[int]
    name_extra_cols: list[int]
    score: float
    primary: bool
    discouraged: bool


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\xa0", " ").strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return re.sub(r"\s+", " ", text)


def clean_header(value: Any) -> str:
    text = clean_cell(value)
    text = re.sub(r"^[*＊#＃\s]+", "", text)
    text = re.sub(r"[:：]+$", "", text)
    return re.sub(r"\s+", "", text)


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def _is_plain_rice_text(compact: str) -> bool:
    if not compact:
        return False
    if any(word in compact for word in RICE_DISH_WORDS):
        return False
    return bool(re.fullmatch(r"(一碗|一份|半份|小份|大份|加|配|赠|送|另加|单点)?(白米饭|米饭|白饭|主食)", compact))


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    compact = re.sub(r"\s+", "", text)
    if _is_plain_rice_text(compact):
        return "米饭"
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,40}[）)]", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|l|克|g|kg|斤|个|只|份|瓶|罐|串|枚|盒|杯|碗|两)", "", text)
    text = re.sub(r"(买一送一|第二份半价|限时|折扣|满减|赠|送)", "", text)
    text = text.replace("西红柿", "番茄")
    text = text.replace("番茄炒鸡蛋", "番茄炒蛋")
    text = text.replace("紫菜鸡蛋汤", "紫菜蛋花汤")
    text = text.replace("爆炒黄牛肉", "小炒黄牛肉")
    text = text.replace("辣椒小炒肉", "辣椒炒肉")
    text = text.replace("农家小炒肉", "辣椒炒肉")
    text = text.replace("农家一碗香", "一碗香")
    text = text.replace("肉沫", "肉末")
    text = text.replace("宫爆鸡丁", "宫保鸡丁")
    for word in MARKETING_WORDS + PLATFORM_WORDS + SPEC_WORDS + FORMAT_WORDS:
        text = text.replace(word, "")
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).strip()


def grams(text: str) -> set[str]:
    if not text:
        return set()
    out = {text}
    for size in (2, 3):
        if len(text) >= size:
            out.update(text[i : i + size] for i in range(len(text) - size + 1))
    return out


def _price_number(value: str) -> str:
    text = clean_cell(value)
    if not text:
        return ""
    text = text.replace("￥", "").replace("¥", "").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        number = float(text)
        return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match and not re.search(r"[折限起购满减送]", text):
        return _price_number(match.group(0))
    return text


def _is_price_like(value: str) -> bool:
    text = clean_cell(value)
    if not text or re.search(r"[折限起购满减送|]", text):
        return False
    return bool(re.fullmatch(r"[￥¥]?\s*-?\d+(?:\.\d+)?\s*", text))


def _numeric_ratio(df: pd.DataFrame, col: int, start_row: int, sample: int = 28) -> float:
    if col >= df.shape[1]:
        return 0.0
    values = [clean_cell(v) for v in df.iloc[start_row : start_row + sample, col].tolist()]
    values = [v for v in values if v]
    if not values:
        return 0.0
    return sum(1 for value in values if _is_price_like(value)) / len(values)


def _choose_name_col(headers: list[str]) -> int | None:
    best: tuple[int, int] | None = None
    for idx, header in enumerate(headers):
        if header in NAME_HEADERS and not any(part in header for part in DISCOURAGED_NAME_PARTS):
            score = NAME_HEADER_PRIORITY.get(header, 80)
            if best is None or score > best[0]:
                best = (score, idx)
    return best[1] if best else None


def _choose_price_col(df: pd.DataFrame, headers: list[str], data_start: int) -> int | None:
    best: tuple[float, int] | None = None
    for idx, header in enumerate(headers):
        if header not in PRICE_HEADER_PRIORITY:
            continue
        ratio = _numeric_ratio(df, idx, data_start)
        if header == "折扣" and ratio < 0.65:
            continue
        score = PRICE_HEADER_PRIORITY[header] + ratio * 35
        if best is None or score > best[0]:
            best = (score, idx)
    return best[1] if best else None


def _sheet_primary(sheet_name: str) -> bool:
    folded = clean_header(sheet_name).lower()
    return any(word.lower() in folded for word in PRIMARY_SHEET_WORDS)


def _sheet_discouraged(sheet_name: str) -> bool:
    folded = clean_header(sheet_name).lower()
    if any(word.lower() in folded for word in PRIMARY_SHEET_WORDS):
        return False
    return any(word.lower() in folded for word in NOISE_SHEET_WORDS)


def _header_score(
    df: pd.DataFrame,
    headers: list[str],
    row_index: int,
    sheet_name: str,
    name_col: int,
    price_col: int | None,
    category_cols: list[int],
    attribute_cols: list[int],
    name_extra_cols: list[int],
) -> float:
    score = 0.0
    score += NAME_HEADER_PRIORITY.get(headers[name_col], 80)
    if price_col is not None:
        score += 50 + _numeric_ratio(df, price_col, row_index + 1) * 24
    score += min(28, 11 * len(category_cols))
    score += min(18, 5 * len(attribute_cols))
    score += min(8, 4 * len(name_extra_cols))
    if _sheet_primary(sheet_name):
        score += 24
    if _sheet_discouraged(sheet_name):
        score -= 38
    if any(part in "".join(headers) for part in DISCOURAGED_NAME_PARTS):
        score -= 18
    return score


def _find_table_candidates(df: pd.DataFrame, sheet_name: str, sheet_index: int) -> list[TableCandidate]:
    candidates = []
    for row_index in range(min(40, len(df))):
        headers = [clean_header(value) for value in df.iloc[row_index].tolist()]
        if not any(headers):
            continue
        name_col = _choose_name_col(headers)
        if name_col is None:
            continue
        price_col = _choose_price_col(df, headers, row_index + 1)
        category_cols = [idx for idx, header in enumerate(headers) if header in CATEGORY_HEADERS]
        attribute_cols = [idx for idx, header in enumerate(headers) if header in ATTRIBUTE_HEADERS]
        name_extra_cols = [idx for idx, header in enumerate(headers) if header in NAME_EXTRA_HEADERS]
        score = _header_score(
            df,
            headers,
            row_index,
            sheet_name,
            name_col,
            price_col,
            category_cols,
            attribute_cols,
            name_extra_cols,
        )
        if score >= 118 or (price_col is not None and score >= 104 and not _sheet_discouraged(sheet_name)):
            candidates.append(
                TableCandidate(
                    sheet_name=sheet_name,
                    sheet_index=sheet_index,
                    header_row=row_index,
                    headers=headers,
                    name_col=name_col,
                    price_col=price_col,
                    category_cols=category_cols,
                    attribute_cols=attribute_cols,
                    name_extra_cols=name_extra_cols,
                    score=score,
                    primary=_sheet_primary(sheet_name),
                    discouraged=_sheet_discouraged(sheet_name),
                )
            )
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def _meaningful_category(values: list[str], current: str) -> str:
    for value in values:
        text = clean_cell(value)
        if text and clean_header(text) not in CATEGORY_IGNORE_VALUES:
            return text
    return current


def _looks_like_header_row(row: list[str], candidate: TableCandidate) -> bool:
    if candidate.name_col >= len(row):
        return False
    name = clean_header(row[candidate.name_col])
    if name in NAME_HEADERS:
        return True
    return name in NAME_EXTRA_HEADERS


def _collect_columns(row: list[str], columns: list[int]) -> list[str]:
    values = []
    for col in columns:
        if col < len(row):
            value = clean_cell(row[col])
            if value:
                values.append(value)
    return values


def split_components(name: str, attrs: str) -> list[str]:
    source = unicodedata.normalize("NFKC", f"{name} {attrs}")
    source = re.sub(r"#{2,}", "#", source)
    source = re.sub(r"(口味|份量|规格|主食|基底|自选一|自选二|赠品三选一|味由您定)[:：]?", "#", source)
    source = re.sub(r"(?<!不)(?:包含|内含|含|搭配|配)(?=[\u4e00-\u9fffA-Za-z0-9])", "#", source)
    parts = re.split(r"[+#/／、,，|丨;；\n]+", source)
    out = []
    seen = set()
    for part in parts:
        clean = re.sub(r"[【\[].*?[】\]]", "", part)
        clean = re.sub(r"[（(].*?[）)]", "", clean)
        clean = re.sub(r"^[#\s:：-]+|[#\s:：-]+$", "", clean).strip()
        norm = normalize(clean)
        if len(norm) < 2 or norm in seen or _basic_category_from_text(clean, "", "") in {BASIC_OTHER, BASIC_ADDON, BASIC_STAPLE}:
            continue
        seen.add(norm)
        out.append(clean)
    return out[:8]


def _has_combo_signal(text: str) -> bool:
    if any(word in text for word in COMBO_WORDS):
        return True
    if re.search(r"[+＋&/／、,，|丨;；]", text):
        return True
    return bool(re.search(r"(?<!不)(?:包含|内含|含|搭配)(?=[\u4e00-\u9fffA-Za-z0-9])", text))


def _is_service_text(name_text: str, text: str) -> bool:
    if not text:
        return False
    if any(word in name_text for word in ("餐具", "发票", "包装费", "配送费", "打包盒", "补差价", "差价")):
        return True
    if any(word in name_text for word in ("勿点", "勿拍", "不要点", "不要拍", "温馨提示", "公告", "说明")):
        return True
    if any(word in name_text for word in ("福利", "收藏", "满减", "红包")):
        food_signal = any(word in text for word in BEVERAGE_WORDS + RICE_NOODLE_WORDS + NOODLE_WORDS + SNACK_WORDS + ("肉", "鸡", "鸭", "鱼", "虾", "菜", "粉", "面", "饭", "汤", "粥", "蛋", "豆", "肠", "丸"))
        return not food_signal
    return False


def _is_addon_text(name_text: str, text: str) -> bool:
    if name_text in ADDON_EXACT_WORDS:
        return True
    if any(word in name_text for word in ADDON_WORDS):
        return True
    if re.fullmatch(r".{1,8}(酱|汁|蘸料|调料)", name_text):
        return True
    if any(word in text for word in ("小料", "加料", "配料")) and len(name_text) <= 8:
        return True
    return bool(re.fullmatch(r"(加|另加|单加|配)(鸡蛋|煎蛋|荷包蛋|卤蛋|茶叶蛋|火腿|香肠|肉|菜|粉|面|饭)", name_text))


def _basic_category_from_text(name: str, attrs: str = "", category: str = "") -> str:
    raw = unicodedata.normalize("NFKC", f"{category or ''} {name or ''} {attrs or ''}")
    text = _compact_text(raw)
    name_text = _compact_text(name)
    category_text = _compact_text(category)
    if _is_service_text(name_text, text):
        return BASIC_OTHER
    if _has_combo_signal(raw):
        return BASIC_COMBO
    if any(word in text for word in BEVERAGE_WORDS):
        return BASIC_BEVERAGE
    if _is_addon_text(name_text, text):
        return BASIC_ADDON
    if _is_plain_rice_text(name_text) or category_text == "主食":
        return BASIC_STAPLE
    if any(word in text for word in RICE_NOODLE_WORDS):
        return BASIC_RICE_NOODLE
    if any(word in text for word in PORRIDGE_WORDS):
        return BASIC_PORRIDGE
    if any(word in text for word in NOODLE_WORDS):
        return BASIC_NOODLE
    if "汤" in text and not any(word in text for word in ("汤饭", "汤面", "汤粉", "汤锅", "汤包")):
        return BASIC_SNACK
    if any(word in text for word in SNACK_WORDS):
        return BASIC_SNACK
    return BASIC_MAIN


def classify_basic_category(name: str, attrs: str = "", category: str = "") -> str:
    return _basic_category_from_text(name, attrs, category)


def detect_kind(name: str, attrs: str = "", category: str = "") -> str:
    basic = classify_basic_category(name, attrs, category)
    if basic == BASIC_COMBO:
        return KIND_COMBO
    if basic == BASIC_OTHER:
        return KIND_OTHER
    if basic in {BASIC_BEVERAGE, BASIC_ADDON, BASIC_STAPLE, BASIC_SNACK}:
        return KIND_SNACK
    return KIND_SINGLE


def kind_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    single = sum(1 for item in items if item.get("kind") == KIND_SINGLE)
    combo = sum(1 for item in items if item.get("kind") == KIND_COMBO)
    snack = sum(1 for item in items if item.get("kind") == KIND_SNACK)
    other = max(0, len(items) - single - combo - snack)
    return {"single": single, "combo": combo, "snack": snack, "other": other, "total": len(items)}


def basic_category_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        BASIC_COMBO: 0,
        BASIC_BEVERAGE: 0,
        BASIC_ADDON: 0,
        BASIC_STAPLE: 0,
        BASIC_RICE_NOODLE: 0,
        BASIC_NOODLE: 0,
        BASIC_PORRIDGE: 0,
        BASIC_MAIN: 0,
        BASIC_SNACK: 0,
        BASIC_OTHER: 0,
    }
    for item in items:
        category = str(item.get("basicCategory") or BASIC_OTHER)
        counts[category] = counts.get(category, 0) + 1
    return counts


def _parse_candidate(df: pd.DataFrame, candidate: TableCandidate) -> list[dict[str, Any]]:
    items = []
    current_category = ""
    for row_index in range(candidate.header_row + 1, len(df)):
        row = [clean_cell(value) for value in df.iloc[row_index].tolist()]
        if not any(row):
            continue
        if _looks_like_header_row(row, candidate):
            continue
        name = row[candidate.name_col] if candidate.name_col < len(row) else ""
        name = clean_cell(name)
        if not name:
            continue
        norm = normalize(name)
        if len(norm) < 2:
            continue

        category_values = _collect_columns(row, candidate.category_cols)
        current_category = _meaningful_category(category_values, current_category)
        category = current_category
        price = ""
        if candidate.price_col is not None and candidate.price_col < len(row):
            price = _price_number(row[candidate.price_col])

        attrs = " ".join(_collect_columns(row, candidate.name_extra_cols + candidate.attribute_cols))
        basic_category = classify_basic_category(name, attrs, category)
        item = {
            "row": row_index + 1,
            "sheet": candidate.sheet_name,
            "category": category,
            "name": name,
            "price": price,
            "kind": detect_kind(name, attrs, category),
            "basicCategory": basic_category,
            "norm": norm,
            "components": split_components(name, attrs),
        }
        items.append(item)
    return items


def _select_candidates(candidates: list[TableCandidate]) -> list[TableCandidate]:
    if not candidates:
        return []
    primary = [candidate for candidate in candidates if candidate.primary and not candidate.discouraged]
    pool = primary or [candidate for candidate in candidates if not candidate.discouraged] or candidates

    by_sheet: dict[str, TableCandidate] = {}
    for candidate in pool:
        current = by_sheet.get(candidate.sheet_name)
        if current is None or candidate.score > current.score:
            by_sheet[candidate.sheet_name] = candidate

    selected = list(by_sheet.values())
    selected.sort(key=lambda item: (item.sheet_index, item.header_row))
    if primary:
        return selected

    top = max(candidate.score for candidate in selected)
    return [candidate for candidate in selected if candidate.score >= top - 18]


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = []
    seen = set()
    for item in items:
        key = (
            clean_header(item.get("category", "")),
            item.get("norm", ""),
            _price_number(str(item.get("price", ""))),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def guess_store(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"^运营数据[_-]?", "", stem)
    stem = re.sub(r"[（(]\d+[）)]$", "", stem)
    stem = re.sub(r"(活动及)?菜单方案$", "", stem)
    return stem.strip(" _-") or path.stem


def parse_menu(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.suffix.lower() not in MENU_EXTS:
        raise ValueError(f"不支持的菜单格式: {source.suffix}")
    if not source.exists():
        raise FileNotFoundError(source)

    errors: list[dict[str, str]] = []
    diagnostics: list[str] = []
    all_candidates: list[tuple[TableCandidate, pd.DataFrame]] = []
    with pd.ExcelFile(source) as workbook:
        for sheet_index, sheet_name in enumerate(workbook.sheet_names):
            try:
                df = pd.read_excel(workbook, sheet_name=sheet_name, header=None, dtype=str).fillna("")
            except Exception as exc:
                errors.append({"sheet": sheet_name, "message": str(exc)})
                continue
            candidates = _find_table_candidates(df, sheet_name, sheet_index)
            if not candidates:
                non_empty = sum(1 for row in df.head(40).itertuples(index=False) if any(clean_cell(value) for value in row))
                if non_empty:
                    diagnostics.append(f"{sheet_name}: 前40行未找到菜品名/商品名称/菜单名等表头")
                else:
                    diagnostics.append(f"{sheet_name}: 空表或前40行无有效内容")
            for candidate in candidates:
                all_candidates.append((candidate, df))

    selected = _select_candidates([candidate for candidate, _ in all_candidates])
    selected_ids = {(candidate.sheet_name, candidate.header_row, candidate.name_col) for candidate in selected}
    items: list[dict[str, Any]] = []
    parsed_sheets = []
    for candidate, df in all_candidates:
        if (candidate.sheet_name, candidate.header_row, candidate.name_col) not in selected_ids:
            continue
        parsed = _parse_candidate(df, candidate)
        if not parsed:
            continue
        parsed_sheets.append(
            {
                "sheet": candidate.sheet_name,
                "headerRow": candidate.header_row + 1,
                "items": len(parsed),
                "score": round(candidate.score, 1),
            }
        )
        items.extend(parsed)

    items = _dedupe_items(items)
    if not items:
        detail = "; ".join(f"{err['sheet']}: {err['message']}" for err in errors)
        if all_candidates and not detail:
            selected_summary = ", ".join(f"{candidate.sheet_name}@{candidate.header_row + 1}" for candidate in selected) or "无"
            detail = f"识别到候选表头但有效菜品为空或菜品名列为空；候选={selected_summary}"
        elif not detail:
            detail = "; ".join(diagnostics[:6]) or "未找到可识别的菜单表头"
        raise ValueError(f"未能从菜单中解析出菜品: {source.name} ({detail})")

    return {
        "store": guess_store(source),
        "file": source.name,
        "count": len(items),
        "kindCounts": kind_counts(items),
        "basicCategoryCounts": basic_category_counts(items),
        "items": items,
        "sheets": parsed_sheets,
        "errors": errors,
        "demo": False,
    }


def iter_menu_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(root)
    return sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in MENU_EXTS)


def audit_menus(directory: str | Path) -> dict[str, Any]:
    root = Path(directory)
    records = []
    failures = []
    for path in iter_menu_files(root):
        try:
            menu = parse_menu(path)
        except Exception as exc:
            failures.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        records.append(
            {
                "store": menu["store"],
                "file": menu["file"],
                "count": menu["count"],
                "kindCounts": menu["kindCounts"],
                "basicCategoryCounts": menu.get("basicCategoryCounts", {}),
                "sheets": menu.get("sheets", []),
                "errors": menu.get("errors", []),
            }
        )
    return {
        "directory": str(root),
        "files": len(records) + len(failures),
        "parsed": len(records),
        "failed": len(failures),
        "totalItems": sum(record["count"] for record in records),
        "menus": records,
        "errors": failures,
    }


def _format_sheet_summary(sheets: list[dict[str, Any]]) -> str:
    if not sheets:
        return "sheet=?"
    return ", ".join(f"{sheet['sheet']}@{sheet['headerRow']}:{sheet['items']}" for sheet in sheets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit local xls/xlsx menu parsing.")
    parser.add_argument("directory", nargs="?", default=str(DEFAULT_MENU_DIR), help="Directory containing menu xls/xlsx files.")
    args = parser.parse_args(argv)

    try:
        audit = audit_menus(args.directory)
    except Exception as exc:
        print(f"ERR {args.directory}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for menu in audit["menus"]:
        counts = menu["kindCounts"]
        basic_counts = menu.get("basicCategoryCounts", {})
        basic_summary = ",".join(f"{key}:{value}" for key, value in basic_counts.items() if value)
        sheet_summary = _format_sheet_summary(menu.get("sheets", []))
        suffix = ""
        if menu.get("errors"):
            suffix = f" warnings={len(menu['errors'])}"
        print(
            f"OK  {menu['file']} | count={menu['count']} "
            f"single={counts['single']} combo={counts['combo']} snack={counts['snack']} other={counts.get('other', 0)} "
            f"| basic={basic_summary or 'none'} | {sheet_summary}{suffix}"
        )
    for error in audit["errors"]:
        print(f"ERR {error['file']} | {error['error']}")
    print(
        f"SUMMARY files={audit['files']} parsed={audit['parsed']} failed={audit['failed']} "
        f"totalItems={audit['totalItems']}"
    )
    return 1 if audit["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
