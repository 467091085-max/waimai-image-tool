from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import billing
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageFont, ImageOps

from admin_panel import AdminDependencies, create_admin_blueprint
from image_pipeline import PLATFORMS, export_delivery_zip
from matching_engine import (
    classify_kind as engine_classify_kind,
    grams as engine_grams,
    normalize_dish,
    similarity as engine_similarity,
    split_components as engine_split_components,
)
from menu_parser import parse_menu as parse_excel_menu

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LIBRARY_DIR = DATA_DIR / "library"
EXPORT_DIR = DATA_DIR / "exports"
MODEL_INPUT_DIR = DATA_DIR / "model_inputs"
for folder in (UPLOAD_DIR, LIBRARY_DIR, EXPORT_DIR, MODEL_INPUT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MENU_EXTS = {".xls", ".xlsx"}
DEFAULT_LIBRARY_SOURCE_DIRS = [
    "/Users/guiguixiaxia/Documents/cleanpic",
    "/Users/guiguixiaxia/Documents/watermarkpic",
]
POINT_RATE = billing.POINT_RATE
BASE_IMAGE_POINTS = billing.QUALITY_POINTS["standard"]
PREMIUM_IMAGE_POINTS = billing.QUALITY_POINTS["premium"]
CUSTOM_EDIT_POINTS = 10
WATERMARK_POINTS = billing.WATERMARK_POINTS
EXTRA_PLATFORM_POINTS = billing.EXTRA_PLATFORM_POINTS
PREVIEW_SAMPLE_COUNT = 6
DEMO_BALANCE_POINTS = int(os.environ.get("DEMO_BALANCE_POINTS", "1880"))
TENCENT_AIART_HOST = "aiart.tencentcloudapi.com"
TENCENT_AIART_SERVICE = "aiart"
TENCENT_AIART_VERSION = "2022-12-29"
TENCENT_HUNYUAN_HOST = "hunyuan.tencentcloudapi.com"
TENCENT_HUNYUAN_SERVICE = "hunyuan"
TENCENT_HUNYUAN_VERSION = "2023-09-01"
TENCENT_REQUEST_TIMEOUT = int(os.environ.get("TENCENT_REQUEST_TIMEOUT", "55"))
TENCENT_SYNC_LIMIT = int(os.environ.get("TENCENT_HUNYUAN_SYNC_LIMIT", "6"))
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

QUALITY_OPTIONS = {
    "standard": {
        "id": "standard",
        "name": "普通出图",
        "points": BASE_IMAGE_POINTS,
        "cash": round(BASE_IMAGE_POINTS / POINT_RATE, 2),
        "description": "适合常规上架图，成本更低。",
    },
    "premium": {
        "id": "premium",
        "name": "精修出图",
        "points": PREMIUM_IMAGE_POINTS,
        "cash": round(PREMIUM_IMAGE_POINTS / POINT_RATE, 2),
        "description": "适合更高质感和更复杂的统一风格。",
    },
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
    source: str = "internal"
    reusable: bool = True


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

BACKGROUND_LABELS = ("一号背景", "二号背景", "三号背景", "四号背景", "五号背景", "六号背景")

STYLE_COLORS = {
    "style-1": ("一号背景", (238, 205, 155), (173, 102, 42)),
    "style-2": ("二号背景", (60, 64, 67), (218, 187, 121)),
    "style-3": ("三号背景", (229, 232, 235), (90, 116, 132)),
    "style-4": ("四号背景", (181, 44, 39), (255, 221, 148)),
    "style-5": ("五号背景", (210, 184, 122), (84, 136, 84)),
    "style-6": ("六号背景", (192, 216, 226), (42, 100, 132)),
}

STYLE_PROMPTS = {
    "style-1": "温暖原木桌面，柔和自然光，真实餐饮摄影，干净外卖主图",
    "style-2": "深色石板背景，高级餐厅质感，柔和侧光，真实餐饮摄影",
    "style-3": "浅灰极简背景，干净明亮，留白舒服，真实餐饮摄影",
    "style-4": "红色节日促销背景，热卖氛围，画面明亮但不出现文字",
    "style-5": "竹编自然背景，中式餐饮质感，清爽自然光，真实菜品摄影",
    "style-6": "冷灰蓝陶瓷砖背景，清爽现代感，柔和自然光，真实餐饮摄影，适合外卖菜品统一主图",
}

NEGATIVE_IMAGE_PROMPT = "文字，水印，logo，品牌名，价格，人物，手，低清晰度，模糊，变形，裁切主体，脏乱背景"
STRICT_MATCH_MIN_SCORE = 0.45
BEVERAGE_WORDS = (
    "可乐",
    "雪碧",
    "芬达",
    "冰红茶",
    "绿茶",
    "王老吉",
    "矿泉水",
    "纯净水",
    "柠檬水",
    "金桔",
    "奶茶",
    "咖啡",
    "果汁",
    "酸梅汤",
    "豆浆",
    "饮料",
    "饮品",
)
SOUP_WORDS = ("汤", "羹", "粥")
GENERIC_MATCH_WORDS = {"米饭", "白饭", "米", "饭", "套餐", "组合", "主食", "餐具", "饮料"}


def configured_library_dirs() -> list[Path]:
    raw = os.environ.get("LIBRARY_SOURCE_DIRS", "")
    values = [x.strip() for x in raw.split(os.pathsep) if x.strip()] if raw else DEFAULT_LIBRARY_SOURCE_DIRS
    dirs = []
    for value in values:
        path = Path(value).expanduser()
        if path.exists() and path.is_dir():
            dirs.append(path.resolve())
    return dirs


def source_kind_for_path(path: Path) -> str:
    text = str(path).lower()
    if "watermarkpic" in text or "watermarkpick" in text:
        return "watermark"
    if "cleanpic" in text or "cleanpick" in text:
        return "clean"
    return "external"


def stable_style_id(store: str, source: str = "external") -> str:
    digest = hashlib.sha1(f"{source}:{store}".encode("utf-8")).hexdigest()[:10]
    return f"{source}-{digest}"


def image_style_name(image: LibraryImage) -> str:
    if image.style_id in STYLE_COLORS:
        return STYLE_COLORS[image.style_id][0]
    suffix = "可复用图库" if image.reusable else "水印图库"
    return f"{image.store} · {suffix}"


def style_name_for(style_id: str) -> str:
    if style_id in STYLE_COLORS:
        return STYLE_COLORS[style_id][0]
    for image in library_images():
        if image.style_id == style_id:
            return image_style_name(image)
    return "上传图库风格"


def style_color_for(style_id: str) -> tuple[int, int, int]:
    if style_id in STYLE_COLORS:
        return STYLE_COLORS[style_id][1]
    digest = hashlib.sha1(style_id.encode("utf-8")).digest()
    return (225 - digest[0] % 42, 228 - digest[1] % 38, 232 - digest[2] % 34)


def env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def tencent_config() -> dict[str, Any]:
    secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID") or os.environ.get("TENCENT_SECRET_ID") or ""
    secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY") or os.environ.get("TENCENT_SECRET_KEY") or ""
    region = os.environ.get("TENCENTCLOUD_REGION") or os.environ.get("TENCENT_REGION") or "ap-guangzhou"
    enabled = env_truthy("TENCENT_HUNYUAN_ENABLED") or env_truthy("TENCENT_AIART_ENABLED")
    mode = os.environ.get("TENCENT_HUNYUAN_MODE", "auto").strip().lower() or "auto"
    return {"secret_id": secret_id, "secret_key": secret_key, "region": region, "enabled": enabled, "mode": mode}


def tencent_ready() -> bool:
    cfg = tencent_config()
    return bool(cfg["enabled"] and cfg["secret_id"] and cfg["secret_key"])


def tencent_status_payload() -> dict[str, Any]:
    cfg = tencent_config()
    missing = []
    if not cfg["enabled"]:
        missing.append("TENCENT_HUNYUAN_ENABLED=true")
    if not cfg["secret_id"]:
        missing.append("TENCENTCLOUD_SECRET_ID")
    if not cfg["secret_key"]:
        missing.append("TENCENTCLOUD_SECRET_KEY")
    return {
        "provider": "tencent-hunyuan" if tencent_ready() else "local-demo",
        "configured": tencent_ready(),
        "enabled": cfg["enabled"],
        "region": cfg["region"],
        "mode": cfg["mode"],
        "syncLimit": TENCENT_SYNC_LIMIT,
        "missing": missing,
    }


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def tencent_cloud_api_request(action: str, payload: dict[str, Any], host: str, service: str, version: str, timeout: int = TENCENT_REQUEST_TIMEOUT) -> dict[str, Any]:
    cfg = tencent_config()
    if not tencent_ready():
        raise RuntimeError("腾讯云生图环境变量未配置完整")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join(["POST", "/", "", canonical_headers, signed_headers, sha256_hex(body)])
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(["TC3-HMAC-SHA256", str(timestamp), credential_scope, sha256_hex(canonical_request)])
    secret_date = hmac_sha256(("TC3" + cfg["secret_key"]).encode("utf-8"), date)
    secret_service = hmac_sha256(secret_date, service)
    secret_signing = hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={cfg['secret_id']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
        "X-TC-Region": cfg["region"],
    }
    req = urllib.request.Request(f"https://{host}", data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{host} HTTP {exc.code}: {raw[:500]}") from exc
    data = json.loads(raw)
    response = data.get("Response", {})
    if "Error" in response:
        error = response["Error"]
        raise RuntimeError(f"{host} {error.get('Code', 'TencentError')}: {error.get('Message', '调用失败')}")
    return response


def tencent_api_request(action: str, payload: dict[str, Any], timeout: int = TENCENT_REQUEST_TIMEOUT) -> dict[str, Any]:
    if action == "TextToImageLite":
        endpoints = [
            (TENCENT_AIART_HOST, TENCENT_AIART_SERVICE, TENCENT_AIART_VERSION),
            (TENCENT_HUNYUAN_HOST, TENCENT_HUNYUAN_SERVICE, TENCENT_HUNYUAN_VERSION),
        ]
        errors = []
        for host, service, version in endpoints:
            try:
                response = tencent_cloud_api_request(action, payload, host, service, version, timeout)
                response["_Endpoint"] = host
                return response
            except RuntimeError as exc:
                errors.append(str(exc))
        raise RuntimeError("；".join(errors))
    response = tencent_cloud_api_request(action, payload, TENCENT_AIART_HOST, TENCENT_AIART_SERVICE, TENCENT_AIART_VERSION, timeout)
    response["_Endpoint"] = TENCENT_AIART_HOST
    return response


def combined_generation_error(primary_label: str, primary_error: Exception, fallback_label: str, fallback_error: Exception) -> RuntimeError:
    return RuntimeError(f"{primary_label}失败：{primary_error}；{fallback_label}失败：{fallback_error}")


def read_remote_image(url: str, timeout: int = 60) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": "waimai-image-tool/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return Image.open(io.BytesIO(raw)).convert("RGB")


def save_result_image(result_image: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if result_image.startswith("http://") or result_image.startswith("https://"):
        img = read_remote_image(result_image)
    else:
        payload = result_image.split(",", 1)[-1]
        img = Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGB")
    img.save(target, "JPEG", quality=92, optimize=True)


def public_base_url() -> str:
    configured = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    if configured:
        return configured.rstrip("/")
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:
        return ""


def is_public_http_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    return "127.0.0.1" not in url and "localhost" not in url


def candidate_public_url(candidate: dict[str, Any]) -> str:
    url = str(candidate.get("url") or "")

    def normalize_public_url(value: str) -> str:
        parts = urllib.parse.urlsplit(value)
        quoted_path = urllib.parse.quote(parts.path, safe="/%")
        quoted_query = urllib.parse.quote(parts.query, safe="=&%/:+,-_.~")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, quoted_path, quoted_query, parts.fragment))

    if is_public_http_url(url):
        return normalize_public_url(url)
    base = public_base_url()
    if not base or "127.0.0.1" in base or "localhost" in base:
        return ""
    if url.startswith("/"):
        return normalize_public_url(f"{base}{url}")
    return ""


def model_input_public_url(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    path_text = str(candidate.get("path") or "")
    path = Path(path_text) if path_text else None
    if path and path.exists() and path.suffix.lower() in IMAGE_EXTS:
        stat = path.stat()
        digest_source = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
        filename = f"{hashlib.sha1(digest_source.encode('utf-8')).hexdigest()[:24]}.jpg"
        target = MODEL_INPUT_DIR / filename
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.open(path).convert("RGB").save(target, "JPEG", quality=92, optimize=True)
        base = public_base_url()
        if base and "127.0.0.1" not in base and "localhost" not in base:
            return f"{base.rstrip('/')}/model-inputs/{filename}"
    return candidate_public_url(candidate)


def external_image_path(image_id_with_ext: str) -> Path | None:
    image_id = Path(image_id_with_ext).stem
    if not re.fullmatch(r"[a-f0-9]{18}", image_id):
        return None
    approved_dirs = configured_library_dirs()
    for image in library_images():
        if image.image_id != image_id:
            continue
        try:
            resolved = image.path.resolve()
        except FileNotFoundError:
            return None
        if any(resolved.is_relative_to(base) for base in approved_dirs):
            return resolved
    return None


def output_resolution_for_style(style_id: str) -> str:
    return "1024:768" if style_id != "style-3" else "1024:1024"


def default_delivery_resolution() -> str:
    return "1024:768"


def style_prompt_for(style_id: str) -> str:
    if style_id in STYLE_PROMPTS:
        return STYLE_PROMPTS[style_id]
    return f"严格贴合所选背景风格「{style_name_for(style_id)}」，背景、光线、色彩和构图保持一致"


def quality_detail(quality: str | None = "standard") -> str:
    detail = "高清真实、菜品细节清楚、主体完整居中"
    if quality_config(quality)["id"] == "premium":
        detail += "、专业商业摄影光影"
    return detail


def row_components_text(row: dict[str, Any]) -> str:
    components = [str(value).strip() for value in row.get("components") or [] if str(value).strip()]
    if components:
        return "、".join(components[:6])
    return str(row.get("name") or "套餐组合")


def prompt_for_generation(row: dict[str, Any], style_id: str, quality: str | None = "standard", prompt_type: str = "text_to_image") -> str:
    style = style_prompt_for(style_id)
    detail = quality_detail(quality)
    kind = row.get("kind") or "菜品"
    dish = row.get("name", "外卖菜品")
    forbidden = "不要出现任何文字、价格、logo、水印、品牌名、人物、包装袋，不要裁切菜品主体。"
    if kind == "套餐/组合" or prompt_type == "combo":
        return (
            f"{dish}，套餐组合外卖主图，外卖平台主图，包含：{row_components_text(row)}，{style}，"
            f"{detail}，多菜品协调摆放，主体完整，背景必须跟所选背景一致。{forbidden}"
        )[:250]
    if prompt_type == "replace_background":
        return (
            f"保留「{dish}」菜品主体完整，仅替换为{style}，外卖平台主图，{detail}，"
            f"背景必须跟所选背景一致，不改变菜品本身，不添加无关物体。{forbidden}"
        )[:250]
    return (
        f"{dish}，{kind}，纯文生图，外卖平台主图，{style}，{detail}，"
        f"背景必须跟所选背景一致，真实餐饮商业摄影质感。{forbidden}"
    )[:250]


def tencent_text_to_image(row: dict[str, Any], style_id: str, quality: str | None, target: Path) -> dict[str, Any]:
    prompt_type = "combo" if row.get("kind") == "套餐/组合" else "text_to_image"
    response = tencent_api_request(
        "TextToImageLite",
        {
            "Prompt": prompt_for_generation(row, style_id, quality, prompt_type),
            "NegativePrompt": NEGATIVE_IMAGE_PROMPT,
            "Resolution": output_resolution_for_style(style_id),
            "RspImgType": "url",
            "LogoAdd": 0,
        },
    )
    save_result_image(str(response.get("ResultImage") or ""), target)
    return {
        "provider": "tencent-hunyuan",
        "action": "TextToImageLite",
        "promptType": prompt_type,
        "requestId": response.get("RequestId"),
        "seed": response.get("Seed"),
        "endpoint": response.get("_Endpoint"),
    }


def prompt_for_style_background(style_id: str) -> str:
    return (
        f"外卖菜品主图背景风格样图，展示{style_prompt_for(style_id)}。"
        "一份普通中式菜品占位，背景、桌面、光影和色调清楚可见。"
        "主体完整居中，背景风格必须鲜明且和其他方案明显不同。"
        "不要出现文字、价格、logo、水印、人物。"
    )[:250]


def tencent_style_background(style_id: str, target: Path) -> dict[str, Any]:
    source_candidate = style_background_seed_candidate()
    product_url = model_input_public_url(source_candidate)
    if not product_url:
        raise RuntimeError("当前图库图片没有公网 URL，无法调用商品背景生成")
    try:
        response = tencent_api_request(
            "ReplaceBackground",
            {
                "ProductUrl": product_url,
                "Prompt": prompt_for_style_background(style_id),
                "Product": "招牌菜品",
                "Resolution": default_delivery_resolution(),
                "RspImgType": "url",
                "LogoAdd": 0,
            },
        )
    except Exception as exc:
        raise RuntimeError(f"ProductUrl={product_url}；{exc}") from exc
    save_result_image(str(response.get("ResultImage") or ""), target)
    return {
        "provider": "tencent-hunyuan",
        "action": "ReplaceBackground",
        "promptType": "style_background",
        "requestId": response.get("RequestId"),
        "seed": response.get("Seed"),
        "endpoint": response.get("_Endpoint"),
    }


def tencent_replace_background(row: dict[str, Any], source_candidate: dict[str, Any], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, Any]:
    product_url = model_input_public_url(source_candidate)
    if not product_url:
        raise RuntimeError("当前图库图片没有公网 URL，无法调用商品背景生成")
    prompt_type = "combo" if row.get("kind") == "套餐/组合" else "replace_background"
    try:
        response = tencent_api_request(
            "ReplaceBackground",
            {
                "ProductUrl": product_url,
                "Prompt": prompt_for_generation(row, style_id, quality, prompt_type),
                "Product": str(row.get("name") or "")[:50],
                "Resolution": output_resolution_for_style(style_id),
                "RspImgType": "url",
                "LogoAdd": 0,
            },
        )
    except Exception as exc:
        raise RuntimeError(f"ProductUrl={product_url}；{exc}") from exc
    save_result_image(str(response.get("ResultImage") or ""), target)
    return {"provider": "tencent-hunyuan", "action": "ReplaceBackground", "promptType": prompt_type, "requestId": response.get("RequestId"), "endpoint": response.get("_Endpoint")}


def normalize(text: str) -> str:
    return normalize_dish(text)


def grams(text: str) -> set[str]:
    return engine_grams(text)


def similarity(menu_name: str, image_name: str, menu_norm: str, image_norm: str, menu_grams: set[str], image_grams: set[str]) -> float:
    return engine_similarity(menu_name, image_name, menu_norm, image_norm, menu_grams, image_grams)


def has_any_word(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def semantic_family(name: str, norm: str) -> str:
    text = f"{name}{norm}"
    if has_any_word(text, BEVERAGE_WORDS):
        return "beverage"
    if has_any_word(text, SOUP_WORDS):
        return "soup"
    return "food"


def significant_bigrams(norm: str) -> set[str]:
    clean = str(norm or "")
    if not clean:
        return set()
    chunks = {clean[i : i + 2] for i in range(max(0, len(clean) - 1))}
    return {chunk for chunk in chunks if chunk and chunk not in GENERIC_MATCH_WORDS}


def is_generic_match_name(name: str, norm: str) -> bool:
    compact = re.sub(r"\s+", "", str(name or ""))
    return compact in GENERIC_MATCH_WORDS or norm in GENERIC_MATCH_WORDS or len(norm) <= 1


def strict_match_allowed(menu_name: str, image_name: str, menu_norm: str, image_norm: str, score: float) -> bool:
    if score < STRICT_MATCH_MIN_SCORE:
        return False
    if not menu_norm or not image_norm:
        return False
    if is_generic_match_name(image_name, image_norm):
        return False
    menu_family = semantic_family(menu_name, menu_norm)
    image_family = semantic_family(image_name, image_norm)
    if menu_family != image_family:
        return False
    if menu_norm == image_norm or menu_norm in image_norm or image_norm in menu_norm:
        return True
    menu_bigrams = significant_bigrams(menu_norm)
    image_bigrams = significant_bigrams(image_norm)
    if menu_bigrams & image_bigrams:
        return True
    common_chars = set(menu_norm) & set(image_norm)
    char_overlap = len(common_chars) / max(1, min(len(set(menu_norm)), len(set(image_norm))))
    return bool(score >= 0.72 and char_overlap >= 0.5)


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
    return engine_split_components(name, attrs)


def detect_kind(name: str, attrs: str) -> str:
    return engine_classify_kind(name, attrs)


def parse_menu(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = current_menu_path()
    if path is None:
        items = demo_menu_items()
        return {"file": "demo_menu.xlsx", "store": "演示盖码饭门店", "count": len(items), "kindCounts": kind_counts(items), "items": items, "demo": True}
    menu = parse_excel_menu(path)
    for item in menu["items"]:
        raw_components = [str(value).strip() for value in item.get("components", []) if str(value).strip()]
        attrs = " ".join(raw_components)
        item["norm"] = normalize(item.get("name", ""))
        item["kind"] = detect_kind(item.get("name", ""), attrs)
        components = raw_components or split_components(item.get("name", ""), "")
        deduped_components = []
        seen_components = set()
        for component in components:
            label = re.sub(r"(套餐|组合|单人餐|双人餐|盖码饭|盖浇饭|木桶饭)$", "", component).strip(" -_·:：")
            label = label or component
            norm = normalize(label)
            if len(norm) < 2 or norm in seen_components:
                continue
            seen_components.add(norm)
            deduped_components.append(label)
        item["components"] = deduped_components[:8]
    menu["kindCounts"] = kind_counts(menu["items"])
    menu["count"] = len(menu["items"])
    return menu


@lru_cache(maxsize=1)
def library_images() -> list[LibraryImage]:
    ensure_demo_data()
    images = []
    has_seed_library = any(path.is_dir() and path.name.startswith("seed_") for path in LIBRARY_DIR.iterdir())
    for path in sorted(LIBRARY_DIR.rglob("*")):
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = path.relative_to(LIBRARY_DIR)
        parts = rel.parts
        if parts and parts[0].startswith("_"):
            continue
        if has_seed_library and parts and parts[0] == "demo_store":
            continue
        store = parts[0] if len(parts) > 1 else "uploaded"
        style_id = next((p for p in parts if p.startswith("style-")), "style-upload")
        dish = path.stem
        norm = normalize(dish)
        if not norm:
            continue
        images.append(LibraryImage(hashlib.sha1(str(path).encode()).hexdigest()[:18], path, store, dish, norm, grams(norm), style_id, "internal", True))
    for source_dir in configured_library_dirs():
        source = source_kind_for_path(source_dir)
        reusable = source != "watermark"
        for path in sorted(source_dir.rglob("*")):
            if path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
                continue
            try:
                rel = path.relative_to(source_dir)
            except ValueError:
                rel = Path(path.name)
            store = rel.parts[0] if len(rel.parts) > 1 else source_dir.name
            dish = path.stem
            norm = normalize(dish)
            if not norm:
                continue
            style_id = stable_style_id(store, source)
            image_id = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:18]
            images.append(LibraryImage(image_id, path, store, dish, norm, grams(norm), style_id, source, reusable))
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


def top_candidates(item: dict[str, Any], library: list[LibraryImage], limit: int = 6, min_score: float = STRICT_MATCH_MIN_SCORE) -> list[dict[str, Any]]:
    item_grams = grams(item["norm"])
    scored = []
    for image in library:
        score = similarity(item["name"], image.dish, item["norm"], image.norm, item_grams, image.grams)
        if score >= min_score and strict_match_allowed(item["name"], image.dish, item["norm"], image.norm, score):
            scored.append((score, image))
    reusable = sorted((x for x in scored if x[1].reusable), key=lambda x: (x[0], x[1].source == "clean"), reverse=True)
    reference_only = sorted((x for x in scored if not x[1].reusable), key=lambda x: x[0], reverse=True)
    scored = (reusable + reference_only)[:limit]
    return [
        {
            "imageId": image.image_id,
            "score": round(score * 100, 1),
            "dishName": image.dish,
            "store": image.store,
            "styleId": image.style_id,
            "styleName": image_style_name(image),
            "source": image.source,
            "reusable": image.reusable,
            "url": media_url_for_path(image.path),
            "path": str(image.path),
        }
        for score, image in scored[:limit]
    ]


def component_matches(item: dict[str, Any], library: list[LibraryImage], limit: int = 4) -> list[dict[str, Any]]:
    matches = []
    for component in item.get("components") or []:
        norm = normalize(component)
        if len(norm) < 2:
            continue
        component_item = {**item, "name": component, "norm": norm}
        candidates = top_candidates(component_item, library, limit)
        matches.append({"name": component, "norm": norm, "candidates": candidates})
    return matches


def media_url_for_path(path: Path) -> str:
    try:
        return f"/media/{path.relative_to(LIBRARY_DIR).as_posix()}"
    except ValueError:
        image_id = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:18]
        return f"/external-media/{image_id}{path.suffix.lower()}"


def candidate_from_path(path: Path, dish: str, style_id: str, source: str, score: float = 100.0) -> dict[str, Any]:
    return {
        "imageId": hashlib.sha1(str(path).encode()).hexdigest()[:18],
        "score": score,
        "dishName": dish,
        "store": source,
        "styleId": style_id,
        "styleName": style_name_for(style_id),
        "source": source,
        "reusable": not source.startswith("watermark"),
        "url": media_url_for_path(path),
        "path": str(path),
        "generated": source.startswith("generated"),
    }


def style_background_seed_candidate() -> dict[str, Any] | None:
    preferred_names = ("辣椒炒肉", "黄牛肉", "红烧肉", "盖码饭", "招牌")
    images = [image for image in library_images() if image.reusable]
    images.sort(key=lambda image: (not any(word in image.dish for word in preferred_names), image.store, image.dish))
    for image in images:
        candidate = candidate_from_path(image.path, image.dish, image.style_id, image.source, 100.0)
        if candidate_public_url(candidate):
            return candidate
    return None


def style_background_target(style_id: str) -> Path:
    return LIBRARY_DIR / "_style_backgrounds" / style_id / "背景风格样图.jpg"


def style_sample_candidate(style_id: str) -> dict[str, Any]:
    target = style_background_target(style_id)
    metadata = load_ai_output_metadata(target) if target.exists() else None
    if target.exists() and (successful_model_metadata(metadata) or not tencent_ready()):
        candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 100.0)
        if metadata:
            candidate_generation_metadata(candidate, metadata)
        return candidate
    if tencent_ready():
        try:
            detail = tencent_style_background(style_id, target)
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "styleId": style_id,
                "tencent": detail,
            }
            write_ai_output_metadata(target, metadata)
            candidate = candidate_from_path(target, "背景风格样图", style_id, "tencent-style-sample", 100.0)
            candidate_generation_metadata(candidate, metadata)
            return candidate
        except Exception as exc:
            if not target.exists():
                draw_demo_image(target, "背景风格样图", style_id)
            candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 80.0)
            candidate["generationStatus"] = "failed"
            candidate["error"] = str(exc)[:220]
            return candidate
    if not target.exists():
        draw_demo_image(target, "背景风格样图", style_id)
    return candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 90.0)


