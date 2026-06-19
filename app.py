from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageFont, ImageOps

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LIBRARY_DIR = DATA_DIR / "library"
EXPORT_DIR = DATA_DIR / "exports"
for folder in (UPLOAD_DIR, LIBRARY_DIR, EXPORT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MENU_EXTS = {".xls", ".xlsx"}
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024


@dataclass
class LibraryImage:
    image_id: str
    path: Path
    store: str
    dish: str
    norm: str
    grams: set[str]
    style_id: str


DEMO_MENU = [
    ("热销", "老长沙辣椒炒肉盖码饭", "19.8", "单品"),
    ("热销", "小炒黄牛肉盖码饭", "25.8", "单品"),
    ("热销", "农家一碗香盖码饭", "19.8", "单品"),
    ("热销", "茄子肉末盖码饭", "18.8", "单品"),
    ("折扣", "香干炒肉盖码饭", "18.8", "单品"),
    ("折扣", "酱辣椒炒鸡盖码饭", "21.8", "单品"),
    ("套餐", "辣椒炒肉+茄子肉末盖码饭", "24.8", "套餐/组合"),
    ("套餐", "小炒黄牛肉+手撕包菜套餐", "29.8", "套餐/组合"),
    ("小吃饮品", "紫菜蛋花汤", "3.9", "饮品/小食"),
    ("小吃饮品", "康师傅冰红茶", "4.0", "饮品/小食"),
]

DEMO_DISHES = [
    "老长沙辣椒炒肉盖码饭",
    "辣椒小炒肉盖饭",
    "小炒黄牛肉盖码饭",
    "农家一碗香盖码饭",
    "茄子肉末盖码饭",
    "香干炒肉盖码饭",
    "酱辣椒炒鸡盖码饭",
    "手撕包菜",
    "紫菜蛋花汤",
    "康师傅冰红茶",
]

STYLE_COLORS = {
    "style-1": ("原木暖色背景", (238, 205, 155), (173, 102, 42)),
    "style-2": ("黑石板背景", (60, 64, 67), (218, 187, 121)),
    "style-3": ("浅灰极简背景", (229, 232, 235), (90, 116, 132)),
    "style-4": ("红色促销背景", (181, 44, 39), (255, 221, 148)),
    "style-5": ("竹编自然背景", (210, 184, 122), (84, 136, 84)),
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,30}[）)]", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|克|g|斤|个|只|份|瓶|罐)", "", text)
    for word in ["招牌", "爆款", "热销", "福利", "现炒", "现煎", "盖码饭", "盖浇饭", "木桶饭", "套餐", "单人餐", "米饭"]:
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


def similarity(menu_name: str, image_name: str, menu_norm: str, image_norm: str, menu_grams: set[str], image_grams: set[str]) -> float:
    if not menu_norm or not image_norm:
        return 0.0
    seq = SequenceMatcher(None, menu_norm, image_norm).ratio()
    jac = len(menu_grams & image_grams) / max(1, len(menu_grams | image_grams))
    contains = 0.22 if menu_norm in image_norm or image_norm in menu_norm else 0.0
    return min(1.0, seq * 0.48 + jac * 0.42 + contains)


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[/:*?\"<>|\\]+", "_", name)
    return re.sub(r"\s+", " ", name).strip()[:90] or "file"


def draw_demo_image(path: Path, dish: str, style_id: str) -> None:
    style_name, bg, accent = STYLE_COLORS[style_id]
    img = Image.new("RGB", (900, 720), bg)
    draw = ImageDraw.Draw(img)
    draw.ellipse((190, 100, 710, 620), fill=(248, 248, 244), outline=accent, width=14)
    draw.ellipse((245, 155, 655, 565), fill=(250, 244, 225))
    for idx, color in enumerate([(190, 68, 42), (70, 145, 68), (228, 170, 60), (125, 77, 44), (230, 230, 210)]):
        x0 = 300 + (idx % 3) * 78
        y0 = 225 + (idx // 3) * 95
        draw.rounded_rectangle((x0, y0, x0 + 180, y0 + 76), radius=28, fill=color)
    try:
        font_big = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 42)
        font_small = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 24)
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
    draw.rounded_rectangle((40, 42, 860, 118), radius=24, fill=(255, 255, 255))
    draw.text((70, 58), dish, fill=(33, 38, 45), font=font_big)
    draw.text((60, 650), style_name, fill=(33, 38, 45), font=font_small)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=88)


