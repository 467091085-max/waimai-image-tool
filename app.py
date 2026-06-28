from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
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

import admin_actions
import ai_asset_repository
import auth_rules
import auth_service
import billing
import asset_security
import commission_settlement_service
import download_guard
import growth_service
import object_storage_service
import pandas as pd
import payment_service
import sms_service
import storage_db
import withdrawal_service
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from admin_panel import AdminDependencies, create_admin_blueprint
from generation_queue import InMemoryGenerationQueue
from image_pipeline import PLATFORMS, assess_generated_asset_quality, export_delivery_zip
from matching_engine import (
    classify_kind as engine_classify_kind,
    grams as engine_grams,
    normalize_dish,
    similarity as engine_similarity,
    split_components as engine_split_components,
)
from menu_parser import parse_menu as parse_excel_menu


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LIBRARY_DIR = DATA_DIR / "library"
EXPORT_DIR = DATA_DIR / "exports"
MODEL_INPUT_DIR = DATA_DIR / "model_inputs"
AI_ASSET_DIR = LIBRARY_DIR / "_ai_asset_library"
for folder in (UPLOAD_DIR, LIBRARY_DIR, EXPORT_DIR, MODEL_INPUT_DIR, AI_ASSET_DIR):
    folder.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MENU_EXTS = {".xls", ".xlsx"}
SAFE_STYLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
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
TENCENT_TOKENHUB_IMAGE_LITE_URL = "https://tokenhub.tencentmaas.com/v1/api/image/lite"
TENCENT_TOKENHUB_IMAGE_SUBMIT_URL = "https://tokenhub.tencentmaas.com/v1/api/image/submit"
TENCENT_TOKENHUB_IMAGE_QUERY_URL = "https://tokenhub.tencentmaas.com/v1/api/image/query"
TENCENT_REQUEST_TIMEOUT = env_int("TENCENT_REQUEST_TIMEOUT", 55)
TENCENT_SYNC_LIMIT = env_int("TENCENT_HUNYUAN_SYNC_LIMIT", 6)
TENCENT_TOKENHUB_POLL_TIMEOUT = env_int("TENCENT_TOKENHUB_POLL_TIMEOUT", 120)
TENCENT_TOKENHUB_POLL_INTERVAL = max(1, env_int("TENCENT_TOKENHUB_POLL_INTERVAL", 3))
FINAL_GENERATION_WORKERS = max(1, env_int("FINAL_GENERATION_WORKERS", 3))
DEFAULT_TENCENT_COS_BUCKET = "waimai-image-tool-inputs-1311836560"
DEFAULT_TENCENT_COS_REGION = "ap-guangzhou"
AI_ASSET_SCHEMA_VERSION = 1
AI_ASSET_MANIFEST_NAME = "manifest.jsonl"
AI_ASSET_MANIFEST_LOCK = threading.Lock()
ADMIN_ROLE_VALUES = {"admin", "super_admin", "superadmin", "ops", "operator"}
MAX_LOGO_DATA_URL_CHARS = 1_500_000
MAX_LOGO_BYTES = 1_000_000
MAX_LOGO_PIXELS = 2_000_000
MAX_EXPORT_IMAGE_BYTES = 25 * 1024 * 1024
MAX_EXPORT_IMAGE_PIXELS = 24_000_000
MAX_EXPORT_IMAGE_SIDE = 12_000
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
generation_queue = InMemoryGenerationQueue(worker_count=FINAL_GENERATION_WORKERS)
ASSET_VERSION = os.environ.get("ASSET_VERSION") or str(
    int(max((BASE_DIR / "static" / "app.js").stat().st_mtime, (BASE_DIR / "static" / "styles.css").stat().st_mtime))
)

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


@app.context_processor
def inject_asset_version() -> dict[str, str]:
    return {"asset_version": ASSET_VERSION}


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

CATEGORY_PROMPTS = {
    "轻食健康餐": {
        "keywords": ("轻食", "沙拉", "健康餐", "健康碗", "能量碗", "鸡胸", "牛油果", "三明治", "意面", "酸奶", "杂粮饭", "波奇饭", "salad", "panini", "sandwich", "pasta", "yogurt", "bowl"),
        "styles": (
            "明亮自然光，浅色木桌，清爽健康轻食餐厅质感，适合沙拉和能量碗",
            "白色大理石台面，绿色植物点缀，清新高端轻食摄影",
            "奶油白背景，柔和漫反射光，干净留白，适合三明治和酸奶碗",
            "日式浅木餐盘场景，低饱和暖色，适合杂粮饭和鸡腿饭",
            "现代咖啡馆桌面，柔和侧光，精致融合料理氛围",
            "冷白灰极简背景，蔬果色彩突出，适合健康餐外卖主图",
        ),
    },
    "盖码饭/盖浇饭": {
        "keywords": ("盖码饭", "盖浇饭", "木桶饭", "辣椒炒肉", "黄牛肉", "现炒", "米饭"),
        "styles": (
            "温暖原木桌面，家常现炒盖码饭氛围",
            "深色石板背景，热气现炒菜商业摄影",
            "浅灰干净台面，米饭和浇头主体突出",
            "暖红促销色调，中式快餐热卖氛围",
            "竹编自然背景，湘菜盖码饭质感",
            "冷灰蓝现代餐盘背景，干净统一外卖主图",
        ),
    },
    "炒菜/川湘菜": {
        "keywords": ("川菜", "湘菜", "小炒", "水煮", "回锅肉", "鱼香肉丝", "辣", "干锅"),
        "styles": (
            "木质餐桌，热辣川湘小炒氛围",
            "黑石板背景，油亮菜品质感突出",
            "浅灰简洁背景，菜品色彩饱满",
            "红色热卖背景，香辣餐饮促销氛围",
            "竹编中式背景，烟火气但画面干净",
            "深蓝灰餐厅背景，高级中餐摄影",
        ),
    },
}

AI_ASSET_LIBRARY_PLAN = [
    {
        "phase": "1. 生成入口统一",
        "goal": "不同品类的背景图、免费样图、正式产品图默认由混元生成，不再把现成图库作为主生成来源。",
        "code": ["ai_first_generation_enabled", "tencent_style_background", "materialize_final_row"],
    },
    {
        "phase": "2. 资产沉淀",
        "goal": "混元生成成功后，把可复用的品类背景和正式产品图复制到 AI 资产库，并写入 manifest。",
        "code": ["persist_ai_generated_asset", "build_ai_asset_record"],
    },
    {
        "phase": "3. 标签入库",
        "goal": "入库时同步保存品类、菜名、归一化菜名、关键词、可匹配名称、风格、质量档、模型动作和源菜单。",
        "code": ["ai_asset_keywords", "ai_asset_match_names"],
    },
    {
        "phase": "4. 存储迁移",
        "goal": "本地开发写入 data/library/_ai_asset_library；生产环境配置 COS 后写入 ai-assets/* 对象前缀。",
        "code": ["persist_ai_generated_asset"],
    },
    {
        "phase": "5. 未来复用",
        "goal": "后续匹配优先查 AI 资产库，命中高置信资产时直接复用；命中不足时再调用混元生成并继续沉淀。",
        "code": ["load_ai_asset_records", "library_images"],
    },
]

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


def external_library_media_enabled() -> bool:
    return env_truthy("EXPOSE_EXTERNAL_LIBRARY_MEDIA", default=False)


def ai_first_generation_enabled() -> bool:
    return env_truthy("AI_FIRST_GENERATION", default=True)


def local_preview_fallback_enabled() -> bool:
    return env_truthy("ALLOW_LOCAL_PREVIEW_FALLBACK", default=False)


def local_final_fallback_enabled() -> bool:
    return env_truthy("ALLOW_LOCAL_FINAL_FALLBACK", default=False)


def local_background_fallback_enabled() -> bool:
    return env_truthy("ALLOW_LOCAL_BACKGROUND_FALLBACK", default=False)


def is_safe_style_id(style_id: str) -> bool:
    return bool(SAFE_STYLE_ID_RE.fullmatch(str(style_id or "")))


def safe_style_path_segment(style_id: str) -> str:
    style_id = str(style_id or "").strip()
    if not is_safe_style_id(style_id):
        raise ValueError("非法风格参数")
    return style_id


def public_library_images() -> list[LibraryImage]:
    images = library_images()
    if external_library_media_enabled():
        return images
    return [image for image in images if image.source == "internal"]


def public_style_ids() -> set[str]:
    return set(STYLE_COLORS) | {image.style_id for image in public_library_images() if image.style_id}


def validate_requested_style(raw_style: str | None, *, allow_empty: bool = False) -> str:
    style_id = str(raw_style or "").strip()
    if not style_id:
        if allow_empty:
            return ""
        raise ValueError("请先选择风格")
    safe_style_path_segment(style_id)
    if style_id not in public_style_ids():
        raise ValueError("风格不存在或不可用")
    return style_id


def configured_request_token(env_names: tuple[str, ...], header_name: str) -> bool:
    expected = next((os.environ.get(name, "").strip() for name in env_names if os.environ.get(name, "").strip()), "")
    if not expected:
        return False
    candidates = [
        request.headers.get(header_name, ""),
        request.headers.get("X-API-Token", ""),
    ]
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:])
    return any(hmac.compare_digest(str(candidate).strip(), expected) for candidate in candidates if str(candidate).strip())


def env_token_set(*env_names: str) -> set[str]:
    values: set[str] = set()
    for env_name in env_names:
        raw = os.environ.get(env_name, "")
        values.update(part.strip() for part in re.split(r"[,;\s]+", raw) if part.strip())
    return values


def is_local_request() -> bool:
    return request.remote_addr in {"127.0.0.1", "::1", None}


def local_demo_auth_allowed() -> bool:
    return env_truthy("ENABLE_LOCAL_DEMO_AUTH", default=True) and is_local_request()


def billing_write_authorized() -> bool:
    return configured_request_token(("BILLING_API_TOKEN", "ADMIN_API_TOKEN"), "X-Billing-Token")


def billing_token_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in ("BILLING_API_TOKEN", "ADMIN_API_TOKEN"))


def local_demo_billing_allowed(user_id: str) -> bool:
    return (
        env_truthy("ENABLE_LOCAL_DEMO_BILLING", default=True)
        and not billing_token_configured()
        and user_id == billing.DEFAULT_USER_ID
    )


def generation_write_authorized() -> bool:
    return configured_request_token(("GENERATION_API_TOKEN", "ADMIN_API_TOKEN"), "X-Generation-Token")


def generation_token_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in ("GENERATION_API_TOKEN", "ADMIN_API_TOKEN"))


def local_demo_generation_allowed() -> bool:
    return (
        env_truthy("ENABLE_LOCAL_DEMO_GENERATION", default=False)
        and not generation_token_configured()
        and is_local_request()
    )


def forbidden(message: str, code: str = "forbidden"):
    return jsonify({"error": message, "code": code}), 403


def product_db_conn(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = os.environ.get("STORAGE_DB_PATH") or os.environ.get("APP_DB_PATH") or None
    conn = storage_db.init_db(db_path)
    auth_service.init_auth_schema(conn)
    payment_service.init_payment_schema(conn)
    admin_actions.init_admin_actions_schema(conn)
    return conn


def session_token_from_request() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    return str(request.headers.get("X-Session-Token") or request.args.get("sessionToken") or "").strip()


def auth_error_response(exc: auth_service.AuthError):
    status = {
        auth_service.ERR_OTP_RATE_LIMITED: 429,
        auth_service.ERR_OTP_ATTEMPT_LIMITED: 429,
        auth_service.ERR_CHALLENGE_NOT_FOUND: 404,
        auth_service.ERR_USER_NOT_FOUND: 404,
    }.get(exc.code, 400)
    return jsonify({"error": str(exc), "code": exc.code}), status


def sms_error_response(exc: sms_service.SmsServiceError):
    return jsonify({"error": str(exc), "code": exc.code}), 503


def payment_error_response(exc: payment_service.PaymentServiceError):
    status = int(getattr(exc, "status_code", 400) or 400)
    if isinstance(exc, payment_service.PaymentSignatureError):
        status = 403
    elif isinstance(exc, payment_service.PaymentOrderNotFound):
        status = 404
    elif isinstance(exc, (payment_service.PaymentOrderConflict, payment_service.PaymentTransitionError)):
        status = 409
    return jsonify(exc.to_dict()), status


def requested_payment_provider(payload: dict[str, object]) -> str:
    return str(payload.get("provider") or os.environ.get("PAYMENT_PROVIDER") or "fake").strip() or "fake"


def fake_payment_provider_guard_error(*, callback: bool = False) -> payment_service.PaymentServiceError:
    if callback:
        return payment_service.FakePaymentProviderForbidden(
            "fake 支付回调未启用",
            provider="fake",
            required="PAYMENT_PROVIDER=fake or ALLOW_FAKE_PAYMENT_PROVIDER=true",
        )
    return payment_service.PaymentProviderUnavailable(
        "fake 支付 provider 未启用，不能创建 fake 支付订单",
        provider="fake",
        required="PAYMENT_PROVIDER=fake or ALLOW_FAKE_PAYMENT_PROVIDER=true",
    )


def growth_error_response(exc: growth_service.GrowthServiceError):
    status = 400
    if isinstance(exc, growth_service.GrowthNotFound):
        status = 404
    elif isinstance(exc, growth_service.GrowthConflict):
        status = 409
    return jsonify(exc.to_dict()), status


def commission_settlement_error_response(exc: commission_settlement_service.CommissionSettlementError):
    status = 400
    if isinstance(exc, commission_settlement_service.CommissionSettlementNotFound):
        status = 404
    elif isinstance(exc, commission_settlement_service.CommissionSettlementConflict):
        status = 409
    return jsonify(exc.to_dict()), status


def withdrawal_error_response(exc: withdrawal_service.WithdrawalServiceError):
    status = 400
    if isinstance(exc, withdrawal_service.WithdrawalNotFound):
        status = 404
    elif isinstance(exc, withdrawal_service.WithdrawalConflict):
        status = 409
    return jsonify(exc.to_dict()), status


def require_authenticated_session() -> tuple[dict[str, Any] | None, Any]:
    token = session_token_from_request()
    if not token:
        return None, (jsonify({"error": "请先登录", "code": "auth_required"}), 401)
    conn = product_db_conn()
    try:
        session = auth_service.get_session(conn, token)
        if session is None:
            return None, (jsonify({"error": "登录已失效", "code": "invalid_session"}), 401)
        return {**session, "token": token}, None
    finally:
        conn.close()


def current_authenticated_user_id() -> str:
    token = session_token_from_request()
    if not token:
        return current_user_id()
    conn = product_db_conn()
    try:
        session = auth_service.get_session(conn, token)
        if session is None:
            return current_user_id()
        return str(session["user_id"])
    finally:
        conn.close()


def auth_session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session.get("id", ""),
        "user_id": session.get("user_id", ""),
        "userId": session.get("user_id", ""),
        "created_at": session.get("created_at", ""),
        "createdAt": session.get("created_at", ""),
        "expires_at": session.get("expires_at", ""),
        "expiresAt": session.get("expires_at", ""),
        "last_seen_at": session.get("last_seen_at", ""),
        "lastSeenAt": session.get("last_seen_at", ""),
    }