def generated_preview_candidate(item: dict[str, Any], style_id: str) -> dict[str, Any] | None:
    if not style_id:
        return None
    target = LIBRARY_DIR / "_generated_previews" / style_id / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    if not target.exists():
        return None
    metadata = load_ai_output_metadata(target)
    if tencent_ready() and not successful_model_metadata(metadata):
        return None
    candidate = candidate_from_path(target, item["name"], style_id, "generated-preview", 99.9)
    if metadata:
        candidate_generation_metadata(candidate, metadata)
    return candidate


def materialize_preview_candidate(item: dict[str, Any], selected_style: str, quality: str | None = "standard") -> tuple[dict[str, Any] | None, dict[str, Any]]:
    target = LIBRARY_DIR / "_generated_previews" / selected_style / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    cached = generated_preview_candidate(item, selected_style)
    if cached:
        return cached, {"status": "cached", "provider": cached.get("aiProvider") or "tencent-hunyuan", "action": cached.get("generationAction") or "Cached"}
    same_style = reusable_selected_style_candidate(item, selected_style)
    if same_style:
        return same_style, {"status": "reused", "provider": "library", "action": "Reuse"}
    source_candidate = source_candidate_for_generation(item)
    result: dict[str, Any] = {"status": "pending", "provider": "tencent-hunyuan" if tencent_ready() else "local-demo", "action": "Preview"}
    if tencent_ready():
        try:
            if source_candidate:
                try:
                    detail = tencent_replace_background(item, source_candidate, selected_style, target, quality)
                except Exception as replace_error:
                    try:
                        detail = tencent_text_to_image(item, selected_style, quality, target)
                    except Exception as text_error:
                        raise combined_generation_error("商品背景生成", replace_error, "文生图兜底", text_error) from text_error
                    result["fallbackFrom"] = "ReplaceBackground"
                    result["fallbackMessage"] = str(replace_error)[:220]
            else:
                detail = tencent_text_to_image(item, selected_style, quality, target)
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "reason": "free_style_preview",
                "row": item.get("row"),
                "dish": item.get("name"),
                "sourceCandidate": {
                    "imageId": source_candidate.get("imageId"),
                    "dishName": source_candidate.get("dishName"),
                    "styleId": source_candidate.get("styleId"),
                    "source": source_candidate.get("source"),
                }
                if source_candidate
                else None,
                "tencent": detail,
            }
            write_ai_output_metadata(target, metadata)
            candidate = candidate_from_path(target, item["name"], selected_style, f"tencent-preview-{detail['action']}", 100.0)
            candidate_generation_metadata(candidate, metadata)
            return candidate, {"status": "succeeded", "provider": "tencent-hunyuan", "action": detail["action"]}
        except Exception as exc:
            result.update({"status": "failed", "action": "Failed", "error": str(exc)[:220]})
            return None, result
    if env_truthy("ALLOW_LOCAL_IMAGE_FALLBACK", default=True):
        draw_demo_image(target, item["name"], selected_style)
        metadata = {"status": "fallback", "provider": "local-demo", "action": "LocalFallback", "reason": "free_style_preview"}
        write_ai_output_metadata(target, metadata)
        candidate = candidate_from_path(target, item["name"], selected_style, "generated-preview", 80.0)
        candidate_generation_metadata(candidate, metadata)
        return candidate, {"status": "fallback", "provider": "local-demo", "action": "LocalFallback"}
    result.update({"status": "pending", "action": "WaitingForModelConfig"})
    return None, result