def ensure_demo_data() -> None:
    marker = LIBRARY_DIR / ".demo_ready"
    if marker.exists() and any(LIBRARY_DIR.rglob("*.jpg")):
        return
    for style_id in STYLE_COLORS:
        for dish in DEMO_DISHES:
            draw_demo_image(LIBRARY_DIR / "demo_store" / style_id / f"{dish}.jpg", dish, style_id)
    marker.write_text(str(time.time()), encoding="utf-8")


def current_menu_path() -> Path | None:
    files = sorted((p for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in MENU_EXTS), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def demo_menu_items() -> list[dict[str, Any]]:
    return [
        {
            "row": idx + 1,
            "category": cat,
            "name": name,
            "price": price,
            "kind": kind,
            "norm": normalize(name),
            "components": split_components(name, ""),
        }
        for idx, (cat, name, price, kind) in enumerate(DEMO_MENU)
    ]


def split_components(name: str, attrs: str) -> list[str]:
    parts = re.split(r"[+#/／、,，|丨]+", unicodedata.normalize("NFKC", f"{name} {attrs}"))
    out = []
    for part in parts:
        clean = re.sub(r"[【\[].*?[】\]]", "", part)
        clean = re.sub(r"[（(].*?[）)]", "", clean).strip()
        if len(normalize(clean)) >= 2:
            out.append(clean)
    return out[:8]


def detect_kind(name: str, attrs: str) -> str:
    text = f"{name} {attrs}"
    if any(x in text for x in ["套餐", "双拼", "三拼", "四拼", "+", "自选", "任选", "组合"]):
        return "套餐/组合"
    if any(x in text for x in ["可乐", "雪梨", "矿泉水", "饮品", "豆浆", "果汁", "王老吉", "芬达", "冰红茶", "雪碧", "汤"]):
        return "饮品/小食"
    return "单品"


def parse_menu(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = current_menu_path()
    if path is None:
        items = demo_menu_items()
        return {"file": "demo_menu.xlsx", "store": "演示盖码饭门店", "count": len(items), "items": items, "demo": True}
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str).fillna("")
    header_row = 0
    name_col = None
    headers = []
    for ridx in range(min(12, len(raw))):
        row = [str(v).strip() for v in raw.iloc[ridx].tolist()]
        for cidx, value in enumerate(row):
            if value in {"菜单名", "菜品名", "商品名", "商品名称", "名称"}:
                header_row = ridx
                name_col = cidx
                headers = row
                break
        if name_col is not None:
            break
    if name_col is None:
        name_col = 0
        headers = [f"列{i + 1}" for i in range(raw.shape[1])]
    cat_col = next((i for i, h in enumerate(headers) if h in {"一级分类", "分类", "分类名", "品类"}), None)
    price_col = next((i for i, h in enumerate(headers) if h in {"活动价", "价格", "售价", "原价"}), None)
    attr_col = next((i for i, h in enumerate(headers) if h in {"属性", "规格", "规格名"}), None)
    items = []
    seen = set()
    for ridx in range(header_row + 1, len(raw)):
        row = [str(v).strip() for v in raw.iloc[ridx].tolist()]
        name = row[name_col] if name_col < len(row) else ""
        norm = normalize(name)
        if len(norm) < 2:
            continue
        cat = row[cat_col] if cat_col is not None and cat_col < len(row) else ""
        price = row[price_col] if price_col is not None and price_col < len(row) else ""
        attrs = row[attr_col] if attr_col is not None and attr_col < len(row) else ""
        key = f"{cat}|{name}|{price}"
        if key in seen:
            continue
        seen.add(key)
        items.append({"row": ridx + 1, "category": cat, "name": name, "price": price, "kind": detect_kind(name, attrs), "norm": norm, "components": split_components(name, attrs)})
    return {"file": path.name, "store": re.sub(r"^运营数据_", "", path.stem), "count": len(items), "items": items, "demo": False}


def library_images() -> list[LibraryImage]:
    ensure_demo_data()
    images = []
    for path in sorted(LIBRARY_DIR.rglob("*")):
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = path.relative_to(LIBRARY_DIR)
        parts = rel.parts
        store = parts[0] if len(parts) > 1 else "uploaded"
        style_id = next((p for p in parts if p.startswith("style-")), "style-upload")
        dish = path.stem
        norm = normalize(dish)
        if not norm:
            continue
        images.append(LibraryImage(hashlib.sha1(str(path).encode()).hexdigest()[:18], path, store, dish, norm, grams(norm), style_id))
    return images


def category_report(menu: dict[str, Any]) -> dict[str, Any]:
    text = " ".join([menu["store"]] + [item["name"] for item in menu["items"]])
    rules = [
        ("盖码饭/盖浇饭", ["盖码饭", "盖浇饭", "木桶饭", "辣椒炒肉", "黄牛肉", "现炒"]),
        ("米粉/米线", ["米粉", "米线", "螺蛳粉", "酸辣粉"]),
        ("炒菜/川湘菜", ["川菜", "湘菜", "小炒", "水煮", "回锅肉", "鱼香肉丝"]),
        ("轻食健康餐", ["轻食", "沙拉", "健康餐", "杂粮饭", "鸡胸"]),
        ("炸鸡/韩餐", ["炸鸡", "韩式", "火鸡面", "年糕", "石锅"]),
    ]
    scores = [{"name": name, "score": sum(text.count(w) for w in words)} for name, words in rules]
    scores.sort(key=lambda x: x["score"], reverse=True)
    best = scores[0] if scores and scores[0]["score"] else {"name": "待人工确认", "score": 0}
    return {"category": best["name"], "confidence": min(96, 55 + best["score"] * 8) if best["score"] else 38, "candidates": scores[:5]}


def standardization_report(menu: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in menu["items"]:
        groups.setdefault(item["norm"], []).append(item)
    samples = []
    for canonical, rows in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
        samples.append({"canonical": canonical, "count": len(rows), "examples": [r["name"] for r in rows[:4]]})
    return {"rawItems": len(menu["items"]), "canonicalItems": len(groups), "aliasMerged": sum(max(0, len(v) - 1) for v in groups.values()), "samples": samples}


def top_candidates(item: dict[str, Any], library: list[LibraryImage], limit: int = 6) -> list[dict[str, Any]]:
    item_grams = grams(item["norm"])
    scored = []
    for image in library:
        score = similarity(item["name"], image.dish, item["norm"], image.norm, item_grams, image.grams)
        if score >= 0.15:
            scored.append((score, image))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "imageId": image.image_id,
            "score": round(score * 100, 1),
            "dishName": image.dish,
            "store": image.store,
            "styleId": image.style_id,
            "url": f"/media/{image.path.relative_to(LIBRARY_DIR).as_posix()}",
            "path": str(image.path),
        }
        for score, image in scored[:limit]
    ]