def object_access_signing_secret() -> str:
    for env_name in ("OBJECT_SIGNING_SECRET", "ASSET_SIGNING_SECRET", "DOWNLOAD_SIGNING_SECRET"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def object_token_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in ("OBJECT_API_TOKEN", "ADMIN_API_TOKEN"))


def object_write_authorized(user_id: str) -> bool:
    del user_id
    return (
        configured_request_token(("OBJECT_API_TOKEN", "ADMIN_API_TOKEN"), "X-Object-Token")
        or (
            env_truthy("ENABLE_LOCAL_DEMO_OBJECTS", default=True)
            and not object_token_configured()
            and is_local_request()
        )
    )


def user_is_admin(user: dict[str, Any] | None) -> bool:
    if not user or str(user.get("status") or "active") != "active":
        return False

    user_id = str(user.get("id") or "").strip()
    if user_id and user_id in env_token_set("ADMIN_USER_IDS", "ADMIN_USER_ID"):
        return True

    phone = str(user.get("phone") or "").strip()
    admin_phones = env_token_set("ADMIN_PHONE_NUMBERS", "ADMIN_PHONE_NUMBER")
    for configured_phone in list(admin_phones):
        try:
            admin_phones.add(auth_rules.normalize_phone(configured_phone))
        except (TypeError, ValueError):
            pass
    if phone and phone in admin_phones:
        return True

    metadata = user.get("metadata") if isinstance(user.get("metadata"), dict) else {}
    if metadata.get("isAdmin") is True or metadata.get("is_admin") is True:
        return True

    roles: list[str] = []
    for key in ("role", "adminRole", "admin_role"):
        if metadata.get(key) is not None:
            roles.append(str(metadata[key]))
    metadata_roles = metadata.get("roles")
    if isinstance(metadata_roles, str):
        roles.extend(part.strip() for part in re.split(r"[,;\s]+", metadata_roles) if part.strip())
    elif isinstance(metadata_roles, (list, tuple, set)):
        roles.extend(str(role) for role in metadata_roles)

    return any(role.strip().lower() in ADMIN_ROLE_VALUES for role in roles)


def admin_session_authorized(token: str | None = None) -> bool:
    session_token = str(token or session_token_from_request() or "").strip()
    if not session_token:
        return False
    conn = product_db_conn()
    try:
        session = auth_service.get_session(conn, session_token)
    finally:
        conn.close()
    return bool(session and user_is_admin(session.get("user") if isinstance(session.get("user"), dict) else None))


def admin_write_authorized() -> bool:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return True
    if session_token_from_request():
        return admin_session_authorized()
    return (
        env_truthy("ENABLE_LOCAL_DEMO_ADMIN", default=True)
        and not os.environ.get("ADMIN_API_TOKEN", "").strip()
        and is_local_request()
    )


def admin_actor_user_id() -> str:
    return str(request.headers.get("X-Admin-User-Id") or current_authenticated_user_id())


