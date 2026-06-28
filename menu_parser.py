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


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,30}[）)]", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|克|g|斤|个|只|份|瓶|罐|串|枚|盒|杯|碗)", "", text)
    for word in [
        "招牌",
        "爆款",
        "热销",
        "福利",
        "收藏",
        "现炒",
        "现煎",
        "盖码饭",
        "盖浇饭",
        "木桶饭",
        "套餐",
        "单人餐",
        "米饭",
    ]:
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
    parts = re.split(r"[+#/／、,，|丨;；\n]+", source)
    out = []
    seen = set()
    for part in parts:
        clean = re.sub(r"[【\[].*?[】\]]", "", part)
        clean = re.sub(r"[（(].*?[）)]", "", clean)
        clean = re.sub(r"^[#\s:：-]+|[#\s:：-]+$", "", clean).strip()
        norm = normalize(clean)
        if len(norm) < 2 or norm in seen:
            continue
        seen.add(norm)
        out.append(clean)
    return out[:8]


def detect_kind(name: str, attrs: str = "", category: str = "") -> str:
    text = unicodedata.normalize("NFKC", f"{category} {name} {attrs}")
    name_text = unicodedata.normalize("NFKC", f"{category} {name}")
    combo_words = [
        "套餐",
        "组合",
        "双拼",
        "三拼",
        "四拼",
        "多拼",
        "自选",
        "任选",
        "多人餐",
        "单人餐",
        "大礼包",
        "全家桶",
        "+",
    ]
    if any(word in text for word in combo_words):
        return KIND_COMBO

    snack_words = [
        "可乐",
        "雪碧",
        "芬达",
        "王老吉",
        "冰红茶",
        "矿泉水",
        "椰子水",
        "豆浆",
        "果汁",
        "柠檬茶",
        "酸梅汤",
        "饮品",
        "饮料",
        "小食",
        "小吃",
        "甜品",
        "冰沙",
        "酸奶",
        "茶叶蛋",
        "溏心蛋",
        "煎蛋",
        "荷包蛋",
        "泡菜",
        "蘸水",
        "沙拉汁",
    ]
    if any(word in name_text for word in snack_words):
        return KIND_SNACK
    norm = normalize(name)
    if re.search(r"(米饭|白饭|珍珠饭|杂粮饭|糙米饭)$", norm) and len(norm) <= 8:
        return KIND_SNACK
    if re.search(r"(酱|汁|蘸料)$", norm) and len(norm) <= 8:
        return KIND_SNACK
    if "汤" in text and not any(word in text for word in ["汤饭", "汤面", "汤粉", "汤锅", "汤包"]):
        return KIND_SNACK
    return KIND_SINGLE


def kind_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    single = sum(1 for item in items if item.get("kind") == KIND_SINGLE)
    combo = sum(1 for item in items if item.get("kind") == KIND_COMBO)
    snack = max(0, len(items) - single - combo)
    return {"single": single, "combo": combo, "snack": snack, "total": len(items)}


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
        item = {
            "row": row_index + 1,
            "sheet": candidate.sheet_name,
            "category": category,
            "name": name,
            "price": price,
            "kind": detect_kind(name, attrs, category),
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
    all_candidates: list[tuple[TableCandidate, pd.DataFrame]] = []
    with pd.ExcelFile(source) as workbook:
        for sheet_index, sheet_name in enumerate(workbook.sheet_names):
            try:
                df = pd.read_excel(workbook, sheet_name=sheet_name, header=None, dtype=str).fillna("")
            except Exception as exc:
                errors.append({"sheet": sheet_name, "message": str(exc)})
                continue
            for candidate in _find_table_candidates(df, sheet_name, sheet_index):
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
        raise ValueError(f"未能从菜单中解析出菜品: {source.name}" + (f" ({detail})" if detail else ""))

    return {
        "store": guess_store(source),
        "file": source.name,
        "count": len(items),
        "kindCounts": kind_counts(items),
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
        sheet_summary = _format_sheet_summary(menu.get("sheets", []))
        suffix = ""
        if menu.get("errors"):
            suffix = f" warnings={len(menu['errors'])}"
        print(
            f"OK  {menu['file']} | count={menu['count']} "
            f"single={counts['single']} combo={counts['combo']} snack={counts['snack']} | {sheet_summary}{suffix}"
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