def style_options(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    style_ids = sorted({c["styleId"] for r in results for c in r["candidates"] if c["styleId"]})
    options = []
    total = max(1, len(results))
    for idx, style_id in enumerate(style_ids[:5], start=1):
        direct = review = bg_replace = custom = 0
        sample = None
        for row in results:
            candidates = row["candidates"]
            if not candidates:
                custom += 1
                continue
            same = next((c for c in candidates if c["styleId"] == style_id), None)
            sample = sample or same
            if same and row["status"] == "直接可用":
                direct += 1
            elif same:
                review += 1
            else:
                bg_replace += 1
        style_name = STYLE_COLORS.get(style_id, (f"上传风格 {idx}", (230, 230, 230), (80, 80, 80)))[0]
        color = STYLE_COLORS.get(style_id, ("", (230, 230, 230), (80, 80, 80)))[1]
        options.append(
            {
                "id": style_id,
                "name": style_name,
                "count": sum(1 for r in results for c in r["candidates"] if c["styleId"] == style_id),
                "sample": sample,
                "color": f"rgb({color[0]},{color[1]},{color[2]})",
                "direct": direct,
                "review": review,
                "bgReplace": bg_replace,
                "custom": custom,
                "directRate": round(direct / total * 100, 1),
                "processingRate": round((review + bg_replace) / total * 100, 1),
                "customRate": round(custom / total * 100, 1),
                "estimatedPoints": direct * 10 + review * 12 + bg_replace * 18 + custom * 49,
            }
        )
    return sorted(options, key=lambda x: x["direct"], reverse=True)


def status_for(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "未找到"
    if candidates[0]["score"] >= 70:
        return "直接可用"
    if candidates[0]["score"] >= 45:
        return "需人工确认"
    return "弱匹配"


def points_for(status: str, action: str, kind: str) -> int:
    if status == "未找到":
        return 49
    points = 10
    if kind == "套餐/组合":
        points += 8
    if action == "需抠图换背景":
        points += 8
    if status != "直接可用":
        points += 2
    return points


def build_plan(selected_style: str = "") -> dict[str, Any]:
    menu = parse_menu()
    library = library_images()
    results = []
    for item in menu["items"]:
        candidates = top_candidates(item, library)
        results.append({**item, "status": status_for(candidates), "candidates": candidates})
    styles = style_options(results)
    selected_style = selected_style or (styles[0]["id"] if styles else "")
    for row in results:
        candidates = row["candidates"]
        if selected_style:
            same = next((c for c in candidates if c["styleId"] == selected_style), None)
            if same:
                candidates.insert(0, candidates.pop(candidates.index(same)))
        chosen = candidates[0] if candidates else None
        action = "需要定制/生成" if not chosen else "背景一致，直接复用" if chosen["styleId"] == selected_style else "需抠图换背景"
        row["backgroundAction"] = action
        row["points"] = points_for(row["status"], action, row["kind"])
    summary = {
        "total": len(results),
        "direct": sum(1 for r in results if r["status"] == "直接可用"),
        "review": sum(1 for r in results if r["status"] in {"需人工确认", "弱匹配"}),
        "missing": sum(1 for r in results if r["status"] == "未找到"),
        "reuse": sum(1 for r in results if r["backgroundAction"] == "背景一致，直接复用"),
        "bgReplace": sum(1 for r in results if r["backgroundAction"] == "需抠图换背景"),
        "custom": sum(1 for r in results if r["backgroundAction"] == "需要定制/生成"),
        "points": sum(r["points"] for r in results),
    }
    price = 69 if summary["total"] <= 60 else 99 if summary["total"] <= 80 else 199
    return {
        "menu": {k: v for k, v in menu.items() if k != "items"},
        "category": category_report(menu),
        "standardization": standardization_report(menu),
        "assetLayer": {"libraryImages": len(library), "libraryStores": len({x.store for x in library}), "menus": len(list(UPLOAD_DIR.glob('*.xlsx'))) or 1},
        "styles": styles,
        "selectedStyle": selected_style,
        "summary": summary,
        "quote": {
            "package": "基础整店出图" if price == 69 else "标准品牌套图" if price == 99 else "高级定制套图",
            "cash": price,
            "points": price * 10,
            "rate": "1 元 = 10 积分",
            "addOns": [
                {"name": "全店品牌水印", "price": 19.9},
                {"name": "全店换餐具", "price": 29.9},
                {"name": "单张定制配菜", "price": "4.9-9.9/张"},
                {"name": "套餐组合图增强", "price": 19.9},
            ],
            "referral": {"registerReward": 100, "firstPayReward": "20% 积分返利，封顶 500 积分", "expireDays": 180},
        },
        "results": results,
    }


def export_zip(selected_style: str, scope: str = "all") -> dict[str, Any]:
    plan = build_plan(selected_style)
    run_dir = EXPORT_DIR / f"export_{int(time.time())}"
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    images = 0
    for idx, row in enumerate(plan["results"], start=1):
        candidate = row["candidates"][0] if row["candidates"] else None
        if scope == "direct" and row["backgroundAction"] != "背景一致，直接复用":
            continue
        if scope == "need_bg" and row["backgroundAction"] != "需抠图换背景":
            continue
        if scope == "missing" and candidate is not None:
            continue
        if scope == "single" and row["kind"] != "单品":
            continue
        if scope == "combo" and row["kind"] != "套餐/组合":
            continue
        copied = ""
        if candidate:
            src = Path(candidate["path"])
            target = image_dir / f"{idx:03d}_{safe_filename(row['name'])}{src.suffix}"
            shutil.copy2(src, target)
            copied = str(target)
            images += 1
        rows.append({"菜单菜品名": row["name"], "分类": row["category"], "类型": row["kind"], "匹配状态": row["status"], "背景处理": row["backgroundAction"], "预计积分": row["points"], "匹配图库菜品": candidate["dishName"] if candidate else "", "图库来源": candidate["store"] if candidate else "", "导出路径": copied})
    report = run_dir / "match_report.xlsx"
    pd.DataFrame(rows).to_excel(report, index=False)
    zip_path = run_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(report, report.name)
        for file in image_dir.glob("*"):
            zf.write(file, f"images/{file.name}")
    return {"rows": len(rows), "images": images, "download": f"/download/{zip_path.relative_to(EXPORT_DIR).as_posix()}"}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/plan")
def api_plan():
    return jsonify(build_plan(request.args.get("style", "")))


@app.get("/api/menu-status")
def api_menu_status():
    path = current_menu_path()
    if path is None:
        return jsonify({"uploaded": False})
    menu = parse_menu(path)
    return jsonify({"uploaded": True, "menu": {k: v for k, v in menu.items() if k != "items"}})


@app.post("/api/upload-menu")
def upload_menu():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "没有收到菜单文件"}), 400
    if Path(file.filename).suffix.lower() not in MENU_EXTS:
        return jsonify({"error": "请上传 .xls 或 .xlsx 格式的 Excel 菜单"}), 400
    target = UPLOAD_DIR / f"menu_{int(time.time())}_{safe_filename(file.filename)}"
    file.save(target)
    try:
        menu = parse_menu(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        return jsonify({"error": f"菜单读取失败：{exc}"}), 400
    return jsonify({"ok": True, "file": target.name, "menu": {k: v for k, v in menu.items() if k != "items"}})


@app.post("/api/upload-library")
def upload_library():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "没有收到图库 zip"}), 400
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "请上传 zip 文件"}), 400
    batch = LIBRARY_DIR / f"uploaded_{int(time.time())}"
    batch.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO(file.read())
    with zipfile.ZipFile(raw) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            suffix = Path(member.filename).suffix.lower()
            if suffix not in IMAGE_EXTS:
                continue
            name = safe_filename(Path(member.filename).name)
            target = batch / "style-upload" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return jsonify({"ok": True, "plan": build_plan()})


@app.post("/api/export")
def api_export():
    payload = request.get_json(silent=True) or {}
    return jsonify(export_zip(str(payload.get("style", "")), str(payload.get("scope", "all"))))


@app.get("/media/<path:name>")
def media(name: str):
    return send_from_directory(LIBRARY_DIR, name)


@app.get("/download/<path:name>")
def download(name: str):
    return send_file(EXPORT_DIR / name, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port)
