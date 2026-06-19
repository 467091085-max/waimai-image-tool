from __future__ import annotations

import csv
import base64
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
POINT_RATE = 10
BASE_IMAGE_POINTS = 10
CUSTOM_EDIT_POINTS = 10
WATERMARK_POINTS = 50
EXTRA_PLATFORM_POINTS = 100
PREVIEW_SAMPLE_COUNT = 5
DEMO_BALANCE_POINTS = int(os.environ.get("DEMO_BALANCE_POINTS", "1880"))
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

PLATFORMS = {
    "meituan": {"name": "美团外卖", "width": 800, "height": 600, "default": True},
    "taobao": {"name": "淘宝", "width": 800, "height": 800, "default": False},
    "jd": {"name": "京东", "width": 800, "height": 800, "default": False},
}


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
    style_name, bg, accent = STYLE_COLORS.get(style_id, ("统一出图风格", (232, 235, 238), (85, 103, 120)))
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


def kind_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    single = sum(1 for item in items if item.get("kind") == "单品")
    combo = sum(1 for item in items if item.get("kind") == "套餐/组合")
    snack = max(0, len(items) - single - combo)
    return {"single": single, "combo": combo, "snack": snack, "total": len(items)}


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
        return {"file": "demo_menu.xlsx", "store": "演示盖码饭门店", "count": len(items), "kindCounts": kind_counts(items), "items": items, "demo": True}
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
    return {"file": path.name, "store": re.sub(r"^运营数据_", "", path.stem), "count": len(items), "kindCounts": kind_counts(items), "items": items, "demo": False}


def library_images() -> list[LibraryImage]:
    ensure_demo_data()
    images = []
    for path in sorted(LIBRARY_DIR.rglob("*")):
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = path.relative_to(LIBRARY_DIR)
        parts = rel.parts
        if parts and parts[0].startswith("_"):
            continue
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


def candidate_from_path(path: Path, dish: str, style_id: str, source: str, score: float = 100.0) -> dict[str, Any]:
    return {
        "imageId": hashlib.sha1(str(path).encode()).hexdigest()[:18],
        "score": score,
        "dishName": dish,
        "store": source,
        "styleId": style_id,
        "url": f"/media/{path.relative_to(LIBRARY_DIR).as_posix()}",
        "path": str(path),
        "generated": source.startswith("generated"),
    }


def style_sample_candidate(style_id: str) -> dict[str, Any]:
    sample = next((p for p in sorted((LIBRARY_DIR / "demo_store" / style_id).glob("*.jpg"))), None)
    if sample is None:
        sample = LIBRARY_DIR / "_style_samples" / style_id / "整店风格预览.jpg"
        if not sample.exists():
            draw_demo_image(sample, "整店风格预览", style_id)
    return candidate_from_path(sample, "整店风格预览", style_id, "generated-sample")


def generated_preview_candidate(item: dict[str, Any], style_id: str) -> dict[str, Any] | None:
    if not style_id:
        return None
    target = LIBRARY_DIR / "_generated_previews" / style_id / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    if not target.exists():
        draw_demo_image(target, item["name"], style_id)
    return candidate_from_path(target, item["name"], style_id, "generated-preview", 99.9)