def ai_output_candidate(item: dict[str, Any], style_id: str, quality: str | None, source: str) -> tuple[dict[str, Any], Path]:
    quality_id = quality_config(quality)["id"]
    target = LIBRARY_DIR / "_ai_outputs" / style_id / quality_id / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    candidate = candidate_from_path(target, item["name"], style_id, source, 100.0)
    candidate["aiProvider"] = "tencent-hunyuan" if source.startswith("tencent") else "local-demo"
    candidate["generated"] = True
    return candidate, target


def ai_output_metadata_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".json")


def load_ai_output_metadata(target: Path) -> dict[str, Any] | None:
    meta_path = ai_output_metadata_path(target)
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_ai_output_metadata(target: Path, metadata: dict[str, Any]) -> None:
    meta_path = ai_output_metadata_path(target)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def existing_ai_output_candidate(item: dict[str, Any], style_id: str, quality: str | None) -> dict[str, Any] | None:
    candidate, target = ai_output_candidate(item, style_id, quality, "generated-final")
    if not target.exists():
        return None
    metadata = load_ai_output_metadata(target)
    if not successful_model_metadata(metadata):
        return None
    assert metadata is not None
    candidate["aiProvider"] = "tencent-hunyuan"
    candidate["generationStatus"] = "cached"
    candidate["generationAction"] = str(metadata.get("action") or "")
    candidate["generationProvider"] = "tencent-hunyuan"
    if isinstance(metadata.get("tencent"), dict):
        candidate["tencent"] = metadata["tencent"]
    return candidate