def agent_profile_for_session(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    requested_agent_id: str = "",
) -> tuple[dict[str, Any] | None, Any]:
    user_id = str(session.get("user_id") or "")
    row = conn.execute(
        """
        SELECT * FROM agent_profiles
        WHERE user_id = ?
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return None, (jsonify({"error": "当前账号不是代理", "code": "agent_profile_required"}), 404)
    agent = dict(row)
    clean_requested_agent_id = str(requested_agent_id or "").strip()
    if clean_requested_agent_id and clean_requested_agent_id != str(agent["id"]):
        return None, forbidden("不能提现或查看其他代理账户", "agent_access_forbidden")
    return agent, None


def request_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return str(request.remote_addr or "")


def asset_action_for_purpose(purpose: str | None, fallback: str = "access") -> str:
    return {
        asset_security.PREVIEW: "preview",
        asset_security.ORIGINAL: "download",
        asset_security.EXPORT: "export",
        asset_security.ADMIN_REVIEW: "admin_review",
    }.get(str(purpose or ""), fallback)


def record_asset_access_audit(
    *,
    asset_id: str,
    action: str,
    user_id: str = "",
    agent_id: str = "",
    asset_type: str = "",
    allowed: bool,
    deny_reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    asset_id = str(asset_id or "").strip()
    action = str(action or "").strip()
    if not asset_id or not action:
        return
    conn = None
    try:
        conn = product_db_conn()
        admin_actions.record_asset_access(
            conn,
            asset_id=asset_id[:500],
            action=action[:80],
            user_id=str(user_id or ""),
            agent_id=str(agent_id or ""),
            asset_type=str(asset_type or ""),
            ip=request_ip(),
            allowed=allowed,
            deny_reason=str(deny_reason or ""),
            request_id=str(request.headers.get("X-Request-Id") or ""),
            user_agent=str(request.headers.get("User-Agent") or ""),
            metadata={
                "path": request.path,
                "method": request.method,
                **(metadata or {}),
            },
        )
    except Exception:
        return
    finally:
        if conn is not None:
            conn.close()


def payload_bool(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in payload:
        return default
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def apply_payment_growth_rewards(order: dict[str, Any], event_id: str = "") -> dict[str, Any]:
    conn = product_db_conn()
    try:
        growth = growth_service.record_payment_growth(
            conn,
            order_id=str(order["order_id"]),
            customer_id=str(order["user_id"]),
            paid_cents=int(order["amount_cents"]),
            source=str(order.get("provider") or "payment"),
            request_id=str(event_id or order["order_id"]),
        )
    finally:
        conn.close()

    reward = growth.get("consumerReferralReward")
    if isinstance(reward, dict) and int(reward.get("inviterPoints") or 0) > 0:
        try:
            growth["consumerReferralBilling"] = billing.credit_account(
                str(reward["inviterUserId"]),
                f"referral-first-payment:{order['order_id']}:{reward['inviteId']}",
                int(reward["inviterPoints"]),
                description="referral-first-payment",
                metadata={
                    "inviteId": reward["inviteId"],
                    "inviteeUserId": reward["inviteeUserId"],
                    "paymentOrderId": order["order_id"],
                },
            )
        except billing.BillingError as exc:
            growth["consumerReferralBillingError"] = exc.to_dict()
    return growth


def refund_cents_from_payload(payload: dict[str, Any], order: dict[str, Any]) -> int:
    raw = (
        payload.get("refund_cents")
        if "refund_cents" in payload
        else payload.get("refundCents", payload.get("refund_amount_cents", payload.get("amount_cents")))
    )
    try:
        return int(raw) if raw is not None else int(order["amount_cents"])
    except (TypeError, ValueError, KeyError):
        return int(order.get("amount_cents") or 0)


def apply_payment_growth_refund(order: dict[str, Any], payload: dict[str, Any], event_id: str = "") -> dict[str, Any]:
    conn = product_db_conn()
    try:
        growth = growth_service.record_payment_refund(
            conn,
            order_id=str(order["order_id"]),
            customer_id=str(order["user_id"]),
            paid_cents=int(order["amount_cents"]),
            refund_cents=refund_cents_from_payload(payload, order),
            source=str(order.get("provider") or "payment_refund"),
            request_id=str(event_id or order["order_id"]),
        )
    finally:
        conn.close()

    reward_refund = growth.get("consumerReferralRefund")
    if isinstance(reward_refund, dict) and int(reward_refund.get("inviterPointsToDebit") or 0) > 0:
        try:
            growth["consumerReferralRefundBilling"] = billing.debit_account(
                str(reward_refund["inviterUserId"]),
                f"referral-first-payment-refund:{order['order_id']}:{reward_refund['inviteId']}",
                int(reward_refund["inviterPointsToDebit"]),
                description="referral-first-payment-refund",
                metadata={
                    "inviteId": reward_refund["inviteId"],
                    "inviteeUserId": reward_refund["inviteeUserId"],
                    "paymentOrderId": order["order_id"],
                },
            )
        except billing.BillingError as exc:
            growth["consumerReferralRefundBillingError"] = exc.to_dict()
    return growth


def tencent_config() -> dict[str, Any]:
    secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID") or os.environ.get("TENCENT_SECRET_ID") or ""
    secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY") or os.environ.get("TENCENT_SECRET_KEY") or ""
    region = os.environ.get("TENCENTCLOUD_REGION") or os.environ.get("TENCENT_REGION") or "ap-guangzhou"
    enabled = env_truthy("TENCENT_HUNYUAN_ENABLED") or env_truthy("TENCENT_AIART_ENABLED")
    mode = os.environ.get("TENCENT_HUNYUAN_MODE", "auto").strip().lower() or "auto"
    return {"secret_id": secret_id, "secret_key": secret_key, "region": region, "enabled": enabled, "mode": mode}


def tokenhub_config() -> dict[str, Any]:
    api_key = (
        os.environ.get("TENCENT_TOKENHUB_API_KEY")
        or os.environ.get("TOKENHUB_API_KEY")
        or os.environ.get("HUNYUAN_TOKENHUB_API_KEY")
        or ""
    ).strip()
    model = (
        os.environ.get("TENCENT_TOKENHUB_IMAGE_MODEL")
        or os.environ.get("TOKENHUB_IMAGE_MODEL")
        or "hy-image-v3.0"
    ).strip()
    enabled = env_truthy("TENCENT_TOKENHUB_ENABLED", default=bool(api_key))
    return {"api_key": api_key, "model": model, "enabled": enabled}


def tokenhub_ready() -> bool:
    cfg = tokenhub_config()
    return bool(cfg["enabled"] and cfg["api_key"])


def tencent_cloud_ready() -> bool:
    cfg = tencent_config()
    return bool(cfg["enabled"] and cfg["secret_id"] and cfg["secret_key"])


def tencent_ready() -> bool:
    return tokenhub_ready() or tencent_cloud_ready()


def tencent_status_payload() -> dict[str, Any]:
    cfg = tencent_config()
    tokenhub = tokenhub_config()
    cos = tencent_cos_config()
    missing = []
    if not tokenhub_ready() and not tencent_cloud_ready():
        missing.append("TENCENT_TOKENHUB_API_KEY")
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
        "tokenhubReady": tokenhub_ready(),
        "tokenhubModel": tokenhub["model"],
        "cloudApiReady": tencent_cloud_ready(),
        "cosReady": cos["ready"],
        "cosBucket": cos["bucket"] if cos["ready"] else "",
        "cosRegion": cos["region"],
        "missing": missing,
    }


def generation_provider_readiness() -> dict[str, Any]:
    status = tencent_status_payload()
    app_env = os.environ.get("APP_ENV", "").strip().lower()
    live_generation_required = env_truthy(
        "REQUIRE_LIVE_GENERATION_PROVIDER",
        default=app_env in {"production", "prod", "staging"},
    )
    tokenhub_required = env_truthy(
        "REQUIRE_TOKENHUB_IMAGE_PROVIDER",
        default=live_generation_required and ai_first_generation_enabled(),
    )
    tokenhub_is_ready = bool(status.get("tokenhubReady"))
    cloud_api_is_ready = bool(status.get("cloudApiReady"))
    provider_configured = bool(status.get("configured"))
    errors: list[str] = []
    warnings: list[str] = []
    required_config: list[dict[str, Any]] = []
    missing_config: list[str] = []

    if tokenhub_is_ready:
        mode = "tokenhub"
    elif cloud_api_is_ready:
        mode = "legacy_cloud_api"
    else:
        mode = "unconfigured"

    if not provider_configured:
        if live_generation_required:
            errors.append("live_generation_provider_required")
        else:
            warnings.append("live_generation_provider_not_configured_local_demo_only")

    if tokenhub_required and not tokenhub_is_ready:
        errors.append("tokenhub_image_provider_required")
        required_config.append(
            {
                "key": "tencent_tokenhub_api_key",
                "env": ["TENCENT_TOKENHUB_API_KEY", "TOKENHUB_API_KEY", "HUNYUAN_TOKENHUB_API_KEY"],
            }
        )
        missing_config.append("TENCENT_TOKENHUB_API_KEY")
    elif cloud_api_is_ready and not tokenhub_is_ready:
        warnings.append("tokenhub_image_provider_not_configured_using_legacy_cloud_api")

    if cloud_api_is_ready and not tokenhub_is_ready:
        warnings.append("legacy_cloud_api_does_not_consume_tokenhub_hy_image_credits")

    return {
        "ready": not errors,
        "provider": status.get("provider"),
        "mode": mode,
        "appEnv": app_env or "development",
        "tokenhubReady": tokenhub_is_ready,
        "tokenhubModel": status.get("tokenhubModel"),
        "cloudApiReady": cloud_api_is_ready,
        "liveGenerationRequired": live_generation_required,
        "tokenhubRequired": tokenhub_required,
        "warnings": warnings,
        "errors": errors,
        "blockingIssues": list(errors),
        "requiredConfig": required_config,
        "missingConfig": missing_config,
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
    if not tencent_cloud_ready():
        raise RuntimeError("腾讯云旧版生图环境变量未配置完整")
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


def tokenhub_image_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = tokenhub_config()
    body: dict[str, Any] = {
        "model": cfg["model"],
        "prompt": str(payload.get("Prompt") or ""),
    }
    mappings = {
        "NegativePrompt": "negative_prompt",
        "Resolution": "resolution",
        "RspImgType": "rsp_img_type",
        "LogoAdd": "logo_add",
    }
    for source_key, target_key in mappings.items():
        if source_key in payload and payload[source_key] not in (None, ""):
            body[target_key] = payload[source_key]
    return body


def tokenhub_http_post(url: str, payload: dict[str, Any], timeout: int = TENCENT_REQUEST_TIMEOUT) -> dict[str, Any]:
    cfg = tokenhub_config()
    if not tokenhub_ready():
        raise RuntimeError("TokenHub API Key 未配置：请在 Render 设置 TENCENT_TOKENHUB_API_KEY")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TokenHub HTTP {exc.code}: {raw[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TokenHub 请求失败：{exc}") from exc
    data = json.loads(raw)
    if isinstance(data.get("error"), dict):
        error = data["error"]
        raise RuntimeError(f"TokenHub {error.get('code', 'Error')}: {error.get('message', '调用失败')}")
    return data


def tokenhub_result_image(response: dict[str, Any]) -> str:
    data = response.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return str(first.get("url") or first.get("image_url") or first.get("b64_json") or "")
    output = response.get("output")
    if isinstance(output, dict):
        images = output.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            return str(images[0].get("url") or images[0].get("image_url") or images[0].get("b64_json") or "")
        return str(output.get("url") or output.get("image_url") or "")
    return str(response.get("url") or response.get("image_url") or response.get("result_url") or "")


def tokenhub_request_id(response: dict[str, Any]) -> str:
    return str(response.get("id") or response.get("task_id") or response.get("request_id") or response.get("RequestId") or "")


def tokenhub_image_action(model: str) -> str:
    return "TokenHubImageLite" if "lite" in model.lower() else "TokenHubImageV3"


def normalize_tokenhub_image_response(response: dict[str, Any], model: str) -> dict[str, Any]:
    result_image = tokenhub_result_image(response)
    if not result_image:
        raise RuntimeError(f"TokenHub {model} 未返回图片 URL")
    return {
        "ResultImage": result_image,
        "RequestId": tokenhub_request_id(response),
        "Seed": response.get("seed") or response.get("Seed"),
        "_Endpoint": "tokenhub.tencentmaas.com",
        "_Action": tokenhub_image_action(model),
        "_Model": model,
        "_Provider": "tencent-hunyuan",
    }


def tokenhub_image_request(payload: dict[str, Any], timeout: int = TENCENT_REQUEST_TIMEOUT) -> dict[str, Any]:
    cfg = tokenhub_config()
    model = str(cfg["model"] or "hy-image-v3.0")
    body = tokenhub_image_payload(payload)
    if "lite" in model.lower():
        response = tokenhub_http_post(TENCENT_TOKENHUB_IMAGE_LITE_URL, body, timeout=timeout)
        return normalize_tokenhub_image_response(response, model)

    submitted = tokenhub_http_post(TENCENT_TOKENHUB_IMAGE_SUBMIT_URL, body, timeout=min(timeout, 30))
    job_id = tokenhub_request_id(submitted)
    if not job_id:
        return normalize_tokenhub_image_response(submitted, model)
    deadline = time.time() + max(1, TENCENT_TOKENHUB_POLL_TIMEOUT)
    last_response = submitted
    while time.time() < deadline:
        status = str(last_response.get("status") or last_response.get("task_status") or "").lower()
        if status in {"succeeded", "success", "completed", "finish", "finished"}:
            return normalize_tokenhub_image_response(last_response, model)
        if status in {"failed", "fail", "error", "canceled", "cancelled"}:
            error = last_response.get("error")
            raise RuntimeError(f"TokenHub {model} 任务失败：{error or last_response}")
        time.sleep(TENCENT_TOKENHUB_POLL_INTERVAL)
        remaining = max(1, int(deadline - time.time()))
        last_response = tokenhub_http_post(
            TENCENT_TOKENHUB_IMAGE_QUERY_URL,
            {"model": model, "id": job_id},
            timeout=min(timeout, remaining, 30),
        )
    status = str(last_response.get("status") or last_response.get("task_status") or "unknown")
    raise RuntimeError(f"TokenHub {model} 任务超时：{job_id} status={status}")


def tencent_api_request(action: str, payload: dict[str, Any], timeout: int = TENCENT_REQUEST_TIMEOUT) -> dict[str, Any]:
    if action == "TextToImageLite":
        errors = []
        if tokenhub_ready():
            try:
                return tokenhub_image_request(payload, timeout=timeout)
            except RuntimeError as exc:
                errors.append(str(exc))
        endpoints = [
            (TENCENT_AIART_HOST, TENCENT_AIART_SERVICE, TENCENT_AIART_VERSION),
            (TENCENT_HUNYUAN_HOST, TENCENT_HUNYUAN_SERVICE, TENCENT_HUNYUAN_VERSION),
        ]
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
        target = prepare_model_input_file(path)
        cos_url = upload_model_input_to_cos(target)
        if cos_url:
            return cos_url
        base = public_base_url()
        if base and "127.0.0.1" not in base and "localhost" not in base:
            return f"{base.rstrip('/')}/model-inputs/{target.name}"
    return candidate_public_url(candidate)


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


def external_image_path(image_id_with_ext: str) -> Path | None:
    if not external_library_media_enabled():
        return None
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
    category_prompt = category_style_prompt(style_id)
    if category_prompt:
        return category_prompt
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
        "provider": str(response.get("_Provider") or "tencent-hunyuan"),
        "action": str(response.get("_Action") or "TextToImageLite"),
        "promptType": prompt_type,
        "requestId": response.get("RequestId"),
        "seed": response.get("Seed"),
        "endpoint": response.get("_Endpoint"),
        "model": response.get("_Model"),
    }


def prompt_for_style_background(style_id: str) -> str:
    category = active_category_name() or "当前菜单品类"
    return (
        f"{category}外卖菜品主图背景风格样图，展示{style_prompt_for(style_id)}。"
        "一份符合该品类的代表菜品占位，背景、桌面、光影和色调清楚可见。"
        "主体完整居中，背景风格必须鲜明且和其他方案明显不同。"
        "不要出现文字、价格、logo、水印、人物。"
    )[:250]


def tencent_style_background(style_id: str, target: Path) -> dict[str, Any]:
    if ai_first_generation_enabled():
        response = tencent_api_request(
            "TextToImageLite",
            {
                "Prompt": prompt_for_style_background(style_id),
                "NegativePrompt": NEGATIVE_IMAGE_PROMPT,
                "Resolution": default_delivery_resolution(),
                "RspImgType": "url",
                "LogoAdd": 0,
            },
        )
        save_result_image(str(response.get("ResultImage") or ""), target)
        return {
            "provider": str(response.get("_Provider") or "tencent-hunyuan"),
            "action": str(response.get("_Action") or "TextToImageLite"),
            "promptType": "style_background",
            "requestId": response.get("RequestId"),
            "seed": response.get("Seed"),
            "endpoint": response.get("_Endpoint"),
            "model": response.get("_Model"),
        }
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


def resample_filter() -> int:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def cover_image(img: Image.Image, size: tuple[int, int], *, centering: tuple[float, float] = (0.5, 0.5), zoom: float = 1.0) -> Image.Image:
    source = img.convert("RGB")
    if zoom > 1:
        enlarged = (max(size[0], int(size[0] * zoom)), max(size[1], int(size[1] * zoom)))
        source = ImageOps.fit(source, enlarged, method=resample_filter(), centering=centering)
    return ImageOps.fit(source, size, method=resample_filter(), centering=centering)


def style_index(style_id: str) -> int:
    try:
        return max(0, list(STYLE_COLORS).index(style_id))
    except ValueError:
        return 0


def current_menu_cache_key() -> str:
    path = current_menu_path()
    if path is None:
        return "demo"
    try:
        stat = path.stat()
        source = f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except FileNotFoundError:
        source = path.name
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def active_category_name() -> str:
    try:
        return str(category_report(parse_menu()).get("category") or "")
    except Exception:
        return ""


def category_keywords(category_name: str | None = None) -> tuple[str, ...]:
    name = category_name or active_category_name()
    config = CATEGORY_PROMPTS.get(name)
    if config:
        return tuple(str(value).lower() for value in config["keywords"])
    return tuple(word.lower() for config in CATEGORY_PROMPTS.values() for word in config["keywords"])


def menu_terms(limit: int = 60) -> list[str]:
    try:
        menu = parse_menu()
    except Exception:
        return []
    terms = []
    for item in menu.get("items", [])[:limit]:
        text = str(item.get("name") or "")
        compact = normalize(text)
        if compact:
            terms.append(compact)
        for token in re.findall(r"[A-Za-z]{3,}", text):
            terms.append(token.lower())
    return terms


def category_style_prompt(style_id: str) -> str:
    category = active_category_name()
    prompts = CATEGORY_PROMPTS.get(category, {}).get("styles") or ()
    index = style_index(style_id)
    if index < len(prompts):
        return str(prompts[index])
    return STYLE_PROMPTS.get(style_id, "真实餐饮摄影，干净外卖主图")


def score_category_image(image: LibraryImage, keywords: tuple[str, ...], terms: list[str]) -> int:
    text = f"{image.store} {image.dish} {image.norm}".lower()
    score = 0
    for word in keywords:
        if word and word in text:
            score += 10
    for term in terms:
        if term and len(term) >= 2 and term in text:
            score += 3
    if image.source == "clean":
        score += 4
    if image.reusable:
        score += 2
    return score


def category_source_images(limit: int = PREVIEW_SAMPLE_COUNT * 2) -> list[LibraryImage]:
    images = [image for image in library_images() if image.reusable and image.path.exists() and image.path.suffix.lower() in IMAGE_EXTS]
    keywords = category_keywords()
    terms = menu_terms()
    ranked = sorted(images, key=lambda image: (score_category_image(image, keywords, terms), image.source == "clean", image.store, image.dish), reverse=True)
    chosen: list[LibraryImage] = []
    seen_paths: set[Path] = set()
    for image in ranked:
        if image.path in seen_paths:
            continue
        if score_category_image(image, keywords, terms) <= 0 and chosen:
            continue
        chosen.append(image)
        seen_paths.add(image.path)
        if len(chosen) >= limit:
            break
    if len(chosen) < limit:
        for image in images:
            if image.path in seen_paths:
                continue
            chosen.append(image)
            seen_paths.add(image.path)
            if len(chosen) >= limit:
                break
    return chosen


def style_tone(style_id: str) -> tuple[float, float, float]:
    return {
        "style-1": (1.04, 1.06, 1.02),
        "style-2": (0.88, 1.08, 1.12),
        "style-3": (1.10, 0.96, 0.96),
        "style-4": (1.03, 1.15, 1.08),
        "style-5": (1.02, 1.04, 0.95),
        "style-6": (0.96, 0.98, 1.10),
    }.get(style_id, (1.0, 1.0, 1.0))


def apply_style_tone(img: Image.Image, style_id: str) -> Image.Image:
    brightness, color, contrast = style_tone(style_id)
    out = ImageEnhance.Brightness(img).enhance(brightness)
    out = ImageEnhance.Color(out).enhance(color)
    out = ImageEnhance.Contrast(out).enhance(contrast)
    return out


def render_local_style_background(target: Path, style_id: str) -> dict[str, Any]:
    sources = category_source_images()
    if sources:
        preferred_offsets = (1, 2, 3, 4, 5, 7)
        source = sources[preferred_offsets[style_index(style_id) % len(preferred_offsets)] % len(sources)]
        img = cover_image(Image.open(source.path), (900, 720), centering=(0.55, 0.42), zoom=1.22)
    else:
        img = Image.new("RGB", (900, 720), STYLE_COLORS.get(style_id, ("", (238, 238, 238), (80, 80, 80)))[1])
    img = apply_style_tone(img, style_id)
    overlay = Image.new("RGBA", img.size, (255, 255, 255, 26 if style_id in {"style-1", "style-3", "style-5", "style-6"} else 10))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target, "JPEG", quality=92, optimize=True)
    return {
        "status": "fallback",
        "provider": "local-category",
        "action": "LocalCategoryBackground",
        "styleId": style_id,
        "category": active_category_name(),
    }


def candidate_path(candidate: dict[str, Any] | None) -> Path | None:
    if not candidate:
        return None
    path_text = str(candidate.get("path") or "")
    if not path_text:
        return None
    path = Path(path_text)
    return path if path.exists() and path.suffix.lower() in IMAGE_EXTS else None


def local_source_for_row(row: dict[str, Any]) -> Path | None:
    path = candidate_path(source_candidate_for_generation(row))
    if path:
        return path
    norm = str(row.get("norm") or normalize(str(row.get("name") or "")))
    item = {**row, "norm": norm}
    candidates = top_candidates(item, library_images(), limit=1, min_score=0.2)
    path = candidate_path(candidates[0] if candidates else None)
    if path:
        return path
    sources = category_source_images(1)
    return sources[0].path if sources else None


def ensure_style_background_image(style_id: str) -> Path:
    target = style_background_target(style_id)
    metadata = load_ai_output_metadata(target) if target.exists() else None
    if target.exists() and metadata and metadata.get("provider") != "local-demo":
        return target
    if not target.exists() or (metadata and metadata.get("provider") == "local-demo"):
        metadata = render_local_style_background(target, style_id)
        write_ai_output_metadata(target, metadata)
    return target


def render_local_composed_image(target: Path, dish: str, style_id: str, source_path: Path | None = None) -> None:
    if source_path and source_path.exists():
        image = cover_image(Image.open(source_path), (900, 720), centering=(0.52, 0.44), zoom=1.08)
    else:
        image = cover_image(Image.open(ensure_style_background_image(style_id)), (900, 720), centering=(0.5, 0.45), zoom=1.05)
    image = apply_style_tone(image, style_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=92, optimize=True)


def ai_asset_library_enabled() -> bool:
    return env_truthy("AI_ASSET_LIBRARY_ENABLED", default=True)


def ai_asset_cos_upload_enabled() -> bool:
    return env_truthy("AI_ASSET_UPLOAD_TO_COS", default=True)


def ai_asset_manifest_path() -> Path:
    return AI_ASSET_DIR / AI_ASSET_MANIFEST_NAME


def ai_asset_safe_part(value: str, fallback: str = "asset") -> str:
    part = safe_filename(value or fallback)
    part = re.sub(r"\s+", "-", part).strip(".-_")
    return part[:80] or fallback


def ai_asset_match_names(dish_name: str, components: list[str] | tuple[str, ...] | None = None) -> list[str]:
    names: list[str] = []
    for value in [dish_name, *(components or [])]:
        text = str(value or "").strip()
        if not text:
            continue
        cleaned = re.sub(r"[【\[].*?[】\]]", "", text)
        cleaned = re.sub(r"[（(][^）)]{0,40}[）)]", "", cleaned).strip(" -_·:：")
        for candidate in (text, cleaned, normalize(cleaned)):
            candidate = str(candidate or "").strip()
            if candidate and candidate not in names:
                names.append(candidate)
    return names[:16]


def ai_asset_keywords(dish_name: str, category: str, components: list[str] | tuple[str, ...] | None = None) -> list[str]:
    stopwords = {"招牌", "热销", "新品", "套餐", "单人餐", "双人餐", "自选", "免费", "活动", "点右上角免费领"}
    keywords: list[str] = []
    candidates = [category, dish_name, *(components or []), *category_keywords(category)]
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        chunks = [text]
        chunks.extend(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", text))
        norm = normalize(text)
        if norm:
            chunks.append(norm)
        for chunk in chunks:
            word = chunk.lower().strip(" -_·:：")
            if len(word) < 2 or word in stopwords or word in keywords:
                continue
            keywords.append(word)
    return keywords[:32]


def local_ai_asset_target(kind: str, category: str, style_id: str, dish_name: str, digest: str) -> Path:
    folder = AI_ASSET_DIR / ("backgrounds" if kind == "category_background" else "products") / ai_asset_safe_part(category, "uncategorized") / safe_style_path_segment(style_id)
    filename = f"{ai_asset_safe_part(dish_name, kind)}_{digest[:12]}.jpg"
    return folder / filename


def ai_asset_cos_key(kind: str, category: str, style_id: str, filename: str) -> str:
    prefix = os.environ.get("TENCENT_COS_AI_ASSET_PREFIX", "ai-assets").strip().strip("/") or "ai-assets"
    folder = "backgrounds" if kind == "category_background" else "products"
    return "/".join([prefix, folder, ai_asset_safe_part(category, "uncategorized"), safe_style_path_segment(style_id), safe_filename(filename)])


def upload_ai_asset_to_cos(path: Path, *, kind: str, category: str, style_id: str) -> dict[str, Any] | None:
    if not ai_asset_cos_upload_enabled():
        return None
    cos = tencent_cos_config()
    if not cos["ready"]:
        return None
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except Exception as exc:
        raise RuntimeError("已配置 TENCENT_COS_BUCKET，但缺少 cos-python-sdk-v5 依赖") from exc
    key = ai_asset_cos_key(kind, category, style_id, path.name)
    config = CosConfig(Region=cos["region"], SecretId=cos["secret_id"], SecretKey=cos["secret_key"], Scheme="https")
    client = CosS3Client(config)
    with path.open("rb") as file_obj:
        client.put_object(Bucket=cos["bucket"], Body=file_obj, Key=key, ContentType="image/jpeg")
    public_base = os.environ.get("TENCENT_COS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return {
        "bucket": cos["bucket"],
        "region": cos["region"],
        "key": key,
        "url": f"{public_base}/{urllib.parse.quote(key, safe='/%')}" if public_base else "",
    }


def image_file_fingerprint(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    with Image.open(path) as img:
        width, height = img.size
    return {"sha256": hashlib.sha256(raw).hexdigest(), "fileSize": len(raw), "width": width, "height": height}


def build_ai_asset_record(
    *,
    kind: str,
    source_path: Path,
    stored_path: Path,
    style_id: str,
    metadata: dict[str, Any],
    row: dict[str, Any] | None = None,
    dish_name: str = "",
    quality: str | None = None,
) -> dict[str, Any]:
    if kind not in {"category_background", "product_image"}:
        raise ValueError(f"invalid AI asset kind: {kind}")
    category_info = category_report(parse_menu())
    category = str(metadata.get("category") or category_info.get("category") or active_category_name() or "待人工确认")
    product_name = str(dish_name or (row or {}).get("name") or ("背景风格样图" if kind == "category_background" else "未命名菜品"))
    components = [str(value) for value in (row or {}).get("components", []) if str(value).strip()]
    fingerprint = image_file_fingerprint(stored_path)
    source_menu = current_menu_path()
    asset_id = hashlib.sha1(f"{kind}|{category}|{style_id}|{product_name}|{fingerprint['sha256']}".encode("utf-8")).hexdigest()[:20]
    return {
        "schemaVersion": AI_ASSET_SCHEMA_VERSION,
        "assetId": asset_id,
        "kind": kind,
        "category": category,
        "categoryConfidence": category_info.get("confidence"),
        "productName": product_name,
        "normalizedProductName": normalize(product_name),
        "matchNames": ai_asset_match_names(product_name, components),
        "keywords": ai_asset_keywords(product_name, category, components),
        "styleId": style_id,
        "styleName": style_name_for(style_id),
        "quality": quality_config(quality)["id"] if quality else "",
        "provider": metadata.get("provider"),
        "modelAction": metadata.get("action"),
        "promptType": metadata.get("promptType"),
        "sourceMenuKey": current_menu_cache_key(),
        "sourceMenuFile": source_menu.name if source_menu else "",
        "sourceRow": (row or {}).get("row"),
        "storageProvider": "local",
        "objectKey": stored_path.relative_to(AI_ASSET_DIR).as_posix(),
        "localPath": str(stored_path),
        "originalOutputPath": str(source_path),
        "sha256": fingerprint["sha256"],
        "width": fingerprint["width"],
        "height": fingerprint["height"],
        "fileSize": fingerprint["fileSize"],
        "reusable": True,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generation": metadata,
    }


def write_ai_asset_record(record: dict[str, Any]) -> dict[str, Any]:
    manifest = ai_asset_manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    record_path = AI_ASSET_DIR / str(record.get("localObjectKey") or record["objectKey"])
    record_path.with_suffix(record_path.suffix + ".json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    with AI_ASSET_MANIFEST_LOCK:
        repository = ai_asset_repository.AIAssetRepository(manifest)
        return repository.upsert(record)


def persist_ai_generated_asset(
    *,
    kind: str,
    source_path: Path,
    style_id: str,
    metadata: dict[str, Any],
    row: dict[str, Any] | None = None,
    dish_name: str = "",
    quality: str | None = None,
) -> dict[str, Any] | None:
    if not ai_asset_library_enabled() or metadata.get("provider") != "tencent-hunyuan":
        return None
    if not source_path.exists() or source_path.suffix.lower() not in IMAGE_EXTS:
        return None
    category = str(metadata.get("category") or active_category_name() or "待人工确认")
    product_name = str(dish_name or (row or {}).get("name") or ("背景风格样图" if kind == "category_background" else "未命名菜品"))
    digest = image_file_fingerprint(source_path)["sha256"]
    stored_path = local_ai_asset_target(kind, category, style_id, product_name, digest)
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    if not stored_path.exists():
        shutil.copyfile(source_path, stored_path)
    record = build_ai_asset_record(kind=kind, source_path=source_path, stored_path=stored_path, style_id=style_id, metadata=metadata, row=row, dish_name=product_name, quality=quality)
    quality_report = assess_generated_asset_quality(stored_path)
    record["qualityReport"] = quality_report
    record["qualityScore"] = quality_report["quality_score"]
    record["qualityStatus"] = quality_report["status"]
    record["qualityReasons"] = quality_report["reasons"]
    try:
        cos_asset = upload_ai_asset_to_cos(stored_path, kind=kind, category=record["category"], style_id=style_id)
    except Exception as exc:
        cos_asset = None
        record["storageError"] = str(exc)[:220]
    if cos_asset:
        record["storageProvider"] = "tencent-cos"
        record["cos"] = cos_asset
        record["objectKey"] = cos_asset["key"]
        record["localObjectKey"] = stored_path.relative_to(AI_ASSET_DIR).as_posix()
    normalized_record = write_ai_asset_record(record)
    return {**record, **normalized_record}


def load_ai_asset_records(limit: int = 5000) -> list[dict[str, Any]]:
    manifest = ai_asset_manifest_path()
    if not manifest.exists():
        return []
    repository = ai_asset_repository.AIAssetRepository(manifest)
    records = repository.list_assets()[: max(0, int(limit))]
    return [_legacy_ai_asset_aliases(record) for record in records]


def _legacy_ai_asset_aliases(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    aliases = {
        "asset_id": "assetId",
        "style_id": "styleId",
        "product_name": "productName",
        "normalized_product_name": "normalizedProductName",
        "match_names": "matchNames",
        "quality_score": "qualityScore",
        "quality_status": "qualityStatus",
        "quality_reasons": "qualityReasons",
        "object_key": "objectKey",
        "local_path": "localPath",
        "created_at": "createdAt",
    }
    for snake, camel in aliases.items():
        if camel not in payload and snake in payload:
            payload[camel] = payload[snake]
    return payload


def ai_asset_library_stats() -> dict[str, Any]:
    records = load_ai_asset_records()
    by_kind: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for record in records:
        kind = str(record.get("kind") or "unknown")
        category = str(record.get("category") or "未分类")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_category[category] = by_category.get(category, 0) + 1
    return {
        "enabled": ai_asset_library_enabled(),
        "cosUploadEnabled": ai_asset_cos_upload_enabled(),
        "manifest": str(ai_asset_manifest_path()),
        "total": len(records),
        "byKind": by_kind,
        "byCategory": by_category,
        "localRoot": str(AI_ASSET_DIR),
    }


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
    for record in load_ai_asset_records():
        if record.get("kind") != "product_image":
            continue
        if str(record.get("status") or "approved") != "approved":
            continue
        path = Path(str(record.get("localPath") or ""))
        if not path.exists() or path.suffix.lower() not in IMAGE_EXTS:
            continue
        dish = str(record.get("productName") or path.stem)
        norm = str(record.get("normalizedProductName") or normalize(dish))
        if not norm:
            continue
        image_id = str(record.get("assetId") or hashlib.sha1(str(path).encode()).hexdigest()[:18])
        style_id = str(record.get("styleId") or "style-upload")
        store = f"AI资产库/{record.get('category') or '未分类'}"
        images.append(LibraryImage(image_id, path, store, dish, norm, grams(norm), style_id, "hunyuan-product", True))
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
        if not external_library_media_enabled():
            return ""
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
    images = [image for image in public_library_images() if image.reusable]
    images.sort(key=lambda image: (not any(word in image.dish for word in preferred_names), image.store, image.dish))
    for image in images:
        candidate = candidate_from_path(image.path, image.dish, image.style_id, image.source, 100.0)
        if candidate_public_url(candidate):
            return candidate
    return None


def style_background_target(style_id: str) -> Path:
    return LIBRARY_DIR / "_style_backgrounds" / current_menu_cache_key() / safe_style_path_segment(style_id) / "背景风格样图.jpg"


def pending_style_background_candidate(style_id: str, action: str = "PendingGeneration") -> dict[str, Any]:
    target = style_background_target(style_id)
    candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 0.0)
    candidate["url"] = ""
    metadata = {
        "status": "pending",
        "provider": "tencent-hunyuan",
        "action": action,
        "styleId": style_id,
        "category": active_category_name(),
    }
    candidate_generation_metadata(candidate, metadata)
    return candidate


def style_sample_candidate(style_id: str, generate: bool = True) -> dict[str, Any]:
    target = style_background_target(style_id)
    metadata = load_ai_output_metadata(target) if target.exists() else None
    provider = str(metadata.get("provider") or "") if metadata else ""
    cached_style_usable = provider == "tencent-hunyuan" or (
        provider == "local-category" and local_background_fallback_enabled() and not tencent_ready()
    )
    if target.exists() and metadata and cached_style_usable:
        candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 100.0)
        if metadata:
            candidate_generation_metadata(candidate, metadata)
        return candidate
    if not generate:
        return pending_style_background_candidate(style_id, "PendingGeneration" if tencent_ready() else "WaitingForProvider")
    provider_error = ""
    if tencent_ready() and env_truthy("GENERATE_STYLE_BACKGROUNDS_WITH_TENCENT", default=True):
        try:
            detail = tencent_style_background(style_id, target)
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "styleId": style_id,
                "category": active_category_name(),
                "tencent": detail,
            }
            write_ai_output_metadata(target, metadata)
            candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 100.0)
            candidate_generation_metadata(candidate, metadata)
            asset_record = persist_ai_generated_asset(kind="category_background", source_path=target, style_id=style_id, metadata=metadata, dish_name="背景风格样图")
            if asset_record:
                candidate["assetRecordId"] = asset_record["assetId"]
            return candidate
        except Exception as exc:
            provider_error = str(exc)
    if not local_background_fallback_enabled():
        candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 0.0)
        candidate["url"] = ""
        metadata = {
            "status": "failed" if provider_error else "pending",
            "provider": "tencent-hunyuan",
            "action": "ProviderError" if provider_error else "WaitingForProvider",
            "styleId": style_id,
            "category": active_category_name(),
        }
        if provider_error:
            metadata["error"] = provider_error
        candidate_generation_metadata(candidate, metadata)
        return candidate
    metadata = render_local_style_background(target, style_id)
    write_ai_output_metadata(target, metadata)
    candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 90.0)
    candidate_generation_metadata(candidate, metadata)
    return candidate


def generated_preview_candidate(item: dict[str, Any], style_id: str) -> dict[str, Any] | None:
    if not style_id:
        return None
    try:
        safe_style = safe_style_path_segment(style_id)
    except ValueError:
        return None
    target = LIBRARY_DIR / "_generated_previews" / current_menu_cache_key() / safe_style / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    if not target.exists():
        return None
    metadata = load_ai_output_metadata(target)
    if not usable_preview_metadata(metadata):
        return None
    candidate = candidate_from_path(target, item["name"], style_id, "generated-preview", 99.9)
    if metadata:
        candidate_generation_metadata(candidate, metadata)
    return candidate


def usable_preview_metadata(metadata: dict[str, Any] | None) -> bool:
    if not metadata or metadata.get("status") not in {"succeeded", "fallback"}:
        return False
    if metadata.get("provider") == "tencent-hunyuan":
        return True
    return bool(metadata.get("provider") == "local-category" and local_preview_fallback_enabled())


def materialize_preview_candidate(item: dict[str, Any], selected_style: str, quality: str | None = "standard") -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        safe_style = safe_style_path_segment(selected_style)
    except ValueError as exc:
        return None, {"status": "failed", "provider": "local-demo", "action": "InvalidStyle", "error": str(exc)}
    target = LIBRARY_DIR / "_generated_previews" / current_menu_cache_key() / safe_style / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    cached = generated_preview_candidate(item, selected_style)
    if cached:
        return cached, {"status": "cached", "provider": cached.get("aiProvider") or "local-demo", "action": cached.get("generationAction") or "Cached"}
    same_style = reusable_selected_style_candidate(item, selected_style)
    if same_style:
        return same_style, {"status": "reused", "provider": "library", "action": "Reuse"}
    result: dict[str, Any] = {"status": "pending", "provider": "local-category", "action": "Preview"}
    if tencent_ready() and env_truthy("GENERATE_PREVIEW_SAMPLES_WITH_TENCENT", default=True):
        try:
            source_candidate = source_candidate_for_generation(item)
            if ai_first_generation_enabled():
                detail = tencent_text_to_image(item, selected_style, quality, target)
            else:
                detail = tencent_replace_background(item, source_candidate, selected_style, target, quality) if source_candidate else tencent_text_to_image(item, selected_style, quality, target)
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "reason": "free_style_preview",
                "row": item.get("row"),
                "dish": item.get("name"),
                "tencent": detail,
            }
            write_ai_output_metadata(target, metadata)
            candidate = candidate_from_path(target, item["name"], selected_style, f"tencent-preview-{detail['action']}", 100.0)
            candidate_generation_metadata(candidate, metadata)
            return candidate, {"status": "succeeded", "provider": "tencent-hunyuan", "action": detail["action"]}
        except Exception:
            pass
    if local_preview_fallback_enabled():
        render_local_composed_image(target, item["name"], selected_style, local_source_for_row(item))
        metadata = {"status": "fallback", "provider": "local-category", "action": "LocalCategoryFallback", "reason": "free_style_preview"}
        write_ai_output_metadata(target, metadata)
        candidate = candidate_from_path(target, item["name"], selected_style, "generated-preview", 80.0)
        candidate_generation_metadata(candidate, metadata)
        return candidate, {"status": "fallback", "provider": "local-category", "action": "LocalCategoryFallback"}
    result.update({"status": "pending", "provider": "tencent-hunyuan", "action": "WaitingForModelConfig", "error": "混元未配置，不能生成高正确率样图"})
    return None, result


def ai_output_candidate(item: dict[str, Any], style_id: str, quality: str | None, source: str) -> tuple[dict[str, Any], Path]:
    safe_style = safe_style_path_segment(style_id)
    quality_id = quality_config(quality)["id"]
    target = LIBRARY_DIR / "_ai_outputs" / current_menu_cache_key() / safe_style / quality_id / f"{int(item['row']):04d}_{safe_filename(item['name'])}.jpg"
    candidate = candidate_from_path(target, item["name"], safe_style, source, 100.0)
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
    if not usable_generated_metadata(metadata):
        return None
    assert metadata is not None
    candidate["aiProvider"] = str(metadata.get("provider") or "local-category")
    candidate["generationStatus"] = "cached"
    candidate["generationAction"] = str(metadata.get("action") or "")
    candidate["generationProvider"] = str(metadata.get("provider") or "")
    if isinstance(metadata.get("tencent"), dict):
        candidate["tencent"] = metadata["tencent"]
    return candidate


def is_generated_candidate(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    source = str(candidate.get("source") or "")
    return bool(candidate.get("generated") or source.startswith("generated") or source.startswith("tencent"))


def is_local_seed_candidate(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    source = str(candidate.get("source") or "").lower()
    text = " ".join(str(candidate.get(key) or "") for key in ("store", "path", "url", "remoteUrl", "cosKey")).lower()
    return source == "internal" or "seed_" in text or "demo_store" in text


def public_gallery_candidate(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    return bool(candidate.get("remoteUrl") or candidate.get("cosKey") or is_public_http_url(str(candidate.get("url") or "")))


def visible_source_candidates(candidates: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    public_candidates = [candidate for candidate in candidates if public_gallery_candidate(candidate) and not is_local_seed_candidate(candidate)]
    if public_candidates:
        return public_candidates[:limit]
    return candidates[:limit]


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
    if ai_first_generation_enabled():
        return "ai_first_generation"
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


def usable_generated_metadata(metadata: dict[str, Any] | None) -> bool:
    if not metadata or metadata.get("status") not in {"succeeded", "fallback"}:
        return False
    if metadata.get("provider") == "tencent-hunyuan":
        return True
    return bool(metadata.get("provider") == "local-category" and local_final_fallback_enabled())


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


SENSITIVE_PUBLIC_PAYLOAD_KEYS = frozenset(
    (
        "path",
        "localPath",
        "local_path",
        "objectKey",
        "object_key",
        "localObjectKey",
        "local_object_key",
        "originalOutputPath",
        "original_output_path",
        "sourcePath",
        "source_path",
        "storageKey",
        "storage_key",
        "cosKey",
        "cos_key",
    )
)


def public_payload_key_sensitive(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return key in SENSITIVE_PUBLIC_PAYLOAD_KEYS


def strip_sensitive_public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_sensitive_public_payload(item)
            for key, item in value.items()
            if not public_payload_key_sensitive(key)
        }
    if isinstance(value, list):
        return [strip_sensitive_public_payload(item) for item in value]
    return value


def public_candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = strip_sensitive_public_payload(candidate)
    if not external_library_media_enabled() and str(payload.get("url") or "").startswith("/external-media/"):
        payload["url"] = ""
    return payload


def public_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = strip_sensitive_public_payload(row)
    if isinstance(payload.get("candidates"), list):
        payload["candidates"] = [public_candidate_payload(candidate) for candidate in payload["candidates"] if isinstance(candidate, dict)]
    if isinstance(payload.get("sourceCandidates"), list):
        payload["sourceCandidates"] = [public_candidate_payload(candidate) for candidate in payload["sourceCandidates"] if isinstance(candidate, dict)]
    if isinstance(payload.get("candidate"), dict):
        payload["candidate"] = public_candidate_payload(payload["candidate"])
    if isinstance(payload.get("componentMatches"), list):
        component_matches = []
        for match in payload["componentMatches"]:
            if not isinstance(match, dict):
                continue
            match_payload = dict(match)
            if isinstance(match_payload.get("candidates"), list):
                match_payload["candidates"] = [
                    public_candidate_payload(candidate) for candidate in match_payload["candidates"] if isinstance(candidate, dict)
                ]
            component_matches.append(match_payload)
        payload["componentMatches"] = component_matches
    return payload


def public_style_payload(style: dict[str, Any]) -> dict[str, Any]:
    payload = strip_sensitive_public_payload(style)
    if isinstance(payload.get("sample"), dict):
        payload["sample"] = public_candidate_payload(payload["sample"])
    return payload


def public_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    payload = strip_sensitive_public_payload(plan)
    if isinstance(payload.get("results"), list):
        payload["results"] = [public_row_payload(row) for row in payload["results"] if isinstance(row, dict)]
    if isinstance(payload.get("styles"), list):
        payload["styles"] = [public_style_payload(style) for style in payload["styles"] if isinstance(style, dict)]
    return payload


def public_preview_payload(preview: dict[str, Any]) -> dict[str, Any]:
    payload = strip_sensitive_public_payload(preview)
    if isinstance(payload.get("samples"), list):
        payload["samples"] = [public_row_payload(sample) for sample in payload["samples"] if isinstance(sample, dict)]
    if isinstance(payload.get("sample"), dict):
        payload["sample"] = public_row_payload(payload["sample"])
    return payload


def public_object_access_payload(access: dict[str, Any]) -> dict[str, Any]:
    return strip_sensitive_public_payload(access)


def signed_export_download_url(download_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(download_url or ""))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/download/"):
        return str(download_url or "")

    relative_name = urllib.parse.unquote(parsed.path[len("/download/") :])
    safe_name = safe_export_download_name(relative_name)
    if safe_name is None:
        return str(download_url or "")

    secret = download_signing_secret()
    if not secret:
        return str(download_url or "")

    token = asset_security.sign_asset_url(
        {
            "asset_id": safe_name,
            "user_id": current_user_id(),
            "order_id": "",
            "variant": asset_security.EXPORT,
            "purpose": asset_security.EXPORT,
            "expires_at": int(time.time() + asset_security.DOWNLOAD_TOKEN_TTL_SECONDS),
            "nonce": secrets.token_urlsafe(16),
        },
        secret,
    )
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if key != "token"]
    query.append(("token", token))
    return urllib.parse.urlunsplit(
        (
            "",
            "",
            f"/download/{urllib.parse.quote(safe_name, safe='/')}",
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def public_export_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public = strip_sensitive_public_payload(payload)
    public["download"] = signed_export_download_url(str(public.get("download") or ""))
    return public


def candidate_generation_metadata(candidate: dict[str, Any], metadata: dict[str, Any]) -> None:
    candidate["aiProvider"] = str(metadata.get("provider") or candidate.get("aiProvider") or "")
    candidate["generationStatus"] = str(metadata.get("status") or "")
    candidate["generationAction"] = str(metadata.get("action") or "")
    candidate["generationProvider"] = str(metadata.get("provider") or "")
    if metadata.get("error"):
        candidate["generationError"] = str(metadata.get("error"))
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


def generation_action_counts(*actions: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action] = counts.get(action, 0) + 1
    return counts


def merge_generation_row_result(generation: dict[str, Any], result: dict[str, Any]) -> None:
    for key in ("succeeded", "fallback", "localFallback", "actionFallback", "failed", "pending"):
        generation[key] += int(result.get(key) or 0)
    generation["errors"].extend(result.get("errors") or [])
    for action, count in (result.get("actions") or {}).items():
        for _ in range(int(count or 0)):
            bump_generation_action(generation, action)


def materialize_final_row(
    row: dict[str, Any],
    selected_style: str,
    quality: str | None,
    status: dict[str, Any],
    reason: str,
    source_candidate: dict[str, Any] | None,
    item_result: dict[str, Any],
) -> dict[str, Any]:
    _, target = ai_output_candidate(row, selected_style, quality, "generated-final")
    metrics = {
        "succeeded": 0,
        "fallback": 0,
        "localFallback": 0,
        "actionFallback": 0,
        "failed": 0,
        "pending": 0,
        "errors": [],
        "actions": {},
        "item": item_result,
    }
    used_tencent = False
    detail: dict[str, Any] | None = None
    replace_error: Exception | None = None
    if status["configured"]:
        item_result["attempted"] = True
        try:
            if ai_first_generation_enabled():
                detail = tencent_text_to_image(row, selected_style, quality, target)
            elif source_candidate:
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
                metrics["fallback"] += 1
                metrics["actionFallback"] += 1
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
                "category": active_category_name(),
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
            asset_record = persist_ai_generated_asset(kind="product_image", source_path=target, style_id=selected_style, metadata=metadata, row=row, quality=quality)
            if asset_record:
                ai_candidate["assetRecordId"] = asset_record["assetId"]
            promote_candidate(row, ai_candidate)
            used_tencent = True
            metrics["succeeded"] += 1
            item_result.update({"provider": "tencent-hunyuan", "action": detail["action"], "status": "succeeded", "succeeded": True, "promptType": detail.get("promptType")})
            metrics["actions"] = generation_action_counts(detail["action"])
        except Exception as exc:
            metrics["errors"].append({"dish": row.get("name"), "message": str(exc)[:220]})
            item_result["error"] = str(exc)[:220]
    if not used_tencent:
        if status["configured"]:
            metrics["failed"] += 1
            metrics["pending"] += 1
            item_result.update({"provider": "tencent-hunyuan", "action": "Failed", "status": "failed"})
            row["backgroundAction"] = "模型生成失败"
            row["publicStatus"] = "模型生成失败"
            row["generationStatus"] = "failed"
            row["generation"] = item_result
            metrics["actions"] = generation_action_counts("Failed")
            return metrics
        if not local_final_fallback_enabled():
            metrics["pending"] += 1
            item_result.update({"provider": "tencent-hunyuan", "action": "WaitingForModelConfig", "status": "pending", "error": "混元未配置，正式图未生成"})
            row["backgroundAction"] = "等待混元生成"
            row["publicStatus"] = "等待混元生成"
            row["generationStatus"] = "pending"
            row["generation"] = item_result
            metrics["actions"] = generation_action_counts("WaitingForModelConfig")
            return metrics
        render_local_composed_image(target, row["name"], selected_style, local_source_for_row(row))
        ai_candidate, _ = ai_output_candidate(row, selected_style, quality, "generated-local")
        metadata = {
            "status": "fallback",
            "provider": "local-category",
            "action": "LocalCategoryFallback",
            "reason": reason,
            "row": row.get("row"),
            "dish": row.get("name"),
            "error": item_result.get("error"),
        }
        write_ai_output_metadata(target, metadata)
        candidate_generation_metadata(ai_candidate, metadata)
        promote_candidate(row, ai_candidate)
        metrics["fallback"] += 1
        metrics["localFallback"] += 1
        item_result.update({"provider": "local-category", "action": "LocalCategoryFallback", "status": "fallback", "fallback": True})
        metrics["actions"] = generation_action_counts("LocalCategoryFallback")
    row["backgroundAction"] = "正式生成"
    row["publicStatus"] = "已生成" if used_tencent else "本地兜底"
    row["generationStatus"] = "succeeded" if used_tencent else "fallback"
    row["generation"] = item_result
    return metrics


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
        "workers": FINAL_GENERATION_WORKERS,
    }
    if not selected_style:
        generation["action"] = "missing_selected_style"
        return generation
    live_budget = TENCENT_SYNC_LIMIT if TENCENT_SYNC_LIMIT >= 0 else 0
    items_by_index: dict[int, dict[str, Any]] = {}
    tasks: list[tuple[int, dict[str, Any], str, dict[str, Any] | None, dict[str, Any]]] = []
    for index, row in enumerate(plan["results"]):
        strip_nonfinal_generated_candidates(row)
        reason = materialization_reason(row, selected_style)
        source_candidate = source_candidate_for_generation(row)
        item_result = generation_row_result(row, status["provider"], "Reuse", reason)
        if reason is None:
            generation["skipped"] += 1
            item_result.update({"provider": "library", "status": "reused", "reason": "same_style_reuse"})
            bump_generation_action(generation, "Reuse")
            row["generation"] = item_result
            items_by_index[index] = item_result
            continue

        _, target = ai_output_candidate(row, selected_style, quality, "generated-final")
        metadata = load_ai_output_metadata(target) if target.exists() else None
        if target.exists() and usable_generated_metadata(metadata):
            assert metadata is not None
            cached_action = str(metadata.get("action") or "Cached")
            provider = str(metadata.get("provider") or "local-category")
            source = f"tencent-{cached_action}" if provider == "tencent-hunyuan" else "generated-local"
            ai_candidate, _ = ai_output_candidate(row, selected_style, quality, source)
            candidate_generation_metadata(ai_candidate, metadata)
            promote_candidate(row, ai_candidate)
            row["publicStatus"] = "已生成"
            row["backgroundAction"] = "正式生成"
            row["generationStatus"] = "cached"
            item_result.update({"provider": provider, "action": cached_action, "status": "cached", "succeeded": True, "cached": True})
            generation["cached"] += 1
            bump_generation_action(generation, "Cached")
            row["generation"] = item_result
            items_by_index[index] = item_result
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
            items_by_index[index] = item_result
            continue

        if status["configured"]:
            generation["attempted"] += 1
        assert reason is not None
        tasks.append((index, row, reason, source_candidate, item_result))

    if tasks:
        worker_count = min(FINAL_GENERATION_WORKERS, len(tasks))
        if worker_count == 1:
            for index, row, reason, source_candidate, item_result in tasks:
                result = materialize_final_row(row, selected_style, quality, status, reason, source_candidate, item_result)
                merge_generation_row_result(generation, result)
                items_by_index[index] = result["item"]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(materialize_final_row, row, selected_style, quality, status, reason, source_candidate, item_result): index
                    for index, row, reason, source_candidate, item_result in tasks
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    result = future.result()
                    merge_generation_row_result(generation, result)
                    items_by_index[index] = result["item"]
    generation["items"] = [items_by_index[index] for index in sorted(items_by_index)]
    return generation


def style_options(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidate_style_ids = {c["styleId"] for r in results for c in r["candidates"] if c["styleId"]}
    library_style_ids = {image.style_id for image in public_library_images() if image.style_id}
    style_ids = list(STYLE_COLORS)
    for style_id in sorted(candidate_style_ids | library_style_ids):
        if style_id not in style_ids:
            style_ids.append(style_id)
    for style_id in sorted({image.style_id for image in public_library_images() if image.style_id}):
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
            sample = style_sample_candidate(style_id, generate=False)
        else:
            sample = sample or style_sample_candidate(style_id, generate=False)
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


def ensure_demo_balance(user_id: str) -> None:
    if not local_demo_billing_allowed(user_id) or DEMO_BALANCE_POINTS <= 0:
        return
    try:
        billing.credit_account(
            user_id,
            "demo_balance_seed",
            DEMO_BALANCE_POINTS,
            description="本地测试演示余额",
            metadata={"demo": True},
        )
    except billing.OrderConflict:
        return
    except billing.BillingError:
        return


def account_payload(user_id: str | None = None) -> dict[str, Any]:
    actual_user_id = user_id or billing.DEFAULT_USER_ID
    ensure_demo_balance(actual_user_id)
    account = billing.account_payload(actual_user_id)
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
    account["referral"] = {
        "inviterRegisterReward": 50,
        "inviteeRegisterReward": 50,
        "firstPayReward": "直接邀请首充返 10% 积分，仅限一级邀请，不能提现",
        "expireDays": 180,
    }
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
        "aiFirstGeneration": ai_first_generation_enabled(),
        "localPreviewFallback": local_preview_fallback_enabled(),
        "localFinalFallback": local_final_fallback_enabled(),
        "localBackgroundFallback": local_background_fallback_enabled(),
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
            {"status": "cached", "provider": candidate.get("aiProvider") or "local-demo", "action": candidate.get("generationAction") or "Cached"}
            if candidate
            else {"status": "pending", "provider": "local-demo", "action": "Preview"}
        )
    public_status = "免费样图"
    if generation.get("status") == "failed":
        public_status = "样图生成失败"
    elif generation.get("status") in {"pending", "limited"}:
        public_status = "等待生成"
    return {**item, "candidate": candidate, "sourceCandidates": visible_source_candidates(candidates, 3), "generation": generation, "points": 0, "publicStatus": public_status}


def preview_sample_payload(selected_style: str, index: int, generate: bool = True) -> dict[str, Any]:
    entries = preview_sample_entries()
    if index < 0 or index >= len(entries):
        raise IndexError("样图序号不存在")
    return preview_sample_payload_from_entry(selected_style, entries[index], generate=generate)


def preview_samples(selected_style: str, generate: bool = False) -> dict[str, Any]:
    entries = preview_sample_entries()
    if generate and entries:
        samples_by_index: dict[int, dict[str, Any]] = {}
        worker_count = min(FINAL_GENERATION_WORKERS, len(entries))
        if worker_count == 1:
            for index, entry in enumerate(entries):
                samples_by_index[index] = preview_sample_payload_from_entry(selected_style, entry, generate=True)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(preview_sample_payload_from_entry, selected_style, entry, True): index
                    for index, entry in enumerate(entries)
                }
                for future in as_completed(future_map):
                    samples_by_index[future_map[future]] = future.result()
        samples = [samples_by_index[index] for index in sorted(samples_by_index)]
    else:
        samples = [preview_sample_payload_from_entry(selected_style, entry, generate=generate) for entry in entries]
    return {
        "style": selected_style,
        "styleName": STYLE_COLORS.get(selected_style, ("上传风格", None, None))[0],
        "samples": samples,
        "previewFreeImages": PREVIEW_SAMPLE_COUNT,
    }


def build_plan(selected_style: str = "", quality: str | None = "standard") -> dict[str, Any]:
    if selected_style:
        selected_style = safe_style_path_segment(selected_style)
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
            "referral": {
                "inviterRegisterReward": 50,
                "inviteeRegisterReward": 50,
                "firstPayReward": "直接邀请首充返 10% 积分，仅限一级邀请，不能提现",
                "expireDays": 180,
            },
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


def validate_image_bounds(img: Image.Image, *, max_pixels: int = MAX_EXPORT_IMAGE_PIXELS) -> None:
    width, height = img.size
    if width <= 0 or height <= 0:
        raise ValueError("invalid image dimensions")
    if width > MAX_EXPORT_IMAGE_SIDE or height > MAX_EXPORT_IMAGE_SIDE:
        raise ValueError("image dimensions exceed limit")
    if width * height > max_pixels:
        raise ValueError("image pixel count exceeds limit")
    if img.format and img.format.upper() not in ALLOWED_IMAGE_FORMATS:
        raise ValueError("unsupported image format")


def open_export_image(path: Path) -> Image.Image:
    if path.stat().st_size > MAX_EXPORT_IMAGE_BYTES:
        raise ValueError("image file size exceeds limit")
    img = Image.open(path)
    try:
        validate_image_bounds(img)
        img.load()
        return img
    except Exception:
        img.close()
        raise


def make_logo_watermark(data_url: str, width: int) -> Image.Image | None:
    payload = str(data_url or "")
    if len(payload) > MAX_LOGO_DATA_URL_CHARS:
        return None
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=True)
        if len(raw) > MAX_LOGO_BYTES:
            return None
        with Image.open(io.BytesIO(raw)) as src:
            validate_image_bounds(src, max_pixels=MAX_LOGO_PIXELS)
            logo = src.convert("RGBA")
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
            try:
                raw_img = open_export_image(src)
            except Exception:
                rows.append({"菜品名": row["name"], "分类": row["category"], "类型": row["kind"], "平台": "", "尺寸": "", "文件大小KB": "", "平台上限KB": "", "图片状态": "待补图", "预计积分": row["points"], "品牌水印": "未添加", "交付文件": ""})
                continue
            with raw_img:
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
    try:
        style = validate_requested_style(request.args.get("style", ""), allow_empty=True)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(public_plan_payload(build_plan(style, request.args.get("quality", "standard"))))


@app.get("/api/style-background")
def api_style_background():
    try:
        style = validate_requested_style(request.args.get("style", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    generate = str(request.args.get("generate") or "").strip().lower() in {"1", "true", "yes", "on"}
    sample = style_sample_candidate(style, generate=generate)
    return jsonify(public_style_payload({
        "id": style,
        "styleId": style,
        "name": style_name_for(style),
        "sample": sample,
    }))


@app.get("/api/style-preview")
def api_style_preview():
    try:
        style = validate_requested_style(request.args.get("style", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    generate = str(request.args.get("generate") or "").strip().lower() in {"1", "true", "yes", "on"}
    return jsonify(public_preview_payload(preview_samples(style, generate=generate)))


@app.get("/api/style-preview-sample")
def api_style_preview_sample():
    try:
        style = validate_requested_style(request.args.get("style", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        index = int(request.args.get("index", "0"))
        return jsonify(public_preview_payload({"style": style, "index": index, "sample": preview_sample_payload(style, index, generate=True)}))
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


@app.post("/api/auth/request-otp")
def api_auth_request_otp():
    payload = request.get_json(silent=True) or {}
    local_demo = local_demo_auth_allowed()
    sms_provider = sms_service.provider_from_env(local_demo_enabled=local_demo)
    try:
        sms_provider.ensure_available()
    except sms_service.SmsServiceError as exc:
        return sms_error_response(exc)

    conn = product_db_conn()
    try:
        challenge = auth_service.request_otp(
            conn,
            str(payload.get("phone") or ""),
            ip=request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",", 1)[0].strip(),
            user_agent=request.headers.get("User-Agent", ""),
        )
        sms_result = sms_provider.send_otp(
            phone=challenge["phone"],
            code=challenge["code"],
            ttl_seconds=auth_service.OTP_TTL_SECONDS,
            purpose="login",
        )
    except auth_service.AuthError as exc:
        return auth_error_response(exc)
    except sms_service.SmsServiceError as exc:
        return sms_error_response(exc)
    finally:
        conn.close()

    body = {
        "ok": True,
        "challenge_id": challenge["challenge_id"],
        "challengeId": challenge["challenge_id"],
        "phone": challenge["phone"],
        "expires_at": challenge["expires_at"],
        "expiresAt": challenge["expires_at"],
        "sms": sms_result.public_payload(),
    }
    expose_mock_otp = env_truthy("AUTH_EXPOSE_MOCK_OTP", default=False) or local_demo
    if expose_mock_otp:
        body["mockCode"] = challenge["code"]
    return jsonify(body)


@app.post("/api/auth/verify-otp")
def api_auth_verify_otp():
    payload = request.get_json(silent=True) or {}
    challenge_id = str(payload.get("challengeId") or payload.get("challenge_id") or "")
    code = str(payload.get("code") or payload.get("otp") or "")
    conn = product_db_conn()
    try:
        result = auth_service.verify_otp(conn, challenge_id, code)
        stores = auth_service.list_user_stores(conn, result["user"]["id"])
        session = result["session"]
        return jsonify(
            {
                "ok": True,
                "user": result["user"],
                "session": {
                    **auth_session_payload(session),
                    "token": session["token"],
                },
                "stores": stores,
            }
        )
    except auth_service.AuthError as exc:
        return auth_error_response(exc)
    finally:
        conn.close()


@app.get("/api/auth/session")
def api_auth_session():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    conn = product_db_conn()
    try:
        stores = auth_service.list_user_stores(conn, str(session["user_id"]))
    finally:
        conn.close()
    return jsonify(
        {
            "ok": True,
            "user": session["user"],
            "session": auth_session_payload(session),
            "stores": stores,
        }
    )


@app.post("/api/auth/logout")
def api_auth_logout():
    token = session_token_from_request()
    if not token:
        return jsonify({"ok": True, "loggedOut": False})
    conn = product_db_conn()
    try:
        logged_out = auth_service.logout(conn, token)
    finally:
        conn.close()
    return jsonify({"ok": True, "loggedOut": logged_out})


@app.get("/api/stores")
def api_list_stores():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    conn = product_db_conn()
    try:
        stores = auth_service.list_user_stores(conn, str(session["user_id"]))
    finally:
        conn.close()
    return jsonify({"ok": True, "stores": stores})


@app.post("/api/stores")
def api_create_store():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        store = auth_service.create_store(conn, str(session["user_id"]), str(payload.get("name") or ""))
    except auth_service.AuthError as exc:
        return auth_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "store": store})


@app.post("/api/growth/agents")
def api_create_agent_profile():
    payload = request.get_json(silent=True) or {}
    admin_allowed = admin_write_authorized()
    if admin_allowed:
        session_user_id = current_authenticated_user_id()
    else:
        session, error = require_authenticated_session()
        if error:
            return error
        assert session is not None
        session_user_id = str(session["user_id"])
    user_id = str(payload.get("userId") or payload.get("user_id") or session_user_id)
    if not admin_allowed and user_id != session_user_id:
        return forbidden("不能为其他用户创建代理档案", "growth_write_forbidden")
    conn = product_db_conn()
    try:
        agent = growth_service.create_agent_profile(
            conn,
            user_id,
            agent_code=str(payload.get("agentCode") or payload.get("agent_code") or ""),
            level=str(payload.get("level") or "standard"),
            status=str(payload.get("status") or "active"),
            settlement_account=payload.get("settlementAccount")
            if isinstance(payload.get("settlementAccount"), dict)
            else {},
            contact=payload.get("contact") if isinstance(payload.get("contact"), dict) else {},
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except growth_service.GrowthServiceError as exc:
        return growth_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "agent": agent})


@app.post("/api/growth/agent-customers")
def api_bind_agent_customer():
    if not admin_write_authorized():
        return forbidden("代理客户归属写接口未授权", "growth_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        relation = growth_service.bind_agent_customer(
            conn,
            agent_id=str(payload.get("agentId") or payload.get("agent_id") or ""),
            customer_id=str(payload.get("customerId") or payload.get("customer_id") or ""),
            source=str(payload.get("source") or "manual"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except growth_service.GrowthServiceError as exc:
        return growth_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "relation": relation})


@app.post("/api/growth/invites/accept")
def api_accept_consumer_invite():
    payload = request.get_json(silent=True) or {}
    explicit_admin_allowed = configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token")
    admin_allowed = explicit_admin_allowed or (admin_write_authorized() and not session_token_from_request())
    auth_context: dict[str, Any] | None = None
    if admin_allowed:
        session_user_id = current_authenticated_user_id()
    else:
        session, error = require_authenticated_session()
        if error:
            return error
        assert session is not None
        session_user_id = str(session["user_id"])
    invitee_user_id = str(payload.get("inviteeUserId") or payload.get("invitee_user_id") or session_user_id)
    if not admin_allowed and invitee_user_id != session_user_id:
        return forbidden("邀请绑定未授权", "growth_write_forbidden")
    conn = product_db_conn()
    try:
        if not admin_allowed:
            auth_context = auth_service.registration_session_context(
                conn,
                user_id=session_user_id,
                session_created_at=str(session["created_at"]),
                session_id=str(session["id"]),
                ip=request_ip(),
                user_agent=request.headers.get("User-Agent", ""),
            )
        invite = growth_service.accept_consumer_invite(
            conn,
            inviter_user_id=str(payload.get("inviterUserId") or payload.get("inviter_user_id") or ""),
            invitee_user_id=invitee_user_id,
            invite_code=str(payload.get("inviteCode") or payload.get("invite_code") or ""),
            agent_id=str(payload.get("agentId") or payload.get("agent_id") or ""),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            phone_verified=payload_bool(payload, "phoneVerified")
            if admin_allowed
            else bool(auth_context["phone_verified"]),
            human_verified=payload_bool(payload, "humanVerified")
            if admin_allowed
            else bool(auth_context["human_verified"]),
            same_device_recent_registrations=int(payload.get("sameDeviceRecentRegistrations") or 0)
            if admin_allowed
            else int(auth_context["same_device_recent_registrations"]),
            same_ip_recent_registrations=int(payload.get("sameIpRecentRegistrations") or 0)
            if admin_allowed
            else int(auth_context["same_ip_recent_registrations"]),
            same_phone_registered=payload_bool(payload, "samePhoneRegistered")
            if admin_allowed
            else bool(auth_context["same_phone_registered"]),
            risk_blocked=payload_bool(payload, "riskBlocked") if admin_allowed else bool(auth_context["risk_blocked"]),
        )
    except (TypeError, ValueError) as exc:
        conn.close()
        return jsonify({"error": str(exc), "code": "invalid_growth_input"}), 400
    except growth_service.GrowthServiceError as exc:
        conn.close()
        return growth_error_response(exc)

    rewards = invite.get("registrationRewards") if isinstance(invite.get("registrationRewards"), dict) else {}
    billing_results: list[dict[str, Any]] = []
    billing_error: dict[str, Any] | None = None
    try:
        inviter_points = int(rewards.get("inviterPoints") or 0)
        invitee_points = int(rewards.get("inviteePoints") or 0)
        if inviter_points > 0:
            billing_results.append(
                billing.credit_account(
                    str(invite["inviterUserId"]),
                    f"referral-register-inviter:{invite['id']}",
                    inviter_points,
                    description="referral-register-inviter",
                    metadata={"inviteId": invite["id"], "inviteeUserId": invite["inviteeUserId"]},
                )
            )
        if invitee_points > 0:
            billing_results.append(
                billing.credit_account(
                    str(invite["inviteeUserId"]),
                    f"referral-register-invitee:{invite['id']}",
                    invitee_points,
                    description="referral-register-invitee",
                    metadata={"inviteId": invite["id"], "inviterUserId": invite["inviterUserId"]},
                )
            )
        if inviter_points > 0 or invitee_points > 0:
            invite = growth_service.mark_invite_reward_granted(conn, str(invite["id"]))
    except billing.BillingError as exc:
        billing_error = exc.to_dict()
    finally:
        conn.close()

    body = {"ok": billing_error is None, "invite": invite, "billing": billing_results}
    if billing_error:
        body["billingError"] = billing_error
        return jsonify(body), 409
    return jsonify(body)


@app.get("/api/growth/withdrawals/balance")
def api_growth_withdrawal_balance():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    requested_agent_id = str(request.args.get("agentId") or request.args.get("agent_id") or "")
    conn = product_db_conn()
    try:
        agent, agent_error = agent_profile_for_session(conn, session, requested_agent_id)
        if agent_error:
            return agent_error
        assert agent is not None
        balance = withdrawal_service.calculate_withdrawable_balance(conn, str(agent["id"]))
    except withdrawal_service.WithdrawalServiceError as exc:
        return withdrawal_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "agentId": agent["id"], "balance": balance})


@app.get("/api/growth/withdrawals")
def api_growth_list_withdrawals():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    requested_agent_id = str(request.args.get("agentId") or request.args.get("agent_id") or "")
    conn = product_db_conn()
    try:
        agent, agent_error = agent_profile_for_session(conn, session, requested_agent_id)
        if agent_error:
            return agent_error
        assert agent is not None
        records = withdrawal_service.list_withdrawal_requests(
            conn,
            agent_id=str(agent["id"]),
            status=str(request.args.get("status") or ""),
            limit=request.args.get("limit") or 50,
        )
    except withdrawal_service.WithdrawalServiceError as exc:
        return withdrawal_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "agentId": agent["id"], "withdrawals": records})


@app.post("/api/growth/withdrawals")
def api_growth_create_withdrawal():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    payload = request.get_json(silent=True) or {}
    requested_agent_id = str(payload.get("agentId") or payload.get("agent_id") or "")
    account_snapshot = payload.get("accountSnapshot")
    if account_snapshot is None:
        account_snapshot = payload.get("account_snapshot")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conn = product_db_conn()
    try:
        agent, agent_error = agent_profile_for_session(conn, session, requested_agent_id)
        if agent_error:
            return agent_error
        assert agent is not None
        record = withdrawal_service.create_withdrawal_request(
            conn,
            agent_id=str(agent["id"]),
            amount_cents=payload.get("amountCents") or payload.get("amount_cents") or 0,
            account_snapshot=account_snapshot if isinstance(account_snapshot, dict) else {},
            metadata={
                **metadata,
                "source": "api",
                "userId": session["user_id"],
                "sessionId": session["id"],
                "ip": request_ip(),
                "userAgent": request.headers.get("User-Agent", ""),
            },
        )
    except withdrawal_service.WithdrawalServiceError as exc:
        return withdrawal_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "agentId": agent["id"], "withdrawal": record}), 201


@app.post("/api/payments/orders")
def api_create_payment_order():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or current_authenticated_user_id())
    provider = requested_payment_provider(payload)
    if provider.lower() == "fake" and not payment_service.fake_payment_provider_enabled(os.environ):
        return payment_error_response(fake_payment_provider_guard_error())
    try:
        if payload.get("cash") is not None:
            cash = int(payload.get("cash") or 0)
            points = billing.points_for_recharge(cash)
            amount_cents = cash * 100
        else:
            amount_cents = int(payload.get("amountCents") or payload.get("amount_cents") or 0)
            points = int(payload.get("points") or 0)
        if amount_cents <= 0 or points <= 0:
            raise payment_service.InvalidPaymentInput("Payment amount and points must be positive")
        conn = product_db_conn()
        try:
            order = payment_service.create_payment_order(
                conn,
                user_id=user_id,
                amount_cents=amount_cents,
                points=points,
                provider=provider,
                order_id=str(payload.get("orderId") or payload.get("order_id") or "").strip() or None,
                idempotency_key=str(payload.get("idempotencyKey") or payload.get("idempotency_key") or "").strip()
                or None,
            )
        finally:
            conn.close()
    except payment_service.PaymentServiceError as exc:
        return payment_error_response(exc)
    except billing.BillingError as exc:
        return billing_json_error(exc)
    except (TypeError, ValueError) as exc:
        return payment_error_response(payment_service.InvalidPaymentInput(str(exc)))

    return jsonify(
        {
            "ok": True,
            "order": order,
            "instructions": payment_service.payment_instructions(order),
        }
    )


@app.post("/api/payments/fake-callback")
def api_fake_payment_callback():
    body = request.get_json(silent=True) or {}
    provider = str(body.get("provider") or "fake")
    if provider.strip().lower() == "fake" and not payment_service.fake_payment_provider_enabled(os.environ):
        return payment_error_response(fake_payment_provider_guard_error(callback=True))
    provider_order_id = str(body.get("providerOrderId") or body.get("provider_order_id") or body.get("orderId") or "")
    event_type = str(body.get("eventType") or body.get("event_type") or "payment_success")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    payload = dict(payload)
    for key in ("signature", "sign", "hmac", "sig", "eventId", "event_id", "status"):
        if body.get(key) is not None and payload.get(key) is None:
            payload[key] = body[key]

    secret = os.environ.get("FAKE_PAYMENT_WEBHOOK_SECRET") or os.environ.get("PAYMENT_WEBHOOK_SECRET") or ""
    if not secret:
        return jsonify({"error": "支付回调签名密钥未配置", "code": "payment_webhook_secret_missing"}), 503
    conn = product_db_conn()
    try:
        result = payment_service.handle_payment_callback(
            conn,
            provider=provider,
            provider_order_id=provider_order_id,
            event_type=event_type,
            payload=payload,
            secret=secret,
        )
    except payment_service.PaymentServiceError as exc:
        return payment_error_response(exc)
    finally:
        conn.close()

    billing_result = None
    billing_error = None
    billing_error_status = 409
    growth_result = None
    order = result["order"]
    user_id = str(order["user_id"])
    try:
        points_to_credit = int(result.get("points_to_credit") or result.get("pointsToCredit") or 0)
        points_to_refund = int(result.get("points_to_refund") or result.get("pointsToRefund") or 0)
        if points_to_credit > 0:
            billing_result = billing.credit_account(
                user_id,
                f"payment:{order['order_id']}",
                points_to_credit,
                description="payment",
                metadata={"provider": provider, "providerOrderId": provider_order_id},
            )
        elif points_to_refund > 0:
            billing_result = billing.debit_account(
                user_id,
                f"payment-refund:{order['order_id']}:{result['event_id']}",
                points_to_refund,
                description="payment-refund",
                metadata={"provider": provider, "providerOrderId": provider_order_id},
            )
    except billing.BillingError as exc:
        billing_error = exc.to_dict()
        billing_error_status = int(getattr(exc, "status_code", 409))

    if billing_error is None:
        event_id = str(result.get("event_id") or result.get("eventId") or "")
        if str(result.get("status") or "") == payment_service.STATUS_PAID:
            growth_result = apply_payment_growth_rewards(order, event_id)
        elif str(result.get("status") or "") == payment_service.STATUS_REFUNDED:
            growth_result = apply_payment_growth_refund(order, payload, event_id)

    body = {
        "ok": billing_error is None,
        "callback": result,
        "billing": billing_result,
        "growth": growth_result,
        "account": account_payload(user_id),
    }
    if billing_error:
        body["billingError"] = billing_error
        return jsonify(body), billing_error_status
    return jsonify(body)


@app.post("/api/objects/sign")
def api_sign_object_access():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or current_authenticated_user_id())
    if not object_write_authorized(user_id):
        return forbidden("对象访问签名未授权", "object_write_forbidden")

    secret = object_access_signing_secret()
    if not secret:
        return jsonify({"error": "对象签名密钥未配置", "code": "object_signing_secret_missing"}), 503

    try:
        access = object_storage_service.create_signed_access(
            object_key=str(payload.get("objectKey") or payload.get("object_key") or ""),
            user_id=user_id,
            purpose=str(payload.get("purpose") or asset_security.PREVIEW),
            variant=str(payload.get("variant") or asset_security.PREVIEW),
            expires_in=float(payload.get("expiresIn") or payload.get("expires_in") or asset_security.DOWNLOAD_TOKEN_TTL_SECONDS),
            secret=secret,
            base_url=str(payload.get("baseUrl") or payload.get("base_url") or "/objects"),
        )
    except (TypeError, ValueError, asset_security.AssetTokenError):
        return jsonify({"error": "对象访问签名请求无效", "code": "invalid_object_access_request"}), 400
    return jsonify({"ok": True, **public_object_access_payload(access)})


@app.post("/api/admin/actions/risk")
def api_admin_record_risk_decision():
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        record = admin_actions.record_risk_decision(
            conn,
            event_type=str(payload.get("eventType") or payload.get("event_type") or ""),
            decision=str(payload.get("decision") or "allow"),
            user_id=str(payload.get("userId") or payload.get("user_id") or ""),
            agent_id=str(payload.get("agentId") or payload.get("agent_id") or ""),
            asset_id=str(payload.get("assetId") or payload.get("asset_id") or ""),
            risk_level=str(payload.get("riskLevel") or payload.get("risk_level") or "info"),
            ip=str(payload.get("ip") or request.remote_addr or ""),
            deny_reason=str(payload.get("denyReason") or payload.get("deny_reason") or ""),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc), "code": "invalid_admin_action"}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "id": record["id"], "record": record})


@app.post("/api/admin/actions/asset-access")
def api_admin_record_asset_access():
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        record = admin_actions.record_asset_access(
            conn,
            asset_id=str(payload.get("assetId") or payload.get("asset_id") or ""),
            action=str(payload.get("action") or ""),
            user_id=str(payload.get("userId") or payload.get("user_id") or ""),
            agent_id=str(payload.get("agentId") or payload.get("agent_id") or ""),
            asset_type=str(payload.get("assetType") or payload.get("asset_type") or ""),
            ip=str(payload.get("ip") or request.remote_addr or ""),
            allowed=bool(payload.get("allowed", True)),
            deny_reason=str(payload.get("denyReason") or payload.get("deny_reason") or ""),
            request_id=str(payload.get("requestId") or payload.get("request_id") or ""),
            user_agent=str(payload.get("userAgent") or payload.get("user_agent") or request.headers.get("User-Agent", "")),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc), "code": "invalid_admin_action"}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "record": record})


@app.post("/api/admin/actions/commissions/<commission_order_id>/status")
def api_admin_update_commission_status(commission_order_id: str):
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        record = admin_actions.update_commission_status(
            conn,
            commission_order_id,
            status=str(payload.get("status") or ""),
            reason=str(payload.get("reason") or ""),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except KeyError as exc:
        return jsonify({"error": str(exc), "code": "commission_not_found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_admin_action"}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "commission": record})


@app.post("/api/admin/actions/commissions/release-eligible")
def api_admin_release_eligible_commissions():
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        result = commission_settlement_service.release_eligible_commissions(
            conn,
            agent_id=str(payload.get("agentId") or payload.get("agent_id") or ""),
            min_age_days=int(payload.get("minAgeDays") or payload.get("min_age_days") or 7),
            now=str(payload.get("now") or "") or None,
            limit=int(payload.get("limit") or 500),
        )
    except commission_settlement_service.CommissionSettlementError as exc:
        return commission_settlement_error_response(exc)
    finally:
        conn.close()
    return jsonify(result)


@app.get("/api/admin/actions/commission-settlements")
def api_admin_list_commission_settlements():
    if not admin_write_authorized():
        return forbidden("管理接口未授权", "admin_write_forbidden")
    conn = product_db_conn()
    try:
        settlements = commission_settlement_service.list_commission_settlements(
            conn,
            agent_id=str(request.args.get("agentId") or request.args.get("agent_id") or ""),
            status=str(request.args.get("status") or ""),
            limit=int(request.args.get("limit") or 50),
        )
    except commission_settlement_service.CommissionSettlementError as exc:
        return commission_settlement_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "settlements": settlements})


@app.post("/api/admin/actions/commission-settlements")
def api_admin_create_commission_settlement():
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    order_ids = payload.get("commissionOrderIds") or payload.get("commission_order_ids")
    if order_ids is not None and not isinstance(order_ids, list):
        return jsonify({"error": "commissionOrderIds must be a list", "code": "invalid_commission_settlement_input"}), 400
    conn = product_db_conn()
    try:
        settlement = commission_settlement_service.create_commission_settlement(
            conn,
            agent_id=str(payload.get("agentId") or payload.get("agent_id") or ""),
            commission_order_ids=[str(item) for item in order_ids] if isinstance(order_ids, list) else None,
            period_start=str(payload.get("periodStart") or payload.get("period_start") or ""),
            period_end=str(payload.get("periodEnd") or payload.get("period_end") or ""),
            settlement_no=str(payload.get("settlementNo") or payload.get("settlement_no") or ""),
            settlement_account=payload.get("settlementAccount")
            if isinstance(payload.get("settlementAccount"), dict)
            else {},
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except commission_settlement_service.CommissionSettlementError as exc:
        return commission_settlement_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "settlement": settlement})


@app.post("/api/admin/actions/commission-settlements/<settlement_id>/status")
def api_admin_update_commission_settlement_status(settlement_id: str):
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        settlement = commission_settlement_service.update_commission_settlement_status(
            conn,
            settlement_id,
            str(payload.get("status") or ""),
            failure_reason=str(payload.get("failureReason") or payload.get("failure_reason") or ""),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            paid_at=str(payload.get("paidAt") or payload.get("paid_at") or "") or None,
        )
    except commission_settlement_service.CommissionSettlementError as exc:
        return commission_settlement_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "settlement": settlement})


@app.post("/api/admin/actions/withdrawals/<withdrawal_id>/status")
def api_admin_update_withdrawal_status(withdrawal_id: str):
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"approved", "rejected", "paid", "canceled"}:
        return jsonify({"error": "invalid withdrawal status", "code": "invalid_withdrawal_input"}), 400
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conn = product_db_conn()
    try:
        record = withdrawal_service.update_withdrawal_request_status(
            conn,
            withdrawal_id,
            status,
            reason=str(payload.get("reason") or ""),
            metadata={
                **metadata,
                "actorUserId": admin_actor_user_id(),
                "source": "admin_api",
            },
        )
    except withdrawal_service.WithdrawalServiceError as exc:
        return withdrawal_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "withdrawal": record})


@app.post("/api/admin/actions/ai-assets/<asset_id>/status")
def api_admin_mark_ai_asset_status(asset_id: str):
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    repo = ai_asset_repository.AIAssetRepository(ai_asset_manifest_path())
    try:
        record = admin_actions.mark_ai_asset_status(repo, asset_id, str(payload.get("status") or ""))
    except KeyError as exc:
        return jsonify({"error": str(exc), "code": "ai_asset_not_found"}), 404
    except (AttributeError, ValueError) as exc:
        return jsonify({"error": str(exc), "code": "invalid_admin_action"}), 400
    return jsonify({"ok": True, "asset": record})


@app.post("/api/admin/actions/audit")
def api_admin_audit_event():
    if not admin_write_authorized():
        return forbidden("管理写接口未授权", "admin_write_forbidden")
    payload = request.get_json(silent=True) or {}
    conn = product_db_conn()
    try:
        record = admin_actions.admin_audit_event(
            conn,
            actor_user_id=str(payload.get("actorUserId") or payload.get("actor_user_id") or admin_actor_user_id()),
            action=str(payload.get("action") or ""),
            target_type=str(payload.get("targetType") or payload.get("target_type") or ""),
            target_id=str(payload.get("targetId") or payload.get("target_id") or ""),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_admin_action"}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, "audit": record})


@app.post("/api/recharge")
def api_recharge():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or current_user_id())
    if not billing_write_authorized() and not local_demo_billing_allowed(user_id):
        return forbidden("计费写接口未授权", "billing_write_forbidden")
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
    if not billing_write_authorized() and not local_demo_billing_allowed(user_id):
        return forbidden("计费写接口未授权", "billing_write_forbidden")
    ensure_demo_balance(user_id)
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
    if not billing_write_authorized() and not local_demo_billing_allowed(user_id):
        return forbidden("计费写接口未授权", "billing_write_forbidden")
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
            "externalDirs": len(configured_library_dirs()),
            "externalMediaEnabled": external_library_media_enabled(),
        }
    )


@app.get("/api/tencent-status")
def api_tencent_status():
    return jsonify(tencent_status_payload())


@app.get("/api/ops/readiness")
def api_ops_readiness():
    object_storage = object_storage_service.assess_object_storage_readiness()
    payments = payment_service.assess_payment_provider_readiness()
    generation_provider = generation_provider_readiness()
    queue = generation_queue.snapshot()
    ready = (
        bool(object_storage.get("ready"))
        and bool(payments.get("ready"))
        and bool(generation_provider.get("ready"))
        and not bool(queue.get("closed"))
    )
    return jsonify(
        {
            "ok": True,
            "ready": ready,
            "objectStorage": object_storage,
            "payments": payments,
            "generationProvider": generation_provider,
            "generationQueue": queue,
        }
    )


@app.get("/api/admin/queue-snapshot")
def api_admin_queue_snapshot():
    return jsonify({"ok": True, "queue": generation_queue.snapshot()})


@app.get("/api/ai-asset-plan")
def api_ai_asset_plan():
    return jsonify(
        {
            "plan": AI_ASSET_LIBRARY_PLAN,
            "assetSchemaVersion": AI_ASSET_SCHEMA_VERSION,
            "stats": ai_asset_library_stats(),
            "storagePrefixes": {
                "backgrounds": "ai-assets/backgrounds/{category}/{styleId}/",
                "products": "ai-assets/products/{category}/{styleId}/",
            },
            "matchPolicy": {
                "reuse": "未来复用时先按 category + normalizedProductName + matchNames + keywords 匹配 AI 产品资产。",
                "regenerate": "低置信度、菜名冲突、图片审核未通过时继续调用混元生成并沉淀新资产。",
            },
        }
    )


GENERATION_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")
GENERATION_JOB_TIMEOUT_ERROR = "generation job timed out"


def validate_generation_job_id(value: str) -> str:
    job_id = str(value or "").strip()
    if not GENERATION_JOB_ID_RE.fullmatch(job_id):
        raise ValueError("任务 ID 格式无效")
    return job_id


def generation_job_id(style: str, quality: str, payload: dict[str, Any]) -> str:
    provided = payload.get("jobId") or payload.get("job_id") or payload.get("idempotencyKey") or payload.get("idempotency_key")
    if provided:
        return validate_generation_job_id(str(provided))

    menu_path = current_menu_path()
    if menu_path is None:
        menu_identity: dict[str, Any] = {"demo": True, "file": "demo_menu.xlsx"}
    else:
        try:
            stat = menu_path.stat()
            menu_identity = {"file": menu_path.name, "mtimeNs": stat.st_mtime_ns, "size": stat.st_size}
        except OSError:
            menu_identity = {"file": menu_path.name}

    basis = {
        "userId": current_user_id(),
        "menu": menu_identity,
        "style": style,
        "quality": quality_config(quality)["id"],
    }
    digest = hashlib.sha1(json.dumps(basis, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"generation-{digest}"


def run_generation_job(style: str, quality: str) -> dict[str, Any]:
    plan = build_plan(style, quality)
    plan["generation"] = materialize_final_images(plan, style, quality)
    return public_plan_payload(plan)


def refresh_generation_timeouts() -> None:
    try:
        generation_queue.fail_timed_out(error=GENERATION_JOB_TIMEOUT_ERROR)
    except Exception:
        return None
    return None


def generation_job_payload(job: Any) -> dict[str, Any]:
    data = job.to_dict()
    try:
        timing = job.timing(limits=generation_queue.limits)
    except Exception:
        timing = {}
    timeout_error = data["error"] == GENERATION_JOB_TIMEOUT_ERROR
    timed_out = bool(timing.get("timed_out")) or timeout_error
    timing_reason = "timeout" if timeout_error else timing.get("reason")
    return {
        "jobId": data["id"],
        "status": data["status"],
        "requested": data["requested"],
        "pending": data["pending"],
        "completed": data["completed"],
        "failed": data["failed"],
        "canceled": data["canceled"],
        "result": data["result"],
        "error": data["error"],
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
        "startedAt": data["started_at"],
        "finishedAt": data["finished_at"],
        "completedAt": data["completed_at"],
        "elapsed": data["elapsed"],
        "elapsedSeconds": data["elapsed_seconds"],
        "stale": bool(timing.get("stale")) and not timeout_error,
        "timedOut": timed_out,
        "timingReason": timing_reason,
        "ageSeconds": timing.get("age_seconds"),
        "inactiveSeconds": timing.get("inactive_seconds"),
        "staleAfterSeconds": timing.get("stale_after_seconds"),
        "timeoutSeconds": timing.get("timeout_seconds"),
    }


def generation_job_write_allowed() -> bool:
    return not tencent_ready() or generation_write_authorized() or local_demo_generation_allowed()


def generation_queue_error_response(exc: RuntimeError):
    reason = str(exc).strip()
    normalized = reason.lower()
    if "max_pending_jobs_exceeded" in normalized or "queue full" in normalized or "admission denied" in normalized:
        return (
            jsonify(
                {
                    "error": "生成队列已满，请稍后重试。",
                    "code": "generation_queue_full",
                    "reason": reason,
                }
            ),
            429,
        )
    return (
        jsonify(
            {
                "error": "生成队列暂不可用，请稍后重试。",
                "code": "generation_queue_unavailable",
                "reason": reason,
            }
        ),
        503,
    )


@app.post("/api/generation-jobs")
def api_generation_jobs():
    payload = request.get_json(silent=True) or {}
    try:
        style = validate_requested_style(str(payload.get("style") or ""))
        quality = quality_config(str(payload.get("quality") or "standard"))["id"]
        job_id = generation_job_id(style, quality, payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not tencent_ready() and not local_final_fallback_enabled():
        return jsonify({"error": "混元未配置，已停止正式出图，避免生成错误图片或误扣积分。"}), 503
    if not generation_job_write_allowed():
        return forbidden("正式生成接口未授权", "generation_write_forbidden")
    refresh_generation_timeouts()
    try:
        job = generation_queue.enqueue(
            job_id,
            run_generation_job,
            style,
            quality,
            requested=1,
            metadata={"style": style, "quality": quality, "userId": current_user_id()},
        )
    except RuntimeError as exc:
        return generation_queue_error_response(exc)
    return jsonify({"jobId": job.job_id, "status": job.status})


@app.get("/api/generation-jobs/<job_id>")
def api_generation_job(job_id: str):
    if not generation_job_write_allowed():
        return forbidden("正式生成接口未授权", "generation_write_forbidden")
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    refresh_generation_timeouts()
    job = generation_queue.get(normalized_job_id)
    if job is None:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(generation_job_payload(job))


@app.post("/api/generation-jobs/<job_id>/cancel")
def api_cancel_generation_job(job_id: str):
    if not generation_job_write_allowed():
        return forbidden("正式生成接口未授权", "generation_write_forbidden")
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    refresh_generation_timeouts()
    job = generation_queue.get(normalized_job_id)
    if job is None:
        return jsonify({"error": "任务不存在"}), 404

    if job.status in {"completed", "failed"}:
        return (
            jsonify(
                {
                    "error": "任务已结束，不能取消。",
                    "code": "generation_job_already_finished",
                    "job": generation_job_payload(job),
                }
            ),
            409,
        )
    if job.status == "canceled":
        return jsonify(generation_job_payload(job))

    try:
        canceled = generation_queue.cancel(normalized_job_id, error="user canceled")
    except KeyError:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(generation_job_payload(canceled))


@app.post("/api/generate-final")
def api_generate_final():
    payload = request.get_json(silent=True) or {}
    try:
        style = validate_requested_style(str(payload.get("style") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    quality = str(payload.get("quality") or "standard")
    if not tencent_ready() and not local_final_fallback_enabled():
        return jsonify({"error": "混元未配置，已停止正式出图，避免生成错误图片或误扣积分。"}), 503
    if tencent_ready() and not generation_write_authorized() and not local_demo_generation_allowed():
        return forbidden("正式生成接口未授权", "generation_write_forbidden")
    plan = build_plan(style, quality)
    plan["generation"] = materialize_final_images(plan, style, quality)
    return jsonify(public_plan_payload(plan))


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
    return jsonify({"ok": True, "plan": public_plan_payload(build_plan())})


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
    try:
        style = validate_requested_style(str(payload.get("style", "")), allow_empty=True)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    plan = build_plan(style, quality)
    export_results = prepare_results_for_export(plan["results"], style)
    return jsonify(
        public_export_payload(
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


@app.get("/objects/<path:object_key>")
def object_access(object_key: str):
    secret = object_access_signing_secret()
    if not secret:
        record_asset_access_audit(
            asset_id=object_key,
            action="object_access",
            asset_type="object",
            allowed=False,
            deny_reason="missing_secret",
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象签名密钥未配置", "code": "object_signing_secret_missing"}), 503

    token = str(request.args.get("token") or "")
    if not token:
        record_asset_access_audit(
            asset_id=object_key,
            action="object_access",
            asset_type="object",
            allowed=False,
            deny_reason="missing_token",
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象访问未授权", "code": "object_access_forbidden", "reason": "missing_token"}), 403

    try:
        payload = object_storage_service.verify_signed_access(token, secret)
        requested_key = object_storage_service.validate_object_key(object_key)
    except (TypeError, ValueError, asset_security.AssetTokenError) as exc:
        reason = type(exc).__name__
        record_asset_access_audit(
            asset_id=object_key,
            action="object_access",
            asset_type="object",
            allowed=False,
            deny_reason=reason,
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象访问未授权", "code": "object_access_forbidden", "reason": reason}), 403

    if payload["object_key"] != requested_key:
        record_asset_access_audit(
            asset_id=requested_key,
            action=asset_action_for_purpose(str(payload.get("purpose") or ""), "object_access"),
            user_id=str(payload.get("user_id") or ""),
            asset_type="object",
            allowed=False,
            deny_reason="object_key_mismatch",
            metadata={"route": "objects", "tokenObjectKey": payload["object_key"]},
        )
        return jsonify({"error": "对象访问未授权", "code": "object_access_forbidden", "reason": "object_key_mismatch"}), 403

    storage = object_storage_service.get_object_storage_service()
    try:
        path = storage.path_for_key(requested_key)
    except (TypeError, ValueError):
        record_asset_access_audit(
            asset_id=requested_key,
            action=asset_action_for_purpose(str(payload.get("purpose") or ""), "object_access"),
            user_id=str(payload.get("user_id") or ""),
            asset_type="object",
            allowed=False,
            deny_reason="object_not_found",
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象不存在", "code": "object_not_found"}), 404
    if not path.is_file():
        record_asset_access_audit(
            asset_id=requested_key,
            action=asset_action_for_purpose(str(payload.get("purpose") or ""), "object_access"),
            user_id=str(payload.get("user_id") or ""),
            asset_type="object",
            allowed=False,
            deny_reason="object_not_found",
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象不存在", "code": "object_not_found"}), 404
    record_asset_access_audit(
        asset_id=requested_key,
        action=asset_action_for_purpose(str(payload.get("purpose") or ""), "object_access"),
        user_id=str(payload.get("user_id") or ""),
        asset_type="object",
        allowed=True,
        metadata={"route": "objects", "purpose": payload.get("purpose"), "variant": payload.get("variant")},
    )
    return send_file(path)


@app.get("/download/<path:name>")
def download(name: str):
    relative_name = safe_export_download_name(name)
    if relative_name is None:
        record_asset_access_audit(
            asset_id=str(name or ""),
            action="export",
            user_id=current_user_id(),
            asset_type="export",
            allowed=False,
            deny_reason="asset_not_found",
            metadata={"route": "download"},
        )
        return jsonify({"error": "文件不存在"}), 404

    signing_secret = download_signing_secret()
    if not signing_secret and not is_local_request():
        record_asset_access_audit(
            asset_id=relative_name,
            action="export",
            user_id=current_user_id(),
            asset_type="export",
            allowed=False,
            deny_reason="missing_secret",
            metadata={"route": "download"},
        )
        return jsonify({"error": "下载签名密钥未配置", "code": "download_signing_secret_missing"}), 503
    if signing_secret:
        user_id = current_user_id()
        decision = download_guard.authorize_download(
            asset_record={
                "asset_id": relative_name,
                "user_id": user_id,
                "allowed_purposes": (asset_security.EXPORT,),
                "available_variants": (asset_security.EXPORT,),
            },
            user_context={"user_id": user_id},
            purpose=asset_security.EXPORT,
            variant=asset_security.EXPORT,
            token=request.args.get("token"),
            secret=signing_secret,
            audit_metadata={"route": "download"},
        )
        if not decision["allowed"]:
            record_asset_access_audit(
                asset_id=relative_name,
                action=str(decision.get("action") or "export"),
                user_id=user_id,
                asset_type="export",
                allowed=False,
                deny_reason=str(decision["reason"]),
                metadata=decision.get("audit") if isinstance(decision.get("audit"), dict) else {"route": "download"},
            )
            return jsonify({"error": "下载未授权", "code": "download_forbidden", "reason": decision["reason"]}), 403
        record_asset_access_audit(
            asset_id=relative_name,
            action=str(decision.get("action") or "export"),
            user_id=user_id,
            asset_type="export",
            allowed=True,
            metadata=decision.get("audit") if isinstance(decision.get("audit"), dict) else {"route": "download"},
        )
    else:
        record_asset_access_audit(
            asset_id=relative_name,
            action="export",
            user_id=current_user_id(),
            asset_type="export",
            allowed=True,
            metadata={"route": "download", "localDemo": True},
        )

    return send_from_directory(EXPORT_DIR, relative_name, as_attachment=True)


def download_signing_secret() -> str:
    for env_name in ("DOWNLOAD_SIGNING_SECRET", "ASSET_SIGNING_SECRET"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def safe_export_download_name(name: str) -> str | None:
    try:
        export_root = EXPORT_DIR.resolve()
        target = (export_root / name).resolve()
        relative = target.relative_to(export_root)
    except (OSError, ValueError):
        return None
    if not target.is_file():
        return None
    return relative.as_posix()


app.register_blueprint(
    create_admin_blueprint(
        AdminDependencies(
            library_images=library_images,
            media_url_for_path=media_url_for_path,
            current_menu_path=current_menu_path,
            parse_menu=parse_menu,
            upload_dir=UPLOAD_DIR,
            db_path=storage_db.DEFAULT_DB_PATH,
            ai_asset_manifest_path=ai_asset_manifest_path(),
        )
    )
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port)
