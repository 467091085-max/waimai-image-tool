from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import threading
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
import generation_engine
import generation_jobs
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageFont, ImageOps

from admin_panel import AdminDependencies, create_admin_blueprint
from image_pipeline import PLATFORMS, export_delivery_zip, require_platforms
from matching_engine import (
    MATCH_REASON_UNMATCHED,
    assess_match as engine_assess_match,
    classify_kind as engine_classify_kind,
    grams as engine_grams,
    normalize_dish,
    semantic_family as engine_semantic_family,
    similarity as engine_similarity,
    split_components as engine_split_components,
    strict_match_allowed as engine_strict_match_allowed,
)
from menu_parser import parse_menu as parse_excel_menu

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LIBRARY_DIR = DATA_DIR / "library"
EXPORT_DIR = DATA_DIR / "exports"
MODEL_INPUT_DIR = DATA_DIR / "model_inputs"
GALLERY_UPLOAD_DIR = DATA_DIR / "gallery_uploads"
for folder in (UPLOAD_DIR, LIBRARY_DIR, EXPORT_DIR, MODEL_INPUT_DIR, GALLERY_UPLOAD_DIR):
    folder.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MENU_EXTS = {".xls", ".xlsx"}
DEFAULT_LIBRARY_SOURCE_DIRS = [
    "/Users/guiguixiaxia/Documents/cleanpic",
    "/Users/guiguixiaxia/Documents/watermarkpic",
]
REMOTE_LIBRARY_INDEX_ENV_VARS = ("COS_LIBRARY_INDEX_URL", "LIBRARY_INDEX_URL")
LIBRARY_INDEX_TIMEOUT = int(os.environ.get("LIBRARY_INDEX_TIMEOUT", "12"))
_REMOTE_MEDIA_URLS: dict[str, str] = {}
_LIBRARY_INDEX_STATUS: dict[str, Any] = {
    "remoteIndex": False,
    "remoteImages": 0,
    "indexImages": 0,
    "indexSource": "",
    "indexError": "",
}
POINT_RATE = billing.POINT_RATE
BASE_IMAGE_POINTS = billing.QUALITY_POINTS["standard"]
PREMIUM_IMAGE_POINTS = billing.QUALITY_POINTS["premium"]
CUSTOM_EDIT_POINTS = billing.CUSTOM_EDIT_POINTS
WATERMARK_POINTS = billing.WATERMARK_POINTS
EXTRA_PLATFORM_POINTS = billing.EXTRA_PLATFORM_POINTS
PREVIEW_SAMPLE_COUNT = 6
STYLE_BACKGROUND_GENERATION_ATTEMPT_LIMIT = int(os.environ.get("STYLE_BACKGROUND_GENERATION_ATTEMPT_LIMIT", str(PREVIEW_SAMPLE_COUNT * 3)))
DEMO_BALANCE_POINTS = int(os.environ.get("DEMO_BALANCE_POINTS", "1880"))
TENCENT_AIART_HOST = "aiart.tencentcloudapi.com"
TENCENT_AIART_SERVICE = "aiart"
TENCENT_AIART_VERSION = "2022-12-29"
TENCENT_HUNYUAN_HOST = "hunyuan.tencentcloudapi.com"
TENCENT_HUNYUAN_SERVICE = "hunyuan"
TENCENT_HUNYUAN_VERSION = "2023-09-01"
TENCENT_REQUEST_TIMEOUT = int(os.environ.get("TENCENT_REQUEST_TIMEOUT", "55"))
TENCENT_IMAGE3_POLL_TIMEOUT = int(os.environ.get("TENCENT_IMAGE3_POLL_TIMEOUT", "150"))
TENCENT_IMAGE3_POLL_INTERVAL = max(1, int(os.environ.get("TENCENT_IMAGE3_POLL_INTERVAL", "3")))
TENCENT_IMAGE3_ENABLED = os.environ.get("TENCENT_IMAGE3_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
TENCENT_IMAGE3_FALLBACK_TO_LITE = os.environ.get("TENCENT_IMAGE3_FALLBACK_TO_LITE", "true").lower() not in {"0", "false", "no", "off"}
TENCENT_SYNC_LIMIT = int(os.environ.get("TENCENT_HUNYUAN_SYNC_LIMIT", "6"))
GENERATION_JOB_SYNC_BATCH_SIZE = int(os.environ.get("GENERATION_JOB_SYNC_BATCH_SIZE", str(max(1, TENCENT_SYNC_LIMIT if TENCENT_SYNC_LIMIT > 0 else 6))))
GENERATION_JOB_ASYNC_RETURN_GRACE_MS = int(os.environ.get("GENERATION_JOB_ASYNC_RETURN_GRACE_MS", "50"))
DEFAULT_TENCENT_COS_BUCKET = "waimai-image-tool-inputs-1311836560"
DEFAULT_TENCENT_COS_REGION = "ap-guangzhou"
DEFAULT_GALLERY_COS_PREFIX = "waimai-gallery"
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
_ACTIVE_GENERATION_JOB_THREADS: dict[str, threading.Thread] = {}
_ACTIVE_GENERATION_JOB_LOCK = threading.Lock()

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
    remote_url: str = ""
    cos_key: str = ""
    index_source: str = ""
    reference_only: bool = False
    has_brand_watermark: bool = False
    has_dish_text: bool = False
    quality_score: float | None = None
    review_reasons: tuple[str, ...] = ()


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

STYLE_BACKGROUND_VARIANTS = (
    "温暖原木餐桌，奶油白墙面，柔和自然窗光，适合家常热菜",
    "深色石板桌面，低饱和暗背景，柔和侧逆光，适合高级餐厅质感",
    "浅灰微水泥台面，明亮极简棚拍，干净留白，适合清爽平台主图",
    "红色暖调背景，节日热卖氛围但不含文字，适合促销款菜品",
    "竹编与浅木色自然背景，中式清爽质感，适合粉面饭与小炒",
    "冷灰蓝陶瓷砖与浅色台面，现代清爽光线，适合轻食和盖饭",
    "米白瓷盘搭配浅绿色点缀，通透自然光，适合健康餐",
    "暖灰布纹桌面与米色背景，柔和餐厅光，适合套餐组合",
)

NEGATIVE_IMAGE_PROMPT = "文字，水印，logo，品牌名，价格，人物，手，低清晰度，模糊，变形，裁切主体，脏乱背景"
STRICT_MATCH_MIN_SCORE = 0.70
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
STYLE_SAMPLE_PREFERRED_WORDS = (
    "辣椒炒肉",
    "小炒黄牛肉",
    "黄牛肉",
    "螺蛳粉",
    "桂林米粉",
    "米粉",
    "米线",
    "红烧肉",
    "回锅肉",
    "番茄炒蛋",
    "茄子",
    "招牌",
    "盖码饭",
    "盖浇饭",
    "木桶饭",
    "牛肉",
    "鸡",
)
STYLE_SAMPLE_SIDE_WORDS = (
    "辣椒包",
    "陈醋",
    "生抽",
    "白糖",
    "蒜粒",
    "大蒜头",
    "香菜沫",
    "蘸碟",
    "蘸料",
    "料汁",
    "酱汁",
    "海带丝",
    "鸭爪",
    "鸭头",
    "鸭肾",
    "兰花干",
    "鱼豆腐",
    "牛丸",
    "撒尿牛丸",
    "小丸子",
    "汤圆",
    "茶叶蛋",
    "卤蛋",
    "泡萝卜",
    "泡黄瓜",
    "饮料",
    "饮品",
)
STYLE_SAMPLE_BAD_WORDS = tuple(
    sorted(
        GENERIC_MATCH_WORDS
        | {
            "背景",
            "勿点",
            "不要",
            "不需要",
            "温馨提示",
            "提示",
            "收藏",
            "宠粉",
            "福利",
            "起点",
            "加购",
            "加料",
            "加粉",
            "加汤",
            "加饭",
            "蘸碟",
            "蘸料",
            "料汁",
            "酱汁",
            "酱料",
            "调料",
            "陈醋",
            "生抽",
            "白糖",
            "辣椒包",
            "蒜粒",
            "大蒜头",
            "香菜沫",
            "辣椒油",
            "餐具",
            "发票",
            "部分肉",
            "绳子",
            "可乐",
            "雪碧",
            "王老吉",
            "矿泉水",
            "冰红茶",
            "加多宝",
            "北冰洋",
            "豆奶",
            "美年达",
            "豆浆",
            "饮料",
            "饮品",
        },
        key=len,
        reverse=True,
    )
)

PREVIEW_SAMPLE_BAD_WORDS = STYLE_SAMPLE_BAD_WORDS + (
    "盛夏",
    "冰沙",
    "提示图",
    "小料",
    "配料",
    "加菜",
    "餐盒",
    "主食",
    "白米饭",
    "白饭",
    "汤圆",
    "茶叶蛋",
    "卤蛋",
    "泡萝卜",
    "泡黄瓜",
    "小食",
    "单点",
)


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


def style_accent_for(style_id: str) -> tuple[int, int, int]:
    if style_id in STYLE_COLORS:
        return STYLE_COLORS[style_id][2]
    digest = hashlib.sha1(f"accent:{style_id}".encode("utf-8")).digest()
    return (58 + digest[0] % 156, 54 + digest[1] % 150, 48 + digest[2] % 145)


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


def tencent_style_backgrounds_enabled() -> bool:
    return env_truthy("GENERATE_STYLE_BACKGROUNDS_WITH_TENCENT", default=tencent_ready())


def allow_local_image_fallback() -> bool:
    """Use local drawn images only when an explicit development flag enables it."""
    return env_truthy("ALLOW_LOCAL_IMAGE_FALLBACK", default=False)


def tencent_status_payload() -> dict[str, Any]:
    cfg = tencent_config()
    cos = tencent_cos_config()
    ready = tencent_ready()
    missing = []
    if not cfg["enabled"]:
        missing.append("TENCENT_HUNYUAN_ENABLED=true")
    if not cfg["secret_id"]:
        missing.append("TENCENTCLOUD_SECRET_ID")
    if not cfg["secret_key"]:
        missing.append("TENCENTCLOUD_SECRET_KEY")
    return {
        "provider": "tencent-hunyuan",
        "status": generation_engine.STATUS_SUCCEEDED if ready else generation_engine.STATUS_QUEUED,
        "providerStatus": generation_engine.STATUS_SUCCEEDED if ready else generation_engine.STATUS_QUEUED,
        "provider_status": generation_engine.STATUS_SUCCEEDED if ready else generation_engine.STATUS_QUEUED,
        "reason": "ready" if ready else generation_engine.STRATEGY_WAITING_FOR_PROVIDER,
        "provider_error": None if ready else generation_engine.WAITING_FOR_PROVIDER_ERROR,
        "providerError": None if ready else generation_engine.WAITING_FOR_PROVIDER_ERROR,
        "retryable": not ready,
        "refund_required": False,
        "refundRequired": False,
        "fallbackProvider": "local-demo" if allow_local_image_fallback() else "",
        "configured": ready,
        "enabled": cfg["enabled"],
        "region": cfg["region"],
        "mode": cfg["mode"],
        "syncLimit": TENCENT_SYNC_LIMIT,
        "image3Enabled": TENCENT_IMAGE3_ENABLED,
        "image3FallbackToLite": TENCENT_IMAGE3_FALLBACK_TO_LITE,
        "image3PollTimeout": TENCENT_IMAGE3_POLL_TIMEOUT,
        "styleBackgroundsLive": tencent_style_backgrounds_enabled(),
        "cosReady": cos["ready"],
        "cosBucket": cos["bucket"] if cos["ready"] else "",
        "cosRegion": cos["region"],
        "missing": missing,
    }


def tencent_cos_config() -> dict[str, Any]:
    cfg = tencent_config()
    bucket = os.environ.get("TENCENT_COS_BUCKET", DEFAULT_TENCENT_COS_BUCKET).strip()
    region = os.environ.get("TENCENT_COS_REGION", cfg["region"] or DEFAULT_TENCENT_COS_REGION).strip() or DEFAULT_TENCENT_COS_REGION
    prefix = os.environ.get("TENCENT_COS_PREFIX", "waimai-model-inputs").strip().strip("/") or "waimai-model-inputs"
    return {
        "bucket": bucket,
        "region": region,
        "prefix": prefix,
        "secret_id": cfg["secret_id"],
        "secret_key": cfg["secret_key"],
        "ready": bool(bucket and cfg["secret_id"] and cfg["secret_key"]),
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


def require_result_image(response: dict[str, Any], action: str) -> str:
    image = response.get("ResultImage")
    if isinstance(image, list):
        image = image[0] if image else ""
    image_text = str(image or "").strip()
    if not image_text:
        request_id = response.get("RequestId") or ""
        suffix = f" requestId={request_id}" if request_id else ""
        raise RuntimeError(f"腾讯混元 {action} 未返回 ResultImage{suffix}")
    return image_text


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
    raw_url = str(candidate.get("url") or "")
    if is_public_http_url(raw_url):
        return candidate_public_url({"url": raw_url})
    path_text = str(candidate.get("path") or "")
    path = Path(path_text) if path_text else None
    if path and path.exists() and path.suffix.lower() in IMAGE_EXTS:
        target = prepare_model_input_file(path)
        cos_url = upload_model_input_to_cos(target)
        if cos_url:
            return cos_url
        return ""
    return candidate_public_url(candidate)


def model_input_unavailable_message(candidate: dict[str, Any] | None) -> str:
    path_text = str((candidate or {}).get("path") or "")
    if path_text and Path(path_text).exists():
        return "当前参考图是本地路径，需配置 TENCENT_COS_BUCKET/TENCENT_COS_REGION 后上传为模型输入，不能直接传 Render 或 Mac 本地路径"
    return "当前参考图没有可访问的远程 URL，也没有可上传的本地图片路径"


def prepare_model_input_file(path: Path) -> Path:
    stat = path.stat()
    digest_source = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"
    filename = f"{hashlib.sha1(digest_source.encode('utf-8')).hexdigest()[:24]}.jpg"
    target = MODEL_INPUT_DIR / filename
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.open(path).convert("RGB").save(target, "JPEG", quality=92, optimize=True)
    return target


def upload_model_input_to_cos(target: Path) -> str:
    cos = tencent_cos_config()
    if not cos["ready"]:
        return ""
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except Exception as exc:
        raise RuntimeError("已配置 TENCENT_COS_BUCKET，但缺少 cos-python-sdk-v5 依赖") from exc
    key = f"{cos['prefix']}/{target.name}"
    config = CosConfig(Region=cos["region"], SecretId=cos["secret_id"], SecretKey=cos["secret_key"], Scheme="https")
    client = CosS3Client(config)
    with target.open("rb") as file_obj:
        client.put_object(Bucket=cos["bucket"], Body=file_obj, Key=key, ContentType="image/jpeg")
    return client.get_presigned_url(Method="GET", Bucket=cos["bucket"], Key=key, Expired=3600)


def create_cos_client_from_config(cos: dict[str, Any]):
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except Exception as exc:
        raise RuntimeError("COS upload requires cos-python-sdk-v5") from exc
    config = CosConfig(Region=cos["region"], SecretId=cos["secret_id"], SecretKey=cos["secret_key"], Scheme="https")
    return CosS3Client(config)


def public_cos_url(bucket: str, region: str, key: str) -> str:
    if not bucket or not region or not key:
        return ""
    return f"https://{bucket}.cos.{region}.myqcloud.com/{urllib.parse.quote(key.lstrip('/'), safe='/%')}"


def gallery_cos_prefix() -> str:
    return (os.environ.get("TENCENT_COS_GALLERY_PREFIX") or os.environ.get("GALLERY_COS_PREFIX") or DEFAULT_GALLERY_COS_PREFIX).strip().strip("/") or DEFAULT_GALLERY_COS_PREFIX


def gallery_index_key(prefix: str | None = None) -> str:
    return f"{(prefix or gallery_cos_prefix()).strip().strip('/')}/index/library_index.jsonl"


def gallery_upload_token() -> str:
    return os.environ.get("GALLERY_UPLOAD_TOKEN", "").strip()


def gallery_upload_session_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", text):
        return hashlib.sha1(f"{time.time()}:{text}".encode("utf-8")).hexdigest()[:18]
    return text


def gallery_upload_session_path(session: str) -> Path:
    return GALLERY_UPLOAD_DIR / f"{gallery_upload_session_id(session)}.jsonl"


def gallery_upload_auth_error(payload: dict[str, Any] | None = None):
    expected = gallery_upload_token()
    if not expected:
        return "未配置 GALLERY_UPLOAD_TOKEN，图库远程上传接口已关闭"
    supplied = (
        request.headers.get("X-Gallery-Upload-Token")
        or request.headers.get("X-Admin-Upload-Token")
        or str((payload or {}).get("token") or "")
    ).strip()
    auth = request.headers.get("Authorization", "").strip()
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth.split(" ", 1)[1].strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return "图库上传 token 无效"
    return ""


def ensure_gallery_cos_ready() -> dict[str, Any]:
    cos = tencent_cos_config()
    if not cos["ready"]:
        raise RuntimeError("Render 未配置腾讯云 COS Secret/Bucket/Region，无法代传真实图库")
    cos["prefix"] = gallery_cos_prefix()
    return cos


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


def style_background_variant_for(style_id: str) -> str:
    match = re.search(r"(\d+)$", str(style_id or ""))
    if match:
        index = max(0, int(match.group(1)) - 1)
    else:
        index = int(hashlib.sha1(str(style_id).encode("utf-8")).hexdigest()[:4], 16)
    return STYLE_BACKGROUND_VARIANTS[index % len(STYLE_BACKGROUND_VARIANTS)]


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
    return generation_engine.prompt_for_generation(row, style_id, quality, prompt_type, style_prompt=style_prompt_for)


def tencent_text_to_image_lite(row: dict[str, Any], style_id: str, quality: str | None, target: Path) -> dict[str, Any]:
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
    save_result_image(require_result_image(response, "TextToImageLite"), target)
    return {
        "status": "succeeded",
        "provider": "tencent-hunyuan",
        "action": "TextToImageLite",
        "promptType": prompt_type,
        "requestId": response.get("RequestId"),
        "seed": response.get("Seed"),
        "endpoint": response.get("_Endpoint"),
    }


def tencent_submit_text_to_image3(row: dict[str, Any], style_id: str, quality: str | None, target: Path) -> dict[str, Any]:
    prompt_type = "combo" if row.get("kind") == "套餐/组合" else "text_to_image"
    submitted = tencent_api_request(
        "SubmitTextToImageJob",
        {
            "Prompt": prompt_for_generation(row, style_id, quality, prompt_type),
            "Resolution": output_resolution_for_style(style_id),
            "LogoAdd": 0,
            "Revise": 1,
        },
    )
    job_id = str(submitted.get("JobId") or "")
    if not job_id:
        raise RuntimeError("混元生图3.0未返回任务ID")

    deadline = time.monotonic() + max(1, TENCENT_IMAGE3_POLL_TIMEOUT)
    while True:
        latest = tencent_api_request("QueryTextToImageJob", {"JobId": job_id})
        status_code = str(latest.get("JobStatusCode") or "")
        status_msg = str(latest.get("JobStatusMsg") or "")
        if status_code == "5":
            save_result_image(require_result_image(latest, "QueryTextToImageJob"), target)
            return {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": "SubmitTextToImageJob",
                "queryAction": "QueryTextToImageJob",
                "promptType": prompt_type,
                "requestId": latest.get("RequestId") or submitted.get("RequestId"),
                "submitRequestId": submitted.get("RequestId"),
                "queryRequestId": latest.get("RequestId"),
                "jobId": job_id,
                "jobStatusCode": status_code,
                "jobStatusMsg": status_msg,
                "resultDetails": latest.get("ResultDetails"),
                "revisedPrompt": latest.get("RevisedPrompt"),
                "endpoint": latest.get("_Endpoint") or submitted.get("_Endpoint"),
            }
        if status_code == "4":
            err_code = latest.get("JobErrorCode") or "Image3JobFailed"
            err_msg = latest.get("JobErrorMsg") or status_msg or "处理失败"
            raise RuntimeError(f"混元生图3.0任务失败 {err_code}: {err_msg}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"混元生图3.0任务超时：{job_id}，当前状态 {status_code or 'unknown'} {status_msg}")
        time.sleep(TENCENT_IMAGE3_POLL_INTERVAL)


def tencent_text_to_image(row: dict[str, Any], style_id: str, quality: str | None, target: Path) -> dict[str, Any]:
    if TENCENT_IMAGE3_ENABLED:
        try:
            return tencent_submit_text_to_image3(row, style_id, quality, target)
        except Exception as image3_error:
            if not TENCENT_IMAGE3_FALLBACK_TO_LITE:
                raise
            try:
                detail = tencent_text_to_image_lite(row, style_id, quality, target)
                detail["fallback"] = True
                detail["fallbackFrom"] = "SubmitTextToImageJob"
                detail["fallbackMessage"] = str(image3_error)[:220]
                return detail
            except Exception as lite_error:
                raise combined_generation_error("混元生图3.0", image3_error, "极速版文生图", lite_error) from lite_error
    return tencent_text_to_image_lite(row, style_id, quality, target)


def prompt_for_style_background(style_id: str) -> str:
    return generation_engine.prompt_for_style_background(
        style_id,
        style_prompt=style_prompt_for,
        variant_prompt=style_background_variant_for(style_id),
    )


def tencent_style_background(style_id: str, target: Path) -> dict[str, Any]:
    source_candidate = style_background_seed_candidate()
    product_url = model_input_public_url(source_candidate)
    if not product_url:
        raise RuntimeError(f"{model_input_unavailable_message(source_candidate)}，无法调用商品背景生成")
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
    save_result_image(require_result_image(response, "ReplaceBackground"), target)
    return {
        "status": "succeeded",
        "provider": "tencent-hunyuan",
        "action": "ReplaceBackground",
        "promptType": "style_background",
        "requestId": response.get("RequestId"),
        "seed": response.get("Seed"),
        "endpoint": response.get("_Endpoint"),
    }


def tencent_replace_background(
    row: dict[str, Any],
    source_candidate: dict[str, Any],
    style_id: str,
    target: Path,
    quality: str | None = "standard",
    prompt_type: str | None = None,
) -> dict[str, Any]:
    product_url = model_input_public_url(source_candidate)
    if not product_url:
        raise RuntimeError(f"{model_input_unavailable_message(source_candidate)}，无法调用商品背景生成")
    prompt_type = prompt_type or ("combo_replace_background" if row.get("kind") == "套餐/组合" else "replace_background")
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
    save_result_image(require_result_image(response, "ReplaceBackground"), target)
    return {
        "status": "succeeded",
        "provider": "tencent-hunyuan",
        "action": "ReplaceBackground",
        "promptType": prompt_type,
        "requestId": response.get("RequestId"),
        "endpoint": response.get("_Endpoint"),
    }


def tencent_reference_redraw(row: dict[str, Any], source_candidate: dict[str, Any], style_id: str, target: Path, quality: str | None = "standard") -> dict[str, Any]:
    detail = tencent_replace_background(row, source_candidate, style_id, target, quality, prompt_type="watermark_redraw")
    detail["action"] = "ReferenceRedraw"
    detail["sourceAction"] = "ReplaceBackground"
    return detail


def normalize(text: str) -> str:
    return normalize_dish(text)


def grams(text: str) -> set[str]:
    return engine_grams(text)


def similarity(menu_name: str, image_name: str, menu_norm: str, image_norm: str, menu_grams: set[str], image_grams: set[str]) -> float:
    return engine_similarity(menu_name, image_name, menu_norm, image_norm, menu_grams, image_grams)


def has_any_word(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def has_sample_bad_word(text: str, words: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    for word in words:
        if not word:
            continue
        if len(word) <= 1:
            if compact == word:
                return True
            continue
        if word in compact:
            return True
    return False


def semantic_family(name: str, norm: str) -> str:
    family = engine_semantic_family(name, norm)
    if family in {"main_dish", "rice_noodle", "noodle", "porridge"}:
        return "food"
    return family


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
    return engine_strict_match_allowed(menu_name, image_name, menu_norm, image_norm, score)


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[/:*?\"<>|\\]+", "_", name)
    return re.sub(r"\s+", " ", name).strip()[:90] or "file"


def first_text_value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def configured_library_index_url() -> str:
    for name in REMOTE_LIBRARY_INDEX_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def configured_library_index_path() -> Path | None:
    value = os.environ.get("LIBRARY_INDEX_PATH", "").strip()
    return Path(value).expanduser() if value else None


def index_status_template() -> dict[str, Any]:
    return {
        "remoteIndex": False,
        "remoteImages": 0,
        "indexImages": 0,
        "indexSource": "",
        "indexError": "",
    }


def library_index_status_snapshot() -> dict[str, Any]:
    return dict(_LIBRARY_INDEX_STATUS)


def set_library_index_status(status: dict[str, Any]) -> None:
    _LIBRARY_INDEX_STATUS.clear()
    _LIBRARY_INDEX_STATUS.update(index_status_template())
    _LIBRARY_INDEX_STATUS.update(status)


def read_jsonl_records(raw: str) -> list[dict[str, Any]]:
    records = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        record = json.loads(text)
        if not isinstance(record, dict):
            raise ValueError(f"index line {line_no} is not an object")
        records.append(record)
    return records


def configured_library_index_records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    status = index_status_template()
    index_path = configured_library_index_path()
    index_url = configured_library_index_url()
    if index_path is not None:
        status["indexSource"] = str(index_path)
        try:
            raw = index_path.read_text(encoding="utf-8")
            records = read_jsonl_records(raw)
            status["indexImages"] = len(records)
            return records, status
        except Exception as exc:
            status["indexError"] = f"{type(exc).__name__}: {exc}"
            return [], status
    if index_url:
        status["remoteIndex"] = True
        status["indexSource"] = index_url
        try:
            req = urllib.request.Request(index_url, headers={"User-Agent": "waimai-image-tool/1.0"})
            with urllib.request.urlopen(req, timeout=LIBRARY_INDEX_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            records = read_jsonl_records(raw)
            status["indexImages"] = len(records)
            return records, status
        except Exception as exc:
            status["indexError"] = f"{type(exc).__name__}: {exc}"
            return [], status
    return [], status


def normalize_index_source(record: dict[str, Any]) -> str:
    raw = first_text_value(record, "source_kind", "source", "kind", "bucket") or "external"
    raw_text = f"{raw} {record.get('source_root') or ''} {record.get('relative_path') or ''} {record.get('path') or ''}".lower()
    text = f"{raw_text} {normalize(raw_text).lower()}"
    if "watermark" in text or "水印" in text:
        return "watermark"
    if "cleanpic" in text or "clean" in text or "可复用" in text:
        return "clean"
    return str(raw).strip() or "external"


def cos_url_from_key(cos_key: str, record: dict[str, Any]) -> str:
    if not cos_key:
        return ""
    base = first_text_value(record, "cos_base_url", "cdn_base_url", "base_url") or os.environ.get("COS_LIBRARY_BASE_URL", "").strip()
    if base:
        return f"{base.rstrip('/')}/{urllib.parse.quote(cos_key.lstrip('/'), safe='/%')}"
    bucket = first_text_value(record, "cos_bucket", "bucket") or os.environ.get("TENCENT_COS_BUCKET", DEFAULT_TENCENT_COS_BUCKET)
    region = first_text_value(record, "cos_region", "region") or os.environ.get("TENCENT_COS_REGION", DEFAULT_TENCENT_COS_REGION)
    if not bucket or not region:
        return ""
    return f"https://{bucket}.cos.{region}.myqcloud.com/{urllib.parse.quote(cos_key.lstrip('/'), safe='/%')}"


def remote_url_for_index_record(record: dict[str, Any]) -> str:
    remote_url = first_text_value(record, "remote_url", "url", "public_url", "image_url", "cos_url", "object_url")
    if remote_url.startswith(("http://", "https://")):
        return remote_url
    cos_key = first_text_value(record, "cos_key", "object_key", "key", "path_key")
    return cos_url_from_key(cos_key, record)


def index_record_local_path(record: dict[str, Any]) -> Path | None:
    path_text = first_text_value(record, "path", "local_path")
    if path_text:
        return Path(path_text).expanduser()
    source_root = first_text_value(record, "source_root", "root")
    relative_path = first_text_value(record, "relative_path", "rel_path")
    if source_root and relative_path:
        return Path(source_root).expanduser() / relative_path
    return None


def suffix_for_index_record(record: dict[str, Any], remote_url: str, local_path: Path | None) -> str:
    suffix = first_text_value(record, "suffix", "extension", "ext").lower()
    if not suffix and local_path is not None:
        suffix = local_path.suffix.lower()
    if not suffix and remote_url:
        suffix = Path(urllib.parse.urlsplit(remote_url).path).suffix.lower()
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"
    return suffix if suffix in IMAGE_EXTS else ".jpg"


def remote_library_path(image_id: str, dish: str, suffix: str, cos_key: str) -> Path:
    name_source = Path(cos_key).name if cos_key else f"{safe_filename(dish or image_id)}{suffix}"
    name = safe_filename(Path(name_source).stem) + suffix
    return Path("__remote_library__") / image_id / name


def register_remote_media(path: Path, remote_url: str) -> None:
    if remote_url:
        _REMOTE_MEDIA_URLS[str(path)] = remote_url


def library_image_url(image: LibraryImage) -> str:
    if image.remote_url:
        return image.remote_url
    return media_url_for_path(image.path)


def library_image_available(image: LibraryImage) -> bool:
    return bool(image.remote_url) or image.path.exists()


def library_image_from_index_record(record: dict[str, Any], index_source: str) -> LibraryImage | None:
    dish = first_text_value(record, "dish", "dish_name", "dishName", "name", "title") or Path(first_text_value(record, "relative_path", "path")).stem
    norm = first_text_value(record, "norm", "normalized", "normalized_dish") or normalize(dish)
    if not dish or not norm:
        return None
    source = normalize_index_source(record)
    store = first_text_value(record, "store", "store_name", "shop", "shop_name") or "remote"
    style_id = first_text_value(record, "style_id", "styleId", "style") or stable_style_id(store, source)
    remote_url = remote_url_for_index_record(record)
    cos_key = first_text_value(record, "cos_key", "object_key", "key", "path_key")
    local_path = index_record_local_path(record)
    if not remote_url and (local_path is None or not local_path.exists()):
        return None
    suffix = suffix_for_index_record(record, remote_url, local_path)
    image_id = first_text_value(record, "image_id", "imageId", "id")
    if not image_id:
        seed = remote_url or (str(local_path) if local_path is not None else f"{source}:{store}:{dish}")
        image_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:18]
    reference_only = bool_value(record.get("reference_only"), False)
    reusable = bool_value(record.get("reusable"), not reference_only)
    if source == "watermark":
        reference_only = True
        reusable = False
    if reference_only:
        reusable = False
    path = remote_library_path(image_id, dish, suffix, cos_key) if remote_url else local_path
    if path is None:
        return None
    register_remote_media(path, remote_url)
    review_reasons_value = record.get("review_reasons") or record.get("reviewReasons") or []
    review_reasons = tuple(str(item) for item in review_reasons_value) if isinstance(review_reasons_value, list) else ()
    quality_score = record.get("quality_score")
    try:
        parsed_quality_score = float(quality_score) if quality_score is not None else None
    except (TypeError, ValueError):
        parsed_quality_score = None
    return LibraryImage(
        image_id=image_id,
        path=path,
        store=store,
        dish=dish,
        norm=norm,
        grams=grams(norm),
        style_id=style_id,
        source=source,
        reusable=reusable,
        remote_url=remote_url,
        cos_key=cos_key,
        index_source=index_source,
        reference_only=reference_only,
        has_brand_watermark=bool_value(record.get("has_brand_watermark"), source == "watermark"),
        has_dish_text=bool_value(record.get("has_dish_text"), False),
        quality_score=parsed_quality_score,
        review_reasons=review_reasons,
    )


def draw_demo_image(path: Path, dish: str, style_id: str) -> None:
    style_name = STYLE_COLORS.get(style_id, ("统一出图风格", None, None))[0]
    bg = style_color_for(style_id)
    accent = style_accent_for(style_id)
    img = Image.new("RGB", (900, 720), bg)
    draw = ImageDraw.Draw(img)
    shadow = tuple(max(0, c - 34) for c in bg)
    highlight = tuple(min(255, c + 28) for c in bg)
    draw.rectangle((0, 0, 900, 720), fill=bg)
    draw.polygon([(0, 0), (900, 0), (900, 190), (0, 330)], fill=highlight)
    draw.polygon([(0, 520), (900, 390), (900, 720), (0, 720)], fill=shadow)
    draw.ellipse((170, 108, 730, 628), fill=(248, 248, 244), outline=accent, width=14)
    draw.ellipse((238, 168, 662, 568), fill=(250, 244, 225))
    food_colors = [
        (190, 68, 42),
        (70, 145, 68),
        (228, 170, 60),
        (125, 77, 44),
        (230, 230, 210),
        accent,
    ]
    for idx, color in enumerate(food_colors):
        x0 = 282 + (idx % 3) * 92
        y0 = 235 + (idx // 3) * 104
        draw.rounded_rectangle((x0, y0, x0 + 175, y0 + 78), radius=30, fill=color)
    try:
        font_small = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 24)
    except Exception:
        font_small = ImageFont.load_default()
    if "背景风格样图" in dish:
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
    _REMOTE_MEDIA_URLS.clear()
    set_library_index_status(index_status_template())
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
    index_records, index_status = configured_library_index_records()
    index_images: list[LibraryImage] = []
    for record in index_records:
        image = library_image_from_index_record(record, str(index_status.get("indexSource") or ""))
        if image is not None:
            index_images.append(image)
    images.extend(index_images)
    index_status["remoteImages"] = sum(1 for image in index_images if image.remote_url)
    set_library_index_status(index_status)
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
        assessment = engine_assess_match(item["name"], image.dish, item["norm"], image.norm, score)
        if assessment and float(assessment["score"]) >= min_score:
            scored.append((float(assessment["score"]), str(assessment["match_reason"]), image))
    reusable = sorted((x for x in scored if x[2].reusable), key=lambda x: (x[0], x[2].source == "clean"), reverse=True)
    reference_only = sorted((x for x in scored if not x[2].reusable), key=lambda x: x[0], reverse=True)
    scored = (reusable + reference_only)[:limit]
    return [
        {
            "imageId": image.image_id,
            "candidate_id": image.image_id,
            "candidateId": image.image_id,
            "score": round(score * 100, 1),
            "confidence": round(score * 100, 1),
            "dishName": image.dish,
            "store": image.store,
            "styleId": image.style_id,
            "styleName": image_style_name(image),
            "source": image.source,
            "reusable": image.reusable,
            "referenceOnly": image.reference_only,
            "match_reason": reason,
            "matchReason": reason,
            "remoteUrl": image.remote_url,
            "cosKey": image.cos_key,
            "url": library_image_url(image),
            "path": str(image.path),
        }
        for score, reason, image in scored[:limit]
    ]


def component_matches(item: dict[str, Any], library: list[LibraryImage], limit: int = 4) -> list[dict[str, Any]]:
    matches = []
    for component in item.get("components") or []:
        norm = normalize(component)
        if len(norm) < 2:
            continue
        component_item = {**item, "name": component, "norm": norm}
        candidates = top_candidates(component_item, library, limit)
        matches.append({"name": component, "norm": norm, "candidates": candidates, **candidate_state_fields(candidates)})
    return matches


def media_url_for_path(path: Path) -> str:
    remote_url = _REMOTE_MEDIA_URLS.get(str(path))
    if remote_url:
        return remote_url
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


def candidate_from_library_image(image: LibraryImage, score: float = 100.0) -> dict[str, Any]:
    candidate = candidate_from_path(image.path, image.dish, image.style_id, image.source, score)
    candidate["imageId"] = image.image_id
    candidate["store"] = image.store
    candidate["source"] = image.source
    candidate["reusable"] = image.reusable
    candidate["referenceOnly"] = image.reference_only
    candidate["remoteUrl"] = image.remote_url
    candidate["cosKey"] = image.cos_key
    candidate["url"] = library_image_url(image)
    candidate["path"] = str(image.path)
    candidate["generated"] = False
    return candidate


def style_background_seed_candidate() -> dict[str, Any] | None:
    preferred_names = ("辣椒炒肉", "黄牛肉", "红烧肉", "盖码饭", "招牌")
    images = [image for image in library_images() if image.reusable]
    images.sort(key=lambda image: (not any(word in image.dish for word in preferred_names), image.store, image.dish))
    for image in images:
        candidate = candidate_from_path(image.path, image.dish, image.style_id, image.source, 100.0)
        if candidate_public_url(candidate) or candidate.get("path"):
            return candidate
    return None


def style_sample_rank(image: LibraryImage) -> tuple[int, str, str]:
    text = image.dish
    score = 0
    if image.source == "internal":
        score += 18
    elif image.source == "clean":
        score += 12
    if semantic_family(text, image.norm) == "food":
        score += 22
    else:
        score -= 60
    if detect_kind(text, "") == "单品":
        score += 16
    else:
        score -= 18
    if has_sample_bad_word(text, STYLE_SAMPLE_BAD_WORDS):
        score -= 120
    if has_sample_bad_word(text, STYLE_SAMPLE_SIDE_WORDS):
        score -= 42
    for index, word in enumerate(STYLE_SAMPLE_PREFERRED_WORDS):
        if word in text:
            score += max(8, 36 - index * 2)
    if 4 <= len(image.norm) <= 18:
        score += 8
    return score, image.store, image.dish


def style_representative_candidate(style_id: str) -> dict[str, Any] | None:
    images = [
        image
        for image in library_images()
        if image.style_id == style_id
        and image.reusable
        and image.store != "demo_store"
        and library_image_available(image)
    ]
    if not images:
        return None
    images.sort(key=style_sample_rank, reverse=True)
    if style_sample_rank(images[0])[0] < 30:
        return None
    candidate = candidate_from_library_image(images[0], 100.0)
    candidate["styleSampleSource"] = "library"
    candidate["needsGeneratedBackground"] = False
    candidate["needs_generated_background"] = False
    return candidate


def library_style_scores() -> dict[str, dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    for image in library_images():
        if not image.reusable or image.store == "demo_store" or not library_image_available(image):
            continue
        sample_rank = style_sample_rank(image)[0]
        if sample_rank < -80:
            continue
        data = scores.setdefault(
            image.style_id,
            {
                "styleId": image.style_id,
                "count": 0,
                "bestRank": -9999,
                "sourceScore": 0,
            },
        )
        data["count"] += 1
        data["bestRank"] = max(int(data["bestRank"]), sample_rank)
        if image.source == "internal":
            data["sourceScore"] = max(int(data["sourceScore"]), 3)
        elif image.source == "clean":
            data["sourceScore"] = max(int(data["sourceScore"]), 2)
        else:
            data["sourceScore"] = max(int(data["sourceScore"]), 1)
    return scores


def ordered_style_ids(results: list[dict[str, Any]]) -> list[str]:
    candidate_counts: dict[str, int] = {}
    for row in results:
        for candidate in row.get("candidates") or []:
            style_id = str(candidate.get("styleId") or "")
            if style_id:
                candidate_counts[style_id] = candidate_counts.get(style_id, 0) + 1

    library_scores = library_style_scores()
    ids = set(candidate_counts) | set(library_scores)
    ordered = sorted(
        ids,
        key=lambda style_id: (
            candidate_counts.get(style_id, 0),
            int(library_scores.get(style_id, {}).get("sourceScore") or 0),
            int(library_scores.get(style_id, {}).get("bestRank") or -9999),
            int(library_scores.get(style_id, {}).get("count") or 0),
            style_id,
        ),
        reverse=True,
    )

    for style_id in STYLE_COLORS:
        if style_id not in ordered:
            ordered.append(style_id)

    index = 1
    minimum_candidates = PREVIEW_SAMPLE_COUNT * 3
    while len(ordered) < minimum_candidates:
        style_id = f"generated-style-{index}"
        if style_id not in ordered:
            ordered.append(style_id)
        index += 1
    return ordered


def style_background_target(style_id: str) -> Path:
    return LIBRARY_DIR / "_style_backgrounds" / style_id / "背景风格样图.jpg"


@lru_cache(maxsize=512)
def background_signature_for_path(path_text: str) -> str:
    path = Path(path_text)
    try:
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.thumbnail((96, 96), Image.Resampling.BILINEAR)
            width, height = image.size
            strip = max(2, min(width, height) // 8)
            pixels = []
            regions = (
                (0, 0, width, strip),
                (0, max(0, height - strip), width, height),
                (0, 0, strip, height),
                (max(0, width - strip), 0, width, height),
            )
            for box in regions:
                crop = image.crop(box)
                pixels.extend(crop.getdata())
            if not pixels:
                return ""
            count = len(pixels)
            red = sum(pixel[0] for pixel in pixels) // count
            green = sum(pixel[1] for pixel in pixels) // count
            blue = sum(pixel[2] for pixel in pixels) // count
            return f"{red // 32}-{green // 32}-{blue // 32}"
    except Exception:
        return ""


@lru_cache(maxsize=512)
def image_hash_signature_for_path(path_text: str) -> str:
    path = Path(path_text)
    try:
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB").resize((16, 16), Image.Resampling.BILINEAR)
            pixels = list(image.getdata())
            if not pixels:
                return ""
            quantized = bytes(channel // 16 for pixel in pixels for channel in pixel)
            return hashlib.sha1(quantized).hexdigest()[:16]
    except Exception:
        return ""


def candidate_background_signature(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    path = str(candidate.get("path") or "")
    if path:
        signature = background_signature_for_path(path)
        if signature:
            return signature
    color = str(candidate.get("color") or "")
    style_id = str(candidate.get("styleId") or "")
    return color or style_id


def candidate_background_signatures(candidate: dict[str, Any] | None) -> set[str]:
    signatures: set[str] = set()
    primary = candidate_background_signature(candidate)
    if primary:
        signatures.add(f"bg:{primary}")
    path = str((candidate or {}).get("path") or "")
    if path:
        image_hash = image_hash_signature_for_path(path)
        if image_hash:
            signatures.add(f"img:{image_hash}")
    return signatures


def annotate_generated_style_background(candidate: dict[str, Any], status: str, provider: str, action: str, error: str = "") -> dict[str, Any]:
    candidate["generated"] = True
    candidate["styleSampleSource"] = "cache" if status == "cached" else "generated"
    candidate["needsGeneratedBackground"] = True
    candidate["needs_generated_background"] = True
    candidate["generationStatus"] = status
    candidate["generationProvider"] = provider
    candidate["generationAction"] = action
    if error:
        candidate["error"] = error[:220]
    return candidate


def style_background_placeholder_candidate(style_id: str, status: str, action: str, error: str = "") -> dict[str, Any]:
    target = style_background_target(style_id)
    candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 0.0)
    candidate["url"] = ""
    candidate["path"] = ""
    provider = "tencent-hunyuan"
    if status == "queued" and not error:
        error = generation_engine.WAITING_FOR_PROVIDER_ERROR
    candidate["provider_error"] = error if error else ""
    candidate["providerError"] = error if error else ""
    candidate["retryable"] = status in {"queued", "failed"}
    return annotate_generated_style_background(candidate, status, provider, action, error)


def style_background_candidate(style_id: str) -> dict[str, Any]:
    target = style_background_target(style_id)
    metadata = load_ai_output_metadata(target)
    if target.exists() and successful_model_metadata(metadata):
        candidate = candidate_from_path(target, "背景风格样图", style_id, "tencent-style-sample", 100.0)
        assert metadata is not None
        candidate_generation_metadata(candidate, metadata)
        return annotate_generated_style_background(candidate, "cached", "tencent-hunyuan", str(metadata.get("action") or "Cached"))
    if not tencent_ready():
        return style_background_placeholder_candidate(style_id, "queued", "WaitingForProvider", generation_engine.WAITING_FOR_PROVIDER_ERROR)
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
            return annotate_generated_style_background(candidate, "succeeded", "tencent-hunyuan", detail["action"])
        except Exception as exc:
            return style_background_placeholder_candidate(style_id, "failed", "ReplaceBackground", str(exc))
    return style_background_placeholder_candidate(style_id, "queued", "WaitingForProvider", generation_engine.WAITING_FOR_PROVIDER_ERROR)


def style_sample_is_real(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    source = str(candidate.get("source") or "")
    return bool(
        candidate.get("styleSampleSource") == "library"
        or (
            source in {"internal", "clean", "external"}
            and candidate.get("reusable", True)
            and not candidate.get("generated")
            and not candidate.get("generationStatus")
        )
    )


def style_card_source(candidate: dict[str, Any] | None) -> str:
    if style_sample_is_real(candidate):
        return "real"
    if candidate and (candidate.get("styleSampleSource") == "cache" or candidate.get("generationStatus") == "cached"):
        return "cache"
    return "generated"


def style_background_job(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if style_sample_is_real(candidate):
        return {"status": "reused", "provider": "library", "action": "LibraryStyleSample"}
    status = str((candidate or {}).get("generationStatus") or "pending")
    provider = str((candidate or {}).get("generationProvider") or (candidate or {}).get("aiProvider") or "tencent-hunyuan")
    action = str(
        (candidate or {}).get("generationAction")
        or ("WaitingForProvider" if status == "queued" else "GenerateStyleBackground" if status in {"pending", "failed"} else "Cached")
    )
    job = {"status": status, "provider": provider, "action": action}
    error = str((candidate or {}).get("error") or (candidate or {}).get("provider_error") or (candidate or {}).get("providerError") or "")
    if error:
        job["error"] = error
        job["provider_error"] = error
        job["providerError"] = error
    tencent = (candidate or {}).get("tencent") if isinstance((candidate or {}).get("tencent"), dict) else {}
    for key in ("requestId", "jobId", "endpoint"):
        value = tencent.get(key)
        if value:
            job[key] = value
    job["evidence"] = generation_evidence(provider, action, status, tencent)
    return job


def style_background_manifest(style_id: str, label: str, sample: dict[str, Any] | None) -> dict[str, Any] | None:
    if style_sample_is_real(sample):
        return None
    job = style_background_job(sample)
    return {
        "type": "style_background",
        "styleId": style_id,
        "label": label,
        "status": job["status"],
        "provider": job["provider"],
        "action": job["action"],
        "prompt": prompt_for_style_background(style_id),
        "reason": job.get("error") or "图库背景不足，需要 AI 补齐不同背景",
    }


def style_sample_candidate(style_id: str) -> dict[str, Any]:
    library_sample = style_representative_candidate(style_id)
    if library_sample:
        return library_sample
    return style_background_candidate(style_id)


def style_background_fill_candidate(style_id: str, generated_attempts: int, max_attempts: int) -> tuple[dict[str, Any], int]:
    if tencent_ready() and generated_attempts >= max_attempts:
        return (
            style_background_placeholder_candidate(
                style_id,
                "queued",
                "GenerateStyleBackground",
                "已达到本次背景生成安全上限，等待后续补图",
            ),
            generated_attempts,
        )
    candidate = style_background_candidate(style_id)
    status = str(candidate.get("generationStatus") or "")
    if tencent_ready() and status in {"succeeded", "failed", "queued"}:
        generated_attempts += 1
    return candidate, generated_attempts


def generated_preview_candidate(item: dict[str, Any], style_id: str) -> dict[str, Any] | None:
    if not style_id:
        return None
    target = LIBRARY_DIR / "_generated_previews" / style_id / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    if not target.exists():
        return None
    metadata = load_ai_output_metadata(target)
    if not usable_ai_output_metadata(metadata):
        return None
    candidate = candidate_from_path(target, item["name"], style_id, "generated-preview", 99.9)
    if metadata:
        candidate_generation_metadata(candidate, metadata)
    return candidate


def generation_evidence(provider: str, action: str, status: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence = {
        "provider": provider,
        "action": action,
        "status": status,
        "providerStatus": generation_engine.provider_status(status),
        "provider_status": generation_engine.provider_status(status),
    }
    source = detail or {}
    for key in generation_engine.EVIDENCE_KEYS:
        value = source.get(key)
        if value:
            evidence[key] = value
    return evidence


def queued_generation_payload(action: str = "WaitingForProvider") -> dict[str, Any]:
    evidence = generation_evidence("tencent-hunyuan", action, generation_engine.STATUS_QUEUED)
    return {
        "status": generation_engine.STATUS_QUEUED,
        "provider": "tencent-hunyuan",
        "action": action,
        "provider_error": generation_engine.WAITING_FOR_PROVIDER_ERROR,
        "providerError": generation_engine.WAITING_FOR_PROVIDER_ERROR,
        "error": generation_engine.WAITING_FOR_PROVIDER_ERROR,
        "retryable": True,
        "refund_required": False,
        "refundRequired": False,
        "reason": generation_engine.STRATEGY_WAITING_FOR_PROVIDER,
        "evidence": evidence,
    }


def materialize_preview_candidate(item: dict[str, Any], selected_style: str, quality: str | None = "standard") -> tuple[dict[str, Any] | None, dict[str, Any]]:
    target = LIBRARY_DIR / "_generated_previews" / selected_style / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    cached = generated_preview_candidate(item, selected_style)
    if cached:
        tencent_detail = cached.get("tencent") if isinstance(cached.get("tencent"), dict) else {}
        status = "cached"
        provider = str(cached.get("aiProvider") or "tencent-hunyuan")
        action = str(cached.get("generationAction") or "Cached")
        return cached, {
            "status": status,
            "provider": provider,
            "action": action,
            "requestId": tencent_detail.get("requestId"),
            "jobId": tencent_detail.get("jobId"),
            "error": cached.get("error"),
            "evidence": generation_evidence(provider, action, status, tencent_detail),
        }
    source_candidate = source_candidate_for_preview_generation(item)
    result: dict[str, Any] = {"status": "pending", "provider": "tencent-hunyuan", "action": "Preview"}
    if not tencent_ready():
        return None, queued_generation_payload()
    if tencent_ready():
        try:
            if source_candidate:
                detail = tencent_replace_background(item, source_candidate, selected_style, target, quality)
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
            return candidate, {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "requestId": detail.get("requestId"),
                "jobId": detail.get("jobId"),
                "evidence": generation_evidence("tencent-hunyuan", str(detail["action"]), "succeeded", detail),
            }
        except Exception as exc:
            failed_action = "ReplaceBackground" if source_candidate else "TextToImage"
            error = str(exc)[:220]
            result.update(
                {
                    "status": "failed",
                    "action": failed_action,
                    "error": error,
                    "provider_error": error,
                    "providerError": error,
                    "retryable": generation_engine.is_retryable_provider_error(error),
                    "evidence": generation_evidence("tencent-hunyuan", failed_action, "failed"),
                }
            )
            return None, result
    return None, queued_generation_payload()


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
    if not usable_ai_output_metadata(metadata):
        return None
    assert metadata is not None
    provider = str(metadata.get("provider") or candidate.get("aiProvider") or "")
    action = str(metadata.get("action") or "")
    candidate["aiProvider"] = provider
    candidate["generationStatus"] = "cached"
    candidate["generationAction"] = action
    candidate["generationProvider"] = provider
    candidate["source"] = f"tencent-{action}" if provider == "tencent-hunyuan" else "generated-final"
    if isinstance(metadata.get("tencent"), dict):
        candidate["tencent"] = metadata["tencent"]
    return candidate


def is_generated_candidate(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    source = str(candidate.get("source") or "")
    return bool(candidate.get("generated") or source.startswith("generated") or source.startswith("tencent"))


def source_candidates_for_generation(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for c in row.get("candidates") or []
        if c.get("path") and not is_generated_candidate(c) and candidate_matches_item(row, c)
    ]


def source_candidate_for_generation(row: dict[str, Any]) -> dict[str, Any] | None:
    candidates = source_candidates_for_generation(row)
    return next((c for c in candidates if c.get("reusable", True)), candidates[0] if candidates else None)


def candidate_matches_item(row: dict[str, Any], candidate: dict[str, Any]) -> bool:
    menu_name = str(row.get("name") or "")
    image_name = str(candidate.get("dishName") or "")
    menu_norm = str(row.get("norm") or normalize(menu_name))
    image_norm = normalize(image_name)
    if not menu_name or not image_name or not menu_norm or not image_norm:
        return False
    score = similarity(menu_name, image_name, menu_norm, image_norm, grams(menu_norm), grams(image_norm))
    return strict_match_allowed(menu_name, image_name, menu_norm, image_norm, score)


def source_candidate_for_preview_generation(row: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in source_candidates_for_generation(row):
        if candidate.get("reusable", True) and candidate_matches_item(row, candidate):
            return candidate
    return None


def strip_nonfinal_generated_candidates(row: dict[str, Any]) -> None:
    row["candidates"] = [
        c for c in row.get("candidates") or []
        if not is_generated_candidate(c) or c.get("aiProvider") == "tencent-hunyuan" or str(c.get("source") or "").startswith("tencent")
    ]


def strip_mismatched_source_candidates(row: dict[str, Any]) -> None:
    row["candidates"] = [
        c
        for c in row.get("candidates") or []
        if is_generated_candidate(c) or not c.get("path") or candidate_matches_item(row, c)
    ]


def reusable_selected_style_candidate(row: dict[str, Any], selected_style: str) -> dict[str, Any] | None:
    return next((c for c in source_candidates_for_generation(row) if c.get("styleId") == selected_style and c.get("reusable", True)), None)


def reusable_selected_style_preview_candidate(row: dict[str, Any], selected_style: str) -> dict[str, Any] | None:
    return next(
        (
            c
            for c in source_candidates_for_generation(row)
            if c.get("styleId") == selected_style and c.get("reusable", True) and candidate_matches_item(row, c)
        ),
        None,
    )


def materialization_reason(row: dict[str, Any], selected_style: str) -> str | None:
    if not selected_style:
        return "no_selected_style"
    sources = source_candidates_for_generation(row)
    selected_candidate = reusable_selected_style_candidate(row, selected_style)
    if selected_candidate:
        return None
    if row.get("kind") == "套餐/组合":
        return "combo"
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


def usable_ai_output_metadata(metadata: dict[str, Any] | None) -> bool:
    if successful_model_metadata(metadata):
        return True
    return bool(
        metadata
        and metadata.get("status") == "fallback"
        and metadata.get("provider") == "local-demo"
        and not tencent_ready()
        and allow_local_image_fallback()
    )


def final_ready_candidate(candidate: dict[str, Any], selected_style: str, action: str) -> bool:
    if candidate.get("aiProvider") == "tencent-hunyuan" or str(candidate.get("source") or "").startswith("tencent"):
        return True
    if candidate.get("aiProvider") == "local-demo" and not tencent_ready() and allow_local_image_fallback():
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
    if metadata.get("error"):
        candidate["error"] = str(metadata.get("error"))[:220]
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


def apply_queued_generation_row(row: dict[str, Any], item_result: dict[str, Any], reason: str | None = None) -> None:
    item_result.update(queued_generation_payload())
    item_result["reason"] = reason or generation_engine.STRATEGY_WAITING_FOR_PROVIDER
    item_result["attempted"] = False
    item_result["succeeded"] = False
    row["backgroundAction"] = "待正式生成"
    row["publicStatus"] = "待正式生成"
    row["generationStatus"] = generation_engine.STATUS_QUEUED
    row["generation"] = item_result


def bump_generation_action(generation: dict[str, Any], action: str) -> None:
    actions = generation.setdefault("actions", {})
    actions[action] = int(actions.get(action) or 0) + 1


def generation_action_for_strategy(strategy: str) -> str:
    return {
        generation_engine.STRATEGY_REUSE: "Reuse",
        generation_engine.STRATEGY_REPLACE_BACKGROUND: "ReplaceBackground",
        generation_engine.STRATEGY_REFERENCE_REDRAW: "ReferenceRedraw",
        generation_engine.STRATEGY_TEXT_TO_IMAGE3: "SubmitTextToImageJob",
        generation_engine.STRATEGY_TEXT_TO_IMAGE_LITE: "TextToImageLite",
        generation_engine.STRATEGY_WAITING_FOR_PROVIDER: "WaitingForProvider",
    }.get(str(strategy or ""), "Failed")


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
        request_data = generation_engine.select_generation_request(
            generation_engine.request_from_row(row, style=selected_style, quality=quality)
        )
        source_candidate = request_data.source_candidate
        item_result = generation_row_result(row, status["provider"], "Reuse", reason)
        if reason is None:
            generation["skipped"] += 1
            item_result.update(
                {
                    "provider": "library",
                    "action": "Reuse",
                    "status": "reused",
                    "reason": "same_dish_same_style",
                    "evidence": generation_evidence("library", "Reuse", "reused"),
                }
            )
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
            tencent_detail = metadata.get("tencent") if isinstance(metadata.get("tencent"), dict) else {}
            item_result.update(
                {
                    "provider": "tencent-hunyuan",
                    "action": cached_action,
                    "status": "cached",
                    "succeeded": True,
                    "cached": True,
                    "requestId": tencent_detail.get("requestId"),
                    "jobId": tencent_detail.get("jobId"),
                    "evidence": generation_evidence("tencent-hunyuan", cached_action, "cached", tencent_detail),
                }
            )
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
                    "evidence": generation_evidence("tencent-hunyuan", "Limited", "limited"),
                }
            )
            row["backgroundAction"] = "待正式生成"
            row["publicStatus"] = "待正式生成"
            row["generationStatus"] = "limited"
            row["generation"] = item_result
            bump_generation_action(generation, "Limited")
            generation["items"].append(item_result)
            continue

        if not status["configured"]:
            generation["pending"] += 1
            apply_queued_generation_row(row, item_result, reason)
            bump_generation_action(generation, "WaitingForProvider")
            generation["items"].append(item_result)
            continue

        used_tencent = False
        detail: dict[str, Any] | None = None
        if status["configured"]:
            generation["attempted"] += 1
            item_result["attempted"] = True
            try:
                if request_data.source_strategy == generation_engine.STRATEGY_REPLACE_BACKGROUND and source_candidate:
                    detail = tencent_replace_background(row, source_candidate, selected_style, target, quality)
                elif request_data.source_strategy == generation_engine.STRATEGY_REFERENCE_REDRAW and source_candidate:
                    detail = tencent_reference_redraw(row, source_candidate, selected_style, target, quality)
                elif request_data.source_strategy in {
                    generation_engine.STRATEGY_TEXT_TO_IMAGE3,
                    generation_engine.STRATEGY_TEXT_TO_IMAGE_LITE,
                }:
                    detail = tencent_text_to_image(row, selected_style, quality, target)
                else:
                    raise RuntimeError(f"Unsupported generation strategy: {request_data.source_strategy}")
                assert detail is not None
                ai_candidate, _ = ai_output_candidate(row, selected_style, quality, f"tencent-{detail['action']}")
                ai_candidate["tencent"] = detail
                metadata = {
                    "status": "succeeded",
                    "provider": "tencent-hunyuan",
                    "action": detail["action"],
                    "promptType": detail.get("promptType"),
                    "sourceStrategy": request_data.source_strategy,
                    "quality": request_data.quality,
                    "qualityPoints": generation_engine.quality_points(request_data.quality),
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
                item_result.update(
                    {
                        "provider": "tencent-hunyuan",
                        "action": detail["action"],
                        "status": "succeeded",
                        "succeeded": True,
                        "promptType": detail.get("promptType"),
                        "sourceStrategy": request_data.source_strategy,
                        "source_strategy": request_data.source_strategy,
                        "quality": request_data.quality,
                        "qualityPoints": generation_engine.quality_points(request_data.quality),
                        "quality_points": generation_engine.quality_points(request_data.quality),
                        "requestId": detail.get("requestId"),
                        "jobId": detail.get("jobId"),
                        "evidence": generation_evidence("tencent-hunyuan", str(detail["action"]), "succeeded", detail),
                    }
                )
                bump_generation_action(generation, detail["action"])
            except Exception as exc:
                error = str(exc)[:220]
                generation["errors"].append({"dish": row.get("name"), "message": error})
                failed_action = generation_action_for_strategy(request_data.source_strategy)
                item_result.update(
                    {
                        "error": error,
                        "provider_error": error,
                        "providerError": error,
                        "retryable": generation_engine.is_retryable_provider_error(error),
                        "evidence": generation_evidence("tencent-hunyuan", failed_action, "failed"),
                    }
                )
        if not used_tencent:
            if status["configured"] and not allow_local_image_fallback():
                generation["failed"] += 1
                generation["pending"] += 1
                failed_action = str((item_result.get("evidence") or {}).get("action") or "Failed")
                item_result.update({"provider": "tencent-hunyuan", "action": failed_action, "status": "failed"})
                row["backgroundAction"] = "模型生成失败"
                row["publicStatus"] = "模型生成失败"
                row["generationStatus"] = "failed"
                row["generation"] = item_result
                bump_generation_action(generation, "Failed")
                generation["items"].append(item_result)
                continue
            if not allow_local_image_fallback():
                generation["pending"] += 1
                apply_queued_generation_row(row, item_result, reason)
                bump_generation_action(generation, "WaitingForProvider")
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


def style_option_payload(
    style_id: str,
    label: str,
    raw_name: str,
    sample: dict[str, Any] | None,
    signature: str,
    count: int,
    direct: int,
    review: int,
    bg_replace: int,
    custom: int,
    total: int,
    estimated_points: int,
) -> dict[str, Any]:
    color = style_color_for(style_id)
    needs_generated = not style_sample_is_real(sample)
    image_url = str((sample or {}).get("url") or "")
    fill_manifest = style_background_manifest(style_id, label, sample)
    return {
        "id": style_id,
        "label": label,
        "name": label,
        "rawName": raw_name,
        "imageUrl": image_url,
        "source": style_card_source(sample),
        "dedupeSignature": signature,
        "dedupe_signature": signature,
        "needsGeneratedBackground": needs_generated,
        "needs_generated_background": needs_generated,
        "needsAiFill": needs_generated,
        "aiFillManifest": fill_manifest,
        "generationManifest": fill_manifest,
        "backgroundJob": style_background_job(sample),
        "count": count,
        "sample": sample,
        "color": f"rgb({color[0]},{color[1]},{color[2]})",
        "direct": direct,
        "review": review,
        "bgReplace": bg_replace,
        "custom": custom,
        "directRate": round(direct / total * 100, 1),
        "processingRate": round((review + bg_replace) / total * 100, 1),
        "customRate": round(custom / total * 100, 1),
        "estimatedPoints": estimated_points,
    }


def style_option_metrics(results: list[dict[str, Any]], style_id: str) -> dict[str, int]:
    direct = review = bg_replace = custom = count = 0
    for row in results:
        candidates = row.get("candidates") or []
        if not candidates:
            custom += 1
            continue
        same = next((c for c in candidates if c.get("styleId") == style_id), None)
        if same:
            count += 1
        if same and same.get("reusable", True) and row.get("status") == "直接可用":
            direct += 1
        elif same and float(same.get("score") or same.get("confidence") or 0) >= 70:
            review += 1
        elif same:
            custom += 1
        else:
            bg_replace += 1
    return {
        "count": count,
        "direct": direct,
        "review": review,
        "bgReplace": bg_replace,
        "custom": custom,
        "estimatedPoints": direct * 10 + review * 12 + bg_replace * 18 + custom * 49,
    }


def normalized_background_signatures(candidate: dict[str, Any] | None, fallback: str) -> tuple[str, set[str]]:
    signature = candidate_background_signature(candidate)
    signatures = candidate_background_signatures(candidate)
    if signature and not signatures:
        signatures = {f"bg:{signature}"}
    if not signature:
        signature = fallback
        signatures = signatures or {f"bg:{fallback}"}
    return signature, signatures


def style_options(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    style_ids = ordered_style_ids(results)
    options: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    used_style_ids: set[str] = set()
    total = max(1, len(results))
    generated_attempts = 0

    def add_option(style_id: str, sample: dict[str, Any] | None) -> bool:
        if not sample or style_id in used_style_ids or len(options) >= PREVIEW_SAMPLE_COUNT:
            return False
        signature, signatures = normalized_background_signatures(sample, style_id)
        if signatures & seen_signatures:
            return False
        display_index = len(options) + 1
        metrics = style_option_metrics(results, style_id)
        options.append(
            style_option_payload(
                style_id,
                BACKGROUND_LABELS[display_index - 1],
                style_name_for(style_id),
                sample,
                signature,
                metrics["count"],
                metrics["direct"],
                metrics["review"],
                metrics["bgReplace"],
                metrics["custom"],
                total,
                metrics["estimatedPoints"],
            )
        )
        used_style_ids.add(style_id)
        seen_signatures.update(signatures)
        return True

    # First pass: use truly available gallery backgrounds and skip duplicates.
    for style_id in style_ids:
        if len(options) >= PREVIEW_SAMPLE_COUNT:
            break
        add_option(style_id, style_representative_candidate(style_id))

    # Second pass: when the gallery lacks enough distinct backgrounds, ask Hunyuan to fill.
    for style_id in style_ids:
        if len(options) >= PREVIEW_SAMPLE_COUNT:
            break
        if style_id in used_style_ids:
            continue
        sample, generated_attempts = style_background_fill_candidate(
            style_id,
            generated_attempts,
            STYLE_BACKGROUND_GENERATION_ATTEMPT_LIMIT,
        )
        add_option(style_id, sample)

    fallback_index = 1
    max_fallbacks = max(PREVIEW_SAMPLE_COUNT * 4, STYLE_BACKGROUND_GENERATION_ATTEMPT_LIMIT)
    while len(options) < PREVIEW_SAMPLE_COUNT and fallback_index <= max_fallbacks:
        style_id = f"generated-style-fallback-{fallback_index}"
        fallback_index += 1
        if style_id in used_style_ids:
            continue
        sample, generated_attempts = style_background_fill_candidate(
            style_id,
            generated_attempts,
            STYLE_BACKGROUND_GENERATION_ATTEMPT_LIMIT,
        )
        add_option(style_id, sample)

    pending_index = 1
    while len(options) < PREVIEW_SAMPLE_COUNT:
        style_id = f"generated-style-needed-{pending_index}"
        pending_index += 1
        if style_id in used_style_ids:
            continue
        sample = style_background_placeholder_candidate(style_id, "pending", "GenerateStyleBackground", "图库背景不足，等待生成不同背景")
        add_option(style_id, sample)
    return options


def status_for(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "未找到"
    if candidates[0]["score"] >= 70:
        return "直接可用"
    return "需生成"


def machine_status_for_candidates(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "no_match"
    score = float(candidates[0].get("score") or candidates[0].get("confidence") or 0)
    if score >= 70:
        return "direct"
    return "needs_generation"


def candidate_state_fields(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    match_status = machine_status_for_candidates(candidates)
    needs_generation = match_status in {"no_match", "needs_generation"}
    if not candidates:
        return {
            "matchStatus": match_status,
            "needsAi": needs_generation,
            "needs_generation": needs_generation,
            "needsGeneration": needs_generation,
            "confidence": 0.0,
            "match_reason": MATCH_REASON_UNMATCHED,
            "matchReason": MATCH_REASON_UNMATCHED,
            "candidate_id": "",
            "candidateId": "",
        }
    top = candidates[0]
    reason = str(top.get("match_reason") or top.get("matchReason") or ("generated" if top.get("generated") else ""))
    candidate_id = str(top.get("candidate_id") or top.get("candidateId") or top.get("imageId") or "")
    confidence = float(top.get("confidence", top.get("score", 0)) or 0)
    return {
        "matchStatus": match_status,
        "needsAi": needs_generation,
        "needs_generation": needs_generation,
        "needsGeneration": needs_generation,
        "confidence": confidence,
        "match_reason": reason,
        "matchReason": reason,
        "candidate_id": candidate_id,
        "candidateId": candidate_id,
    }


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


def generation_job_json_error(exc: generation_jobs.GenerationJobError):
    return jsonify(exc.to_dict()), exc.status_code


def job_poll_response(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "job": job,
        "poll": {"url": f"/api/jobs/{job['id']}", "intervalMs": 1500},
        "idempotent": bool(job.get("idempotent")),
    }


def job_plan_snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    keys = ("menu", "category", "standardization", "summary", "selectedStyle", "quality", "quote", "pricing", "pipeline")
    return {key: plan[key] for key in keys if key in plan}


def selected_job_rows(plan: dict[str, Any], selected_rows: Any = None) -> list[dict[str, Any]]:
    rows = list(plan.get("results") or [])
    if not isinstance(selected_rows, list) or not selected_rows:
        return rows
    selected: set[int] = set()
    for value in selected_rows:
        try:
            selected.add(int(value))
        except Exception:
            continue
    if not selected:
        return rows
    return [row for index, row in enumerate(rows, start=1) if index in selected or int(row.get("row") or 0) in selected]


def direct_job_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw_items = payload.get("items")
    if raw_items is None:
        return None
    if not isinstance(raw_items, list):
        raise generation_jobs.InvalidJobInput("items must be a list", field="items")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise generation_jobs.InvalidJobInput("job item must be an object", field=f"items[{index}]")
        name = str(item.get("name") or item.get("dish") or item.get("dishName") or "").strip()
        if not name:
            raise generation_jobs.InvalidJobInput("job item name is required", field=f"items[{index}].name")
        row = dict(item)
        row.setdefault("row", index)
        row.setdefault("category", "smoke")
        row.setdefault("kind", "单品")
        row.setdefault("components", [])
        row.setdefault("candidates", [])
        row.setdefault("backgroundAction", "智能补图")
        row.setdefault("publicStatus", "待正式生成")
        row.setdefault("points", 0)
        row["name"] = name
        rows.append(row)
    if not rows:
        raise generation_jobs.InvalidJobInput("items must not be empty", field="items")
    return rows


def job_points_for_rows(rows: list[dict[str, Any]], fallback: Any = 0) -> int:
    try:
        explicit = int(fallback)
    except Exception:
        explicit = 0
    if explicit > 0:
        return explicit
    return sum(int(row.get("points") or 0) for row in rows)


def job_run_limit(value: Any = None) -> int:
    if value in (None, ""):
        return max(1, GENERATION_JOB_SYNC_BATCH_SIZE)
    try:
        return max(1, min(100, int(value)))
    except Exception:
        return max(1, GENERATION_JOB_SYNC_BATCH_SIZE)


def job_limit_from_payload(payload: dict[str, Any], *, async_mode: bool) -> int | None:
    if payload.get("limit") in (None, ""):
        return None if async_mode else job_run_limit(None)
    return job_run_limit(payload.get("limit"))


def job_sync_requested(payload: dict[str, Any]) -> bool:
    if "sync" in payload:
        return bool_value(payload.get("sync"), default=False)
    return bool_value(request.args.get("sync"), default=False)


def job_wait_ms_from_payload(payload: dict[str, Any]) -> int | None:
    value = payload.get("waitMs")
    if value in (None, ""):
        return None
    try:
        return max(0, min(5000, int(value)))
    except Exception:
        return None


def app_quality_id(quality: str | None) -> str:
    normalized = generation_engine.normalize_quality(quality)
    return "premium" if normalized == generation_engine.QUALITY_PREMIUM else "standard"


def record_formal_model_output(
    row: dict[str, Any],
    *,
    style: str,
    quality: str | None,
    request_data: generation_engine.GenerationRequest,
    detail: dict[str, Any],
    target: Path,
    source_candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    ai_candidate, _ = ai_output_candidate(row, style, quality, f"tencent-{detail['action']}")
    ai_candidate["tencent"] = detail
    metadata = {
        "status": "succeeded",
        "provider": "tencent-hunyuan",
        "action": detail["action"],
        "promptType": detail.get("promptType"),
        "sourceStrategy": request_data.source_strategy,
        "kind": request_data.kind,
        "quality": request_data.quality,
        "platforms": list(request_data.platforms),
        "watermark": request_data.watermark,
        "row": row.get("row"),
        "dish": row.get("name"),
        "sourceCandidate": {
            "imageId": source_candidate.get("imageId"),
            "dishName": source_candidate.get("dishName"),
            "styleId": source_candidate.get("styleId"),
            "source": source_candidate.get("source"),
            "reusable": source_candidate.get("reusable"),
        }
        if source_candidate
        else None,
        "tencent": detail,
    }
    write_ai_output_metadata(target, metadata)
    candidate_generation_metadata(ai_candidate, metadata)
    promote_candidate(row, ai_candidate)
    row["backgroundAction"] = "正式生成"
    row["publicStatus"] = "已生成"
    row["generationStatus"] = "succeeded"
    return ai_candidate


class AppGenerationProvider:
    def __init__(self, style: str, quality: str | None) -> None:
        self.style = style
        self.quality = app_quality_id(quality)
        self.configured = bool(tencent_status_payload().get("configured"))
        self.allow_lite_fallback = TENCENT_IMAGE3_FALLBACK_TO_LITE

    def reuse(self, request_data: generation_engine.GenerationRequest) -> dict[str, Any]:
        candidate = request_data.source_candidate or {}
        if candidate:
            promote_candidate(request_data.row, candidate)
        provider = str(candidate.get("aiProvider") or candidate.get("generationProvider") or "library")
        action = str(candidate.get("generationAction") or ("Cached" if provider == "tencent-hunyuan" else "Reuse"))
        tencent = candidate.get("tencent") if isinstance(candidate.get("tencent"), dict) else {}
        request_data.row["backgroundAction"] = "正式生成" if provider == "tencent-hunyuan" else "背景一致，直接复用"
        request_data.row["publicStatus"] = "已生成"
        request_data.row["generationStatus"] = "cached" if provider == "tencent-hunyuan" else "reused"
        return {
            "provider": provider,
            "action": action,
            "promptType": "reuse",
            "qualityPoints": generation_engine.quality_points(request_data.quality),
            "quality_points": generation_engine.quality_points(request_data.quality),
            "candidate": candidate,
            "path": candidate.get("path") or "",
            "reason": "cached_model_output" if provider == "tencent-hunyuan" else "same_dish_same_style",
            "requestId": tencent.get("requestId"),
            "jobId": tencent.get("jobId"),
            "endpoint": tencent.get("endpoint"),
        }

    def replace_background(self, request_data: generation_engine.GenerationRequest) -> dict[str, Any]:
        source_candidate = request_data.source_candidate or {}
        target = ai_output_candidate(request_data.row, self.style, self.quality, "generated-final")[1]
        detail = tencent_replace_background(request_data.row, source_candidate, self.style, target, self.quality)
        candidate = record_formal_model_output(
            request_data.row,
            style=self.style,
            quality=self.quality,
            request_data=request_data,
            detail=detail,
            target=target,
            source_candidate=source_candidate,
        )
        return {**detail, "candidate": candidate, "path": str(target)}

    def reference_redraw(self, request_data: generation_engine.GenerationRequest) -> dict[str, Any]:
        source_candidate = request_data.source_candidate or {}
        target = ai_output_candidate(request_data.row, self.style, self.quality, "generated-final")[1]
        detail = tencent_reference_redraw(request_data.row, source_candidate, self.style, target, self.quality)
        candidate = record_formal_model_output(
            request_data.row,
            style=self.style,
            quality=self.quality,
            request_data=request_data,
            detail=detail,
            target=target,
            source_candidate=source_candidate,
        )
        return {**detail, "candidate": candidate, "path": str(target)}

    def text_to_image(self, request_data: generation_engine.GenerationRequest) -> dict[str, Any]:
        target = ai_output_candidate(request_data.row, self.style, self.quality, "generated-final")[1]
        detail = tencent_text_to_image(request_data.row, self.style, self.quality, target)
        candidate = record_formal_model_output(
            request_data.row,
            style=self.style,
            quality=self.quality,
            request_data=request_data,
            detail=detail,
            target=target,
            source_candidate=None,
        )
        return {**detail, "candidate": candidate, "path": str(target)}


def run_formal_generation_item(
    row: dict[str, Any],
    *,
    style: str,
    quality: str | None = "standard",
    platforms: list[str] | tuple[str, ...] | None = None,
    watermark: bool | dict[str, Any] = False,
) -> dict[str, Any]:
    strip_nonfinal_generated_candidates(row)
    strip_mismatched_source_candidates(row)
    if isinstance(watermark, dict):
        row["watermark"] = watermark
    if platforms:
        row["_delivery_platforms"] = [str(platform) for platform in platforms if str(platform or "").strip()]
    request_data = generation_engine.request_from_row(row, style=style, quality=quality, platforms=platforms, watermark=watermark)
    request_data = generation_engine.select_generation_request(request_data)
    provider = AppGenerationProvider(style, quality)
    result = generation_engine.execute_generation_request(request_data, provider)
    result_payload = result.to_dict()
    if result.status == generation_engine.STATUS_FAILED:
        row["backgroundAction"] = "模型生成失败"
        row["publicStatus"] = "模型生成失败"
        row["generationStatus"] = "failed"
    elif result.status == generation_engine.STATUS_QUEUED:
        row["backgroundAction"] = "待正式生成"
        row["publicStatus"] = "待正式生成"
        row["generationStatus"] = "queued"
    row["generation"] = {
        "row": row.get("row"),
        "dish": row.get("name"),
        "provider": result.provider,
        "action": result.action,
        "reason": result.reason,
        "attempted": result.source_strategy != generation_engine.STRATEGY_REUSE,
        "succeeded": result.status in {generation_engine.STATUS_SUCCEEDED, generation_engine.STATUS_REUSED},
        "status": result.status,
        "providerStatus": generation_engine.provider_status(result.status),
        "provider_status": generation_engine.provider_status(result.status),
        "promptType": result.prompt_type,
        "quality": result.quality,
        "qualityPoints": result.quality_points,
        "quality_points": result.quality_points,
        "sourceStrategy": result.source_strategy,
        "source_strategy": result.source_strategy,
        "providerError": result.provider_error,
        "provider_error": result.provider_error,
        "retryable": result.retryable,
        "refundRequired": result.refund_required,
        "refund_required": result.refund_required,
        "evidence": result_payload.get("evidence"),
    }
    for key in ("requestId", "jobId", "submitRequestId", "queryRequestId", "endpoint"):
        value = result_payload.get(key) or result.metadata.get(key)
        if value:
            row["generation"][key] = value
    if result.candidate:
        row["generation"]["candidate"] = result.candidate
    return {"result": result_payload, "row": row}


def generation_job_item_runner(style: str, quality: str, platforms: list[str] | tuple[str, ...] | None = None, watermark: bool | dict[str, Any] = False):
    def run_item(item: dict[str, Any]) -> dict[str, Any]:
        row = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if not row:
            raise generation_jobs.InvalidJobInput("Job item payload is empty", itemIndex=item.get("index"))
        item_output = run_formal_generation_item(row, style=style, quality=quality, platforms=platforms, watermark=watermark)
        updated_row = item_output["row"]
        item_result = item_output["result"]
        result_status = str(item_result.get("status") or "")
        if result_status in {"failed"}:
            item_status = generation_jobs.ITEM_FAILED
        elif result_status in {"pending", "limited", "queued"}:
            item_status = generation_jobs.ITEM_QUEUED
        else:
            item_status = generation_jobs.ITEM_COMPLETED
        return {
            "itemStatus": item_status,
            "status": result_status,
            "providerStatus": item_result.get("providerStatus") or item_result.get("provider_status") or generation_engine.provider_status(result_status),
            "provider_status": item_result.get("provider_status") or item_result.get("providerStatus") or generation_engine.provider_status(result_status),
            "provider": item_result.get("provider"),
            "action": item_result.get("action"),
            "reason": item_result.get("reason"),
            "requestId": item_result.get("requestId"),
            "jobId": item_result.get("jobId"),
            "submitRequestId": item_result.get("submitRequestId"),
            "queryRequestId": item_result.get("queryRequestId"),
            "endpoint": item_result.get("endpoint"),
            "error": item_result.get("error") or item_result.get("provider_error"),
            "provider_error": item_result.get("provider_error") or item_result.get("providerError"),
            "providerError": item_result.get("provider_error") or item_result.get("providerError"),
            "retryable": bool(item_result.get("retryable")),
            "refund_required": bool(item_result.get("refund_required") or item_result.get("refundRequired")),
            "refundRequired": bool(item_result.get("refund_required") or item_result.get("refundRequired")),
            "generation": updated_row.get("generation") or item_result,
            "generationResult": item_result,
            "evidence": item_result.get("evidence"),
            "payload": updated_row,
        }

    return run_item


def generation_job_runner_for(job: dict[str, Any]):
    job_request = job.get("request") if isinstance(job.get("request"), dict) else {}
    return generation_job_item_runner(
        str(job["style"]),
        str(job["quality"]),
        platforms=job_request.get("platforms") if isinstance(job_request.get("platforms"), list) else None,
        watermark=job_request.get("watermark") if isinstance(job_request.get("watermark"), dict) else False,
    )


def execute_generation_job(job_id: str, *, limit: int | None = None) -> dict[str, Any]:
    job = generation_jobs.get_job(job_id)
    return generation_jobs.run_job(job_id, generation_job_runner_for(job), limit=limit)


def generation_job_thread_active(job_id: str) -> bool:
    with _ACTIVE_GENERATION_JOB_LOCK:
        thread = _ACTIVE_GENERATION_JOB_THREADS.get(job_id)
        if thread and thread.is_alive():
            return True
        if thread:
            _ACTIVE_GENERATION_JOB_THREADS.pop(job_id, None)
    return False


def generation_job_background_target(job_id: str, limit: int | None) -> None:
    try:
        execute_generation_job(job_id, limit=limit)
    except Exception as exc:
        message = str(exc)[:1000] or type(exc).__name__
        app.logger.exception("generation job failed in background", extra={"job_id": job_id})
        try:
            generation_jobs.set_job_stage(
                job_id,
                generation_jobs.JOB_STAGE_FORMAL_GENERATION,
                generation_jobs.STAGE_FAILED,
                detail="正式生图后台任务失败",
                error=message,
            )
            generation_jobs.mark_failed(job_id, message)
        except Exception:
            app.logger.exception("failed to mark generation job failed", extra={"job_id": job_id})
    finally:
        with _ACTIVE_GENERATION_JOB_LOCK:
            thread = _ACTIVE_GENERATION_JOB_THREADS.get(job_id)
            if thread is threading.current_thread():
                _ACTIVE_GENERATION_JOB_THREADS.pop(job_id, None)


def start_generation_job_async(job_id: str, *, limit: int | None = None) -> tuple[dict[str, Any], bool, bool]:
    with _ACTIVE_GENERATION_JOB_LOCK:
        existing = _ACTIVE_GENERATION_JOB_THREADS.get(job_id)
        if existing and existing.is_alive():
            return generation_jobs.get_job(job_id), False, True
        if existing:
            _ACTIVE_GENERATION_JOB_THREADS.pop(job_id, None)

        job = generation_jobs.get_job(job_id)
        if str(job.get("status") or "").lower() == generation_jobs.JOB_RUNNING:
            return job, False, True
        if str(job.get("status") or "").lower() in generation_jobs.JOB_TERMINAL_STATUSES and int(job.get("pendingItems") or 0) <= 0:
            return job, False, False

        job = generation_jobs.set_job_stage(
            job_id,
            generation_jobs.JOB_STAGE_FORMAL_GENERATION,
            generation_jobs.STAGE_QUEUED,
            detail="正式生图任务已进入后台队列",
        )
        thread = threading.Thread(
            target=generation_job_background_target,
            args=(job_id, limit),
            name=f"generation-job-{job_id[:12]}",
            daemon=True,
        )
        _ACTIVE_GENERATION_JOB_THREADS[job_id] = thread
        thread.start()
        return job, True, False


def async_job_response(job_id: str, *, limit: int | None = None, wait_ms: int | None = None) -> dict[str, Any]:
    job, started, already_running = start_generation_job_async(job_id, limit=limit)
    grace_ms = GENERATION_JOB_ASYNC_RETURN_GRACE_MS if wait_ms is None else max(0, wait_ms)
    if started and grace_ms > 0:
        with _ACTIVE_GENERATION_JOB_LOCK:
            thread = _ACTIVE_GENERATION_JOB_THREADS.get(job_id)
        if thread:
            thread.join(grace_ms / 1000)
        job = generation_jobs.get_job(job_id)
    response = job_poll_response(job)
    response.update({"mode": "async", "started": started, "alreadyRunning": already_running})
    return response


def pipeline_payload() -> dict[str, Any]:
    tencent = tencent_status_payload()
    return {
        "provider": tencent["provider"],
        "imageEditApiReady": tencent["configured"],
        "objectStorageReady": bool(tencent.get("cosReady") or os.environ.get("OBJECT_STORAGE_BUCKET")),
        "expectedEnv": [
            "TENCENT_HUNYUAN_ENABLED",
            "TENCENTCLOUD_SECRET_ID",
            "TENCENTCLOUD_SECRET_KEY",
            "TENCENTCLOUD_REGION",
            "PUBLIC_BASE_URL",
            "TENCENT_COS_BUCKET",
            "TENCENT_COS_REGION",
        ],
        "tencent": tencent,
        "stages": ["菜单解析", "风格确认", "图库匹配", "统一背景", "预览导出"],
    }


def preview_sample_rank(item: dict[str, Any], candidates: list[dict[str, Any]]) -> int:
    name = str(item.get("name") or "")
    norm = str(item.get("norm") or normalize(name))
    score = 0
    if item.get("kind") == "单品":
        score += 20
    if semantic_family(name, norm) == "food":
        score += 24
    else:
        score -= 60
    if detect_kind(name, "") != "单品":
        score -= 24
    if has_sample_bad_word(name, PREVIEW_SAMPLE_BAD_WORDS):
        score -= 110
    if has_sample_bad_word(name, STYLE_SAMPLE_SIDE_WORDS):
        score -= 36
    for index, word in enumerate(STYLE_SAMPLE_PREFERRED_WORDS):
        if word in name:
            score += max(8, 34 - index * 2)
    if any(word in name for word in ("螺蛳粉", "米粉", "米线", "盖码饭", "盖浇饭", "炒菜", "小炒", "套餐")):
        score += 12
    if 4 <= len(norm) <= 20:
        score += 8
    if candidates:
        top = candidates[0]
        score += min(32, int(float(top.get("score") or 0) // 3))
        if top.get("reusable", True):
            score += 10
    else:
        score -= 18
    return score


def preview_sample_item_allowed(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "")
    norm = str(item.get("norm") or normalize(name))
    if item.get("kind") != "单品":
        return False
    if detect_kind(name, "") != "单品":
        return False
    if semantic_family(name, norm) != "food":
        return False
    if has_sample_bad_word(name, PREVIEW_SAMPLE_BAD_WORDS):
        return False
    if has_sample_bad_word(name, STYLE_SAMPLE_SIDE_WORDS):
        return False
    return bool(norm)


def preview_sample_entries() -> list[dict[str, Any]]:
    menu = parse_menu()
    library = library_images()
    scored_entries: list[tuple[int, int, dict[str, Any], list[dict[str, Any]]]] = []
    seen_norms: set[str] = set()
    for order, raw_item in enumerate(menu["items"]):
        item = dict(raw_item)
        if not item.get("norm"):
            item["norm"] = normalize(str(item.get("name") or ""))
        if not preview_sample_item_allowed(item):
            continue
        norm = str(item.get("norm") or "")
        if not norm or norm in seen_norms:
            continue
        seen_norms.add(norm)
        candidates = top_candidates(item, library)
        scored_entries.append((preview_sample_rank(item, candidates), -order, item, candidates))
    scored_entries.sort(reverse=True)
    entries = [{"item": item, "candidates": candidates} for score, _, item, candidates in scored_entries[:PREVIEW_SAMPLE_COUNT]]
    if len(entries) < PREVIEW_SAMPLE_COUNT:
        existing_norms = {entry["item"].get("norm") for entry in entries}
        for item in demo_menu_items():
            if not preview_sample_item_allowed(item) or item.get("norm") in existing_norms:
                continue
            demo_item = {**item, "category": "风格样图"}
            existing_norms.add(demo_item.get("norm"))
            candidates = top_candidates(demo_item, library)
            entries.append({"item": demo_item, "candidates": candidates})
            if len(entries) >= PREVIEW_SAMPLE_COUNT:
                break
    return entries


def preview_sample_job_payload(generation: dict[str, Any]) -> dict[str, Any]:
    job = {
        "status": str(generation.get("status") or "pending"),
        "provider": str(generation.get("provider") or "tencent-hunyuan"),
        "action": str(generation.get("action") or "Preview"),
    }
    for key in (
        "error",
        "provider_error",
        "providerError",
        "reason",
        "fallbackFrom",
        "fallbackMessage",
        "promptType",
        "requestId",
        "jobId",
        "evidence",
    ):
        value = generation.get(key)
        if value:
            job[key] = value
    return job


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
            else queued_generation_payload() if not tencent_ready() else {"status": "pending", "provider": "tencent-hunyuan", "action": "Preview"}
        )
    public_status = "免费样图"
    if generation.get("status") == "failed":
        public_status = "样图生成失败"
    elif generation.get("status") in {"pending", "limited", "queued"}:
        public_status = "等待生成"
    job = preview_sample_job_payload(generation)
    return {
        **item,
        "styleId": selected_style,
        "styleName": style_name_for(selected_style),
        "candidate": candidate,
        "sourceCandidates": candidates[:3],
        "generation": generation,
        "job": job,
        "sampleJob": job,
        "status": job["status"],
        "error": job.get("error") or job.get("provider_error") or "",
        "points": 0,
        "publicStatus": public_status,
    }


def preview_sample_payload(selected_style: str, index: int, generate: bool = True) -> dict[str, Any]:
    entries = preview_sample_entries()
    if index < 0 or index >= len(entries):
        raise IndexError("样图序号不存在")
    return preview_sample_payload_from_entry(selected_style, entries[index], generate=generate)


def preview_samples(selected_style: str, generate: bool = True) -> dict[str, Any]:
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
        results.append({**item, "status": original_status, "originalStatus": original_status, "candidates": candidates, "componentMatches": components, **candidate_state_fields(candidates)})
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
        chosen_score = float((chosen or {}).get("score") or (chosen or {}).get("confidence") or 0)
        if not chosen:
            action = "需要定制/生成"
        elif chosen_score < 70:
            action = "智能补图"
        elif not chosen.get("reusable", True):
            action = "需去水印/重绘"
        elif row["kind"] == "套餐/组合" and detect_kind(str(chosen.get("dishName") or ""), "") != "套餐/组合":
            action = "套餐组合生成"
        elif chosen.get("generated") and row["originalStatus"] == "未找到":
            action = "智能补图"
        elif chosen.get("generated"):
            action = "智能统一风格"
        else:
            action = "智能统一风格" if chosen["styleId"] == selected_style else "需抠图换背景"
        row["backgroundAction"] = action
        row.update(candidate_state_fields(candidates))
        if not chosen:
            public_status = "待补图"
        elif not chosen.get("reusable", True):
            public_status = "待处理"
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
        "styleFillManifests": [style["aiFillManifest"] for style in styles if style.get("aiFillManifest")],
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
    remote_images = 0
    for image in library:
        sources[image.source] = sources.get(image.source, 0) + 1
        if image.reusable:
            reusable += 1
        if image.remote_url:
            remote_images += 1
    index_status = library_index_status_snapshot()
    return jsonify(
        {
            "total": len(library),
            "reusable": reusable,
            "sources": sources,
            "stores": len({image.store for image in library}),
            "styles": len({image.style_id for image in library}),
            "externalDirs": [str(path) for path in configured_library_dirs()],
            "remoteIndex": bool(index_status.get("remoteIndex")),
            "remoteImages": remote_images,
            "indexImages": int(index_status.get("indexImages") or 0),
            "indexSource": str(index_status.get("indexSource") or ""),
            "indexError": str(index_status.get("indexError") or ""),
        }
    )


@app.get("/api/tencent-status")
def api_tencent_status():
    return jsonify(tencent_status_payload())


@app.get("/api/admin/gallery-upload/status")
def api_gallery_upload_status():
    cos = tencent_cos_config()
    prefix = gallery_cos_prefix()
    index_url = public_cos_url(cos.get("bucket", ""), cos.get("region", ""), gallery_index_key(prefix)) if cos.get("bucket") else ""
    configured_index_url = configured_library_index_url()
    upload_enabled = bool(gallery_upload_token())
    return jsonify(
        {
            "enabled": upload_enabled,
            "disabledReason": "" if upload_enabled else "未配置 GALLERY_UPLOAD_TOKEN，图库远程上传接口已关闭",
            "cosReady": bool(cos.get("ready")),
            "bucket": cos.get("bucket") if cos.get("ready") else "",
            "region": cos.get("region"),
            "prefix": prefix,
            "indexKey": gallery_index_key(prefix),
            "indexUrl": index_url,
            "configuredIndexUrl": configured_index_url,
            "runtimeIndexActive": bool(configured_index_url and configured_index_url == index_url),
            "renderEnv": {"COS_LIBRARY_INDEX_URL": index_url} if index_url else {},
        }
    )


@app.post("/api/admin/gallery-upload/batch")
def api_gallery_upload_batch():
    payload = request.get_json(silent=True) or {}
    auth_error = gallery_upload_auth_error(payload)
    if auth_error:
        return jsonify({"error": auth_error, "code": "gallery_upload_auth_failed"}), 403
    try:
        cos = ensure_gallery_cos_ready()
        client = create_cos_client_from_config(cos)
    except Exception as exc:
        return jsonify({"error": str(exc), "code": "gallery_cos_not_ready"}), 503

    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return jsonify({"error": "records 不能为空", "code": "empty_gallery_batch"}), 400
    session = gallery_upload_session_id(str(payload.get("session") or "default"))
    session_path = gallery_upload_session_path(session)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    uploaded: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    prefix = str(cos["prefix"]).strip().strip("/")
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            errors.append({"index": str(index), "error": "record item must be an object"})
            continue
        record = item.get("record") if isinstance(item.get("record"), dict) else {}
        image_b64 = str(item.get("image") or "")
        key = str(record.get("cos_key") or record.get("object_key") or "").strip().lstrip("/")
        if not key or not key.startswith(f"{prefix}/"):
            errors.append({"index": str(index), "error": f"invalid cos_key: {key[:80]}"})
            continue
        try:
            image_bytes = base64.b64decode(image_b64, validate=True)
            if not image_bytes:
                raise ValueError("empty image")
            content_type = str(item.get("contentType") or "image/jpeg")
            client.put_object(
                Bucket=cos["bucket"],
                Body=io.BytesIO(image_bytes),
                Key=key,
                ContentType=content_type,
                ACL="public-read",
            )
            remote_url = public_cos_url(str(cos["bucket"]), str(cos["region"]), key)
            record.update(
                {
                    "cos_bucket": cos["bucket"],
                    "cos_region": cos["region"],
                    "cos_key": key,
                    "object_key": key,
                    "url": remote_url,
                    "public_url": remote_url,
                    "remote_url": remote_url,
                    "upload_state": "uploaded",
                    "uploaded": True,
                }
            )
            uploaded.append(record)
        except Exception as exc:
            errors.append({"index": str(index), "cos_key": key, "error": str(exc)[:300]})
    if uploaded:
        with session_path.open("a", encoding="utf-8") as handle:
            for record in uploaded:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return jsonify(
        {
            "ok": not errors,
            "session": session,
            "uploaded": len(uploaded),
            "errors": errors,
            "sessionRecords": sum(1 for _ in session_path.open("r", encoding="utf-8")) if session_path.exists() else 0,
        }
    ), 207 if errors else 200


@app.post("/api/admin/gallery-upload/publish")
def api_gallery_upload_publish():
    payload = request.get_json(silent=True) or {}
    auth_error = gallery_upload_auth_error(payload)
    if auth_error:
        return jsonify({"error": auth_error, "code": "gallery_upload_auth_failed"}), 403
    try:
        cos = ensure_gallery_cos_ready()
        client = create_cos_client_from_config(cos)
    except Exception as exc:
        return jsonify({"error": str(exc), "code": "gallery_cos_not_ready"}), 503
    session = gallery_upload_session_id(str(payload.get("session") or "default"))
    session_path = gallery_upload_session_path(session)
    if not session_path.exists():
        return jsonify({"error": "上传 session 不存在，请先上传 batch", "code": "gallery_session_missing"}), 404
    data = session_path.read_bytes()
    if not data.strip():
        return jsonify({"error": "上传 session 没有索引记录", "code": "gallery_session_empty"}), 400
    key = gallery_index_key(str(cos["prefix"]))
    client.put_object(
        Bucket=cos["bucket"],
        Body=io.BytesIO(data),
        Key=key,
        ContentType="application/x-ndjson; charset=utf-8",
        ACL="public-read",
    )
    index_url = public_cos_url(str(cos["bucket"]), str(cos["region"]), key)
    os.environ["COS_LIBRARY_INDEX_URL"] = index_url
    library_images.cache_clear()
    return jsonify(
        {
            "ok": True,
            "session": session,
            "records": len([line for line in data.decode("utf-8", errors="ignore").splitlines() if line.strip()]),
            "indexKey": key,
            "indexUrl": index_url,
            "activatedIndexUrl": index_url,
            "runtimeIndexActive": True,
            "renderEnv": {"COS_LIBRARY_INDEX_URL": index_url},
        }
    )


@app.post("/api/jobs")
def api_create_generation_job():
    payload = request.get_json(silent=True) or {}
    style = str(payload.get("style") or "")
    quality = str(payload.get("quality") or "standard")
    if not style:
        return jsonify({"error": "请先选择风格"}), 400
    try:
        direct_rows = direct_job_rows(payload)
        if direct_rows is None:
            plan = build_plan(style, quality)
            quality = str(plan.get("quality", {}).get("id") or quality)
            rows = selected_job_rows(plan, payload.get("selectedRows"))
            plan_snapshot = job_plan_snapshot(plan)
        else:
            quality = app_quality_id(quality)
            rows = direct_rows
            plan_snapshot = {
                "selectedStyle": style,
                "quality": {"id": quality},
                "summary": {"total": len(rows), "points": job_points_for_rows(rows, payload.get("points"))},
                "pipeline": pipeline_payload(),
                "source": "direct_items",
            }
        if not rows:
            return jsonify({"error": "没有可生成的菜品"}), 400
        order_id = str(payload.get("orderId") or "").strip() or None
        job = generation_jobs.create_job(
            user_id=str(payload.get("userId") or current_user_id()),
            style=style,
            quality=quality,
            items=rows,
            request_payload={
                "style": style,
                "quality": quality,
                "selectedRows": payload.get("selectedRows") if isinstance(payload.get("selectedRows"), list) else None,
                "watermark": payload.get("watermark") if isinstance(payload.get("watermark"), dict) else None,
                "platforms": payload.get("platforms") if isinstance(payload.get("platforms"), list) else None,
                "source": "direct_items" if direct_rows is not None else "plan",
            },
            plan_snapshot=plan_snapshot,
            points=job_points_for_rows(rows, payload.get("points")),
            order_id=order_id,
            mark_paid=bool(payload.get("paid") or order_id),
        )
        return jsonify(job_poll_response(job)), 201
    except generation_jobs.GenerationJobError as exc:
        return generation_job_json_error(exc)


@app.get("/api/jobs/<job_id>")
def api_get_generation_job(job_id: str):
    try:
        job = generation_jobs.get_job(job_id)
        return jsonify(job_poll_response(job))
    except generation_jobs.GenerationJobError as exc:
        return generation_job_json_error(exc)


@app.post("/api/jobs/<job_id>/run")
def api_run_generation_job(job_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        job = generation_jobs.get_job(job_id)
        order_id = str(payload.get("orderId") or "").strip()
        if payload.get("paid") or order_id:
            job = generation_jobs.mark_paid(job_id, order_id=order_id or None)
        sync = job_sync_requested(payload)
        limit = job_limit_from_payload(payload, async_mode=not sync)
        if sync:
            job = generation_jobs.run_job(job_id, generation_job_runner_for(job), limit=limit)
            response = job_poll_response(job)
            response.update({"mode": "sync", "started": True, "alreadyRunning": False})
            return jsonify(response)
        return jsonify(async_job_response(job_id, limit=limit, wait_ms=job_wait_ms_from_payload(payload)))
    except generation_jobs.GenerationJobError as exc:
        return generation_job_json_error(exc)


@app.post("/api/jobs/<job_id>/retry")
def api_retry_generation_job(job_id: str):
    payload = request.get_json(silent=True) or {}
    raw_indexes = payload.get("itemIndexes") or payload.get("items")
    item_indexes = raw_indexes if isinstance(raw_indexes, list) else None
    try:
        job = generation_jobs.retry_failed_items(job_id, item_indexes=item_indexes)
        if bool_value(payload.get("run"), default=True):
            sync = job_sync_requested(payload)
            limit = job_limit_from_payload(payload, async_mode=not sync)
            if sync:
                job = generation_jobs.run_job(job_id, generation_job_runner_for(job), limit=limit)
                response = job_poll_response(job)
                response.update({"mode": "sync", "started": True, "alreadyRunning": False})
                return jsonify(response)
            return jsonify(async_job_response(job_id, limit=limit, wait_ms=job_wait_ms_from_payload(payload)))
        response = job_poll_response(job)
        response.update({"mode": "queued", "started": False, "alreadyRunning": generation_job_thread_active(job_id)})
        return jsonify(response)
    except generation_jobs.GenerationJobError as exc:
        return generation_job_json_error(exc)


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
    selected_rows_payload = payload.get("selectedRows") or []
    if not isinstance(selected_rows_payload, list):
        selected_rows_payload = []
    selected_rows = [int(x) for x in selected_rows_payload if str(x).isdigit()]
    selected_ids = payload.get("selectedIds") or []
    if not isinstance(selected_ids, list):
        selected_ids = []
    selected_ids = [*selected_ids, *[x for x in selected_rows_payload if not str(x).isdigit()]]
    watermark = payload.get("watermark") if isinstance(payload.get("watermark"), dict) else None
    try:
        platforms = require_platforms(payload.get("platforms"))
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "platform_required"}), 400
    quality = str(payload.get("quality", "standard"))
    style = str(payload.get("style", ""))
    image_format = str(payload.get("format") or payload.get("imageFormat") or "jpg")
    if not style:
        return jsonify({"error": "请先选择风格并生成正式图片"}), 400
    try:
        plan = build_plan(style, quality)
        export_results = prepare_results_for_export(plan["results"], style)
        result = export_delivery_zip(
            export_results,
            EXPORT_DIR,
            scope=str(payload.get("scope", "all")),
            selected_rows=selected_rows,
            selected_ids=[str(item) for item in selected_ids],
            image_format=image_format,
            watermark=watermark,
            platforms=platforms,
        )
    except Exception:
        app.logger.exception("export delivery zip failed")
        return jsonify({"error": "导出失败：服务器生成 ZIP 时遇到问题，请检查图片是否存在后重试", "code": "export_failed"}), 500
    if int(result.get("images") or 0) <= 0:
        return jsonify({"error": "没有可导出的成图，请先完成正式生图，或只勾选已生成的图片", "export": result}), 400
    return jsonify(result)


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
    try:
        export_root = EXPORT_DIR.resolve()
        target = (EXPORT_DIR / name).resolve()
        target.relative_to(export_root)
    except (OSError, ValueError):
        return jsonify({"error": "下载文件不存在或已失效", "code": "download_not_found"}), 404
    if not target.is_file():
        return jsonify({"error": "下载文件不存在或已失效", "code": "download_not_found"}), 404
    return send_file(target, as_attachment=True, download_name=target.name)


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