def is_generated_candidate(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    source = str(candidate.get("source") or "")
    return bool(candidate.get("generated") or source.startswith("generated") or source.startswith("tencent"))


def source_candidates_for_generation(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in row.get("candidates") or [] if c.get("path") and not is_generated_candidate(c)]


def source_candidate_for_generation(row: dict[str, Any]) -> dict[str, Any] | None:
    candidates = source_candidates_for_generation(row)
    return next((c for c in candidates if c.get("reusable", True)), candidates[0] if candidates else None)


def strip_nonfinal_generated_candidates(row: dict[str, Any]) -> None:
    row["candidates"] = [
        c for c in row.get("candidates") or []
        if not is_generated_candidate(c) or c.get("aiProvider") == "tencent-hunyuan" or str(c.get("source") or "").startswith("tencent")
    ]


def reusable_selected_style_candidate(row: dict[str, Any], selected_style: str) -> dict[str, Any] | None:
    return next((c for c in source_candidates_for_generation(row) if c.get("styleId") == selected_style and c.get("reusable", True)), None)


def materialization_reason(row: dict[str, Any], selected_style: str) -> str | None:
    if not selected_style:
        return "no_selected_style"
    sources = source_candidates_for_generation(row)
    if row.get("kind") == "套餐/组合":
        return "combo"
    if reusable_selected_style_candidate(row, selected_style):
        return None
    if not sources:
        return "missing_image"
    if any(not c.get("reusable", True) for c in sources):
        return "not_reusable"
    return "style_mismatch"


def should_materialize(row: dict[str, Any], selected_style: str = "") -> bool:
    if selected_style:
        return materialization_reason(row, selected_style) is not None
    candidate = row["candidates"][0] if row.get("candidates") else None
    return bool(not candidate or candidate.get("generated") or row.get("backgroundAction") in {"智能补图", "智能统一风格", "需抠图换背景", "需要定制/生成", "需去水印/重绘", "套餐组合生成"})


def successful_model_metadata(metadata: dict[str, Any] | None) -> bool:
    return bool(metadata and metadata.get("status") == "succeeded" and metadata.get("provider") == "tencent-hunyuan")


def final_ready_candidate(candidate: dict[str, Any], selected_style: str, action: str) -> bool:
    if candidate.get("aiProvider") == "tencent-hunyuan" or str(candidate.get("source") or "").startswith("tencent"):
        return True
    return action == "背景一致，直接复用" and candidate.get("styleId") == selected_style and not candidate.get("generated")


def prepare_results_for_export(results: list[dict[str, Any]], selected_style: str) -> list[dict[str, Any]]:
    prepared = []
    for row in results:
        copy_row = {**row}
        action = str(copy_row.get("backgroundAction") or "")
        copy_row["candidates"] = [
            candidate for candidate in copy_row.get("candidates") or []
            if final_ready_candidate(candidate, selected_style, action)
        ]
        if not copy_row["candidates"] and action not in {"背景一致，直接复用", "正式生成"}:
            copy_row["publicStatus"] = copy_row.get("publicStatus") or "待正式生成"
        prepared.append(copy_row)
    return prepared


def candidate_generation_metadata(candidate: dict[str, Any], metadata: dict[str, Any]) -> None:
    candidate["aiProvider"] = str(metadata.get("provider") or candidate.get("aiProvider") or "")
    candidate["generationStatus"] = str(metadata.get("status") or "")
    candidate["generationAction"] = str(metadata.get("action") or "")
    candidate["generationProvider"] = str(metadata.get("provider") or "")
    if isinstance(metadata.get("tencent"), dict):
        candidate["tencent"] = metadata["tencent"]


def promote_candidate(row: dict[str, Any], candidate: dict[str, Any]) -> None:
    image_id = candidate.get("imageId")
    path = candidate.get("path")
    row["candidates"] = [candidate] + [c for c in row.get("candidates", []) if c.get("imageId") != image_id and c.get("path") != path]


def generation_row_result(row: dict[str, Any], provider: str, action: str, reason: str | None) -> dict[str, Any]:
    return {
        "row": row.get("row"),
        "dish": row.get("name"),
        "provider": provider,
        "action": action,
        "reason": reason,
        "attempted": False,
        "succeeded": False,
        "fallback": False,
        "status": "pending",
    }


def bump_generation_action(generation: dict[str, Any], action: str) -> None:
    actions = generation.setdefault("actions", {})
    actions[action] = int(actions.get(action) or 0) + 1


def materialize_final_images(plan: dict[str, Any], selected_style: str, quality: str | None = "standard") -> dict[str, Any]:
    status = tencent_status_payload()
    generation = {
        "provider": status["provider"],
        "configured": status["configured"],
        "action": "materialize_final_images",
        "attempted": 0,
        "succeeded": 0,
        "fallback": 0,
        "localFallback": 0,
        "actionFallback": 0,
        "cached": 0,
        "limited": 0,
        "failed": 0,
        "pending": 0,
        "skipped": 0,
        "limit": TENCENT_SYNC_LIMIT,
        "errors": [],
        "actions": {},
        "items": [],
    }
    if not selected_style:
        generation["action"] = "missing_selected_style"
        return generation
    live_budget = TENCENT_SYNC_LIMIT if TENCENT_SYNC_LIMIT >= 0 else 0
    for row in plan["results"]:
        strip_nonfinal_generated_candidates(row)
        reason = materialization_reason(row, selected_style)
        source_candidate = source_candidate_for_generation(row)
        item_result = generation_row_result(row, status["provider"], "Reuse", reason)
        if reason is None:
            generation["skipped"] += 1
            item_result.update({"provider": "library", "status": "reused", "reason": "same_style_reuse"})
            bump_generation_action(generation, "Reuse")
            row["generation"] = item_result
            generation["items"].append(item_result)
            continue

        _, target = ai_output_candidate(row, selected_style, quality, "generated-final")
        metadata = load_ai_output_metadata(target) if target.exists() else None
        if target.exists() and successful_model_metadata(metadata):
            assert metadata is not None
            cached_action = str(metadata.get("action") or "Cached")
            ai_candidate, _ = ai_output_candidate(row, selected_style, quality, f"tencent-{cached_action}")
            candidate_generation_metadata(ai_candidate, metadata)
            promote_candidate(row, ai_candidate)
            row["publicStatus"] = "已生成"
            row["backgroundAction"] = "正式生成"
            row["generationStatus"] = "cached"
            item_result.update({"provider": "tencent-hunyuan", "action": cached_action, "status": "cached", "succeeded": True, "cached": True})
            generation["cached"] += 1
            bump_generation_action(generation, "Cached")
            row["generation"] = item_result
            generation["items"].append(item_result)
            continue

        if status["configured"] and generation["attempted"] >= live_budget:
            generation["limited"] += 1
            generation["pending"] += 1
            item_result.update(
                {
                    "provider": "tencent-hunyuan",
                    "action": "Limited",
                    "status": "limited",
                    "reason": f"{reason}: TENCENT_HUNYUAN_SYNC_LIMIT reached",
                }
            )
            row["backgroundAction"] = "待正式生成"
            row["publicStatus"] = "待正式生成"
            row["generationStatus"] = "limited"
            row["generation"] = item_result
            bump_generation_action(generation, "Limited")
            generation["items"].append(item_result)
            continue

        used_tencent = False
        detail: dict[str, Any] | None = None
        replace_error: Exception | None = None
        if status["configured"]:
            generation["attempted"] += 1
            item_result["attempted"] = True
            try:
                if source_candidate:
                    try:
                        detail = tencent_replace_background(row, source_candidate, selected_style, target, quality)
                    except Exception as exc:
                        replace_error = exc
                        try:
                            detail = tencent_text_to_image(row, selected_style, quality, target)
                        except Exception as text_error:
                            raise combined_generation_error("商品背景生成", exc, "文生图兜底", text_error) from text_error
                else:
                    detail = tencent_text_to_image(row, selected_style, quality, target)
                if replace_error is not None:
                    generation["fallback"] += 1
                    generation["actionFallback"] += 1
                    item_result["fallback"] = True
                    item_result["fallbackFrom"] = "ReplaceBackground"
                    item_result["fallbackMessage"] = str(replace_error)[:220]
                assert detail is not None
                ai_candidate, _ = ai_output_candidate(row, selected_style, quality, f"tencent-{detail['action']}")
                ai_candidate["tencent"] = detail
                metadata = {
                    "status": "succeeded",
                    "provider": "tencent-hunyuan",
                    "action": detail["action"],
                    "promptType": detail.get("promptType"),
                    "reason": reason,
                    "row": row.get("row"),
                    "dish": row.get("name"),
                    "sourceCandidate": {
                        "imageId": source_candidate.get("imageId"),
                        "dishName": source_candidate.get("dishName"),
                        "styleId": source_candidate.get("styleId"),
                        "source": source_candidate.get("source"),
                    }
                    if source_candidate
                    else None,
                    "tencent": detail,
                }
                write_ai_output_metadata(target, metadata)
                candidate_generation_metadata(ai_candidate, metadata)
                promote_candidate(row, ai_candidate)
                used_tencent = True
                generation["succeeded"] += 1
                item_result.update({"provider": "tencent-hunyuan", "action": detail["action"], "status": "succeeded", "succeeded": True, "promptType": detail.get("promptType")})
                bump_generation_action(generation, detail["action"])
            except Exception as exc:
                generation["errors"].append({"dish": row.get("name"), "message": str(exc)[:220]})
                item_result["error"] = str(exc)[:220]
        if not used_tencent:
            if status["configured"]:
                generation["failed"] += 1
                generation["pending"] += 1
                item_result.update({"provider": "tencent-hunyuan", "action": "Failed", "status": "failed"})
                row["backgroundAction"] = "模型生成失败"
                row["publicStatus"] = "模型生成失败"
                row["generationStatus"] = "failed"
                row["generation"] = item_result
                bump_generation_action(generation, "Failed")
                generation["items"].append(item_result)
                continue
            if not env_truthy("ALLOW_LOCAL_IMAGE_FALLBACK", default=True):
                generation["pending"] += 1
                item_result.update({"provider": "local", "action": "WaitingForModelConfig", "status": "pending"})
                row["backgroundAction"] = "等待模型配置"
                row["publicStatus"] = "等待模型配置"
                row["generationStatus"] = "pending"
                row["generation"] = item_result
                bump_generation_action(generation, "WaitingForModelConfig")
                generation["items"].append(item_result)
                continue
            draw_demo_image(target, row["name"], selected_style)
            ai_candidate, _ = ai_output_candidate(row, selected_style, quality, "generated-local")
            metadata = {
                "status": "fallback",
                "provider": "local-demo",
                "action": "LocalFallback",
                "reason": reason,
                "row": row.get("row"),
                "dish": row.get("name"),
                "error": item_result.get("error"),
            }
            write_ai_output_metadata(target, metadata)
            candidate_generation_metadata(ai_candidate, metadata)
            promote_candidate(row, ai_candidate)
            generation["fallback"] += 1
            generation["localFallback"] += 1
            item_result.update({"provider": "local-demo", "action": "LocalFallback", "status": "fallback", "fallback": True})
            bump_generation_action(generation, "LocalFallback")
        row["backgroundAction"] = "正式生成"
        row["publicStatus"] = "已生成" if used_tencent else "本地兜底"
        row["generationStatus"] = "succeeded" if used_tencent else "fallback"
        row["generation"] = item_result
        generation["items"].append(item_result)
    return generation


def style_options(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidate_style_ids = {c["styleId"] for r in results for c in r["candidates"] if c["styleId"]}
    library_style_ids = {image.style_id for image in library_images() if image.style_id}
    style_ids = list(STYLE_COLORS)
    for style_id in sorted(candidate_style_ids | library_style_ids):
        if style_id not in style_ids:
            style_ids.append(style_id)
    for style_id in sorted({image.style_id for image in library_images() if image.style_id}):
        if style_id not in style_ids:
            style_ids.append(style_id)
    if not style_ids:
        style_ids = list(STYLE_COLORS)
    options = []
    total = max(1, len(results))
    for idx, style_id in enumerate(style_ids[:PREVIEW_SAMPLE_COUNT], start=1):
        direct = review = bg_replace = custom = 0
        sample = None
        for row in results:
            candidates = row["candidates"]
            if not candidates:
                custom += 1
                continue
            same = next((c for c in candidates if c["styleId"] == style_id), None)
            sample = sample or same
            if same and same.get("reusable", True) and row["status"] == "直接可用":
                direct += 1
            elif same:
                review += 1
            else:
                bg_replace += 1
        if style_id in STYLE_COLORS:
            sample = style_sample_candidate(style_id)
        else:
            sample = sample or style_sample_candidate(style_id)
        style_name = style_name_for(style_id)
        display_name = BACKGROUND_LABELS[idx - 1] if idx <= len(BACKGROUND_LABELS) else f"{idx}号背景"
        color = style_color_for(style_id)
        options.append(
            {
                "id": style_id,
                "name": display_name,
                "rawName": style_name,
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
    return options


def status_for(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "未找到"
    if candidates[0]["score"] >= 70:
        return "直接可用"
    if candidates[0]["score"] >= 45:
        return "需人工确认"
    return "弱匹配"


def quality_config(value: str | None) -> dict[str, Any]:
    key = str(value or "standard")
    return QUALITY_OPTIONS.get(key, QUALITY_OPTIONS["standard"])


def quality_options_payload() -> list[dict[str, Any]]:
    return [dict(option) for option in QUALITY_OPTIONS.values()]


def points_for(status: str, action: str, kind: str, quality: str | None = "standard") -> int:
    return int(quality_config(quality)["points"])


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
        "premiumImagePoints": PREMIUM_IMAGE_POINTS,
        "premiumImageCash": round(PREMIUM_IMAGE_POINTS / POINT_RATE, 2),
        "qualityDefault": "standard",
        "qualityOptions": quality_options_payload(),
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


def current_user_id() -> str:
    return str(request.headers.get("X-User-Id") or request.args.get("userId") or billing.DEFAULT_USER_ID)


def account_payload(user_id: str | None = None) -> dict[str, Any]:
    account = billing.account_payload(user_id or billing.DEFAULT_USER_ID)
    package_names = {49: "体验充值", 99: "整店常用", 299: "小团队包"}
    package_base_points = {49: 490, 99: 990, 299: 2990}
    for package in account["packages"]:
        cash = package["cash"]
        total_points = package["points"]
        base_points = package_base_points.get(cash, total_points)
        package["name"] = package_names.get(cash, package["name"])
        package["points"] = base_points
        package["bonus"] = max(total_points - base_points, 0)
    account["rate"] = f"1 元 = {POINT_RATE} 积分"
    account["customRecharge"] = {"minPoints": 100, "rate": POINT_RATE}
    account["referral"] = {"registerReward": 100, "firstPayReward": "20% 积分返利，封顶 500 积分", "expireDays": 180}
    account["pricing"] = pricing_payload()
    return account


def billing_json_error(exc: billing.BillingError):
    body, status = billing.billing_error_response(exc)
    return jsonify(body), status


def pipeline_payload() -> dict[str, Any]:
    tencent = tencent_status_payload()
    return {
        "provider": tencent["provider"],
        "imageEditApiReady": tencent["configured"],
        "objectStorageReady": bool(os.environ.get("OBJECT_STORAGE_BUCKET")),
        "expectedEnv": ["TENCENT_HUNYUAN_ENABLED", "TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_REGION", "PUBLIC_BASE_URL"],
        "tencent": tencent,
        "stages": ["菜单解析", "风格确认", "图库匹配", "统一背景", "预览导出"],
    }


def preview_sample_entries() -> list[dict[str, Any]]:
    menu = parse_menu()
    library = library_images()
    single_items = [item for item in menu["items"] if item.get("kind") == "单品"]
    seen_norms = {item.get("norm") for item in single_items}
    if len(single_items) < PREVIEW_SAMPLE_COUNT:
        for item in demo_menu_items():
            if item.get("kind") != "单品" or item.get("norm") in seen_norms:
                continue
            single_items.append({**item, "category": "风格样图"})
            seen_norms.add(item.get("norm"))
            if len(single_items) >= PREVIEW_SAMPLE_COUNT:
                break
    entries = []
    for item in single_items[:PREVIEW_SAMPLE_COUNT]:
        if not item.get("norm"):
            item = {**item, "norm": normalize(str(item.get("name") or ""))}
        candidates = top_candidates(item, library)
        entries.append({"item": item, "candidates": candidates})
    return entries


def preview_sample_payload_from_entry(selected_style: str, entry: dict[str, Any], generate: bool = True) -> dict[str, Any]:
    item = entry["item"]
    candidates = entry["candidates"]
    preview_item = {**item, "candidates": candidates}
    candidate = None
    generation: dict[str, Any]
    if generate:
        candidate, generation = materialize_preview_candidate(preview_item, selected_style, "standard")
    else:
        candidate = generated_preview_candidate(preview_item, selected_style)
        generation = (
            {"status": "cached", "provider": candidate.get("aiProvider") or "tencent-hunyuan", "action": candidate.get("generationAction") or "Cached"}
            if candidate
            else {"status": "pending", "provider": "tencent-hunyuan" if tencent_ready() else "local-demo", "action": "Preview"}
        )
    public_status = "免费样图"
    if generation.get("status") == "failed":
        public_status = "样图生成失败"
    elif generation.get("status") in {"pending", "limited"}:
        public_status = "等待生成"
    return {**item, "candidate": candidate, "sourceCandidates": candidates[:3], "generation": generation, "points": 0, "publicStatus": public_status}


def preview_sample_payload(selected_style: str, index: int, generate: bool = True) -> dict[str, Any]:
    entries = preview_sample_entries()
    if index < 0 or index >= len(entries):
        raise IndexError("样图序号不存在")
    return preview_sample_payload_from_entry(selected_style, entries[index], generate=generate)


def preview_samples(selected_style: str, generate: bool = False) -> dict[str, Any]:
    entries = preview_sample_entries()
    samples = [preview_sample_payload_from_entry(selected_style, entry, generate=generate) for entry in entries]
    return {
        "style": selected_style,
        "styleName": STYLE_COLORS.get(selected_style, ("上传风格", None, None))[0],
        "samples": samples,
        "previewFreeImages": PREVIEW_SAMPLE_COUNT,
    }


def build_plan(selected_style: str = "", quality: str | None = "standard") -> dict[str, Any]:
    menu = parse_menu()
    library = library_images()
    requested_style = selected_style
    quality_info = quality_config(quality)
    results = []
    for item in menu["items"]:
        candidates = top_candidates(item, library)
        components = component_matches(item, library) if item.get("kind") == "套餐/组合" else []
        original_status = status_for(candidates)
        results.append({**item, "status": original_status, "originalStatus": original_status, "candidates": candidates, "componentMatches": components})
    styles = style_options(results)
    selected_style = selected_style or (styles[0]["id"] if styles else "")
    for row in results:
        candidates = row["candidates"]
        if requested_style:
            same = next((c for c in candidates if c["styleId"] == selected_style), None)
            final_image = existing_ai_output_candidate(row, selected_style, quality_info["id"])
            if final_image:
                candidates.insert(0, final_image)
            elif same:
                candidates.insert(0, candidates.pop(candidates.index(same)))
        chosen = candidates[0] if candidates else None
        if not chosen:
            action = "需要定制/生成"
        elif not chosen.get("reusable", True):
            action = "需去水印/重绘"
        elif row["kind"] == "套餐/组合" and detect_kind(str(chosen.get("dishName") or ""), "") != "套餐/组合":
            action = "套餐组合生成"
        elif chosen.get("generated") and row["originalStatus"] == "未找到":
            action = "智能补图"
        elif chosen.get("generated"):
            action = "智能统一风格"
        else:
            action = "背景一致，直接复用" if chosen["styleId"] == selected_style else "需抠图换背景"
        row["backgroundAction"] = action
        if not chosen:
            public_status = "待补图"
        elif not chosen.get("reusable", True):
            public_status = "待处理"
        elif action == "背景一致，直接复用":
            public_status = "已生成"
        elif chosen.get("generated") and chosen.get("generationStatus") in {"succeeded", "cached"}:
            public_status = "已生成"
        elif action in {"智能统一风格", "需抠图换背景", "智能补图", "需要定制/生成", "套餐组合生成"}:
            public_status = "待正式生成"
        else:
            public_status = "待正式生成"
        row["publicStatus"] = public_status
        row["points"] = points_for(row["status"], action, row["kind"], quality_info["id"])
    total_points = sum(int(row.get("points") or 0) for row in results)
    summary = {
        "total": len(results),
        "direct": sum(1 for r in results if r["backgroundAction"] == "背景一致，直接复用"),
        "review": sum(1 for r in results if r["backgroundAction"] in {"智能统一风格", "需抠图换背景"}),
        "missing": sum(1 for r in results if r["backgroundAction"] in {"智能补图", "需要定制/生成", "需去水印/重绘", "套餐组合生成"}),
        "reuse": sum(1 for r in results if r["backgroundAction"] == "背景一致，直接复用"),
        "bgReplace": sum(1 for r in results if r["backgroundAction"] in {"智能统一风格", "需抠图换背景"}),
        "custom": sum(1 for r in results if r["backgroundAction"] in {"智能补图", "需要定制/生成", "需去水印/重绘", "套餐组合生成"}),
        "points": total_points,
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
        "quality": quality_info,
        "quote": {
            "package": f"{quality_info['name']} · 按张正式出图",
            "cash": round(summary["points"] / POINT_RATE, 2),
            "points": summary["points"],
            "rate": f"1 元 = {POINT_RATE} 积分",
            "addOns": [
                {"name": "风格预览", "price": f"免费 {PREVIEW_SAMPLE_COUNT} 张样图"},
                {"name": "正式出图", "price": f"{quality_info['points']} 积分/张"},
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


def save_platform_image(img: Image.Image, target: Path, image_format: str, max_kb: int) -> int:
    max_bytes = max(64, int(max_kb)) * 1024
    if image_format in {"jpg", "jpeg"}:
        rgb = img.convert("RGB")
        for quality in range(92, 61, -5):
            rgb.save(target, "JPEG", quality=quality, optimize=True, progressive=True)
            if target.stat().st_size <= max_bytes:
                return target.stat().st_size
        rgb.save(target, "JPEG", quality=60, optimize=True, progressive=True)
        return target.stat().st_size
    rgb = img.convert("RGB")
    rgb.save(target, "PNG", optimize=True)
    if target.stat().st_size > max_bytes:
        rgb.quantize(colors=256).convert("RGB").save(target, "PNG", optimize=True)
    return target.stat().st_size


def export_zip(
    selected_style: str,
    scope: str = "all",
    selected_rows: list[int] | None = None,
    image_format: str = "jpg",
    watermark: dict[str, Any] | None = None,
    platforms: list[str] | str | None = None,
    quality: str | None = "standard",
) -> dict[str, Any]:
    plan = build_plan(selected_style, quality)
    run_dir = EXPORT_DIR / f"export_{int(time.time())}"
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    selected = set(selected_rows or [])
    selected_platforms = parse_platforms(platforms)
    image_format = image_format.lower()
    if image_format not in {"jpg", "jpeg"}:
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
                    file_size = save_platform_image(img, target, image_format, int(spec.get("maxKB", 500)))
                    copied = str(target)
                    images += 1
                    rows.append({"菜品名": row["name"], "分类": row["category"], "类型": row["kind"], "平台": spec["name"], "尺寸": f"{spec['width']}x{spec['height']}", "文件大小KB": round(file_size / 1024, 1), "平台上限KB": spec.get("maxKB", 500), "图片状态": "已生成", "预计积分": row["points"], "品牌水印": "已添加" if watermark_enabled else "未添加", "交付文件": f"{platform_dir.name}/{target.name}"})
        else:
            rows.append({"菜品名": row["name"], "分类": row["category"], "类型": row["kind"], "平台": "", "尺寸": "", "文件大小KB": "", "平台上限KB": "", "图片状态": "待补图", "预计积分": row["points"], "品牌水印": "未添加", "交付文件": ""})
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


@app.get("/download-menu-template")
def download_menu_template():
    rows = [
        {"分类": "热销", "菜品名": "老长沙辣椒炒肉盖码饭", "价格": "19.8", "类型": "单品", "套餐内容/规格": "", "备注": "菜品名为必填"},
        {"分类": "热销", "菜品名": "小炒黄牛肉盖码饭", "价格": "25.8", "类型": "单品", "套餐内容/规格": "", "备注": ""},
        {"分类": "套餐", "菜品名": "辣椒炒肉+茄子肉末盖码饭", "价格": "24.8", "类型": "套餐/组合", "套餐内容/规格": "辣椒炒肉；茄子肉末；米饭", "备注": "套餐建议写清包含菜品"},
        {"分类": "小吃饮品", "菜品名": "紫菜蛋花汤", "价格": "3.9", "类型": "饮品/小食", "套餐内容/规格": "", "备注": ""},
    ]
    output = io.BytesIO()
    pd.DataFrame(rows).to_excel(output, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="外卖菜品菜单模板.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/plan")
def api_plan():
    return jsonify(build_plan(request.args.get("style", ""), request.args.get("quality", "standard")))


@app.get("/api/style-preview")
def api_style_preview():
    style = request.args.get("style", "")
    if not style:
        return jsonify({"error": "请先选择风格"}), 400
    return jsonify(preview_samples(style, generate=False))


@app.get("/api/style-preview-sample")
def api_style_preview_sample():
    style = request.args.get("style", "")
    if not style:
        return jsonify({"error": "请先选择风格"}), 400
    try:
        index = int(request.args.get("index", "0"))
        return jsonify({"style": style, "index": index, "sample": preview_sample_payload(style, index, generate=True)})
    except IndexError:
        return jsonify({"error": "样图序号不存在"}), 404


@app.get("/api/menu-status")
def api_menu_status():
    path = current_menu_path()
    if path is None:
        return jsonify({"uploaded": False})
    menu = parse_menu(path)
    return jsonify({"uploaded": True, "menu": {k: v for k, v in menu.items() if k != "items"}})


@app.get("/api/account")
def api_account():
    return jsonify(account_payload(current_user_id()))


@app.post("/api/recharge")
def api_recharge():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or current_user_id())
    order_id = str(payload.get("orderId") or f"recharge_{int(time.time() * 1000)}")
    try:
        if payload.get("points") is not None:
            points = int(payload.get("points") or 0)
            if points < 100:
                raise billing.InvalidRechargePackage("自定义充值最低 100 积分起充", points=points)
            result = billing.credit_account(
                user_id,
                order_id,
                points,
                description="custom-recharge",
                metadata={"cash": round(points / POINT_RATE, 2), "custom": True},
            )
        else:
            result = billing.credit_recharge(user_id, order_id, payload.get("cash"))
        return jsonify({"ok": True, "transaction": result, "account": account_payload(user_id)})
    except billing.BillingError as exc:
        return billing_json_error(exc)


@app.post("/api/debit")
def api_debit():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or current_user_id())
    order_id = str(payload.get("orderId") or f"debit_{int(time.time() * 1000)}")
    try:
        points = int(payload.get("points") or 0)
        if points <= 0:
            points = billing.calculate_image_charge(
                image_count=payload.get("imageCount", payload.get("images", 0)),
                quality=payload.get("quality", "standard"),
                watermark=bool(payload.get("watermark", False)),
                platforms=payload.get("platforms"),
                platform_count=payload.get("platformCount"),
            )
        result = billing.debit_account(
            user_id,
            order_id,
            points,
            description=str(payload.get("description") or "扣除积分"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
        return jsonify({"ok": True, "transaction": result, "account": account_payload(user_id)})
    except billing.BillingError as exc:
        return billing_json_error(exc)


@app.post("/api/refund")
def api_refund():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or current_user_id())
    source_order_id = str(payload.get("sourceOrderId") or payload.get("orderId") or int(time.time() * 1000))
    order_id = f"refund_{source_order_id}"
    try:
        points = int(payload.get("points") or 0)
        if points <= 0:
            raise billing.InvalidBillingInput("退款积分必须大于 0", points=points)
        result = billing.credit_account(
            user_id,
            order_id,
            points,
            description=str(payload.get("description") or "生成失败退回积分"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
        return jsonify({"ok": True, "transaction": result, "account": account_payload(user_id)})
    except billing.BillingError as exc:
        return billing_json_error(exc)


@app.get("/api/pipeline-config")
def api_pipeline_config():
    return jsonify(pipeline_payload())


@app.get("/api/library-status")
def api_library_status():
    library = library_images()
    sources: dict[str, int] = {}
    reusable = 0
    for image in library:
        sources[image.source] = sources.get(image.source, 0) + 1
        if image.reusable:
            reusable += 1
    return jsonify(
        {
            "total": len(library),
            "reusable": reusable,
            "sources": sources,
            "stores": len({image.store for image in library}),
            "styles": len({image.style_id for image in library}),
            "externalDirs": [str(path) for path in configured_library_dirs()],
        }
    )


@app.get("/api/tencent-status")
def api_tencent_status():
    return jsonify(tencent_status_payload())


@app.post("/api/generate-final")
def api_generate_final():
    payload = request.get_json(silent=True) or {}
    style = str(payload.get("style") or "")
    quality = str(payload.get("quality") or "standard")
    if not style:
        return jsonify({"error": "请先选择风格"}), 400
    plan = build_plan(style, quality)
    plan["generation"] = materialize_final_images(plan, style, quality)
    return jsonify(plan)


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
    library_images.cache_clear()
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
    quality = str(payload.get("quality", "standard"))
    style = str(payload.get("style", ""))
    plan = build_plan(style, quality)
    export_results = prepare_results_for_export(plan["results"], style)
    return jsonify(
        export_delivery_zip(
            export_results,
            EXPORT_DIR,
            scope=str(payload.get("scope", "all")),
            selected_rows=selected_rows,
            image_format=str(payload.get("format", "jpg")),
            watermark=watermark,
            platforms=platforms,
        )
    )


@app.get("/media/<path:name>")
def media(name: str):
    return send_from_directory(LIBRARY_DIR, name)


@app.get("/external-media/<path:name>")
def external_media(name: str):
    path = external_image_path(name)
    if path is None:
        return jsonify({"error": "图片不存在或未授权"}), 404
    return send_file(path)


@app.get("/model-inputs/<path:name>")
def model_inputs(name: str):
    if not re.fullmatch(r"[a-f0-9]{24}\.jpg", name):
        return jsonify({"error": "图片不存在"}), 404
    return send_from_directory(MODEL_INPUT_DIR, name)


@app.get("/download/<path:name>")
def download(name: str):
    return send_file(EXPORT_DIR / name, as_attachment=True)


app.register_blueprint(
    create_admin_blueprint(
        AdminDependencies(
            library_images=library_images,
            media_url_for_path=media_url_for_path,
            current_menu_path=current_menu_path,
            parse_menu=parse_menu,
            upload_dir=UPLOAD_DIR,
        )
    )
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port)