def style_options(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    style_ids = sorted({c["styleId"] for r in results for c in r["candidates"] if c["styleId"]})
    if not style_ids:
        style_ids = list(STYLE_COLORS)
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
        sample = sample or style_sample_candidate(style_id)
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
    return BASE_IMAGE_POINTS


def free_rework_quota(total: int) -> int:
    if total <= 0:
        return 0
    return min(10, max(3, (total + 19) // 20))


def pricing_payload(total: int = 0) -> dict[str, Any]:
    return {
        "rate": f"1 元 = {POINT_RATE} 积分",
        "previewFreeImages": PREVIEW_SAMPLE_COUNT,
        "baseImagePoints": BASE_IMAGE_POINTS,
        "baseImageCash": round(BASE_IMAGE_POINTS / POINT_RATE, 2),
        "customEditPoints": CUSTOM_EDIT_POINTS,
        "customEditCash": round(CUSTOM_EDIT_POINTS / POINT_RATE, 2),
        "watermarkPoints": WATERMARK_POINTS,
        "watermarkCash": round(WATERMARK_POINTS / POINT_RATE, 2),
        "extraPlatformPoints": EXTRA_PLATFORM_POINTS,
        "platforms": PLATFORMS,
        "freeReworkQuota": free_rework_quota(total),
        "manualRetouch": {
            "name": "复杂人工精修",
            "rule": "主体严重错误、需要人工审美判断或多轮局部合成时进入人工工单",
            "delivery": "人工处理后回传成图",
            "price": "按难度报价",
        },
    }


def account_payload() -> dict[str, Any]:
    return {
        "balance": DEMO_BALANCE_POINTS,
        "rate": f"1 元 = {POINT_RATE} 积分",
        "packages": [
            {"name": "体验充值", "cash": 49, "points": 490, "bonus": 0},
            {"name": "整店常用", "cash": 99, "points": 990, "bonus": 80},
            {"name": "小团队包", "cash": 299, "points": 2990, "bonus": 360},
        ],
        "referral": {"registerReward": 100, "firstPayReward": "20% 积分返利，封顶 500 积分", "expireDays": 180},
        "pricing": pricing_payload(),
    }


def pipeline_payload() -> dict[str, Any]:
    provider = os.environ.get("IMAGE_EDIT_PROVIDER", "mock")
    return {
        "provider": provider,
        "imageEditApiReady": bool(os.environ.get("IMAGE_EDIT_API_KEY")),
        "objectStorageReady": bool(os.environ.get("OBJECT_STORAGE_BUCKET")),
        "expectedEnv": ["IMAGE_EDIT_PROVIDER", "IMAGE_EDIT_API_KEY", "OBJECT_STORAGE_BUCKET"],
        "stages": ["菜单解析", "风格确认", "图库匹配", "统一背景", "预览导出"],
    }


def preview_samples(selected_style: str) -> dict[str, Any]:
    menu = parse_menu()
    library = library_images()
    samples = []
    for item in menu["items"][:PREVIEW_SAMPLE_COUNT]:
        candidates = top_candidates(item, library)
        same = next((c for c in candidates if c["styleId"] == selected_style), None)
        candidate = same or generated_preview_candidate(item, selected_style)
        samples.append({**item, "candidate": candidate, "points": 0, "publicStatus": "免费样图"})
    return {
        "style": selected_style,
        "styleName": STYLE_COLORS.get(selected_style, ("上传风格", None, None))[0],
        "samples": samples,
        "previewFreeImages": PREVIEW_SAMPLE_COUNT,
    }


def build_plan(selected_style: str = "") -> dict[str, Any]:
    menu = parse_menu()
    library = library_images()
    requested_style = selected_style
    results = []
    for item in menu["items"]:
        candidates = top_candidates(item, library)
        original_status = status_for(candidates)
        results.append({**item, "status": original_status, "originalStatus": original_status, "candidates": candidates})
    styles = style_options(results)
    selected_style = selected_style or (styles[0]["id"] if styles else "")
    for row in results:
        candidates = row["candidates"]
        if requested_style:
            same = next((c for c in candidates if c["styleId"] == selected_style), None)
            if same:
                candidates.insert(0, candidates.pop(candidates.index(same)))
            elif candidates:
                generated = generated_preview_candidate(row, selected_style)
                if generated:
                    candidates.insert(0, generated)
            else:
                generated = generated_preview_candidate(row, selected_style)
                if generated:
                    candidates.append(generated)
        chosen = candidates[0] if candidates else None
        if not chosen:
            action = "需要定制/生成"
        elif chosen.get("generated") and row["originalStatus"] == "未找到":
            action = "智能补图"
        elif chosen.get("generated"):
            action = "智能统一风格"
        else:
            action = "背景一致，直接复用" if chosen["styleId"] == selected_style else "需抠图换背景"
        row["backgroundAction"] = action
        row["publicStatus"] = "已生成" if chosen else "待补图"
        row["points"] = points_for(row["status"], action, row["kind"])
    summary = {
        "total": len(results),
        "direct": sum(1 for r in results if r["backgroundAction"] == "背景一致，直接复用"),
        "review": sum(1 for r in results if r["backgroundAction"] in {"智能统一风格", "需抠图换背景"}),
        "missing": sum(1 for r in results if r["backgroundAction"] in {"智能补图", "需要定制/生成"}),
        "reuse": sum(1 for r in results if r["backgroundAction"] == "背景一致，直接复用"),
        "bgReplace": sum(1 for r in results if r["backgroundAction"] in {"智能统一风格", "需抠图换背景"}),
        "custom": sum(1 for r in results if r["backgroundAction"] in {"智能补图", "需要定制/生成"}),
        "points": len(results) * BASE_IMAGE_POINTS,
    }
    menu_count = sum(1 for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in MENU_EXTS) or 1
    pricing = pricing_payload(summary["total"])
    return {
        "menu": {k: v for k, v in menu.items() if k != "items"},
        "category": category_report(menu),
        "standardization": standardization_report(menu),
        "assetLayer": {"libraryImages": len(library), "libraryStores": len({x.store for x in library}), "menus": menu_count},
        "styles": styles,
        "selectedStyle": selected_style,
        "summary": summary,
        "account": account_payload(),
        "pipeline": pipeline_payload(),
        "pricing": pricing,
        "quote": {
            "package": "按张正式出图",
            "cash": round(summary["points"] / POINT_RATE, 2),
            "points": summary["points"],
            "rate": f"1 元 = {POINT_RATE} 积分",
            "addOns": [
                {"name": "风格预览", "price": f"免费 {PREVIEW_SAMPLE_COUNT} 张样图"},
                {"name": "正式出图", "price": f"{BASE_IMAGE_POINTS} 积分/张"},
                {"name": "自定义修改", "price": f"{CUSTOM_EDIT_POINTS} 积分/张"},
                {"name": "品牌水印", "price": f"{WATERMARK_POINTS} 积分/单"},
                {"name": "增加平台尺寸", "price": f"{EXTRA_PLATFORM_POINTS} 积分/平台"},
                {"name": "免费重做额度", "price": f"{pricing['freeReworkQuota']} 张/单"},
                {"name": "复杂人工精修", "price": "人工报价"},
            ],
            "referral": {"registerReward": 100, "firstPayReward": "20% 积分返利，封顶 500 积分", "expireDays": 180},
        },
        "results": results,
    }


def font(size: int):
    for path in ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc"]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def text_size(draw: ImageDraw.ImageDraw, text: str, mark_font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=mark_font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def watermark_position(base_size: tuple[int, int], mark_size: tuple[int, int], position: str, margin: int) -> tuple[int, int]:
    bw, bh = base_size
    mw, mh = mark_size
    positions = {
        "top-left": (margin, margin),
        "top-right": (bw - mw - margin, margin),
        "bottom-left": (margin, bh - mh - margin),
        "bottom-right": (bw - mw - margin, bh - mh - margin),
        "center": ((bw - mw) // 2, (bh - mh) // 2),
    }
    return positions.get(position, positions["bottom-right"])


def make_text_watermark(text: str, width: int) -> Image.Image:
    mark_font = font(max(24, width // 28))
    probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    tw, th = text_size(draw, text, mark_font)
    pad_x = max(4, width // 160)
    pad_y = max(3, width // 220)
    mark = Image.new("RGBA", (tw + pad_x * 2, th + pad_y * 2), (0, 0, 0, 0))
    mark_draw = ImageDraw.Draw(mark)
    mark_draw.text((pad_x, pad_y), text, fill=(24, 32, 42, 175), font=mark_font)
    return mark


def make_logo_watermark(data_url: str, width: int) -> Image.Image | None:
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    try:
        raw = base64.b64decode(data_url)
        logo = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    max_w = max(90, width // 5)
    max_h = max(60, width // 8)
    logo.thumbnail((max_w, max_h))
    return logo


def paste_tiled(overlay: Image.Image, mark: Image.Image) -> None:
    gap_x = max(40, mark.width // 2)
    gap_y = max(38, mark.height)
    for y in range(-mark.height, overlay.height + mark.height, mark.height + gap_y):
        offset = 0 if (y // max(1, mark.height + gap_y)) % 2 == 0 else (mark.width + gap_x) // 2
        for x in range(-mark.width + offset, overlay.width + mark.width, mark.width + gap_x):
            overlay.alpha_composite(mark, (x, y))


def apply_watermark(img: Image.Image, settings: dict[str, Any] | None) -> Image.Image:
    if not isinstance(settings, dict) or not settings.get("enabled"):
        return img
    base = img.convert("RGBA")
    text = str(settings.get("text") or "品牌水印").strip()[:24] or "品牌水印"
    mark_type = str(settings.get("type") or "text")
    mark = make_logo_watermark(str(settings.get("logoData") or ""), base.width) if mark_type == "logo" else None
    if mark is None:
        mark = make_text_watermark(text, base.width)
    if mark.width <= 0 or mark.height <= 0:
        return base
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if str(settings.get("pattern") or "corner") == "tile":
        if mark_type == "text":
            mark = mark.rotate(-22, expand=True)
        paste_tiled(overlay, mark)
    else:
        margin = max(24, base.width // 34)
        overlay.alpha_composite(mark, watermark_position(base.size, mark.size, str(settings.get("position") or "bottom-right"), margin))
    return Image.alpha_composite(base, overlay)


def parse_platforms(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        value = ["meituan"]
    out = []
    for item in value:
        key = str(item)
        if key in PLATFORMS and key not in out:
            out.append(key)
    if not out:
        out = ["meituan"]
    return out


def edge_background(img: Image.Image) -> tuple[int, int, int, int]:
    rgba = img.convert("RGBA")
    samples = [
        rgba.getpixel((0, 0)),
        rgba.getpixel((rgba.width - 1, 0)),
        rgba.getpixel((0, rgba.height - 1)),
        rgba.getpixel((rgba.width - 1, rgba.height - 1)),
    ]
    return tuple(int(sum(pixel[i] for pixel in samples) / len(samples)) for i in range(4))


def fit_to_platform(img: Image.Image, platform_id: str) -> Image.Image:
    spec = PLATFORMS.get(platform_id, PLATFORMS["meituan"])
    target = (int(spec["width"]), int(spec["height"]))
    src = img.convert("RGBA")
    fitted = ImageOps.contain(src, target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", target, edge_background(src))
    canvas.alpha_composite(fitted, ((target[0] - fitted.width) // 2, (target[1] - fitted.height) // 2))
    return canvas


def export_zip(
    selected_style: str,
    scope: str = "all",
    selected_rows: list[int] | None = None,
    image_format: str = "jpg",
    watermark: dict[str, Any] | None = None,
    platforms: list[str] | str | None = None,
) -> dict[str, Any]:
    plan = build_plan(selected_style)
    run_dir = EXPORT_DIR / f"export_{int(time.time())}"
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    selected = set(selected_rows or [])
    selected_platforms = parse_platforms(platforms)
    image_format = image_format.lower()
    if image_format not in {"jpg", "jpeg", "png", "webp"}:
        image_format = "jpg"
    ext = ".jpg" if image_format in {"jpg", "jpeg"} else f".{image_format}"
    watermark_enabled = isinstance(watermark, dict) and bool(watermark.get("enabled"))
    rows = []
    images = 0
    for idx, row in enumerate(plan["results"], start=1):
        candidate = row["candidates"][0] if row["candidates"] else None
        if selected and idx not in selected:
            continue
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
            with Image.open(src) as raw_img:
                for platform_id in selected_platforms:
                    spec = PLATFORMS[platform_id]
                    platform_dir = image_dir / f"{platform_id}_{spec['name']}_{spec['width']}x{spec['height']}"
                    platform_dir.mkdir(parents=True, exist_ok=True)
                    target = platform_dir / f"{idx:03d}_{safe_filename(row['name'])}{ext}"
                    img = fit_to_platform(raw_img, platform_id)
                    img = apply_watermark(img, watermark)
                    if image_format in {"jpg", "jpeg", "webp"}:
                        img = img.convert("RGB")
                    if image_format in {"jpg", "jpeg"}:
                        img.save(target, "JPEG", quality=92)
                    elif image_format == "webp":
                        img.save(target, "WEBP", quality=92)
                    else:
                        img.save(target, "PNG")
                    copied = str(target)
                    images += 1
                    rows.append({"菜品名": row["name"], "分类": row["category"], "类型": row["kind"], "平台": spec["name"], "尺寸": f"{spec['width']}x{spec['height']}", "图片状态": "已生成", "预计积分": row["points"], "品牌水印": "已添加" if watermark_enabled else "未添加", "交付文件": f"{platform_dir.name}/{target.name}"})
        else:
            rows.append({"菜品名": row["name"], "分类": row["category"], "类型": row["kind"], "平台": "", "尺寸": "", "图片状态": "待补图", "预计积分": row["points"], "品牌水印": "未添加", "交付文件": ""})
    report = run_dir / "delivery_report.xlsx"
    pd.DataFrame(rows).to_excel(report, index=False)
    zip_path = run_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(report, report.name)
        for file in image_dir.rglob("*"):
            if file.is_file():
                zf.write(file, f"images/{file.relative_to(image_dir).as_posix()}")
    return {"rows": len(rows), "images": images, "platforms": selected_platforms, "watermark": watermark_enabled, "download": f"/download/{zip_path.relative_to(EXPORT_DIR).as_posix()}"}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/plan")
def api_plan():
    return jsonify(build_plan(request.args.get("style", "")))


@app.get("/api/style-preview")
def api_style_preview():
    style = request.args.get("style", "")
    if not style:
        return jsonify({"error": "请先选择风格"}), 400
    return jsonify(preview_samples(style))


@app.get("/api/menu-status")
def api_menu_status():
    path = current_menu_path()
    if path is None:
        return jsonify({"uploaded": False})
    menu = parse_menu(path)
    return jsonify({"uploaded": True, "menu": {k: v for k, v in menu.items() if k != "items"}})


@app.get("/api/account")
def api_account():
    return jsonify(account_payload())


@app.get("/api/pipeline-config")
def api_pipeline_config():
    return jsonify(pipeline_payload())


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
    selected_rows = payload.get("selectedRows") or []
    if not isinstance(selected_rows, list):
        selected_rows = []
    selected_rows = [int(x) for x in selected_rows if str(x).isdigit()]
    watermark = payload.get("watermark") if isinstance(payload.get("watermark"), dict) else None
    platforms = payload.get("platforms") or ["meituan"]
    return jsonify(export_zip(str(payload.get("style", "")), str(payload.get("scope", "all")), selected_rows, str(payload.get("format", "jpg")), watermark, platforms))


@app.get("/media/<path:name>")
def media(name: str):
    return send_from_directory(LIBRARY_DIR, name)


@app.get("/download/<path:name>")
def download(name: str):
    return send_file(EXPORT_DIR / name, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port)
