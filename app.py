from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import hmac
import http.client
import ipaddress
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import threading
import time
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import admin_actions
import ai_asset_repository
import auth_rules
import auth_service
import background_catalog
import background_profiles
import billing
import asset_security
from chroma_foreground import (
    ChromaExtractionError,
    EXTRACTION_VERSION as CHROMA_EXTRACTION_VERSION,
    extract_chroma_mask,
)
import commission_settlement_service
import download_guard
import growth_rules
import growth_service
import object_storage_service
import pandas as pd
import payment_service
import sms_service
import storage_db
import withdrawal_service
from flask import (
    Flask,
    Response,
    after_this_request,
    has_request_context,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from shared import (
    json_limits,
    payment_catalog,
    product_admin_read_store,
    product_admin_security_store,
    product_asset_library_store,
    product_auth_store,
    product_export_store,
    product_finance_store,
    product_growth_outbox,
    product_growth_store,
    product_library_import_store,
    product_otp_store,
    product_payment_store,
)
from admin_panel import (
    ADMIN_AI_ASSETS_READ_SCOPE,
    ADMIN_AI_ASSETS_WRITE_SCOPE,
    ADMIN_FINANCE_READ_SCOPE,
    ADMIN_READ_SCOPE,
    ADMIN_RISK_READ_SCOPE,
    AdminAuthorization,
    AdminDependencies,
    create_admin_blueprint,
)
from background_compositor import (
    CompositionError,
    compose_selected_background,
    outside_mask_pixels_equal,
)
from generation_queue import InMemoryGenerationQueue
from image_pipeline import PLATFORMS, assess_generated_asset_quality, export_delivery_zip
from image_edit_provider import gemini_image_edit_readiness
from matching_engine import (
    TAXONOMY_COMBO,
    TAXONOMY_LABELS,
    TAXONOMY_UNKNOWN,
    TAXONOMY_VERSION,
    classify_taxonomy,
    classify_kind as engine_classify_kind,
    grams as engine_grams,
    normalize_dish,
    similarity as engine_similarity,
    split_components as engine_split_components,
    taxonomy_label,
)
from menu_parser import parse_menu as parse_excel_menu
from shared.batch_contract import (
    BatchContractError,
    JOB_TYPE as BATCH_JOB_TYPE,
    SCHEMA_VERSION as BATCH_CONTRACT_SCHEMA_VERSION,
    canonical_json,
    freeze_menu_batch_contract,
)
from shared.menu_upload_store import (
    InvalidMenuUploadInput,
    MenuUploadNotFound as PostgresMenuUploadNotFound,
    MenuUploadStore,
    MenuUploadStoreError,
)
from shared.postgres_runtime import (
    PostgresRuntimeError,
    postgres_connection,
)
from shared.product_generation_settlement import (
    ProductGenerationSettlementError,
    apply_generation_completion,
    completion_from_redis_task,
    settle_terminal_job,
)
from shared.product_job_store import (
    CancellationRequested as PostgresCancellationRequested,
    FenceMismatch,
    InsufficientPointBalance,
    InvalidProductJobInput,
    JobNotFound as PostgresJobNotFound,
    JobStateConflict,
    PointOrderConflict,
    ProductJobStore,
    ProductJobStoreError,
    RequestDigestConflict,
    RevisionJobCandidate,
    SettlementConflict,
    WalletIntegrityError,
)
from shared.product_revision_settlement import (
    revision_completion_from_redis_task,
)
from shared.refinement_contract import (
    JOB_TYPE as REVISION_JOB_TYPE,
    RefinementContractError,
    freeze_revision_batch_contract,
    public_revision_payload,
    revision_request_sha256,
)
from shared.redis_queue import (
    IdempotencyConflict as RedisIdempotencyConflict,
    QueueError as RedisQueueError,
    TaskNotFound as RedisTaskNotFound,
    product_queue_from_env as redis_product_queue_from_env,
)


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
REMOTE_IMAGE_DOWNLOAD_MAX_ATTEMPTS = 3
REMOTE_IMAGE_DOWNLOAD_RETRY_STATUS_CODES = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)
REMOTE_IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS = 0.25
REMOTE_IMAGE_DOWNLOAD_TOTAL_TIMEOUT_SECONDS = max(
    5,
    env_int("REMOTE_IMAGE_DOWNLOAD_TOTAL_TIMEOUT_SECONDS", 150),
)
REMOTE_IMAGE_ALLOWED_HOST_SUFFIXES = tuple(
    suffix.strip().lower().lstrip(".")
    for suffix in os.environ.get(
        "REMOTE_IMAGE_ALLOWED_HOST_SUFFIXES",
        "myqcloud.com",
    ).split(",")
    if suffix.strip()
)
FINAL_GENERATION_WORKERS = max(1, env_int("FINAL_GENERATION_WORKERS", 3))
GENERATION_QUEUE_STALE_AFTER_SECONDS = max(
    1,
    env_int("GENERATION_QUEUE_STALE_AFTER_SECONDS", 5 * 60),
)
GENERATION_QUEUE_TIMEOUT_SECONDS = max(
    GENERATION_QUEUE_STALE_AFTER_SECONDS,
    env_int("GENERATION_QUEUE_TIMEOUT_SECONDS", 30 * 60),
)
DEFAULT_TENCENT_COS_BUCKET = "waimai-image-tool-inputs-1311836560"
DEFAULT_TENCENT_COS_REGION = "ap-guangzhou"
AI_ASSET_SCHEMA_VERSION = 1
AI_ASSET_MANIFEST_NAME = "manifest.jsonl"
MENU_PARSER_VERSION = 1
MENU_UPLOAD_PRIVATE_METADATA_KEY = "_server"
STYLE_BACKGROUND_PROMPT_VERSION = 11
DISH_GENERATION_PROMPT_VERSION = 2
EXACT_BACKGROUND_PIPELINE_VERSION = 4
CHROMA_FOREGROUND_PROMPT_VERSION = 2
CHROMA_FOREGROUND_PROMPT_MAX_CHARS = 600
EXACT_BACKGROUND_MASK_CACHE_VERSION = 1
STAGING_E2E_INSTANCE_NONCE_PATH = Path(
    "/tmp/waimai-staging-e2e-instance-nonce"
)
AI_ASSET_MANIFEST_LOCK = threading.Lock()
TENCENT_TOKENHUB_GENERATION_LOCK = threading.Lock()
TENCENT_MASK_EXTRACTION_LOCK = threading.Lock()
EXACT_FOREGROUND_CACHE_LOCKS = tuple(threading.Lock() for _ in range(64))
GENERATION_BATCH_SUBMIT_LOCK = threading.Lock()
REVISION_BATCH_SUBMIT_LOCK = threading.Lock()
LOCAL_ASSET_NONCE_CONSUMER = download_guard.InMemoryNonceConsumer()
ADMIN_ROLE_VALUES = {"admin", "super_admin", "superadmin", "ops", "operator"}
ADMIN_FINANCE_ROLE_VALUES = {"admin", "super_admin", "superadmin", "owner", "finance", "financial", "accountant"}
ADMIN_WITHDRAWAL_REVIEW_ROLE_VALUES = ADMIN_ROLE_VALUES | ADMIN_FINANCE_ROLE_VALUES | {"support", "reviewer"}
ADMIN_AI_ASSET_REVIEW_ROLE_VALUES = ADMIN_ROLE_VALUES | {"reviewer", "quality_reviewer", "qa", "support"}
ADMIN_AI_ASSET_DISABLE_ROLE_VALUES = {"admin", "super_admin", "superadmin", "owner"}
ADMIN_RISK_REVIEW_ROLE_VALUES = ADMIN_ROLE_VALUES | {"risk", "risk_reviewer", "risk_admin", "fraud", "security", "support", "reviewer"}
ADMIN_RISK_DENY_ROLE_VALUES = {"admin", "super_admin", "superadmin", "owner", "risk", "risk_admin", "risk_manager", "fraud", "security"}
MAX_LOGO_DATA_URL_CHARS = 1_500_000
MAX_LOGO_BYTES = 1_000_000
MAX_LOGO_PIXELS = 2_000_000
MAX_EXPORT_IMAGE_BYTES = 25 * 1024 * 1024
MAX_EXPORT_IMAGE_PIXELS = 24_000_000
MAX_EXPORT_IMAGE_SIDE = 12_000
MAX_AI_ASSET_BYTES = max(
    1 * 1024 * 1024,
    min(env_int("MAX_AI_ASSET_BYTES", 25 * 1024 * 1024), 512 * 1024 * 1024),
)
MAX_AI_ASSET_BASE64_CHARS = ((MAX_AI_ASSET_BYTES + 2) // 3) * 4 + 256
MAX_GENERATION_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_REVISION_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_REVISION_IMAGE_BYTES = 30 * 1024 * 1024
MAX_PROVIDER_JSON_BYTES = MAX_AI_ASSET_BASE64_CHARS + (2 * 1024 * 1024)
MAX_PREVIEW_METADATA_BYTES = 1 * 1024 * 1024
MAX_MENU_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_LIBRARY_ZIP_BYTES = 100 * 1024 * 1024
MAX_LIBRARY_ZIP_ENTRIES = 200
MAX_LIBRARY_ZIP_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_LIBRARY_ZIP_COMPRESSION_RATIO = 100
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
generation_queue = InMemoryGenerationQueue(
    worker_count=FINAL_GENERATION_WORKERS,
    stale_after_seconds=GENERATION_QUEUE_STALE_AFTER_SECONDS,
    timeout_seconds=GENERATION_QUEUE_TIMEOUT_SECONDS,
)
ASSET_VERSION = os.environ.get("ASSET_VERSION") or str(
    int(max((BASE_DIR / "static" / "app.js").stat().st_mtime, (BASE_DIR / "static" / "styles.css").stat().st_mtime))
)
ACTIVE_MENU_PATH: ContextVar[Path | None] = ContextVar("active_menu_path", default=None)
ACTIVE_PREVIEW_PRINCIPAL: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_preview_principal",
    default=None,
)
ACTIVE_PREVIEW_MENU_UPLOAD_ID: ContextVar[str] = ContextVar(
    "active_preview_menu_upload_id",
    default="",
)
ACTIVE_ASSET_OWNER_USER_ID: ContextVar[str] = ContextVar(
    "active_asset_owner_user_id",
    default="",
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


class SelectedBackgroundError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderResultDownloadError(RuntimeError):
    """A paid provider result exists, but its image could not be retrieved."""


class MenuUploadError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class PreviewObjectStorageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SelectedBackgroundAsset:
    asset_id: str
    menu_key: str
    style_id: str
    sha256: str
    path: Path
    width: int
    height: int
    library_asset_id: str = ""

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "assetId": self.asset_id,
            "menuKey": self.menu_key,
            "styleId": self.style_id,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
        }
        if self.library_asset_id:
            payload["libraryAssetId"] = self.library_asset_id
        return payload


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


def postgres_product_runtime_enabled() -> bool:
    app_env = str(os.environ.get("APP_ENV") or "").strip().lower()
    live_runtime = app_env in {"production", "prod", "staging", "render"}
    if not app_env:
        live_runtime = bool(
            any(
                str(os.environ.get(name) or "").strip()
                for name in (
                    "RENDER",
                    "RENDER_SERVICE_ID",
                    "RENDER_EXTERNAL_URL",
                )
            )
            or ".onrender.com"
            in str(os.environ.get("PUBLIC_BASE_URL") or "").lower()
        )
    testing_sqlite_override = bool(
        app.config.get("TESTING")
        and env_truthy(
            "ALLOW_SQLITE_PRODUCT_RUNTIME_FOR_TESTS",
            default=False,
        )
    )
    if live_runtime and not testing_sqlite_override:
        return True

    database_url = str(os.environ.get("DATABASE_URL") or "").strip().lower()
    if not database_url.startswith(("postgres://", "postgresql://")):
        return False
    return env_truthy("PRODUCT_POSTGRES_ENABLED", default=False)


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


def approved_background_catalog_enabled() -> bool:
    return env_truthy("BACKGROUND_CATALOG_APPROVED_ONLY", default=False)


def background_catalog_manifest_backend() -> str:
    return str(
        os.environ.get("BACKGROUND_CATALOG_MANIFEST_BACKEND") or "postgres"
    ).strip().lower()


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


def staging_test_access_configured() -> bool:
    return bool(
        str(os.environ.get("APP_ENV") or "").strip().lower()
        == "staging-demo"
        and str(os.environ.get("STAGING_BASIC_AUTH_USER") or "").strip()
        and str(os.environ.get("STAGING_BASIC_AUTH_PASSWORD") or "").strip()
    )


def staging_test_access_allowed() -> bool:
    if not staging_test_access_configured() or not has_request_context():
        return False
    authorization = request.authorization
    if authorization is None or str(authorization.type or "").lower() != "basic":
        return False
    expected_user = str(os.environ.get("STAGING_BASIC_AUTH_USER") or "").strip()
    expected_password = str(
        os.environ.get("STAGING_BASIC_AUTH_PASSWORD") or ""
    ).strip()
    return hmac.compare_digest(
        str(authorization.username or ""),
        expected_user,
    ) and hmac.compare_digest(
        str(authorization.password or ""),
        expected_password,
    )


def staging_in_process_generation_allowed() -> bool:
    return (
        staging_test_access_configured()
        and env_truthy(
            "ALLOW_STAGING_IN_PROCESS_GENERATION",
            default=False,
        )
    )


@app.before_request
def require_staging_test_access():
    if request.path == "/healthz":
        return None
    if not staging_test_access_configured() or staging_test_access_allowed():
        return None
    return Response(
        "Staging access required",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Waimai Image Tool Staging"'},
    )


@app.get("/api/staging-e2e-instance")
def api_staging_e2e_instance():
    if not staging_in_process_generation_allowed():
        return jsonify({"error": "not found"}), 404
    try:
        nonce = STAGING_E2E_INSTANCE_NONCE_PATH.read_text(
            encoding="ascii"
        ).strip()
    except OSError:
        nonce = ""
    supplied = str(
        request.headers.get("X-Waimai-Staging-Instance") or ""
    ).strip()
    if not (
        re.fullmatch(r"[0-9a-f]{64}", nonce)
        and hmac.compare_digest(supplied, nonce)
    ):
        return jsonify({"instanceMatched": False}), 409
    return jsonify({"instanceMatched": True})


def local_demo_auth_allowed() -> bool:
    return (
        not postgres_product_runtime_enabled()
        and env_truthy("ENABLE_LOCAL_DEMO_AUTH", default=True)
        and (is_local_request() or staging_test_access_allowed())
    )


def billing_write_authorized() -> bool:
    return configured_request_token(("BILLING_API_TOKEN", "ADMIN_API_TOKEN"), "X-Billing-Token")


def billing_token_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in ("BILLING_API_TOKEN", "ADMIN_API_TOKEN"))


def local_demo_billing_allowed(user_id: str) -> bool:
    return (
        env_truthy("ENABLE_LOCAL_DEMO_BILLING", default=True)
        and not postgres_product_runtime_enabled()
        and not billing_token_configured()
        and user_id == billing.DEFAULT_USER_ID
        and (
            not has_request_context()
            or is_local_request()
            or staging_test_access_allowed()
        )
    )


def local_demo_admin_allowed() -> bool:
    app_env = runtime_environment_label()
    if app_env in {"production", "prod", "staging", "render"} or render_runtime_detected():
        return False
    return (
        env_truthy("ENABLE_LOCAL_DEMO_ADMIN", default=True)
        and not os.environ.get("ADMIN_API_TOKEN", "").strip()
        and is_local_request()
    )


def generation_write_authorized() -> bool:
    return configured_request_token(("GENERATION_API_TOKEN", "ADMIN_API_TOKEN"), "X-Generation-Token")


def generation_token_configured() -> bool:
    return any(os.environ.get(name, "").strip() for name in ("GENERATION_API_TOKEN", "ADMIN_API_TOKEN"))


def local_demo_generation_allowed() -> bool:
    return (
        env_truthy("ENABLE_LOCAL_DEMO_GENERATION", default=False)
        and not generation_token_configured()
        and (is_local_request() or staging_test_access_allowed())
    )


def forbidden(message: str, code: str = "forbidden"):
    return jsonify({"error": message, "code": code}), 403


class LiveSQLiteAccessForbidden(RuntimeError):
    pass


@app.errorhandler(LiveSQLiteAccessForbidden)
def live_sqlite_access_forbidden_response(
    _exc: LiveSQLiteAccessForbidden,
):
    return jsonify(
        {
            "error": "正式环境持久化服务暂时不可用",
            "code": "live_sqlite_access_forbidden",
        }
    ), 503


def product_db_conn(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    if postgres_product_runtime_enabled():
        raise LiveSQLiteAccessForbidden(
            "SQLite product persistence is disabled in live runtime"
        )
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


def product_auth_error_response(exc: Exception):
    if isinstance(
        exc,
        (
            PostgresRuntimeError,
            product_auth_store.ProductAuthConfigurationError,
            product_otp_store.ProductOtpConfigurationError,
            product_otp_store.ProductOtpUnavailable,
        ),
    ):
        code = str(getattr(exc, "code", "") or "auth_runtime_unavailable")
        return jsonify(
            {
                "error": "认证服务暂时不可用",
                "code": code,
            }
        ), 503
    if isinstance(exc, product_otp_store.ProductOtpError):
        status = {
            product_otp_store.ERR_OTP_RATE_LIMITED: 429,
            product_otp_store.ERR_OTP_SEND_COOLDOWN: 429,
            product_otp_store.ERR_OTP_ATTEMPT_LIMITED: 429,
            product_otp_store.ERR_CHALLENGE_NOT_FOUND: 404,
        }.get(exc.code, 400)
        body: dict[str, Any] = {
            "error": str(exc),
            "code": exc.code,
        }
        if exc.retry_after_seconds > 0:
            body["retryAfterSeconds"] = exc.retry_after_seconds
        if exc.attempts > 0:
            body["attempts"] = exc.attempts
        return jsonify(body), status
    if isinstance(exc, product_auth_store.AuthUserUnavailable):
        return jsonify(
            {
                "error": "用户不存在或已停用",
                "code": "user_not_found",
            }
        ), 404
    if isinstance(
        exc,
        (
            product_auth_store.InvalidProductAuthInput,
            product_otp_store.InvalidProductOtpInput,
        ),
    ):
        message = str(exc)
        code = (
            auth_service.ERR_STORE_NAME_REQUIRED
            if message == "store name is required"
            else "invalid_auth_input"
        )
        return jsonify({"error": message, "code": code}), 400
    return jsonify(
        {
            "error": "认证服务暂时不可用",
            "code": "auth_runtime_unavailable",
        }
    ), 503


def close_product_otp_client(store: product_otp_store.ProductOtpStore | None) -> None:
    if store is None:
        return
    close = getattr(store.redis, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def resolve_auth_session(token: str) -> dict[str, Any] | None:
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                store = product_auth_store.product_auth_store_from_env(
                    connection
                )
                return store.resolve_session(token=token)
        except product_auth_store.InvalidProductAuthInput:
            return None

    conn = product_db_conn()
    try:
        return auth_service.get_session(conn, token)
    finally:
        conn.close()


def list_auth_user_stores(user_id: str) -> list[dict[str, Any]]:
    if postgres_product_runtime_enabled():
        with postgres_connection() as connection:
            store = product_auth_store.product_auth_store_from_env(connection)
            return store.list_user_stores(user_id=user_id)

    conn = product_db_conn()
    try:
        return auth_service.list_user_stores(conn, user_id)
    finally:
        conn.close()


def create_auth_user_store(user_id: str, name: str) -> dict[str, Any]:
    if postgres_product_runtime_enabled():
        with postgres_connection() as connection:
            store = product_auth_store.product_auth_store_from_env(connection)
            return store.create_store(user_id=user_id, name=name)

    conn = product_db_conn()
    try:
        return auth_service.create_store(conn, user_id, name)
    finally:
        conn.close()


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


def product_payment_error_response(
    exc: product_payment_store.ProductPaymentStoreError | PostgresRuntimeError,
):
    if isinstance(exc, PostgresRuntimeError):
        return jsonify(
            {
                "error": "PostgreSQL payment store is temporarily unavailable",
                "code": "postgres_payment_store_unavailable",
            }
        ), 503
    status = 500
    code = "product_payment_store_error"
    details: dict[str, Any] = {}
    if isinstance(exc, product_payment_store.InvalidProductPaymentInput):
        status = 400
        code = "invalid_payment_input"
    elif isinstance(exc, product_payment_store.ProductPaymentOrderNotFound):
        status = 404
        code = "payment_order_not_found"
    elif isinstance(exc, product_payment_store.ProductPaymentAmountMismatch):
        status = 400
        code = "payment_amount_mismatch"
        details = {
            "expectedAmountCents": exc.expected_amount_cents,
            "actualAmountCents": exc.actual_amount_cents,
        }
    elif isinstance(
        exc,
        product_payment_store.InsufficientPaymentRefundBalance,
    ):
        status = 409
        code = "insufficient_payment_refund_balance"
        details = {
            "availablePoints": exc.available_points,
            "requiredPoints": exc.required_points,
        }
    elif isinstance(
        exc,
        (
            product_payment_store.ProductPaymentOrderConflict,
            product_payment_store.ProductPaymentEventConflict,
            product_payment_store.ProductPaymentStateConflict,
            product_payment_store.ProductPaymentWalletIntegrityError,
        ),
    ):
        status = 409
        code = "payment_order_conflict"
    return jsonify({"error": str(exc), "code": code, **details}), status


def postgres_payment_runtime_enabled(provider: str) -> bool:
    return (
        str(provider or "").strip().lower() != "fake"
        and postgres_product_runtime_enabled()
    )


def product_payment_order_payload(
    order: dict[str, Any],
    *,
    idempotent: bool,
) -> dict[str, Any]:
    provider_payload = {
        "paymentCatalogSnapshot": dict(order.get("catalog_snapshot") or {}),
        "callbackAmountRequired": True,
        **dict(order.get("provider_payload") or {}),
    }
    payload = {
        "ok": True,
        "idempotent": bool(idempotent),
        "order_id": str(order["id"]),
        "orderId": str(order["id"]),
        "user_id": str(order["owner_user_id"]),
        "userId": str(order["owner_user_id"]),
        "provider": str(order["provider"]),
        "provider_order_id": str(order["provider_order_id"]),
        "providerOrderId": str(order["provider_order_id"]),
        "amount_cents": int(order["amount_cents"]),
        "amountCents": int(order["amount_cents"]),
        "points": int(order["points"]),
        "status": str(order["status"]),
        "idempotency_key": str(order["idempotency_key"]),
        "idempotencyKey": str(order["idempotency_key"]),
        "created_at": order.get("created_at"),
        "createdAt": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "updatedAt": order.get("updated_at"),
        "paid_at": order.get("paid_at"),
        "paidAt": order.get("paid_at"),
        "closed_at": order.get("closed_at"),
        "closedAt": order.get("closed_at"),
        "refunded_at": order.get("refunded_at"),
        "refundedAt": order.get("refunded_at"),
        "credited_points": int(order.get("credited_points") or 0),
        "creditedPoints": int(order.get("credited_points") or 0),
        "refunded_amount_cents": int(
            order.get("refunded_amount_cents") or 0
        ),
        "refundedAmountCents": int(
            order.get("refunded_amount_cents") or 0
        ),
        "refunded_points": int(order.get("refunded_points") or 0),
        "refundedPoints": int(order.get("refunded_points") or 0),
        "provider_payload": provider_payload,
        "providerPayload": provider_payload,
    }
    return payload


def product_payment_event_kind(target_status: str) -> str:
    mapping = {
        payment_service.STATUS_PAID:
            product_payment_store.EVENT_PAYMENT_SUCCEEDED,
        payment_service.STATUS_REFUNDED:
            product_payment_store.EVENT_REFUND_SUCCEEDED,
        payment_service.STATUS_FAILED:
            product_payment_store.EVENT_PAYMENT_FAILED,
        payment_service.STATUS_CLOSED:
            product_payment_store.EVENT_PAYMENT_CLOSED,
    }
    try:
        return mapping[str(target_status)]
    except KeyError as exc:
        raise payment_service.PaymentTransitionError(
            "Unsupported durable payment event target",
            targetStatus=str(target_status),
        ) from exc


def apply_postgres_payment_event(
    event: dict[str, Any],
    *,
    transaction_effect: Callable[[Any, Any], None] | None = None,
) -> dict[str, Any]:
    with postgres_connection() as connection:
        store = product_payment_store.ProductPaymentStore(connection)
        existing_order = store.get_order_by_provider(
            provider=str(event["provider"]),
            provider_order_id=str(event["provider_order_id"]),
        )
        target_status = str(event["target_status"])
        amount_cents = event.get("amount_cents")
        if target_status == payment_service.STATUS_PAID and amount_cents is None:
            amount_cents = int(existing_order["amount_cents"])
        elif (
            target_status == payment_service.STATUS_REFUNDED
            and amount_cents is None
        ):
            amount_cents = int(existing_order["amount_cents"]) - int(
                existing_order.get("refunded_amount_cents") or 0
            )
        result = store.apply_verified_event(
            owner_user_id=str(existing_order["owner_user_id"]),
            provider=str(event["provider"]),
            provider_order_id=str(event["provider_order_id"]),
            provider_event_id=str(event["provider_event_id"]),
            event_kind=product_payment_event_kind(target_status),
            event_type=str(event["event_type"]),
            payload=dict(event["payload"]),
            amount_cents=(
                int(amount_cents) if amount_cents is not None else None
            ),
            transaction_effect=transaction_effect,
        )

    order = product_payment_order_payload(
        result.order,
        idempotent=False,
    )
    event_row = result.event
    if result.idempotent:
        previous_status = str(result.order["status"])
    elif str(event_row["event_kind"]) in {
        product_payment_store.EVENT_PAYMENT_SUCCEEDED,
        product_payment_store.EVENT_PAYMENT_FAILED,
        product_payment_store.EVENT_PAYMENT_CLOSED,
    }:
        previous_status = payment_service.STATUS_PENDING
    elif int(event_row.get("refunded_amount_cents_after") or 0) == int(
        event_row.get("amount_cents") or 0
    ):
        previous_status = payment_service.STATUS_PAID
    else:
        previous_status = "partially_refunded"
    return {
        "ok": True,
        "idempotent": bool(result.idempotent),
        "event_id": str(event_row["provider_event_id"]),
        "eventId": str(event_row["provider_event_id"]),
        "event_type": str(event_row["event_type"]),
        "eventType": str(event_row["event_type"]),
        "status": str(result.order["status"]),
        "target_status": str(event_row["target_status"]),
        "targetStatus": str(event_row["target_status"]),
        "previous_status": previous_status,
        "previousStatus": previous_status,
        "order_id": str(result.order["id"]),
        "orderId": str(result.order["id"]),
        "provider_order_id": str(result.order["provider_order_id"]),
        "providerOrderId": str(result.order["provider_order_id"]),
        "points": int(result.order["points"]),
        "points_to_credit": int(result.points_credited),
        "pointsToCredit": int(result.points_credited),
        "points_to_refund": int(result.points_debited),
        "pointsToRefund": int(result.points_debited),
        "order": order,
    }


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


def normalize_points_adjustment_direction(value: str) -> str:
    direction = str(value or "").strip().lower()
    aliases = {
        "add": "credit",
        "grant": "credit",
        "refund": "credit",
        "recharge": "credit",
        "credit": "credit",
        "deduct": "debit",
        "charge": "debit",
        "debit": "debit",
        "subtract": "debit",
    }
    if direction not in aliases:
        raise ValueError("invalid points adjustment direction")
    return aliases[direction]


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


def product_growth_error_response(exc: Exception):
    if isinstance(exc, PostgresRuntimeError):
        return jsonify(
            {
                "error": "增长服务暂时不可用",
                "code": "postgres_growth_store_unavailable",
            }
        ), 503
    status = 500
    code = "product_growth_store_error"
    if isinstance(
        exc,
        (
            product_growth_store.InvalidProductGrowthInput,
            product_growth_outbox.InvalidGrowthOutboxInput,
        ),
    ):
        status = 400
        code = "invalid_growth_input"
    elif isinstance(exc, product_growth_store.ProductGrowthNotFound):
        status = 404
        code = "growth_record_not_found"
    elif isinstance(
        exc,
        (
            product_growth_store.ProductGrowthConflict,
            product_growth_outbox.GrowthEventConflict,
        ),
    ):
        status = 409
        code = "growth_record_conflict"
    elif isinstance(exc, product_growth_outbox.ProductGrowthOutboxError):
        status = 503
        code = "growth_event_store_unavailable"
    return jsonify({"error": str(exc), "code": code}), status


def product_finance_error_response(exc: Exception):
    if isinstance(exc, PostgresRuntimeError):
        return jsonify(
            {
                "error": "财务服务暂时不可用",
                "code": "postgres_finance_store_unavailable",
            }
        ), 503
    status = 500
    code = "product_finance_store_error"
    details: dict[str, Any] = {}
    if isinstance(exc, product_finance_store.InvalidProductFinanceInput):
        status = 400
        code = "invalid_finance_input"
    elif isinstance(exc, product_finance_store.ProductFinanceNotFound):
        status = 404
        code = "finance_record_not_found"
    elif isinstance(
        exc,
        product_finance_store.InsufficientWithdrawableBalance,
    ):
        status = 409
        code = "insufficient_withdrawable_balance"
        details = {
            "agentId": exc.agent_id,
            "requestedCents": exc.requested_cents,
            "availableCents": exc.available_cents,
        }
    elif isinstance(exc, product_finance_store.ProductFinanceConflict):
        status = 409
        code = "finance_record_conflict"
    return jsonify({"error": str(exc), "code": code, **details}), status


def product_growth_tenant_id() -> str:
    return (
        str(os.environ.get("PRODUCT_GROWTH_TENANT_ID") or "").strip()
        or "waimai"
    )


def product_idempotency_key_required():
    value = str(request.headers.get("Idempotency-Key") or "").strip()
    if value:
        return value, None
    return "", (
        jsonify(
            {
                "error": "Idempotency-Key header is required",
                "code": "idempotency_key_required",
            }
        ),
        400,
    )


def product_growth_invite_code(owner_user_id: str) -> str:
    secret = str(
        os.environ.get("GROWTH_INVITE_CODE_SECRET") or ""
    ).strip()
    if len(secret) < 32:
        raise ValueError("growth_invite_code_secret_required")
    identity = (
        f"{product_growth_tenant_id()}\x00{str(owner_user_id).strip()}"
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"WM{digest[:30]}"


def product_api_record(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            re.sub(
                r"_([a-zA-Z0-9])",
                lambda match: match.group(1).upper(),
                str(key),
            ): product_api_record(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [product_api_record(item) for item in value]
    return value


def product_growth_agent_view(record: dict[str, Any]) -> dict[str, Any]:
    payload = product_api_record(record)
    payload["userId"] = payload.get("ownerUserId", "")
    payload["level"] = "standard"
    return payload


def product_growth_binding_view(record: dict[str, Any]) -> dict[str, Any]:
    payload = product_api_record(record)
    payload["customerId"] = payload.get("customerUserId", "")
    return payload


def product_growth_invite_view(
    record: dict[str, Any],
    *,
    reward_status: str,
) -> dict[str, Any]:
    payload = product_api_record(record)
    pending = reward_status in {"pending", "granted"}
    payload["rewardStatus"] = reward_status
    payload["registrationRewards"] = {
        "inviterPoints": (
            int(payload.get("registrationInviterPoints") or 0)
            if pending
            else 0
        ),
        "inviteePoints": (
            int(payload.get("registrationInviteePoints") or 0)
            if pending
            else 0
        ),
    }
    return payload


def require_authenticated_session() -> tuple[dict[str, Any] | None, Any]:
    token = session_token_from_request()
    if not token:
        return None, (jsonify({"error": "请先登录", "code": "auth_required"}), 401)
    try:
        session = resolve_auth_session(token)
        if session is None:
            return None, (jsonify({"error": "登录已失效", "code": "invalid_session"}), 401)
        return {**session, "token": token}, None
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        return None, product_auth_error_response(exc)


def customer_request_principal() -> tuple[dict[str, Any] | None, Any]:
    if session_token_from_request():
        session, error = require_authenticated_session()
        if error is not None:
            return None, error
        assert session is not None
        return {
            "userId": str(session["user_id"]),
            "localDemo": False,
        }, None
    if local_demo_auth_allowed():
        return {
            "userId": current_user_id(),
            "localDemo": True,
        }, None
    return None, (
        jsonify({"error": "请先登录", "code": "auth_required"}),
        401,
    )


def current_authenticated_user_id() -> str:
    token = session_token_from_request()
    if not token:
        return current_user_id()
    try:
        session = resolve_auth_session(token)
        if session is None:
            return ""
        return str(session["user_id"])
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return ""


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
            and (is_local_request() or staging_test_access_allowed())
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


def admin_user_role_values(user: dict[str, Any] | None) -> set[str]:
    if not user or str(user.get("status") or "active") != "active":
        return set()

    roles: set[str] = set()
    user_id = str(user.get("id") or "").strip()
    if user_id and user_id in env_token_set("ADMIN_USER_IDS", "ADMIN_USER_ID"):
        roles.add("super_admin")

    phone = str(user.get("phone") or "").strip()
    admin_phones = env_token_set("ADMIN_PHONE_NUMBERS", "ADMIN_PHONE_NUMBER")
    for configured_phone in list(admin_phones):
        try:
            admin_phones.add(auth_rules.normalize_phone(configured_phone))
        except (TypeError, ValueError):
            pass
    if phone and phone in admin_phones:
        roles.add("super_admin")

    metadata = user.get("metadata") if isinstance(user.get("metadata"), dict) else {}
    if metadata.get("isAdmin") is True or metadata.get("is_admin") is True:
        roles.add("admin")

    for key in ("role", "adminRole", "admin_role"):
        if metadata.get(key) is not None:
            roles.add(str(metadata[key]).strip().lower())
    metadata_roles = metadata.get("roles")
    if isinstance(metadata_roles, str):
        roles.update(part.strip().lower() for part in re.split(r"[,;\s]+", metadata_roles) if part.strip())
    elif isinstance(metadata_roles, (list, tuple, set)):
        roles.update(str(role).strip().lower() for role in metadata_roles if str(role).strip())
    return roles


def admin_panel_request_authorizer(
    request_value: Any,
    scope: str,
) -> AdminAuthorization:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return AdminAuthorization(authenticated=True, allowed=True)

    session_token = session_token_from_request()
    if not session_token:
        local_allowed = local_demo_admin_allowed()
        return AdminAuthorization(
            authenticated=local_allowed,
            allowed=local_allowed,
        )

    try:
        session = resolve_auth_session(session_token)
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return AdminAuthorization(authenticated=False, allowed=False)
    if not isinstance(session, dict):
        return AdminAuthorization(authenticated=False, allowed=False)
    user = (
        session.get("user")
        if isinstance(session.get("user"), dict)
        else None
    )
    roles = admin_user_role_values(user)
    if scope == ADMIN_READ_SCOPE:
        allowed = user_is_admin(user)
    elif scope == ADMIN_FINANCE_READ_SCOPE:
        allowed = bool(roles & ADMIN_FINANCE_ROLE_VALUES)
    elif scope == ADMIN_RISK_READ_SCOPE:
        allowed = bool(roles & ADMIN_RISK_REVIEW_ROLE_VALUES)
    elif scope == ADMIN_AI_ASSETS_READ_SCOPE:
        allowed = bool(roles & ADMIN_AI_ASSET_REVIEW_ROLE_VALUES)
    elif scope == ADMIN_AI_ASSETS_WRITE_SCOPE:
        payload = request_value.get_json(silent=True) or {}
        status = (
            str(payload.get("status") or payload.get("action") or "")
            .strip()
            .lower()
            if isinstance(payload, dict)
            else ""
        )
        required_roles = (
            ADMIN_AI_ASSET_DISABLE_ROLE_VALUES
            if status == "disabled"
            else ADMIN_AI_ASSET_REVIEW_ROLE_VALUES
        )
        allowed = bool(roles & required_roles)
    else:
        allowed = False
    return AdminAuthorization(authenticated=True, allowed=allowed)


def admin_session_authorized(token: str | None = None) -> bool:
    session_token = str(token or session_token_from_request() or "").strip()
    if not session_token:
        return False
    try:
        session = resolve_auth_session(session_token)
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return False
    return bool(session and user_is_admin(session.get("user") if isinstance(session.get("user"), dict) else None))


def admin_write_authorized() -> bool:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return True
    if session_token_from_request():
        return admin_session_authorized()
    return local_demo_admin_allowed()


def admin_finance_action_authorized() -> bool:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return True
    if not session_token_from_request():
        return local_demo_admin_allowed()

    try:
        session = resolve_auth_session(session_token_from_request())
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return False
    user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else None
    return bool(admin_user_role_values(user) & ADMIN_FINANCE_ROLE_VALUES)


def admin_withdrawal_status_authorized(status: str) -> bool:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return True
    if status == "paid":
        return admin_finance_action_authorized()
    if not session_token_from_request():
        return local_demo_admin_allowed()

    try:
        session = resolve_auth_session(session_token_from_request())
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return False
    user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else None
    roles = admin_user_role_values(user)
    return bool(roles & ADMIN_WITHDRAWAL_REVIEW_ROLE_VALUES)


def admin_commission_settlement_status_authorized(status: str) -> bool:
    if status == "paid":
        return admin_finance_action_authorized()
    return admin_write_authorized()


def admin_ai_asset_status_authorized(status: str) -> bool:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return True
    if not session_token_from_request():
        return local_demo_admin_allowed()

    try:
        session = resolve_auth_session(session_token_from_request())
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return False
    user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else None
    roles = admin_user_role_values(user)
    if status == "disabled":
        return bool(roles & ADMIN_AI_ASSET_DISABLE_ROLE_VALUES)
    return bool(roles & ADMIN_AI_ASSET_REVIEW_ROLE_VALUES)


def admin_risk_decision_authorized(decision: str) -> bool:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return True
    if not session_token_from_request():
        return local_demo_admin_allowed()

    try:
        session = resolve_auth_session(session_token_from_request())
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthConfigurationError,
        product_auth_store.ProductAuthStoreError,
    ):
        return False
    user = session.get("user") if isinstance(session, dict) and isinstance(session.get("user"), dict) else None
    roles = admin_user_role_values(user)
    if decision == "deny":
        return bool(roles & ADMIN_RISK_DENY_ROLE_VALUES)
    return bool(roles & ADMIN_RISK_REVIEW_ROLE_VALUES)


def admin_actor_user_id() -> str:
    if configured_request_token(("ADMIN_API_TOKEN",), "X-Admin-Token"):
        return "service:admin-api-token"

    if not postgres_product_runtime_enabled() and local_demo_admin_allowed():
        return str(
            request.headers.get("X-Admin-User-Id")
            or current_authenticated_user_id()
            or "local-demo-admin"
        )

    authenticated_user_id = current_authenticated_user_id()
    if authenticated_user_id:
        return authenticated_user_id
    return ""


def product_admin_dashboard_provider() -> dict[str, Any] | None:
    if not postgres_product_runtime_enabled():
        return None

    with postgres_connection() as connection:
        read_store = product_admin_read_store.ProductAdminReadStore(
            connection
        )
        summary = read_store.dashboard_summary()
        recent_jobs = read_store.recent_jobs(limit=12)
        commissions = read_store.commission_summary()

        security_store = (
            product_admin_security_store.ProductAdminSecurityStore(
                connection
            )
        )
        risk_counts = security_store.risk_summary()
        access_counts = security_store.access_summary()
        recent_risk = security_store.list_risk_decisions(limit=100)
        recent_access = security_store.list_asset_access(limit=100)

    risk_levels = Counter(
        str(row.get("risk_level") or "info") for row in recent_risk
    )
    risk_events = Counter(
        str(row.get("event_type") or "") for row in recent_risk
        if str(row.get("event_type") or "")
    )
    risk_ips = Counter(
        str(row.get("subject_value") or "") for row in recent_risk
        if row.get("subject_type") == "ip"
    )
    risk_level_order = ("critical", "high", "medium", "low", "info")
    highest_level = next(
        (level for level in risk_level_order if risk_levels.get(level)),
        "info",
    )

    access_actions = Counter(
        str(row.get("action") or "") for row in recent_access
        if str(row.get("action") or "")
    )
    access_types = Counter(
        str(row.get("asset_type") or "") for row in recent_access
        if str(row.get("asset_type") or "")
    )
    access_assets = Counter(
        str(row.get("asset_id") or "") for row in recent_access
        if str(row.get("asset_id") or "")
    )
    deny_reasons = Counter(
        str(row.get("deny_reason") or "") for row in recent_access
        if not bool(row.get("allowed"))
        and str(row.get("deny_reason") or "")
    )

    summary["risk"] = {
        "total": int(risk_counts["total"]),
        "review": int(risk_counts["review"]),
        "denied": int(risk_counts["deny"]),
        "currentDeniedSubjects": int(
            risk_counts["current_denied_subjects"]
        ),
    }
    summary["assetAccess"] = {
        "total": int(access_counts["total"]),
        "allowed": int(access_counts["allowed"]),
        "denied": int(access_counts["denied"]),
    }
    return {
        "ok": True,
        "summary": summary,
        "recentJobs": recent_jobs,
        "commissions": commissions,
        "risk": {
            "total": int(risk_counts["total"]),
            "highestLevel": highest_level,
            "byLevel": dict(risk_levels),
            "byDecision": {
                "allow": int(risk_counts["allow"]),
                "deny": int(risk_counts["deny"]),
                "review": int(risk_counts["review"]),
            },
            "topEvents": _admin_counter_rows(risk_events),
            "topIps": _admin_counter_rows(risk_ips),
            "recent": [
                product_admin_risk_view(row) for row in recent_risk[:8]
            ],
        },
        "assetAccess": {
            "total": int(access_counts["total"]),
            "allowed": int(access_counts["allowed"]),
            "denied": int(access_counts["denied"]),
            "topDenyReason": (
                deny_reasons.most_common(1)[0][0]
                if deny_reasons
                else ""
            ),
            "byAction": _admin_counter_rows(access_actions),
            "byAssetType": _admin_counter_rows(access_types),
            "topAssets": [
                {"assetId": value, "accessCount": count}
                for value, count in access_assets.most_common(8)
            ],
            "recentDenied": [
                product_admin_asset_access_view(row)
                for row in recent_access
                if not bool(row.get("allowed"))
            ][:8],
        },
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def product_admin_list_provider(
    resource: str,
    args: Any,
) -> dict[str, Any] | None:
    if not postgres_product_runtime_enabled():
        return None

    clean_args = dict(args or {})
    with postgres_connection() as connection:
        if resource == "risk-events":
            user_id = str(clean_args.pop("user_id", "") or "").strip()
            if user_id:
                clean_args["subject_type"] = "user"
                clean_args["subject_value"] = user_id
            page = (
                product_admin_security_store.ProductAdminSecurityStore(
                    connection
                ).page_risk_decisions(**clean_args)
            )
            return {
                **page,
                "items": [
                    product_admin_risk_view(row)
                    for row in page["items"]
                ],
            }

        if resource == "asset-access":
            status = str(clean_args.pop("status", "") or "").lower()
            if "allowed" not in clean_args and status:
                if status not in {"allowed", "denied"}:
                    raise ValueError("invalid asset access status")
                clean_args["allowed"] = status == "allowed"
            page = (
                product_admin_security_store.ProductAdminSecurityStore(
                    connection
                ).page_asset_access(**clean_args)
            )
            return {
                **page,
                "items": [
                    product_admin_asset_access_view(row)
                    for row in page["items"]
                ],
            }

        read_method = {
            "users": "list_users",
            "stores": "list_stores",
            "orders": "list_orders",
            "generation-tasks": "list_generation_tasks",
            "commission-settlements": "list_commission_settlements",
            "withdrawals": "list_withdrawals",
        }.get(resource)
        if not read_method:
            raise ValueError("unsupported admin list resource")
        store = product_admin_read_store.ProductAdminReadStore(connection)
        return getattr(store, read_method)(**clean_args)


def product_admin_risk_view(record: dict[str, Any]) -> dict[str, Any]:
    subject_type = str(record.get("subject_type") or "")
    subject_value = str(record.get("subject_value") or "")
    return {
        "id": str(record.get("action_id") or ""),
        "actorUserId": str(record.get("actor_user_id") or ""),
        "eventType": str(record.get("event_type") or ""),
        "subjectType": subject_type,
        "subjectValue": subject_value,
        "userId": subject_value if subject_type == "user" else "",
        "agentId": subject_value if subject_type == "agent" else "",
        "assetId": subject_value if subject_type == "asset" else "",
        "ip": subject_value if subject_type == "ip" else "",
        "riskLevel": str(record.get("risk_level") or ""),
        "decision": str(record.get("decision") or ""),
        "denyReason": str(record.get("deny_reason") or ""),
        "metadata": (
            record.get("metadata")
            if isinstance(record.get("metadata"), dict)
            else {}
        ),
        "createdAt": record.get("created_at"),
    }


def product_admin_asset_access_view(
    record: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": str(record.get("action_id") or ""),
        "actorUserId": str(record.get("actor_user_id") or ""),
        "requestId": str(record.get("request_id") or ""),
        "assetId": str(record.get("asset_id") or ""),
        "assetType": str(record.get("asset_type") or ""),
        "action": str(record.get("action") or ""),
        "userId": str(record.get("user_id") or ""),
        "agentId": str(record.get("agent_id") or ""),
        "ip": str(record.get("ip_address") or ""),
        "allowed": bool(record.get("allowed")),
        "status": (
            "allowed" if bool(record.get("allowed")) else "denied"
        ),
        "denyReason": str(record.get("deny_reason") or ""),
        "userAgent": str(record.get("user_agent") or ""),
        "metadata": (
            record.get("metadata")
            if isinstance(record.get("metadata"), dict)
            else {}
        ),
        "createdAt": record.get("created_at"),
    }


def _admin_counter_rows(
    values: Counter[str],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    return [
        {"name": value, "count": count}
        for value, count in values.most_common(limit)
    ]


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


def product_growth_agent_for_session(
    connection: Any,
    session: dict[str, Any],
    requested_agent_id: str = "",
) -> tuple[dict[str, Any] | None, Any]:
    agent = product_growth_store.ProductGrowthStore(
        connection
    ).get_agent_by_user(
        tenant_id=product_growth_tenant_id(),
        owner_user_id=str(session.get("user_id") or ""),
    )
    if agent is None:
        return None, (
            jsonify(
                {
                    "error": "当前账号不是代理",
                    "code": "agent_profile_required",
                }
            ),
            404,
        )
    clean_requested_agent_id = str(requested_agent_id or "").strip()
    if (
        clean_requested_agent_id
        and clean_requested_agent_id != str(agent["id"])
    ):
        return None, forbidden(
            "不能提现或查看其他代理账户",
            "agent_access_forbidden",
        )
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
    audit_metadata = {
        "path": request.path,
        "method": request.method,
        **(metadata or {}),
    }
    if postgres_product_runtime_enabled():
        nonce = secrets.token_hex(16)
        action_id = product_growth_store.stable_growth_id(
            "asset_access",
            product_growth_tenant_id(),
            nonce,
        )
        request_id = product_growth_store.stable_growth_id(
            "http_request",
            product_growth_tenant_id(),
            nonce,
        )
        actor_user_id = (
            str(user_id or agent_id or "").strip()
            or "service:anonymous"
        )
        try:
            with postgres_connection() as connection:
                result = (
                    product_admin_security_store
                    .ProductAdminSecurityStore(connection)
                    .record_asset_access(
                        action_id=action_id,
                        actor_user_id=actor_user_id,
                        request_id=request_id,
                        asset_id=asset_id,
                        asset_type=(
                            str(asset_type or "").strip() or "unknown"
                        ),
                        action=action,
                        user_id=str(user_id or ""),
                        agent_id=str(agent_id or ""),
                        ip=request_ip(),
                        allowed=allowed,
                        deny_reason=str(deny_reason or ""),
                        user_agent=str(
                            request.headers.get("User-Agent") or ""
                        ),
                        metadata={
                            **audit_metadata,
                            "clientRequestId": str(
                                request.headers.get("X-Request-Id") or ""
                            ),
                        },
                    )
                )
                if not result.record:
                    raise RuntimeError("asset access audit was not recorded")
        except Exception:
            app.logger.exception(
                "PostgreSQL asset access audit unavailable"
            )
            raise
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
            metadata=audit_metadata,
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
            growth["consumerReferralBilling"] = credit_points(
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
            growth["consumerReferralRefundBilling"] = debit_points(
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


def generation_provenance_snapshot() -> dict[str, str]:
    tokenhub = tokenhub_config()
    if tokenhub_ready():
        provider = "tencent-hunyuan"
        provider_mode = "tokenhub-fail-closed-v2"
        model_name = str(tokenhub["model"] or "hy-image-v3.0")
        model_version = (
            f"{model_name}.aiart-{TENCENT_AIART_VERSION}."
            f"hunyuan-{TENCENT_HUNYUAN_VERSION}"
        )
    elif tencent_cloud_ready():
        provider = "tencent-hunyuan"
        provider_mode = "cloud-fallback-v1"
        model_name = "tencent-cloud-text-to-image"
        model_version = (
            f"aiart-{TENCENT_AIART_VERSION}."
            f"hunyuan-{TENCENT_HUNYUAN_VERSION}"
        )
    else:
        provider = "local-demo"
        provider_mode = "local-fallback-v1"
        model_name = "local-category-compositor"
        model_version = "local-category.v1"
    return {
        "taxonomyVersion": TAXONOMY_VERSION,
        "dishPromptVersion": (
            f"dish-generation.v{DISH_GENERATION_PROMPT_VERSION}"
        ),
        "pipelineVersion": (
            f"exact-background.v{EXACT_BACKGROUND_PIPELINE_VERSION}"
        ),
        "provider": product_asset_version_token(provider, "provider"),
        "providerMode": product_asset_version_token(
            provider_mode,
            "provider-mode",
        ),
        "modelName": product_asset_version_token(
            model_name,
            "model",
        ),
        "modelVersion": product_asset_version_token(
            model_version,
            "model-version",
        ),
    }


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


def render_runtime_detected() -> bool:
    if any(os.environ.get(name, "").strip() for name in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL")):
        return True
    return ".onrender.com" in os.environ.get("PUBLIC_BASE_URL", "").strip().lower()


def runtime_environment_label() -> str:
    app_env = os.environ.get("APP_ENV", "").strip().lower()
    if app_env:
        return app_env
    if render_runtime_detected():
        return "render"
    return "development"


def generation_provider_readiness() -> dict[str, Any]:
    status = tencent_status_payload()
    app_env = runtime_environment_label()
    live_generation_required = env_truthy(
        "REQUIRE_LIVE_GENERATION_PROVIDER",
        default=app_env in {"production", "prod", "staging", "render"},
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
        "appEnv": app_env,
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


def product_generation_readiness() -> dict[str, Any]:
    app_env = runtime_environment_label()
    live_runtime = app_env in {"production", "prod", "staging", "render"} or render_runtime_detected()
    redis_configured = bool(str(os.environ.get("REDIS_URL") or "").strip())
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    postgres_database_configured = database_url.lower().startswith(
        ("postgres://", "postgresql://")
    )
    postgres_enabled = env_truthy(
        "PRODUCT_POSTGRES_ENABLED",
        default=live_runtime,
    )
    postgres_configured = postgres_database_configured and postgres_enabled
    product_worker_declared = env_truthy("PRODUCT_WORKER_ENABLED", default=False)
    product_worker_service_id = (
        str(os.environ.get("PRODUCT_WORKER_SERVICE_ID") or "product-worker").strip()
        or "product-worker"
    )
    outbox_dispatcher_declared = env_truthy(
        "PRODUCT_OUTBOX_DISPATCHER_ENABLED",
        default=False,
    )
    settlement_reconciler_declared = env_truthy(
        "PRODUCT_SETTLEMENT_RECONCILER_ENABLED",
        default=False,
    )
    outbox_dispatcher_service_id = "product-outbox-dispatcher"
    settlement_reconciler_service_id = "product-settlement-reconciler"
    durable_job_store_integrated = True
    transactional_outbox_integrated = True
    settlement_reconciler_integrated = True
    worker_liveness_integrated = True
    queue_name = (
        str(os.environ.get("REDIS_PRODUCT_QUEUE") or "product-generate").strip()
        or "product-generate"
    )

    def missing_liveness(service_id: str) -> dict[str, Any]:
        return {
            "ready": False,
            "serviceId": service_id,
            "queueName": queue_name,
            "reason": "redis_not_configured",
        }

    worker_liveness = missing_liveness(product_worker_service_id)
    outbox_dispatcher_liveness = missing_liveness(
        outbox_dispatcher_service_id
    )
    settlement_reconciler_liveness = missing_liveness(
        settlement_reconciler_service_id
    )
    if redis_configured:
        try:
            readiness_queue = redis_product_queue_from_env()
        except Exception as exc:  # noqa: BLE001 - readiness must report Redis failures
            for liveness in (
                worker_liveness,
                outbox_dispatcher_liveness,
                settlement_reconciler_liveness,
            ):
                liveness["reason"] = "heartbeat_read_failed"
                liveness["errorType"] = type(exc).__name__
        else:
            def read_liveness(service_id: str) -> dict[str, Any]:
                base = missing_liveness(service_id)
                try:
                    heartbeat = readiness_queue.service_liveness(service_id)
                except Exception as exc:  # noqa: BLE001 - readiness is diagnostic
                    base["reason"] = "heartbeat_read_failed"
                    base["errorType"] = type(exc).__name__
                    return base
                if heartbeat is None:
                    base["reason"] = "heartbeat_missing_or_expired"
                    return base
                return {
                    "ready": True,
                    "serviceId": service_id,
                    "queueName": str(heartbeat.get("queueName") or ""),
                    "lastSeenAgeMs": int(heartbeat.get("ageMs") or 0),
                    "heartbeatTtlSeconds": int(
                        heartbeat.get("ttlSeconds") or 0
                    ),
                }

            worker_liveness = read_liveness(product_worker_service_id)
            outbox_dispatcher_liveness = read_liveness(
                outbox_dispatcher_service_id
            )
            settlement_reconciler_liveness = read_liveness(
                settlement_reconciler_service_id
            )

    blocking_issues: list[str] = []
    warnings: list[str] = []
    missing_config: list[str] = []
    required_config: list[dict[str, Any]] = []

    if live_runtime:
        if not redis_configured:
            blocking_issues.append("product_redis_queue_required")
            missing_config.append("REDIS_URL")
            required_config.append({"key": "redis_url", "env": ["REDIS_URL"]})
        if not postgres_database_configured:
            blocking_issues.append("postgres_product_job_store_required")
            missing_config.append("DATABASE_URL")
            required_config.append({"key": "database_url", "env": ["DATABASE_URL"]})
        if not postgres_enabled:
            blocking_issues.append("postgres_product_runtime_enabled_required")
            missing_config.append("PRODUCT_POSTGRES_ENABLED")
            required_config.append(
                {
                    "key": "product_postgres_enabled",
                    "env": ["PRODUCT_POSTGRES_ENABLED"],
                    "allowedValues": ["true"],
                }
            )
        if not product_worker_declared:
            blocking_issues.append("product_worker_service_required")
            missing_config.append("PRODUCT_WORKER_ENABLED")
            required_config.append(
                {
                    "key": "product_worker_enabled",
                    "env": ["PRODUCT_WORKER_ENABLED"],
                    "allowedValues": ["true"],
                }
            )
        if not outbox_dispatcher_declared:
            blocking_issues.append(
                "product_outbox_dispatcher_service_required"
            )
            missing_config.append("PRODUCT_OUTBOX_DISPATCHER_ENABLED")
            required_config.append(
                {
                    "key": "product_outbox_dispatcher_enabled",
                    "env": ["PRODUCT_OUTBOX_DISPATCHER_ENABLED"],
                    "allowedValues": ["true"],
                }
            )
        if not settlement_reconciler_declared:
            blocking_issues.append(
                "product_settlement_reconciler_service_required"
            )
            missing_config.append("PRODUCT_SETTLEMENT_RECONCILER_ENABLED")
            required_config.append(
                {
                    "key": "product_settlement_reconciler_enabled",
                    "env": ["PRODUCT_SETTLEMENT_RECONCILER_ENABLED"],
                    "allowedValues": ["true"],
                }
            )
        if not durable_job_store_integrated:
            blocking_issues.append("postgres_product_job_store_not_integrated")
        if not transactional_outbox_integrated:
            blocking_issues.append("transactional_product_outbox_not_integrated")
        if not settlement_reconciler_integrated:
            blocking_issues.append(
                "product_settlement_reconciler_not_integrated"
            )
        if not bool(worker_liveness.get("ready")):
            blocking_issues.append("product_worker_not_live")
        if not bool(outbox_dispatcher_liveness.get("ready")):
            blocking_issues.append("product_outbox_dispatcher_not_live")
        if not bool(settlement_reconciler_liveness.get("ready")):
            blocking_issues.append("product_settlement_reconciler_not_live")
    else:
        warnings.extend(
            [
                "sqlite_product_job_store_is_for_local_demo_only",
                "in_process_generation_fallback_is_for_local_demo_only",
            ]
        )
        if not redis_configured:
            warnings.append("product_redis_queue_not_configured")
        elif not bool(worker_liveness.get("ready")):
            warnings.append("product_worker_not_live")

    return {
        "ready": not blocking_issues,
        "appEnv": app_env,
        "mode": "redis_worker" if redis_configured else "in_process_demo",
        "queueName": queue_name,
        "redisConfigured": redis_configured,
        "postgresConfigured": postgres_configured,
        "postgresDatabaseConfigured": postgres_database_configured,
        "postgresEnabled": postgres_enabled,
        "durableJobStore": (
            "postgresql" if postgres_configured else "sqlite_local_demo"
        ),
        "durableJobStoreIntegrated": durable_job_store_integrated,
        "transactionalOutboxIntegrated": transactional_outbox_integrated,
        "settlementReconcilerIntegrated": settlement_reconciler_integrated,
        "productWorkerDeclared": product_worker_declared,
        "outboxDispatcherDeclared": outbox_dispatcher_declared,
        "settlementReconcilerDeclared": settlement_reconciler_declared,
        "workerLivenessIntegrated": worker_liveness_integrated,
        "workerLiveness": worker_liveness,
        "outboxDispatcherLiveness": outbox_dispatcher_liveness,
        "settlementReconcilerLiveness": settlement_reconciler_liveness,
        "warnings": warnings,
        "errors": list(blocking_issues),
        "blockingIssues": blocking_issues,
        "requiredConfig": required_config,
        "missingConfig": missing_config,
    }


def product_growth_readiness() -> dict[str, Any]:
    app_env = runtime_environment_label()
    live_runtime = (
        app_env in {"production", "prod", "staging", "render"}
        or render_runtime_detected()
    )
    redis_configured = bool(str(os.environ.get("REDIS_URL") or "").strip())
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    postgres_database_configured = database_url.lower().startswith(
        ("postgres://", "postgresql://")
    )
    postgres_enabled = env_truthy(
        "PRODUCT_POSTGRES_ENABLED",
        default=live_runtime,
    )
    worker_declared = env_truthy(
        "GROWTH_EVENT_WORKER_ENABLED",
        default=False,
    )
    invite_code_secret_configured = (
        len(
            str(
                os.environ.get("GROWTH_INVITE_CODE_SECRET") or ""
            ).strip()
        )
        >= 32
    )
    service_id = (
        str(
            os.environ.get("GROWTH_EVENT_WORKER_SERVICE_ID")
            or "growth-event-worker"
        ).strip()
        or "growth-event-worker"
    )
    queue_name = (
        str(os.environ.get("REDIS_PRODUCT_QUEUE") or "product-generate").strip()
        or "product-generate"
    )
    liveness: dict[str, Any] = {
        "ready": False,
        "serviceId": service_id,
        "queueName": queue_name,
        "reason": "redis_not_configured",
    }
    if redis_configured:
        try:
            heartbeat = redis_product_queue_from_env().service_liveness(
                service_id
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            liveness["reason"] = "heartbeat_read_failed"
            liveness["errorType"] = type(exc).__name__
        else:
            if heartbeat is None:
                liveness["reason"] = "heartbeat_missing_or_expired"
            else:
                liveness = {
                    "ready": True,
                    "serviceId": service_id,
                    "queueName": str(heartbeat.get("queueName") or ""),
                    "lastSeenAgeMs": int(heartbeat.get("ageMs") or 0),
                    "heartbeatTtlSeconds": int(
                        heartbeat.get("ttlSeconds") or 0
                    ),
                }

    blocking_issues: list[str] = []
    missing_config: list[str] = []
    required_config: list[dict[str, Any]] = []
    warnings: list[str] = []
    if live_runtime:
        if not postgres_database_configured:
            blocking_issues.append("growth_postgres_database_required")
            missing_config.append("DATABASE_URL")
            required_config.append(
                {"key": "database_url", "env": ["DATABASE_URL"]}
            )
        if not postgres_enabled:
            blocking_issues.append("growth_postgres_runtime_enabled_required")
            missing_config.append("PRODUCT_POSTGRES_ENABLED")
            required_config.append(
                {
                    "key": "product_postgres_enabled",
                    "env": ["PRODUCT_POSTGRES_ENABLED"],
                    "allowedValues": ["true"],
                }
            )
        if not redis_configured:
            blocking_issues.append("growth_worker_redis_required")
            missing_config.append("REDIS_URL")
            required_config.append(
                {"key": "redis_url", "env": ["REDIS_URL"]}
            )
        if not worker_declared:
            blocking_issues.append("growth_event_worker_service_required")
            missing_config.append("GROWTH_EVENT_WORKER_ENABLED")
            required_config.append(
                {
                    "key": "growth_event_worker_enabled",
                    "env": ["GROWTH_EVENT_WORKER_ENABLED"],
                    "allowedValues": ["true"],
                }
            )
        if not invite_code_secret_configured:
            blocking_issues.append("growth_invite_code_secret_required")
            missing_config.append("GROWTH_INVITE_CODE_SECRET")
            required_config.append(
                {
                    "key": "growth_invite_code_secret",
                    "env": ["GROWTH_INVITE_CODE_SECRET"],
                }
            )
        if not bool(liveness.get("ready")):
            blocking_issues.append("growth_event_worker_not_live")
    else:
        warnings.append("sqlite_growth_is_for_local_demo_only")
        if not redis_configured:
            warnings.append("growth_event_worker_not_configured")
        elif not bool(liveness.get("ready")):
            warnings.append("growth_event_worker_not_live")

    return {
        "ready": not blocking_issues,
        "appEnv": app_env,
        "mode": (
            "postgres_worker"
            if postgres_database_configured and postgres_enabled
            else "sqlite_local_demo"
        ),
        "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
        "postgresDatabaseConfigured": postgres_database_configured,
        "postgresEnabled": postgres_enabled,
        "redisConfigured": redis_configured,
        "workerDeclared": worker_declared,
        "inviteCodeSecretConfigured": invite_code_secret_configured,
        "workerLiveness": liveness,
        "warnings": warnings,
        "errors": list(blocking_issues),
        "blockingIssues": blocking_issues,
        "requiredConfig": required_config,
        "missingConfig": missing_config,
    }


def image_refinement_readiness() -> dict[str, Any]:
    app_env = runtime_environment_label()
    live_required = env_truthy(
        "REQUIRE_IMAGE_REFINEMENT_PROVIDER",
        default=(
            app_env in {"production", "prod", "staging", "render"}
            or render_runtime_detected()
        ),
    )
    provider = gemini_image_edit_readiness()
    provider_ready = bool(provider.get("ready"))
    blocking_issues = (
        list(provider.get("blockingIssues") or [])
        if live_required and not provider_ready
        else []
    )
    warnings = (
        ["image_refinement_provider_not_configured_local_only"]
        if not live_required and not provider_ready
        else []
    )
    return {
        **provider,
        "ready": not blocking_issues,
        "providerConfigured": provider_ready,
        "liveRequired": live_required,
        "blockingIssues": blocking_issues,
        "errors": list(blocking_issues),
        "warnings": warnings,
    }


PRODUCT_AUTH_TABLES = (
    "product_users",
    "product_auth_sessions",
    "product_registration_security_events",
    "product_stores",
    "product_user_stores",
)


def probe_product_auth_postgres() -> dict[str, Any]:
    try:
        with postgres_connection() as connection:
            product_auth_store.product_auth_store_from_env(connection)
            cursor = connection.cursor()
            try:
                missing_tables: list[str] = []
                for table in PRODUCT_AUTH_TABLES:
                    cursor.execute("SELECT to_regclass(%s)", (table,))
                    row = cursor.fetchone()
                    if not row or row[0] is None:
                        missing_tables.append(table)
            finally:
                cursor.close()
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        return {
            "configured": True,
            "reachable": False,
            "schemaReady": False,
            "missingTables": [],
            "error": str(getattr(exc, "code", "") or "auth_postgres_probe_failed"),
        }
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "schemaReady": False,
            "missingTables": [],
            "error": "auth_postgres_probe_failed",
            "errorType": type(exc).__name__,
        }
    return {
        "configured": True,
        "reachable": True,
        "schemaReady": not missing_tables,
        "missingTables": missing_tables,
        "error": "" if not missing_tables else "auth_postgres_schema_incomplete",
    }


def probe_product_auth_redis() -> dict[str, Any]:
    store: product_otp_store.ProductOtpStore | None = None
    try:
        store = product_otp_store.product_otp_store_from_env()
        reachable = bool(store.redis.ping())
        if not reachable:
            raise product_otp_store.ProductOtpUnavailable()
    except product_otp_store.ProductOtpStoreError as exc:
        return {
            "configured": True,
            "reachable": False,
            "error": str(getattr(exc, "code", "") or "auth_redis_probe_failed"),
        }
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "error": "auth_redis_probe_failed",
            "errorType": type(exc).__name__,
        }
    finally:
        close_product_otp_client(store)
    return {
        "configured": True,
        "reachable": True,
        "error": "",
    }


def product_auth_readiness() -> dict[str, Any]:
    app_env = runtime_environment_label()
    live_required = (
        app_env in {"production", "prod", "staging", "render"}
        or render_runtime_detected()
    )
    if not live_required:
        return {
            "ready": True,
            "appEnv": app_env,
            "mode": "sqlite_local_demo",
            "liveRequired": False,
            "postgres": {
                "configured": False,
                "reachable": False,
                "schemaReady": False,
                "missingTables": list(PRODUCT_AUTH_TABLES),
                "error": "",
            },
            "redis": {
                "configured": False,
                "reachable": False,
                "error": "",
            },
            "blockingIssues": [],
            "errors": [],
            "warnings": ["sqlite_auth_is_for_local_demo_only"],
            "missingConfig": [],
        }

    missing_config: list[str] = []
    blocking_issues: list[str] = []
    database_url = str(os.environ.get("DATABASE_URL") or "").strip().lower()
    redis_url = str(os.environ.get("REDIS_URL") or "").strip().lower()
    if not database_url.startswith(("postgres://", "postgresql://")):
        missing_config.append("DATABASE_URL")
        blocking_issues.append("auth_postgres_database_required")
    if not redis_url.startswith(("redis://", "rediss://", "unix://")):
        missing_config.append("REDIS_URL")
        blocking_issues.append("auth_redis_required")

    for env_name, issue in (
        ("AUTH_SESSION_HASH_SECRET", "auth_session_hash_secret_required"),
        ("AUTH_OTP_HASH_SECRET", "auth_otp_hash_secret_required"),
    ):
        value = str(os.environ.get(env_name) or "")
        if not value:
            missing_config.append(env_name)
            blocking_issues.append(issue)
        elif len(value.encode("utf-8")) < 32:
            blocking_issues.append(f"{issue.removesuffix('_required')}_invalid")

    sms_provider_name = str(os.environ.get("SMS_PROVIDER") or "").strip()
    if not sms_provider_name:
        missing_config.append("SMS_PROVIDER")
        blocking_issues.append("auth_sms_provider_required")
    if sms_provider_name.lower() == "webhook" and not str(
        os.environ.get("SMS_WEBHOOK_URL") or ""
    ).strip():
        missing_config.append("SMS_WEBHOOK_URL")
        blocking_issues.append("auth_sms_webhook_url_required")

    postgres_probe = {
        "configured": "DATABASE_URL" not in missing_config,
        "reachable": False,
        "schemaReady": False,
        "missingTables": list(PRODUCT_AUTH_TABLES),
        "error": "",
    }
    redis_probe = {
        "configured": "REDIS_URL" not in missing_config,
        "reachable": False,
        "error": "",
    }
    if not blocking_issues:
        postgres_probe = probe_product_auth_postgres()
        redis_probe = probe_product_auth_redis()
        if not bool(postgres_probe.get("reachable")):
            blocking_issues.append("auth_postgres_unreachable")
        elif not bool(postgres_probe.get("schemaReady")):
            blocking_issues.append("auth_postgres_schema_incomplete")
        if not bool(redis_probe.get("reachable")):
            blocking_issues.append("auth_redis_unreachable")
        try:
            sms_service.provider_from_env(
                local_demo_enabled=False
            ).ensure_available()
        except sms_service.SmsServiceError:
            blocking_issues.append("auth_sms_provider_unavailable")

    return {
        "ready": not blocking_issues,
        "appEnv": app_env,
        "mode": "postgres_redis",
        "liveRequired": True,
        "postgres": postgres_probe,
        "redis": redis_probe,
        "blockingIssues": blocking_issues,
        "errors": list(blocking_issues),
        "warnings": [],
        "missingConfig": sorted(set(missing_config)),
    }


SENSITIVE_ENV_NAME_PARTS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "PRIVATE", "CERT")


def first_configured_env(env_names: tuple[str, ...] | list[str], values: Any = None) -> str:
    env = os.environ if values is None else values
    for name in env_names:
        if str(env.get(name) or "").strip():
            return name
    return ""


def deployment_config_item(
    key: str,
    env_names: tuple[str, ...] | list[str],
    *,
    required: bool = True,
    sensitive: bool | None = None,
    recommended: str = "",
    description: str = "",
    allowed_values: tuple[str, ...] | list[str] = (),
    values: Any = None,
) -> dict[str, Any]:
    env = os.environ if values is None else values
    configured_env = first_configured_env(env_names, env)
    if sensitive is None:
        sensitive = any(part in name.upper() for name in env_names for part in SENSITIVE_ENV_NAME_PARTS)
    item: dict[str, Any] = {
        "key": key,
        "env": list(env_names),
        "required": required,
        "sensitive": bool(sensitive),
        "configured": bool(configured_env),
        "status": "configured" if configured_env else ("missing" if required else "optional"),
    }
    if configured_env:
        item["configuredEnv"] = configured_env
        item["value"] = "configured" if sensitive else str(env.get(configured_env) or "")
    if recommended:
        item["recommended"] = recommended
    if description:
        item["description"] = description
    if allowed_values:
        item["allowedValues"] = list(allowed_values)
    return item


def deployment_config_section(
    section_id: str,
    title: str,
    items: list[dict[str, Any]],
    blocking_issues: list[str] | tuple[str, ...] = (),
    warnings: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    missing_required = [item["env"][0] for item in items if item.get("required") and not item.get("configured")]
    issues = list(blocking_issues)
    return {
        "id": section_id,
        "title": title,
        "ready": not missing_required and not issues,
        "missingRequiredEnv": missing_required,
        "blockingIssues": issues,
        "warnings": list(warnings),
        "items": items,
    }


def deployment_config_report() -> dict[str, Any]:
    object_storage = object_storage_service.assess_object_storage_readiness()
    payments = payment_service.assess_payment_provider_readiness()
    generation_provider = generation_provider_readiness()
    product_generation = product_generation_readiness()
    product_growth = product_growth_readiness()
    refinement_provider = image_refinement_readiness()
    product_auth = product_auth_readiness()
    app_env = runtime_environment_label()
    live_env = app_env in {"production", "prod", "staging", "render"}
    queue = generation_queue.snapshot()

    runtime_section = deployment_config_section(
        "runtime",
        "Runtime",
        [
            deployment_config_item("app_env", ("APP_ENV",), required=live_env, recommended="staging"),
            deployment_config_item("public_base_url", ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"), required=False),
            deployment_config_item("admin_api_token", ("ADMIN_API_TOKEN",), required=live_env),
        ],
        warnings=["admin_api_token_recommended_for_ops_endpoints"] if live_env and not os.environ.get("ADMIN_API_TOKEN", "").strip() else [],
    )

    generation_section = deployment_config_section(
        "generationProvider",
        "AI Generation Provider",
        [
            deployment_config_item(
                "tencent_tokenhub_api_key",
                ("TENCENT_TOKENHUB_API_KEY", "TOKENHUB_API_KEY", "HUNYUAN_TOKENHUB_API_KEY"),
                required=bool(generation_provider.get("tokenhubRequired")),
                description="TokenHub HY-Image-3.0 API key used for real background and product image generation.",
            ),
            deployment_config_item(
                "tencent_tokenhub_image_model",
                ("TENCENT_TOKENHUB_IMAGE_MODEL",),
                required=False,
                recommended="hy-image-v3.0",
            ),
            deployment_config_item("tencent_hunyuan_enabled", ("TENCENT_HUNYUAN_ENABLED",), required=False, recommended="true"),
        ],
        generation_provider.get("blockingIssues", []),
        generation_provider.get("warnings", []),
    )

    object_section = deployment_config_section(
        "objectStorage",
        "Object Storage",
        [
            deployment_config_item("object_storage_provider", ("OBJECT_STORAGE_PROVIDER",), required=live_env, recommended="cos"),
            deployment_config_item("object_storage_private", ("OBJECT_STORAGE_PRIVATE",), required=live_env, recommended="true"),
            deployment_config_item("object_storage_bucket", ("OBJECT_STORAGE_BUCKET", "TENCENT_COS_BUCKET"), required=live_env),
            deployment_config_item("object_storage_region", ("OBJECT_STORAGE_REGION", "TENCENT_COS_REGION", "TENCENTCLOUD_REGION"), required=live_env, recommended=DEFAULT_TENCENT_COS_REGION),
            deployment_config_item("object_storage_prefix", ("OBJECT_STORAGE_PREFIX", "TENCENT_COS_PREFIX"), required=False, recommended="app-objects"),
            deployment_config_item("object_signing_secret", ("OBJECT_SIGNING_SECRET", "DOWNLOAD_SIGNING_SECRET"), required=live_env),
            deployment_config_item("object_storage_secret_id", ("OBJECT_STORAGE_SECRET_ID", "TENCENTCLOUD_SECRET_ID", "TENCENT_SECRET_ID"), required=live_env),
            deployment_config_item("object_storage_secret_key", ("OBJECT_STORAGE_SECRET_KEY", "TENCENTCLOUD_SECRET_KEY", "TENCENT_SECRET_KEY"), required=live_env),
        ],
        object_storage.get("blockingIssues", []),
        object_storage.get("warnings", []),
    )

    payment_required_items = [
        deployment_config_item(
            "payment_provider",
            ("PAYMENT_PROVIDER",),
            required=live_env,
            recommended="alipay",
            allowed_values=tuple(sorted(payment_service.REAL_PAYMENT_PROVIDERS)),
        ),
        deployment_config_item(
            "payment_webhook_secret",
            ("PAYMENT_WEBHOOK_SECRET",),
            required=live_env and payments.get("provider") not in {"alipay", "wechat"},
        ),
    ]
    for required_config in payments.get("requiredConfig", []):
        env_names = tuple(str(name) for name in required_config.get("env", ()) if str(name))
        if env_names and not any(item["env"] == list(env_names) for item in payment_required_items):
            payment_required_items.append(
                deployment_config_item(
                    str(required_config.get("key") or env_names[0]).lower(),
                    env_names,
                    required=live_env,
                    allowed_values=tuple(required_config.get("allowedValues", ())),
                )
            )

    payment_section = deployment_config_section(
        "payments",
        "Payments",
        payment_required_items,
        payments.get("blockingIssues", []),
        payments.get("warnings", []),
    )

    queue_section = deployment_config_section(
        "generationQueue",
        "Generation Queue",
        [],
        ["generation_queue_closed"] if queue.get("closed") else [],
    )

    product_generation_section = deployment_config_section(
        "productGeneration",
        "Product Generation Runtime",
        [
            deployment_config_item("redis_url", ("REDIS_URL",), required=live_env),
            deployment_config_item(
                "redis_product_queue",
                ("REDIS_PRODUCT_QUEUE",),
                required=False,
                recommended="product-generate",
            ),
            deployment_config_item("database_url", ("DATABASE_URL",), required=live_env),
            deployment_config_item(
                "product_worker_enabled",
                ("PRODUCT_WORKER_ENABLED",),
                required=live_env,
                recommended="true",
                allowed_values=("true",),
            ),
        ],
        product_generation.get("blockingIssues", []),
        product_generation.get("warnings", []),
    )

    product_growth_section = deployment_config_section(
        "productGrowth",
        "Growth And Finance Runtime",
        [
            deployment_config_item(
                "database_url",
                ("DATABASE_URL",),
                required=live_env,
            ),
            deployment_config_item(
                "redis_url",
                ("REDIS_URL",),
                required=live_env,
            ),
            deployment_config_item(
                "growth_event_worker_enabled",
                ("GROWTH_EVENT_WORKER_ENABLED",),
                required=live_env,
                recommended="true",
                allowed_values=("true",),
            ),
            deployment_config_item(
                "growth_invite_code_secret",
                ("GROWTH_INVITE_CODE_SECRET",),
                required=live_env,
            ),
            deployment_config_item(
                "product_growth_tenant_id",
                ("PRODUCT_GROWTH_TENANT_ID",),
                required=False,
                recommended="waimai",
            ),
        ],
        product_growth.get("blockingIssues", []),
        product_growth.get("warnings", []),
    )

    auth_section = deployment_config_section(
        "authentication",
        "Authentication",
        [
            deployment_config_item(
                "database_url",
                ("DATABASE_URL",),
                required=live_env,
            ),
            deployment_config_item(
                "redis_url",
                ("REDIS_URL",),
                required=live_env,
            ),
            deployment_config_item(
                "auth_session_hash_secret",
                ("AUTH_SESSION_HASH_SECRET",),
                required=live_env,
            ),
            deployment_config_item(
                "auth_otp_hash_secret",
                ("AUTH_OTP_HASH_SECRET",),
                required=live_env,
            ),
            deployment_config_item(
                "sms_provider",
                ("SMS_PROVIDER",),
                required=live_env,
                recommended="webhook",
            ),
            deployment_config_item(
                "sms_webhook_url",
                ("SMS_WEBHOOK_URL",),
                required=live_env
                and str(os.environ.get("SMS_PROVIDER") or "").lower()
                == "webhook",
            ),
        ],
        product_auth.get("blockingIssues", []),
        product_auth.get("warnings", []),
    )

    refinement_section = deployment_config_section(
        "imageRefinement",
        "Image Refinement Provider",
        [
            deployment_config_item(
                "gemini_api_key",
                ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                required=bool(refinement_provider.get("liveRequired")),
                description="Google Gemini API key used only by the product Worker for image revisions.",
            ),
            deployment_config_item(
                "gemini_image_edit_model",
                ("GEMINI_IMAGE_EDIT_MODEL",),
                required=False,
                recommended="gemini-3.1-flash-image",
            ),
        ],
        refinement_provider.get("blockingIssues", []),
        refinement_provider.get("warnings", []),
    )

    sections = [
        runtime_section,
        generation_section,
        object_section,
        payment_section,
        auth_section,
        queue_section,
        product_generation_section,
        product_growth_section,
        refinement_section,
    ]
    missing_required_env = sorted({env for section in sections for env in section.get("missingRequiredEnv", [])})
    blocking_issues = [issue for section in sections for issue in section.get("blockingIssues", [])]

    return {
        "ok": True,
        "ready": not missing_required_env and not blocking_issues,
        "appEnv": app_env,
        "renderDetected": render_runtime_detected(),
        "secretsRedacted": True,
        "missingRequiredEnv": missing_required_env,
        "blockingIssues": blocking_issues,
        "sections": sections,
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
            raw_bytes = resp.read(MAX_PROVIDER_JSON_BYTES + 1)
        if len(raw_bytes) > MAX_PROVIDER_JSON_BYTES:
            raise RuntimeError(f"{host} response exceeds size limit")
        raw = raw_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read(65_537)[:65_536].decode("utf-8", errors="replace")
        raise RuntimeError(f"{host} HTTP {exc.code}: {raw[:500]}") from exc
    data = json.loads(raw)
    response = data.get("Response", {})
    if "Error" in response:
        error = response["Error"]
        raise RuntimeError(f"{host} {error.get('Code', 'TencentError')}: {error.get('Message', '调用失败')}")
    return response


def tokenhub_image_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = tokenhub_config()
    model = str(cfg["model"] or "hy-image-v3.0")
    is_lite_model = "lite" in model.lower()
    body: dict[str, Any] = {
        "model": model,
        "prompt": str(payload.get("Prompt") or ""),
    }
    mappings = {
        "Resolution": "resolution",
        "RspImgType": "rsp_img_type",
        "LogoAdd": "logo_add",
    }
    if is_lite_model:
        mappings["NegativePrompt"] = "negative_prompt"
    else:
        mappings["Revise"] = "revise"
        mappings["Seed"] = "seed"
    for source_key, target_key in mappings.items():
        if source_key in payload and payload[source_key] not in (None, ""):
            body[target_key] = payload[source_key]
    images = payload.get("Images")
    if isinstance(images, list):
        normalized_images = [str(image).strip() for image in images if str(image).strip()]
        if normalized_images:
            body["images"] = normalized_images[:3]
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
            raw_bytes = resp.read(MAX_PROVIDER_JSON_BYTES + 1)
        if len(raw_bytes) > MAX_PROVIDER_JSON_BYTES:
            raise RuntimeError("TokenHub response exceeds size limit")
        raw = raw_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read(65_537)[:65_536].decode("utf-8", errors="replace")
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
        if tokenhub_ready():
            with TENCENT_TOKENHUB_GENERATION_LOCK:
                return tokenhub_image_request(payload, timeout=timeout)
        errors = []
        endpoints = [
            (TENCENT_AIART_HOST, TENCENT_AIART_SERVICE, TENCENT_AIART_VERSION),
            (TENCENT_HUNYUAN_HOST, TENCENT_HUNYUAN_SERVICE, TENCENT_HUNYUAN_VERSION),
        ]
        cloud_payload = dict(payload)
        cloud_payload.pop("Images", None)
        cloud_payload.pop("Revise", None)
        cloud_payload.pop("Seed", None)
        for host, service, version in endpoints:
            try:
                response = tencent_cloud_api_request(action, cloud_payload, host, service, version, timeout)
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


def decode_bounded_provider_image(value: str) -> bytes:
    payload = str(value or "").split(",", 1)[-1]
    if not payload or len(payload) > MAX_AI_ASSET_BASE64_CHARS:
        raise ValueError("provider image payload exceeds limit")
    raw = base64.b64decode(payload, validate=True)
    if not raw or len(raw) > MAX_AI_ASSET_BYTES:
        raise ValueError("provider image bytes exceed limit")
    return raw


def bounded_pil_image_from_bytes(
    raw: bytes,
    *,
    max_bytes: int | None = None,
    max_pixels: int | None = None,
) -> Image.Image:
    resolved_max_bytes = (
        MAX_AI_ASSET_BYTES if max_bytes is None else int(max_bytes)
    )
    resolved_max_pixels = (
        MAX_EXPORT_IMAGE_PIXELS if max_pixels is None else int(max_pixels)
    )
    if not raw or len(raw) > resolved_max_bytes:
        raise ValueError("image bytes exceed limit")
    with Image.open(io.BytesIO(raw)) as image:
        validate_image_bounds(image, max_pixels=resolved_max_pixels)
        image.load()
        return image.copy()


def validate_remote_image_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider image URL is invalid") from exc
    host = str(parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("provider image URL must use credential-free HTTPS")
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in REMOTE_IMAGE_ALLOWED_HOST_SUFFIXES
    ):
        raise ValueError("provider image host is not allowlisted")
    try:
        addresses = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("provider image host could not be resolved") from exc
    if not addresses:
        raise ValueError("provider image host has no addresses")
    for address in addresses:
        ip_text = str(address[4][0]).split("%", 1)[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("provider image host resolved to an invalid address") from exc
        if (
            not resolved_ip.is_global
            or resolved_ip.is_private
            or resolved_ip.is_loopback
            or resolved_ip.is_link_local
            or resolved_ip.is_reserved
            or resolved_ip.is_unspecified
            or resolved_ip.is_multicast
        ):
            raise ValueError("provider image host resolved to a non-public address")
    return host


class ProviderImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_remote_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_validated_remote_image(
    request: urllib.request.Request,
    timeout: float,
) -> Any:
    validate_remote_image_url(request.full_url)
    opener = urllib.request.build_opener(ProviderImageRedirectHandler())
    return opener.open(request, timeout=timeout)


def retryable_remote_image_error(exc: Exception) -> bool:
    reason: Any = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (ssl.SSLCertVerificationError, socket.gaierror)):
        return False
    return isinstance(
        reason,
        (
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.IncompleteRead,
            ssl.SSLError,
        ),
    )


def read_remote_pil_image(url: str, timeout: int = 60) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": "waimai-image-tool/1.0"})
    raw = b""
    deadline = time.monotonic() + min(
        max(1, timeout) * REMOTE_IMAGE_DOWNLOAD_MAX_ATTEMPTS,
        REMOTE_IMAGE_DOWNLOAD_TOTAL_TIMEOUT_SECONDS,
    )
    for attempt in range(1, REMOTE_IMAGE_DOWNLOAD_MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("provider image download deadline exceeded")
        try:
            with open_validated_remote_image(
                req,
                timeout=max(0.1, min(float(timeout), remaining)),
            ) as resp:
                raw = resp.read(MAX_AI_ASSET_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code in REMOTE_IMAGE_DOWNLOAD_RETRY_STATUS_CODES
            if getattr(exc, "fp", None) is not None:
                exc.close()
            if not retryable or attempt >= REMOTE_IMAGE_DOWNLOAD_MAX_ATTEMPTS:
                raise
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.IncompleteRead,
            ssl.SSLError,
        ) as exc:
            if (
                not retryable_remote_image_error(exc)
                or attempt >= REMOTE_IMAGE_DOWNLOAD_MAX_ATTEMPTS
            ):
                raise
        delay = REMOTE_IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS * attempt
        if time.monotonic() + delay >= deadline:
            raise TimeoutError("provider image download deadline exceeded")
        time.sleep(delay)
    if len(raw) > MAX_AI_ASSET_BYTES:
        raise ValueError("remote image exceeds limit")
    return bounded_pil_image_from_bytes(raw)


def read_remote_image(url: str, timeout: int = 60) -> Image.Image:
    source = read_remote_pil_image(url, timeout=timeout)
    try:
        return source.convert("RGB")
    finally:
        source.close()


def save_result_image(result_image: str, target: Path) -> None:
    temporary = target.with_name(
        f".{target.name}.{secrets.token_hex(8)}.tmp"
    )
    img: Image.Image | None = None
    failure: Exception | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            scheme = urllib.parse.urlsplit(result_image).scheme.lower()
        except ValueError:
            scheme = "https" if str(result_image).lower().startswith("https:") else ""
        if scheme in {"http", "https"}:
            img = read_remote_image(result_image)
        else:
            source = bounded_pil_image_from_bytes(
                decode_bounded_provider_image(result_image)
            )
            try:
                img = source.convert("RGB")
            finally:
                source.close()
        if target.suffix.lower() == ".png":
            img.save(temporary, "PNG", optimize=True)
        else:
            img.save(temporary, "JPEG", quality=92, optimize=True)
        os.replace(temporary, target)
    except Exception as exc:
        failure = exc
    finally:
        if img is not None:
            try:
                img.close()
            except Exception as exc:
                if failure is None:
                    failure = exc
        try:
            temporary.unlink(missing_ok=True)
        except Exception as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        if isinstance(failure, ProviderResultDownloadError):
            raise failure
        try:
            host = str(
                urllib.parse.urlsplit(result_image).hostname or "embedded"
            )
        except ValueError:
            host = "invalid"
        raise ProviderResultDownloadError(
            f"provider result image processing failed ({host})"
        ) from failure


def provider_mask_image(result_image: str) -> Image.Image:
    if result_image.startswith("http://") or result_image.startswith("https://"):
        image = read_remote_pil_image(result_image)
    else:
        image = bounded_pil_image_from_bytes(
            decode_bounded_provider_image(result_image)
        )
    if "A" in image.getbands():
        alpha = image.getchannel("A")
        if alpha.getextrema() != (255, 255):
            image.close()
            return alpha
        alpha.close()
    mask = image.convert("L")
    image.close()
    border = [
        mask.crop((0, 0, mask.width, 1)),
        mask.crop((0, mask.height - 1, mask.width, mask.height)),
        mask.crop((0, 0, 1, mask.height)),
        mask.crop((mask.width - 1, 0, mask.width, mask.height)),
    ]
    border_pixels = [value for band in border for value in band.getdata()]
    if border_pixels and sum(border_pixels) / len(border_pixels) > 127:
        mask = ImageOps.invert(mask)
    return mask


def save_result_mask(result_image: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{secrets.token_hex(8)}.tmp"
    )
    mask = provider_mask_image(result_image)
    try:
        mask.save(temporary, "PNG", optimize=True)
        os.replace(temporary, target)
    finally:
        mask.close()
        temporary.unlink(missing_ok=True)


def require_generated_output_quality(target: Path) -> dict[str, Any]:
    try:
        report = assess_generated_asset_quality(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise SelectedBackgroundError(
            "generated_image_invalid",
            "生成图片无法通过完整性检查",
        ) from exc
    if not report.get("passed"):
        reasons = ",".join(str(reason) for reason in report.get("reasons") or []) or "quality_check_failed"
        target.unlink(missing_ok=True)
        raise SelectedBackgroundError(
            "generated_image_quality_failed",
            f"生成图片质量检查未通过：{reasons}",
        )
    return report


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


def tencent_text_to_image(
    row: dict[str, Any],
    style_id: str,
    quality: str | None,
    target: Path,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any]:
    prompt_type = "combo" if row.get("kind") == "套餐/组合" else "text_to_image"
    payload: dict[str, Any] = {
        "Prompt": prompt_for_generation(row, style_id, quality, prompt_type),
        "NegativePrompt": NEGATIVE_IMAGE_PROMPT,
        "Resolution": output_resolution_for_style(style_id),
        "RspImgType": "url",
        "LogoAdd": 0,
    }
    reference_url = ""
    if selected_background is not None and tokenhub_ready():
        reference_candidate = candidate_from_path(
            selected_background.path,
            "所选背景",
            selected_background.style_id,
            "selected-background-reference",
        )
        reference_url = model_input_public_url(reference_candidate)
        if reference_url:
            payload["Images"] = [reference_url]
    response = tencent_api_request(
        "TextToImageLite",
        payload,
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
        "referenceConditioned": bool(reference_url),
        "backgroundIdentityVerified": False,
    }


def prompt_for_style_background(style_id: str) -> str:
    return background_profiles.pure_background_prompt(
        active_category_id(),
        style_id,
    )


def tencent_style_background(style_id: str, target: Path) -> dict[str, Any]:
    category_context = active_category_context()
    category_id = str(
        category_context.get("taxonomyId")
        or background_profiles.MIXED_CATEGORY_ID
    )
    prompt = background_profiles.pure_background_prompt(
        category_id,
        style_id,
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if ai_first_generation_enabled():
        response = tencent_api_request(
            "TextToImageLite",
            {
                "Prompt": prompt,
                "NegativePrompt": (
                    background_profiles.PURE_BACKGROUND_NEGATIVE_PROMPT
                ),
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
            "categoryId": category_id,
            "backgroundProfileVersion": (
                background_profiles.BACKGROUND_PROFILE_VERSION
            ),
            "promptSha256": prompt_sha256,
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
                "Prompt": prompt,
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
        "categoryId": category_id,
        "backgroundProfileVersion": (
            background_profiles.BACKGROUND_PROFILE_VERSION
        ),
        "promptSha256": prompt_sha256,
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


def chroma_foreground_fast_path_enabled() -> bool:
    return bool(
        tokenhub_ready()
        and env_truthy("EXACT_BACKGROUND_CHROMA_FAST_PATH", default=False)
    )


def prompt_for_chroma_foreground(
    row: dict[str, Any],
    quality: str | None,
) -> str:
    dish = str(row.get("name") or "外卖菜品")
    kind = str(row.get("kind") or "菜品")
    category_id = active_category_id()
    component_values = [
        str(value).strip()
        for value in row.get("components") or []
        if str(value).strip()
    ]
    semantic_source = f"{dish} {' '.join(component_values)}"
    semantic_hints: list[str] = []
    if category_id in {"mixed_rice", "topped_rice"} or any(
        word in semantic_source for word in ("拌饭", "盖饭", "盖码饭", "烤肉饭")
    ):
        semantic_hints.append(
            "这是中式外卖米饭餐，必须清楚出现白米饭，不是西式牛排拼盘"
        )
    if "烤肉" in semantic_source:
        semantic_hints.append("烤肉是切片中式蜜汁烤肉")
    if "烤排" in semantic_source:
        semantic_hints.append("烤排是切片中式黑椒无骨猪排，不是整块西式牛排")
    if "鸡排" in semantic_source:
        semantic_hints.append("鸡排是完整鸡排，不得替换成牛排")
    if "腿排" in semantic_source:
        semantic_hints.append("腿排是去骨鸡腿排，不是西式牛排")
    if "猪排" in semantic_source:
        semantic_hints.append("猪排是中式猪排，不是牛排")
    portion_match = re.search(r"([双三四])拼", dish)
    if portion_match:
        portion_count = {"双": 2, "三": 3, "四": 4}[portion_match.group(1)]
        semantic_hints.append(
            f"{portion_match.group(1)}拼必须呈现{portion_count}种不同肉类"
        )
    if re.search(r"(?:[二三四五六七八九十\d]+选一|任选|自选|可选)", semantic_source):
        semantic_hints.append(
            "标注选一、任选或自选的配菜只出现其中一种，不得同时摆出全部备选"
        )
    if kind == "套餐/组合":
        semantic_hints.append("非备选的套餐核心食材必须分别可辨，不得漏项或替换")
    semantics = "；".join(semantic_hints)
    if semantics:
        semantics = f"。菜品语义：{semantics}"
    components = ""
    if kind == "套餐/组合":
        components = f"，套餐构成：{row_components_text(row)}"
    return (
        f"真实中式外卖商品摄影，严格生成菜名“{dish}”，{kind}{components}{semantics}。"
        "菜品自然装在同一个完整餐盘、餐碗、餐盒或托盘中，"
        f"{quality_detail(quality)}，主体约占画面70%，居中完整且仅留必要抠图边距。"
        "背景必须是完全均匀的纯青色抠图幕布"
        "（RGB 0,255,255），无桌面、无墙面、无地平线、无渐变、无阴影、无反射、无道具。"
        "不要出现文字、价格、logo、水印、品牌名、人物、手、小图、相框或边框，不要裁切主体。"
    )[:CHROMA_FOREGROUND_PROMPT_MAX_CHARS]


def exact_product_identity(row: dict[str, Any]) -> str:
    def exact_text(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        return " ".join(normalized.casefold().split())

    payload = {
        "schemaVersion": 1,
        "name": exact_text(row.get("name")),
        "kind": exact_text(row.get("kind")),
        "components": [
            exact_text(value)
            for value in row.get("components") or []
            if exact_text(value)
        ],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def tencent_chroma_foreground(
    row: dict[str, Any],
    quality: str | None,
    target: Path,
) -> dict[str, Any]:
    response = tencent_api_request(
        "TextToImageLite",
        {
            "Prompt": prompt_for_chroma_foreground(row, quality),
            "NegativePrompt": (
                "文字，水印，logo，品牌名，价格，人物，手，裁切主体，复杂背景，"
                "桌面，墙面，地平线，渐变背景，阴影，反射，额外道具，拼贴，边框"
            ),
            "Resolution": default_delivery_resolution(),
            "RspImgType": "url",
            "LogoAdd": 0,
        },
    )
    save_result_image(str(response.get("ResultImage") or ""), target)
    return {
        "provider": str(response.get("_Provider") or "tencent-hunyuan"),
        "action": str(response.get("_Action") or "TextToImageLite"),
        "promptType": "chroma_foreground",
        "promptVersion": CHROMA_FOREGROUND_PROMPT_VERSION,
        "requestId": response.get("RequestId"),
        "seed": response.get("Seed"),
        "endpoint": response.get("_Endpoint"),
        "model": response.get("_Model"),
        "referenceConditioned": False,
    }


def foreground_cache_targets(
    row: dict[str, Any],
    selected_background: SelectedBackgroundAsset,
    quality: str | None,
) -> tuple[Path, Path]:
    quality_id = quality_config(quality)["id"]
    folder = (
        generation_cache_root(LIBRARY_DIR / "_foreground_sources")
        / selected_background.style_id
        / selected_background.sha256[:16]
        / quality_id
    )
    identity = exact_product_identity(row)
    return folder / f"{identity}.png", folder / f"{identity}.mask.png"


def exact_foreground_cache_lock(target: Path) -> threading.Lock:
    digest = hashlib.sha256(str(target).encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(
        EXACT_FOREGROUND_CACHE_LOCKS
    )
    return EXACT_FOREGROUND_CACHE_LOCKS[index]


def tencent_extract_foreground_mask(
    row: dict[str, Any],
    foreground_path: Path,
    mask_target: Path,
) -> dict[str, Any]:
    if not tencent_cloud_ready():
        raise SelectedBackgroundError(
            "foreground_mask_provider_not_configured",
            "商品主体 Mask 服务未配置，不能保证使用所选背景",
        )
    candidate = candidate_from_path(
        foreground_path,
        str(row.get("name") or "菜品"),
        "",
        "generated-foreground",
    )
    product_url = model_input_public_url(candidate)
    if not product_url:
        raise SelectedBackgroundError(
            "foreground_source_not_public",
            "菜品前景无法提交到 Mask 服务",
        )
    with TENCENT_MASK_EXTRACTION_LOCK:
        response = tencent_api_request(
            "ReplaceBackground",
            {
                "ProductUrl": product_url,
                "Prompt": "纯白色无纹理背景，保留菜品主体完整，不增加任何物体",
                "Product": str(row.get("name") or "菜品")[:50],
                "RspImgType": "url",
                "LogoAdd": 0,
            },
        )
    mask_image = str(response.get("MaskImage") or "")
    if not mask_image:
        raise SelectedBackgroundError(
            "foreground_mask_missing",
            "Mask 服务没有返回菜品主体 Mask",
        )
    save_result_mask(mask_image, mask_target)
    return {
        "provider": "tencent-hunyuan",
        "action": "ReplaceBackgroundMask",
        "requestId": response.get("RequestId"),
        "endpoint": response.get("_Endpoint"),
    }


def validate_selected_background_snapshot(selected_background: SelectedBackgroundAsset) -> None:
    try:
        fingerprint = image_file_fingerprint(selected_background.path)
    except (OSError, ValueError) as exc:
        raise SelectedBackgroundError(
            "selected_background_snapshot_missing",
            "所选背景快照不可用，请重新选择背景",
        ) from exc
    if not (
        hmac.compare_digest(str(fingerprint["sha256"]), selected_background.sha256)
        and int(fingerprint["width"]) == selected_background.width
        and int(fingerprint["height"]) == selected_background.height
    ):
        raise SelectedBackgroundError(
            "selected_background_snapshot_changed",
            "所选背景快照已变化，请重新选择背景",
        )


def tencent_exact_background_image(
    row: dict[str, Any],
    selected_background: SelectedBackgroundAsset,
    quality: str | None,
    target: Path,
    *,
    _foreground_lock_held: bool = False,
    _batch_failure_cache: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    if not _foreground_lock_held:
        foreground_target, _ = foreground_cache_targets(
            row,
            selected_background,
            quality,
        )
        with exact_foreground_cache_lock(foreground_target):
            failure_key = str(foreground_target)
            cached_failure = (
                _batch_failure_cache.get(failure_key)
                if _batch_failure_cache is not None
                else None
            )
            if cached_failure is not None:
                raise SelectedBackgroundError(*cached_failure)
            try:
                return tencent_exact_background_image(
                    row,
                    selected_background,
                    quality,
                    target,
                    _foreground_lock_held=True,
                    _batch_failure_cache=_batch_failure_cache,
                )
            except Exception as exc:
                if _batch_failure_cache is not None:
                    code = (
                        exc.code
                        if isinstance(exc, SelectedBackgroundError)
                        else "exact_product_generation_failed"
                    )
                    _batch_failure_cache[failure_key] = (
                        str(code),
                        str(exc)[:220] or "同款菜品生成失败",
                    )
                raise
    validate_selected_background_snapshot(selected_background)
    fast_chroma_enabled = chroma_foreground_fast_path_enabled()
    foreground_mode = (
        f"chroma-key.v{CHROMA_FOREGROUND_PROMPT_VERSION}"
        if fast_chroma_enabled
        else "reference-conditioned.v1"
    )
    mask_cache_version = (
        f"selected-background-mask.v{EXACT_BACKGROUND_MASK_CACHE_VERSION}:"
        + (
            f"chroma.v{CHROMA_EXTRACTION_VERSION}"
            if fast_chroma_enabled
            else "cloud.v1"
        )
    )
    foreground_target, mask_target = foreground_cache_targets(row, selected_background, quality)
    product_identity = exact_product_identity(row)
    foreground_metadata = load_ai_output_metadata(foreground_target) if foreground_target.exists() else None
    try:
        foreground_fingerprint = image_file_fingerprint(foreground_target) if foreground_target.exists() else None
    except (OSError, ValueError):
        foreground_fingerprint = None
    foreground_cached = bool(
        foreground_fingerprint
        and metadata_matches_selected_background(foreground_metadata, selected_background)
        and foreground_metadata
        and foreground_metadata.get("provider") == "tencent-hunyuan"
        and foreground_metadata.get("pipelineVersion") == EXACT_BACKGROUND_PIPELINE_VERSION
        and foreground_metadata.get("dishPromptVersion") == DISH_GENERATION_PROMPT_VERSION
        and foreground_metadata.get("foregroundMode") == foreground_mode
        and hmac.compare_digest(
            str(foreground_metadata.get("exactProductIdentity") or ""),
            product_identity,
        )
        and hmac.compare_digest(
            str(foreground_metadata.get("foregroundSha256") or ""),
            str(foreground_fingerprint["sha256"]),
        )
    )
    if foreground_cached:
        assert foreground_metadata is not None
        foreground_detail = dict(foreground_metadata.get("tencent") or {})
    else:
        if fast_chroma_enabled:
            foreground_detail = tencent_chroma_foreground(
                row,
                quality,
                foreground_target,
            )
        else:
            foreground_detail = tencent_text_to_image(
                row,
                selected_background.style_id,
                quality,
                foreground_target,
                selected_background,
            )
        foreground_fingerprint = image_file_fingerprint(foreground_target)
        foreground_metadata = {
            "status": "foreground_ready",
            "provider": "tencent-hunyuan",
            "pipelineVersion": EXACT_BACKGROUND_PIPELINE_VERSION,
            "dishPromptVersion": DISH_GENERATION_PROMPT_VERSION,
            "foregroundMode": foreground_mode,
            "exactProductIdentity": product_identity,
            "action": foreground_detail.get("action"),
            "promptType": foreground_detail.get("promptType"),
            "row": row.get("row"),
            "dish": row.get("name"),
            "foregroundSha256": foreground_fingerprint["sha256"],
            "tencent": foreground_detail,
            **selected_background_metadata(selected_background),
        }
        write_ai_output_metadata(foreground_target, foreground_metadata)

    try:
        mask_fingerprint = image_file_fingerprint(mask_target) if mask_target.exists() else None
    except (OSError, ValueError):
        mask_fingerprint = None
    mask_cached = bool(
        mask_fingerprint
        and foreground_cached
        and isinstance((foreground_metadata or {}).get("maskExtraction"), dict)
        and (foreground_metadata or {}).get("maskCacheVersion")
        == mask_cache_version
        and hmac.compare_digest(
            str((foreground_metadata or {}).get("maskSha256") or ""),
            str(mask_fingerprint["sha256"]),
        )
    )
    if mask_cached:
        mask_detail = dict((foreground_metadata or {}).get("maskExtraction") or {})
    else:
        if fast_chroma_enabled:
            try:
                with Image.open(foreground_target) as foreground_image:
                    chroma_result = extract_chroma_mask(foreground_image)
                mask_target.parent.mkdir(parents=True, exist_ok=True)
                mask_temporary = mask_target.with_name(
                    f".{mask_target.name}.{secrets.token_hex(8)}.tmp"
                )
                try:
                    chroma_result.mask.save(
                        mask_temporary,
                        "PNG",
                        optimize=True,
                    )
                    os.replace(mask_temporary, mask_target)
                finally:
                    chroma_result.mask.close()
                    mask_temporary.unlink(missing_ok=True)
                mask_detail = {
                    **chroma_result.metadata,
                    "pipelineVersion": EXACT_BACKGROUND_PIPELINE_VERSION,
                    "extractionVersion": CHROMA_EXTRACTION_VERSION,
                }
            except ChromaExtractionError as exc:
                mask_detail = tencent_extract_foreground_mask(
                    row,
                    foreground_target,
                    mask_target,
                )
                mask_detail = {
                    **mask_detail,
                    "fallbackFrom": "local-chroma-key",
                    "fallbackReasonCode": exc.code,
                }
        else:
            mask_detail = tencent_extract_foreground_mask(
                row,
                foreground_target,
                mask_target,
            )
        mask_fingerprint = image_file_fingerprint(mask_target)
        foreground_metadata = {
            **(foreground_metadata or {}),
            "maskExtraction": mask_detail,
            "maskCacheVersion": mask_cache_version,
            "maskSha256": mask_fingerprint["sha256"],
        }
        write_ai_output_metadata(foreground_target, foreground_metadata)

    try:
        with (
            Image.open(selected_background.path) as background_image,
            Image.open(foreground_target) as foreground_image,
            Image.open(mask_target) as mask_image,
        ):
            composition = compose_selected_background(
                background_image,
                foreground_image,
                mask_image,
                target_size=(selected_background.width, selected_background.height),
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() != ".png":
                raise SelectedBackgroundError(
                    "lossless_master_required",
                    "所选背景规范母版必须使用无损 PNG",
                )
            output_temporary = target.with_name(
                f".{target.name}.{secrets.token_hex(8)}.tmp"
            )
            try:
                composition.image.save(
                    output_temporary,
                    "PNG",
                    optimize=True,
                )
                os.replace(output_temporary, target)
            finally:
                output_temporary.unlink(missing_ok=True)
    except CompositionError as exc:
        raise SelectedBackgroundError(exc.code, str(exc)) from exc
    with Image.open(target) as persisted_output:
        persisted_background_verified = outside_mask_pixels_equal(
            composition.normalized_background,
            persisted_output,
            composition.foreground_mask,
        )
    if not persisted_background_verified:
        target.unlink(missing_ok=True)
        raise SelectedBackgroundError(
            "persisted_background_identity_mismatch",
            "持久化图片未能保留所选背景像素",
        )
    quality_report = require_generated_output_quality(target)
    output_fingerprint = image_file_fingerprint(target)

    return {
        "provider": "tencent-hunyuan",
        "action": "DeterministicBackgroundComposite",
        "promptType": foreground_detail.get("promptType"),
        "requestId": foreground_detail.get("requestId"),
        "endpoint": foreground_detail.get("endpoint"),
        "model": foreground_detail.get("model"),
        "referenceConditioned": bool(foreground_detail.get("referenceConditioned")),
        "backgroundIdentityVerified": True,
        "persistedOutputBackgroundVerified": True,
        "pipelineVersion": EXACT_BACKGROUND_PIPELINE_VERSION,
        "dishPromptVersion": DISH_GENERATION_PROMPT_VERSION,
        "outputSha256": output_fingerprint["sha256"],
        "qualityReport": quality_report,
        "composition": {
            **composition.metadata,
            "backgroundAssetId": selected_background.asset_id,
            "backgroundSha256": selected_background.sha256,
            "pipelineVersion": EXACT_BACKGROUND_PIPELINE_VERSION,
            "foregroundSha256": str((foreground_fingerprint or {}).get("sha256") or ""),
            "maskSha256": str((mask_fingerprint or {}).get("sha256") or ""),
            "foregroundCached": foreground_cached,
            "maskCached": mask_cached,
        },
        "foregroundGeneration": foreground_detail,
        "maskExtraction": mask_detail,
    }


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
        return file_sha256(path)[:12]
    except FileNotFoundError:
        return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]


def private_preview_scope_segment(
    owner_user_id: str,
    menu_upload_id: str,
) -> str:
    owner = str(owner_user_id or "").strip()
    upload_id = str(menu_upload_id or "").strip()
    if not owner or not upload_id:
        raise PreviewObjectStorageError(
            "preview_storage_context_missing",
            "私有样图缺少用户或菜单归属",
        )
    digest = hashlib.sha256(
        canonical_json(
            {
                "schemaVersion": 1,
                "ownerUserId": owner,
                "menuUploadId": upload_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"scope-{digest[:32]}"


def generation_cache_root(root: Path, *, menu_key: str = "") -> Path:
    principal = ACTIVE_PREVIEW_PRINCIPAL.get() or {}
    if bool(principal.get("localDemo")):
        return root / (menu_key or current_menu_cache_key())
    owner_user_id = str(
        ACTIVE_ASSET_OWNER_USER_ID.get()
        or principal.get("userId")
        or ""
    ).strip()
    menu_upload_id = ACTIVE_PREVIEW_MENU_UPLOAD_ID.get().strip()
    if owner_user_id or menu_upload_id:
        scope = private_preview_scope_segment(
            owner_user_id,
            menu_upload_id,
        )
        return root / scope / (menu_key or current_menu_cache_key())
    if (
        runtime_environment_label()
        in {"staging", "production", "prod", "render"}
        and not bool(app.config.get("TESTING"))
    ):
        raise PreviewObjectStorageError(
            "preview_storage_context_missing",
            "私有生成缓存缺少用户或菜单归属",
        )
    return root / (menu_key or current_menu_cache_key())


def active_category_context() -> dict[str, Any]:
    try:
        return category_report(parse_menu())
    except Exception:
        return background_profiles.menu_background_context({})


def active_category_name() -> str:
    return str(active_category_context().get("category") or "")


def active_category_id() -> str:
    return str(
        active_category_context().get("taxonomyId")
        or background_profiles.MIXED_CATEGORY_ID
    )


def category_keywords(category_name: str | None = None) -> tuple[str, ...]:
    category_id = (
        background_profiles.normalize_category_id(category_name)
        if category_name
        else active_category_id()
    )
    return tuple(
        value.lower()
        for value in background_profiles.profile_keywords(category_id)
    )


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
    return background_profiles.style_prompt(
        active_category_id(),
        style_id,
    )


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
        **style_background_prompt_metadata(style_id),
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


def local_ai_asset_target(
    kind: str,
    category: str,
    style_id: str,
    dish_name: str,
    digest: str,
    suffix: str = ".jpg",
) -> Path:
    folder = AI_ASSET_DIR / ("backgrounds" if kind == "category_background" else "products") / ai_asset_safe_part(category, "uncategorized") / safe_style_path_segment(style_id)
    safe_suffix = suffix.lower() if suffix.lower() in IMAGE_EXTS else ".jpg"
    filename = f"{ai_asset_safe_part(dish_name, kind)}_{digest[:12]}{safe_suffix}"
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
        content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        client.put_object(Bucket=cos["bucket"], Body=file_obj, Key=key, ContentType=content_type)
    public_base = os.environ.get("TENCENT_COS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return {
        "bucket": cos["bucket"],
        "region": cos["region"],
        "key": key,
        "url": f"{public_base}/{urllib.parse.quote(key, safe='/%')}" if public_base else "",
    }


class ProductAssetRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LibraryImportRequestError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class NormalizedLibraryImportImage:
    member_name: str
    standard_name: str
    data: bytes
    suffix: str
    sha256: str
    size_bytes: int
    width: int
    height: int


def product_asset_tenant_id(owner_user_id: str) -> str:
    owner = str(owner_user_id or "").strip()
    if not product_asset_library_store.IDENTIFIER_RE.fullmatch(owner):
        raise ProductAssetRuntimeError("asset_owner_invalid")
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    return f"tenant-{digest[:32]}"


def product_shared_asset_tenant_id() -> str:
    tenant_id = str(
        os.environ.get("PRODUCT_SHARED_ASSET_TENANT_ID")
        or "waimai-shared"
    ).strip()
    if not product_asset_library_store.TENANT_ID_RE.fullmatch(tenant_id):
        raise ProductAssetRuntimeError("shared_asset_tenant_invalid")
    return tenant_id


def library_import_spec() -> dict[str, str]:
    asset_kind = str(
        request.form.get("assetKind")
        or request.form.get("asset_kind")
        or ""
    ).strip().lower()
    if asset_kind not in {"background", "product"}:
        raise LibraryImportRequestError(
            "invalid_library_asset_kind",
            "assetKind must be background or product",
        )
    category_id = str(
        request.form.get("categoryId")
        or request.form.get("category_id")
        or ""
    ).strip().lower()
    if category_id not in TAXONOMY_LABELS:
        raise LibraryImportRequestError(
            "invalid_library_category",
            "categoryId must be a known taxonomy category",
        )
    if asset_kind == "product" and category_id == TAXONOMY_COMBO:
        raise LibraryImportRequestError(
            "combo_import_manifest_required",
            "套餐图片必须由带完整组件契约的正式生成流程入库",
        )
    style_id = str(
        request.form.get("styleId")
        or request.form.get("style_id")
        or "style-upload"
    ).strip().lower()
    if not product_asset_library_store.STYLE_ID_RE.fullmatch(style_id):
        raise LibraryImportRequestError(
            "invalid_library_style",
            "styleId is invalid",
        )
    background_asset_id = str(
        request.form.get("backgroundAssetId")
        or request.form.get("background_asset_id")
        or ""
    ).strip()
    background_sha256 = str(
        request.form.get("backgroundSha256")
        or request.form.get("background_sha256")
        or ""
    ).strip().lower()
    if asset_kind == "product":
        if (
            not product_asset_library_store.TENANT_ASSET_ID_RE.fullmatch(
                background_asset_id
            )
            or not product_asset_library_store.SHA256_RE.fullmatch(
                background_sha256
            )
        ):
            raise LibraryImportRequestError(
                "library_background_binding_required",
                "product imports require backgroundAssetId and backgroundSha256",
            )
    elif background_asset_id or background_sha256:
        raise LibraryImportRequestError(
            "invalid_library_background_binding",
            "background imports cannot bind another background",
        )
    return {
        "asset_kind": asset_kind,
        "category_id": category_id,
        "category_name": taxonomy_label(category_id),
        "style_id": style_id,
        "background_asset_id": background_asset_id,
        "background_sha256": background_sha256,
    }


def read_library_import_zip(file: Any) -> list[NormalizedLibraryImportImage]:
    if not file or not str(file.filename or "").lower().endswith(".zip"):
        raise LibraryImportRequestError(
            "invalid_library_zip",
            "请上传 zip 文件",
        )
    raw = file.stream.read(MAX_LIBRARY_ZIP_BYTES + 1)
    if len(raw) > MAX_LIBRARY_ZIP_BYTES:
        raise LibraryImportRequestError(
            "library_zip_too_large",
            "图库 zip 文件过大",
            status=413,
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise LibraryImportRequestError(
            "invalid_library_zip",
            "图库 zip 文件无法读取",
        ) from exc

    images: list[NormalizedLibraryImportImage] = []
    seen_members: set[str] = set()
    total_uncompressed = 0
    with archive:
        members = archive.infolist()
        if len(members) > MAX_LIBRARY_ZIP_ENTRIES:
            raise LibraryImportRequestError(
                "library_zip_entry_limit",
                "图库 zip 文件条目过多",
                status=413,
            )
        for member in members:
            if member.is_dir():
                continue
            member_name = str(member.filename or "").strip()
            if (
                not member_name
                or "\\" in member_name
                or member_name.startswith("/")
                or "//" in member_name
                or any(
                    part in {"", ".", ".."}
                    for part in member_name.split("/")
                )
                or "\x00" in member_name
            ):
                raise LibraryImportRequestError(
                    "invalid_library_zip_member",
                    "图库 zip 包含不安全路径",
                )
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise LibraryImportRequestError(
                    "invalid_library_zip_member",
                    "图库 zip 不允许符号链接",
                )
            if member.flag_bits & 0x1:
                raise LibraryImportRequestError(
                    "encrypted_library_zip_unsupported",
                    "图库 zip 不支持加密条目",
                )
            total_uncompressed += int(member.file_size)
            if total_uncompressed > MAX_LIBRARY_ZIP_UNCOMPRESSED_BYTES:
                raise LibraryImportRequestError(
                    "library_zip_uncompressed_limit",
                    "图库 zip 解压后体积过大",
                    status=413,
                )
            if (
                member.file_size > 0
                and member.file_size
                > max(1, member.compress_size)
                * MAX_LIBRARY_ZIP_COMPRESSION_RATIO
            ):
                raise LibraryImportRequestError(
                    "library_zip_compression_ratio",
                    "图库 zip 压缩比异常",
                    status=413,
                )
            suffix = Path(member_name).suffix.lower()
            if suffix not in IMAGE_EXTS:
                continue
            if member_name in seen_members:
                raise LibraryImportRequestError(
                    "duplicate_library_zip_member",
                    "图库 zip 包含重复图片路径",
                )
            if member.file_size <= 0 or member.file_size > MAX_AI_ASSET_BYTES:
                raise LibraryImportRequestError(
                    "library_image_size_invalid",
                    "图库图片体积不合法",
                    status=413,
                )
            seen_members.add(member_name)
            with archive.open(member) as source:
                payload = source.read(MAX_AI_ASSET_BYTES + 1)
            if (
                len(payload) != member.file_size
                or len(payload) > MAX_AI_ASSET_BYTES
            ):
                raise LibraryImportRequestError(
                    "library_image_size_invalid",
                    "图库图片体积不合法",
                    status=413,
                )
            images.append(
                normalize_library_import_image(member_name, payload)
            )
    if not images:
        raise LibraryImportRequestError(
            "library_zip_has_no_images",
            "图库 zip 中没有可导入图片",
        )
    if len(images) > MAX_LIBRARY_ZIP_ENTRIES:
        raise LibraryImportRequestError(
            "library_zip_entry_limit",
            "图库 zip 图片过多",
            status=413,
        )
    return sorted(images, key=lambda image: image.member_name)


def normalize_library_import_image(
    member_name: str,
    payload: bytes,
) -> NormalizedLibraryImportImage:
    try:
        with Image.open(io.BytesIO(payload)) as source:
            if str(source.format or "").upper() not in ALLOWED_IMAGE_FORMATS:
                raise ValueError("unsupported image format")
            validate_image_bounds(
                source,
                max_pixels=MAX_EXPORT_IMAGE_PIXELS,
            )
            normalized = ImageOps.exif_transpose(source)
            normalized.load()
            width, height = normalized.size
            output = io.BytesIO()
            has_alpha = (
                "A" in normalized.getbands()
                or (
                    normalized.mode == "P"
                    and "transparency" in normalized.info
                )
            )
            if has_alpha:
                normalized.convert("RGBA").save(
                    output,
                    format="PNG",
                    optimize=True,
                )
                suffix = ".png"
            else:
                normalized.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=95,
                    subsampling=0,
                    optimize=True,
                )
                suffix = ".jpg"
    except Exception as exc:
        raise LibraryImportRequestError(
            "invalid_library_image",
            f"图库图片无法解码：{member_name}",
        ) from exc
    data = output.getvalue()
    if not data or len(data) > MAX_AI_ASSET_BYTES:
        raise LibraryImportRequestError(
            "library_image_size_invalid",
            "规范化后的图库图片体积不合法",
            status=413,
        )
    standard_name = safe_filename(Path(member_name).stem)
    return NormalizedLibraryImportImage(
        member_name=member_name,
        standard_name=standard_name,
        data=data,
        suffix=suffix,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        width=width,
        height=height,
    )


def persist_postgres_library_import(
    *,
    images: list[NormalizedLibraryImportImage],
    spec: dict[str, str],
    actor_user_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    tenant_id = product_shared_asset_tenant_id()
    request_document = {
        "schemaVersion": 1,
        "tenantId": tenant_id,
        "assetKind": spec["asset_kind"],
        "categoryId": spec["category_id"],
        "styleId": spec["style_id"],
        "backgroundAssetId": spec["background_asset_id"],
        "backgroundSha256": spec["background_sha256"],
        "images": [
            {
                "memberName": image.member_name,
                "sha256": image.sha256,
                "sizeBytes": image.size_bytes,
            }
            for image in images
        ],
    }
    request_sha256 = hashlib.sha256(
        canonical_json(request_document).encode("utf-8")
    ).hexdigest()
    storage = object_storage_service.get_object_storage_service()
    prepared: list[dict[str, Any]] = []
    newly_created_object_keys: list[str] = []
    try:
        for image in images:
            asset_idempotency_key = (
                "import-"
                + hashlib.sha256(
                    canonical_json(
                        {
                            "tenantId": tenant_id,
                            "batchIdempotencyKey": idempotency_key,
                            "memberName": image.member_name,
                        }
                    ).encode("utf-8")
                ).hexdigest()
            )
            asset_id = product_asset_library_store.tenant_bound_asset_id(
                tenant_id,
                asset_idempotency_key,
            )
            object_key = object_storage_service.validate_object_key(
                f"{object_storage_service.AI_ASSETS_PREFIX}{tenant_id}/"
                f"{asset_id}/original{image.suffix}"
            )
            existed = storage.exists(object_key)
            if existed:
                stored_bytes = object_storage_service.read_object_bytes_limited(
                    storage,
                    object_key,
                    MAX_AI_ASSET_BYTES,
                )
                if not hmac.compare_digest(
                    hashlib.sha256(stored_bytes).hexdigest(),
                    image.sha256,
                ):
                    raise product_library_import_store.ProductLibraryImportConflict(
                        "existing import object content changed"
                    )
            else:
                stored_key = storage.put_bytes(
                    image.data,
                    object_key=object_key,
                )
                if stored_key != object_key:
                    raise ProductAssetRuntimeError(
                        "library_import_object_key_mismatch"
                )
                newly_created_object_keys.append(object_key)
                stored_bytes = object_storage_service.read_object_bytes_limited(
                    storage,
                    object_key,
                    MAX_AI_ASSET_BYTES,
                )
                if (
                    len(stored_bytes) != image.size_bytes
                    or not hmac.compare_digest(
                        hashlib.sha256(stored_bytes).hexdigest(),
                        image.sha256,
                    )
                ):
                    raise ProductAssetRuntimeError(
                        "library_import_object_readback_mismatch"
                    )
            prepared.append(
                {
                    "image": image,
                    "asset_id": asset_id,
                    "asset_idempotency_key": asset_idempotency_key,
                    "object_key": object_key,
                }
            )

        with postgres_connection() as connection:
            cursor = connection.cursor()
            try:
                records: list[dict[str, Any]] = []
                batch_items: list[dict[str, Any]] = []
                for item in prepared:
                    image = item["image"]
                    standard_name = (
                        f"{spec['category_name']}背景"
                        if spec["asset_kind"] == "background"
                        else image.standard_name
                    )
                    result = product_asset_library_store.register_asset(
                        cursor,
                        tenant_id=tenant_id,
                        owner_user_id=actor_user_id,
                        idempotency_key=item[
                            "asset_idempotency_key"
                        ],
                        asset_kind=spec["asset_kind"],
                        taxonomy_version=TAXONOMY_VERSION,
                        category_id=spec["category_id"],
                        category_name=spec["category_name"],
                        style_id=spec["style_id"],
                        background_asset_id=spec[
                            "background_asset_id"
                        ],
                        background_sha256=spec["background_sha256"],
                        standard_name=standard_name,
                        aliases=ai_asset_match_names(
                            standard_name,
                            [],
                        ),
                        match_keywords=ai_asset_keywords(
                            standard_name,
                            spec["category_name"],
                            [],
                        ),
                        reuse_scope="tenant",
                        source_kind="imported",
                        source_provider="admin-upload",
                        prompt_version=(
                            f"style-background.v"
                            f"{STYLE_BACKGROUND_PROMPT_VERSION}"
                            if spec["asset_kind"] == "background"
                            else f"dish-generation.v"
                            f"{DISH_GENERATION_PROMPT_VERSION}"
                        ),
                        model_name="manual-upload",
                        model_version="manual.v1",
                        pipeline_version=(
                            f"style-background.v"
                            f"{STYLE_BACKGROUND_PROMPT_VERSION}"
                            if spec["asset_kind"] == "background"
                            else f"exact-background.v"
                            f"{EXACT_BACKGROUND_PIPELINE_VERSION}"
                        ),
                        original_object_ref=item["object_key"],
                        original_sha256=image.sha256,
                        original_size_bytes=image.size_bytes,
                    )
                    records.append(result.record)
                    batch_items.append(
                        {
                            "member_name": image.member_name,
                            "asset_id": result.record["id"],
                            "object_ref": item["object_key"],
                            "object_sha256": image.sha256,
                            "object_size_bytes": image.size_bytes,
                        }
                    )
                batch = (
                    product_library_import_store.record_import_batch(
                        cursor,
                        tenant_id=tenant_id,
                        actor_user_id=actor_user_id,
                        idempotency_key=idempotency_key,
                        request_sha256=request_sha256,
                        items=batch_items,
                    )
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
    except Exception:
        for object_key in reversed(newly_created_object_keys):
            try:
                storage.delete(object_key)
            except Exception:
                app.logger.exception(
                    "Unable to clean failed library import object %s",
                    object_key,
                )
        raise
    return {
        "batchId": str(batch.batch["id"]),
        "idempotent": not batch.created,
        "tenantId": tenant_id,
        "assetCount": len(records),
        "assets": [
            product_asset_admin_view(record)
            for record in records
        ],
    }


def current_asset_owner_user_id() -> str:
    owner = str(ACTIVE_ASSET_OWNER_USER_ID.get() or "").strip()
    if not owner:
        principal = ACTIVE_PREVIEW_PRINCIPAL.get() or {}
        owner = str(principal.get("userId") or "").strip()
    if not product_asset_library_store.IDENTIFIER_RE.fullmatch(owner):
        raise ProductAssetRuntimeError("asset_owner_context_required")
    return owner


def product_asset_pipeline_version(kind: str) -> str:
    if kind == "category_background":
        return f"style-background.v{STYLE_BACKGROUND_PROMPT_VERSION}"
    if kind == "product_image":
        return f"exact-background.v{EXACT_BACKGROUND_PIPELINE_VERSION}"
    raise ProductAssetRuntimeError("asset_kind_invalid")


def product_asset_prompt_version(kind: str) -> str:
    if kind == "category_background":
        return f"style-background.v{STYLE_BACKGROUND_PROMPT_VERSION}"
    if kind == "product_image":
        return f"dish-generation.v{DISH_GENERATION_PROMPT_VERSION}"
    raise ProductAssetRuntimeError("asset_kind_invalid")


def product_asset_version_token(value: Any, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[^A-Za-z0-9._:+-]+", "-", text).strip(".-")
    return (text[:128] or fallback)[:128]


def active_menu_asset_taxonomy() -> str:
    try:
        menu = parse_menu()
    except Exception:
        return TAXONOMY_UNKNOWN
    taxonomy = str(
        background_profiles.menu_background_context(menu).get(
            "taxonomyId"
        )
        or ""
    )
    return (
        taxonomy
        if taxonomy in TAXONOMY_LABELS
        else TAXONOMY_UNKNOWN
    )


def product_asset_taxonomy(
    kind: str,
    row: dict[str, Any] | None,
) -> str:
    if kind == "category_background":
        return active_menu_asset_taxonomy()
    item = row or {}
    if str(item.get("kind") or "") == "套餐/组合":
        return TAXONOMY_COMBO
    taxonomy = str(item.get("taxonomy") or "").strip()
    if not product_asset_library_store.CATEGORY_ID_RE.fullmatch(taxonomy):
        taxonomy = ""
    if not taxonomy or taxonomy == TAXONOMY_COMBO:
        taxonomy = classify_taxonomy(
            str(item.get("name") or ""),
            "",
            str(item.get("category") or ""),
        )
    return taxonomy or TAXONOMY_UNKNOWN


def product_asset_combo_component_variants(
    source_name: str,
    component_name: str,
) -> list[dict[str, Any]]:
    source = unicodedata.normalize("NFKC", str(source_name or ""))
    name = unicodedata.normalize("NFKC", str(component_name or "")).strip()
    if not source or not name:
        return [{"name": name, "quantity": 1, "specification": ""}]

    variants: dict[str, dict[str, Any]] = {}
    for match in re.finditer(re.escape(name), source):
        tail = source[match.end() :]
        boundary = re.search(r"[+＋#&＆/／、,，|丨;；]", tail)
        fragment = tail[: boundary.start()] if boundary else tail
        fragment = re.sub(
            r"(?:单人|双人|三人|多人|家庭|分享)?套餐.*$",
            "",
            fragment,
        ).strip()
        specification = ""
        parenthetical = re.match(r"^[（(]([^）)]{1,80})[）)]", fragment)
        if parenthetical is not None:
            content = parenthetical.group(1).strip()
            if not re.search(
                r"(包含|含有|内含|搭配|附赠|赠送|任选|自选|可选|[+＋、,，])",
                content,
            ):
                specification = content
            fragment = fragment[parenthetical.end() :].strip()

        quantity = 1
        quantity_match = re.search(
            r"(?:[xX×*]\s*(\d{1,3})|(\d{1,3})\s*(?:份|个|只|杯|瓶|罐|盒|件))",
            fragment,
        )
        if quantity_match is not None:
            quantity = int(
                quantity_match.group(1)
                or quantity_match.group(2)
                or "1"
            )
            if not 1 <= quantity <= (
                product_asset_library_store.MAX_COMBO_COMPONENT_QUANTITY
            ):
                raise ProductAssetRuntimeError(
                    "combo_component_quantity_invalid"
                )

        if not specification:
            specification_match = re.search(
                r"(加大份|超大份|大份|中份|小份|迷你份|"
                r"\d+(?:\.\d+)?\s*(?:ml|毫升|l|克|g|kg|斤|两|寸))",
                fragment,
                flags=re.IGNORECASE,
            )
            if specification_match is not None:
                specification = specification_match.group(1).strip()

        key = unicodedata.normalize(
            "NFKC",
            specification,
        ).strip().lower()
        existing = variants.get(key)
        if existing is None:
            variants[key] = {
                "name": name,
                "quantity": quantity,
                "specification": specification[:240],
            }
        else:
            existing["quantity"] = int(existing["quantity"]) + quantity

    return list(variants.values()) or [
        {"name": name, "quantity": 1, "specification": ""}
    ]


def product_asset_combo_components(
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    names = [
        str(value).strip()
        for value in row.get("components") or []
        if str(value).strip()
    ]
    if len(names) < 2:
        raise ProductAssetRuntimeError("combo_components_incomplete")
    variants = [
        variant
        for name in names
        for variant in product_asset_combo_component_variants(
            str(row.get("name") or ""),
            name,
        )
    ]

    drink_taxonomies = {
        "milk_fruit_tea",
        "coffee_cocoa",
        "bottled_drinks",
        "fresh_drinks",
    }
    staple_taxonomies = {
        "topped_rice",
        "mixed_rice",
        "porridge_soup_rice",
        "rice_noodles",
        "wheat_noodles",
        "dumpling_wonton",
        "buns_dim_sum",
        "chinese_wraps",
        "burger_hotdog",
        "pizza",
        "sandwich_bagel",
        "pasta_steak",
    }
    roles: list[str] = []
    for variant in variants:
        name = str(variant["name"])
        taxonomy = classify_taxonomy(name)
        normalized = normalize(name)
        if taxonomy in drink_taxonomies:
            role = "drink"
        elif taxonomy == "dessert_bakery":
            role = "dessert"
        elif taxonomy in staple_taxonomies or re.fullmatch(
            r"(白)?米饭|杂粮饭|糙米饭|珍珠饭",
            normalized,
        ):
            role = "staple"
        elif re.search(r"(酱|汁|蘸料)$", normalized):
            role = "condiment"
        else:
            role = "side"
        roles.append(role)

    main_index = next(
        (
            index
            for index, role in enumerate(roles)
            if role not in {"drink", "dessert", "condiment", "staple"}
        ),
        0,
    )
    roles[main_index] = "main"
    return [
        {
            "name": str(variant["name"]),
            "role": roles[index],
            "quantity": int(variant["quantity"]),
            **(
                {"specification": str(variant["specification"])}
                if str(variant.get("specification") or "")
                else {}
            ),
        }
        for index, variant in enumerate(variants)
    ]


def product_asset_registration_fields(
    *,
    kind: str,
    row: dict[str, Any] | None,
    dish_name: str,
) -> dict[str, Any]:
    taxonomy = product_asset_taxonomy(kind, row)
    if kind == "category_background":
        standard_name = f"{taxonomy_label(taxonomy)}背景"
        components: list[dict[str, Any]] | None = None
    else:
        standard_name = str(
            dish_name or (row or {}).get("name") or ""
        ).strip()
        components = (
            product_asset_combo_components(row or {})
            if taxonomy == TAXONOMY_COMBO
            else None
        )
    if not standard_name:
        raise ProductAssetRuntimeError("asset_standard_name_required")
    raw_components = [
        str(value)
        for value in (row or {}).get("components") or []
        if str(value).strip()
    ]
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "category_id": taxonomy,
        "category_name": taxonomy_label(taxonomy),
        "standard_name": standard_name,
        "aliases": ai_asset_match_names(standard_name, raw_components),
        "match_keywords": ai_asset_keywords(
            standard_name,
            taxonomy_label(taxonomy),
            raw_components,
        ),
        "combo_components": components,
    }


def product_asset_background_binding(
    kind: str,
    metadata: dict[str, Any],
) -> tuple[str, str]:
    if kind == "category_background":
        return "", ""
    background_asset_id = str(
        metadata.get("backgroundLibraryAssetId")
        or metadata.get("backgroundAssetId")
        or ""
    ).strip()
    background_sha256 = str(
        metadata.get("backgroundSha256") or ""
    ).strip().lower()
    if (
        not product_asset_library_store.IDENTIFIER_RE.fullmatch(
            background_asset_id
        )
        or not product_asset_library_store.SHA256_RE.fullmatch(
            background_sha256
        )
    ):
        raise ProductAssetRuntimeError(
            "asset_exact_background_binding_required"
        )
    return background_asset_id, background_sha256


def product_asset_source_versions(
    kind: str,
    metadata: dict[str, Any],
) -> dict[str, str]:
    provider_detail = (
        metadata.get("tencent")
        if isinstance(metadata.get("tencent"), dict)
        else {}
    )
    model = str(
        provider_detail.get("model")
        or metadata.get("model")
        or "hy-image-v3.0"
    ).strip()
    return {
        "source_provider": str(
            metadata.get("provider") or "tencent-hunyuan"
        )[:128],
        "prompt_version": product_asset_prompt_version(kind),
        "model_name": (
            re.sub(r"[^A-Za-z0-9._:+-]+", "-", model).strip(".-")
            or "hy-image"
        )[:128],
        "model_version": product_asset_version_token(
            model,
            "hy-image-v3.0",
        ),
        "pipeline_version": product_asset_pipeline_version(kind),
    }


def persist_postgres_ai_generated_asset(
    *,
    kind: str,
    source_path: Path,
    style_id: str,
    metadata: dict[str, Any],
    row: dict[str, Any] | None,
    dish_name: str,
) -> dict[str, Any]:
    owner_user_id = current_asset_owner_user_id()
    tenant_id = product_asset_tenant_id(owner_user_id)
    try:
        source_bytes = object_storage_service.read_file_bytes_limited(
            source_path,
            MAX_AI_ASSET_BYTES,
        )
    except (
        FileNotFoundError,
        object_storage_service.ObjectStorageReadLimitExceeded,
    ) as exc:
        raise ProductAssetRuntimeError(
            "asset_file_size_invalid"
        ) from exc
    source_size = len(source_bytes)
    if source_size <= 0:
        raise ProductAssetRuntimeError("asset_file_size_invalid")
    fingerprint = image_bytes_fingerprint(source_bytes)
    registration = product_asset_registration_fields(
        kind=kind,
        row=row,
        dish_name=dish_name,
    )
    background_asset_id, background_sha256 = (
        product_asset_background_binding(kind, metadata)
    )
    versions = product_asset_source_versions(kind, metadata)
    idempotency_basis = {
        "kind": kind,
        "ownerUserId": owner_user_id,
        "taxonomyVersion": registration["taxonomy_version"],
        "categoryId": registration["category_id"],
        "styleId": style_id,
        "standardName": registration["standard_name"],
        "backgroundAssetId": background_asset_id,
        "backgroundSha256": background_sha256,
        "pipelineVersion": versions["pipeline_version"],
        "outputSha256": fingerprint["sha256"],
    }
    idempotency_key = (
        "asset-"
        + hashlib.sha256(
            canonical_json(idempotency_basis).encode("utf-8")
        ).hexdigest()
    )
    asset_id = product_asset_library_store.tenant_bound_asset_id(
        tenant_id,
        idempotency_key,
    )
    suffix = (
        source_path.suffix.lower()
        if source_path.suffix.lower() in IMAGE_EXTS
        else ".image"
    )
    object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.AI_ASSETS_PREFIX}{tenant_id}/"
        f"{asset_id}/original{suffix}"
    )
    storage = object_storage_service.get_object_storage_service()
    with tempfile.TemporaryDirectory(
        prefix="ai-asset-upload-snapshot-"
    ) as snapshot_folder:
        snapshot_path = Path(snapshot_folder) / f"asset{suffix}"
        snapshot_path.write_bytes(source_bytes)
        stored_key = object_storage_service.put_object_file_limited(
            storage,
            snapshot_path,
            object_key=object_key,
            max_bytes=source_size,
        )
    with tempfile.TemporaryDirectory(prefix="ai-asset-readback-") as folder:
        verified_path = Path(folder) / f"asset{suffix}"
        object_storage_service.download_object_file_limited(
            storage,
            stored_key,
            verified_path,
            source_size,
        )
        verified_size = verified_path.stat().st_size
        if verified_size != source_size:
            raise ProductAssetRuntimeError("asset_storage_size_mismatch")
        verified = image_file_fingerprint(verified_path)
        if not hmac.compare_digest(
            str(verified["sha256"]),
            str(fingerprint["sha256"]),
        ):
            raise ProductAssetRuntimeError("asset_storage_sha256_mismatch")

    combo_components = registration["combo_components"]
    combo_version = (
        product_asset_library_store.COMBO_FINGERPRINT_VERSION
        if combo_components is not None
        else ""
    )
    with postgres_connection() as connection:
        result = product_asset_library_store.ProductAssetLibraryStore(
            connection
        ).register_asset(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            asset_kind=(
                "background"
                if kind == "category_background"
                else "product"
            ),
            taxonomy_version=registration["taxonomy_version"],
            category_id=registration["category_id"],
            category_name=registration["category_name"],
            style_id=style_id,
            background_asset_id=background_asset_id,
            background_sha256=background_sha256,
            standard_name=registration["standard_name"],
            aliases=registration["aliases"],
            match_keywords=registration["match_keywords"],
            reuse_scope="owner",
            source_kind="generated",
            original_object_ref=stored_key,
            original_sha256=str(fingerprint["sha256"]),
            original_size_bytes=source_size,
            combo_fingerprint_version=combo_version,
            combo_components=combo_components,
            **versions,
        )
    record = result.record
    return {
        "assetId": str(record["id"]),
        "status": str(record["status"]),
        "reviewStatus": str(record["review_status"]),
        "created": result.created,
        "storageProvider": "private-object-storage",
        "objectKey": str(record["original_object_ref"]),
        "sha256": str(record["original_sha256"]),
        "fileSize": int(record["original_size_bytes"]),
        "category": str(record["category_name"]),
        "categoryId": str(record["category_id"]),
        "styleId": str(record["style_id"]),
        "productName": str(record["standard_name"]),
        "pipelineVersion": str(record["pipeline_version"]),
    }


def postgres_reusable_asset_record(
    *,
    kind: str,
    style_id: str,
    row: dict[str, Any] | None = None,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any] | None:
    if not postgres_product_runtime_enabled() or not ai_asset_library_enabled():
        return None
    owner_user_id = current_asset_owner_user_id()
    tenant_id = product_asset_tenant_id(owner_user_id)
    registration = product_asset_registration_fields(
        kind=kind,
        row=row,
        dish_name=str((row or {}).get("name") or ""),
    )
    if kind == "product_image":
        if selected_background is None:
            return None
        background_asset_id = (
            selected_background.library_asset_id
            or selected_background.asset_id
        )
        background_sha256 = selected_background.sha256
    else:
        background_asset_id = ""
        background_sha256 = ""
    combo_components = registration["combo_components"]
    with postgres_connection() as connection:
        store = product_asset_library_store.ProductAssetLibraryStore(
            connection
        )
        query = {
            "owner_user_id": owner_user_id,
            "taxonomy_version": registration["taxonomy_version"],
            "category_id": registration["category_id"],
            "style_id": style_id,
            "background_asset_id": background_asset_id,
            "background_sha256": background_sha256,
            "standard_name": registration["standard_name"],
            "asset_kind": (
                "background"
                if kind == "category_background"
                else "product"
            ),
            "pipeline_version": product_asset_pipeline_version(kind),
            "combo_fingerprint_version": (
                product_asset_library_store.COMBO_FINGERPRINT_VERSION
                if combo_components is not None
                else ""
            ),
            "combo_components": combo_components,
            "limit": 1,
        }
        records = store.find_reusable_assets(
            tenant_id=tenant_id,
            include_tenant_scope=False,
            **query,
        )
        if not records:
            records = store.find_reusable_assets(
                tenant_id=product_shared_asset_tenant_id(),
                include_tenant_scope=True,
                **query,
            )
    return records[0] if records else None


def materialize_postgres_asset_record(
    record: dict[str, Any],
    target: Path,
) -> dict[str, Any]:
    expected_size = int(record.get("original_size_bytes") or 0)
    expected_sha256 = str(record.get("original_sha256") or "")
    if (
        expected_size <= 0
        or expected_size > MAX_AI_ASSET_BYTES
        or not product_asset_library_store.SHA256_RE.fullmatch(
            expected_sha256
        )
    ):
        raise ProductAssetRuntimeError("asset_record_integrity_invalid")
    object_key = object_storage_service.validate_object_key(
        str(record.get("original_object_ref") or "")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        object_storage_service.download_object_file_limited(
            object_storage_service.get_object_storage_service(),
            object_key,
            temporary,
            expected_size,
        )
        if temporary.stat().st_size != expected_size:
            raise ProductAssetRuntimeError("asset_object_size_mismatch")
        fingerprint = image_file_fingerprint(temporary)
        if not hmac.compare_digest(
            str(fingerprint["sha256"]),
            expected_sha256,
        ):
            raise ProductAssetRuntimeError("asset_object_sha256_mismatch")
        os.replace(temporary, target)
        return fingerprint
    finally:
        temporary.unlink(missing_ok=True)


def materialize_reusable_product_asset(
    row: dict[str, Any],
    selected_style: str,
    selected_background: SelectedBackgroundAsset,
    quality: str | None,
    target: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        record = postgres_reusable_asset_record(
            kind="product_image",
            style_id=selected_style,
            row=row,
            selected_background=selected_background,
        )
        if record is None:
            return None
        if target is None:
            candidate, output_target = ai_output_candidate(
                row,
                selected_style,
                quality,
                "asset-library",
                selected_background,
            )
        else:
            output_target = target
            candidate = candidate_from_path(
                target,
                str(row.get("name") or ""),
                selected_style,
                "approved-product-asset",
                100.0,
            )
        fingerprint = materialize_postgres_asset_record(
            record,
            output_target,
        )
    except ProductAssetRuntimeError:
        raise
    except Exception as exc:
        raise ProductAssetRuntimeError(
            "asset_library_unavailable"
        ) from exc
    metadata = {
        "status": "succeeded",
        "provider": "asset-library",
        "action": "ApprovedAssetReuse",
        "reason": "exact_approved_asset_reuse",
        "row": row.get("row"),
        "dish": row.get("name"),
        "assetRecordId": str(record["id"]),
        "backgroundIdentityVerified": True,
        "persistedOutputBackgroundVerified": True,
        "pipelineVersion": EXACT_BACKGROUND_PIPELINE_VERSION,
        "outputSha256": str(fingerprint["sha256"]),
        **selected_background_metadata(selected_background),
    }
    write_ai_output_metadata(output_target, metadata)
    candidate_generation_metadata(candidate, metadata)
    return candidate, metadata


def materialize_reusable_background_asset(
    style_id: str,
    target: Path,
    *,
    record: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        if record is None:
            record = postgres_reusable_asset_record(
                kind="category_background",
                style_id=style_id,
            )
        if record is None:
            return None
        fingerprint = materialize_postgres_asset_record(record, target)
    except ProductAssetRuntimeError:
        raise
    except Exception as exc:
        raise ProductAssetRuntimeError(
            "asset_library_unavailable"
        ) from exc
    metadata = {
        "status": "succeeded",
        "provider": "asset-library",
        "action": "ApprovedBackgroundReuse",
        "promptType": "style_background",
        "styleId": style_id,
        **style_background_prompt_metadata(style_id),
        "assetRecordId": str(record["id"]),
        "outputSha256": str(fingerprint["sha256"]),
    }
    write_ai_output_metadata(target, metadata)
    return record, metadata


def object_storage_background_catalog_manifest(
    base: dict[str, Any],
    *,
    category_id: str,
    prompt_version: str,
) -> dict[str, Any]:
    empty = {
        **base,
        "status": "incomplete",
        "ready": False,
        "code": "background_catalog_incomplete",
        "approvedCount": 0,
        "missingStyleIds": list(background_catalog.STYLE_IDS),
        "duplicateStyleIds": [],
        "pendingCount": 0,
        "rejectedCount": 0,
        "assets": [],
        "_recordsByStyle": {},
    }
    readiness = object_storage_service.assess_object_storage_readiness()
    if not readiness.get("ready"):
        return {
            **empty,
            "status": "unavailable",
            "code": "background_catalog_unavailable",
        }
    key = background_catalog.catalog_manifest_key(
        category_id,
        prompt_version,
        tenant_id=product_shared_asset_tenant_id(),
    )
    try:
        raw = object_storage_service.read_object_bytes_limited_if_exists(
            object_storage_service.get_object_storage_service(),
            key,
            2 * 1024 * 1024,
        )
        if raw is None:
            return empty
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        app.logger.error(
            "Background catalog manifest read failed for %s: %s",
            category_id,
            type(exc).__name__,
        )
        return {
            **empty,
            "status": "unavailable",
            "code": "background_catalog_unavailable",
        }
    if not isinstance(document, dict):
        return empty
    expected_document = {
        "schemaVersion": background_catalog.CATALOG_SCHEMA_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": TAXONOMY_VERSION,
        "categoryId": category_id,
        "promptVersion": prompt_version,
    }
    if any(document.get(name) != value for name, value in expected_document.items()):
        return empty

    catalog_approved = str(
        document.get("reviewStatus") or ""
    ).lower() == "approved"
    entries: list[dict[str, Any]] = []
    records_by_style: dict[str, list[dict[str, Any]]] = {}
    invalid_style_ids: list[str] = []
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > 100:
        return empty
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            continue
        style_id = str(raw_asset.get("styleId") or "")
        if style_id not in background_catalog.STYLE_IDS:
            continue
        prompt = background_profiles.pure_background_prompt(
            category_id,
            style_id,
        )
        prompt_sha256 = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        asset_sha256 = str(raw_asset.get("sha256") or "").lower()
        object_key = str(raw_asset.get("objectKey") or "")
        try:
            file_size = int(raw_asset.get("fileSize") or 0)
        except (TypeError, ValueError):
            invalid_style_ids.append(style_id)
            continue
        suffix = Path(object_key).suffix.lower()
        try:
            expected_key = background_catalog.catalog_object_key(
                category_id=category_id,
                style_id=style_id,
                prompt_version=prompt_version,
                prompt_sha256=prompt_sha256,
                asset_sha256=asset_sha256,
                suffix=suffix,
                tenant_id=product_shared_asset_tenant_id(),
            )
        except (TypeError, ValueError):
            invalid_style_ids.append(style_id)
            continue
        valid = bool(
            str(raw_asset.get("catalogVersion") or "")
            == background_catalog.CATALOG_VERSION
            and str(raw_asset.get("taxonomyVersion") or "")
            == TAXONOMY_VERSION
            and str(raw_asset.get("categoryId") or "") == category_id
            and str(raw_asset.get("promptVersion") or "")
            == prompt_version
            and str(raw_asset.get("promptSha256") or "").lower()
            == prompt_sha256
            and object_key == expected_key
            and 0 < file_size <= MAX_AI_ASSET_BYTES
        )
        if not valid:
            invalid_style_ids.append(style_id)
            continue
        review_status = str(
            raw_asset.get("reviewStatus") or "pending"
        ).lower()
        if not catalog_approved:
            review_status = "pending"
        entry = {
            "catalogVersion": background_catalog.CATALOG_VERSION,
            "taxonomyVersion": TAXONOMY_VERSION,
            "categoryId": category_id,
            "categoryName": background_catalog.category_label(category_id),
            "styleId": style_id,
            "styleSlotId": background_catalog.style_slot(style_id).slot_id,
            "promptVersion": prompt_version,
            "promptSha256": prompt_sha256,
            "pipelineVersion": prompt_version,
            "provider": str(raw_asset.get("provider") or ""),
            "model": str(raw_asset.get("model") or ""),
            "assetRecordId": str(
                raw_asset.get("assetRecordId")
                or f"cos-catalog-{asset_sha256[:32]}"
            ),
            "sha256": asset_sha256,
            "fileSize": file_size,
            "reviewStatus": review_status,
            "reviewedAt": raw_asset.get("reviewedAt"),
            "createdAt": raw_asset.get("createdAt"),
        }
        entries.append(entry)
        if review_status == "approved":
            records_by_style.setdefault(style_id, []).append(
                {
                    "id": entry["assetRecordId"],
                    "taxonomy_version": TAXONOMY_VERSION,
                    "category_id": category_id,
                    "category_name": entry["categoryName"],
                    "style_id": style_id,
                    "prompt_version": prompt_version,
                    "pipeline_version": prompt_version,
                    "source_provider": entry["provider"],
                    "model_name": entry["model"],
                    "original_object_ref": object_key,
                    "original_sha256": asset_sha256,
                    "original_size_bytes": file_size,
                    "review_status": "approved",
                    "reviewed_at": entry["reviewedAt"],
                    "created_at": entry["createdAt"],
                }
            )

    status = background_catalog.category_manifest_status(
        entries,
        category_id=category_id,
        prompt_version=prompt_version,
    )
    if invalid_style_ids:
        status = {
            **status,
            "status": "incomplete",
            "ready": False,
            "invalidStyleIds": sorted(set(invalid_style_ids)),
        }
    unique_records = {
        style_id: matches[0]
        for style_id, matches in records_by_style.items()
        if len(matches) == 1
    }
    return {
        **base,
        **status,
        "code": "" if status["ready"] else "background_catalog_incomplete",
        "_recordsByStyle": unique_records,
    }


def approved_background_catalog_manifest() -> dict[str, Any]:
    context = active_category_context()
    category_id = str(context.get("taxonomyId") or "")
    prompt_version = product_asset_prompt_version("category_background")
    backend = background_catalog_manifest_backend()
    base = {
        "mode": "approved",
        "schemaVersion": background_catalog.CATALOG_SCHEMA_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": TAXONOMY_VERSION,
        "categoryId": category_id,
        "categoryName": str(context.get("category") or "复合餐饮"),
        "categoryConfidence": int(context.get("confidence") or 0),
        "categorySelectionReason": str(
            context.get("selectionReason") or ""
        ),
        "promptVersion": prompt_version,
        "manifestBackend": backend,
    }
    if category_id not in background_catalog.CATEGORY_LABELS:
        return {
            **base,
            "status": "classification_review",
            "ready": False,
            "code": "background_category_review_required",
            "approvedCount": 0,
            "missingStyleIds": list(background_catalog.STYLE_IDS),
            "duplicateStyleIds": [],
            "assets": [],
            "_recordsByStyle": {},
        }
    if backend == "object-storage":
        return object_storage_background_catalog_manifest(
            base,
            category_id=category_id,
            prompt_version=prompt_version,
        )
    if backend != "postgres":
        return {
            **base,
            "status": "unavailable",
            "ready": False,
            "code": "background_catalog_unavailable",
            "approvedCount": 0,
            "missingStyleIds": list(background_catalog.STYLE_IDS),
            "duplicateStyleIds": [],
            "assets": [],
            "_recordsByStyle": {},
        }
    if not postgres_product_runtime_enabled() or not ai_asset_library_enabled():
        return {
            **base,
            "status": "unavailable",
            "ready": False,
            "code": "background_catalog_unavailable",
            "approvedCount": 0,
            "missingStyleIds": list(background_catalog.STYLE_IDS),
            "duplicateStyleIds": [],
            "assets": [],
            "_recordsByStyle": {},
        }

    owner_user_id = current_asset_owner_user_id()
    try:
        with postgres_connection() as connection:
            records = product_asset_library_store.ProductAssetLibraryStore(
                connection
            ).list_approved_background_catalog(
                tenant_id=product_shared_asset_tenant_id(),
                owner_user_id=owner_user_id,
                taxonomy_version=TAXONOMY_VERSION,
                category_id=category_id,
                pipeline_version=product_asset_pipeline_version(
                    "category_background"
                ),
                style_ids=background_catalog.STYLE_IDS,
                include_tenant_scope=True,
            )
    except Exception as exc:
        app.logger.error(
            "Approved background catalog lookup failed for %s: %s",
            category_id,
            type(exc).__name__,
        )
        return {
            **base,
            "status": "unavailable",
            "ready": False,
            "code": "background_catalog_unavailable",
            "approvedCount": 0,
            "missingStyleIds": list(background_catalog.STYLE_IDS),
            "duplicateStyleIds": [],
            "assets": [],
            "_recordsByStyle": {},
        }

    entries = []
    records_by_style: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        style_id = str(record.get("style_id") or "")
        records_by_style.setdefault(style_id, []).append(record)
        prompt = background_profiles.pure_background_prompt(
            category_id,
            style_id,
        )
        entries.append(
            {
                "catalogVersion": background_catalog.CATALOG_VERSION,
                "taxonomyVersion": str(
                    record.get("taxonomy_version") or ""
                ),
                "categoryId": str(record.get("category_id") or ""),
                "categoryName": str(record.get("category_name") or ""),
                "styleId": style_id,
                "styleSlotId": background_catalog.style_slot(
                    style_id
                ).slot_id,
                "promptVersion": str(record.get("prompt_version") or ""),
                "promptSha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                "pipelineVersion": str(
                    record.get("pipeline_version") or ""
                ),
                "provider": str(record.get("source_provider") or ""),
                "model": str(record.get("model_name") or ""),
                "assetRecordId": str(record.get("id") or ""),
                "sha256": str(record.get("original_sha256") or ""),
                "fileSize": int(record.get("original_size_bytes") or 0),
                "reviewStatus": str(record.get("review_status") or ""),
                "reviewedAt": record.get("reviewed_at"),
                "createdAt": record.get("created_at"),
            }
        )

    status = background_catalog.category_manifest_status(
        entries,
        category_id=category_id,
        prompt_version=prompt_version,
    )
    unique_records = {
        style_id: matches[0]
        for style_id, matches in records_by_style.items()
        if len(matches) == 1
    }
    return {
        **base,
        **status,
        "code": "" if status["ready"] else "background_catalog_incomplete",
        "_recordsByStyle": unique_records,
    }


def image_file_fingerprint(path: Path) -> dict[str, Any]:
    try:
        raw = object_storage_service.read_file_bytes_limited(
            path,
            MAX_AI_ASSET_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise ValueError("image file size exceeds limit") from exc
    return image_bytes_fingerprint(raw)


def image_bytes_fingerprint(
    raw: bytes,
    *,
    max_bytes: int | None = None,
    max_pixels: int | None = None,
) -> dict[str, Any]:
    img = bounded_pil_image_from_bytes(
        raw,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )
    try:
        width, height = img.size
    finally:
        img.close()
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
    product_name = str(
        dish_name
        or (row or {}).get("name")
        or (
            "背景风格样图"
            if kind == "category_background"
            else "未命名菜品"
        )
    )
    if postgres_product_runtime_enabled():
        try:
            return persist_postgres_ai_generated_asset(
                kind=kind,
                source_path=source_path,
                style_id=style_id,
                metadata=metadata,
                row=row,
                dish_name=product_name,
            )
        except Exception as exc:
            app.logger.error(
                "AI asset persistence failed for %s/%s: %s",
                kind,
                style_id,
                type(exc).__name__,
            )
            return None
    category = str(metadata.get("category") or active_category_name() or "待人工确认")
    digest = image_file_fingerprint(source_path)["sha256"]
    stored_path = local_ai_asset_target(
        kind,
        category,
        style_id,
        product_name,
        digest,
        source_path.suffix,
    )
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


def safely_persist_ai_generated_asset(**kwargs: Any) -> dict[str, Any] | None:
    try:
        return persist_ai_generated_asset(**kwargs)
    except Exception as exc:
        app.logger.error(
            "AI asset cache write failed for %s/%s: %s",
            kwargs.get("kind"),
            kwargs.get("style_id"),
            type(exc).__name__,
        )
        return None


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
    active_path = ACTIVE_MENU_PATH.get()
    if active_path is not None:
        return active_path
    files = sorted((p for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in MENU_EXTS), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


@contextmanager
def active_menu_path(path: Path):
    token = ACTIVE_MENU_PATH.set(path)
    try:
        yield
    finally:
        ACTIVE_MENU_PATH.reset(token)


@contextmanager
def active_asset_owner(user_id: str):
    token = ACTIVE_ASSET_OWNER_USER_ID.set(str(user_id or "").strip())
    try:
        yield
    finally:
        ACTIVE_ASSET_OWNER_USER_ID.reset(token)


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
    return background_profiles.menu_background_context(menu)


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
        relative_name = path.relative_to(LIBRARY_DIR).as_posix()
        return f"/media/{urllib.parse.quote(relative_name, safe='/')}"
    except ValueError:
        if not external_library_media_enabled():
            return ""
        image_id = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:18]
        return f"/external-media/{image_id}{path.suffix.lower()}"


PRIVATE_MEDIA_PREFIXES = frozenset(
    (
        "_ai_outputs",
        "_generated_previews",
        "_selected_backgrounds",
        "_selected_background_assets",
        "_style_backgrounds",
    )
)
DURABLE_PRIVATE_PREVIEW_PREFIXES = frozenset(
    ("_style_backgrounds", "_generated_previews")
)
PUBLIC_MEDIA_PREFIXES = frozenset(("seed_public", "demo_store"))
PUBLIC_MEDIA_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".webp"))
PRIVATE_PREVIEW_STORAGE_SCHEMA_VERSION = 1


def library_media_relative_name(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/media/"):
        return ""
    return urllib.parse.unquote(parsed.path[len("/media/") :])


def safe_library_media_path(
    name: str,
    *,
    allowed_prefixes: frozenset[str],
    require_file: bool = True,
) -> Path | None:
    if "\\" in name:
        return None
    relative = Path(name)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) < 2
        or relative.parts[0] not in allowed_prefixes
        or relative.suffix.lower() not in PUBLIC_MEDIA_EXTENSIONS
    ):
        return None
    allowed_root = (LIBRARY_DIR / relative.parts[0]).resolve()
    target = (LIBRARY_DIR / relative).resolve()
    try:
        target.relative_to(allowed_root)
    except ValueError:
        return None
    return target if not require_file or target.is_file() else None


def private_preview_relative_name(target: Path) -> str:
    try:
        relative = target.resolve().relative_to(LIBRARY_DIR.resolve())
    except ValueError:
        return ""
    if (
        len(relative.parts) < 2
        or relative.parts[0] not in DURABLE_PRIVATE_PREVIEW_PREFIXES
        or relative.suffix.lower() not in PUBLIC_MEDIA_EXTENSIONS
    ):
        return ""
    return object_storage_service.validate_object_key(relative.as_posix())


def private_preview_storage_context(
    target: Path,
) -> dict[str, Any] | None:
    relative_name = private_preview_relative_name(target)
    if not relative_name:
        return None
    principal = ACTIVE_PREVIEW_PRINCIPAL.get()
    if (
        runtime_environment_label()
        not in {"staging", "production", "prod", "render"}
        or (
            isinstance(principal, dict)
            and bool(principal.get("localDemo"))
        )
    ):
        return None
    if not isinstance(principal, dict):
        raise PreviewObjectStorageError(
            "preview_storage_context_missing",
            "私有样图存储上下文缺失",
        )
    user_id = str(principal.get("userId") or "").strip()
    menu_upload_id = ACTIVE_PREVIEW_MENU_UPLOAD_ID.get().strip()
    if not user_id or not menu_upload_id:
        raise PreviewObjectStorageError(
            "preview_storage_context_missing",
            "私有样图缺少用户或菜单归属",
        )
    expected_scope = private_preview_scope_segment(
        user_id,
        menu_upload_id,
    )
    relative_parts = Path(relative_name).parts
    if (
        len(relative_parts) < 3
        or not hmac.compare_digest(
            str(relative_parts[1]),
            expected_scope,
        )
    ):
        raise PreviewObjectStorageError(
            "preview_object_scope_mismatch",
            "私有样图缓存归属校验失败",
        )
    readiness = object_storage_service.assess_object_storage_readiness()
    if not readiness.get("ready") or readiness.get("mode") != "remote_private":
        raise PreviewObjectStorageError(
            "preview_object_storage_unavailable",
            "私有对象存储未就绪",
        )
    try:
        storage = object_storage_service.get_object_storage_service()
        object_key = object_storage_service.private_preview_object_key(
            user_id,
            menu_upload_id,
            relative_name,
        )
    except Exception as exc:
        raise PreviewObjectStorageError(
            "preview_object_storage_unavailable",
            "私有对象存储暂时不可用",
        ) from exc
    return {
        "storage": storage,
        "relativeName": relative_name,
        "objectKey": object_key,
        "metadataKey": f"{object_key}.json",
    }


def private_preview_storage_record(
    *,
    object_key: str,
    relative_name: str,
    image_bytes: bytes,
) -> dict[str, Any]:
    return {
        "schemaVersion": PRIVATE_PREVIEW_STORAGE_SCHEMA_VERSION,
        "objectKey": object_key,
        "relativeName": relative_name,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "fileSize": len(image_bytes),
    }


def private_preview_metadata_bytes(metadata: dict[str, Any]) -> bytes:
    try:
        json_limits.validate_json_size(
            metadata,
            MAX_PREVIEW_METADATA_BYTES,
        )
        raw = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (
        json_limits.InvalidJsonValue,
        json_limits.JsonSizeLimitExceeded,
        TypeError,
        UnicodeEncodeError,
    ) as exc:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图元数据超过大小限制",
        ) from exc
    if len(raw) > MAX_PREVIEW_METADATA_BYTES:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图元数据超过大小限制",
        )
    return raw


def validated_private_preview_metadata(
    raw: bytes,
    *,
    object_key: str,
    relative_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not raw or len(raw) > MAX_PREVIEW_METADATA_BYTES:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图元数据超过大小限制",
        )
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图元数据损坏",
        ) from exc
    if not isinstance(metadata, dict):
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图元数据格式无效",
        )
    record = metadata.get("privatePreviewStorage")
    if not isinstance(record, dict):
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图缺少存储完整性记录",
        )
    expected = (
        record.get("schemaVersion")
        == PRIVATE_PREVIEW_STORAGE_SCHEMA_VERSION
        and record.get("objectKey") == object_key
        and record.get("relativeName") == relative_name
        and isinstance(record.get("sha256"), str)
        and bool(re.fullmatch(r"[a-f0-9]{64}", record["sha256"]))
        and isinstance(record.get("fileSize"), int)
        and record["fileSize"] > 0
        and record["fileSize"] <= MAX_AI_ASSET_BYTES
    )
    if not expected:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图存储记录校验失败",
        )
    return metadata, record


def write_private_preview_cache_file(target: Path, raw: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{secrets.token_hex(6)}.tmp"
    )
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def persist_private_preview_asset(target: Path) -> dict[str, Any] | None:
    context = private_preview_storage_context(target)
    if context is None:
        return None
    metadata = load_ai_output_metadata(target)
    if not target.is_file() or not isinstance(metadata, dict):
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图文件或元数据缺失",
        )
    if target.stat().st_size > MAX_AI_ASSET_BYTES:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图超过大小限制",
        )
    image_bytes = target.read_bytes()
    try:
        image_bytes_fingerprint(image_bytes)
    except ValueError as exc:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图格式或尺寸无效",
        ) from exc
    record = private_preview_storage_record(
        object_key=context["objectKey"],
        relative_name=context["relativeName"],
        image_bytes=image_bytes,
    )
    persisted_metadata = dict(metadata)
    persisted_metadata["privatePreviewStorage"] = record
    metadata_bytes = private_preview_metadata_bytes(persisted_metadata)
    if len(metadata_bytes) > MAX_PREVIEW_METADATA_BYTES:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图元数据超过大小限制",
        )
    storage = context["storage"]
    try:
        stored_image_key = storage.put_bytes(
            image_bytes,
            object_key=context["objectKey"],
        )
        stored_metadata_key = storage.put_bytes(
            metadata_bytes,
            object_key=context["metadataKey"],
        )
        persisted_image = object_storage_service.read_object_bytes_limited(
            storage,
            stored_image_key,
            MAX_AI_ASSET_BYTES,
        )
        persisted_metadata_bytes = (
            object_storage_service.read_object_bytes_limited(
                storage,
                stored_metadata_key,
                MAX_PREVIEW_METADATA_BYTES,
            )
        )
    except Exception as exc:
        raise PreviewObjectStorageError(
            "preview_object_storage_unavailable",
            "私有样图写入对象存储失败",
        ) from exc
    if (
        stored_image_key != context["objectKey"]
        or stored_metadata_key != context["metadataKey"]
        or not hmac.compare_digest(
            hashlib.sha256(persisted_image).hexdigest(),
            record["sha256"],
        )
        or persisted_metadata_bytes != metadata_bytes
    ):
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图对象存储回读校验失败",
        )
    write_private_preview_cache_file(
        ai_output_metadata_path(target),
        metadata_bytes,
    )
    return record


def ensure_private_preview_asset(target: Path) -> bool:
    context = private_preview_storage_context(target)
    if context is None:
        return target.is_file()
    storage = context["storage"]
    try:
        remote_metadata = (
            object_storage_service.read_object_bytes_limited_if_exists(
                storage,
                context["metadataKey"],
                MAX_PREVIEW_METADATA_BYTES,
            )
        )
    except Exception as exc:
        raise PreviewObjectStorageError(
            "preview_object_storage_unavailable",
            "私有样图对象存储暂时不可用",
        ) from exc
    if remote_metadata is None:
        if target.is_file() and ai_output_metadata_path(target).is_file():
            persist_private_preview_asset(target)
            return True
        return False

    _metadata, record = validated_private_preview_metadata(
        remote_metadata,
        object_key=context["objectKey"],
        relative_name=context["relativeName"],
    )
    local_image = (
        target.read_bytes()
        if target.is_file()
        and target.stat().st_size <= MAX_AI_ASSET_BYTES
        else None
    )
    if (
        local_image is None
        or len(local_image) != record["fileSize"]
        or not hmac.compare_digest(
            hashlib.sha256(local_image).hexdigest(),
            record["sha256"],
        )
        ):
        try:
            local_image = (
                object_storage_service.read_object_bytes_limited_if_exists(
                    storage,
                    context["objectKey"],
                    MAX_AI_ASSET_BYTES,
                )
            )
        except Exception as exc:
            raise PreviewObjectStorageError(
                "preview_object_storage_unavailable",
                "私有样图对象存储暂时不可用",
            ) from exc
        if local_image is None:
            raise PreviewObjectStorageError(
                "preview_object_integrity_failed",
                "私有样图对象缺失",
            )
    if (
        len(local_image) != record["fileSize"]
        or not hmac.compare_digest(
            hashlib.sha256(local_image).hexdigest(),
            record["sha256"],
        )
    ):
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图对象完整性校验失败",
        )
    try:
        image_bytes_fingerprint(local_image)
    except ValueError as exc:
        raise PreviewObjectStorageError(
            "preview_object_integrity_failed",
            "私有样图格式或尺寸无效",
        ) from exc
    write_private_preview_cache_file(target, local_image)
    write_private_preview_cache_file(
        ai_output_metadata_path(target),
        remote_metadata,
    )
    return True


def private_media_access_context(relative_name: str) -> dict[str, str]:
    digest = hashlib.sha256(relative_name.encode("utf-8")).hexdigest()
    return {
        "assetId": f"media:{digest}",
        "orderId": f"preview:{digest[:32]}",
    }


def signed_private_media_url(url: str) -> str:
    relative_name = library_media_relative_name(url)
    if not relative_name:
        return url
    target = safe_library_media_path(
        relative_name,
        allowed_prefixes=PRIVATE_MEDIA_PREFIXES,
    )
    if target is None:
        return ""
    principal = ACTIVE_PREVIEW_PRINCIPAL.get()
    if not isinstance(principal, dict):
        return ""
    user_id = str(principal.get("userId") or "").strip()
    if not user_id:
        return ""
    base_url = (
        f"/api/private-media/"
        f"{urllib.parse.quote(relative_name, safe='/')}"
    )
    secret = object_access_signing_secret()
    if not secret:
        return url if principal.get("localDemo") else ""
    context = private_media_access_context(relative_name)
    token = asset_security.sign_asset_url(
        {
            "asset_id": context["assetId"],
            "user_id": user_id,
            "order_id": context["orderId"],
            "variant": asset_security.PREVIEW,
            "purpose": asset_security.PREVIEW,
            "expires_at": int(
                time.time() + asset_security.PREVIEW_TOKEN_TTL_SECONDS
            ),
            "nonce": secrets.token_urlsafe(16),
            "menu_upload_id": ACTIVE_PREVIEW_MENU_UPLOAD_ID.get(),
        },
        secret,
    )
    return f"{base_url}?{urllib.parse.urlencode({'token': token})}"


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
    return (
        generation_cache_root(LIBRARY_DIR / "_style_backgrounds")
        / safe_style_path_segment(style_id)
        / "背景风格样图.jpg"
    )


def selected_background_snapshot_path(menu_key: str, style_id: str, sha256: str, suffix: str) -> Path:
    safe_suffix = suffix.lower() if suffix.lower() in IMAGE_EXTS else ".jpg"
    return (
        generation_cache_root(
            LIBRARY_DIR / "_selected_background_assets",
            menu_key=menu_key,
        )
        / safe_style_path_segment(style_id)
        / f"{sha256}{safe_suffix}"
    )


def write_immutable_image_snapshot(target: Path, raw: bytes, expected_sha256: str) -> None:
    if target.exists():
        existing_sha256 = file_sha256(target)
        if hmac.compare_digest(existing_sha256, expected_sha256):
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_bytes(raw)
        if not hmac.compare_digest(file_sha256(temporary), expected_sha256):
            raise SelectedBackgroundError(
                "selected_background_snapshot_failed",
                "所选背景快照校验失败，请重新选择背景",
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_selected_background_asset(style_id: str, target: Path | None = None) -> SelectedBackgroundAsset:
    safe_style = safe_style_path_segment(style_id)
    source_path = target or style_background_target(safe_style)
    if not source_path.is_file():
        ensure_private_preview_asset(source_path)
    if not source_path.is_file():
        raise SelectedBackgroundError("selected_background_not_ready", "所选背景图尚未生成完成")
    if source_path.stat().st_size > MAX_AI_ASSET_BYTES:
        raise SelectedBackgroundError(
            "selected_background_too_large",
            "所选背景超过大小限制",
        )
    raw = source_path.read_bytes()
    fingerprint = image_bytes_fingerprint(raw)
    menu_key = current_menu_cache_key()
    digest = str(fingerprint["sha256"])
    snapshot_path = selected_background_snapshot_path(
        menu_key,
        safe_style,
        digest,
        source_path.suffix,
    )
    write_immutable_image_snapshot(snapshot_path, raw, digest)
    snapshot_identity = snapshot_path.relative_to(LIBRARY_DIR).as_posix()
    asset_id = "bg_" + hashlib.sha1(
        f"{snapshot_identity}|{digest}".encode("utf-8")
    ).hexdigest()[:24]
    metadata = load_ai_output_metadata(source_path) or {}
    library_asset_id = str(metadata.get("assetRecordId") or "").strip()
    if not product_asset_library_store.TENANT_ASSET_ID_RE.fullmatch(
        library_asset_id
    ):
        library_asset_id = ""
    return SelectedBackgroundAsset(
        asset_id=asset_id,
        menu_key=menu_key,
        style_id=safe_style,
        sha256=digest,
        path=snapshot_path,
        width=int(fingerprint["width"]),
        height=int(fingerprint["height"]),
        library_asset_id=library_asset_id,
    )


def resolve_selected_background(
    style_id: str,
    *,
    expected_asset_id: str = "",
    expected_sha256: str = "",
) -> SelectedBackgroundAsset:
    asset = build_selected_background_asset(style_id)
    if expected_asset_id and not hmac.compare_digest(asset.asset_id, str(expected_asset_id).strip()):
        raise SelectedBackgroundError("selected_background_changed", "所选背景已变化，请重新选择背景")
    if expected_sha256 and not hmac.compare_digest(asset.sha256, str(expected_sha256).strip().lower()):
        raise SelectedBackgroundError("selected_background_changed", "所选背景已变化，请重新选择背景")
    return asset


def selected_background_metadata(asset: SelectedBackgroundAsset) -> dict[str, Any]:
    metadata = {
        "backgroundAssetId": asset.asset_id,
        "backgroundSha256": asset.sha256,
        "backgroundStyleId": asset.style_id,
        "backgroundMenuKey": asset.menu_key,
    }
    if asset.library_asset_id:
        metadata["backgroundLibraryAssetId"] = asset.library_asset_id
    return metadata


def attach_selected_background(candidate: dict[str, Any], asset: SelectedBackgroundAsset) -> None:
    candidate["backgroundAssetId"] = asset.asset_id
    candidate["backgroundSha256"] = asset.sha256
    candidate["backgroundMenuKey"] = asset.menu_key


def style_background_prompt_metadata(style_id: str) -> dict[str, Any]:
    context = active_category_context()
    category_id = str(
        context.get("taxonomyId")
        or background_profiles.MIXED_CATEGORY_ID
    )
    prompt = background_profiles.pure_background_prompt(
        category_id,
        style_id,
    )
    slot = background_catalog.style_slot(style_id)
    return {
        "category": str(context.get("category") or "复合餐饮"),
        "categoryId": category_id,
        "backgroundProfileVersion": (
            background_profiles.BACKGROUND_PROFILE_VERSION
        ),
        "promptVersion": STYLE_BACKGROUND_PROMPT_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": TAXONOMY_VERSION,
        "styleSlotId": slot.slot_id,
        "styleSlotName": slot.name,
        "styleSceneType": slot.scene_type,
        "promptSha256": hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest(),
    }


def pending_style_background_candidate(style_id: str, action: str = "PendingGeneration") -> dict[str, Any]:
    target = style_background_target(style_id)
    candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 0.0)
    candidate["url"] = ""
    metadata = {
        "status": "pending",
        "provider": "tencent-hunyuan",
        "action": action,
        "styleId": style_id,
        **style_background_prompt_metadata(style_id),
    }
    candidate_generation_metadata(candidate, metadata)
    return candidate


def catalog_status_background_candidate(
    style_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    target = style_background_target(style_id)
    candidate = candidate_from_path(
        target,
        "背景风格样图",
        style_id,
        "approved-background-catalog",
        0.0,
    )
    candidate["url"] = ""
    action = {
        "classification_review": "CategoryReviewRequired",
        "incomplete": "CatalogIncomplete",
        "unavailable": "CatalogUnavailable",
    }.get(str(manifest.get("status") or ""), "CatalogIncomplete")
    metadata = {
        "status": "pending",
        "provider": "asset-library",
        "action": action,
        "styleId": style_id,
        "errorCode": str(
            manifest.get("code") or "background_catalog_incomplete"
        ),
        "retryable": str(manifest.get("status") or "") == "unavailable",
        **style_background_prompt_metadata(style_id),
    }
    candidate_generation_metadata(candidate, metadata)
    return candidate


def materialize_approved_background_candidate(
    style_id: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    target = style_background_target(style_id)
    reused_background = materialize_reusable_background_asset(
        style_id,
        target,
        record=record,
    )
    if reused_background is None:
        raise ProductAssetRuntimeError("background_catalog_asset_missing")
    _record, metadata = reused_background
    persist_private_preview_asset(target)
    candidate = candidate_from_path(
        target,
        "背景风格样图",
        style_id,
        "approved-background-asset",
        100.0,
    )
    asset = build_selected_background_asset(style_id, target)
    attach_selected_background(candidate, asset)
    candidate_generation_metadata(candidate, metadata)
    candidate["assetRecordId"] = str(metadata["assetRecordId"])
    return candidate


def style_sample_candidate(style_id: str, generate: bool = True) -> dict[str, Any]:
    if approved_background_catalog_enabled():
        manifest = approved_background_catalog_manifest()
        if not manifest.get("ready"):
            return catalog_status_background_candidate(style_id, manifest)
        record = (manifest.get("_recordsByStyle") or {}).get(style_id)
        if not isinstance(record, dict):
            return catalog_status_background_candidate(
                style_id,
                {
                    **manifest,
                    "status": "incomplete",
                    "code": "background_catalog_incomplete",
                },
            )
        try:
            return materialize_approved_background_candidate(
                style_id,
                record,
            )
        except ProductAssetRuntimeError as exc:
            raise PreviewObjectStorageError(
                exc.code,
                "已审核背景资产暂时不可用，请稍后重试",
            ) from exc
    target = style_background_target(style_id)
    ensure_private_preview_asset(target)
    metadata = load_ai_output_metadata(target) if target.exists() else None
    provider = str(metadata.get("provider") or "") if metadata else ""
    expected_prompt = style_background_prompt_metadata(style_id)
    prompt_identity_matches = bool(
        metadata
        and metadata.get("promptVersion")
        == expected_prompt["promptVersion"]
        and metadata.get("backgroundProfileVersion")
        == expected_prompt["backgroundProfileVersion"]
        and metadata.get("categoryId")
        == expected_prompt["categoryId"]
        and metadata.get("promptSha256")
        == expected_prompt["promptSha256"]
    )
    cached_style_usable = (
        provider == "tencent-hunyuan"
        and prompt_identity_matches
    ) or (
        provider == "asset-library"
        and prompt_identity_matches
    ) or (
        provider == "local-category"
        and prompt_identity_matches
        and local_background_fallback_enabled()
        and not tencent_ready()
    )
    if target.exists() and metadata and cached_style_usable:
        candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 100.0)
        asset = build_selected_background_asset(style_id, target)
        attach_selected_background(candidate, asset)
        if metadata:
            candidate_generation_metadata(candidate, metadata)
        return candidate
    if not generate:
        return pending_style_background_candidate(style_id, "PendingGeneration" if tencent_ready() else "WaitingForProvider")
    provider_error = ""
    if postgres_product_runtime_enabled():
        try:
            reused_background = materialize_reusable_background_asset(
                style_id,
                target,
            )
        except ProductAssetRuntimeError as exc:
            raise PreviewObjectStorageError(
                exc.code,
                "已审核背景资产暂时不可用，请稍后重试",
            ) from exc
        if reused_background is not None:
            _record, metadata = reused_background
            persist_private_preview_asset(target)
            candidate = candidate_from_path(
                target,
                "背景风格样图",
                style_id,
                "approved-background-asset",
                100.0,
            )
            asset = build_selected_background_asset(style_id, target)
            attach_selected_background(candidate, asset)
            candidate_generation_metadata(candidate, metadata)
            candidate["assetRecordId"] = str(metadata["assetRecordId"])
            return candidate
    if tencent_ready() and env_truthy("GENERATE_STYLE_BACKGROUNDS_WITH_TENCENT", default=True):
        try:
            detail = tencent_style_background(style_id, target)
            quality_report = require_generated_output_quality(target)
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "styleId": style_id,
                **style_background_prompt_metadata(style_id),
                "qualityReport": quality_report,
                "tencent": detail,
            }
            asset = build_selected_background_asset(style_id, target)
            metadata.update(selected_background_metadata(asset))
            write_ai_output_metadata(target, metadata)
            persist_private_preview_asset(target)
            candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 100.0)
            attach_selected_background(candidate, asset)
            candidate_generation_metadata(candidate, metadata)
            asset_record = safely_persist_ai_generated_asset(kind="category_background", source_path=target, style_id=style_id, metadata=metadata, dish_name="背景风格样图")
            if asset_record:
                candidate["assetRecordId"] = asset_record["assetId"]
                metadata["assetRecordId"] = asset_record["assetId"]
                write_ai_output_metadata(target, metadata)
                persist_private_preview_asset(target)
            return candidate
        except PreviewObjectStorageError:
            raise
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
            **style_background_prompt_metadata(style_id),
        }
        if provider_error:
            metadata["error"] = provider_error
        candidate_generation_metadata(candidate, metadata)
        return candidate
    metadata = render_local_style_background(target, style_id)
    asset = build_selected_background_asset(style_id, target)
    metadata.update(selected_background_metadata(asset))
    write_ai_output_metadata(target, metadata)
    persist_private_preview_asset(target)
    candidate = candidate_from_path(target, "背景风格样图", style_id, "generated-style-sample", 90.0)
    attach_selected_background(candidate, asset)
    candidate_generation_metadata(candidate, metadata)
    return candidate


def preview_output_target(
    item: dict[str, Any],
    style_id: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> Path:
    safe_style = safe_style_path_segment(style_id)
    root = (
        generation_cache_root(LIBRARY_DIR / "_generated_previews")
        / safe_style
    )
    if selected_background is not None:
        root /= selected_background.sha256[:16]
    suffix = ".png" if selected_background is not None else ".jpg"
    return root / f"{int(item['row']):04d}_{safe_filename(item['name'])}{suffix}"


def generated_preview_candidate(
    item: dict[str, Any],
    style_id: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any] | None:
    if not style_id:
        return None
    try:
        target = preview_output_target(item, style_id, selected_background)
    except ValueError:
        return None
    ensure_private_preview_asset(target)
    if not target.exists():
        return None
    metadata = load_ai_output_metadata(target)
    if not usable_preview_metadata(metadata):
        return None
    if selected_background is not None and not verified_exact_output_metadata(
        metadata,
        target,
        selected_background,
    ):
        return None
    candidate = candidate_from_path(target, item["name"], style_id, "generated-preview", 99.9)
    if metadata:
        candidate_generation_metadata(candidate, metadata)
    return candidate


def usable_preview_metadata(metadata: dict[str, Any] | None) -> bool:
    if not metadata or metadata.get("status") not in {"succeeded", "fallback"}:
        return False
    if metadata.get("provider") in {
        "tencent-hunyuan",
        "asset-library",
    }:
        return True
    return bool(metadata.get("provider") == "local-category" and local_preview_fallback_enabled())


def metadata_matches_selected_background(
    metadata: dict[str, Any] | None,
    selected_background: SelectedBackgroundAsset,
) -> bool:
    if not metadata:
        return False
    return bool(
        hmac.compare_digest(str(metadata.get("backgroundAssetId") or ""), selected_background.asset_id)
        and hmac.compare_digest(str(metadata.get("backgroundSha256") or "").lower(), selected_background.sha256)
        and str(metadata.get("backgroundMenuKey") or "") == selected_background.menu_key
    )


def verified_exact_output_metadata(
    metadata: dict[str, Any] | None,
    target: Path,
    selected_background: SelectedBackgroundAsset,
) -> bool:
    if not (
        metadata_matches_selected_background(metadata, selected_background)
        and metadata
        and metadata.get("backgroundIdentityVerified") is True
        and metadata.get("persistedOutputBackgroundVerified") is True
        and metadata.get("pipelineVersion") == EXACT_BACKGROUND_PIPELINE_VERSION
        and (
            metadata.get("provider") == "asset-library"
            or metadata.get("dishPromptVersion")
            == DISH_GENERATION_PROMPT_VERSION
        )
    ):
        return False
    try:
        fingerprint = image_file_fingerprint(target)
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(
        str(metadata.get("outputSha256") or ""),
        str(fingerprint["sha256"]),
    )


def verified_exact_candidate(
    candidate: dict[str, Any] | None,
    selected_background: SelectedBackgroundAsset,
) -> bool:
    if not (
        candidate
        and candidate.get("backgroundIdentityVerified") is True
        and candidate.get("persistedOutputBackgroundVerified") is True
        and candidate.get("pipelineVersion") == EXACT_BACKGROUND_PIPELINE_VERSION
        and (
            candidate.get("generationProvider") == "asset-library"
            or candidate.get("dishPromptVersion")
            == DISH_GENERATION_PROMPT_VERSION
        )
        and hmac.compare_digest(str(candidate.get("backgroundAssetId") or ""), selected_background.asset_id)
        and hmac.compare_digest(str(candidate.get("backgroundSha256") or "").lower(), selected_background.sha256)
        and str(candidate.get("backgroundMenuKey") or "") == selected_background.menu_key
    ):
        return False
    path_text = str(candidate.get("path") or "")
    if not path_text:
        return False
    try:
        fingerprint = image_file_fingerprint(Path(path_text))
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(
        str(candidate.get("outputSha256") or ""),
        str(fingerprint["sha256"]),
    )


def sanitized_provider_error(exc: Exception) -> str:
    raw_error = str(exc).strip() or "混元生成失败"
    safe_error = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"\1[redacted]",
        raw_error,
    )
    safe_error = re.sub(
        r"(?i)((?:api[_-]?key|secret(?:id|key)?|token)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        safe_error,
    )[:500]
    safe_error = re.sub(
        r"(?i)((?:credential|signature)\s*[:=]\s*)[^\s,;}\]]+",
        r"\1[redacted]",
        safe_error,
    )
    return safe_error


def preview_provider_failure(exc: Exception) -> dict[str, Any]:
    normalized = f"{type(exc).__name__} {str(exc)}".lower()
    transient_markers = (
        "http 408",
        "http 425",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "failedoperation.innererror",
        "failedoperation.rpcfail",
        "failedoperation.servererror",
        "requestlimitexceeded",
        "jobnumexceed",
        "timeout",
        "timed out",
        "request failed",
        "任务超时",
        "请求失败",
        "temporar",
        "connectionerror",
        "connection reset",
        "连接重置",
        "限流",
        "throttl",
        "too many requests",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
    )
    quota_markers = (
        "resourceinsufficient",
        "resourceunavailable.inarrears",
        "resourceunavailable.lowbalance",
        "resourceunavailable.notexist",
        "resourceunavailable.stopusing",
        "resourcessoldout.chargestatusexception",
        "资源不足",
        "余额不足",
        "quota",
        "insufficient",
    )
    auth_markers = (
        "authfailure",
        "signaturefailure",
        "invalidcredential",
        "unauthorized",
        "forbidden",
        "鉴权",
        "密钥",
        "http 401",
        "http 403",
    )
    if any(marker in normalized for marker in quota_markers):
        error_code = "provider_quota"
        retryable = False
    elif any(marker in normalized for marker in auth_markers):
        error_code = "provider_auth"
        retryable = False
    elif any(marker in normalized for marker in transient_markers):
        error_code = "provider_transient"
        retryable = True
    else:
        error_code = "provider_error"
        retryable = False
    public_errors = {
        "provider_transient": "混元服务暂时繁忙，请稍后重试",
        "provider_quota": "混元资源不足，请检查资源额度",
        "provider_auth": "混元鉴权失败，请联系管理员检查配置",
        "provider_error": "混元生成失败，请稍后重试",
    }
    return {
        "status": "failed",
        "provider": "tencent-hunyuan",
        "action": "ProviderError",
        "error": public_errors[error_code],
        "errorCode": error_code,
        "retryable": retryable,
    }


def materialize_preview_candidate(
    item: dict[str, Any],
    selected_style: str,
    quality: str | None = "standard",
    selected_background: SelectedBackgroundAsset | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        safe_style_path_segment(selected_style)
    except ValueError as exc:
        return None, {"status": "failed", "provider": "local-demo", "action": "InvalidStyle", "error": str(exc)}
    target = preview_output_target(item, selected_style, selected_background)
    cached = generated_preview_candidate(item, selected_style, selected_background)
    if cached:
        return cached, {"status": "cached", "provider": cached.get("aiProvider") or "local-demo", "action": cached.get("generationAction") or "Cached"}
    same_style = reusable_selected_style_candidate(item, selected_style) if selected_background is None else None
    if same_style:
        return same_style, {"status": "reused", "provider": "library", "action": "Reuse"}
    if selected_background is not None and postgres_product_runtime_enabled():
        try:
            reused_asset = materialize_reusable_product_asset(
                item,
                selected_style,
                selected_background,
                quality,
                target,
            )
        except ProductAssetRuntimeError as exc:
            return None, {
                "status": "failed",
                "provider": "asset-library",
                "action": "AssetLibraryUnavailable",
                "error": "已审核菜品资产暂时不可用，请稍后重试",
                "errorCode": exc.code,
                "retryable": True,
            }
        if reused_asset is not None:
            candidate, metadata = reused_asset
            persist_private_preview_asset(target)
            return candidate, {
                "status": "reused",
                "provider": "asset-library",
                "action": "ApprovedAssetReuse",
                "assetRecordId": metadata["assetRecordId"],
            }
    result: dict[str, Any] = {"status": "pending", "provider": "local-category", "action": "Preview"}
    provider_failure: dict[str, Any] | None = None
    if tencent_ready() and env_truthy("GENERATE_PREVIEW_SAMPLES_WITH_TENCENT", default=True):
        try:
            if selected_background is not None:
                detail = tencent_exact_background_image(item, selected_background, quality, target)
            else:
                source_candidate = source_candidate_for_generation(item)
                if ai_first_generation_enabled():
                    detail = tencent_text_to_image(item, selected_style, quality, target)
                else:
                    detail = (
                        tencent_replace_background(item, source_candidate, selected_style, target, quality)
                        if source_candidate
                        else tencent_text_to_image(item, selected_style, quality, target)
                    )
            if selected_background is not None and not detail.get("backgroundIdentityVerified"):
                raise SelectedBackgroundError(
                    "foreground_mask_required",
                    "当前生成结果没有可靠前景 Mask，不能冒充使用了所选背景",
                )
            if selected_background is not None and not (
                detail.get("persistedOutputBackgroundVerified") is True
                and detail.get("pipelineVersion") == EXACT_BACKGROUND_PIPELINE_VERSION
                and detail.get("outputSha256")
            ):
                raise SelectedBackgroundError(
                    "generated_output_identity_missing",
                    "生成结果缺少文件摘要，不能作为所选背景样图",
                )
            metadata = {
                "status": "succeeded",
                "provider": "tencent-hunyuan",
                "action": detail["action"],
                "promptType": detail.get("promptType"),
                "reason": "free_style_preview",
                "row": item.get("row"),
                "dish": item.get("name"),
                "tencent": detail,
                "backgroundIdentityVerified": bool(detail.get("backgroundIdentityVerified")),
                "persistedOutputBackgroundVerified": bool(
                    detail.get("persistedOutputBackgroundVerified")
                ),
                "pipelineVersion": detail.get("pipelineVersion"),
                "dishPromptVersion": (
                    DISH_GENERATION_PROMPT_VERSION
                    if selected_background is not None
                    else detail.get("dishPromptVersion")
                ),
                "outputSha256": detail.get("outputSha256"),
                "qualityReport": detail.get("qualityReport"),
            }
            if selected_background is not None:
                metadata.update(selected_background_metadata(selected_background))
            write_ai_output_metadata(target, metadata)
            persist_private_preview_asset(target)
            candidate = candidate_from_path(target, item["name"], selected_style, f"tencent-preview-{detail['action']}", 100.0)
            asset_record = safely_persist_ai_generated_asset(
                kind="product_image",
                source_path=target,
                style_id=selected_style,
                metadata=metadata,
                row=item,
                quality=quality,
            )
            if asset_record:
                metadata["assetRecordId"] = asset_record["assetId"]
                write_ai_output_metadata(target, metadata)
                candidate["assetRecordId"] = asset_record["assetId"]
                persist_private_preview_asset(target)
            candidate_generation_metadata(candidate, metadata)
            return candidate, {"status": "succeeded", "provider": "tencent-hunyuan", "action": detail["action"]}
        except PreviewObjectStorageError:
            raise
        except Exception as exc:
            provider_failure = preview_provider_failure(exc)
            app.logger.warning(
                "Preview generation failed for row %s (%s): %s",
                item.get("row"),
                provider_failure["errorCode"],
                sanitized_provider_error(exc),
            )
    if selected_background is not None:
        return None, provider_failure or {
            "status": "failed",
            "provider": "tencent-hunyuan",
            "action": "ForegroundMaskUnavailable",
            "error": "无法可靠提取菜品主体，未生成不一致的样图",
            "errorCode": "foreground_mask_required",
            "retryable": False,
        }
    if local_preview_fallback_enabled():
        render_local_composed_image(target, item["name"], selected_style, local_source_for_row(item))
        metadata = {"status": "fallback", "provider": "local-category", "action": "LocalCategoryFallback", "reason": "free_style_preview"}
        write_ai_output_metadata(target, metadata)
        persist_private_preview_asset(target)
        candidate = candidate_from_path(target, item["name"], selected_style, "generated-preview", 80.0)
        candidate_generation_metadata(candidate, metadata)
        return candidate, {"status": "fallback", "provider": "local-category", "action": "LocalCategoryFallback"}
    if provider_failure:
        return None, provider_failure
    if tencent_ready():
        result.update(
            {
                "status": "failed",
                "provider": "tencent-hunyuan",
                "action": "ProviderDisabled",
                "error": "免费样图生成未启用",
                "errorCode": "provider_disabled",
                "retryable": False,
            }
        )
        return None, result
    result.update({"status": "pending", "provider": "tencent-hunyuan", "action": "WaitingForModelConfig", "error": "混元未配置，不能生成高正确率样图"})
    return None, result


def ai_output_candidate(
    item: dict[str, Any],
    style_id: str,
    quality: str | None,
    source: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> tuple[dict[str, Any], Path]:
    safe_style = safe_style_path_segment(style_id)
    quality_id = quality_config(quality)["id"]
    root = (
        generation_cache_root(LIBRARY_DIR / "_ai_outputs")
        / safe_style
    )
    if selected_background is not None:
        root /= selected_background.sha256[:16]
    suffix = ".png" if selected_background is not None else ".jpg"
    target = root / quality_id / f"{int(item['row']):04d}_{safe_filename(item['name'])}{suffix}"
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
        if meta_path.stat().st_size > MAX_PREVIEW_METADATA_BYTES:
            return None
        with meta_path.open("rb") as source:
            raw = source.read(MAX_PREVIEW_METADATA_BYTES + 1)
        if len(raw) > MAX_PREVIEW_METADATA_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_ai_output_metadata(target: Path, metadata: dict[str, Any]) -> None:
    json_limits.validate_json_size(
        metadata,
        MAX_PREVIEW_METADATA_BYTES,
    )
    raw = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_PREVIEW_METADATA_BYTES:
        raise json_limits.JsonSizeLimitExceeded(
            "AI output metadata exceeds size limit"
        )
    meta_path = ai_output_metadata_path(target)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = meta_path.with_name(
        f".{meta_path.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, meta_path)
    finally:
        temporary.unlink(missing_ok=True)


def existing_ai_output_candidate(
    item: dict[str, Any],
    style_id: str,
    quality: str | None,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any] | None:
    candidate, target = ai_output_candidate(item, style_id, quality, "generated-final", selected_background)
    if not target.exists():
        return None
    metadata = load_ai_output_metadata(target)
    if not usable_generated_metadata(metadata):
        return None
    if selected_background is not None and not verified_exact_output_metadata(
        metadata,
        target,
        selected_background,
    ):
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


def materialization_reason(
    row: dict[str, Any],
    selected_style: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> str | None:
    if not selected_style:
        return "no_selected_style"
    if selected_background is not None:
        candidate = row["candidates"][0] if row.get("candidates") else None
        if not candidate:
            return "missing_candidate"
        if not verified_exact_candidate(candidate, selected_background):
            return "selected_background_asset_mismatch"
        return None
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


def should_materialize(
    row: dict[str, Any],
    selected_style: str = "",
    selected_background: SelectedBackgroundAsset | None = None,
) -> bool:
    if selected_style:
        return materialization_reason(row, selected_style, selected_background) is not None
    candidate = row["candidates"][0] if row.get("candidates") else None
    return bool(not candidate or candidate.get("generated") or row.get("backgroundAction") in {"智能补图", "智能统一风格", "需抠图换背景", "需要定制/生成", "需去水印/重绘", "套餐组合生成"})


def successful_model_metadata(metadata: dict[str, Any] | None) -> bool:
    return bool(metadata and metadata.get("status") == "succeeded" and metadata.get("provider") == "tencent-hunyuan")


def usable_generated_metadata(metadata: dict[str, Any] | None) -> bool:
    if not metadata or metadata.get("status") not in {"succeeded", "fallback"}:
        return False
    if metadata.get("provider") in {
        "tencent-hunyuan",
        "asset-library",
    }:
        return True
    return bool(metadata.get("provider") == "local-category" and local_final_fallback_enabled())


def final_ready_candidate(
    candidate: dict[str, Any],
    selected_style: str,
    action: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> bool:
    if selected_background is not None:
        return verified_exact_candidate(candidate, selected_background)
    if candidate.get("aiProvider") == "tencent-hunyuan" or str(candidate.get("source") or "").startswith("tencent"):
        return True
    return action == "背景一致，直接复用" and candidate.get("styleId") == selected_style and not candidate.get("generated")


def prepare_results_for_export(
    results: list[dict[str, Any]],
    selected_style: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> list[dict[str, Any]]:
    prepared = []
    for row in results:
        copy_row = {**row}
        action = str(copy_row.get("backgroundAction") or "")
        copy_row["candidates"] = [
            candidate for candidate in copy_row.get("candidates") or []
            if final_ready_candidate(candidate, selected_style, action, selected_background)
        ]
        if not copy_row["candidates"] and action not in {"背景一致，直接复用", "正式生成"}:
            copy_row["publicStatus"] = copy_row.get("publicStatus") or "待正式生成"
        prepared.append(copy_row)
    return prepared


SENSITIVE_PUBLIC_PAYLOAD_KEYS = frozenset(
    (
        "deliveryAssets",
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
    candidate_url = str(payload.get("url") or "")
    relative_name = library_media_relative_name(candidate_url)
    if relative_name and Path(relative_name).parts:
        prefix = Path(relative_name).parts[0]
        if prefix in PRIVATE_MEDIA_PREFIXES:
            payload["url"] = signed_private_media_url(candidate_url)
        elif prefix not in PUBLIC_MEDIA_PREFIXES:
            payload["url"] = ""
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
            "order_id": (
                "export:"
                + hashlib.sha256(safe_name.encode("utf-8")).hexdigest()[:32]
            ),
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


def export_package_size_limit() -> int:
    raw = str(
        os.environ.get("MAX_EXPORT_PACKAGE_BYTES")
        or 512 * 1024 * 1024
    ).strip()
    try:
        limit = int(raw)
    except ValueError as exc:
        raise RuntimeError("MAX_EXPORT_PACKAGE_BYTES must be an integer") from exc
    if limit < 1024 * 1024 or limit > 8 * 1024 * 1024 * 1024:
        raise RuntimeError("MAX_EXPORT_PACKAGE_BYTES is out of range")
    return limit


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_object_file(
    storage: Any,
    object_key: str,
    *,
    expected_sha256: str,
    expected_size: int,
) -> Path:
    declared_size = int(expected_size)
    if (
        isinstance(expected_size, bool)
        or declared_size <= 0
        or declared_size > export_package_size_limit()
    ):
        raise ValueError("export object size is invalid")
    download_dir = EXPORT_DIR / "_object_downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix="verified_export_",
        suffix=".zip",
        dir=download_dir,
        delete=False,
    )
    target = Path(handle.name)
    handle.close()
    try:
        object_storage_service.download_object_file_limited(
            storage,
            object_key,
            target,
            declared_size,
        )
        actual_size = target.stat().st_size
        actual_sha256 = file_sha256(target)
        if (
            actual_size != declared_size
            or not hmac.compare_digest(actual_sha256, expected_sha256)
        ):
            raise ValueError("export object integrity mismatch")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def signed_postgres_export_payload(
    record: dict[str, Any],
    payload: dict[str, Any],
    *,
    signing_secret: str,
) -> dict[str, Any]:
    stored_key = object_storage_service.validate_object_key(
        str(record.get("zip_object_ref") or "")
    )
    package_id = str(record.get("id") or "")
    owner_user_id = str(record.get("owner_user_id") or "")
    access = object_storage_service.create_signed_access(
        stored_key,
        owner_user_id,
        asset_security.EXPORT,
        asset_security.EXPORT,
        signing_secret,
        base_url="/objects",
        extra_claims={"export_id": package_id},
    )
    return {
        **payload,
        "download": access["url"],
        "downloadProvider": "object_storage",
        "exportPackageId": package_id,
        "expiresAt": access["expires_at"],
    }


def postgres_existing_export_payload(
    generation_context: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    signing_secret = object_access_signing_secret()
    if not signing_secret:
        raise RuntimeError("object signing secret is required")
    try:
        with postgres_connection() as connection:
            record = product_export_store.ProductExportStore(
                connection,
                audit_digest_secret=signing_secret,
            ).get_owned_private_record_by_idempotency(
                owner_user_id=str(generation_context["ownerUserId"]),
                idempotency_key=str(
                    generation_context["idempotencyKey"]
                ),
            )
    except (
        product_export_store.ProductExportStoreError,
        PostgresRuntimeError,
    ):
        raise
    except Exception as exc:
        raise PostgresRuntimeError(
            "export_store_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    if record is None:
        return None
    if (
        str(record.get("status") or "") != "ready"
        or not hmac.compare_digest(
            str(record.get("export_request_sha256") or ""),
            str(generation_context["exportRequestSha256"]),
        )
    ):
        raise product_export_store.ProductExportConflict(
            "export idempotency key was reused for a different request"
        )
    return signed_postgres_export_payload(
        record,
        payload,
        signing_secret=signing_secret,
    )


def object_storage_export_payload(
    payload: dict[str, Any],
    *,
    scope: str,
    platforms: list[str] | str | None,
    generation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    secret = object_access_signing_secret()
    if not secret:
        if postgres_product_runtime_enabled() or product_redis_required():
            raise RuntimeError("object signing secret is required")
        return payload

    local_export = export_file_from_download_url(str(payload.get("download") or ""))
    if local_export is None:
        if postgres_product_runtime_enabled():
            raise RuntimeError("durable export package path is required")
        return payload

    local_path, relative_name = local_export
    local_size = local_path.stat().st_size
    if local_size > export_package_size_limit():
        raise RuntimeError("export package exceeds configured size limit")
    local_sha256 = file_sha256(local_path)
    durable_export = postgres_product_runtime_enabled()
    if durable_export:
        if not isinstance(generation_context, dict):
            raise RuntimeError("durable export generation context is required")
        owner_scope = hashlib.sha256(
            str(generation_context["ownerUserId"]).encode("utf-8")
        ).hexdigest()[:24]
        export_scope = hashlib.sha256(
            str(generation_context["exportId"]).encode("utf-8")
        ).hexdigest()[:24]
        object_key = object_storage_service.validate_object_key(
            f"{object_storage_service.EXPORTS_PREFIX}v2/"
            f"{owner_scope}/{export_scope}/"
            f"{local_sha256}-{secrets.token_hex(8)}.zip"
        )
    else:
        object_key = object_storage_service.validate_object_key(
            f"{object_storage_service.EXPORTS_PREFIX}{relative_name}"
    )
    storage = object_storage_service.get_object_storage_service()
    stored_key = ""
    export_record_persisted = False
    try:
        stored_key = storage.put_file(local_path, object_key=object_key)
        verified_path = verified_object_file(
            storage,
            stored_key,
            expected_sha256=local_sha256,
            expected_size=local_size,
        )
        verified_path.unlink(missing_ok=True)

        if durable_export:
            assert isinstance(generation_context, dict)
            with postgres_connection() as connection:
                created = product_export_store.ProductExportStore(
                    connection,
                    audit_digest_secret=secret,
                ).create_or_get_private_record(
                    export_id=str(generation_context["exportId"]),
                    owner_user_id=str(
                        generation_context["ownerUserId"]
                    ),
                    idempotency_key=str(
                        generation_context["idempotencyKey"]
                    ),
                    generation_job_id=str(
                        generation_context["generationJobId"]
                    ),
                    generation_request_sha256=str(
                        generation_context["generationRequestSha256"]
                    ),
                    export_request_sha256=str(
                        generation_context["exportRequestSha256"]
                    ),
                    manifest_object_ref=str(
                        generation_context["manifestObjectRef"]
                    ),
                    manifest_sha256=str(
                        generation_context["manifestSha256"]
                    ),
                    platforms=(
                        list(platforms)
                        if isinstance(platforms, list)
                        else [str(platforms or "")]
                    ),
                    watermark=(
                        generation_context.get("watermark")
                        if isinstance(
                            generation_context.get("watermark"),
                            dict,
                        )
                        else {}
                    ),
                    zip_object_ref=stored_key,
                    zip_sha256=local_sha256,
                    zip_size_bytes=local_size,
                )
            record = created.record
            export_record_persisted = True
            recorded_key = str(record.get("zip_object_ref") or "")
            if not created.created and recorded_key != stored_key:
                storage.delete(stored_key)
                stored_key = ""
            return signed_postgres_export_payload(
                record,
                payload,
                signing_secret=secret,
            )

        package_id = record_export_package(
            object_key=stored_key,
            scope=scope,
            platforms=platforms,
            image_count=int(payload.get("images") or 0),
            file_size=local_size,
            metadata={
                "rows": payload.get("rows"),
                "watermark": payload.get("watermark"),
                "legacyDownload": payload.get("download"),
            },
        )
        access = object_storage_service.create_signed_access(
            stored_key,
            current_user_id(),
            asset_security.EXPORT,
            asset_security.EXPORT,
            secret,
            base_url="/objects",
        )
        return {
            **payload,
            "download": access["url"],
            "downloadProvider": "object_storage",
            "exportPackageId": package_id,
            "expiresAt": access["expires_at"],
        }
    except Exception:
        if stored_key and not export_record_persisted:
            try:
                storage.delete(stored_key)
            except Exception:
                app.logger.exception(
                    "Failed to clean orphaned export object %s",
                    stored_key,
                )
        raise


def postgres_export_generation_context(
    contract: dict[str, Any],
    principal: dict[str, Any],
    *,
    primary_manifest: dict[str, Any],
    revision_manifests: list[dict[str, Any]],
    scope: str,
    selected_rows: list[int],
    image_format: str,
    platforms: list[str],
    watermark: dict[str, Any] | None,
) -> dict[str, Any]:
    owner_user_id = str(principal.get("userId") or "")
    job_id = str(contract.get("jobId") or "")
    idempotency = (
        contract.get("idempotency")
        if isinstance(contract.get("idempotency"), dict)
        else {}
    )
    request_sha256 = str(idempotency.get("requestSha256") or "")
    manifest_object_ref = object_storage_service.validate_object_key(
        str(primary_manifest.get("objectKey") or "")
    )
    manifest_sha256 = str(primary_manifest.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise RuntimeError("generation manifest digest is invalid")
    client_key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not client_key:
        client_key = secrets.token_urlsafe(24)
    idempotency_digest = hashlib.sha256(
        f"{owner_user_id}\0{client_key}".encode("utf-8")
    ).hexdigest()
    frozen_watermark = (
        dict(watermark or {})
        if isinstance(watermark, dict)
        else {}
    )
    frozen_watermark.pop("logoData", None)
    normalized_revisions = sorted(
        [
            {
                "jobId": str(item.get("jobId") or ""),
                "rowNumber": int(item.get("rowNumber") or 0),
                "objectKey": object_storage_service.validate_object_key(
                    str(item.get("objectKey") or "")
                ),
                "sha256": str(item.get("sha256") or ""),
                "requestSha256": str(
                    item.get("requestSha256") or ""
                ),
            }
            for item in revision_manifests
        ],
        key=lambda item: (item["rowNumber"], item["jobId"]),
    )
    for item in normalized_revisions:
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise RuntimeError("revision manifest digest is invalid")
    export_request = {
        "version": "product-export-request-v1",
        "ownerUserId": owner_user_id,
        "generationJobId": job_id,
        "generationRequestSha256": request_sha256,
        "primaryManifest": {
            "objectKey": manifest_object_ref,
            "sha256": manifest_sha256,
        },
        "revisionManifests": normalized_revisions,
        "scope": str(scope or "all"),
        "selectedRows": sorted(set(selected_rows)),
        "format": str(image_format or "jpg").lower(),
        "platforms": sorted(set(platforms)),
        "watermark": frozen_watermark,
    }
    export_request_sha256 = hashlib.sha256(
        canonical_json(export_request).encode("utf-8")
    ).hexdigest()
    return {
        "exportId": f"export_{idempotency_digest[:32]}",
        "ownerUserId": owner_user_id,
        "idempotencyKey": f"export_idem_{idempotency_digest}",
        "generationJobId": job_id,
        "generationRequestSha256": request_sha256,
        "exportRequestSha256": export_request_sha256,
        "manifestObjectRef": manifest_object_ref,
        "manifestSha256": manifest_sha256,
        "watermark": frozen_watermark,
    }


def export_file_from_download_url(download_url: str) -> tuple[Path, str] | None:
    parsed = urllib.parse.urlsplit(str(download_url or ""))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/download/"):
        return None
    relative_name = urllib.parse.unquote(parsed.path[len("/download/") :])
    safe_name = safe_export_download_name(relative_name)
    if safe_name is None:
        return None
    return (EXPORT_DIR / safe_name).resolve(), safe_name


def record_export_package(
    *,
    object_key: str,
    scope: str,
    platforms: list[str] | str | None,
    image_count: int,
    file_size: int,
    metadata: dict[str, Any],
) -> str:
    package_id = storage_db.new_id("export_pkg")
    now = storage_db.utc_now()
    platform_text = ",".join(platforms) if isinstance(platforms, list) else str(platforms or "")
    conn = product_db_conn()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO export_packages (
                    id, job_id, object_key, platform, scope, image_count, file_size,
                    status, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package_id,
                    None,
                    object_key,
                    platform_text,
                    str(scope or ""),
                    max(0, int(image_count)),
                    max(0, int(file_size)),
                    "ready",
                    storage_db.json_dumps(metadata),
                    now,
                    now,
                ),
            )
    finally:
        conn.close()
    return package_id


def persist_menu_upload(
    local_path: Path,
    *,
    original_filename: str,
    content_type: str,
    menu: dict[str, Any],
    owner_user_id: str,
) -> str:
    storage = object_storage_service.get_object_storage_service()
    if (
        not local_path.is_file()
        or local_path.stat().st_size <= 0
        or local_path.stat().st_size > MAX_MENU_UPLOAD_BYTES
    ):
        raise MenuUploadError(
            "menu_upload_too_large",
            "菜单文件超过大小限制",
            status=413,
        )
    raw = local_path.read_bytes()
    object_key = storage.put_file(
        local_path,
        prefix=object_storage_service.MENUS_PREFIX,
        filename=local_path.name,
    )
    menu_upload_id = storage_db.new_id("menu")
    menu_summary = {key: value for key, value in menu.items() if key != "items"}
    if postgres_product_runtime_enabled():
        resolved_content_type = str(content_type or "").strip().lower()
        if "/" not in resolved_content_type:
            resolved_content_type = (
                "application/vnd.ms-excel"
                if local_path.suffix.lower() == ".xls"
                else (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                )
            )
        with postgres_connection() as connection:
            result = MenuUploadStore(connection).create_or_get_private_record(
                upload_id=menu_upload_id,
                owner_user_id=str(owner_user_id),
                object_ref=object_key,
                object_sha256=hashlib.sha256(raw).hexdigest(),
                original_filename=(
                    str(original_filename or local_path.name).strip()
                    or local_path.name
                ),
                content_type=resolved_content_type,
                file_size=len(raw),
                parser_version=str(MENU_PARSER_VERSION),
                item_count=max(0, int(menu.get("count") or 0)),
                store_name=str(menu.get("store") or ""),
                parsed_summary=menu_summary,
            )
        return str(result.record["id"])

    now = storage_db.utc_now()
    menu_summary[MENU_UPLOAD_PRIVATE_METADATA_KEY] = {
        "ownerUserId": str(owner_user_id),
        "parserVersion": MENU_PARSER_VERSION,
    }
    conn = product_db_conn()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO menu_uploads (
                    id, store_name, original_filename, object_key, content_type,
                    file_size, sha256, status, parsed_summary_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    menu_upload_id,
                    str(menu.get("store") or ""),
                    str(original_filename or local_path.name),
                    object_key,
                    str(content_type or ""),
                    local_path.stat().st_size,
                    hashlib.sha256(raw).hexdigest(),
                    "parsed",
                    storage_db.json_dumps(menu_summary),
                    now,
                    now,
                ),
            )
    finally:
        conn.close()
    return menu_upload_id


def resolve_menu_upload_snapshot(
    menu_upload_id: str,
    principal: dict[str, Any],
) -> dict[str, Any]:
    normalized_id = str(menu_upload_id or "").strip()
    if not re.fullmatch(r"menu_[a-f0-9]{32}", normalized_id):
        raise MenuUploadError(
            "invalid_menu_upload_id",
            "菜单上传记录格式无效",
            status=400,
        )

    principal_user_id = str(principal.get("userId") or "").strip()
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                postgres_record = MenuUploadStore(
                    connection
                ).get_owned_private_record(
                    upload_id=normalized_id,
                    owner_user_id=principal_user_id,
                )
        except PostgresMenuUploadNotFound as exc:
            raise MenuUploadError(
                "menu_upload_not_found",
                "菜单上传记录不存在",
                status=404,
            ) from exc
        except (
            InvalidMenuUploadInput,
            MenuUploadStoreError,
            PostgresRuntimeError,
        ) as exc:
            raise MenuUploadError(
                "menu_upload_store_unavailable",
                "菜单上传记录暂时不可用",
                status=503,
            ) from exc
        row: dict[str, Any] = {
            "status": postgres_record.get("status"),
            "object_key": postgres_record.get("object_ref"),
            "sha256": postgres_record.get("object_sha256"),
            "original_filename": postgres_record.get("original_filename"),
        }
        summary = postgres_record.get("parsed_summary")
        if not isinstance(summary, dict):
            summary = {}
        owner_user_id = str(
            postgres_record.get("owner_user_id") or ""
        ).strip()
        private_metadata = {
            "ownerUserId": owner_user_id,
            "parserVersion": postgres_record.get("parser_version"),
        }
        ready_statuses = {"parsed", "frozen"}
    else:
        conn = product_db_conn()
        try:
            sqlite_row = conn.execute(
                "SELECT * FROM menu_uploads WHERE id = ?",
                (normalized_id,),
            ).fetchone()
        finally:
            conn.close()
        if sqlite_row is None:
            raise MenuUploadError(
                "menu_upload_not_found",
                "菜单上传记录不存在",
                status=404,
            )
        row = dict(sqlite_row)
        summary = storage_db.json_loads(
            row.get("parsed_summary_json"),
            {},
        )
        if not isinstance(summary, dict):
            summary = {}
        private_metadata = summary.get(MENU_UPLOAD_PRIVATE_METADATA_KEY)
        if not isinstance(private_metadata, dict):
            private_metadata = {}
        owner_user_id = str(
            private_metadata.get("ownerUserId") or ""
        ).strip()
        if principal.get("internal") is True:
            pass
        elif owner_user_id:
            if not hmac.compare_digest(owner_user_id, principal_user_id):
                raise MenuUploadError(
                    "menu_upload_not_found",
                    "菜单上传记录不存在",
                    status=404,
                )
        elif not principal.get("localDemo"):
            raise MenuUploadError(
                "menu_upload_not_found",
                "菜单上传记录不存在",
                status=404,
            )
        ready_statuses = {"parsed"}

    if str(row.get("status") or "") not in ready_statuses:
        raise MenuUploadError(
            "menu_upload_not_ready",
            "菜单上传记录尚未解析完成",
            status=425,
        )

    try:
        object_key = object_storage_service.validate_object_key(
            str(row.get("object_key") or "")
        )
    except ValueError as exc:
        raise MenuUploadError(
            "menu_upload_invalid_object",
            "菜单上传记录的对象地址无效",
        ) from exc
    expected_sha256 = str(row.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise MenuUploadError(
            "menu_upload_invalid_digest",
            "菜单上传记录的摘要无效",
        )
    storage = object_storage_service.get_object_storage_service()
    try:
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            MAX_MENU_UPLOAD_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "menu_upload_too_large",
            "菜单文件超过大小限制",
            status=413,
        ) from exc
    except (FileNotFoundError, KeyError, OSError) as exc:
        raise MenuUploadError(
            "menu_upload_object_missing",
            "菜单原文件不存在或暂时不可用",
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise MenuUploadError(
            "menu_upload_integrity_mismatch",
            "菜单原文件完整性校验失败",
        )
    try:
        parser_version = int(private_metadata.get("parserVersion") or MENU_PARSER_VERSION)
    except (TypeError, ValueError) as exc:
        raise MenuUploadError(
            "menu_upload_invalid_parser_version",
            "菜单解析版本无效",
        ) from exc
    if parser_version != MENU_PARSER_VERSION:
        raise MenuUploadError(
            "menu_upload_parser_version_mismatch",
            "菜单解析版本已过期，请重新上传菜单",
        )

    public_summary = {
        key: value
        for key, value in summary.items()
        if key != MENU_UPLOAD_PRIVATE_METADATA_KEY
    }
    return {
        "id": normalized_id,
        "objectKey": object_key,
        "sha256": expected_sha256,
        "parserVersion": parser_version,
        "ownerUserId": owner_user_id or principal_user_id,
        "originalFilename": str(row.get("original_filename") or ""),
        "summary": public_summary,
    }


def latest_menu_upload_snapshot(principal: dict[str, Any]) -> dict[str, Any] | None:
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                record = MenuUploadStore(
                    connection
                ).latest_owned_private_record(
                    owner_user_id=str(principal.get("userId") or ""),
                )
        except (
            InvalidMenuUploadInput,
            MenuUploadStoreError,
            PostgresRuntimeError,
        ) as exc:
            raise MenuUploadError(
                "menu_upload_store_unavailable",
                "菜单上传记录暂时不可用",
                status=503,
            ) from exc
        if record is None:
            return None
        return resolve_menu_upload_snapshot(str(record["id"]), principal)

    conn = product_db_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM menu_uploads ORDER BY created_at DESC, id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            return resolve_menu_upload_snapshot(str(row["id"]), principal)
        except MenuUploadError as exc:
            if exc.code == "menu_upload_not_found":
                continue
            raise
    return None


def menu_upload_error_response(exc: MenuUploadError):
    return jsonify({"error": str(exc), "code": exc.code}), exc.status


def materialize_menu_upload_snapshot(snapshot: dict[str, Any]) -> Path:
    object_key = object_storage_service.validate_object_key(str(snapshot.get("objectKey") or ""))
    expected_sha256 = str(snapshot.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise MenuUploadError("menu_upload_invalid_digest", "菜单上传记录的摘要无效")
    storage = object_storage_service.get_object_storage_service()
    try:
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            MAX_MENU_UPLOAD_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "menu_upload_too_large",
            "菜单文件超过大小限制",
            status=413,
        ) from exc
    except Exception as exc:
        raise MenuUploadError(
            "menu_upload_object_missing",
            "菜单原文件不存在或暂时不可用",
            status=503,
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise MenuUploadError(
            "menu_upload_integrity_mismatch",
            "菜单原文件完整性校验失败",
        )
    original_filename = Path(
        str(snapshot.get("originalFilename") or "")
    ).name
    if not original_filename:
        original_filename = Path(object_key).name
        original_filename = re.sub(
            r"^[a-f0-9]{16}_",
            "",
            original_filename,
        )
        original_filename = re.sub(
            r"^menu_\d+_",
            "",
            original_filename,
        )
    suffix = Path(original_filename).suffix.lower()
    if suffix not in MENU_EXTS:
        suffix = ".xls" if raw.startswith(b"\xd0\xcf\x11\xe0") else ".xlsx"
    safe_source_stem = safe_filename(Path(original_filename).stem)
    target = (
        MODEL_INPUT_DIR
        / "_menu_uploads"
        / expected_sha256
        / f"{safe_source_stem}{suffix}"
    )
    if (
        target.is_file()
        and target.stat().st_size <= MAX_MENU_UPLOAD_BYTES
        and hmac.compare_digest(
            file_sha256(target),
            expected_sha256,
        )
    ):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_bytes(raw)
        if not hmac.compare_digest(file_sha256(temporary), expected_sha256):
            raise MenuUploadError(
                "menu_upload_snapshot_failed",
                "菜单原文件快照校验失败",
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def resolve_customer_preview_menu() -> tuple[
    Path | None,
    dict[str, Any] | None,
    str,
    Any,
]:
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return None, None, "", principal_error
    assert principal is not None

    menu_upload_id = str(
        request.args.get("menuUploadId")
        or request.args.get("menu_upload_id")
        or ""
    ).strip()
    if not menu_upload_id:
        if principal.get("localDemo"):
            return current_menu_path(), principal, "", None
        return None, principal, "", (
            jsonify(
                {
                    "error": "请先上传菜单",
                    "code": "menu_upload_id_required",
                }
            ),
            400,
        )
    try:
        snapshot = resolve_menu_upload_snapshot(menu_upload_id, principal)
        return (
            materialize_menu_upload_snapshot(snapshot),
            principal,
            menu_upload_id,
            None,
        )
    except MenuUploadError as exc:
        return None, principal, menu_upload_id, menu_upload_error_response(exc)


@contextmanager
def customer_preview_menu_path(
    path: Path | None,
    principal: dict[str, Any],
    menu_upload_id: str,
):
    principal_token = ACTIVE_PREVIEW_PRINCIPAL.set(principal)
    menu_upload_token = ACTIVE_PREVIEW_MENU_UPLOAD_ID.set(menu_upload_id)
    try:
        with active_asset_owner(str(principal.get("userId") or "")):
            if path is None:
                yield
                return
            with active_menu_path(path):
                yield
    finally:
        ACTIVE_PREVIEW_MENU_UPLOAD_ID.reset(menu_upload_token)
        ACTIVE_PREVIEW_PRINCIPAL.reset(principal_token)


def selected_background_batch_snapshot(
    selected_background: SelectedBackgroundAsset,
    menu_snapshot: dict[str, Any],
) -> dict[str, Any]:
    expected_menu_key = str(menu_snapshot["sha256"])[:12]
    if not hmac.compare_digest(selected_background.menu_key, expected_menu_key):
        raise SelectedBackgroundError(
            "selected_background_menu_mismatch",
            "所选背景不属于当前菜单，请重新选择背景",
        )
    if selected_background.path.stat().st_size > MAX_AI_ASSET_BYTES:
        raise SelectedBackgroundError(
            "selected_background_changed",
            "所选背景超过大小限制",
        )
    raw = selected_background.path.read_bytes()
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), selected_background.sha256):
        raise SelectedBackgroundError(
            "selected_background_changed",
            "所选背景已变化，请重新选择背景",
        )
    object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}selected-backgrounds/"
        f"{selected_background.asset_id}/{selected_background.sha256}.image"
    )
    storage = object_storage_service.get_object_storage_service()
    stored_key = storage.put_bytes(raw, object_key=object_key)
    try:
        persisted = object_storage_service.read_object_bytes_limited(
            storage,
            stored_key,
            MAX_AI_ASSET_BYTES,
        )
    except Exception as exc:
        raise SelectedBackgroundError(
            "selected_background_snapshot_failed",
            "所选背景快照暂时不可用",
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(persisted).hexdigest(), selected_background.sha256):
        raise SelectedBackgroundError(
            "selected_background_snapshot_failed",
            "所选背景快照校验失败，请重新选择背景",
        )
    return {
        "assetId": selected_background.asset_id,
        "libraryAssetId": selected_background.library_asset_id,
        "styleId": selected_background.style_id,
        "sha256": selected_background.sha256,
        "objectKey": stored_key,
        "width": selected_background.width,
        "height": selected_background.height,
    }


def selected_background_from_batch_contract(contract: dict[str, Any]) -> SelectedBackgroundAsset:
    background = contract["selectedBackground"]
    expected_sha256 = str(background["sha256"])
    storage = object_storage_service.get_object_storage_service()
    try:
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            str(background["objectKey"]),
            MAX_AI_ASSET_BYTES,
        )
    except Exception as exc:
        raise SelectedBackgroundError(
            "selected_background_snapshot_failed",
            "所选背景快照暂时不可用",
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise SelectedBackgroundError(
            "selected_background_changed",
            "所选背景快照完整性校验失败",
        )
    fingerprint = image_bytes_fingerprint(raw)
    if (
        int(fingerprint["width"]) != int(background["width"])
        or int(fingerprint["height"]) != int(background["height"])
    ):
        raise SelectedBackgroundError(
            "selected_background_changed",
            "所选背景尺寸已变化",
        )
    target = (
        MODEL_INPUT_DIR
        / "_selected_backgrounds"
        / str(background["assetId"])
        / f"{expected_sha256}.image"
    )
    write_immutable_image_snapshot(target, raw, expected_sha256)
    return SelectedBackgroundAsset(
        asset_id=str(background["assetId"]),
        menu_key=str(contract["menu"]["sha256"])[:12],
        style_id=str(background["styleId"]),
        sha256=expected_sha256,
        path=target,
        width=int(background["width"]),
        height=int(background["height"]),
        library_asset_id=str(background.get("libraryAssetId") or ""),
    )


def batch_watermark_snapshot(
    raw_value: Any,
    *,
    user_id: str,
) -> dict[str, Any]:
    source = dict(raw_value) if isinstance(raw_value, dict) else {}
    source.pop("logoObjectKey", None)
    if not source.get("enabled") or str(source.get("type") or "text").lower() != "logo":
        source.pop("logoData", None)
        return source
    data_url = str(source.pop("logoData", "") or "")
    if not data_url or len(data_url) > MAX_LOGO_DATA_URL_CHARS:
        raise BatchContractError(
            "invalid_watermark_logo",
            "watermark logo is missing or too large",
            field="watermark.logoData",
        )
    encoded = data_url.split(",", 1)[1] if "," in data_url else data_url
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_LOGO_BYTES:
            raise ValueError("logo too large")
        with Image.open(io.BytesIO(raw)) as image:
            validate_image_bounds(image, max_pixels=MAX_LOGO_PIXELS)
            normalized = image.convert("RGBA")
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=True)
            normalized_bytes = output.getvalue()
    except Exception as exc:
        raise BatchContractError(
            "invalid_watermark_logo",
            "watermark logo is not a valid image",
            field="watermark.logoData",
        ) from exc
    digest = hashlib.sha256(normalized_bytes).hexdigest()
    owner_key = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
    object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.ORIGINALS_PREFIX}watermarks/{owner_key}/{digest}.png"
    )
    stored_key = object_storage_service.get_object_storage_service().put_bytes(
        normalized_bytes,
        object_key=object_key,
    )
    source["logoObjectKey"] = stored_key
    return source


def persist_uploaded_library_image(
    local_path: Path,
    *,
    upload_batch: str,
    original_member: str,
    style_id: str = "style-upload",
) -> str:
    object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.ORIGINALS_PREFIX}{upload_batch}/{style_id}/{local_path.name}"
    )
    if local_path.stat().st_size > MAX_AI_ASSET_BYTES:
        raise ProductAssetRuntimeError("library_image_size_invalid")
    storage = object_storage_service.get_object_storage_service()
    stored_key = storage.put_file(local_path, object_key=object_key)
    raw = local_path.read_bytes()
    width = 0
    height = 0
    try:
        with Image.open(local_path) as img:
            width, height = img.size
    except Exception:
        pass
    conn = product_db_conn()
    try:
        record = storage_db.create_library_image(
            conn,
            object_key=stored_key,
            dish_name=local_path.stem,
            store_name=upload_batch,
            style_id=style_id,
            source="uploaded",
            sha256=hashlib.sha256(raw).hexdigest(),
            category_path=style_id,
            width=width,
            height=height,
            file_size=local_path.stat().st_size,
            reusable=True,
            tags=[style_id, "uploaded"],
            metadata={"originalMember": original_member, "uploadBatch": upload_batch},
        )
    finally:
        conn.close()
    return str(record["id"])


def candidate_generation_metadata(candidate: dict[str, Any], metadata: dict[str, Any]) -> None:
    candidate["aiProvider"] = str(metadata.get("provider") or candidate.get("aiProvider") or "")
    candidate["generationStatus"] = str(metadata.get("status") or "")
    candidate["generationAction"] = str(metadata.get("action") or "")
    candidate["generationProvider"] = str(metadata.get("provider") or "")
    if metadata.get("error"):
        candidate["generationError"] = str(metadata.get("error"))
    if metadata.get("errorCode"):
        candidate["generationErrorCode"] = str(metadata.get("errorCode"))
    if metadata.get("retryable") is not None:
        candidate["retryable"] = bool(metadata.get("retryable"))
    for key in (
        "assetRecordId",
        "backgroundAssetId",
        "backgroundLibraryAssetId",
        "backgroundSha256",
        "backgroundMenuKey",
        "backgroundIdentityVerified",
        "persistedOutputBackgroundVerified",
        "pipelineVersion",
        "dishPromptVersion",
        "outputSha256",
    ):
        if metadata.get(key) not in (None, ""):
            candidate[key] = metadata[key]
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


def publish_generation_progress(
    callback: Callable[[int, int, int], None] | None,
    items_by_index: dict[int, dict[str, Any]],
    requested: int,
) -> None:
    if callback is None:
        return
    completed_statuses = {"succeeded", "cached", "reused", "fallback"}
    completed = sum(
        1
        for item in items_by_index.values()
        if str(item.get("status") or "") in completed_statuses
    )
    failed = sum(
        1
        for item in items_by_index.values()
        if str(item.get("status") or "") == "failed"
    )
    pending = max(0, int(requested) - completed - failed)
    callback(completed, failed, pending)


def materialize_final_row(
    row: dict[str, Any],
    selected_style: str,
    quality: str | None,
    status: dict[str, Any],
    reason: str,
    source_candidate: dict[str, Any] | None,
    item_result: dict[str, Any],
    selected_background: SelectedBackgroundAsset | None = None,
    execution_guard: Callable[[], None] | None = None,
    exact_failure_cache: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    _, target = ai_output_candidate(row, selected_style, quality, "generated-final", selected_background)
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
            if execution_guard is not None:
                execution_guard()
            if selected_background is not None:
                detail = tencent_exact_background_image(
                    row,
                    selected_background,
                    quality,
                    target,
                    _batch_failure_cache=exact_failure_cache,
                )
            elif ai_first_generation_enabled():
                detail = tencent_text_to_image(row, selected_style, quality, target)
            elif source_candidate:
                try:
                    detail = tencent_replace_background(row, source_candidate, selected_style, target, quality)
                except ProviderResultDownloadError:
                    raise
                except Exception as exc:
                    replace_error = exc
                    try:
                        detail = tencent_text_to_image(row, selected_style, quality, target)
                    except Exception as text_error:
                        raise combined_generation_error("商品背景生成", exc, "文生图兜底", text_error) from text_error
            else:
                detail = tencent_text_to_image(row, selected_style, quality, target)
            if execution_guard is not None:
                execution_guard()
            if selected_background is not None and not detail.get("backgroundIdentityVerified"):
                raise SelectedBackgroundError(
                    "foreground_mask_required",
                    "当前生成结果没有可靠前景 Mask，不能冒充使用了所选背景",
                )
            if selected_background is not None and not (
                detail.get("persistedOutputBackgroundVerified") is True
                and detail.get("pipelineVersion") == EXACT_BACKGROUND_PIPELINE_VERSION
                and detail.get("outputSha256")
            ):
                raise SelectedBackgroundError(
                    "generated_output_identity_missing",
                    "生成结果缺少文件摘要，不能作为所选背景正式图",
                )
            if replace_error is not None:
                metrics["fallback"] += 1
                metrics["actionFallback"] += 1
                item_result["fallback"] = True
                item_result["fallbackFrom"] = "ReplaceBackground"
                item_result["fallbackMessage"] = str(replace_error)[:220]
            assert detail is not None
            ai_candidate, _ = ai_output_candidate(
                row,
                selected_style,
                quality,
                f"tencent-{detail['action']}",
                selected_background,
            )
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
                "backgroundIdentityVerified": bool(detail.get("backgroundIdentityVerified")),
                "persistedOutputBackgroundVerified": bool(
                    detail.get("persistedOutputBackgroundVerified")
                ),
                "pipelineVersion": detail.get("pipelineVersion"),
                "dishPromptVersion": (
                    DISH_GENERATION_PROMPT_VERSION
                    if selected_background is not None
                    else detail.get("dishPromptVersion")
                ),
                "outputSha256": detail.get("outputSha256"),
                "qualityReport": detail.get("qualityReport"),
            }
            if selected_background is not None:
                metadata.update(selected_background_metadata(selected_background))
            write_ai_output_metadata(target, metadata)
            candidate_generation_metadata(ai_candidate, metadata)
            asset_record = safely_persist_ai_generated_asset(kind="product_image", source_path=target, style_id=selected_style, metadata=metadata, row=row, quality=quality)
            if asset_record:
                ai_candidate["assetRecordId"] = asset_record["assetId"]
                metadata["assetRecordId"] = asset_record["assetId"]
                write_ai_output_metadata(target, metadata)
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
        if selected_background is not None or not local_final_fallback_enabled():
            metrics["pending"] += 1
            action = "ForegroundMaskUnavailable" if selected_background is not None else "WaitingForModelConfig"
            error = (
                item_result.get("error")
                or ("无法可靠提取菜品主体，未生成不一致的正式图" if selected_background is not None else "混元未配置，正式图未生成")
            )
            item_result.update({"provider": "tencent-hunyuan", "action": action, "status": "pending", "error": error})
            row["backgroundAction"] = "等待混元生成"
            row["publicStatus"] = "等待混元生成"
            row["generationStatus"] = "pending"
            row["generation"] = item_result
            metrics["actions"] = generation_action_counts(action)
            return metrics
        render_local_composed_image(target, row["name"], selected_style, local_source_for_row(row))
        ai_candidate, _ = ai_output_candidate(row, selected_style, quality, "generated-local", selected_background)
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


def verified_preview_candidate_for_final(
    row: dict[str, Any],
    selected_style: str,
    quality: str | None,
    selected_background: SelectedBackgroundAsset | None,
) -> dict[str, Any] | None:
    if (
        selected_background is None
        or quality_config(quality)["id"] != "standard"
    ):
        return None
    principal = ACTIVE_PREVIEW_PRINCIPAL.get()
    principal_token = None
    if principal is None:
        owner_user_id = ACTIVE_ASSET_OWNER_USER_ID.get().strip()
        menu_upload_id = ACTIVE_PREVIEW_MENU_UPLOAD_ID.get().strip()
        if owner_user_id and menu_upload_id:
            principal_token = ACTIVE_PREVIEW_PRINCIPAL.set(
                {
                    "userId": owner_user_id,
                    "localDemo": staging_in_process_generation_allowed(),
                }
            )
    try:
        try:
            candidate = generated_preview_candidate(
                row,
                selected_style,
                selected_background,
            )
        except PreviewObjectStorageError:
            return None
    finally:
        if principal_token is not None:
            ACTIVE_PREVIEW_PRINCIPAL.reset(principal_token)
    if not verified_exact_candidate(candidate, selected_background):
        return None
    return candidate


def materialize_verified_preview_as_final(
    row: dict[str, Any],
    selected_style: str,
    quality: str | None,
    selected_background: SelectedBackgroundAsset | None,
    target: Path,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    preview = verified_preview_candidate_for_final(
        row,
        selected_style,
        quality,
        selected_background,
    )
    if preview is None or selected_background is None:
        return None
    preview_path = Path(str(preview.get("path") or ""))
    preview_metadata = load_ai_output_metadata(preview_path)
    if not verified_exact_output_metadata(
        preview_metadata,
        preview_path,
        selected_background,
    ):
        return None
    assert preview_metadata is not None
    raw = preview_path.read_bytes()
    if len(raw) > MAX_AI_ASSET_BYTES:
        return None
    source_action = str(preview_metadata.get("action") or "Preview")
    final_metadata = {
        key: value
        for key, value in preview_metadata.items()
        if key != "privatePreviewStorage"
    }
    final_metadata.update(
        {
            "action": "PreviewReuse",
            "reason": "verified_free_preview_reuse",
            "sourceAction": source_action,
        }
    )
    write_private_preview_cache_file(target, raw)
    write_ai_output_metadata(target, final_metadata)
    if not verified_exact_output_metadata(
        final_metadata,
        target,
        selected_background,
    ):
        target.unlink(missing_ok=True)
        ai_output_metadata_path(target).unlink(missing_ok=True)
        return None
    candidate, _ = ai_output_candidate(
        row,
        selected_style,
        quality,
        "tencent-PreviewReuse",
        selected_background,
    )
    candidate_generation_metadata(candidate, final_metadata)
    return candidate, final_metadata


def materialize_final_images(
    plan: dict[str, Any],
    selected_style: str,
    quality: str | None = "standard",
    selected_background: SelectedBackgroundAsset | None = None,
    execution_guard: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    if execution_guard is not None:
        execution_guard()
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
    exact_failure_cache: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(plan["results"]):
        strip_nonfinal_generated_candidates(row)
        reason = materialization_reason(row, selected_style, selected_background)
        source_candidate = source_candidate_for_generation(row)
        item_result = generation_row_result(row, status["provider"], "Reuse", reason)
        if reason is None:
            generation["skipped"] += 1
            item_result.update({"provider": "library", "status": "reused", "reason": "same_style_reuse"})
            bump_generation_action(generation, "Reuse")
            row["generation"] = item_result
            items_by_index[index] = item_result
            continue

        _, target = ai_output_candidate(row, selected_style, quality, "generated-final", selected_background)
        metadata = load_ai_output_metadata(target) if target.exists() else None
        cache_matches_background = selected_background is None or verified_exact_output_metadata(
            metadata,
            target,
            selected_background,
        )
        if target.exists() and usable_generated_metadata(metadata) and cache_matches_background:
            assert metadata is not None
            cached_action = str(metadata.get("action") or "Cached")
            provider = str(metadata.get("provider") or "local-category")
            source = f"tencent-{cached_action}" if provider == "tencent-hunyuan" else "generated-local"
            ai_candidate, _ = ai_output_candidate(row, selected_style, quality, source, selected_background)
            candidate_generation_metadata(ai_candidate, metadata)
            promote_candidate(row, ai_candidate)
            row["publicStatus"] = "已生成"
            row["backgroundAction"] = "正式生成"
            row["generationStatus"] = "cached"
            item_result.update({"provider": provider, "action": cached_action, "status": "cached", "succeeded": True, "cached": True})
            generation["cached"] += 1
            generation["succeeded"] += 1
            bump_generation_action(generation, "Cached")
            row["generation"] = item_result
            items_by_index[index] = item_result
            continue

        preview_reuse = materialize_verified_preview_as_final(
            row,
            selected_style,
            quality,
            selected_background,
            target,
        )
        if preview_reuse is not None:
            ai_candidate, preview_metadata = preview_reuse
            promote_candidate(row, ai_candidate)
            row["publicStatus"] = "已生成"
            row["backgroundAction"] = "正式生成"
            row["generationStatus"] = "cached"
            item_result.update(
                {
                    "provider": "tencent-hunyuan",
                    "action": "PreviewReuse",
                    "status": "cached",
                    "succeeded": True,
                    "cached": True,
                    "sourceAction": preview_metadata.get("sourceAction"),
                }
            )
            generation["cached"] += 1
            generation["succeeded"] += 1
            bump_generation_action(generation, "PreviewReuse")
            row["generation"] = item_result
            items_by_index[index] = item_result
            continue

        if (
            selected_background is not None
            and postgres_product_runtime_enabled()
        ):
            try:
                reused_asset = materialize_reusable_product_asset(
                    row,
                    selected_style,
                    selected_background,
                    quality,
                )
            except ProductAssetRuntimeError as exc:
                generation["failed"] += 1
                generation["pending"] += 1
                generation["errors"].append(
                    {
                        "dish": row.get("name"),
                        "message": exc.code,
                    }
                )
                item_result.update(
                    {
                        "provider": "asset-library",
                        "action": "AssetLibraryUnavailable",
                        "status": "failed",
                        "error": exc.code,
                    }
                )
                row["backgroundAction"] = "待正式生成"
                row["publicStatus"] = "资产库暂时不可用"
                row["generationStatus"] = "failed"
                row["generation"] = item_result
                bump_generation_action(
                    generation,
                    "AssetLibraryUnavailable",
                )
                items_by_index[index] = item_result
                continue
            if reused_asset is not None:
                ai_candidate, reuse_metadata = reused_asset
                promote_candidate(row, ai_candidate)
                row["publicStatus"] = "已生成"
                row["backgroundAction"] = "正式生成"
                row["generationStatus"] = "reused"
                item_result.update(
                    {
                        "provider": "asset-library",
                        "action": "ApprovedAssetReuse",
                        "status": "reused",
                        "succeeded": True,
                        "cached": True,
                        "assetRecordId": reuse_metadata[
                            "assetRecordId"
                        ],
                    }
                )
                generation["cached"] += 1
                generation["succeeded"] += 1
                bump_generation_action(
                    generation,
                    "ApprovedAssetReuse",
                )
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

    publish_generation_progress(
        progress_callback,
        items_by_index,
        len(plan["results"]),
    )
    if tasks:
        worker_count = min(FINAL_GENERATION_WORKERS, len(tasks))
        if worker_count == 1:
            for index, row, reason, source_candidate, item_result in tasks:
                if execution_guard is not None:
                    execution_guard()
                result = materialize_final_row(
                    row,
                    selected_style,
                    quality,
                    status,
                    reason,
                    source_candidate,
                    item_result,
                    selected_background,
                    execution_guard,
                    exact_failure_cache,
                )
                merge_generation_row_result(generation, result)
                items_by_index[index] = result["item"]
                publish_generation_progress(
                    progress_callback,
                    items_by_index,
                    len(plan["results"]),
                )
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {}
                for (
                    index,
                    row,
                    reason,
                    source_candidate,
                    item_result,
                ) in tasks:
                    context = copy_context()
                    future = executor.submit(
                        context.run,
                        materialize_final_row,
                        row,
                        selected_style,
                        quality,
                        status,
                        reason,
                        source_candidate,
                        item_result,
                        selected_background,
                        execution_guard,
                        exact_failure_cache,
                    )
                    future_map[future] = index
                for future in as_completed(future_map):
                    index = future_map[future]
                    result = future.result()
                    merge_generation_row_result(generation, result)
                    items_by_index[index] = result["item"]
                    publish_generation_progress(
                        progress_callback,
                        items_by_index,
                        len(plan["results"]),
                    )
    if execution_guard is not None:
        execution_guard()
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


def postgres_wallet_request_sha256(
    *,
    direction: str,
    user_id: str,
    order_id: str,
    points: int,
    description: str,
    metadata: dict[str, Any] | None,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "schemaVersion": 1,
                "direction": direction,
                "userId": user_id,
                "orderId": order_id,
                "points": points,
                "description": description,
                "metadata": dict(metadata or {}),
            }
        ).encode("utf-8")
    ).hexdigest()


def postgres_wallet_transaction(
    *,
    direction: str,
    user_id: str,
    result: Any,
) -> dict[str, Any]:
    account = result.account
    order = result.order
    return {
        "ok": True,
        "idempotent": bool(result.idempotent),
        "userId": user_id,
        "orderId": str(order["id"]),
        "direction": direction,
        "points": int(order["points"]),
        "balance": int(account["balance_points"]),
        "balanceAfter": int(account["balance_points"]),
        "ledgerId": None,
        "createdAt": order.get("created_at"),
    }


class ProductWalletUnavailable(billing.BillingError):
    code = "postgres_wallet_unavailable"
    status_code = 503


def postgres_wallet_error(exc: ProductJobStoreError) -> billing.BillingError:
    if isinstance(exc, InsufficientPointBalance):
        return billing.InsufficientBalance(
            required=exc.required_points,
            available=exc.available_points,
        )
    if isinstance(exc, PointOrderConflict):
        return billing.OrderConflict(str(exc))
    if isinstance(exc, InvalidProductJobInput):
        return billing.InvalidBillingInput(str(exc))
    return ProductWalletUnavailable(
        "PostgreSQL point ledger is temporarily unavailable"
    )


def credit_points(
    user_id: str,
    order_id: str,
    points: int,
    *,
    description: str,
    metadata: dict[str, Any] | None = None,
    transaction_effect: Callable[[Any, Any], None] | None = None,
) -> dict[str, Any]:
    if not postgres_product_runtime_enabled():
        return billing.credit_account(
            user_id,
            order_id,
            points,
            description=description,
            metadata=metadata,
        )
    digest = postgres_wallet_request_sha256(
        direction="credit",
        user_id=user_id,
        order_id=order_id,
        points=points,
        description=description,
        metadata=metadata,
    )
    stored_metadata = {
        "description": str(description or "credit"),
        "context": dict(metadata or {}),
    }
    try:
        with postgres_connection() as connection:
            result = ProductJobStore(connection).credit_points(
                owner_user_id=user_id,
                order_id=order_id,
                points=points,
                request_sha256=digest,
                metadata=stored_metadata,
                transaction_effect=transaction_effect,
            )
    except ProductJobStoreError as exc:
        raise postgres_wallet_error(exc) from exc
    except PostgresRuntimeError as exc:
        raise ProductWalletUnavailable(
            "PostgreSQL point ledger is temporarily unavailable"
        ) from exc
    return postgres_wallet_transaction(
        direction="credit",
        user_id=user_id,
        result=result,
    )


def debit_points(
    user_id: str,
    order_id: str,
    points: int,
    *,
    description: str,
    metadata: dict[str, Any] | None = None,
    transaction_effect: Callable[[Any, Any], None] | None = None,
) -> dict[str, Any]:
    if not postgres_product_runtime_enabled():
        return billing.debit_account(
            user_id,
            order_id,
            points,
            description=description,
            metadata=metadata,
        )
    digest = postgres_wallet_request_sha256(
        direction="debit",
        user_id=user_id,
        order_id=order_id,
        points=points,
        description=description,
        metadata=metadata,
    )
    stored_metadata = {
        "description": str(description or "debit"),
        "context": dict(metadata or {}),
    }
    try:
        with postgres_connection() as connection:
            result = ProductJobStore(connection).debit_points(
                owner_user_id=user_id,
                order_id=order_id,
                points=points,
                request_sha256=digest,
                metadata=stored_metadata,
                transaction_effect=transaction_effect,
            )
    except ProductJobStoreError as exc:
        raise postgres_wallet_error(exc) from exc
    except PostgresRuntimeError as exc:
        raise ProductWalletUnavailable(
            "PostgreSQL point ledger is temporarily unavailable"
        ) from exc
    return postgres_wallet_transaction(
        direction="debit",
        user_id=user_id,
        result=result,
    )


def ensure_demo_balance(user_id: str) -> None:
    if not local_demo_billing_allowed(user_id) or DEMO_BALANCE_POINTS <= 0:
        return
    try:
        credit_points(
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
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                stored = ProductJobStore(connection).get_or_create_account(
                    owner_user_id=actual_user_id,
                    metadata={"source": "account_api"},
                ).account
        except ProductJobStoreError as exc:
            raise postgres_wallet_error(exc) from exc
        except PostgresRuntimeError as exc:
            raise ProductWalletUnavailable(
                "PostgreSQL point ledger is temporarily unavailable"
            ) from exc
        account = {
            "userId": actual_user_id,
            "balance": int(stored["balance_points"]),
            "updatedAt": stored.get("updated_at"),
            "rate": f"1 yuan = {POINT_RATE} points",
            "packages": billing.recharge_packages_payload(),
            "customRecharge": {
                "minCash": billing.CUSTOM_RECHARGE_MIN_CASH,
                "rate": POINT_RATE,
            },
            "pricing": billing.pricing_payload(),
        }
    else:
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
        "inviterRegisterReward": 100,
        "inviteeRegisterReward": 20,
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
        "approvedBackgroundCatalog": approved_background_catalog_enabled(),
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


PREVIEW_ANNOUNCEMENT_RE = re.compile(
    r"(?:祝(?:顾客|您)|温馨提示|下单须知|门店公告|联系客服|勿拍|不要下单|仅供展示|图片仅供参考)"
)


def preview_item_taxonomies(item: dict[str, Any]) -> set[str]:
    taxonomies = {str(item.get("taxonomy") or "").strip()}
    for component in item.get("components") or []:
        taxonomy_id = classify_taxonomy(str(component or ""))
        if taxonomy_id:
            taxonomies.add(taxonomy_id)
    return {value for value in taxonomies if value}


def preview_item_is_announcement(item: dict[str, Any]) -> bool:
    return bool(PREVIEW_ANNOUNCEMENT_RE.search(str(item.get("name") or "")))


def select_preview_sample_items(
    menu: dict[str, Any],
    limit: int = PREVIEW_SAMPLE_COUNT,
) -> list[dict[str, Any]]:
    items = [
        item
        for item in menu.get("items", [])
        if str(item.get("name") or "").strip()
        and not preview_item_is_announcement(item)
    ]
    target_taxonomy = str(category_report(menu).get("taxonomyId") or "").strip()
    if target_taxonomy in {TAXONOMY_UNKNOWN, TAXONOMY_COMBO, background_profiles.MIXED_CATEGORY_ID}:
        target_taxonomy = ""

    primary_singles = [
        item
        for item in items
        if item.get("kind") == "单品"
        and target_taxonomy
        and str(item.get("taxonomy") or "") == target_taxonomy
    ]
    primary_combos = [
        item
        for item in items
        if item.get("kind") == "套餐/组合"
        and target_taxonomy
        and target_taxonomy in preview_item_taxonomies(item)
    ]
    other_combos = [item for item in items if item.get("kind") == "套餐/组合"]
    known_singles = [
        item
        for item in items
        if item.get("kind") == "单品"
        and str(item.get("taxonomy") or "") not in {"", TAXONOMY_UNKNOWN, TAXONOMY_COMBO}
    ]
    known_combos = [
        item
        for item in items
        if item.get("kind") == "套餐/组合"
        and preview_item_taxonomies(item) - {TAXONOMY_UNKNOWN, TAXONOMY_COMBO}
    ]
    other_singles = [item for item in items if item.get("kind") == "单品"]
    other_items = [item for item in items if item.get("kind") != "单品"]

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(pool: list[dict[str, Any]]) -> None:
        for item in pool:
            norm = str(item.get("norm") or normalize(str(item.get("name") or "")))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            selected.append(item)
            if len(selected) >= limit:
                return

    add(primary_combos[:2])
    add(primary_singles)
    add(primary_combos[2:])
    add(other_combos)
    add(known_singles)
    add(known_combos)
    add(other_singles)
    add(other_items)
    return selected[:limit]


def preview_sample_entries() -> list[dict[str, Any]]:
    menu = parse_menu()
    library = library_images()
    sample_items = select_preview_sample_items(menu)
    seen_norms = {item.get("norm") for item in sample_items}
    if len(sample_items) < PREVIEW_SAMPLE_COUNT:
        for item in demo_menu_items():
            if item.get("kind") != "单品" or item.get("norm") in seen_norms:
                continue
            sample_items.append({**item, "category": "风格样图"})
            seen_norms.add(item.get("norm"))
            if len(sample_items) >= PREVIEW_SAMPLE_COUNT:
                break
    entries = []
    for item in sample_items[:PREVIEW_SAMPLE_COUNT]:
        if not item.get("norm"):
            item = {**item, "norm": normalize(str(item.get("name") or ""))}
        candidates = top_candidates(item, library)
        entries.append({"item": item, "candidates": candidates})
    return entries


def preview_sample_payload_from_entry(
    selected_style: str,
    entry: dict[str, Any],
    generate: bool = True,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any]:
    item = entry["item"]
    candidates = entry["candidates"]
    preview_item = {**item, "candidates": candidates}
    candidate = None
    generation: dict[str, Any]
    if generate:
        candidate, generation = materialize_preview_candidate(
            preview_item,
            selected_style,
            "standard",
            selected_background,
        )
    else:
        candidate = generated_preview_candidate(preview_item, selected_style, selected_background)
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


def preview_sample_payload(
    selected_style: str,
    index: int,
    generate: bool = True,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any]:
    entries = preview_sample_entries()
    if index < 0 or index >= len(entries):
        raise IndexError("样图序号不存在")
    return preview_sample_payload_from_entry(
        selected_style,
        entries[index],
        generate=generate,
        selected_background=selected_background,
    )


def failed_preview_sample_payload(selected_style: str, entry: dict[str, Any], exc: Exception) -> dict[str, Any]:
    item = entry["item"]
    candidates = entry["candidates"]
    generation = preview_provider_failure(exc)
    app.logger.warning(
        "Preview sample failed for row %s (%s): %s",
        item.get("row"),
        generation["errorCode"],
        sanitized_provider_error(exc),
    )
    return {
        **item,
        "candidate": None,
        "sourceCandidates": visible_source_candidates(candidates, 3),
        "generation": generation,
        "points": 0,
        "publicStatus": "样图生成失败",
        "style": selected_style,
    }


def submit_preview_sample_with_context(
    executor: ThreadPoolExecutor,
    *args: Any,
):
    context = copy_context()
    return executor.submit(
        context.run,
        preview_sample_payload_from_entry,
        *args,
    )


def preview_samples(
    selected_style: str,
    generate: bool = False,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any]:
    entries = preview_sample_entries()
    if generate and entries:
        samples_by_index: dict[int, dict[str, Any]] = {}
        worker_count = min(FINAL_GENERATION_WORKERS, len(entries))
        if worker_count == 1:
            for index, entry in enumerate(entries):
                try:
                    samples_by_index[index] = (
                        preview_sample_payload_from_entry(
                            selected_style,
                            entry,
                            generate=True,
                            selected_background=selected_background,
                        )
                        if selected_background is not None
                        else preview_sample_payload_from_entry(selected_style, entry, generate=True)
                    )
                except PreviewObjectStorageError:
                    raise
                except Exception as exc:
                    samples_by_index[index] = failed_preview_sample_payload(selected_style, entry, exc)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                if selected_background is not None:
                    future_map = {
                        submit_preview_sample_with_context(
                            executor,
                            selected_style,
                            entry,
                            True,
                            selected_background,
                        ): index
                        for index, entry in enumerate(entries)
                    }
                else:
                    future_map = {
                        submit_preview_sample_with_context(
                            executor,
                            selected_style,
                            entry,
                            True,
                        ): index
                        for index, entry in enumerate(entries)
                    }
                for future in as_completed(future_map):
                    index = future_map[future]
                    try:
                        samples_by_index[index] = future.result()
                    except PreviewObjectStorageError:
                        raise
                    except Exception as exc:
                        samples_by_index[index] = failed_preview_sample_payload(selected_style, entries[index], exc)
        samples = [samples_by_index[index] for index in sorted(samples_by_index)]
    else:
        samples = (
            [
                preview_sample_payload_from_entry(
                    selected_style,
                    entry,
                    generate=generate,
                    selected_background=selected_background,
                )
                for entry in entries
            ]
            if selected_background is not None
            else [
                preview_sample_payload_from_entry(selected_style, entry, generate=generate)
                for entry in entries
            ]
        )
    payload = {
        "style": selected_style,
        "styleName": STYLE_COLORS.get(selected_style, ("上传风格", None, None))[0],
        "samples": samples,
        "previewFreeImages": PREVIEW_SAMPLE_COUNT,
    }
    if selected_background is not None:
        payload["selectedBackground"] = selected_background.public_payload()
    return payload


def build_plan(
    selected_style: str = "",
    quality: str | None = "standard",
    selected_background: SelectedBackgroundAsset | None = None,
    *,
    menu_snapshot: dict[str, Any] | None = None,
    account_user_id: str | None = None,
    include_account: bool = True,
) -> dict[str, Any]:
    if selected_style:
        selected_style = safe_style_path_segment(selected_style)
    menu = menu_snapshot if menu_snapshot is not None else parse_menu()
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
            final_image = existing_ai_output_candidate(
                row,
                selected_style,
                quality_info["id"],
                selected_background,
            )
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
    payload = {
        "menu": {k: v for k, v in menu.items() if k != "items"},
        "category": category_report(menu),
        "standardization": standardization_report(menu),
        "assetLayer": {"libraryImages": len(library), "libraryStores": len({x.store for x in library}), "menus": menu_count},
        "styles": styles,
        "selectedStyle": selected_style,
        "summary": summary,
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
                "inviterRegisterReward": 100,
                "inviteeRegisterReward": 20,
                "firstPayReward": "直接邀请首充返 10% 积分，仅限一级邀请，不能提现",
                "expireDays": 180,
            },
        },
        "results": results,
    }
    if include_account:
        payload["account"] = account_payload(account_user_id)
    if selected_background is not None:
        payload["selectedBackground"] = selected_background.public_payload()
    return payload


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


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "waimai-image-tool"})


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
    menu_path, principal, menu_upload_id, menu_error = (
        resolve_customer_preview_menu()
    )
    if menu_error is not None:
        return menu_error
    assert principal is not None
    with customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        try:
            style = validate_requested_style(request.args.get("style", ""), allow_empty=True)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            plan = build_plan(
                style,
                request.args.get("quality", "standard"),
            )
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        return jsonify(public_plan_payload(plan))


@app.get("/api/style-background")
def api_style_background():
    menu_path, principal, menu_upload_id, menu_error = (
        resolve_customer_preview_menu()
    )
    if menu_error is not None:
        return menu_error
    assert principal is not None
    with customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        try:
            style = validate_requested_style(request.args.get("style", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        generate = str(request.args.get("generate") or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            sample = style_sample_candidate(style, generate=generate)
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        return jsonify(public_style_payload({
            "id": style,
            "styleId": style,
            "name": style_name_for(style),
            "sample": sample,
        }))


@app.get("/api/background-catalog")
def api_background_catalog():
    menu_path, principal, menu_upload_id, menu_error = (
        resolve_customer_preview_menu()
    )
    if menu_error is not None:
        return menu_error
    assert principal is not None
    with customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        if not approved_background_catalog_enabled():
            return jsonify(
                {
                    "mode": "live-generation",
                    "status": "disabled",
                    "ready": False,
                    "styles": [],
                }
            )
        manifest = approved_background_catalog_manifest()
        styles = []
        records_by_style = manifest.get("_recordsByStyle") or {}
        for index, style_id in enumerate(
            background_catalog.STYLE_IDS,
            start=1,
        ):
            try:
                sample = (
                    materialize_approved_background_candidate(
                        style_id,
                        records_by_style[style_id],
                    )
                    if manifest.get("ready")
                    and isinstance(records_by_style.get(style_id), dict)
                    else catalog_status_background_candidate(
                        style_id,
                        manifest,
                    )
                )
            except ProductAssetRuntimeError as exc:
                return preview_object_storage_error_response(
                    PreviewObjectStorageError(
                        exc.code,
                        "已审核背景资产暂时不可用，请稍后重试",
                    )
                )
            styles.append(
                public_style_payload(
                    {
                        "id": style_id,
                        "styleId": style_id,
                        "name": BACKGROUND_LABELS[index - 1],
                        "rawName": style_name_for(style_id),
                        "slot": background_catalog.style_slot(
                            style_id
                        ).public_payload(),
                        "sample": sample,
                    }
                )
            )
        public_manifest = strip_sensitive_public_payload(manifest)
        public_manifest["styles"] = styles
        return jsonify(public_manifest)


def requested_selected_background(
    style: str,
    payload: dict[str, Any] | None = None,
) -> SelectedBackgroundAsset | None:
    source = request.args if payload is None else payload
    asset_id = str(source.get("backgroundAssetId") or source.get("background_asset_id") or "").strip()
    sha256 = str(source.get("backgroundSha256") or source.get("background_sha256") or "").strip().lower()
    identity_required = env_truthy(
        "REQUIRE_SELECTED_BACKGROUND_IDENTITY",
        default=render_runtime_detected(),
    )
    if not asset_id and not sha256:
        if identity_required:
            raise SelectedBackgroundError(
                "selected_background_identity_required",
                "请重新选择背景后再生成图片",
            )
        return None
    if not asset_id or not sha256:
        raise SelectedBackgroundError(
            "selected_background_identity_incomplete",
            "所选背景身份不完整，请重新选择背景",
        )
    return resolve_selected_background(
        style,
        expected_asset_id=asset_id,
        expected_sha256=sha256,
    )


def selected_background_error_response(exc: SelectedBackgroundError):
    status = 409 if exc.code != "selected_background_not_ready" else 425
    return jsonify({"error": str(exc), "code": exc.code}), status


def preview_object_storage_error_response(
    exc: PreviewObjectStorageError,
):
    app.logger.error(
        "Private preview object storage failure (%s): %s",
        exc.code,
        str(exc),
    )
    return (
        jsonify(
            {
                "error": "私有样图存储暂时不可用，请稍后重试",
                "code": exc.code,
            }
        ),
        503,
    )


@app.get("/api/style-preview")
def api_style_preview():
    menu_path, principal, menu_upload_id, menu_error = (
        resolve_customer_preview_menu()
    )
    if menu_error is not None:
        return menu_error
    assert principal is not None
    with customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        try:
            style = validate_requested_style(request.args.get("style", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            selected_background = requested_selected_background(style)
        except SelectedBackgroundError as exc:
            return selected_background_error_response(exc)
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        generate = str(request.args.get("generate") or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            preview = (
                preview_samples(
                    style,
                    generate=generate,
                    selected_background=selected_background,
                )
                if selected_background is not None
                else preview_samples(style, generate=generate)
            )
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        return jsonify(
            public_preview_payload(
                preview
            )
        )


@app.get("/api/style-preview-sample")
def api_style_preview_sample():
    menu_path, principal, menu_upload_id, menu_error = (
        resolve_customer_preview_menu()
    )
    if menu_error is not None:
        return menu_error
    assert principal is not None
    with customer_preview_menu_path(
        menu_path,
        principal,
        menu_upload_id,
    ):
        try:
            style = validate_requested_style(request.args.get("style", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        raw_index = request.args.get("index")
        if raw_index is None or not raw_index.strip():
            return jsonify({"error": "样图序号必须是整数"}), 400
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return jsonify({"error": "样图序号必须是整数"}), 400
        try:
            selected_background = requested_selected_background(style)
        except SelectedBackgroundError as exc:
            return selected_background_error_response(exc)
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        try:
            sample = (
                preview_sample_payload(
                    style,
                    index,
                    generate=True,
                    selected_background=selected_background,
                )
                if selected_background is not None
                else preview_sample_payload(style, index, generate=True)
            )
            return jsonify(
                public_preview_payload(
                    {
                        "style": style,
                        "index": index,
                        "selectedBackground": selected_background.public_payload() if selected_background else None,
                        "sample": sample,
                    }
                )
            )
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        except IndexError:
            return jsonify({"error": "样图序号不存在"}), 404


@app.get("/api/menu-status")
def api_menu_status():
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    requested_upload_id = str(request.args.get("menuUploadId") or "").strip()
    try:
        snapshot = (
            resolve_menu_upload_snapshot(requested_upload_id, principal)
            if requested_upload_id
            else latest_menu_upload_snapshot(principal)
        )
    except MenuUploadError as exc:
        return menu_upload_error_response(exc)
    if snapshot is None:
        return jsonify({"uploaded": False})
    return jsonify(
        {
            "uploaded": True,
            "menuUploadId": snapshot["id"],
            "menu": snapshot["summary"],
        }
    )


@app.get("/api/account")
def api_account():
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    return jsonify(account_payload(str(principal["userId"])))


@app.post("/api/auth/request-otp")
def api_auth_request_otp():
    payload = request.get_json(silent=True) or {}
    local_demo = local_demo_auth_allowed()
    durable_auth = postgres_product_runtime_enabled()
    sms_provider = sms_service.provider_from_env(
        local_demo_enabled=local_demo and not durable_auth
    )
    try:
        sms_provider.ensure_available()
    except sms_service.SmsServiceError as exc:
        return sms_error_response(exc)

    conn: sqlite3.Connection | None = None
    durable_otp_store: product_otp_store.ProductOtpStore | None = None
    try:
        if durable_auth:
            durable_otp_store = product_otp_store.product_otp_store_from_env()
            requested = durable_otp_store.request_otp(
                phone=str(payload.get("phone") or ""),
                ip=request_ip(),
                idempotency_key=(
                    str(request.headers.get("Idempotency-Key") or "").strip()
                    or None
                ),
            )
            challenge = {
                "challenge_id": requested.challenge_id,
                "code": requested.code,
                "phone": requested.phone,
                "expires_at": requested.expires_at,
            }
            ttl_seconds = durable_otp_store.config.otp_ttl_seconds
        else:
            conn = product_db_conn()
            challenge = auth_service.request_otp(
                conn,
                str(payload.get("phone") or ""),
                ip=request_ip(),
                user_agent=request.headers.get("User-Agent", ""),
            )
            ttl_seconds = auth_service.OTP_TTL_SECONDS
        sms_result = sms_provider.send_otp(
            phone=challenge["phone"],
            code=challenge["code"],
            ttl_seconds=ttl_seconds,
            purpose="login",
        )
    except auth_service.AuthError as exc:
        return auth_error_response(exc)
    except (
        product_otp_store.ProductOtpStoreError,
        product_auth_store.ProductAuthStoreError,
        PostgresRuntimeError,
    ) as exc:
        return product_auth_error_response(exc)
    except sms_service.SmsServiceError as exc:
        return sms_error_response(exc)
    finally:
        if conn is not None:
            conn.close()
        close_product_otp_client(durable_otp_store)

    body = {
        "ok": True,
        "challenge_id": challenge["challenge_id"],
        "challengeId": challenge["challenge_id"],
        "phone": challenge["phone"],
        "expires_at": challenge["expires_at"],
        "expiresAt": challenge["expires_at"],
        "sms": sms_result.public_payload(),
    }
    expose_mock_otp = (not durable_auth) and (
        env_truthy("AUTH_EXPOSE_MOCK_OTP", default=False) or local_demo
    )
    if expose_mock_otp:
        body["mockCode"] = challenge["code"]
    return jsonify(body)


@app.post("/api/auth/verify-otp")
def api_auth_verify_otp():
    payload = request.get_json(silent=True) or {}
    challenge_id = str(payload.get("challengeId") or payload.get("challenge_id") or "")
    code = str(payload.get("code") or payload.get("otp") or "")
    durable_auth = postgres_product_runtime_enabled()
    conn: sqlite3.Connection | None = None
    durable_otp_store: product_otp_store.ProductOtpStore | None = None
    try:
        if durable_auth:
            durable_otp_store = product_otp_store.product_otp_store_from_env()
            verified = durable_otp_store.verify_otp(
                challenge_id=challenge_id,
                code=code,
            )
            with postgres_connection() as connection:
                auth_store = product_auth_store.product_auth_store_from_env(
                    connection
                )
                user_result = auth_store.get_or_create_user(
                    phone=verified.phone,
                    metadata={"source": "sms_otp"},
                )
                issued = auth_store.issue_session(
                    user_id=str(user_result.user["id"]),
                    registration_context={
                        "is_new_user": bool(user_result.created),
                        "ip": request_ip(),
                        "user_agent": request.headers.get("User-Agent", ""),
                    },
                )
                stores = auth_store.list_user_stores(
                    user_id=str(user_result.user["id"])
                )
            result = {
                "user": issued.user,
                "session": {
                    **issued.session,
                    "token": issued.token,
                },
            }
        else:
            conn = product_db_conn()
            result = auth_service.verify_otp(conn, challenge_id, code)
            stores = auth_service.list_user_stores(
                conn,
                result["user"]["id"],
            )
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
    except (
        product_otp_store.ProductOtpStoreError,
        product_auth_store.ProductAuthStoreError,
        PostgresRuntimeError,
    ) as exc:
        return product_auth_error_response(exc)
    finally:
        if conn is not None:
            conn.close()
        close_product_otp_client(durable_otp_store)


@app.get("/api/auth/session")
def api_auth_session():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    try:
        stores = list_auth_user_stores(str(session["user_id"]))
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        return product_auth_error_response(exc)
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
    try:
        if postgres_product_runtime_enabled():
            try:
                with postgres_connection() as connection:
                    store = product_auth_store.product_auth_store_from_env(
                        connection
                    )
                    logged_out = store.revoke_session(token=token).revoked
            except product_auth_store.InvalidProductAuthInput:
                logged_out = False
        else:
            conn = product_db_conn()
            try:
                logged_out = auth_service.logout(conn, token)
            finally:
                conn.close()
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        return product_auth_error_response(exc)
    return jsonify({"ok": True, "loggedOut": logged_out})


@app.get("/api/stores")
def api_list_stores():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    try:
        stores = list_auth_user_stores(str(session["user_id"]))
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        return product_auth_error_response(exc)
    return jsonify({"ok": True, "stores": stores})


@app.post("/api/stores")
def api_create_store():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    payload = request.get_json(silent=True) or {}
    try:
        store = create_auth_user_store(
            str(session["user_id"]),
            str(payload.get("name") or ""),
        )
    except auth_service.AuthError as exc:
        return auth_error_response(exc)
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        return product_auth_error_response(exc)
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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        metadata = (
            payload.get("metadata")
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        actor_user_id = (
            admin_actor_user_id() if admin_allowed else session_user_id
        )
        try:
            with postgres_connection() as connection:
                cursor = connection.cursor()
                try:
                    result = product_growth_store.create_or_get_agent(
                        cursor,
                        tenant_id=product_growth_tenant_id(),
                        owner_user_id=user_id,
                        idempotency_key=idempotency_key,
                        agent_code=str(
                            payload.get("agentCode")
                            or payload.get("agent_code")
                            or ""
                        ),
                        metadata=metadata,
                        actor_user_id=actor_user_id,
                    )
                    product_finance_store.ensure_agent_finance_account(
                        cursor,
                        agent_id=str(result.record["id"]),
                        metadata={
                            "growthTenantId": product_growth_tenant_id(),
                            "growthAgentOwnerUserId": user_id,
                        },
                    )
                    connection.commit()
                finally:
                    cursor.close()
        except (
            product_growth_store.ProductGrowthStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_growth_error_response(exc)
        except product_finance_store.ProductFinanceStoreError as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "idempotent": not result.created,
                "agent": product_growth_agent_view(result.record),
            }
        )

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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        try:
            with postgres_connection() as connection:
                result = product_growth_store.ProductGrowthStore(
                    connection
                ).bind_agent_customer(
                    tenant_id=product_growth_tenant_id(),
                    owner_user_id=str(
                        payload.get("customerId")
                        or payload.get("customer_id")
                        or ""
                    ),
                    agent_id=str(
                        payload.get("agentId")
                        or payload.get("agent_id")
                        or ""
                    ),
                    idempotency_key=idempotency_key,
                    source=str(payload.get("source") or "manual"),
                    metadata=(
                        payload.get("metadata")
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                    actor_user_id=admin_actor_user_id(),
                )
        except (
            product_growth_store.ProductGrowthStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_growth_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "idempotent": not result.created,
                "relation": product_growth_binding_view(result.record),
            }
        )

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


@app.post("/api/growth/invites/code")
def api_issue_consumer_invite_code():
    session, error = require_authenticated_session()
    if error:
        return error
    assert session is not None
    if not postgres_product_runtime_enabled():
        return jsonify(
            {
                "error": "邀请链接需要持久化增长服务",
                "code": "durable_growth_store_required",
            }
        ), 503
    idempotency_key, idempotency_error = (
        product_idempotency_key_required()
    )
    if idempotency_error is not None:
        return idempotency_error
    owner_user_id = str(session["user_id"])
    try:
        invite_code = product_growth_invite_code(owner_user_id)
    except ValueError:
        return jsonify(
            {
                "error": "邀请链接服务未配置",
                "code": "growth_invite_code_secret_required",
            }
        ), 503
    try:
        with postgres_connection() as connection:
            result = product_growth_store.ProductGrowthStore(
                connection
            ).issue_consumer_invite_code(
                tenant_id=product_growth_tenant_id(),
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                plaintext_code=invite_code,
                metadata={"source": "customer_api"},
                actor_user_id=owner_user_id,
            )
    except (
        product_growth_store.ProductGrowthStoreError,
        PostgresRuntimeError,
    ) as exc:
        return product_growth_error_response(exc)
    return jsonify(
        {
            "ok": True,
            "idempotent": not result.created,
            "inviteCode": invite_code,
            "inviteCodeId": str(result.record["id"]),
            "ruleVersion": str(result.record["rule_version"]),
            "createdAt": result.record.get("created_at"),
        }
    )


@app.post("/api/growth/invites/accept")
def api_accept_consumer_invite():
    payload = request.get_json(silent=True) or {}
    if postgres_product_runtime_enabled():
        session, error = require_authenticated_session()
        if error:
            return error
        assert session is not None
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        invitee_user_id = str(session["user_id"])
        invite_code = str(
            payload.get("inviteCode")
            or payload.get("invite_code")
            or ""
        )
        try:
            with postgres_connection() as connection:
                auth_context = (
                    product_auth_store.product_auth_store_from_env(
                        connection
                    ).registration_session_context(
                        user_id=invitee_user_id,
                        session_id=str(session["id"]),
                    )
                )
        except (
            PostgresRuntimeError,
            product_auth_store.ProductAuthStoreError,
        ) as exc:
            return product_auth_error_response(exc)

        try:
            with postgres_connection() as connection:
                cursor = connection.cursor()
                try:
                    tenant_id = product_growth_tenant_id()
                    existing = (
                        product_growth_store.get_invite_for_invitee(
                            cursor,
                            tenant_id=tenant_id,
                            owner_user_id=invitee_user_id,
                        )
                    )
                    resolved_code = (
                        product_growth_store.resolve_consumer_invite_code(
                            cursor,
                            tenant_id=tenant_id,
                            invite_code=invite_code,
                        )
                    )
                    if resolved_code is None:
                        raise product_growth_store.ProductGrowthNotFound(
                            "invite code not found in tenant"
                        )
                    if existing is None:
                        durable_risk_blocked = (
                            product_admin_security_store
                            .registration_risk_blocked(
                                cursor,
                                user_id=invitee_user_id,
                                ip=request_ip(),
                            )
                        )
                        auth_context = {
                            **auth_context,
                            "risk_blocked": bool(
                                auth_context["risk_blocked"]
                                or durable_risk_blocked
                            ),
                        }
                        decision = (
                            growth_rules.invite_registration_reward_decision(
                                phone_verified=bool(
                                    auth_context["phone_verified"]
                                ),
                                human_verified=bool(
                                    auth_context["human_verified"]
                                ),
                                same_phone_registered=bool(
                                    auth_context["same_phone_registered"]
                                ),
                                same_device_recent_registrations=int(
                                    auth_context[
                                        "same_device_recent_registrations"
                                    ]
                                ),
                                same_ip_recent_registrations=int(
                                    auth_context[
                                        "same_ip_recent_registrations"
                                    ]
                                ),
                                self_invite=hmac.compare_digest(
                                    str(resolved_code["owner_user_id"]),
                                    invitee_user_id,
                                ),
                                risk_blocked=bool(
                                    auth_context["risk_blocked"]
                                ),
                            )
                        )
                        risk_snapshot = {
                            **auth_context,
                            "reward_allowed": decision.allowed,
                            "reward_reasons": list(decision.reasons),
                        }
                        invite_metadata = {"source": "customer_api"}
                    else:
                        risk_snapshot = dict(
                            existing.get("risk_snapshot") or {}
                        )
                        invite_metadata = dict(
                            existing.get("metadata") or {}
                        )
                        decision = (
                            growth_rules.invite_registration_reward_decision(
                                phone_verified=bool(
                                    risk_snapshot.get("phone_verified")
                                ),
                                human_verified=bool(
                                    risk_snapshot.get("human_verified")
                                ),
                                same_phone_registered=bool(
                                    risk_snapshot.get(
                                        "same_phone_registered"
                                    )
                                ),
                                same_device_recent_registrations=int(
                                    risk_snapshot.get(
                                        "same_device_recent_registrations"
                                    )
                                    or 0
                                ),
                                same_ip_recent_registrations=int(
                                    risk_snapshot.get(
                                        "same_ip_recent_registrations"
                                    )
                                    or 0
                                ),
                                self_invite=False,
                                risk_blocked=bool(
                                    risk_snapshot.get("risk_blocked")
                                ),
                            )
                        )

                    result = product_growth_store.accept_consumer_invite(
                        cursor,
                        tenant_id=tenant_id,
                        owner_user_id=invitee_user_id,
                        idempotency_key=idempotency_key,
                        invite_code=invite_code,
                        risk_snapshot=risk_snapshot,
                        metadata=invite_metadata,
                        actor_user_id=invitee_user_id,
                    )
                    event_result = None
                    if decision.allowed:
                        dedupe_key = (
                            product_growth_outbox.stable_growth_dedupe_key(
                                product_growth_outbox.EVENT_INVITE_REWARD,
                                tenant_id,
                                str(result.record["id"]),
                                growth_rules.GROWTH_RULE_VERSION,
                            )
                        )
                        event_result = (
                            product_growth_outbox.enqueue_growth_event(
                                cursor,
                                event_type=(
                                    product_growth_outbox.EVENT_INVITE_REWARD
                                ),
                                dedupe_key=dedupe_key,
                                payload={
                                    "schemaVersion": 1,
                                    "ruleVersion": (
                                        growth_rules.GROWTH_RULE_VERSION
                                    ),
                                    "tenantId": tenant_id,
                                    "inviteRelationId": str(
                                        result.record["id"]
                                    ),
                                    "inviteeUserId": invitee_user_id,
                                    "inviterUserId": str(
                                        result.record["inviter_user_id"]
                                    ),
                                },
                            )
                        )
                    connection.commit()
                finally:
                    cursor.close()
        except (
            product_growth_store.ProductGrowthStoreError,
            product_growth_outbox.ProductGrowthOutboxError,
            PostgresRuntimeError,
        ) as exc:
            return product_growth_error_response(exc)
        except product_admin_security_store.ProductAdminSecurityStoreError as exc:
            app.logger.exception(
                "PostgreSQL registration risk lookup unavailable"
            )
            return jsonify(
                {
                    "error": "注册风控暂时不可用",
                    "code": "postgres_admin_security_unavailable",
                    "reason": type(exc).__name__,
                }
            ), 503

        event_status = (
            str(event_result.event["status"])
            if event_result is not None
            else ""
        )
        reward_status = {
            "succeeded": "granted",
            "dead_letter": "failed",
        }.get(event_status, "pending" if decision.allowed else "canceled")
        response_body = {
            "ok": True,
            "idempotent": not result.created,
            "invite": product_growth_invite_view(
                result.record,
                reward_status=reward_status,
            ),
            "billing": [],
        }
        if event_result is not None:
            response_body["growthEvent"] = {
                "id": str(event_result.event["id"]),
                "status": event_status,
                "idempotent": not event_result.created,
            }
        return jsonify(response_body)

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
            if postgres_product_runtime_enabled():
                with postgres_connection() as connection:
                    auth_context = (
                        product_auth_store.product_auth_store_from_env(
                            connection
                        ).registration_session_context(
                            user_id=session_user_id,
                            session_id=str(session["id"]),
                        )
                    )
            else:
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
    except (
        PostgresRuntimeError,
        product_auth_store.ProductAuthStoreError,
    ) as exc:
        conn.close()
        return product_auth_error_response(exc)
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
                credit_points(
                    str(invite["inviterUserId"]),
                    f"referral-register-inviter:{invite['id']}",
                    inviter_points,
                    description="referral-register-inviter",
                    metadata={"inviteId": invite["id"], "inviteeUserId": invite["inviteeUserId"]},
                )
            )
        if invitee_points > 0:
            billing_results.append(
                credit_points(
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
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                agent, agent_error = product_growth_agent_for_session(
                    connection,
                    session,
                    requested_agent_id,
                )
                if agent_error:
                    return agent_error
                assert agent is not None
                balance = product_finance_store.ProductFinanceStore(
                    connection
                ).get_withdrawable_balance(agent_id=str(agent["id"]))
        except product_growth_store.ProductGrowthStoreError as exc:
            return product_growth_error_response(exc)
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        except (TypeError, ValueError) as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "invalid_finance_input",
                }
            ), 400
        return jsonify(
            {
                "ok": True,
                "agentId": str(agent["id"]),
                "balance": product_api_record(balance),
            }
        )

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
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                agent, agent_error = product_growth_agent_for_session(
                    connection,
                    session,
                    requested_agent_id,
                )
                if agent_error:
                    return agent_error
                assert agent is not None
                records = product_finance_store.ProductFinanceStore(
                    connection
                ).list_withdrawals(
                    agent_id=str(agent["id"]),
                    status=str(request.args.get("status") or ""),
                    limit=request.args.get("limit") or 50,
                )
        except product_growth_store.ProductGrowthStoreError as exc:
            return product_growth_error_response(exc)
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "agentId": str(agent["id"]),
                "withdrawals": product_api_record(records),
            }
        )

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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        try:
            with postgres_connection() as connection:
                agent, agent_error = product_growth_agent_for_session(
                    connection,
                    session,
                    requested_agent_id,
                )
                if agent_error:
                    return agent_error
                assert agent is not None
                withdrawal_id = product_growth_store.stable_growth_id(
                    "withdrawal",
                    product_growth_tenant_id(),
                    str(agent["id"]),
                    idempotency_key,
                )
                result = product_finance_store.ProductFinanceStore(
                    connection
                ).create_withdrawal(
                    withdrawal_id=withdrawal_id,
                    agent_id=str(agent["id"]),
                    idempotency_key=idempotency_key,
                    amount_cents=(
                        payload.get("amountCents")
                        or payload.get("amount_cents")
                        or 0
                    ),
                    account_snapshot=(
                        account_snapshot
                        if isinstance(account_snapshot, dict)
                        else {}
                    ),
                    metadata={
                        **metadata,
                        "source": "customer_api",
                        "userId": str(session["user_id"]),
                    },
                )
        except product_growth_store.ProductGrowthStoreError as exc:
            return product_growth_error_response(exc)
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return (
            jsonify(
                {
                    "ok": True,
                    "idempotent": not result.created,
                    "agentId": str(agent["id"]),
                    "withdrawal": product_api_record(result.record),
                }
            ),
            201 if result.created else 200,
        )

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
    session, session_error = require_authenticated_session()
    if session_error is not None:
        return session_error
    assert session is not None

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    provider = requested_payment_provider({})
    try:
        payment_service.ensure_payment_checkout_available(provider, os.environ)
        if postgres_payment_runtime_enabled(provider):
            idempotency_key = str(
                request.headers.get("Idempotency-Key") or ""
            ).strip()
            if not idempotency_key:
                return (
                    jsonify(
                        {
                            "error": "Idempotency-Key header is required",
                            "code": "idempotency_key_required",
                        }
                    ),
                    400,
                )
            package = payment_catalog.resolve_client_package(payload)
            with postgres_connection() as connection:
                store = product_payment_store.ProductPaymentStore(connection)
                created = store.create_or_get_order(
                    owner_user_id=str(session["user_id"]),
                    provider=provider,
                    package_id=str(package["packageId"]),
                    idempotency_key=idempotency_key,
                )
                stored_order = created.order
                if not stored_order.get("provider_payload"):
                    checkout_order = product_payment_order_payload(
                        stored_order,
                        idempotent=not created.created,
                    )
                    checkout = payment_service.create_payment_checkout(
                        checkout_order,
                        os.environ,
                    )
                    stored_order = store.attach_provider_payload(
                        owner_user_id=str(session["user_id"]),
                        order_id=str(stored_order["id"]),
                        provider_payload=checkout,
                    )
                order = product_payment_order_payload(
                    stored_order,
                    idempotent=not created.created,
                )
        else:
            conn = product_db_conn()
            try:
                order = payment_service.create_catalog_payment_order(
                    conn,
                    authenticated_user_id=str(session["user_id"]),
                    request=payload,
                    provider=provider,
                    idempotency_key=str(
                        request.headers.get("Idempotency-Key") or ""
                    ).strip() or None,
                )
                if order["provider"] != "fake":
                    checkout = payment_service.create_payment_checkout(
                        order,
                        os.environ,
                    )
                    order = payment_service.attach_payment_provider_payload(
                        conn,
                        order["order_id"],
                        checkout,
                    )
            finally:
                conn.close()
    except payment_service.PaymentServiceError as exc:
        return payment_error_response(exc)
    except payment_catalog.PaymentCatalogError as exc:
        return payment_error_response(
            payment_service.InvalidPaymentInput(str(exc))
        )
    except (
        product_payment_store.ProductPaymentStoreError,
        PostgresRuntimeError,
    ) as exc:
        return product_payment_error_response(exc)
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


def apply_payment_callback_effects(
    result: dict[str, Any],
    *,
    provider: str,
    provider_order_id: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    billing_result = None
    billing_error = None
    billing_error_status = 409
    growth_result = None
    order = result["order"]
    user_id = str(order["user_id"])
    try:
        points_to_credit = int(result.get("points_to_credit") or result.get("pointsToCredit") or 0)
        points_to_refund = int(result.get("points_to_refund") or result.get("pointsToRefund") or 0)
        if result.get("idempotent") is True:
            status = str(result.get("status") or "")
            if status == payment_service.STATUS_PAID and points_to_credit <= 0:
                # The payment event may have committed before the wallet mutation
                # failed. Replaying the same idempotent wallet order closes that
                # crash window without double-crediting.
                points_to_credit = int(order.get("points") or 0)
            elif status == payment_service.STATUS_REFUNDED and points_to_refund <= 0:
                paid_cents = int(order.get("amount_cents") or order.get("amountCents") or 0)
                original_points = int(order.get("points") or 0)
                refund_cents = min(
                    max(refund_cents_from_payload(payload, order), 0),
                    paid_cents,
                )
                if paid_cents > 0:
                    points_to_refund = min(
                        original_points,
                        refund_cents * original_points // paid_cents,
                    )
        if points_to_credit > 0:
            billing_result = credit_points(
                user_id,
                f"payment:{order['order_id']}",
                points_to_credit,
                description="payment",
                metadata={"provider": provider, "providerOrderId": provider_order_id},
            )
        elif points_to_refund > 0:
            billing_result = debit_points(
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

    body: dict[str, Any] = {
        "ok": billing_error is None,
        "callback": result,
        "billing": billing_result,
        "growth": growth_result,
        "account": account_payload(user_id),
    }
    if billing_error:
        body["billingError"] = billing_error
        return body, billing_error_status
    return body, 200


def apply_postgres_payment_callback_effects(
    result: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    order = result["order"]
    status = str(result.get("status") or "")
    growth_queued = status in {
        payment_service.STATUS_PAID,
        "partially_refunded",
        payment_service.STATUS_REFUNDED,
    }
    growth_result = {
        "status": "queued" if growth_queued else "not_required",
        "durable": growth_queued,
        "store": "postgresql_outbox" if growth_queued else "",
    }
    return {
        "ok": True,
        "callback": result,
        "billing": {
            "idempotent": bool(result.get("idempotent")),
            "pointsCredited": int(result.get("pointsToCredit") or 0),
            "pointsDebited": int(result.get("pointsToRefund") or 0),
            "store": "postgresql",
        },
        "growth": growth_result,
        "account": account_payload(str(order["user_id"])),
    }, 200


@app.post("/api/payments/fake-callback")
def api_fake_payment_callback():
    if postgres_product_runtime_enabled():
        return jsonify(
            {
                "error": "正式环境不提供模拟支付回调",
                "code": "fake_payment_callback_disabled_live",
            }
        ), 410
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

    body, status = apply_payment_callback_effects(
        result,
        provider=provider,
        provider_order_id=provider_order_id,
        payload=payload,
    )
    if status != 200:
        return jsonify(body), status
    return jsonify(body)


@app.post("/api/payments/alipay/notify")
def api_alipay_payment_notify():
    body = dict(request.form.items())
    if not body:
        json_body = request.get_json(silent=True)
        body = dict(json_body) if isinstance(json_body, dict) else {}
    try:
        provider_order_id = payment_service.alipay_provider_order_id(body)
        event_type = payment_service.alipay_callback_event_type(body)
        if postgres_payment_runtime_enabled("alipay"):
            event = payment_service.normalize_verified_payment_event(
                provider="alipay",
                provider_order_id=provider_order_id,
                event_type=event_type,
                payload=body,
                secret=payment_service.alipay_public_key(os.environ),
            )
            result = apply_postgres_payment_event(event)
        else:
            conn = product_db_conn()
            try:
                result = payment_service.handle_payment_callback(
                    conn,
                    provider="alipay",
                    provider_order_id=provider_order_id,
                    event_type=event_type,
                    payload=body,
                    secret=payment_service.alipay_public_key(os.environ),
                )
            finally:
                conn.close()
    except payment_service.PaymentServiceError as exc:
        status = 403 if isinstance(exc, payment_service.PaymentSignatureError) else int(getattr(exc, "status_code", 400) or 400)
        return Response("failure", status=status, mimetype="text/plain")
    except (
        product_payment_store.ProductPaymentStoreError,
        PostgresRuntimeError,
    ):
        app.logger.exception(
            "PostgreSQL payment callback failed for %s",
            provider_order_id,
        )
        return Response("failure", status=503, mimetype="text/plain")

    if postgres_payment_runtime_enabled("alipay"):
        effect_body, effect_status = apply_postgres_payment_callback_effects(
            result,
            payload=body,
        )
    else:
        effect_body, effect_status = apply_payment_callback_effects(
            result,
            provider="alipay",
            provider_order_id=provider_order_id,
            payload=body,
        )
    if effect_status != 200 or not effect_body.get("ok"):
        return Response("failure", status=effect_status, mimetype="text/plain")
    return Response("success", mimetype="text/plain")


@app.post("/api/admin/actions/payments/reconcile")
def api_admin_reconcile_payment():
    if not admin_finance_action_authorized():
        if not admin_write_authorized():
            return forbidden("管理写接口未授权", "admin_write_forbidden")
        return forbidden("支付对账权限不足", "admin_permission_forbidden")

    live_runtime = postgres_product_runtime_enabled()
    idempotency_key = ""
    if live_runtime:
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error

    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "payment reconciliation reason is required", "code": "invalid_payment_reconciliation"}), 400

    provider = str(body.get("provider") or "")
    if live_runtime and provider.strip().lower() == "fake":
        return jsonify(
            {
                "error": "正式支付对账不能使用模拟支付渠道",
                "code": "durable_payment_provider_required",
            }
        ), 409
    provider_order_id = str(
        body.get("providerOrderId") or body.get("provider_order_id") or body.get("orderId") or body.get("order_id") or ""
    )
    target_status = str(body.get("status") or body.get("targetStatus") or body.get("target_status") or "").strip()
    event_id = str(body.get("eventId") or body.get("event_id") or "").strip() or None
    event_type = str(body.get("eventType") or body.get("event_type") or "").strip() or None
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    payload = {
        **payload,
        "manual": True,
        "manualReason": reason,
        "metadata": metadata,
    }
    if event_id:
        payload["reportedEventId"] = event_id
    actor_user_id = admin_actor_user_id()
    audit_record: dict[str, Any] = {}

    try:
        if postgres_payment_runtime_enabled(provider):
            event = payment_service.normalize_manual_payment_event(
                provider=provider,
                provider_order_id=provider_order_id,
                target_status=target_status,
                payload=payload,
                event_id=None if live_runtime else event_id,
                event_type=event_type,
            )
            transaction_effect = None
            if live_runtime:
                event["provider_event_id"] = (
                    product_growth_store.stable_growth_id(
                        "manualpayevent",
                        product_growth_tenant_id(),
                        str(event["provider"]),
                        str(event["provider_order_id"]),
                        str(event["target_status"]),
                        idempotency_key,
                    )
                )
                action_id = product_growth_store.stable_growth_id(
                    "financeaction",
                    product_growth_tenant_id(),
                    "payment-reconcile",
                    str(event["provider"]),
                    str(event["provider_order_id"]),
                    idempotency_key,
                )

                def record_reconciliation_audit(
                    cursor: Any,
                    applied: Any,
                ) -> None:
                    event_row = applied.event
                    points_delta = int(
                        event_row.get("points_delta") or 0
                    )
                    if (
                        str(event_row["event_kind"])
                        == product_payment_store.EVENT_REFUND_SUCCEEDED
                    ):
                        previous_refunded = int(
                            event_row.get(
                                "refunded_amount_cents_after"
                            )
                            or 0
                        ) - int(event_row.get("amount_cents") or 0)
                        previous_status = (
                            payment_service.STATUS_PAID
                            if previous_refunded <= 0
                            else "partially_refunded"
                        )
                    else:
                        previous_status = payment_service.STATUS_PENDING
                    audit = product_finance_store.record_finance_audit(
                        cursor,
                        action_id=action_id,
                        actor_user_id=actor_user_id,
                        action_domain="payment",
                        action="payment_reconciled",
                        target_type="payment_order",
                        target_id=str(applied.order["id"]),
                        status="succeeded",
                        reason=reason,
                        metadata={
                            "provider": str(event_row["provider"]),
                            "providerOrderId": str(
                                event_row["provider_order_id"]
                            ),
                            "eventId": str(
                                event_row["provider_event_id"]
                            ),
                            "eventType": str(event_row["event_type"]),
                            "fromStatus": previous_status,
                            "toStatus": str(
                                event_row["target_status"]
                            ),
                            "pointsToCredit": max(points_delta, 0),
                            "pointsToRefund": max(-points_delta, 0),
                            "billingOk": True,
                            "metadata": metadata,
                        },
                    )
                    audit_record.update(audit.record)

                transaction_effect = record_reconciliation_audit
            result = apply_postgres_payment_event(
                event,
                transaction_effect=transaction_effect,
            )
        else:
            conn = product_db_conn()
            try:
                result = payment_service.reconcile_payment_event(
                    conn,
                    provider=provider,
                    provider_order_id=provider_order_id,
                    target_status=target_status,
                    payload=payload,
                    event_id=event_id,
                    event_type=event_type,
                )
            finally:
                conn.close()
    except payment_service.PaymentServiceError as exc:
        return payment_error_response(exc)
    except (
        product_payment_store.ProductPaymentStoreError,
        PostgresRuntimeError,
    ) as exc:
        return product_payment_error_response(exc)
    except product_finance_store.ProductFinanceStoreError as exc:
        return product_finance_error_response(exc)

    normalized_provider = str(result["order"]["provider"])
    normalized_provider_order_id = str(result["provider_order_id"])
    if postgres_payment_runtime_enabled(normalized_provider):
        response_body, status = apply_postgres_payment_callback_effects(
            result,
            payload=payload,
        )
    else:
        response_body, status = apply_payment_callback_effects(
            result,
            provider=normalized_provider,
            provider_order_id=normalized_provider_order_id,
            payload=payload,
        )

    if live_runtime:
        audit = product_api_record(audit_record)
    else:
        conn = product_db_conn()
        try:
            audit = admin_actions.admin_audit_event(
                conn,
                actor_user_id=actor_user_id,
                action="payment_reconciled",
                target_type="payment_order",
                target_id=str(result["order_id"]),
                status="succeeded" if status == 200 and response_body.get("ok") else "failed",
                reason=reason,
                metadata={
                    "provider": normalized_provider,
                    "providerOrderId": normalized_provider_order_id,
                    "eventId": result.get("eventId"),
                    "eventType": result.get("eventType"),
                    "fromStatus": result.get("previousStatus"),
                    "toStatus": result.get("status"),
                    "pointsToCredit": result.get("pointsToCredit"),
                    "pointsToRefund": result.get("pointsToRefund"),
                    "billingOk": response_body.get("ok"),
                    "metadata": metadata,
                },
            )
        finally:
            conn.close()

    response_body["audit"] = audit
    if status != 200:
        return jsonify(response_body), status
    return jsonify(response_body)


@app.post("/api/objects/sign")
def api_sign_object_access():
    payload = request.get_json(silent=True) or {}
    internal_signer = configured_request_token(
        ("OBJECT_API_TOKEN", "ADMIN_API_TOKEN"),
        "X-Object-Token",
    )
    user_id = (
        str(payload.get("userId") or current_authenticated_user_id())
        if internal_signer
        else current_authenticated_user_id()
    )
    if not object_write_authorized(user_id):
        return forbidden("对象访问签名未授权", "object_write_forbidden")

    secret = object_access_signing_secret()
    if not secret:
        return jsonify({"error": "对象签名密钥未配置", "code": "object_signing_secret_missing"}), 503

    try:
        purpose = str(payload.get("purpose") or asset_security.PREVIEW)
        variant = str(payload.get("variant") or asset_security.PREVIEW)
        expires_in = float(
            payload.get("expiresIn")
            or payload.get("expires_in")
            or (
                asset_security.PREVIEW_TOKEN_TTL_SECONDS
                if purpose == asset_security.PREVIEW
                else asset_security.DOWNLOAD_TOKEN_TTL_SECONDS
            )
        )
        max_ttl = (
            asset_security.PREVIEW_TOKEN_TTL_SECONDS
            if purpose == asset_security.PREVIEW
            else asset_security.DOWNLOAD_TOKEN_TTL_SECONDS
        )
        if expires_in <= 0 or expires_in > max_ttl:
            raise ValueError("signed object access TTL is out of range")
        access = object_storage_service.create_signed_access(
            object_key=str(payload.get("objectKey") or payload.get("object_key") or ""),
            user_id=user_id,
            purpose=purpose,
            variant=variant,
            expires_in=expires_in,
            secret=secret,
            base_url="/objects",
        )
    except (TypeError, ValueError, asset_security.AssetTokenError):
        return jsonify({"error": "对象访问签名请求无效", "code": "invalid_object_access_request"}), 400
    return jsonify({"ok": True, **public_object_access_payload(access)})


@app.post("/api/admin/actions/risk")
def api_admin_record_risk_decision():
    payload = request.get_json(silent=True) or {}
    decision = str(payload.get("decision") or "allow").strip().lower()
    if decision not in admin_actions.RISK_DECISIONS:
        if not (admin_write_authorized() or admin_risk_decision_authorized("review")):
            return forbidden("管理写接口未授权", "admin_write_forbidden")
        return jsonify({"error": f"invalid risk decision: {decision}", "code": "invalid_admin_action"}), 400
    if not admin_risk_decision_authorized(decision):
        if not (admin_write_authorized() or admin_risk_decision_authorized("review")):
            return forbidden("管理写接口未授权", "admin_write_forbidden")
        return forbidden("风控处置权限不足", "admin_permission_forbidden")

    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        explicit_subject_type = str(
            payload.get("subjectType")
            or payload.get("subject_type")
            or ""
        ).strip()
        explicit_subject_value = str(
            payload.get("subjectValue")
            or payload.get("subject_value")
            or ""
        ).strip()
        if bool(explicit_subject_type) != bool(explicit_subject_value):
            return jsonify(
                {
                    "error": (
                        "subjectType and subjectValue must be provided "
                        "together"
                    ),
                    "code": "invalid_admin_action",
                }
            ), 400
        if explicit_subject_type:
            subjects = [
                (explicit_subject_type, explicit_subject_value)
            ]
        else:
            subjects = [
                ("user", str(payload.get("userId") or payload.get("user_id") or "").strip()),
                ("agent", str(payload.get("agentId") or payload.get("agent_id") or "").strip()),
                ("asset", str(payload.get("assetId") or payload.get("asset_id") or "").strip()),
                ("ip", str(payload.get("ip") or "").strip()),
                ("phone", str(payload.get("phone") or "").strip()),
                ("device", str(payload.get("deviceId") or payload.get("device_id") or "").strip()),
            ]
            subjects = [
                (subject_type, subject_value)
                for subject_type, subject_value in subjects
                if subject_value
            ]
        if len(subjects) != 1:
            return jsonify(
                {
                    "error": "exactly one risk subject is required",
                    "code": "invalid_admin_action",
                }
            ), 400
        subject_type, subject_value = subjects[0]
        action_id = product_growth_store.stable_growth_id(
            "risk_action",
            product_growth_tenant_id(),
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = (
                    product_admin_security_store
                    .ProductAdminSecurityStore(connection)
                    .record_risk_decision(
                        action_id=action_id,
                        actor_user_id=admin_actor_user_id(),
                        event_type=str(
                            payload.get("eventType")
                            or payload.get("event_type")
                            or ""
                        ),
                        subject_type=subject_type,
                        subject_value=subject_value,
                        decision=decision,
                        risk_level=str(
                            payload.get("riskLevel")
                            or payload.get("risk_level")
                            or "info"
                        ),
                        deny_reason=str(
                            payload.get("denyReason")
                            or payload.get("deny_reason")
                            or ""
                        ),
                        metadata=(
                            payload.get("metadata")
                            if isinstance(payload.get("metadata"), dict)
                            else {}
                        ),
                    )
                )
        except product_admin_security_store.InvalidProductAdminSecurityInput as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "invalid_admin_action",
                }
            ), 400
        except product_admin_security_store.ProductAdminSecurityConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "admin_action_conflict",
                }
            ), 409
        except (
            product_admin_security_store.ProductAdminSecurityStoreError,
            PostgresRuntimeError,
        ) as exc:
            app.logger.exception("PostgreSQL risk decision unavailable")
            return jsonify(
                {
                    "error": "风控记录暂时不可用",
                    "code": "postgres_admin_security_unavailable",
                    "reason": type(exc).__name__,
                }
            ), 503
        return jsonify(
            {
                "ok": True,
                "idempotent": not result.created,
                "record": product_admin_risk_view(result.record),
            }
        )

    conn = product_db_conn()
    try:
        record = admin_actions.record_risk_decision(
            conn,
            event_type=str(payload.get("eventType") or payload.get("event_type") or ""),
            decision=decision,
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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        action_id = product_growth_store.stable_growth_id(
            "admin_asset_access",
            product_growth_tenant_id(),
            idempotency_key,
        )
        request_id = product_growth_store.stable_growth_id(
            "admin_request",
            product_growth_tenant_id(),
            idempotency_key,
        )
        metadata = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        client_request_id = str(
            payload.get("requestId")
            or payload.get("request_id")
            or ""
        ).strip()
        if client_request_id:
            metadata["clientRequestId"] = client_request_id
        try:
            with postgres_connection() as connection:
                result = (
                    product_admin_security_store
                    .ProductAdminSecurityStore(connection)
                    .record_asset_access(
                        action_id=action_id,
                        actor_user_id=admin_actor_user_id(),
                        request_id=request_id,
                        asset_id=str(
                            payload.get("assetId")
                            or payload.get("asset_id")
                            or ""
                        ),
                        action=str(payload.get("action") or ""),
                        user_id=str(
                            payload.get("userId")
                            or payload.get("user_id")
                            or ""
                        ),
                        agent_id=str(
                            payload.get("agentId")
                            or payload.get("agent_id")
                            or ""
                        ),
                        asset_type=str(
                            payload.get("assetType")
                            or payload.get("asset_type")
                            or ""
                        ),
                        ip=str(payload.get("ip") or request_ip()),
                        allowed=payload_bool(
                            payload,
                            "allowed",
                            default=True,
                        ),
                        deny_reason=str(
                            payload.get("denyReason")
                            or payload.get("deny_reason")
                            or ""
                        ),
                        user_agent=str(
                            payload.get("userAgent")
                            or payload.get("user_agent")
                            or request.headers.get("User-Agent", "")
                        ),
                        metadata=metadata,
                    )
                )
        except product_admin_security_store.InvalidProductAdminSecurityInput as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "invalid_admin_action",
                }
            ), 400
        except product_admin_security_store.ProductAdminSecurityConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "admin_action_conflict",
                }
            ), 409
        except (
            product_admin_security_store.ProductAdminSecurityStoreError,
            PostgresRuntimeError,
        ) as exc:
            app.logger.exception("PostgreSQL asset access unavailable")
            return jsonify(
                {
                    "error": "资产访问审计暂时不可用",
                    "code": "postgres_admin_security_unavailable",
                    "reason": type(exc).__name__,
                }
            ), 503
        return jsonify(
            {
                "ok": True,
                "idempotent": not result.created,
                "record": product_admin_asset_access_view(
                    result.record
                ),
            }
        )

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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        action_id = product_growth_store.stable_growth_id(
            "finance_action",
            product_growth_tenant_id(),
            "commission-status",
            commission_order_id,
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = product_finance_store.ProductFinanceStore(
                    connection
                ).transition_commission_order(
                    commission_order_id=commission_order_id,
                    target_status=str(payload.get("status") or ""),
                    action_id=action_id,
                    actor_user_id=admin_actor_user_id(),
                    reason=str(payload.get("reason") or ""),
                    metadata=(
                        payload.get("metadata")
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                )
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "idempotent": result.idempotent,
                "commission": product_api_record(result.record),
            }
        )

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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        min_age_days = (
            payload["minAgeDays"]
            if "minAgeDays" in payload
            else payload.get("min_age_days", 7)
        )
        action_id = product_growth_store.stable_growth_id(
            "finance_action",
            product_growth_tenant_id(),
            "release-eligible",
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = product_finance_store.ProductFinanceStore(
                    connection
                ).release_eligible_commissions(
                    action_id=action_id,
                    actor_user_id=admin_actor_user_id(),
                    agent_id=str(
                        payload.get("agentId")
                        or payload.get("agent_id")
                        or ""
                    ),
                    min_age_days=int(min_age_days),
                    limit=int(payload.get("limit") or 500),
                    reason=str(payload.get("reason") or ""),
                    metadata=(
                        payload.get("metadata")
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                )
        except (TypeError, ValueError) as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "invalid_finance_input",
                }
            ), 400
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "idempotent": result.idempotent,
                **product_api_record(result.record),
            }
        )

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
    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                settlements = product_finance_store.ProductFinanceStore(
                    connection
                ).list_settlements(
                    agent_id=str(
                        request.args.get("agentId")
                        or request.args.get("agent_id")
                        or ""
                    ),
                    status=str(request.args.get("status") or ""),
                    limit=request.args.get("limit") or 50,
                )
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "settlements": product_api_record(settlements),
            }
        )

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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        agent_id = str(
            payload.get("agentId") or payload.get("agent_id") or ""
        )
        if not agent_id or not order_ids:
            return jsonify(
                {
                    "error": (
                        "agentId and commissionOrderIds are required"
                    ),
                    "code": "invalid_commission_settlement_input",
                }
            ), 400
        settlement_id = product_growth_store.stable_growth_id(
            "settlement",
            product_growth_tenant_id(),
            agent_id,
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = product_finance_store.ProductFinanceStore(
                    connection
                ).create_commission_settlement(
                    settlement_id=settlement_id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_key,
                    commission_order_ids=(
                        [str(item) for item in order_ids]
                        if isinstance(order_ids, list)
                        else []
                    ),
                    settlement_account=(
                        payload.get("settlementAccount")
                        if isinstance(
                            payload.get("settlementAccount"),
                            dict,
                        )
                        else {}
                    ),
                    settlement_no=str(
                        payload.get("settlementNo")
                        or payload.get("settlement_no")
                        or ""
                    ),
                    metadata=(
                        payload.get("metadata")
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                )
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return (
            jsonify(
                {
                    "ok": True,
                    "idempotent": not result.created,
                    "settlement": product_api_record(result.record),
                }
            ),
            201 if result.created else 200,
        )

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
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "").strip().lower()
    if status not in commission_settlement_service.SETTLEMENT_STATUSES:
        if not admin_write_authorized():
            return forbidden("管理写接口未授权", "admin_write_forbidden")
        return jsonify({"error": "invalid commission settlement status", "code": "invalid_commission_settlement_input"}), 400
    if not admin_commission_settlement_status_authorized(status):
        return forbidden("佣金结算操作权限不足", "admin_permission_forbidden")
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        action_id = product_growth_store.stable_growth_id(
            "finance_action",
            product_growth_tenant_id(),
            "settlement-status",
            settlement_id,
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = product_finance_store.ProductFinanceStore(
                    connection
                ).transition_commission_settlement(
                    settlement_id=settlement_id,
                    target_status=status,
                    action_id=action_id,
                    actor_user_id=admin_actor_user_id(),
                    reason=str(
                        payload.get("failureReason")
                        or payload.get("failure_reason")
                        or payload.get("reason")
                        or ""
                    ),
                    metadata=(
                        payload.get("metadata")
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                )
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "idempotent": result.idempotent,
                "settlement": product_api_record(result.record),
            }
        )

    conn = product_db_conn()
    try:
        settlement = commission_settlement_service.update_commission_settlement_status(
            conn,
            settlement_id,
            status,
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
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"approved", "rejected", "paid", "canceled"}:
        if not admin_write_authorized():
            return forbidden("管理写接口未授权", "admin_write_forbidden")
        return jsonify({"error": "invalid withdrawal status", "code": "invalid_withdrawal_input"}), 400
    if not admin_withdrawal_status_authorized(status):
        return forbidden("提现操作权限不足", "admin_permission_forbidden")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    actor_user_id = admin_actor_user_id()
    reason = str(payload.get("reason") or "")
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        action_id = product_growth_store.stable_growth_id(
            "finance_action",
            product_growth_tenant_id(),
            "withdrawal-status",
            withdrawal_id,
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = product_finance_store.ProductFinanceStore(
                    connection
                ).review_withdrawal(
                    withdrawal_id=withdrawal_id,
                    target_status=status,
                    action_id=action_id,
                    actor_user_id=actor_user_id,
                    reason=reason,
                    metadata=metadata,
                )
        except (
            product_finance_store.ProductFinanceStoreError,
            PostgresRuntimeError,
        ) as exc:
            return product_finance_error_response(exc)
        return jsonify(
            {
                "ok": True,
                "idempotent": result.idempotent,
                "withdrawal": product_api_record(result.record),
            }
        )

    conn = product_db_conn()
    try:
        record = withdrawal_service.update_withdrawal_request_status(
            conn,
            withdrawal_id,
            status,
            reason=reason,
            metadata={
                **metadata,
                "actorUserId": actor_user_id,
                "source": "admin_api",
            },
        )
        status_history = record.get("metadata", {}).get("statusHistory")
        last_transition = status_history[-1] if isinstance(status_history, list) and status_history else {}
        admin_actions.admin_audit_event(
            conn,
            actor_user_id=actor_user_id,
            action="withdrawal_status_updated",
            target_type="agent_withdrawal_request",
            target_id=str(record.get("id") or withdrawal_id),
            reason=reason,
            metadata={
                "withdrawalId": record.get("id"),
                "agentId": record.get("agentId"),
                "amountCents": record.get("amountCents"),
                "fromStatus": last_transition.get("from") if isinstance(last_transition, dict) else None,
                "toStatus": record.get("status"),
                "statusReason": record.get("statusReason"),
            },
        )
    except withdrawal_service.WithdrawalServiceError as exc:
        return withdrawal_error_response(exc)
    finally:
        conn.close()
    return jsonify({"ok": True, "withdrawal": record})


def product_asset_admin_view(
    record: dict[str, Any],
    *,
    viewer_user_id: str = "",
) -> dict[str, Any]:
    status = str(record.get("status") or "")
    payload = {
        "assetId": str(record.get("id") or record.get("assetId") or ""),
        "tenantId": str(record.get("tenant_id") or ""),
        "ownerUserId": str(record.get("owner_user_id") or ""),
        "kind": str(record.get("asset_kind") or record.get("kind") or ""),
        "taxonomyVersion": str(record.get("taxonomy_version") or ""),
        "categoryId": str(record.get("category_id") or ""),
        "category": str(
            record.get("category_name") or record.get("category") or ""
        ),
        "styleId": str(record.get("style_id") or record.get("styleId") or ""),
        "backgroundAssetId": str(
            record.get("background_asset_id") or ""
        ),
        "backgroundSha256": str(
            record.get("background_sha256") or ""
        ),
        "productName": str(
            record.get("standard_name")
            or record.get("productName")
            or ""
        ),
        "aliases": list(record.get("aliases") or []),
        "keywords": list(
            record.get("match_keywords")
            or record.get("keywords")
            or []
        ),
        "status": (
            "pending"
            if status == "pending_review"
            else status or "pending"
        ),
        "reviewStatus": str(record.get("review_status") or ""),
        "qualityNote": str(
            record.get("review_note")
            or record.get("qualityNote")
            or ""
        ),
        "reviewerUserId": str(record.get("reviewer_user_id") or ""),
        "reviewedAt": record.get("reviewed_at"),
        "disabledByUserId": str(
            record.get("disabled_by_user_id") or ""
        ),
        "disableNote": str(record.get("disable_note") or ""),
        "disabledAt": record.get("disabled_at"),
        "provider": str(
            record.get("source_provider") or record.get("provider") or ""
        ),
        "modelName": str(record.get("model_name") or ""),
        "modelVersion": str(record.get("model_version") or ""),
        "promptVersion": str(record.get("prompt_version") or ""),
        "pipelineVersion": str(record.get("pipeline_version") or ""),
        "sha256": str(
            record.get("original_sha256") or record.get("sha256") or ""
        ),
        "fileSize": int(
            record.get("original_size_bytes")
            or record.get("fileSize")
            or 0
        ),
        "createdAt": record.get("created_at") or record.get("createdAt"),
        "updatedAt": record.get("updated_at") or record.get("updatedAt"),
    }
    object_key = str(record.get("original_object_ref") or "")
    signing_secret = object_access_signing_secret()
    if object_key and viewer_user_id and signing_secret:
        try:
            access = object_storage_service.create_signed_access(
                object_key,
                viewer_user_id,
                asset_security.ADMIN_REVIEW,
                asset_security.ADMIN_REVIEW,
                signing_secret,
                expires_in=asset_security.PREVIEW_TOKEN_TTL_SECONDS,
            )
            payload["reviewUrl"] = access["url"]
            payload["reviewUrlExpiresAt"] = access["expires_at"]
        except (TypeError, ValueError):
            pass
    return payload


def product_asset_admin_summary(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": 0,
        "approved": 0,
        "rejected": 0,
        "disabled": 0,
        "pending": 0,
        "byKind": {},
        "byCategory": {},
    }
    for row in rows:
        count = int(row.get("asset_count") or 0)
        status = str(row.get("status") or "")
        kind = str(row.get("asset_kind") or "unknown")
        category = str(row.get("category_name") or "未分类")
        summary["total"] += count
        status_key = "pending" if status == "pending_review" else status
        if status_key in {
            "approved",
            "rejected",
            "disabled",
            "pending",
        }:
            summary[status_key] += count
        summary["byKind"][kind] = (
            int(summary["byKind"].get(kind) or 0) + count
        )
        summary["byCategory"][category] = (
            int(summary["byCategory"].get(category) or 0) + count
        )
    summary["byKind"] = dict(sorted(summary["byKind"].items()))
    summary["byCategory"] = dict(
        sorted(summary["byCategory"].items())
    )
    return summary


@app.get("/api/admin/ai-assets")
def api_admin_ai_assets():
    authorization = admin_panel_request_authorizer(
        request,
        ADMIN_AI_ASSETS_READ_SCOPE,
    )
    if not authorization.authenticated:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Admin authentication required.",
                    "code": "admin_authentication_required",
                }
            ),
            401,
        )
    if not authorization.allowed:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Admin permission required.",
                    "code": "admin_permission_required",
                }
            ),
            403,
        )
    try:
        limit = max(
            1,
            min(int(request.args.get("limit", "50")), 100),
        )
        offset = max(0, int(request.args.get("offset", "0")))
    except (TypeError, ValueError):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "invalid pagination",
                    "code": "invalid_admin_asset_query",
                }
            ),
            400,
        )
    requested_status = str(
        request.args.get("status") or ""
    ).strip().lower()
    if requested_status == "pending":
        requested_status = "pending_review"

    if postgres_product_runtime_enabled():
        try:
            with postgres_connection() as connection:
                store = (
                    product_asset_library_store.ProductAssetLibraryStore(
                        connection
                    )
                )
                records = store.list_assets_for_admin(
                    status=requested_status,
                    limit=limit,
                    offset=offset,
                )
                summary_rows = store.summarize_assets_for_admin()
        except product_asset_library_store.InvalidProductAssetInput as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": "invalid_admin_asset_query",
                    }
                ),
                400,
            )
        except Exception as exc:
            app.logger.exception("PostgreSQL AI asset list unavailable")
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "AI 资产库暂时不可用",
                        "code": "postgres_asset_library_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        viewer_user_id = admin_actor_user_id()
        return jsonify(
            {
                "ok": True,
                "summary": product_asset_admin_summary(summary_rows),
                "assets": [
                    product_asset_admin_view(
                        record,
                        viewer_user_id=viewer_user_id,
                    )
                    for record in records
                ],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "returned": len(records),
                },
            }
        )

    legacy_records = load_ai_asset_records()
    if requested_status:
        requested_legacy_status = (
            "pending"
            if requested_status == "pending_review"
            else requested_status
        )
        legacy_records = [
            record
            for record in legacy_records
            if str(record.get("status") or "approved")
            == requested_legacy_status
        ]
    page = legacy_records[offset : offset + limit]
    summary_rows = [
        {
            "status": (
                "pending_review"
                if str(record.get("status") or "") == "pending"
                else str(record.get("status") or "approved")
            ),
            "asset_kind": str(record.get("kind") or "unknown"),
            "category_name": str(record.get("category") or "未分类"),
            "asset_count": 1,
        }
        for record in load_ai_asset_records()
    ]
    return jsonify(
        {
            "ok": True,
            "summary": product_asset_admin_summary(summary_rows),
            "assets": [
                product_asset_admin_view(record)
                for record in page
            ],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(page),
            },
        }
    )


@app.post("/api/admin/actions/ai-assets/<asset_id>/status")
def api_admin_mark_ai_asset_status(asset_id: str):
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "").strip().lower()
    if status in {"approve", "reject", "disable"}:
        status = {"approve": "approved", "reject": "rejected", "disable": "disabled"}[status]
    if not admin_ai_asset_status_authorized("pending"):
        return forbidden("AI 资产审核权限不足", "admin_permission_forbidden")
    if status not in {"approved", "rejected", "disabled", "pending"}:
        return jsonify({"error": "invalid AI asset status", "code": "invalid_admin_action"}), 400
    if not admin_ai_asset_status_authorized(status):
        return forbidden("AI 资产审核权限不足", "admin_permission_forbidden")
    if postgres_product_runtime_enabled():
        if status == "pending":
            return (
                jsonify(
                    {
                        "error": "生产资产审核不可回退为 pending",
                        "code": "invalid_admin_action",
                    }
                ),
                409,
            )
        actor_user_id = admin_actor_user_id()
        note = str(
            payload.get("qualityNote")
            or payload.get("quality_note")
            or payload.get("reason")
            or ""
        ).strip()
        if status == "disabled" and not note:
            return (
                jsonify(
                    {
                        "error": "禁用资产必须填写原因",
                        "code": "invalid_admin_action",
                    }
                ),
                400,
            )
        try:
            with postgres_connection() as connection:
                store = (
                    product_asset_library_store.ProductAssetLibraryStore(
                        connection
                    )
                )
                existing = store.get_asset(asset_id=asset_id)
                tenant_id = str(existing["tenant_id"])
                if status == "disabled":
                    result = store.disable_asset(
                        asset_id=asset_id,
                        tenant_id=tenant_id,
                        disabled_by_user_id=actor_user_id,
                        disable_note=note,
                    )
                else:
                    result = store.review_asset(
                        asset_id=asset_id,
                        tenant_id=tenant_id,
                        reviewer_user_id=actor_user_id,
                        decision=status,
                        review_note=note,
                    )
        except product_asset_library_store.ProductAssetNotFound:
            return (
                jsonify(
                    {
                        "error": "AI asset not found",
                        "code": "ai_asset_not_found",
                    }
                ),
                404,
            )
        except (
            product_asset_library_store.InvalidProductAssetInput,
            product_asset_library_store.ProductAssetReviewConflict,
        ) as exc:
            return (
                jsonify(
                    {
                        "error": str(exc),
                        "code": "invalid_admin_action",
                    }
                ),
                409,
            )
        except Exception as exc:
            app.logger.exception("PostgreSQL AI asset review unavailable")
            return (
                jsonify(
                    {
                        "error": "AI 资产审核暂时不可用",
                        "code": "postgres_asset_library_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        return jsonify(
            {
                "ok": True,
                "asset": product_asset_admin_view(result.record),
                "idempotent": result.idempotent,
            }
        )
    repo = ai_asset_repository.AIAssetRepository(ai_asset_manifest_path())
    try:
        record = admin_actions.mark_ai_asset_status(repo, asset_id, status)
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
    if postgres_product_runtime_enabled():
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        action_id = product_growth_store.stable_growth_id(
            "admin_audit",
            product_growth_tenant_id(),
            idempotency_key,
        )
        try:
            with postgres_connection() as connection:
                result = (
                    product_admin_security_store
                    .ProductAdminSecurityStore(connection)
                    .record_admin_audit(
                        action_id=action_id,
                        actor_user_id=admin_actor_user_id(),
                        action=str(payload.get("action") or ""),
                        target_type=str(
                            payload.get("targetType")
                            or payload.get("target_type")
                            or ""
                        ),
                        target_id=str(
                            payload.get("targetId")
                            or payload.get("target_id")
                            or ""
                        ),
                        status=str(
                            payload.get("status") or "succeeded"
                        ),
                        reason=str(payload.get("reason") or ""),
                        metadata=(
                            payload.get("metadata")
                            if isinstance(payload.get("metadata"), dict)
                            else {}
                        ),
                    )
                )
        except product_admin_security_store.InvalidProductAdminSecurityInput as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "invalid_admin_action",
                }
            ), 400
        except product_admin_security_store.ProductAdminSecurityConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "admin_action_conflict",
                }
            ), 409
        except (
            product_admin_security_store.ProductAdminSecurityStoreError,
            PostgresRuntimeError,
        ) as exc:
            app.logger.exception("PostgreSQL admin audit unavailable")
            return jsonify(
                {
                    "error": "管理审计暂时不可用",
                    "code": "postgres_admin_security_unavailable",
                    "reason": type(exc).__name__,
                }
            ), 503
        return jsonify(
            {
                "ok": True,
                "idempotent": not result.created,
                "audit": product_api_record(result.record),
            }
        )

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


@app.post("/api/admin/actions/points-adjustments")
def api_admin_adjust_points():
    if not admin_finance_action_authorized():
        if not admin_write_authorized():
            return forbidden("管理写接口未授权", "admin_write_forbidden")
        return forbidden("积分调整权限不足", "admin_permission_forbidden")

    payload = request.get_json(silent=True) or {}
    live_runtime = postgres_product_runtime_enabled()
    idempotency_key = ""
    if live_runtime:
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
    try:
        user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
        if not user_id:
            raise billing.InvalidBillingInput("userId is required")
        direction = normalize_points_adjustment_direction(str(payload.get("direction") or payload.get("kind") or ""))
        points = int(payload.get("points") or 0)
        if points <= 0:
            raise billing.InvalidBillingInput("调整积分必须大于 0", points=points)
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise billing.InvalidBillingInput("积分调整必须填写原因")
        actor_user_id = admin_actor_user_id()
        order_id = str(payload.get("orderId") or payload.get("order_id") or "").strip()
        if not order_id:
            order_id = f"admin_points_{direction}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        billing_metadata = {
            "source": "admin_points_adjustment",
            "actorUserId": actor_user_id,
            "reason": reason,
            **metadata,
        }
        audit_record: dict[str, Any] = {}
        transaction_effect = None
        if live_runtime:
            order_id = product_growth_store.stable_growth_id(
                "pointorder",
                product_growth_tenant_id(),
                "admin-adjustment",
                direction,
                user_id,
                idempotency_key,
            )
            action_id = product_growth_store.stable_growth_id(
                "financeaction",
                product_growth_tenant_id(),
                "admin-points-adjustment",
                direction,
                user_id,
                idempotency_key,
            )

            def record_adjustment_audit(cursor: Any, result: Any) -> None:
                audit = product_finance_store.record_finance_audit(
                    cursor,
                    action_id=action_id,
                    actor_user_id=actor_user_id,
                    action_domain="points",
                    action=f"points.{direction}.adjusted",
                    target_type="billing_account",
                    target_id=user_id,
                    reason=reason,
                    metadata={
                        "direction": direction,
                        "points": points,
                        "billingOrderId": order_id,
                        "balanceAfter": int(
                            result.account["balance_points"]
                        ),
                        "metadata": metadata,
                    },
                )
                audit_record.update(audit.record)

            transaction_effect = record_adjustment_audit

        if direction == "credit":
            transaction = credit_points(
                user_id,
                order_id,
                points,
                description=str(payload.get("description") or "admin-points-credit"),
                metadata=billing_metadata,
                transaction_effect=transaction_effect,
            )
        else:
            transaction = debit_points(
                user_id,
                order_id,
                points,
                description=str(payload.get("description") or "admin-points-debit"),
                metadata=billing_metadata,
                transaction_effect=transaction_effect,
            )
    except billing.BillingError as exc:
        return billing_json_error(exc)
    except product_finance_store.ProductFinanceStoreError as exc:
        return product_finance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return billing_json_error(billing.InvalidBillingInput(str(exc)))

    if live_runtime:
        audit = product_api_record(audit_record)
    else:
        conn = product_db_conn()
        try:
            audit = admin_actions.admin_audit_event(
                conn,
                actor_user_id=actor_user_id,
                action=f"points_{direction}_adjusted",
                target_type="billing_account",
                target_id=user_id,
                reason=reason,
                metadata={
                    "direction": direction,
                    "points": points,
                    "billingOrderId": transaction["orderId"],
                    "balanceAfter": transaction["balanceAfter"],
                    "metadata": metadata,
                },
            )
        finally:
            conn.close()

    return jsonify(
        {
            "ok": True,
            "transaction": transaction,
            "account": account_payload(user_id),
            "audit": audit,
        }
    )


@app.post("/api/recharge")
def api_recharge():
    if postgres_product_runtime_enabled():
        return jsonify(
            {
                "error": "正式充值必须先创建支付订单",
                "code": "payment_order_required",
            }
        ), 409
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
            result = credit_points(
                user_id,
                order_id,
                points,
                description="custom-recharge",
                metadata={"cash": round(points / POINT_RATE, 2), "custom": True},
            )
        else:
            cash = int(payload.get("cash") or 0)
            points = billing.points_for_recharge(cash)
            result = credit_points(
                user_id,
                order_id,
                points,
                description=f"recharge:{cash}",
                metadata={
                    "cash": cash,
                    "package": cash in billing.RECHARGE_PACKAGES,
                },
            )
        return jsonify({"ok": True, "transaction": result, "account": account_payload(user_id)})
    except billing.BillingError as exc:
        return billing_json_error(exc)


@app.post("/api/debit")
def api_debit():
    if postgres_product_runtime_enabled():
        return jsonify(
            {
                "error": "正式扣费必须由生成或精修任务触发",
                "code": "task_owned_debit_required",
            }
        ), 409
    payload = request.get_json(silent=True) or {}
    server_points: int | None = None
    if billing_write_authorized():
        user_id = str(payload.get("userId") or current_user_id())
    else:
        principal, principal_error = customer_request_principal()
        if principal_error is not None:
            return principal_error
        assert principal is not None
        user_id = str(principal["userId"])
        if not principal.get("localDemo"):
            action = str(payload.get("action") or "").strip()
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            if action == "single_rework":
                server_points = billing.quality_point_value(str(metadata.get("quality") or "standard"))
            elif action == "custom_refine":
                server_points = CUSTOM_EDIT_POINTS
            else:
                return forbidden("客户端不能直接指定扣费积分", "client_points_forbidden")
    ensure_demo_balance(user_id)
    order_id = str(payload.get("orderId") or f"debit_{int(time.time() * 1000)}")
    try:
        points = server_points if server_points is not None else int(payload.get("points") or 0)
        if points <= 0:
            points = billing.calculate_image_charge(
                image_count=payload.get("imageCount", payload.get("images", 0)),
                quality=payload.get("quality", "standard"),
                watermark=bool(payload.get("watermark", False)),
                platforms=payload.get("platforms"),
                platform_count=payload.get("platformCount"),
            )
        result = debit_points(
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
    if not billing_write_authorized():
        return forbidden("退款只能由服务端任务触发", "server_refund_required")
    if postgres_product_runtime_enabled():
        return jsonify(
            {
                "error": "正式任务退款必须由 PostgreSQL 结算服务处理",
                "code": "durable_settlement_refund_required",
            }
        ), 409
    user_id = str(payload.get("userId") or current_user_id())
    source_order_id = str(payload.get("sourceOrderId") or payload.get("orderId") or "")
    try:
        requested_points = payload.get("points")
        result = billing.refund_debit(
            user_id,
            source_order_id,
            points=int(requested_points) if requested_points is not None else None,
            refund_order_id=str(payload.get("refundOrderId") or f"refund:{source_order_id}"),
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
    product_generation = product_generation_readiness()
    product_growth = product_growth_readiness()
    refinement_provider = image_refinement_readiness()
    product_auth = product_auth_readiness()
    queue = generation_queue.snapshot()
    ready = (
        bool(object_storage.get("ready"))
        and bool(payments.get("ready"))
        and bool(generation_provider.get("ready"))
        and bool(product_generation.get("ready"))
        and bool(product_growth.get("ready"))
        and bool(refinement_provider.get("ready"))
        and bool(product_auth.get("ready"))
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
            "productGeneration": product_generation,
            "productGrowth": product_growth,
            "imageRefinement": refinement_provider,
            "authentication": product_auth,
        }
    )


@app.get("/api/image-providers")
def api_image_providers():
    generation = generation_provider_readiness()
    refinement = image_refinement_readiness()
    return jsonify(
        {
            "ok": True,
            "generation": {
                "id": "tencent-hunyuan",
                "label": "混元 3.0",
                "ready": bool(generation.get("ready")),
                "mode": generation.get("mode"),
                "model": generation.get("tokenhubModel"),
            },
            "refinement": {
                "id": "google-gemini",
                "label": "Gemini",
                "ready": bool(refinement.get("ready")),
                "model": refinement.get("model"),
            },
            "routing": {
                "background": "tencent-hunyuan",
                "freeSamples": "tencent-hunyuan",
                "formalGeneration": "tencent-hunyuan",
                "singleImageRefinement": "google-gemini",
            },
        }
    )


@app.get("/api/ops/deployment-config")
def api_ops_deployment_config():
    return jsonify(deployment_config_report())


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


def generation_job_id(
    style: str,
    quality: str,
    payload: dict[str, Any],
    selected_background: SelectedBackgroundAsset | None = None,
    *,
    user_id: str | None = None,
    menu_upload_id: str = "",
    menu_sha256: str = "",
) -> str:
    resolved_user_id = str(user_id or current_user_id()).strip()
    provided = payload.get("jobId") or payload.get("job_id") or payload.get("idempotencyKey") or payload.get("idempotency_key")
    if provided:
        client_key = validate_generation_job_id(str(provided))
        digest = hashlib.sha256(
            f"{resolved_user_id}\0{client_key}".encode("utf-8")
        ).hexdigest()[:24]
        return f"generation-{digest}"

    if menu_upload_id and menu_sha256:
        menu_identity: dict[str, Any] = {
            "menuUploadId": menu_upload_id,
            "sha256": menu_sha256,
        }
    else:
        menu_path = current_menu_path()
        if menu_path is None:
            menu_identity = {"demo": True, "file": "demo_menu.xlsx"}
        else:
            try:
                menu_identity = {
                    "file": menu_path.name,
                    "sha256": file_sha256(menu_path),
                }
            except OSError:
                menu_identity = {"file": menu_path.name}

    basis = {
        "userId": resolved_user_id,
        "menu": menu_identity,
        "style": style,
        "quality": quality_config(quality)["id"],
        "backgroundAssetId": selected_background.asset_id if selected_background else "",
        "backgroundSha256": selected_background.sha256 if selected_background else "",
    }
    digest = hashlib.sha1(json.dumps(basis, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"generation-{digest}"


def revision_job_id(
    *,
    user_id: str,
    idempotency_key: str,
) -> str:
    clean_key = validate_generation_job_id(idempotency_key)
    digest = hashlib.sha256(
        f"{str(user_id).strip()}\0{clean_key}".encode("utf-8")
    ).hexdigest()[:24]
    return f"revision-{digest}"


def resolve_parent_delivery_asset_snapshot(
    parent_job_id: str,
    asset_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    record, parent_contract = persisted_generation_contract(
        parent_job_id,
        principal,
    )
    if (
        str(parent_contract.get("jobType") or "") != "menu_batch_generation"
        or str(record.get("status") or "") != "succeeded"
    ):
        raise MenuUploadError(
            "revision_source_not_ready",
            "原始正式图片尚未完成，暂不能修改",
            status=425,
        )
    result_pointer = (
        record.get("result", {}).get("manifest")
        if isinstance(record.get("result"), dict)
        and isinstance(record.get("result", {}).get("manifest"), dict)
        else {}
    )
    result_document = load_generation_result_manifest_document(
        result_pointer,
        parent_contract,
    )
    normalized_asset_id = str(asset_id or "").strip().lower()
    source_object_key, source_bytes = generation_delivery_asset(
        result_document,
        normalized_asset_id,
    )
    delivery_assets = (
        result_document.get("deliveryAssets")
        if isinstance(result_document.get("deliveryAssets"), list)
        else []
    )
    delivery_asset = next(
        (
            value
            for value in delivery_assets
            if isinstance(value, dict)
            and hmac.compare_digest(
                str(value.get("assetId") or "").lower(),
                normalized_asset_id,
            )
        ),
        None,
    )
    if delivery_asset is None:
        raise MenuUploadError(
            "generation_asset_not_found",
            "生成图片不存在",
            status=404,
        )
    row_number = int(delivery_asset.get("row") or 0)
    source_row: dict[str, Any] | None = None
    for index, value in enumerate(result_document.get("results") or [], start=1):
        if not isinstance(value, dict):
            continue
        candidate_ids = {
            str(candidate.get("deliveryAssetId") or "").lower()
            for candidate in value.get("candidates") or []
            if isinstance(candidate, dict)
        }
        candidate_row = int(value.get("row") or index)
        if (
            normalized_asset_id in candidate_ids
            and candidate_row == row_number
        ):
            source_row = value
            break
    if source_row is None or row_number <= 0:
        raise MenuUploadError(
            "generation_asset_binding_invalid",
            "生成图片与菜单行的绑定无效",
        )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if not hmac.compare_digest(
        source_sha256,
        str(delivery_asset.get("sha256") or "").lower(),
    ):
        raise MenuUploadError(
            "generation_asset_integrity_mismatch",
            "生成图片完整性校验失败",
        )

    background_snapshot = verified_selected_background_snapshot(
        parent_contract
    )
    return (
        record,
        {
            "assetId": normalized_asset_id,
            "objectKey": source_object_key,
            "sha256": source_sha256,
            "rowNumber": row_number,
            "dishName": str(source_row.get("name") or "").strip(),
        },
        background_snapshot,
    )


def verified_selected_background_snapshot(
    parent_contract: dict[str, Any],
) -> dict[str, Any]:
    background = (
        parent_contract.get("selectedBackground")
        if isinstance(parent_contract.get("selectedBackground"), dict)
        else {}
    )
    try:
        background_object_key = object_storage_service.validate_object_key(
            str(background.get("objectKey") or "")
        )
        background_bytes = object_storage_service.read_object_bytes_limited(
            object_storage_service.get_object_storage_service(),
            background_object_key,
            MAX_AI_ASSET_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "selected_background_too_large",
            "所选背景超过大小限制",
            status=413,
        ) from exc
    except Exception as exc:
        raise MenuUploadError(
            "selected_background_unavailable",
            "所选背景暂时不可用",
            status=503,
        ) from exc
    background_sha256 = hashlib.sha256(background_bytes).hexdigest()
    if not hmac.compare_digest(
        background_sha256,
        str(background.get("sha256") or "").lower(),
    ):
        raise MenuUploadError(
            "selected_background_integrity_mismatch",
            "所选背景完整性校验失败",
        )
    try:
        image_bytes_fingerprint(background_bytes)
    except ValueError as exc:
        raise MenuUploadError(
            "selected_background_invalid",
            "所选背景格式或尺寸无效",
        ) from exc
    return {
        "assetId": str(background.get("assetId") or ""),
        "objectKey": background_object_key,
        "sha256": background_sha256,
    }


def resolve_revision_delivery_asset_snapshot(
    source_revision_job_id: str,
    source_asset_id: str,
    parent_generation_job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    revision_record, revision_contract = persisted_revision_record(
        source_revision_job_id,
        principal,
    )
    if (
        str(revision_record.get("status") or "") != "succeeded"
        or not hmac.compare_digest(
            str(
                revision_contract.get("parentGenerationJobId")
                or ""
            ),
            parent_generation_job_id,
        )
    ):
        raise MenuUploadError(
            "revision_source_not_ready",
            "上一版修改图片尚未完成，暂不能继续修改",
            status=425,
        )
    result = (
        revision_record.get("result")
        if isinstance(revision_record.get("result"), dict)
        else {}
    )
    manifest = (
        result.get("manifest")
        if isinstance(result.get("manifest"), dict)
        else {}
    )
    document = load_revision_result_manifest_document(
        manifest,
        revision_contract,
    )
    image_asset = document["imageAsset"]
    normalized_asset_id = str(source_asset_id or "").strip()
    if not hmac.compare_digest(
        str(image_asset.get("assetId") or ""),
        normalized_asset_id,
    ):
        raise MenuUploadError(
            "revision_source_not_found",
            "上一版修改图片不存在",
            status=404,
        )
    parent_record, parent_contract = persisted_generation_contract(
        parent_generation_job_id,
        principal,
    )
    if str(parent_record.get("status") or "") != "succeeded":
        raise MenuUploadError(
            "revision_source_not_ready",
            "原始正式图片尚未完成，暂不能继续修改",
            status=425,
        )
    return (
        parent_record,
        {
            "assetId": normalized_asset_id,
            "objectKey": str(image_asset["objectKey"]),
            "sha256": str(image_asset["sha256"]),
            "rowNumber": int(
                revision_contract["sourceDeliveryAsset"]["rowNumber"]
            ),
            "dishName": str(
                revision_contract["sourceDeliveryAsset"]["dishName"]
            ),
        },
        verified_selected_background_snapshot(parent_contract),
    )


def revision_free_rework_usage(
    *,
    parent_generation_job_id: str,
    user_id: str,
) -> int:
    if postgres_product_runtime_enabled():
        with postgres_connection() as connection:
            return ProductJobStore(connection).count_active_free_reworks(
                owner_user_id=user_id,
                parent_generation_job_id=parent_generation_job_id,
                revision_job_type=REVISION_JOB_TYPE,
            )
    conn = product_db_conn()
    try:
        rows = conn.execute(
            """
            SELECT status, request_json
            FROM generation_jobs
            WHERE status IN ('queued', 'running', 'succeeded')
            """
        ).fetchall()
    finally:
        conn.close()
    used = 0
    for row in rows:
        try:
            contract = json.loads(row["request_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        billing_snapshot = (
            contract.get("billing")
            if isinstance(contract, dict)
            and isinstance(contract.get("billing"), dict)
            else {}
        )
        if (
            str(contract.get("jobType") or "") == REVISION_JOB_TYPE
            and hmac.compare_digest(
                str(contract.get("parentGenerationJobId") or ""),
                parent_generation_job_id,
            )
            and hmac.compare_digest(
                str(contract.get("userId") or ""),
                user_id,
            )
            and str(contract.get("mode") or "") == "rework"
            and billing_snapshot.get("freeReworkQuotaVerified") is True
        ):
            used += 1
    return used


def persist_revision_batch_contract(
    contract: dict[str, Any],
    *,
    menu_upload_id: str | None,
    style_id: str,
) -> tuple[dict[str, Any], bool]:
    conn = product_db_conn()
    try:
        try:
            record = storage_db.create_generation_job(
                conn,
                menu_upload_id=menu_upload_id,
                style_id=style_id,
                quality=str(contract["quality"]["id"]),
                requested_count=1,
                request=contract,
                status="queued",
                job_id=str(contract["jobId"]),
            )
            return record, True
        except sqlite3.IntegrityError:
            try:
                existing = storage_db.get_generation_job(
                    conn,
                    str(contract["jobId"]),
                )
            except KeyError as exc:
                raise RefinementContractError(
                    "revision_job_persist_failed",
                    "revision job could not be persisted",
                    field="jobId",
                ) from exc
            existing_contract = (
                existing.get("request")
                if isinstance(existing.get("request"), dict)
                else {}
            )
            existing_sha = str(
                existing_contract.get("idempotency", {}).get(
                    "requestSha256",
                    "",
                )
                if isinstance(existing_contract.get("idempotency"), dict)
                else ""
            )
            expected_sha = str(contract["idempotency"]["requestSha256"])
            if (
                str(existing_contract.get("jobType") or "") != REVISION_JOB_TYPE
                or not existing_sha
                or not hmac.compare_digest(existing_sha, expected_sha)
            ):
                raise RefinementContractError(
                    "idempotency_conflict",
                    "idempotency key was already used for a different request",
                    field="idempotency.key",
                )
            return existing, False
    finally:
        conn.close()


def refund_revision_batch(
    contract: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any] | None:
    points = int(contract.get("billing", {}).get("totalPoints") or 0)
    if points <= 0:
        return None
    return billing.refund_debit_to_total(
        str(contract["userId"]),
        str(contract["billing"]["debitOrderId"]),
        target_points=points,
        refund_order_prefix=str(contract["billing"]["refundOrderId"]),
        description="图片修改自动退款",
        metadata={
            "jobId": contract["jobId"],
            "parentGenerationJobId": contract["parentGenerationJobId"],
            "requestSha256": contract["idempotency"]["requestSha256"],
            "reason": str(reason or "")[:160],
        },
    )


def persisted_revision_record(
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if postgres_product_runtime_enabled():
        detail = postgres_owned_revision_detail(job_id, principal)
        contract = detail.job["request_payload"]
        return postgres_compatibility_job_record(
            detail,
            contract,
            revision=True,
        ), contract
    conn = product_db_conn()
    try:
        try:
            record = storage_db.get_generation_job(conn, job_id)
        except KeyError as exc:
            raise RedisTaskNotFound("task not found") from exc
    finally:
        conn.close()
    contract = (
        record.get("request")
        if isinstance(record.get("request"), dict)
        else {}
    )
    owner_user_id = str(contract.get("userId") or "")
    if (
        str(contract.get("jobType") or "") != REVISION_JOB_TYPE
        or (
            principal.get("internal") is not True
            and (
                not owner_user_id
                or not hmac.compare_digest(
                    owner_user_id,
                    str(principal.get("userId") or ""),
                )
            )
        )
    ):
        raise RedisTaskNotFound("task not found")
    expected_sha = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if (
        not expected_sha
        or not hmac.compare_digest(
            revision_request_sha256(contract),
            expected_sha,
        )
    ):
        raise RefinementContractError(
            "revision_job_contract_mismatch",
            "persisted revision job contract is invalid",
            field="idempotency.requestSha256",
        )
    return record, contract


def public_revision_submission(
    contract: dict[str, Any],
    *,
    status: str,
    transaction: dict[str, Any] | None = None,
    idempotent: bool = False,
) -> dict[str, Any]:
    response = {
        "jobId": str(contract["jobId"]),
        "status": status,
        "revision": public_revision_payload(contract),
        "account": account_payload(str(contract["userId"])),
        "idempotent": bool(idempotent),
    }
    if transaction is not None:
        response["transaction"] = transaction
    return response


def revision_result_manifest_pointer(
    task_result: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    return {
        "objectKey": str(task_result.get("manifest_object_key") or ""),
        "sha256": str(task_result.get("manifest_sha256") or ""),
        "size": int(task_result.get("manifest_size") or 0),
        "requestSha256": str(task_result.get("request_sha256") or ""),
        "jobId": str(task_result.get("job_id") or ""),
        "taskType": str(task_result.get("task_type") or ""),
    }


def load_revision_result_manifest_document(
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    request_sha256 = str(contract["idempotency"]["requestSha256"])
    expected_manifest_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}revisions/"
        f"{contract['jobId']}/{request_sha256}/manifest.json"
    )
    try:
        object_key = object_storage_service.validate_object_key(
            str(manifest.get("objectKey") or "")
        )
    except (TypeError, ValueError) as exc:
        raise MenuUploadError(
            "revision_manifest_not_ready",
            "修改结果尚未准备完成",
            status=425,
        ) from exc
    if (
        not hmac.compare_digest(
            str(manifest.get("requestSha256") or ""),
            request_sha256,
        )
        or not hmac.compare_digest(
            str(manifest.get("jobId") or str(contract["jobId"])),
            str(contract["jobId"]),
        )
        or (
            str(manifest.get("taskType") or "product_revision")
            != "product_revision"
        )
        or not hmac.compare_digest(object_key, expected_manifest_key)
    ):
        raise MenuUploadError(
            "revision_manifest_pointer_mismatch",
            "修改结果与任务请求不匹配",
        )
    expected_manifest_sha256 = str(
        manifest.get("sha256") or ""
    ).strip().lower()
    expected_manifest_size = int(manifest.get("size") or 0)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256)
        or expected_manifest_size <= 0
        or expected_manifest_size > MAX_REVISION_MANIFEST_BYTES
    ):
        raise MenuUploadError(
            "revision_manifest_invalid",
            "修改结果摘要无效",
        )
    storage = object_storage_service.get_object_storage_service()
    try:
        raw_manifest = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            expected_manifest_size,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "revision_manifest_too_large",
            "修改结果清单超过大小限制",
            status=413,
        ) from exc
    except Exception as exc:
        raise MenuUploadError(
            "revision_manifest_unavailable",
            "修改结果暂时不可用",
            status=503,
        ) from exc
    if (
        len(raw_manifest) != expected_manifest_size
        or not hmac.compare_digest(
            hashlib.sha256(raw_manifest).hexdigest(),
            expected_manifest_sha256,
        )
    ):
        raise MenuUploadError(
            "revision_manifest_integrity_mismatch",
            "修改结果完整性校验失败",
        )
    try:
        document = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MenuUploadError(
            "revision_manifest_invalid",
            "修改结果格式无效",
        ) from exc
    if (
        not isinstance(document, dict)
        or canonical_json(document).encode("utf-8") != raw_manifest
    ):
        raise MenuUploadError(
            "revision_manifest_invalid",
            "修改结果格式无效",
        )
    expected_scalars = {
        "schemaVersion": 1,
        "taskType": "product_revision",
        "jobType": REVISION_JOB_TYPE,
        "jobId": str(contract["jobId"]),
        "parentGenerationJobId": str(contract["parentGenerationJobId"]),
        "requestSha256": request_sha256,
        "mode": str(contract["mode"]),
    }
    if any(
        document.get(key) != value
        for key, value in expected_scalars.items()
    ):
        raise MenuUploadError(
            "revision_manifest_contract_mismatch",
            "修改结果与任务合同不匹配",
        )
    for snapshot_name in ("sourceDeliveryAsset", "selectedBackground"):
        actual = (
            document.get(snapshot_name)
            if isinstance(document.get(snapshot_name), dict)
            else {}
        )
        expected = (
            contract.get(snapshot_name)
            if isinstance(contract.get(snapshot_name), dict)
            else {}
        )
        if any(
            not hmac.compare_digest(
                str(actual.get(key) or ""),
                str(expected.get(key) or ""),
            )
            for key in ("assetId", "objectKey", "sha256")
        ):
            raise MenuUploadError(
                "revision_manifest_contract_mismatch",
                "修改结果使用了不匹配的源图或背景",
            )
    image_asset = (
        document.get("imageAsset")
        if isinstance(document.get("imageAsset"), dict)
        else {}
    )
    expected_asset_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}revisions/"
        f"{contract['jobId']}/{request_sha256}/revision.png"
    )
    try:
        asset_key = object_storage_service.validate_object_key(
            str(image_asset.get("objectKey") or "")
        )
    except (TypeError, ValueError) as exc:
        raise MenuUploadError(
            "revision_asset_invalid",
            "修改图片记录无效",
        ) from exc
    asset_sha256 = str(image_asset.get("sha256") or "").strip().lower()
    asset_size = int(image_asset.get("size") or 0)
    asset_id = str(image_asset.get("assetId") or "")
    if (
        not hmac.compare_digest(asset_key, expected_asset_key)
        or not re.fullmatch(r"revision_[a-f0-9]{32}", asset_id)
        or not re.fullmatch(r"[a-f0-9]{64}", asset_sha256)
        or asset_size <= 0
        or asset_size > MAX_REVISION_IMAGE_BYTES
        or image_asset.get("mimeType") != "image/png"
    ):
        raise MenuUploadError(
            "revision_asset_invalid",
            "修改图片记录无效",
        )
    try:
        raw_asset = object_storage_service.read_object_bytes_limited(
            storage,
            asset_key,
            asset_size,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "revision_asset_too_large",
            "修改图片超过大小限制",
            status=413,
        ) from exc
    except Exception as exc:
        raise MenuUploadError(
            "revision_asset_unavailable",
            "修改图片暂时不可用",
            status=503,
        ) from exc
    if (
        len(raw_asset) != asset_size
        or not hmac.compare_digest(
            hashlib.sha256(raw_asset).hexdigest(),
            asset_sha256,
        )
    ):
        raise MenuUploadError(
            "revision_asset_integrity_mismatch",
            "修改图片完整性校验失败",
        )
    try:
        image_bytes_fingerprint(
            raw_asset,
            max_bytes=MAX_REVISION_IMAGE_BYTES,
        )
    except ValueError as exc:
        raise MenuUploadError(
            "revision_asset_invalid",
            "修改图片格式或尺寸无效",
        ) from exc
    return document


def public_revision_result(
    document: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    image_asset = document["imageAsset"]
    source_asset = contract["sourceDeliveryAsset"]
    provider = (
        document.get("provider")
        if isinstance(document.get("provider"), dict)
        else {}
    )
    composition = (
        document.get("composition")
        if isinstance(document.get("composition"), dict)
        else {}
    )
    return {
        "jobId": str(contract["jobId"]),
        "parentGenerationJobId": str(
            contract["parentGenerationJobId"]
        ),
        "requestSha256": str(
            contract["idempotency"]["requestSha256"]
        ),
        "mode": str(contract["mode"]),
        "rowNumber": int(source_asset["rowNumber"]),
        "dishName": str(source_asset["dishName"]),
        "sourceAssetId": str(source_asset["assetId"]),
        "image": {
            "assetId": str(image_asset["assetId"]),
            "sha256": str(image_asset["sha256"]),
            "size": int(image_asset["size"]),
            "mimeType": "image/png",
            "url": (
                f"/api/image-refinements/"
                f"{urllib.parse.quote(str(contract['jobId']), safe='')}/asset"
            ),
        },
        "provider": {
            "name": str(provider.get("name") or ""),
            "model": str(provider.get("model") or ""),
        },
        "backgroundIdentityVerified": bool(
            composition.get("backgroundIdentityVerified")
        ),
        "outsideMaskPixelsPreserved": bool(
            composition.get("outsideMaskPixelsPreserved")
        ),
    }


def load_persisted_revision_result(
    record: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    result = (
        record.get("result")
        if isinstance(record.get("result"), dict)
        else {}
    )
    manifest = (
        result.get("manifest")
        if isinstance(result.get("manifest"), dict)
        else {}
    )
    return public_revision_result(
        load_revision_result_manifest_document(manifest, contract),
        contract,
    )


def settle_persisted_revision_job(
    contract: dict[str, Any],
    *,
    status: str,
    manifest: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    job_id = str(contract["jobId"])
    conn = product_db_conn()
    try:
        record = storage_db.get_generation_job(conn, job_id)
        persisted_contract = (
            record.get("request")
            if isinstance(record.get("request"), dict)
            else {}
        )
        persisted_sha256 = str(
            persisted_contract.get("idempotency", {}).get(
                "requestSha256",
                "",
            )
            if isinstance(
                persisted_contract.get("idempotency"),
                dict,
            )
            else ""
        )
        if (
            str(persisted_contract.get("jobType") or "")
            != REVISION_JOB_TYPE
            or not hmac.compare_digest(
                persisted_sha256,
                str(contract["idempotency"]["requestSha256"]),
            )
        ):
            raise RefinementContractError(
                "revision_job_contract_mismatch",
                "persisted revision job does not match Redis task",
                field="idempotency.requestSha256",
            )
        current_status = str(record.get("status") or "")
        if current_status in storage_db.TERMINAL_JOB_STATUSES:
            if current_status != status:
                raise RefinementContractError(
                    "revision_job_terminal_conflict",
                    "persisted revision job has a different terminal status",
                    field="status",
                )
            return
        if status == "succeeded" and current_status == "queued":
            storage_db.update_generation_job(
                conn,
                job_id,
                status="running",
            )
        storage_db.update_generation_job(
            conn,
            job_id,
            status=status,
            completed_count=1 if status == "succeeded" else 0,
            failed_count=1 if status == "failed" else 0,
            result={"manifest": manifest} if manifest is not None else None,
            error_message=error_message,
        )
    finally:
        conn.close()


def redis_revision_contract(
    task: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    owner_user_id = str(task.get("owner_user_id") or "")
    if (
        principal.get("internal") is not True
        and (
            not owner_user_id
            or not hmac.compare_digest(
                owner_user_id,
                str(principal.get("userId") or ""),
            )
        )
    ):
        raise RedisTaskNotFound("task not found")
    payload = (
        task.get("payload")
        if isinstance(task.get("payload"), dict)
        else {}
    )
    if str(payload.get("taskType") or "") != "product_revision":
        raise RedisTaskNotFound("task not found")
    contract = (
        payload.get("revisionContract")
        if isinstance(payload.get("revisionContract"), dict)
        else {}
    )
    expected_sha256 = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if (
        str(contract.get("jobType") or "") != REVISION_JOB_TYPE
        or not expected_sha256
        or not hmac.compare_digest(
            revision_request_sha256(contract),
            expected_sha256,
        )
        or not hmac.compare_digest(
            expected_sha256,
            str(task.get("request_sha256") or ""),
        )
        or not hmac.compare_digest(
            str(contract.get("jobId") or ""),
            str(task.get("task_id") or ""),
        )
    ):
        raise RefinementContractError(
            "revision_task_contract_mismatch",
            "revision task contract does not match its Redis envelope",
            field="idempotency.requestSha256",
        )
    return contract


def recover_missing_revision_redis_task(
    redis_product_queue: Any,
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    record, contract = persisted_revision_record(job_id, principal)
    if str(record.get("status") or "") in storage_db.TERMINAL_JOB_STATUSES:
        return None, record
    task = redis_product_queue.enqueue_idempotent(
        {
            "taskType": "product_revision",
            "revisionContract": contract,
        },
        user_id=str(contract["userId"]),
        idempotency_key=str(contract["idempotency"]["key"]),
        request_sha256=str(contract["idempotency"]["requestSha256"]),
        task_id=job_id,
    )
    return task, record


def revision_redis_task_or_terminal_record(
    redis_product_queue: Any,
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return redis_product_queue.get(job_id), None
    except RedisTaskNotFound:
        task, record = recover_missing_revision_redis_task(
            redis_product_queue,
            job_id,
            principal,
        )
        return task, record if task is None else None


def settle_redis_revision_task(
    task: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[dict[str, Any] | None, int]:
    redis_result = (
        task.get("result")
        if isinstance(task.get("result"), dict)
        else {}
    )
    status = product_task_status(
        str(task.get("status") or ""),
        redis_result,
    )
    total_points = int(contract["billing"]["totalPoints"])
    if status == "completed":
        manifest = revision_result_manifest_pointer(
            redis_result,
            contract,
        )
        document = load_revision_result_manifest_document(
            manifest,
            contract,
        )
        result = public_revision_result(document, contract)
        settle_persisted_revision_job(
            contract,
            status="succeeded",
            manifest=manifest,
        )
        return result, 0
    if status in {"failed", "canceled"}:
        refund_revision_batch(
            contract,
            reason=(
                "user_canceled"
                if status == "canceled"
                else "worker_failed"
            ),
        )
        settle_persisted_revision_job(
            contract,
            status="canceled" if status == "canceled" else "failed",
            error_message=(
                "user_canceled"
                if status == "canceled"
                else "worker_failed"
            ),
        )
        return None, total_points
    return None, 0


def revision_job_response(
    *,
    contract: dict[str, Any],
    status: str,
    result: dict[str, Any] | None,
    refunded_points: int,
    cancel_requested: bool = False,
) -> dict[str, Any]:
    response = public_revision_submission(
        contract,
        status=status,
    )
    response.update(
        {
            "result": result,
            "error": (
                "修改任务已取消，积分已自动退回。"
                if status == "canceled"
                else "修改任务失败，积分已自动退回。"
                if status == "failed"
                else None
            ),
            "cancelRequested": bool(cancel_requested),
            "billing": {
                "chargedPoints": int(
                    contract["billing"]["totalPoints"]
                ),
                "refundedPoints": max(0, int(refunded_points)),
                "netPoints": max(
                    0,
                    int(contract["billing"]["totalPoints"])
                    - max(0, int(refunded_points)),
                ),
                "freeReworkQuotaVerified": bool(
                    contract["billing"]["freeReworkQuotaVerified"]
                ),
            },
        }
    )
    return response


def redis_revision_job_payload(
    task: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    contract = redis_revision_contract(task, principal)
    redis_result = (
        task.get("result")
        if isinstance(task.get("result"), dict)
        else {}
    )
    status = product_task_status(
        str(task.get("status") or ""),
        redis_result,
    )
    result, refunded_points = settle_redis_revision_task(
        task,
        contract,
    )
    return revision_job_response(
        contract=contract,
        status=status,
        result=result,
        refunded_points=refunded_points,
        cancel_requested=bool(task.get("cancel_requested")),
    )


def persisted_revision_job_payload(
    record: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    status = {
        "queued": "queued",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
    }.get(str(record.get("status") or ""), "failed")
    result = (
        load_persisted_revision_result(record, contract)
        if status == "completed"
        else None
    )
    refunded_points = (
        int(contract["billing"]["totalPoints"])
        if status in {"failed", "canceled"}
        else 0
    )
    return revision_job_response(
        contract=contract,
        status=status,
        result=result,
        refunded_points=refunded_points,
    )


def run_generation_job(
    style: str,
    quality: str,
    selected_background: SelectedBackgroundAsset | None = None,
) -> dict[str, Any]:
    plan = (
        build_plan(style, quality, selected_background)
        if selected_background is not None
        else build_plan(style, quality)
    )
    plan["generation"] = (
        materialize_final_images(plan, style, quality, selected_background)
        if selected_background is not None
        else materialize_final_images(plan, style, quality)
    )
    return public_plan_payload(plan)


def create_postgres_generation_job(
    contract: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    menu = contract["menu"]
    background = contract["selectedBackground"]
    billing_snapshot = contract["billing"]
    idempotency = contract["idempotency"]
    user_id = str(contract["userId"])
    with postgres_connection() as connection:
        store = ProductJobStore(connection)
        result = store.create_or_get_job_with_debit(
            owner_user_id=user_id,
            idempotency_key=str(idempotency["key"]),
            request_sha256=str(idempotency["requestSha256"]),
            request_payload=contract,
            menu_upload_id=str(contract["menuUploadId"]),
            menu_object_ref=str(menu["objectKey"]),
            menu_object_sha256=str(menu["sha256"]),
            selected_background_ref=str(background["objectKey"]),
            selected_background_sha256=str(background["sha256"]),
            requested_count=int(billing_snapshot["imageCount"]),
            debit_order_id=str(billing_snapshot["debitOrderId"]),
            debit_points=int(billing_snapshot["totalPoints"]),
            refund_order_id=str(billing_snapshot["refundOrderId"]),
            job_id=str(contract["jobId"]),
            outbox_id=f"outbox:{contract['jobId']}",
            account_metadata={"source": "customer_generation"},
            debit_metadata={
                "menuUploadId": contract["menuUploadId"],
                "pricingVersion": billing_snapshot["pricingVersion"],
                "jobType": contract["jobType"],
            },
        )
        account = store.get_account(owner_user_id=user_id)
    if account is None:
        raise WalletIntegrityError(
            f"point account disappeared after job creation: {contract['jobId']}"
        )
    return result.job, account, result.created


def create_postgres_revision_job(
    contract: dict[str, Any],
    parent_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    background = contract["selectedBackground"]
    billing_snapshot = contract["billing"]
    idempotency = contract["idempotency"]
    user_id = str(contract["userId"])
    menu_upload_id = str(parent_record.get("menu_upload_id") or "")
    menu_object_ref = str(parent_record.get("menu_object_ref") or "")
    menu_object_sha256 = str(
        parent_record.get("menu_object_sha256") or ""
    )
    if not menu_upload_id or not menu_object_ref or not menu_object_sha256:
        raise InvalidProductJobInput(
            "parent generation job is missing immutable menu metadata"
        )

    common = {
        "owner_user_id": user_id,
        "idempotency_key": str(idempotency["key"]),
        "request_sha256": str(idempotency["requestSha256"]),
        "request_payload": contract,
        "menu_upload_id": menu_upload_id,
        "menu_object_ref": menu_object_ref,
        "menu_object_sha256": menu_object_sha256,
        "selected_background_ref": str(background["objectKey"]),
        "selected_background_sha256": str(background["sha256"]),
        "requested_count": 1,
        "debit_order_id": str(billing_snapshot["debitOrderId"]),
        "debit_points": int(billing_snapshot["totalPoints"]),
        "refund_order_id": str(billing_snapshot["refundOrderId"]),
        "job_id": str(contract["jobId"]),
        "outbox_id": f"outbox:{contract['jobId']}",
    }
    with postgres_connection() as connection:
        store = ProductJobStore(connection)
        if int(billing_snapshot["totalPoints"]) > 0:
            result = store.create_or_get_job_with_debit(
                **common,
                account_metadata={"source": "customer_revision"},
                debit_metadata={
                    "parentGenerationJobId": contract[
                        "parentGenerationJobId"
                    ],
                    "sourceAssetId": contract["sourceDeliveryAsset"][
                        "assetId"
                    ],
                    "pricingVersion": billing_snapshot["pricingVersion"],
                    "jobType": contract["jobType"],
                },
            )
        else:
            store.get_or_create_account(
                owner_user_id=user_id,
                metadata={"source": "customer_revision"},
            )
            result = store.create_or_get_job(**common)
        account = store.get_account(owner_user_id=user_id)
    if account is None:
        raise WalletIntegrityError(
            "point account disappeared after revision creation: "
            f"{contract['jobId']}"
        )
    return result.job, account, result.created


def postgres_revision_job_candidate(
    contract: dict[str, Any],
    parent_record: dict[str, Any],
) -> RevisionJobCandidate:
    background = contract["selectedBackground"]
    billing_snapshot = contract["billing"]
    user_id = str(contract["userId"])
    menu_upload_id = str(parent_record.get("menu_upload_id") or "")
    menu_object_ref = str(parent_record.get("menu_object_ref") or "")
    menu_object_sha256 = str(
        parent_record.get("menu_object_sha256") or ""
    )
    if not menu_upload_id or not menu_object_ref or not menu_object_sha256:
        raise InvalidProductJobInput(
            "parent generation job is missing immutable menu metadata"
        )
    return RevisionJobCandidate(
        request_sha256=str(
            contract["idempotency"]["requestSha256"]
        ),
        request_payload=contract,
        menu_upload_id=menu_upload_id,
        menu_object_ref=menu_object_ref,
        menu_object_sha256=menu_object_sha256,
        selected_background_ref=str(background["objectKey"]),
        selected_background_sha256=str(background["sha256"]),
        requested_count=1,
        debit_order_id=str(billing_snapshot["debitOrderId"]),
        debit_points=int(billing_snapshot["totalPoints"]),
        refund_order_id=str(billing_snapshot["refundOrderId"]),
        job_id=str(contract["jobId"]),
        outbox_id=f"outbox:{contract['jobId']}",
        account_metadata={"source": "customer_revision"},
        debit_metadata={
            "parentGenerationJobId": contract[
                "parentGenerationJobId"
            ],
            "sourceAssetId": contract["sourceDeliveryAsset"][
                "assetId"
            ],
            "pricingVersion": billing_snapshot["pricingVersion"],
            "jobType": contract["jobType"],
        },
    )


def create_postgres_rework_revision_job_with_quota(
    *,
    free_contract: dict[str, Any],
    paid_contract: dict[str, Any],
    parent_record: dict[str, Any],
    free_rework_limit: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    user_id = str(paid_contract["userId"])
    with postgres_connection() as connection:
        store = ProductJobStore(connection)
        result = store.create_or_get_revision_job_with_quota(
            owner_user_id=user_id,
            parent_generation_job_id=str(
                paid_contract["parentGenerationJobId"]
            ),
            revision_job_type=REVISION_JOB_TYPE,
            idempotency_key=str(
                paid_contract["idempotency"]["key"]
            ),
            free_rework_limit=free_rework_limit,
            free_candidate=postgres_revision_job_candidate(
                free_contract,
                parent_record,
            ),
            paid_candidate=postgres_revision_job_candidate(
                paid_contract,
                parent_record,
            ),
        )
        account = store.get_account(owner_user_id=user_id)
    if account is None:
        raise WalletIntegrityError(
            "point account disappeared after atomic revision creation: "
            f"{paid_contract['jobId']}"
        )
    return (
        result.request_payload,
        result.job,
        account,
        result.created,
    )


def postgres_revision_submission_payload(
    contract: dict[str, Any],
    job: dict[str, Any],
    account: dict[str, Any],
    *,
    created: bool,
) -> dict[str, Any]:
    status = {
        "queued": "queued",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
    }.get(str(job.get("status") or ""), "failed")
    response = {
        "jobId": str(job["id"]),
        "status": status,
        "revision": public_revision_payload(contract),
        "account": {
            "userId": str(contract["userId"]),
            "balance": int(account["balance_points"]),
            "updatedAt": account.get("updated_at"),
        },
        "idempotent": not created,
    }
    total_points = int(contract["billing"]["totalPoints"])
    if total_points > 0:
        response["transaction"] = {
            "ok": True,
            "idempotent": not created,
            "userId": str(contract["userId"]),
            "orderId": str(contract["billing"]["debitOrderId"]),
            "direction": "debit",
            "points": total_points,
            "balance": int(account["balance_points"]),
            "balanceAfter": int(account["balance_points"]),
            "createdAt": job.get("created_at"),
        }
    return response


def postgres_generation_submission_payload(
    contract: dict[str, Any],
    job: dict[str, Any],
    account: dict[str, Any],
    *,
    created: bool,
) -> dict[str, Any]:
    status = {
        "queued": "queued",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
    }.get(str(job.get("status") or ""), "failed")
    total_points = int(contract["billing"]["totalPoints"])
    return {
        "jobId": str(job["id"]),
        "status": status,
        "generationBatch": public_generation_batch_payload(contract),
        "account": {
            "userId": str(contract["userId"]),
            "balance": int(account["balance_points"]),
            "updatedAt": account.get("updated_at"),
        },
        "transaction": {
            "ok": True,
            "idempotent": not created,
            "userId": str(contract["userId"]),
            "orderId": str(contract["billing"]["debitOrderId"]),
            "direction": "debit",
            "points": total_points,
            "balance": int(account["balance_points"]),
            "balanceAfter": int(account["balance_points"]),
            "createdAt": job.get("created_at"),
        },
        "idempotent": not created,
    }


def persist_generation_batch_contract(contract: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    conn = product_db_conn()
    try:
        try:
            record = storage_db.create_generation_job(
                conn,
                menu_upload_id=str(contract["menuUploadId"]),
                style_id=str(contract["selectedBackground"]["styleId"]),
                quality=str(contract["quality"]["id"]),
                requested_count=int(contract["billing"]["imageCount"]),
                request=contract,
                status="queued",
                job_id=str(contract["jobId"]),
            )
            return record, True
        except sqlite3.IntegrityError:
            try:
                existing = storage_db.get_generation_job(conn, str(contract["jobId"]))
            except KeyError as exc:
                raise BatchContractError(
                    "generation_job_persist_failed",
                    "generation job could not be persisted",
                    field="jobId",
                ) from exc
            existing_sha = str(
                existing.get("request", {})
                .get("idempotency", {})
                .get("requestSha256", "")
            )
            expected_sha = str(contract["idempotency"]["requestSha256"])
            if not existing_sha or not hmac.compare_digest(existing_sha, expected_sha):
                raise BatchContractError(
                    "idempotency_conflict",
                    "idempotency key was already used for a different request",
                    field="idempotency.key",
                )
            return existing, False
    finally:
        conn.close()


def update_persisted_generation_job(job_id: str, **updates: Any) -> None:
    conn = product_db_conn()
    try:
        storage_db.update_generation_job(conn, job_id, **updates)
    finally:
        conn.close()


def generation_batch_refund_points(
    contract: dict[str, Any],
    generation: dict[str, Any] | None,
) -> int:
    total_points = int(contract["billing"]["totalPoints"])
    image_count = int(contract["billing"]["imageCount"])
    if not isinstance(generation, dict):
        return total_points
    succeeded = max(0, min(image_count, int(generation.get("succeeded") or 0)))
    if succeeded == 0:
        return total_points
    failed_or_missing = max(0, image_count - succeeded)
    points_per_image = int(contract["quality"]["pointsPerImage"])
    return min(total_points, failed_or_missing * points_per_image)


def refund_generation_batch(
    contract: dict[str, Any],
    *,
    points: int,
    reason: str,
) -> dict[str, Any] | None:
    if points <= 0:
        return None
    return billing.refund_debit_to_total(
        str(contract["userId"]),
        str(contract["billing"]["debitOrderId"]),
        target_points=min(
            int(contract["billing"]["totalPoints"]),
            int(points),
        ),
        refund_order_prefix=str(contract["billing"]["refundOrderId"]),
        description="生成任务自动退款",
        metadata={
            "jobId": contract["jobId"],
            "requestSha256": contract["idempotency"]["requestSha256"],
            "reason": str(reason or "")[:160],
        },
    )


def public_generation_batch_payload(
    contract: dict[str, Any],
    *,
    refunded_points: int = 0,
) -> dict[str, Any]:
    return {
        "jobId": contract["jobId"],
        "menuUploadId": contract["menuUploadId"],
        "requestSha256": contract["idempotency"]["requestSha256"],
        "pricingVersion": contract["billing"]["pricingVersion"],
        "chargedPoints": contract["billing"]["totalPoints"],
        "refundedPoints": max(0, int(refunded_points)),
        "netPoints": max(
            0,
            int(contract["billing"]["totalPoints"]) - max(0, int(refunded_points)),
        ),
    }


def persist_generation_result_manifest(
    contract: dict[str, Any],
    result_document: dict[str, Any],
) -> dict[str, Any]:
    try:
        json_limits.validate_json_size(
            result_document,
            MAX_GENERATION_MANIFEST_BYTES,
        )
        raw = json.dumps(
            result_document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (
        json_limits.InvalidJsonValue,
        json_limits.JsonSizeLimitExceeded,
        TypeError,
        UnicodeEncodeError,
    ) as exc:
        raise RuntimeError(
            "generation result manifest exceeds size limit"
        ) from exc
    if len(raw) > MAX_GENERATION_MANIFEST_BYTES:
        raise RuntimeError("generation result manifest exceeds size limit")
    request_digest = str(contract["idempotency"]["requestSha256"])
    object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}manifests/"
        f"{contract['jobId']}/{request_digest}.json"
    )
    storage = object_storage_service.get_object_storage_service()
    if storage.exists(object_key):
        stored_key = object_key
    else:
        stored_key = storage.put_bytes(raw, object_key=object_key)
    persisted = object_storage_service.read_object_bytes_limited(
        storage,
        stored_key,
        MAX_GENERATION_MANIFEST_BYTES,
    )
    digest = hashlib.sha256(persisted).hexdigest()
    try:
        persisted_payload = json.loads(persisted)
    except json.JSONDecodeError as exc:
        raise RuntimeError("generation result manifest is invalid") from exc
    persisted_batch = (
        persisted_payload.get("generationBatch")
        if isinstance(persisted_payload, dict)
        and isinstance(persisted_payload.get("generationBatch"), dict)
        else {}
    )
    if not hmac.compare_digest(
        str(persisted_batch.get("requestSha256") or ""),
        request_digest,
    ):
        raise RuntimeError("generation result manifest request digest mismatch")
    return {
        "objectKey": stored_key,
        "sha256": digest,
        "size": len(persisted),
        "requestSha256": request_digest,
    }


def load_generation_result_manifest_document(
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    request_digest = str(contract["idempotency"]["requestSha256"])
    if not hmac.compare_digest(
        str(manifest.get("requestSha256") or ""),
        request_digest,
    ):
        raise MenuUploadError(
            "generation_manifest_request_mismatch",
            "生成结果与任务请求不匹配",
        )
    expected_object_key = object_storage_service.validate_object_key(
        f"{object_storage_service.GENERATED_PREFIX}manifests/"
        f"{contract['jobId']}/{request_digest}.json"
    )
    try:
        object_key = object_storage_service.validate_object_key(
            str(manifest.get("objectKey") or "")
        )
    except ValueError as exc:
        raise MenuUploadError(
            "generation_manifest_not_ready",
            "生成结果尚未准备完成",
            status=425,
        ) from exc
    if not hmac.compare_digest(object_key, expected_object_key):
        raise MenuUploadError(
            "generation_manifest_pointer_mismatch",
            "生成结果对象与任务请求不匹配",
        )
    expected_sha256 = str(manifest.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise MenuUploadError(
            "generation_manifest_invalid",
            "生成结果摘要无效",
        )
    try:
        raw = object_storage_service.read_object_bytes_limited(
            object_storage_service.get_object_storage_service(),
            object_key,
            MAX_GENERATION_MANIFEST_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "generation_manifest_too_large",
            "生成结果清单超过大小限制",
            status=413,
        ) from exc
    except Exception as exc:
        raise MenuUploadError(
            "generation_manifest_unavailable",
            "生成结果暂时不可用",
            status=503,
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise MenuUploadError(
            "generation_manifest_integrity_mismatch",
            "生成结果完整性校验失败",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MenuUploadError(
            "generation_manifest_invalid",
            "生成结果格式无效",
        ) from exc
    if not isinstance(payload, dict):
        raise MenuUploadError(
            "generation_manifest_invalid",
            "生成结果格式无效",
        )
    generation_batch = (
        payload.get("generationBatch")
        if isinstance(payload.get("generationBatch"), dict)
        else {}
    )
    if not hmac.compare_digest(
        str(generation_batch.get("requestSha256") or ""),
        request_digest,
    ):
        raise MenuUploadError(
            "generation_manifest_request_mismatch",
            "生成结果与任务请求不匹配",
        )
    return payload


def load_generation_result_manifest_pointer(
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    return public_plan_payload(
        load_generation_result_manifest_document(manifest, contract)
    )


def load_persisted_generation_manifest_context(
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    conn = product_db_conn()
    try:
        try:
            record = storage_db.get_generation_job(conn, job_id)
        except KeyError as exc:
            raise MenuUploadError(
                "generation_manifest_not_found",
                "生成结果不存在",
                status=404,
            ) from exc
    finally:
        conn.close()
    request_contract = record.get("request") if isinstance(record.get("request"), dict) else {}
    owner_user_id = str(request_contract.get("userId") or "")
    if (
        principal.get("internal") is not True
        and (
            not owner_user_id
            or not hmac.compare_digest(
                owner_user_id,
                str(principal.get("userId") or ""),
            )
        )
    ):
        raise MenuUploadError(
            "generation_manifest_not_found",
            "生成结果不存在",
            status=404,
        )
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    return (
        load_generation_result_manifest_document(manifest, request_contract),
        request_contract,
    )


def load_persisted_generation_manifest_document(
    job_id: str,
    principal: dict[str, Any],
) -> dict[str, Any]:
    return load_persisted_generation_manifest_context(job_id, principal)[0]


def load_generation_result_manifest(
    job_id: str,
    principal: dict[str, Any],
) -> dict[str, Any]:
    return public_plan_payload(
        load_persisted_generation_manifest_document(job_id, principal)
    )


def generation_delivery_asset(
    result_document: dict[str, Any],
    asset_id: str,
) -> tuple[str, bytes]:
    normalized_asset_id = str(asset_id or "").strip().lower()
    if not re.fullmatch(r"asset_[a-f0-9]{32}", normalized_asset_id):
        raise MenuUploadError(
            "generation_asset_not_found",
            "生成图片不存在",
            status=404,
        )
    assets = result_document.get("deliveryAssets")
    if not isinstance(assets, list):
        raise MenuUploadError(
            "generation_asset_not_found",
            "生成图片不存在",
            status=404,
        )
    match = next(
        (
            value
            for value in assets
            if isinstance(value, dict)
            and hmac.compare_digest(
                str(value.get("assetId") or "").lower(),
                normalized_asset_id,
            )
        ),
        None,
    )
    if match is None:
        raise MenuUploadError(
            "generation_asset_not_found",
            "生成图片不存在",
            status=404,
        )
    try:
        object_key = object_storage_service.validate_object_key(
            str(match.get("objectKey") or "")
        )
    except (TypeError, ValueError) as exc:
        raise MenuUploadError(
            "generation_asset_invalid",
            "生成图片记录无效",
        ) from exc
    expected_sha256 = str(match.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise MenuUploadError(
            "generation_asset_invalid",
            "生成图片摘要无效",
        )
    try:
        raw = object_storage_service.read_object_bytes_limited(
            object_storage_service.get_object_storage_service(),
            object_key,
            MAX_AI_ASSET_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise MenuUploadError(
            "generation_asset_too_large",
            "生成图片超过大小限制",
            status=413,
        ) from exc
    except Exception as exc:
        raise MenuUploadError(
            "generation_asset_unavailable",
            "生成图片暂时不可用",
            status=503,
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise MenuUploadError(
            "generation_asset_integrity_mismatch",
            "生成图片完整性校验失败",
        )
    try:
        image_bytes_fingerprint(raw)
    except ValueError as exc:
        raise MenuUploadError(
            "generation_asset_invalid",
            "生成图片格式或尺寸无效",
        ) from exc
    return object_key, raw


def persist_generation_delivery_assets(
    contract: dict[str, Any],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    storage = object_storage_service.get_object_storage_service()
    job_id = str(contract["jobId"])
    request_digest = str(contract["idempotency"]["requestSha256"])
    assets: list[dict[str, Any]] = []

    for index, row in enumerate(plan.get("results") or [], start=1):
        if not isinstance(row, dict):
            continue
        candidates = [
            value
            for value in row.get("candidates") or []
            if isinstance(value, dict)
        ]
        candidate = next(
            (
                value
                for value in candidates
                if str(value.get("path") or "")
            ),
            None,
        )
        if candidate is None:
            if candidates:
                raise RuntimeError("generation delivery candidate has no local source")
            continue
        source = Path(str(candidate["path"])).expanduser()
        if not source.is_file():
            raise RuntimeError("generation delivery candidate source is missing")
        if source.stat().st_size > MAX_AI_ASSET_BYTES:
            raise RuntimeError("generation delivery candidate exceeds size limit")
        raw = source.read_bytes()
        fingerprint = image_bytes_fingerprint(raw)
        suffix = source.suffix.lower() if source.suffix.lower() in IMAGE_EXTS else ".png"
        row_number = int(row.get("row") or index)
        object_key = object_storage_service.validate_object_key(
            f"{object_storage_service.GENERATED_PREFIX}delivery/"
            f"{job_id}/{request_digest}/{row_number:04d}_{fingerprint['sha256']}{suffix}"
        )
        if not storage.exists(object_key):
            stored_key = storage.put_bytes(raw, object_key=object_key)
        else:
            stored_key = object_key
        persisted = object_storage_service.read_object_bytes_limited(
            storage,
            stored_key,
            MAX_AI_ASSET_BYTES,
        )
        persisted_sha256 = hashlib.sha256(persisted).hexdigest()
        if not hmac.compare_digest(persisted_sha256, fingerprint["sha256"]):
            raise RuntimeError("generation delivery asset integrity mismatch")

        asset_id = "asset_" + hashlib.sha256(
            f"{job_id}|{request_digest}|{row_number}|{persisted_sha256}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        candidate["deliveryAssetId"] = asset_id
        candidate["url"] = (
            f"/api/generation-jobs/{urllib.parse.quote(job_id, safe='')}"
            f"/assets/{asset_id}"
        )
        row["candidates"] = [candidate]
        assets.append(
            {
                "assetId": asset_id,
                "row": row_number,
                "imageId": str(candidate.get("imageId") or ""),
                "objectKey": stored_key,
                "sha256": persisted_sha256,
                "size": len(persisted),
                "width": int(fingerprint["width"]),
                "height": int(fingerprint["height"]),
            }
        )
    return assets


def generation_manifest_export_results(
    result_document: dict[str, Any],
    staging_dir: Path,
) -> list[dict[str, Any]]:
    public_results = result_document.get("results")
    if not isinstance(public_results, list):
        raise MenuUploadError(
            "generation_manifest_invalid",
            "生成结果缺少菜品图片",
        )
    results = json.loads(json.dumps(public_results, ensure_ascii=False))
    staging_dir.mkdir(parents=True, exist_ok=True)
    for row in results:
        if not isinstance(row, dict):
            continue
        candidates = row.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            continue
        candidate = candidates[0] if isinstance(candidates[0], dict) else None
        if candidate is None:
            continue
        asset_id = str(candidate.get("deliveryAssetId") or "")
        if not asset_id:
            raise MenuUploadError(
                "generation_export_asset_missing",
                "正式图片尚未写入共享存储，暂不能导出",
                status=425,
            )
        object_key, raw = generation_delivery_asset(result_document, asset_id)
        suffix = Path(object_key).suffix.lower()
        if suffix not in IMAGE_EXTS:
            suffix = ".png"
        target = staging_dir / f"{asset_id}{suffix}"
        target.write_bytes(raw)
        candidate["path"] = str(target)
    return results


def apply_revision_export_overrides(
    results: list[dict[str, Any]],
    *,
    parent_generation_job_id: str,
    revision_job_ids: Any,
    principal: dict[str, Any],
    staging_dir: Path,
    source_manifests: list[dict[str, Any]] | None = None,
) -> int:
    if revision_job_ids in (None, []):
        return 0
    if (
        not isinstance(revision_job_ids, list)
        or len(revision_job_ids) > 1000
    ):
        raise MenuUploadError(
            "invalid_revision_exports",
            "修改版本列表无效",
        )
    normalized_ids: list[str] = []
    for value in revision_job_ids:
        try:
            normalized_ids.append(
                validate_generation_job_id(str(value or ""))
            )
        except ValueError as exc:
            raise MenuUploadError(
                "invalid_revision_exports",
                "修改版本列表无效",
            ) from exc
    if len(set(normalized_ids)) != len(normalized_ids):
        raise MenuUploadError(
            "duplicate_revision_export",
            "修改版本列表包含重复任务",
        )

    rows_by_number: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(results, start=1):
        if not isinstance(row, dict):
            continue
        row_number = int(row.get("row") or index)
        rows_by_number[row_number] = row

    overridden_rows: set[int] = set()
    storage = object_storage_service.get_object_storage_service()
    for revision_job_id_value in normalized_ids:
        record, contract = persisted_revision_record(
            revision_job_id_value,
            principal,
        )
        if (
            str(record.get("status") or "") != "succeeded"
            or not hmac.compare_digest(
                str(contract.get("parentGenerationJobId") or ""),
                parent_generation_job_id,
            )
        ):
            raise MenuUploadError(
                "revision_export_not_ready",
                "修改版本尚未完成或不属于当前正式任务",
                status=425,
            )
        result = (
            record.get("result")
            if isinstance(record.get("result"), dict)
            else {}
        )
        manifest = (
            result.get("manifest")
            if isinstance(result.get("manifest"), dict)
            else {}
        )
        document = load_revision_result_manifest_document(
            manifest,
            contract,
        )
        row_number = int(contract["sourceDeliveryAsset"]["rowNumber"])
        if row_number in overridden_rows or row_number not in rows_by_number:
            raise MenuUploadError(
                "revision_export_row_conflict",
                "修改版本与正式任务菜品行不匹配",
            )
        image_asset = document["imageAsset"]
        object_key = object_storage_service.validate_object_key(
            str(image_asset["objectKey"])
        )
        try:
            raw = object_storage_service.read_object_bytes_limited(
                storage,
                object_key,
                MAX_REVISION_IMAGE_BYTES,
            )
        except object_storage_service.ObjectStorageReadLimitExceeded as exc:
            raise MenuUploadError(
                "revision_asset_too_large",
                "修改图片超过大小限制",
                status=413,
            ) from exc
        if (
            len(raw) != int(image_asset["size"])
            or not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(),
                str(image_asset["sha256"]),
            )
        ):
            raise MenuUploadError(
                "revision_asset_integrity_mismatch",
                "修改图片完整性校验失败",
            )
        try:
            image_bytes_fingerprint(
                raw,
                max_bytes=MAX_REVISION_IMAGE_BYTES,
            )
        except ValueError as exc:
            raise MenuUploadError(
                "revision_asset_invalid",
                "修改图片格式或尺寸无效",
            ) from exc
        target = (
            staging_dir
            / f"revision_{row_number:04d}_{image_asset['assetId']}.png"
        )
        target.write_bytes(raw)
        row = rows_by_number[row_number]
        candidates = (
            row.get("candidates")
            if isinstance(row.get("candidates"), list)
            else []
        )
        candidate = (
            dict(candidates[0])
            if candidates and isinstance(candidates[0], dict)
            else {}
        )
        candidate.update(
            {
                "path": str(target),
                "url": (
                    f"/api/image-refinements/"
                    f"{urllib.parse.quote(revision_job_id_value, safe='')}/asset"
                ),
                "revisionJobId": revision_job_id_value,
                "revisionAssetId": str(image_asset["assetId"]),
            }
        )
        row["candidates"] = [candidate]
        if source_manifests is not None:
            source_manifests.append(
                {
                    "jobId": revision_job_id_value,
                    "rowNumber": row_number,
                    "objectKey": str(manifest.get("objectKey") or ""),
                    "sha256": str(manifest.get("sha256") or ""),
                    "requestSha256": str(
                        manifest.get("requestSha256") or ""
                    ),
                }
            )
        overridden_rows.add(row_number)
    return len(overridden_rows)


def generation_contract_export_platforms(
    contract: dict[str, Any],
    requested: Any,
) -> list[str]:
    allowed = [
        str(value.get("id") or "")
        for value in contract.get("platforms") or []
        if isinstance(value, dict) and str(value.get("id") or "") in PLATFORMS
    ]
    requested_platforms = parse_platforms(requested if requested else allowed)
    if not requested_platforms or any(
        platform_id not in allowed for platform_id in requested_platforms
    ):
        raise MenuUploadError(
            "generation_export_platform_not_authorized",
            "导出平台不在已购买的任务范围内",
        )
    return requested_platforms


def generation_contract_export_watermark(
    contract: dict[str, Any],
) -> dict[str, Any]:
    watermark = dict(contract.get("watermark") or {})
    if not watermark.get("enabled") or watermark.get("type") != "logo":
        return watermark
    try:
        object_key = object_storage_service.validate_object_key(
            str(watermark.get("logoObjectKey") or "")
        )
        raw = object_storage_service.read_object_bytes_limited(
            object_storage_service.get_object_storage_service(),
            object_key,
            MAX_LOGO_BYTES,
        )
        expected_sha256 = Path(object_key).stem.lower()
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
            or not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(),
                expected_sha256,
            )
        ):
            raise ValueError("watermark logo digest mismatch")
        logo = bounded_pil_image_from_bytes(
            raw,
            max_bytes=MAX_LOGO_BYTES,
            max_pixels=MAX_LOGO_PIXELS,
        )
        logo.close()
    except Exception as exc:
        raise MenuUploadError(
            "generation_watermark_unavailable",
            "品牌 Logo 暂时不可用",
            status=503,
        ) from exc
    watermark["logoData"] = (
        "data:image/png;base64,"
        + base64.b64encode(raw).decode("ascii")
    )
    return watermark


def execute_generation_batch_job(
    contract: dict[str, Any],
    *,
    execution_guard: Callable[[], None] | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    if execution_guard is not None:
        execution_guard()
    if int(contract.get("schemaVersion") or 0) != (
        BATCH_CONTRACT_SCHEMA_VERSION
    ):
        raise MenuUploadError(
            "generation_contract_version_changed",
            "生成任务合同版本已变化，请重新提交",
        )
    if int((contract.get("menu") or {}).get("parserVersion") or 0) != (
        MENU_PARSER_VERSION
    ):
        raise MenuUploadError(
            "generation_parser_version_changed",
            "菜单解析版本已变化，请重新提交",
        )
    frozen_provenance = contract.get("generationProvenance")
    current_provenance = generation_provenance_snapshot()
    if (
        not isinstance(frozen_provenance, dict)
        or canonical_json(frozen_provenance)
        != canonical_json(current_provenance)
    ):
        raise MenuUploadError(
            "generation_provenance_changed",
            "生图模型或处理版本已变化，请重新提交",
        )
    menu_path = materialize_menu_upload_snapshot(contract["menu"])
    menu_upload_token = ACTIVE_PREVIEW_MENU_UPLOAD_ID.set(
        str(contract.get("menuUploadId") or "").strip()
    )
    try:
        with active_asset_owner(str(contract.get("userId") or "")):
            with active_menu_path(menu_path):
                menu = parse_menu(menu_path)
                expected_count = int(contract["billing"]["imageCount"])
                if int(menu.get("count") or 0) != expected_count:
                    raise MenuUploadError(
                        "menu_upload_count_mismatch",
                        "菜单条目数量与冻结批次不一致",
                    )
                if execution_guard is not None:
                    execution_guard()
                selected_background = selected_background_from_batch_contract(
                    contract
                )
                style = str(contract["selectedBackground"]["styleId"])
                quality = str(contract["quality"]["id"])
                plan = build_plan(
                    style,
                    quality,
                    selected_background,
                    menu_snapshot=menu,
                    account_user_id=None,
                    include_account=False,
                )
                generation = materialize_final_images(
                    plan,
                    style,
                    quality,
                    selected_background,
                    execution_guard,
                    progress_callback,
                )
    finally:
        ACTIVE_PREVIEW_MENU_UPLOAD_ID.reset(menu_upload_token)
    refunded_points = generation_batch_refund_points(contract, generation)
    plan["generation"] = generation
    plan["generationBatch"] = public_generation_batch_payload(
        contract,
        refunded_points=refunded_points,
    )
    plan["deliveryAssets"] = persist_generation_delivery_assets(contract, plan)
    public_result = public_plan_payload(plan)
    if execution_guard is not None:
        execution_guard()
    manifest = persist_generation_result_manifest(contract, plan)
    if execution_guard is not None:
        execution_guard()
    return {
        "publicResult": public_result,
        "manifest": manifest,
        "generation": generation,
        "refundPoints": refunded_points,
    }


def local_generation_progress_callback(
    job_id: str,
) -> Callable[[int, int, int], None]:
    def update(completed: int, failed: int, pending: int) -> None:
        try:
            generation_queue.progress(
                job_id,
                result={
                    "rowProgress": {
                        "processed": completed + failed,
                        "succeeded": completed,
                        "failed": failed,
                        "pending": pending,
                    }
                },
            )
        except Exception:
            app.logger.exception(
                "Local generation progress update failed for %s",
                job_id,
            )

    return update


def run_generation_batch_job(
    contract: dict[str, Any],
    *,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    job_id = str(contract["jobId"])
    try:
        update_persisted_generation_job(job_id, status="running")
        execution = execute_generation_batch_job(
            contract,
            progress_callback=progress_callback,
        )
        generation = execution["generation"]
        refunded_points = int(execution["refundPoints"])
        refund_generation_batch(
            contract,
            points=refunded_points,
            reason="partial_or_missing_outputs",
        )
        public_result = dict(execution["publicResult"])
        public_result["account"] = account_payload(str(contract["userId"]))
        succeeded = max(0, int(generation.get("succeeded") or 0))
        failed = max(0, int(contract["billing"]["imageCount"]) - succeeded)
        update_persisted_generation_job(
            job_id,
            status="succeeded",
            completed_count=succeeded,
            failed_count=failed,
            result={"manifest": execution["manifest"]},
        )
        return public_result
    except Exception as exc:
        refund_error: Exception | None = None
        try:
            refund_generation_batch(
                contract,
                points=int(contract["billing"]["totalPoints"]),
                reason=type(exc).__name__,
            )
        except Exception as compensation_exc:
            refund_error = compensation_exc
            app.logger.exception("Generation refund failed for %s", job_id)
        try:
            update_persisted_generation_job(
                job_id,
                status="failed",
                failed_count=int(contract["billing"]["imageCount"]),
                error_message=(
                    f"{type(exc).__name__}; refund={type(refund_error).__name__}"
                    if refund_error is not None
                    else type(exc).__name__
                ),
            )
        except Exception:
            app.logger.exception("Generation job persistence update failed for %s", job_id)
        if refund_error is not None:
            raise RuntimeError("generation failed and automatic refund requires reconciliation") from exc
        raise


def refresh_generation_timeouts() -> None:
    try:
        generation_queue.fail_timed_out(error=GENERATION_JOB_TIMEOUT_ERROR)
    except Exception:
        return None
    return None


def generation_job_payload(job: Any) -> dict[str, Any]:
    data = job.to_dict()
    row_progress = None
    if isinstance(data.get("result"), dict) and isinstance(
        data["result"].get("rowProgress"),
        dict,
    ):
        row_progress = dict(data["result"]["rowProgress"])
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
        "rowProgress": row_progress,
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


def generation_request_principal() -> tuple[dict[str, Any] | None, Any]:
    if generation_write_authorized():
        return {
            "userId": current_user_id(),
            "internal": True,
            "localDemo": False,
        }, None
    if session_token_from_request():
        session, error = require_authenticated_session()
        if error is not None:
            return None, error
        assert session is not None
        return {
            "userId": str(session["user_id"]),
            "internal": False,
            "localDemo": False,
        }, None
    if local_demo_generation_allowed():
        return {
            "userId": current_user_id(),
            "internal": False,
            "localDemo": True,
        }, None
    return None, (
        jsonify({"error": "请先登录", "code": "auth_required"}),
        401,
    )


def generation_job_owned_by(
    job: Any,
    principal: dict[str, Any],
) -> bool:
    if principal.get("internal") is True:
        return True
    data = job.to_dict() if hasattr(job, "to_dict") else {}
    metadata = data.get("metadata") if isinstance(data, dict) else {}
    owner_user_id = str(
        metadata.get("userId") or ""
        if isinstance(metadata, dict)
        else ""
    ).strip()
    if owner_user_id:
        return hmac.compare_digest(
            owner_user_id,
            str(principal.get("userId") or ""),
        )
    return bool(principal.get("localDemo"))


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


def product_redis_queue():
    if not str(os.environ.get("REDIS_URL") or "").strip():
        return None
    return redis_product_queue_from_env()


def product_redis_required() -> bool:
    if staging_in_process_generation_allowed():
        return False
    return (
        runtime_environment_label() in {"production", "prod", "staging", "render"}
        or render_runtime_detected()
    )


def asset_nonce_consumer() -> download_guard.NonceConsumer | None:
    if not product_redis_required():
        return LOCAL_ASSET_NONCE_CONSUMER
    queue = product_redis_queue()
    if queue is None:
        return None
    return download_guard.RedisNonceConsumer(
        queue.redis,
        key_prefix=f"{queue.config.key_prefix}:asset-nonce",
    )


EXPORT_ACCESS_REQUEST_REJECTED = "request_already_denied"


class PostgresExportNonceConsumer:
    def __init__(
        self,
        *,
        export_id: str,
        owner_user_id: str,
        object_key: str,
        token_nonce: str,
        token_expires_at: int | float,
        digest_secret: str,
        ip_address: str,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        request_seed = str(
            request.headers.get("X-Request-Id")
            or secrets.token_urlsafe(24)
        )
        nonce_digest = hashlib.sha256(
            token_nonce.encode("utf-8")
        ).hexdigest()
        request_digest = hashlib.sha256(
            (
                f"{request_seed}\0{export_id}\0{nonce_digest}"
            ).encode("utf-8")
        ).hexdigest()
        self.export_id = export_id
        self.owner_user_id = owner_user_id
        self.object_key = object_key
        self.token_nonce = token_nonce
        self.token_expires_at = token_expires_at
        self.digest_secret = digest_secret
        self.ip_address = ip_address
        self.expected_sha256 = expected_sha256
        self.expected_size = expected_size
        self.action_id = f"export_access_{request_digest[:32]}"
        self.request_id = f"export_request_{request_digest[:32]}"
        self.reservation_id = (
            f"export_reservation_{secrets.token_hex(24)}"
        )
        self.previous_denial_reason = ""
        self._advisory_lock_key = int.from_bytes(
            hashlib.sha256(token_nonce.encode("utf-8")).digest()[:8],
            "big",
            signed=True,
        )
        self._lock_context: Any | None = None
        self._lock_connection: Any | None = None
        self.reservation_active = False
        self.access_recorded = False

    def consume_once(
        self,
        nonce_request: download_guard.NonceConsumptionRequest,
        *,
        now: int | float | datetime | None = None,
    ) -> download_guard.NonceConsumptionResult:
        del now
        expected_nonce_key = (
            "asset-nonce:"
            + hashlib.sha256(
                self.token_nonce.encode("utf-8")
            ).hexdigest()
        )
        if (
            nonce_request.nonce_key != expected_nonce_key
            or nonce_request.user_id != self.owner_user_id
            or nonce_request.asset_id != self.object_key
            or nonce_request.purpose != asset_security.EXPORT
        ):
            return download_guard.NonceConsumptionResult(
                download_guard.NonceConsumptionStatus.SCOPE_MISMATCH
            )
        if self._lock_connection is None:
            connection_context = postgres_connection()
            connection = connection_context.__enter__()
        else:
            connection_context = None
            connection = self._lock_connection
        try:
            result = product_export_store.ProductExportStore(
                connection,
                audit_digest_secret=self.digest_secret,
            ).consume_nonce(
                action_id=self.action_id,
                request_id=self.request_id,
                owner_user_id=self.owner_user_id,
                export_id=self.export_id,
                token_nonce=self.token_nonce,
                token_expires_at=self.token_expires_at,
                ip_address=self.ip_address,
                metadata={
                    "route": "objects",
                    "purpose": asset_security.EXPORT,
                },
                reservation_id=self.reservation_id,
            )
        except BaseException:
            if connection_context is not None:
                connection_context.__exit__(None, None, None)
            raise
        else:
            if connection_context is not None:
                connection_context.__exit__(None, None, None)
            self._close_advisory_lock()
        self.access_recorded = True
        if result.allowed and not result.idempotent:
            self.reservation_active = False
            status = download_guard.NonceConsumptionStatus.CONSUMED
        elif (
            result.reason
            == product_export_store.ACCESS_TOKEN_EXPIRED
        ):
            status = download_guard.NonceConsumptionStatus.EXPIRED
        elif (
            result.reason
            == product_export_store.ACCESS_TOKEN_REPLAYED
            or result.idempotent
        ):
            status = download_guard.NonceConsumptionStatus.REPLAYED
        else:
            status = download_guard.NonceConsumptionStatus.SCOPE_MISMATCH
        return download_guard.NonceConsumptionResult(status)

    def reserve_for_download(self) -> str:
        if not self._try_acquire_advisory_lock():
            return product_export_store.ACCESS_TOKEN_IN_USE
        assert self._lock_connection is not None
        try:
            store = product_export_store.ProductExportStore(
                self._lock_connection,
                audit_digest_secret=self.digest_secret,
            )
            try:
                existing_audit = store.get_owned_audit(
                    action_id=self.action_id,
                    owner_user_id=self.owner_user_id,
                    export_id=self.export_id,
                )
            except product_export_store.ProductExportNotFound:
                existing_audit = None
            if existing_audit is not None:
                if str(existing_audit.get("request_id") or "") != (
                    self.request_id
                ):
                    raise product_export_store.ProductExportAccessConflict(
                        "export access request identity changed"
                    )
                if bool(existing_audit.get("allowed")):
                    status = product_export_store.ACCESS_TOKEN_REPLAYED
                else:
                    self.previous_denial_reason = str(
                        existing_audit.get("deny_reason") or ""
                    )
                    status = EXPORT_ACCESS_REQUEST_REJECTED
            else:
                status = store.reserve_nonce(
                    owner_user_id=self.owner_user_id,
                    export_id=self.export_id,
                    token_nonce=self.token_nonce,
                    token_expires_at=self.token_expires_at,
                    reservation_id=self.reservation_id,
                    replace_active_reservation=True,
                )
        except BaseException:
            self._close_advisory_lock()
            raise
        if status != product_export_store.NONCE_RESERVED:
            self._close_advisory_lock()
        if status not in {
            EXPORT_ACCESS_REQUEST_REJECTED,
            product_export_store.NONCE_RESERVED,
            product_export_store.ACCESS_TOKEN_REPLAYED,
            product_export_store.ACCESS_TOKEN_EXPIRED,
            product_export_store.ACCESS_TOKEN_IN_USE,
            product_export_store.ACCESS_EXPORT_NOT_READY,
        }:
            raise product_export_store.ProductExportAccessConflict(
                "export nonce reservation returned an invalid status"
            )
        self.reservation_active = (
            status == product_export_store.NONCE_RESERVED
        )
        return status

    def _try_acquire_advisory_lock(self) -> bool:
        context = postgres_connection()
        connection = context.__enter__()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (self._advisory_lock_key,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            acquired = bool(row and row[0])
        except BaseException:
            context.__exit__(None, None, None)
            raise
        if not acquired:
            context.__exit__(None, None, None)
            return False
        self._lock_context = context
        self._lock_connection = connection
        return True

    def _close_advisory_lock(self) -> None:
        context = self._lock_context
        connection = self._lock_connection
        self._lock_context = None
        self._lock_connection = None
        if context is None or connection is None:
            return
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (self._advisory_lock_key,),
                )
                cursor.fetchone()
            finally:
                cursor.close()
        except Exception:
            app.logger.exception(
                "PostgreSQL export advisory lock release unavailable"
            )
        finally:
            context.__exit__(None, None, None)

    def release_reservation(self) -> bool:
        if not self.reservation_active:
            self._close_advisory_lock()
            return False
        connection = self._lock_connection
        try:
            if connection is None:
                with postgres_connection() as fallback_connection:
                    released = product_export_store.ProductExportStore(
                        fallback_connection,
                        audit_digest_secret=self.digest_secret,
                    ).release_nonce_reservation(
                        owner_user_id=self.owner_user_id,
                        export_id=self.export_id,
                        token_nonce=self.token_nonce,
                        token_expires_at=self.token_expires_at,
                        reservation_id=self.reservation_id,
                    )
            else:
                released = product_export_store.ProductExportStore(
                    connection,
                    audit_digest_secret=self.digest_secret,
                ).release_nonce_reservation(
                    owner_user_id=self.owner_user_id,
                    export_id=self.export_id,
                    token_nonce=self.token_nonce,
                    token_expires_at=self.token_expires_at,
                    reservation_id=self.reservation_id,
                )
        finally:
            self.reservation_active = False
            self._close_advisory_lock()
        return released

    def record_denial(self, reason: str) -> None:
        if self.access_recorded:
            return
        with postgres_connection() as connection:
            product_export_store.ProductExportStore(
                connection,
                audit_digest_secret=self.digest_secret,
            ).record_denial(
                action_id=self.action_id,
                request_id=self.request_id,
                owner_user_id=self.owner_user_id,
                export_id=self.export_id,
                ip_address=self.ip_address,
                token_nonce=self.token_nonce,
                deny_reason=reason,
                metadata={
                    "route": "objects",
                    "purpose": asset_security.EXPORT,
                },
            )
        self.access_recorded = True

    def finalize_denial(self, reason: str) -> None:
        if self.access_recorded:
            self._close_advisory_lock()
            return
        if self.reservation_active:
            connection = self._lock_connection
            try:
                if connection is None:
                    with postgres_connection() as fallback_connection:
                        product_export_store.ProductExportStore(
                            fallback_connection,
                            audit_digest_secret=self.digest_secret,
                        ).release_and_record_denial(
                            action_id=self.action_id,
                            request_id=self.request_id,
                            owner_user_id=self.owner_user_id,
                            export_id=self.export_id,
                            token_nonce=self.token_nonce,
                            token_expires_at=self.token_expires_at,
                            reservation_id=self.reservation_id,
                            ip_address=self.ip_address,
                            deny_reason=reason,
                            metadata={
                                "route": "objects",
                                "purpose": asset_security.EXPORT,
                            },
                        )
                else:
                    product_export_store.ProductExportStore(
                        connection,
                        audit_digest_secret=self.digest_secret,
                    ).release_and_record_denial(
                        action_id=self.action_id,
                        request_id=self.request_id,
                        owner_user_id=self.owner_user_id,
                        export_id=self.export_id,
                        token_nonce=self.token_nonce,
                        token_expires_at=self.token_expires_at,
                        reservation_id=self.reservation_id,
                        ip_address=self.ip_address,
                        deny_reason=reason,
                        metadata={
                            "route": "objects",
                            "purpose": asset_security.EXPORT,
                        },
                    )
            finally:
                self.reservation_active = False
                self._close_advisory_lock()
            self.access_recorded = True
            return
        self._close_advisory_lock()
        self.record_denial(reason)

    def record_preflight_denial(self, reason: str) -> None:
        suffix = secrets.token_hex(8)
        with postgres_connection() as connection:
            product_export_store.ProductExportStore(
                connection,
                audit_digest_secret=self.digest_secret,
            ).record_denial(
                action_id=f"{self.action_id}_pre_{suffix}",
                request_id=f"{self.request_id}_pre_{suffix}",
                owner_user_id=self.owner_user_id,
                export_id=self.export_id,
                ip_address=self.ip_address,
                token_nonce=self.token_nonce,
                deny_reason=reason,
                metadata={
                    "route": "objects",
                    "phase": "storage_preflight",
                    "purpose": asset_security.EXPORT,
                },
            )


def postgres_export_nonce_consumer(
    *,
    payload: dict[str, Any],
    requested_key: str,
    owner_user_id: str,
    digest_secret: str,
) -> PostgresExportNonceConsumer:
    export_id = str(payload.get("export_id") or "")
    token_nonce = str(payload.get("nonce") or "")
    token_expires_at = payload.get("expires_at")
    if (
        not export_id
        or not token_nonce
        or isinstance(token_expires_at, bool)
        or not isinstance(token_expires_at, (int, float))
    ):
        raise product_export_store.InvalidProductExportInput(
            "signed export token claims are incomplete"
        )
    with postgres_connection() as connection:
        export_record = product_export_store.ProductExportStore(
            connection,
            audit_digest_secret=digest_secret,
        ).get_owned_private_record(
            export_id=export_id,
            owner_user_id=owner_user_id,
        )
    if not hmac.compare_digest(
        str(export_record.get("zip_object_ref") or ""),
        requested_key,
    ):
        raise product_export_store.ProductExportNotFound(export_id)
    expected_sha256 = str(export_record.get("zip_sha256") or "")
    expected_size = int(export_record.get("zip_size_bytes") or -1)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or expected_size < 0
        or str(export_record.get("status") or "") != "ready"
    ):
        raise product_export_store.ProductExportNotFound(export_id)
    return PostgresExportNonceConsumer(
        export_id=export_id,
        owner_user_id=owner_user_id,
        object_key=requested_key,
        token_nonce=token_nonce,
        token_expires_at=token_expires_at,
        digest_secret=digest_secret,
        ip_address=request_ip(),
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )


def product_task_status(redis_status: str, result: dict[str, Any] | None = None) -> str:
    if redis_status == "pending":
        return "queued"
    if redis_status == "running":
        return "running"
    if redis_status == "done":
        return "completed"
    if isinstance(result, dict) and result.get("canceled") is True:
        return "canceled"
    return "failed"


def redis_batch_contract(
    task: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    owner_user_id = str(task.get("owner_user_id") or "")
    if (
        principal.get("internal") is not True
        and (
            not owner_user_id
            or not hmac.compare_digest(
                owner_user_id,
                str(principal.get("userId") or ""),
            )
        )
    ):
        raise RedisTaskNotFound("task not found")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    if str(payload.get("taskType") or "") != "product_batch":
        raise RedisTaskNotFound("task not found")
    contract = (
        payload.get("batchContract")
        if isinstance(payload.get("batchContract"), dict)
        else {}
    )
    expected_sha = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if (
        not expected_sha
        or not hmac.compare_digest(
            expected_sha,
            str(task.get("request_sha256") or ""),
        )
        or not hmac.compare_digest(str(contract.get("jobId") or ""), str(task.get("task_id") or ""))
    ):
        raise BatchContractError(
            "generation_task_contract_mismatch",
            "generation task contract does not match its Redis envelope",
            field="idempotency.requestSha256",
        )
    return contract


def redis_result_manifest(
    task_result: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    return {
        "objectKey": str(task_result.get("manifest_object_key") or ""),
        "sha256": str(task_result.get("manifest_sha256") or ""),
        "requestSha256": str(task_result.get("request_sha256") or ""),
        "size": int(task_result.get("manifest_size") or 0),
    }


def postgres_compatibility_job_record(
    detail: Any,
    contract: dict[str, Any],
    *,
    revision: bool,
) -> dict[str, Any]:
    job = detail.job
    manifest: dict[str, Any] | None = None
    manifest_ref = str(job.get("manifest_ref") or "")
    manifest_sha256 = str(job.get("manifest_sha256") or "")
    if manifest_ref and manifest_sha256:
        manifest = {
            "objectKey": manifest_ref,
            "sha256": manifest_sha256,
            "requestSha256": str(job.get("request_sha256") or ""),
        }
        if revision:
            try:
                manifest["size"] = len(
                    object_storage_service.read_object_bytes_limited(
                        object_storage_service.get_object_storage_service(),
                        manifest_ref,
                        MAX_REVISION_MANIFEST_BYTES,
                    )
                )
            except Exception as exc:
                raise MenuUploadError(
                    "revision_manifest_unavailable",
                    "修改结果暂时不可用",
                    status=503,
                ) from exc
            manifest["jobId"] = str(job.get("id") or "")
            manifest["taskType"] = "product_revision"
    background = (
        contract.get("selectedBackground")
        if isinstance(contract.get("selectedBackground"), dict)
        else {}
    )
    return {
        "id": str(job["id"]),
        "status": str(job.get("status") or ""),
        "request": contract,
        "menu_upload_id": str(job.get("menu_upload_id") or ""),
        "menu_object_ref": str(job.get("menu_object_ref") or ""),
        "menu_object_sha256": str(
            job.get("menu_object_sha256") or ""
        ),
        "style_id": str(background.get("styleId") or ""),
        "requested_count": int(job.get("requested_count") or 0),
        "completed_count": int(job.get("completed_count") or 0),
        "failed_count": int(job.get("failed_count") or 0),
        "result": {"manifest": manifest} if manifest is not None else None,
        "error_message": str(job.get("error_message") or ""),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "cancel_requested": bool(job.get("cancel_requested")),
    }


def postgres_owned_generation_detail(
    job_id: str,
    principal: dict[str, Any],
) -> Any:
    owner_user_id = str(principal.get("userId") or "")
    with postgres_connection() as connection:
        detail = ProductJobStore(connection).get_owned_job_detail(
            job_id=job_id,
            owner_user_id=owner_user_id,
        )
    if detail is None:
        raise RedisTaskNotFound("task not found")
    contract = (
        detail.job.get("request_payload")
        if isinstance(detail.job.get("request_payload"), dict)
        else {}
    )
    expected_sha = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if (
        str(contract.get("jobType") or "") != BATCH_JOB_TYPE
        or not expected_sha
        or not hmac.compare_digest(
            expected_sha,
            str(detail.job.get("request_sha256") or ""),
        )
        or not hmac.compare_digest(
            str(contract.get("jobId") or ""),
            str(detail.job.get("id") or ""),
        )
        or not hmac.compare_digest(
            str(contract.get("userId") or ""),
            owner_user_id,
        )
    ):
        raise BatchContractError(
            "generation_job_contract_mismatch",
            "PostgreSQL generation job does not match its frozen request",
            field="idempotency.requestSha256",
        )
    return detail


def postgres_owned_revision_detail(
    job_id: str,
    principal: dict[str, Any],
) -> Any:
    owner_user_id = str(principal.get("userId") or "")
    with postgres_connection() as connection:
        detail = ProductJobStore(connection).get_owned_job_detail(
            job_id=job_id,
            owner_user_id=owner_user_id,
        )
    if detail is None:
        raise RedisTaskNotFound("task not found")
    contract = (
        detail.job.get("request_payload")
        if isinstance(detail.job.get("request_payload"), dict)
        else {}
    )
    expected_sha = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if (
        str(contract.get("jobType") or "") != REVISION_JOB_TYPE
        or not expected_sha
        or not hmac.compare_digest(
            expected_sha,
            str(detail.job.get("request_sha256") or ""),
        )
        or not hmac.compare_digest(
            str(contract.get("jobId") or ""),
            str(detail.job.get("id") or ""),
        )
        or not hmac.compare_digest(
            str(contract.get("userId") or ""),
            owner_user_id,
        )
        or not hmac.compare_digest(
            revision_request_sha256(contract),
            expected_sha,
        )
    ):
        raise RefinementContractError(
            "revision_job_contract_mismatch",
            "PostgreSQL revision job does not match its frozen request",
            field="idempotency.requestSha256",
        )
    return detail


def load_postgres_generation_export_context(
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    detail = postgres_owned_generation_detail(job_id, principal)
    job = detail.job
    if str(job.get("status") or "") != "succeeded":
        raise MenuUploadError(
            "generation_manifest_not_ready",
            "生成结果尚未准备完成",
            status=425,
        )
    contract = (
        job.get("request_payload")
        if isinstance(job.get("request_payload"), dict)
        else {}
    )
    manifest = {
        "objectKey": str(job.get("manifest_ref") or ""),
        "sha256": str(job.get("manifest_sha256") or ""),
        "requestSha256": str(job.get("request_sha256") or ""),
    }
    return (
        load_generation_result_manifest_document(manifest, contract),
        contract,
        manifest,
    )


def load_postgres_generation_manifest_context(
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    document, contract, _manifest = (
        load_postgres_generation_export_context(job_id, principal)
    )
    return document, contract


def postgres_apply_generation_settlement(
    job_id: str,
    owner_user_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with postgres_connection() as connection:
        store = ProductJobStore(connection)
        result = settle_terminal_job(
            store,
            job_id=job_id,
            owner_user_id=owner_user_id,
            reconciler_id="web-settlement-reconciler",
        )
    return result.get("settlement"), result.get("account")


def postgres_account_snapshot(owner_user_id: str) -> dict[str, Any]:
    with postgres_connection() as connection:
        account = ProductJobStore(connection).get_account(
            owner_user_id=owner_user_id,
        )
    if account is None:
        raise WalletIntegrityError(
            f"PostgreSQL point account is missing for {owner_user_id}"
        )
    return account


def settle_postgres_generation_task(
    task: dict[str, Any],
    principal: dict[str, Any],
) -> None:
    try:
        completion = completion_from_redis_task(task)
    except ProductGenerationSettlementError as exc:
        raise BatchContractError(
            "generation_task_contract_mismatch",
            str(exc),
            field="idempotency.requestSha256",
        ) from exc
    if completion is None:
        return
    if (
        principal.get("internal") is not True
        and not hmac.compare_digest(
            completion.owner_user_id,
            str(principal.get("userId") or ""),
        )
    ):
        raise RedisTaskNotFound("task not found")
    try:
        with postgres_connection() as connection:
            apply_generation_completion(
                ProductJobStore(connection),
                completion,
                reconciler_id="web-status-reconciler",
            )
    except ProductGenerationSettlementError as exc:
        raise BatchContractError(
            "generation_job_contract_mismatch",
            str(exc),
            field="idempotency.requestSha256",
        )


def settle_postgres_revision_task(
    task: dict[str, Any],
    principal: dict[str, Any],
) -> None:
    try:
        completion = revision_completion_from_redis_task(task)
    except ProductGenerationSettlementError as exc:
        raise RefinementContractError(
            "revision_task_contract_mismatch",
            str(exc),
            field="idempotency.requestSha256",
        ) from exc
    if completion is None:
        return
    if (
        principal.get("internal") is not True
        and not hmac.compare_digest(
            completion.owner_user_id,
            str(principal.get("userId") or ""),
        )
    ):
        raise RedisTaskNotFound("task not found")
    try:
        with postgres_connection() as connection:
            apply_generation_completion(
                ProductJobStore(connection),
                completion,
                reconciler_id="web-revision-status-reconciler",
            )
    except ProductGenerationSettlementError as exc:
        raise RefinementContractError(
            "revision_job_contract_mismatch",
            str(exc),
            field="idempotency.requestSha256",
        ) from exc


def postgres_revision_job_payload(
    job_id: str,
    principal: dict[str, Any],
) -> dict[str, Any]:
    detail = postgres_owned_revision_detail(job_id, principal)
    job = detail.job
    contract = job["request_payload"]
    settlement = detail.settlement
    account = postgres_account_snapshot(str(principal["userId"]))
    record = postgres_compatibility_job_record(
        detail,
        contract,
        revision=True,
    )
    status = {
        "queued": "queued",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
    }.get(str(job.get("status") or ""), "failed")
    result = (
        load_persisted_revision_result(record, contract)
        if status == "completed"
        else None
    )
    refunded_points = int(
        (settlement or {}).get("refund_applied_points") or 0
    )
    account_balance = int(account.get("balance_points") or 0)
    charged_points = int(contract["billing"]["totalPoints"])
    refund_pending = (
        status in {"failed", "canceled"}
        and refunded_points < charged_points
    )
    return {
        "jobId": str(job["id"]),
        "status": status,
        "revision": public_revision_payload(contract),
        "account": {
            "userId": str(principal["userId"]),
            "balance": account_balance,
            "updatedAt": account.get("updated_at"),
        },
        "idempotent": True,
        "result": result,
        "error": (
            "修改任务已取消，积分退款处理中。"
            if status == "canceled" and refund_pending
            else "修改任务已取消，积分已自动退回。"
            if status == "canceled"
            else "修改任务失败，积分退款处理中。"
            if status == "failed" and refund_pending
            else "修改任务失败，积分已自动退回。"
            if status == "failed"
            else None
        ),
        "settlementPending": refund_pending,
        "cancelRequested": bool(job.get("cancel_requested")),
        "billing": {
            "chargedPoints": charged_points,
            "refundedPoints": refunded_points,
            "netPoints": max(
                0,
                charged_points - refunded_points,
            ),
            "freeReworkQuotaVerified": bool(
                contract["billing"]["freeReworkQuotaVerified"]
            ),
        },
    }


def postgres_generation_job_payload(
    job_id: str,
    principal: dict[str, Any],
) -> dict[str, Any]:
    detail = postgres_owned_generation_detail(job_id, principal)
    job = detail.job
    contract = job["request_payload"]
    status = {
        "queued": "queued",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
    }.get(str(job.get("status") or ""), "failed")
    result = None
    if status == "completed":
        result = load_generation_result_manifest_pointer(
            {
                "objectKey": str(job.get("manifest_ref") or ""),
                "sha256": str(job.get("manifest_sha256") or ""),
                "requestSha256": str(job.get("request_sha256") or ""),
            },
            contract,
        )
    settlement = detail.settlement
    account = postgres_account_snapshot(str(principal["userId"]))
    requested = int(job.get("requested_count") or 0)
    completed = int(job.get("completed_count") or 0)
    failed = int(job.get("failed_count") or 0)
    canceled = requested if status == "canceled" else 0
    refunded_points = int(
        (settlement or {}).get("refund_applied_points") or 0
    )
    account_balance = int(account.get("balance_points") or 0)
    charged_points = int(contract["billing"]["totalPoints"])
    refund_pending = (
        status in {"failed", "canceled"}
        and refunded_points < charged_points
    )
    if isinstance(result, dict):
        result["account"] = {
            "userId": str(principal["userId"]),
            "balance": account_balance,
        }
    return {
        "jobId": str(job["id"]),
        "status": status,
        "requested": requested,
        "pending": max(0, requested - completed - failed - canceled),
        "completed": completed,
        "failed": failed,
        "canceled": canceled,
        "result": result,
        "error": (
            "任务已取消，积分退款处理中。"
            if status == "canceled" and refund_pending
            else "任务已取消，积分已自动退回。"
            if status == "canceled"
            else "生成任务失败，积分退款处理中。"
            if status == "failed" and refund_pending
            else "生成任务失败，积分已自动退回。"
            if status == "failed"
            else None
        ),
        "settlementPending": refund_pending,
        "createdAt": job.get("created_at"),
        "updatedAt": job.get("updated_at"),
        "startedAt": job.get("started_at"),
        "finishedAt": job.get("completed_at"),
        "completedAt": job.get("completed_at"),
        "elapsed": None,
        "elapsedSeconds": None,
        "stale": False,
        "timedOut": False,
        "timingReason": None,
        "ageSeconds": None,
        "inactiveSeconds": None,
        "staleAfterSeconds": None,
        "timeoutSeconds": None,
        "account": {
            "userId": str(principal["userId"]),
            "balance": account_balance,
        },
        "cancelRequested": bool(job.get("cancel_requested")),
        "generationBatch": public_generation_batch_payload(
            contract,
            refunded_points=refunded_points,
        ),
    }


def settle_persisted_generation_job(
    contract: dict[str, Any],
    *,
    status: str,
    completed_count: int,
    failed_count: int,
    manifest: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    job_id = str(contract["jobId"])
    conn = product_db_conn()
    try:
        record = storage_db.get_generation_job(conn, job_id)
        persisted_sha = str(
            record.get("request", {})
            .get("idempotency", {})
            .get("requestSha256", "")
        )
        expected_sha = str(contract["idempotency"]["requestSha256"])
        if not persisted_sha or not hmac.compare_digest(persisted_sha, expected_sha):
            raise BatchContractError(
                "generation_job_contract_mismatch",
                "persisted generation job does not match Redis task",
                field="idempotency.requestSha256",
            )
        current_status = str(record.get("status") or "")
        if current_status in storage_db.TERMINAL_JOB_STATUSES:
            if current_status != status:
                raise BatchContractError(
                    "generation_job_terminal_conflict",
                    "persisted generation job has a different terminal status",
                    field="status",
                )
            return
        if status == "succeeded" and current_status == "queued":
            storage_db.update_generation_job(conn, job_id, status="running")
        storage_db.update_generation_job(
            conn,
            job_id,
            status=status,
            completed_count=completed_count,
            failed_count=failed_count,
            result={"manifest": manifest} if manifest is not None else None,
            error_message=error_message,
        )
    finally:
        conn.close()


def persisted_generation_contract(
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if postgres_product_runtime_enabled():
        detail = postgres_owned_generation_detail(job_id, principal)
        contract = detail.job["request_payload"]
        return postgres_compatibility_job_record(
            detail,
            contract,
            revision=False,
        ), contract
    conn = product_db_conn()
    try:
        try:
            record = storage_db.get_generation_job(conn, job_id)
        except KeyError as exc:
            raise RedisTaskNotFound("task not found") from exc
    finally:
        conn.close()
    contract = record.get("request") if isinstance(record.get("request"), dict) else {}
    owner_user_id = str(contract.get("userId") or "")
    if (
        principal.get("internal") is not True
        and (
            not owner_user_id
            or not hmac.compare_digest(
                owner_user_id,
                str(principal.get("userId") or ""),
            )
        )
    ):
        raise RedisTaskNotFound("task not found")
    expected_sha = str(
        contract.get("idempotency", {}).get("requestSha256") or ""
        if isinstance(contract.get("idempotency"), dict)
        else ""
    )
    if not expected_sha:
        raise BatchContractError(
            "generation_job_contract_missing",
            "persisted generation job does not contain a frozen request",
            field="idempotency.requestSha256",
        )
    return record, contract


def recover_missing_product_redis_task(
    redis_product_queue: Any,
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    record, contract = persisted_generation_contract(job_id, principal)
    if str(record.get("status") or "") in storage_db.TERMINAL_JOB_STATUSES:
        return None, record
    task = redis_product_queue.enqueue_idempotent(
        {
            "taskType": "product_batch",
            "batchContract": contract,
        },
        user_id=str(contract["userId"]),
        idempotency_key=str(contract["idempotency"]["key"]),
        request_sha256=str(contract["idempotency"]["requestSha256"]),
        task_id=job_id,
    )
    return task, record


def product_redis_task_or_terminal_record(
    redis_product_queue: Any,
    job_id: str,
    principal: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return redis_product_queue.get(job_id), None
    except RedisTaskNotFound:
        task, record = recover_missing_product_redis_task(
            redis_product_queue,
            job_id,
            principal,
        )
        return task, record if task is None else None


def persisted_generation_job_payload(
    record: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    contract = record.get("request") if isinstance(record.get("request"), dict) else {}
    status_map = {
        "queued": "queued",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "canceled": "canceled",
    }
    status = status_map.get(str(record.get("status") or ""), "failed")
    result = (
        load_generation_result_manifest(str(record["id"]), principal)
        if status == "completed"
        else None
    )
    account = account_payload(str(principal["userId"]))
    if isinstance(result, dict):
        result["account"] = account
    requested = int(record.get("requested_count") or 0)
    completed = int(record.get("completed_count") or 0)
    failed = int(record.get("failed_count") or 0)
    canceled = requested if status == "canceled" else 0
    pending = max(0, requested - completed - failed - canceled)
    refunded_points = 0
    if isinstance(result, dict) and isinstance(result.get("generationBatch"), dict):
        refunded_points = int(
            result["generationBatch"].get("refundedPoints") or 0
        )
    elif status in {"failed", "canceled"}:
        refunded_points = int(contract.get("billing", {}).get("totalPoints") or 0)
    return {
        "jobId": str(record["id"]),
        "status": status,
        "requested": requested,
        "pending": pending,
        "completed": completed,
        "failed": failed,
        "canceled": canceled,
        "result": result,
        "error": (
            "任务已取消，积分已自动退回。"
            if status == "canceled"
            else "生成任务失败，积分已自动退回。"
            if status == "failed"
            else None
        ),
        "createdAt": record.get("created_at"),
        "updatedAt": record.get("updated_at"),
        "startedAt": record.get("started_at"),
        "finishedAt": record.get("completed_at"),
        "completedAt": record.get("completed_at"),
        "elapsed": None,
        "elapsedSeconds": None,
        "stale": False,
        "timedOut": False,
        "timingReason": None,
        "ageSeconds": None,
        "inactiveSeconds": None,
        "staleAfterSeconds": None,
        "timeoutSeconds": None,
        "account": account,
        "cancelRequested": False,
        "generationBatch": public_generation_batch_payload(
            contract,
            refunded_points=refunded_points,
        ),
    }


def settle_redis_generation_task(
    task: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[dict[str, Any] | None, int]:
    redis_result = task.get("result") if isinstance(task.get("result"), dict) else {}
    status = product_task_status(str(task.get("status") or ""), redis_result)
    image_count = int(contract["billing"]["imageCount"])
    if status == "completed":
        manifest = redis_result_manifest(redis_result, contract)
        public_result = load_generation_result_manifest_pointer(manifest, contract)
        generation = (
            public_result.get("generation")
            if isinstance(public_result.get("generation"), dict)
            else {}
        )
        refunded_points = generation_batch_refund_points(contract, generation)
        refund_generation_batch(
            contract,
            points=refunded_points,
            reason="partial_or_missing_outputs",
        )
        succeeded = max(0, min(image_count, int(generation.get("succeeded") or 0)))
        settle_persisted_generation_job(
            contract,
            status="succeeded",
            completed_count=succeeded,
            failed_count=max(0, image_count - succeeded),
            manifest=manifest,
        )
        return public_result, refunded_points
    if status in {"failed", "canceled"}:
        refund_generation_batch(
            contract,
            points=int(contract["billing"]["totalPoints"]),
            reason="user_canceled" if status == "canceled" else "worker_failed",
        )
        settle_persisted_generation_job(
            contract,
            status="canceled" if status == "canceled" else "failed",
            completed_count=0,
            failed_count=0 if status == "canceled" else image_count,
            error_message="user_canceled" if status == "canceled" else "worker_failed",
        )
        return None, int(contract["billing"]["totalPoints"])
    return None, 0


def redis_timestamp_iso(value: Any) -> str | None:
    try:
        milliseconds = int(value or 0)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def redis_generation_job_payload(
    task: dict[str, Any],
    principal: dict[str, Any],
) -> dict[str, Any]:
    contract = redis_batch_contract(task, principal)
    redis_result = task.get("result") if isinstance(task.get("result"), dict) else {}
    status = product_task_status(str(task.get("status") or ""), redis_result)
    result, refunded_points = settle_redis_generation_task(task, contract)
    generation = result.get("generation") if isinstance(result, dict) and isinstance(result.get("generation"), dict) else {}
    requested = int(contract.get("billing", {}).get("imageCount") or 0) if isinstance(contract.get("billing"), dict) else 0
    completed = int(generation.get("succeeded") or (requested if status == "completed" else 0))
    failed = int(generation.get("failed") or (requested if status == "failed" else 0))
    canceled = requested if status == "canceled" else 0
    pending = max(0, requested - completed - failed - canceled)
    account = account_payload(str(principal["userId"]))
    if isinstance(result, dict):
        result["account"] = account
    response = {
        "jobId": str(task.get("task_id") or ""),
        "status": status,
        "requested": requested,
        "pending": pending,
        "completed": completed,
        "failed": failed,
        "canceled": canceled,
        "result": result,
        "error": (
            "任务已取消，积分已自动退回。"
            if status == "canceled"
            else "生成任务失败，积分已自动退回。"
            if status == "failed"
            else None
        ),
        "createdAt": redis_timestamp_iso(task.get("created_at")),
        "updatedAt": redis_timestamp_iso(task.get("updated_at")),
        "startedAt": redis_timestamp_iso(task.get("started_at")),
        "finishedAt": redis_timestamp_iso(task.get("finished_at")),
        "completedAt": redis_timestamp_iso(task.get("finished_at")),
        "elapsed": None,
        "elapsedSeconds": None,
        "stale": False,
        "timedOut": False,
        "timingReason": None,
        "ageSeconds": None,
        "inactiveSeconds": None,
        "staleAfterSeconds": None,
        "timeoutSeconds": None,
        "account": account,
        "cancelRequested": bool(task.get("cancel_requested")),
    }
    if contract:
        response["generationBatch"] = public_generation_batch_payload(
            contract,
            refunded_points=refunded_points,
        )
    return response


def batch_contract_error_response(exc: BatchContractError):
    status = 409 if exc.code in {"idempotency_conflict", "generation_job_terminal"} else 400
    return (
        jsonify(
            {
                "error": str(exc),
                "code": exc.code,
                "field": exc.field,
            }
        ),
        status,
    )


@app.post("/api/image-refinements")
def api_image_refinements():
    payload = request.get_json(silent=True) or {}
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        parent_job_id = validate_generation_job_id(
            str(
                payload.get("parentGenerationJobId")
                or payload.get("parent_generation_job_id")
                or ""
            )
        )
        source_asset_id = validate_generation_job_id(
            str(
                payload.get("sourceAssetId")
                or payload.get("source_asset_id")
                or ""
            )
        )
        source_revision_job_id_raw = str(
            payload.get("sourceRevisionJobId")
            or payload.get("source_revision_job_id")
            or ""
        ).strip()
        source_revision_job_id = (
            validate_generation_job_id(source_revision_job_id_raw)
            if source_revision_job_id_raw
            else ""
        )
        mode = str(payload.get("mode") or "").strip().lower()
        refine_prompt = payload.get(
            "prompt",
            payload.get("refinePrompt"),
        )
        idempotency_key = validate_generation_job_id(
            str(
                payload.get("idempotencyKey")
                or payload.get("idempotency_key")
                or ""
            )
        )
        job_id = revision_job_id(
            user_id=str(principal["userId"]),
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_revision_request"}), 400

    with REVISION_BATCH_SUBMIT_LOCK:
        try:
            existing_record, existing_contract = persisted_revision_record(
                job_id,
                principal,
            )
        except RedisTaskNotFound:
            existing_record = None
            existing_contract = None
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except RefinementContractError as exc:
            return batch_contract_error_response(exc)
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL revision replay lookup failed for %s",
                job_id,
            )
            return (
                jsonify(
                    {
                        "error": "图片修改服务暂时不可用，未重复扣费。",
                        "code": "postgres_revision_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        if existing_record is not None and existing_contract is not None:
            if not hmac.compare_digest(
                str(
                    existing_contract.get(
                        "sourceDeliveryAsset",
                        {},
                    ).get("assetId")
                    if isinstance(
                        existing_contract.get("sourceDeliveryAsset"),
                        dict,
                    )
                    else ""
                ),
                source_asset_id,
            ):
                return batch_contract_error_response(
                    RefinementContractError(
                        "idempotency_conflict",
                        "idempotency key was already used for a different source image",
                        field="idempotency.key",
                    )
                )
            try:
                replay_contract = freeze_revision_batch_contract(
                    job_id=job_id,
                    parent_generation_job_id=parent_job_id,
                    user_id=str(principal["userId"]),
                    source_delivery_asset=existing_contract[
                        "sourceDeliveryAsset"
                    ],
                    selected_background=existing_contract[
                        "selectedBackground"
                    ],
                    quality=str(existing_contract["quality"]["id"]),
                    mode=mode,
                    refine_prompt=refine_prompt,
                    idempotency_key=idempotency_key,
                    free_rework_quota_verified=bool(
                        existing_contract["billing"][
                            "freeReworkQuotaVerified"
                        ]
                    ),
                    debit_order_id=str(
                        existing_contract["billing"]["debitOrderId"]
                    ),
                    refund_order_id=str(
                        existing_contract["billing"]["refundOrderId"]
                    ),
                    pricing_version=str(
                        existing_contract["billing"]["pricingVersion"]
                    ),
                    created_at=str(existing_contract["createdAt"]),
                )
            except (KeyError, RefinementContractError) as exc:
                if isinstance(exc, RefinementContractError):
                    return batch_contract_error_response(exc)
                return batch_contract_error_response(
                    RefinementContractError(
                        "revision_job_contract_mismatch",
                        "persisted revision job contract is invalid",
                        field="jobId",
                    )
                )
            if not hmac.compare_digest(
                str(
                    replay_contract["idempotency"]["requestSha256"]
                ),
                str(
                    existing_contract["idempotency"]["requestSha256"]
                ),
            ):
                return batch_contract_error_response(
                    RefinementContractError(
                        "idempotency_conflict",
                        "idempotency key was already used for a different request",
                        field="idempotency.key",
                    )
                )
            status_map = {
                "queued": "queued",
                "running": "running",
                "succeeded": "completed",
                "failed": "failed",
                "canceled": "canceled",
            }
            return jsonify(
                public_revision_submission(
                    existing_contract,
                    status=status_map.get(
                        str(existing_record.get("status") or ""),
                        "failed",
                    ),
                    idempotent=True,
                )
            )

        readiness = gemini_image_edit_readiness()
        if not bool(readiness.get("ready")):
            return (
                jsonify(
                    {
                        "error": "图片精修服务未配置，已停止扣费。",
                        "code": "image_refinement_provider_not_ready",
                        "readiness": readiness,
                    }
                ),
                503,
            )
        try:
            redis_product_queue = product_redis_queue()
        except (RedisQueueError, ValueError, TypeError) as exc:
            return generation_queue_error_response(RuntimeError(str(exc)))
        if redis_product_queue is None:
            return (
                jsonify(
                    {
                        "error": "图片修改队列未配置，已停止扣费。",
                        "code": "redis_revision_queue_required",
                    }
                ),
                503,
            )

        postgres_runtime = postgres_product_runtime_enabled()
        free_contract: dict[str, Any] | None = None
        paid_contract: dict[str, Any] | None = None
        try:
            if source_revision_job_id:
                (
                    parent_record,
                    source_snapshot,
                    background_snapshot,
                ) = resolve_revision_delivery_asset_snapshot(
                    source_revision_job_id,
                    source_asset_id,
                    parent_job_id,
                    principal,
                )
            else:
                (
                    parent_record,
                    source_snapshot,
                    background_snapshot,
                ) = resolve_parent_delivery_asset_snapshot(
                    parent_job_id,
                    source_asset_id,
                    principal,
                )
            parent_contract = (
                parent_record.get("request")
                if isinstance(parent_record.get("request"), dict)
                else {}
            )
            parent_image_count = int(
                parent_contract.get("billing", {}).get("imageCount") or 0
                if isinstance(parent_contract.get("billing"), dict)
                else 0
            )
            free_quota = free_rework_quota(parent_image_count)
            contract_kwargs = {
                "job_id": job_id,
                "parent_generation_job_id": parent_job_id,
                "user_id": str(principal["userId"]),
                "source_delivery_asset": source_snapshot,
                "selected_background": background_snapshot,
                "quality": str(
                    parent_contract.get("quality", {}).get("id") or ""
                ),
                "mode": mode,
                "refine_prompt": refine_prompt,
                "idempotency_key": idempotency_key,
            }
            if postgres_runtime and mode == "rework":
                free_contract = freeze_revision_batch_contract(
                    **contract_kwargs,
                    free_rework_quota_verified=True,
                )
                paid_contract = freeze_revision_batch_contract(
                    **contract_kwargs,
                    free_rework_quota_verified=False,
                )
                contract = paid_contract
            else:
                free_rework = False
                if mode == "rework":
                    free_used = revision_free_rework_usage(
                        parent_generation_job_id=parent_job_id,
                        user_id=str(principal["userId"]),
                    )
                    free_rework = free_used < free_quota
                contract = freeze_revision_batch_contract(
                    **contract_kwargs,
                    free_rework_quota_verified=free_rework,
                )
        except RedisTaskNotFound:
            return jsonify({"error": "原始生成任务不存在"}), 404
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except RefinementContractError as exc:
            return batch_contract_error_response(exc)
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc), "code": "invalid_revision_request"}), 400

        if postgres_runtime:
            try:
                if mode == "rework":
                    assert free_contract is not None
                    assert paid_contract is not None
                    (
                        contract,
                        postgres_job,
                        postgres_account,
                        postgres_created,
                    ) = create_postgres_rework_revision_job_with_quota(
                        free_contract=free_contract,
                        paid_contract=paid_contract,
                        parent_record=parent_record,
                        free_rework_limit=free_quota,
                    )
                else:
                    (
                        postgres_job,
                        postgres_account,
                        postgres_created,
                    ) = create_postgres_revision_job(
                        contract,
                        parent_record,
                    )
            except InsufficientPointBalance as exc:
                return billing_json_error(postgres_wallet_error(exc))
            except RequestDigestConflict as exc:
                return batch_contract_error_response(
                    RefinementContractError(
                        "idempotency_conflict",
                        str(exc),
                        field="idempotency.key",
                    )
                )
            except (PointOrderConflict, WalletIntegrityError) as exc:
                app.logger.exception(
                    "PostgreSQL revision wallet integrity failed for %s",
                    job_id,
                )
                return (
                    jsonify(
                        {
                            "error": "积分账本暂时不可用，未重复扣费。",
                            "code": "postgres_wallet_integrity_failed",
                            "reason": type(exc).__name__,
                        }
                    ),
                    503,
                )
            except InvalidProductJobInput as exc:
                return batch_contract_error_response(
                    RefinementContractError(
                        "invalid_revision_job",
                        str(exc),
                        field="jobId",
                    )
                )
            except (ProductJobStoreError, PostgresRuntimeError) as exc:
                app.logger.exception(
                    "PostgreSQL revision submission failed for %s",
                    job_id,
                )
                return (
                    jsonify(
                        {
                            "error": "图片修改服务暂时不可用，未创建重复任务。",
                            "code": "postgres_revision_unavailable",
                            "reason": type(exc).__name__,
                        }
                    ),
                    503,
                )
            return jsonify(
                postgres_revision_submission_payload(
                    contract,
                    postgres_job,
                    postgres_account,
                    created=postgres_created,
                )
            )

        ensure_demo_balance(str(principal["userId"]))
        total_points = int(contract["billing"]["totalPoints"])
        transaction: dict[str, Any] | None = None
        if total_points > 0:
            try:
                transaction = billing.debit_account(
                    str(principal["userId"]),
                    str(contract["billing"]["debitOrderId"]),
                    total_points,
                    description=(
                        "单张换一版"
                        if mode == "rework"
                        else "自定义修改"
                    ),
                    metadata={
                        "jobId": job_id,
                        "parentGenerationJobId": parent_job_id,
                        "sourceAssetId": source_asset_id,
                        "requestSha256": contract["idempotency"][
                            "requestSha256"
                        ],
                        "pricingVersion": contract["billing"][
                            "pricingVersion"
                        ],
                    },
                )
            except billing.BillingError as exc:
                return billing_json_error(exc)
        try:
            persisted, created = persist_revision_batch_contract(
                contract,
                menu_upload_id=(
                    str(parent_record.get("menu_upload_id") or "") or None
                ),
                style_id=str(parent_record.get("style_id") or ""),
            )
        except RefinementContractError as exc:
            if (
                total_points > 0
                and transaction is not None
                and not bool(transaction.get("idempotent"))
            ):
                try:
                    refund_revision_batch(contract, reason=exc.code)
                except billing.BillingError:
                    app.logger.exception(
                        "Revision persistence compensation failed for %s",
                        job_id,
                    )
            return batch_contract_error_response(exc)
        if (
            not created
            and str(persisted.get("status") or "")
            in storage_db.TERMINAL_JOB_STATUSES
        ):
            if (
                total_points > 0
                and transaction is not None
                and not bool(transaction.get("idempotent"))
            ):
                try:
                    refund_revision_batch(
                        contract,
                        reason="revision_job_terminal",
                    )
                except billing.BillingError:
                    app.logger.exception(
                        "Revision terminal compensation failed for %s",
                        job_id,
                    )
            return batch_contract_error_response(
                RefinementContractError(
                    "revision_job_terminal",
                    "this idempotency key already completed or failed",
                    field="idempotency.key",
                )
            )
        try:
            task = redis_product_queue.enqueue_idempotent(
                {
                    "taskType": "product_revision",
                    "revisionContract": contract,
                },
                user_id=str(principal["userId"]),
                idempotency_key=idempotency_key,
                request_sha256=str(
                    contract["idempotency"]["requestSha256"]
                ),
                task_id=job_id,
            )
        except RedisIdempotencyConflict as exc:
            return batch_contract_error_response(
                RefinementContractError(
                    "idempotency_conflict",
                    str(exc),
                    field="idempotency.key",
                )
            )
        except RedisQueueError as exc:
            try:
                accepted_task = redis_product_queue.get(job_id)
            except (RedisTaskNotFound, RedisQueueError):
                accepted_task = None
            if accepted_task is None:
                try:
                    refund_revision_batch(
                        contract,
                        reason="enqueue_failed",
                    )
                    update_persisted_generation_job(
                        job_id,
                        status="failed",
                        failed_count=1,
                        error_message="enqueue_failed",
                    )
                except Exception:
                    app.logger.exception(
                        "Revision enqueue compensation failed for %s",
                        job_id,
                    )
                return generation_queue_error_response(RuntimeError(str(exc)))
            task = accepted_task

    return jsonify(
        public_revision_submission(
            contract,
            status=product_task_status(
                str(task.get("status") or ""),
                task.get("result")
                if isinstance(task.get("result"), dict)
                else {},
            ),
            transaction=transaction,
            idempotent=bool(
                transaction.get("idempotent")
                if transaction is not None
                else not created
            ),
        )
    )


@app.get("/api/image-refinements/<job_id>")
def api_image_refinement(job_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if postgres_product_runtime_enabled():
        try:
            return jsonify(
                postgres_revision_job_payload(
                    normalized_job_id,
                    principal,
                )
            )
        except (RedisTaskNotFound, PostgresJobNotFound):
            return jsonify({"error": "修改任务不存在"}), 404
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except RefinementContractError as exc:
            return batch_contract_error_response(exc)
        except (
            FenceMismatch,
            JobStateConflict,
            SettlementConflict,
            WalletIntegrityError,
        ) as exc:
            app.logger.exception(
                "PostgreSQL revision settlement pending for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "修改任务已结束，但积分结算需要重试。",
                        "code": "revision_settlement_pending",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL revision status unavailable for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "修改任务状态暂时不可用",
                        "code": "postgres_revision_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except RedisQueueError as exc:
            return generation_queue_error_response(RuntimeError(str(exc)))
    try:
        redis_product_queue = product_redis_queue()
    except (RedisQueueError, ValueError, TypeError) as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))
    try:
        if redis_product_queue is None:
            record, contract = persisted_revision_record(
                normalized_job_id,
                principal,
            )
            if (
                str(record.get("status") or "")
                not in storage_db.TERMINAL_JOB_STATUSES
            ):
                return (
                    jsonify(
                        {
                            "error": "图片修改队列暂不可用",
                            "code": "redis_revision_queue_required",
                        }
                    ),
                    503,
                )
            return jsonify(
                persisted_revision_job_payload(record, contract)
            )
        task, terminal_record = revision_redis_task_or_terminal_record(
            redis_product_queue,
            normalized_job_id,
            principal,
        )
        if task is None:
            assert terminal_record is not None
            _record, contract = persisted_revision_record(
                normalized_job_id,
                principal,
            )
            return jsonify(
                persisted_revision_job_payload(
                    terminal_record,
                    contract,
                )
            )
        return jsonify(
            redis_revision_job_payload(task, principal)
        )
    except RedisTaskNotFound:
        return jsonify({"error": "修改任务不存在"}), 404
    except MenuUploadError as exc:
        return menu_upload_error_response(exc)
    except RefinementContractError as exc:
        return batch_contract_error_response(exc)
    except billing.BillingError as exc:
        app.logger.exception(
            "Revision settlement failed for %s",
            normalized_job_id,
        )
        return (
            jsonify(
                {
                    "error": "修改任务已结束，但积分结算需要重试。",
                    "code": "revision_settlement_pending",
                    "reason": exc.code,
                }
            ),
            503,
        )
    except RedisQueueError as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))


@app.get("/api/image-refinements/<job_id>/asset")
def api_image_refinement_asset(job_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        record, contract = persisted_revision_record(
            normalized_job_id,
            principal,
        )
        if str(record.get("status") or "") != "succeeded":
            raise MenuUploadError(
                "revision_asset_not_ready",
                "修改图片尚未准备完成",
                status=425,
            )
        result = (
            record.get("result")
            if isinstance(record.get("result"), dict)
            else {}
        )
        manifest = (
            result.get("manifest")
            if isinstance(result.get("manifest"), dict)
            else {}
        )
        document = load_revision_result_manifest_document(
            manifest,
            contract,
        )
        image_asset = document["imageAsset"]
        object_key = object_storage_service.validate_object_key(
            str(image_asset["objectKey"])
        )
        raw = object_storage_service.read_object_bytes_limited(
            object_storage_service.get_object_storage_service(),
            object_key,
            MAX_REVISION_IMAGE_BYTES,
        )
        if (
            len(raw) != int(image_asset["size"])
            or not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(),
                str(image_asset["sha256"]),
            )
        ):
            raise MenuUploadError(
                "revision_asset_integrity_mismatch",
                "修改图片完整性校验失败",
            )
        image_bytes_fingerprint(
            raw,
            max_bytes=MAX_REVISION_IMAGE_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded:
        return (
            jsonify(
                {
                    "error": "修改图片超过大小限制",
                    "code": "revision_asset_too_large",
                }
            ),
            413,
        )
    except RedisTaskNotFound:
        return jsonify({"error": "修改图片不存在"}), 404
    except MenuUploadError as exc:
        return menu_upload_error_response(exc)
    except ValueError as exc:
        return (
            jsonify(
                {
                    "error": "修改图片格式或尺寸无效",
                    "code": "revision_asset_invalid",
                    "reason": type(exc).__name__,
                }
            ),
            409,
        )
    except RefinementContractError as exc:
        return batch_contract_error_response(exc)
    except (ProductJobStoreError, PostgresRuntimeError) as exc:
        app.logger.exception(
            "PostgreSQL revision asset unavailable for %s",
            normalized_job_id,
        )
        return (
            jsonify(
                {
                    "error": "修改图片暂时不可用",
                    "code": "postgres_revision_unavailable",
                    "reason": type(exc).__name__,
                }
            ),
            503,
        )
    response = send_file(
        io.BytesIO(raw),
        mimetype="image/png",
        download_name=Path(object_key).name,
    )
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@app.post("/api/image-refinements/<job_id>/cancel")
def api_cancel_image_refinement(job_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if postgres_product_runtime_enabled():
        try:
            detail = postgres_owned_revision_detail(
                normalized_job_id,
                principal,
            )
            durable_status = str(detail.job.get("status") or "")
            if durable_status in {"succeeded", "failed", "canceled"}:
                existing = postgres_revision_job_payload(
                    normalized_job_id,
                    principal,
                )
                if existing["status"] == "canceled":
                    return jsonify(existing)
                return (
                    jsonify(
                        {
                            "error": "任务已结束，不能取消。",
                            "code": "revision_job_already_finished",
                            "job": existing,
                        }
                    ),
                    409,
                )

            try:
                redis_product_queue = product_redis_queue()
            except (RedisQueueError, ValueError, TypeError) as exc:
                return generation_queue_error_response(
                    RuntimeError(str(exc))
                )
            task = None
            if redis_product_queue is not None:
                try:
                    task = redis_product_queue.get(normalized_job_id)
                except RedisTaskNotFound:
                    task = None
                if task is not None:
                    redis_revision_contract(task, principal)
                    redis_status = product_task_status(
                        str(task.get("status") or ""),
                        task.get("result")
                        if isinstance(task.get("result"), dict)
                        else {},
                    )
                    if redis_status in {
                        "completed",
                        "failed",
                        "canceled",
                    }:
                        existing = postgres_revision_job_payload(
                            normalized_job_id,
                            principal,
                        )
                        if redis_status == "canceled":
                            return jsonify(
                                {
                                    **existing,
                                    "status": "canceling",
                                    "cancelRequested": True,
                                    "reconciliationPending": True,
                                }
                            )
                        return (
                            jsonify(
                                {
                                    "error": "任务已结束，不能取消。",
                                    "code": "revision_job_already_finished",
                                    "job": existing,
                                }
                            ),
                            409,
                        )
                    redis_product_queue.request_cancel(normalized_job_id)
                elif durable_status == "running":
                    raise RedisQueueError(
                        "running PostgreSQL revision has no Redis task"
                    )
            elif durable_status == "running":
                raise RedisQueueError(
                    "running PostgreSQL revision queue is unavailable"
                )

            with postgres_connection() as connection:
                canceled = ProductJobStore(connection).request_cancel(
                    job_id=normalized_job_id,
                    owner_user_id=str(principal["userId"]),
                )
            return jsonify(
                postgres_revision_job_payload(
                    normalized_job_id,
                    principal,
                )
            )
        except (PostgresJobNotFound, RedisTaskNotFound):
            return jsonify({"error": "修改任务不存在"}), 404
        except JobStateConflict:
            try:
                existing = postgres_revision_job_payload(
                    normalized_job_id,
                    principal,
                )
            except (PostgresJobNotFound, RedisTaskNotFound):
                return jsonify({"error": "修改任务不存在"}), 404
            return (
                jsonify(
                    {
                        "error": "任务已结束，不能取消。",
                        "code": "revision_job_already_finished",
                        "job": existing,
                    }
                ),
                409,
            )
        except RefinementContractError as exc:
            return batch_contract_error_response(exc)
        except (
            SettlementConflict,
            WalletIntegrityError,
            FenceMismatch,
        ) as exc:
            app.logger.exception(
                "PostgreSQL revision cancellation settlement pending for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "任务已取消，但自动退款需要重试。",
                        "code": "revision_cancel_refund_pending",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL revision cancellation unavailable for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "取消任务暂时不可用",
                        "code": "postgres_revision_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except RedisQueueError as exc:
            return generation_queue_error_response(RuntimeError(str(exc)))
    try:
        redis_product_queue = product_redis_queue()
    except (RedisQueueError, ValueError, TypeError) as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))
    if redis_product_queue is None:
        return (
            jsonify(
                {
                    "error": "图片修改队列暂不可用",
                    "code": "redis_revision_queue_required",
                }
            ),
            503,
        )
    try:
        task, terminal_record = revision_redis_task_or_terminal_record(
            redis_product_queue,
            normalized_job_id,
            principal,
        )
        if task is None:
            assert terminal_record is not None
            _record, contract = persisted_revision_record(
                normalized_job_id,
                principal,
            )
            response = persisted_revision_job_payload(
                terminal_record,
                contract,
            )
            if response["status"] == "canceled":
                return jsonify(response)
            return (
                jsonify(
                    {
                        "error": "任务已结束，不能取消。",
                        "code": "revision_job_already_finished",
                        "job": response,
                    }
                ),
                409,
            )
        current = redis_revision_job_payload(task, principal)
        if current["status"] in {"completed", "failed"}:
            return (
                jsonify(
                    {
                        "error": "任务已结束，不能取消。",
                        "code": "revision_job_already_finished",
                        "job": current,
                    }
                ),
                409,
            )
        if current["status"] == "canceled":
            return jsonify(current)
        requested = redis_product_queue.request_cancel(
            normalized_job_id
        )
        response = redis_revision_job_payload(requested, principal)
        if response["status"] == "running":
            response["cancelRequested"] = True
        return jsonify(response)
    except RedisTaskNotFound:
        return jsonify({"error": "修改任务不存在"}), 404
    except MenuUploadError as exc:
        return menu_upload_error_response(exc)
    except RefinementContractError as exc:
        return batch_contract_error_response(exc)
    except billing.BillingError as exc:
        app.logger.exception(
            "Revision cancellation settlement failed for %s",
            normalized_job_id,
        )
        return (
            jsonify(
                {
                    "error": "任务已取消，但自动退款需要人工核对",
                    "code": "revision_cancel_refund_pending",
                    "reason": exc.code,
                }
            ),
            503,
        )
    except RedisQueueError as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))


@app.post("/api/generation-jobs")
def api_generation_jobs():
    payload = request.get_json(silent=True) or {}
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    menu_upload_id = str(payload.get("menuUploadId") or payload.get("menu_upload_id") or "").strip()

    if not menu_upload_id and principal.get("localDemo"):
        try:
            style = validate_requested_style(str(payload.get("style") or ""))
            quality = quality_config(str(payload.get("quality") or "standard"))["id"]
            selected_background = requested_selected_background(style, payload)
            job_id = generation_job_id(
                style,
                quality,
                payload,
                selected_background,
                user_id=str(principal["userId"]),
            )
        except SelectedBackgroundError as exc:
            return selected_background_error_response(exc)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not tencent_ready() and not local_final_fallback_enabled():
            return jsonify({"error": "混元未配置，已停止正式出图，避免生成错误图片或误扣积分。"}), 503
        refresh_generation_timeouts()
        try:
            job = generation_queue.enqueue(
                job_id,
                run_generation_job,
                style,
                quality,
                selected_background,
                requested=1,
                metadata={
                    "style": style,
                    "quality": quality,
                    "userId": principal["userId"],
                    "selectedBackground": selected_background.public_payload() if selected_background else None,
                    "localDemo": True,
                },
            )
        except RuntimeError as exc:
            return generation_queue_error_response(exc)
        return jsonify({"jobId": job.job_id, "status": job.status, "localDemo": True})

    if not menu_upload_id:
        return (
            jsonify(
                {
                    "error": "请重新上传菜单后再创建生成任务",
                    "code": "menu_upload_id_required",
                }
            ),
            400,
        )
    try:
        menu_snapshot = resolve_menu_upload_snapshot(menu_upload_id, principal)
        menu_path = materialize_menu_upload_snapshot(menu_snapshot)
        style = validate_requested_style(str(payload.get("style") or ""))
        quality = quality_config(str(payload.get("quality") or "standard"))["id"]
        with customer_preview_menu_path(
            menu_path,
            principal,
            menu_upload_id,
        ):
            selected_background = requested_selected_background(style, payload)
            image_count = int(
                menu_snapshot.get("summary", {}).get("count") or 0
            )
            if image_count <= 0:
                image_count = int(parse_menu(menu_path).get("count") or 0)
        if selected_background is None:
            raise SelectedBackgroundError(
                "selected_background_identity_required",
                "请重新选择背景后再生成图片",
            )
        job_id = generation_job_id(
            style,
            quality,
            payload,
            selected_background,
            user_id=str(principal["userId"]),
            menu_upload_id=menu_upload_id,
            menu_sha256=str(menu_snapshot["sha256"]),
        )
        background_snapshot = selected_background_batch_snapshot(
            selected_background,
            menu_snapshot,
        )
        watermark = batch_watermark_snapshot(
            payload.get("watermark"),
            user_id=str(principal["userId"]),
        )
        client_idempotency_key = str(
            payload.get("idempotencyKey")
            or payload.get("idempotency_key")
            or payload.get("jobId")
            or payload.get("job_id")
            or job_id
        )
        contract = freeze_menu_batch_contract(
            job_id=job_id,
            user_id=str(principal["userId"]),
            menu_upload_id=menu_upload_id,
            menu={
                "objectKey": menu_snapshot["objectKey"],
                "sha256": menu_snapshot["sha256"],
                "parserVersion": menu_snapshot["parserVersion"],
            },
            selected_background=background_snapshot,
            generation_provenance=generation_provenance_snapshot(),
            quality=quality,
            image_count=image_count,
            platforms=payload.get("platforms") or ["meituan"],
            watermark=watermark,
            idempotency_key=client_idempotency_key,
        )
    except SelectedBackgroundError as exc:
        return selected_background_error_response(exc)
    except MenuUploadError as exc:
        return menu_upload_error_response(exc)
    except BatchContractError as exc:
        return batch_contract_error_response(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not tencent_ready() and not local_final_fallback_enabled():
        return jsonify({"error": "混元未配置，已停止正式出图，避免生成错误图片或误扣积分。"}), 503

    try:
        redis_product_queue = product_redis_queue()
    except (RedisQueueError, ValueError, TypeError) as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))
    if redis_product_queue is None and product_redis_required():
        return (
            jsonify(
                {
                    "error": "正式生成队列未配置，已停止扣费。",
                    "code": "redis_generation_queue_required",
                }
            ),
            503,
        )
    if postgres_product_runtime_enabled():
        try:
            postgres_job, postgres_account, postgres_created = (
                create_postgres_generation_job(contract)
            )
        except InsufficientPointBalance as exc:
            return billing_json_error(postgres_wallet_error(exc))
        except RequestDigestConflict as exc:
            return batch_contract_error_response(
                BatchContractError(
                    "idempotency_conflict",
                    str(exc),
                    field="idempotency.key",
                )
            )
        except (PointOrderConflict, WalletIntegrityError) as exc:
            app.logger.exception(
                "PostgreSQL generation wallet integrity failed for %s",
                job_id,
            )
            return (
                jsonify(
                    {
                        "error": "积分账本暂时不可用，未重复扣费。",
                        "code": "postgres_wallet_integrity_failed",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except InvalidProductJobInput as exc:
            return batch_contract_error_response(
                BatchContractError(
                    "invalid_generation_job",
                    str(exc),
                    field="jobId",
                )
            )
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL generation submission failed for %s",
                job_id,
            )
            return (
                jsonify(
                    {
                        "error": "正式生成服务暂时不可用，未创建重复任务。",
                        "code": "postgres_generation_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        return jsonify(
            postgres_generation_submission_payload(
                contract,
                postgres_job,
                postgres_account,
                created=postgres_created,
            )
        )
    if redis_product_queue is None:
        refresh_generation_timeouts()
    with GENERATION_BATCH_SUBMIT_LOCK:
        existing_job = (
            generation_queue.get(job_id)
            if redis_product_queue is None
            else None
        )
        if existing_job is not None:
            existing_metadata = existing_job.metadata or {}
            existing_sha = str(existing_metadata.get("requestSha256") or "")
            if not existing_sha or not hmac.compare_digest(
                existing_sha,
                str(contract["idempotency"]["requestSha256"]),
            ):
                return batch_contract_error_response(
                    BatchContractError(
                        "idempotency_conflict",
                        "idempotency key was already used for a different request",
                        field="idempotency.key",
                    )
                )
            return jsonify(
                {
                    "jobId": existing_job.job_id,
                    "status": existing_job.status,
                    "generationBatch": public_generation_batch_payload(contract),
                    "account": account_payload(str(principal["userId"])),
                    "idempotent": True,
                }
            )

        ensure_demo_balance(str(principal["userId"]))
        try:
            debit = billing.debit_account(
                str(principal["userId"]),
                str(contract["billing"]["debitOrderId"]),
                int(contract["billing"]["totalPoints"]),
                description="正式出图",
                metadata={
                    "jobId": job_id,
                    "menuUploadId": menu_upload_id,
                    "requestSha256": contract["idempotency"]["requestSha256"],
                    "pricingVersion": contract["billing"]["pricingVersion"],
                },
            )
        except billing.BillingError as exc:
            return billing_json_error(exc)

        try:
            persisted, created = persist_generation_batch_contract(contract)
        except BatchContractError as exc:
            if not bool(debit.get("idempotent")):
                try:
                    refund_generation_batch(
                        contract,
                        points=int(contract["billing"]["totalPoints"]),
                        reason=exc.code,
                    )
                except billing.BillingError:
                    app.logger.exception("Generation persistence compensation failed for %s", job_id)
            return batch_contract_error_response(exc)
        if not created and str(persisted.get("status") or "") in storage_db.TERMINAL_JOB_STATUSES:
            if not bool(debit.get("idempotent")):
                try:
                    refund_generation_batch(
                        contract,
                        points=int(contract["billing"]["totalPoints"]),
                        reason="generation_job_terminal",
                    )
                except billing.BillingError:
                    app.logger.exception("Generation terminal compensation failed for %s", job_id)
            return batch_contract_error_response(
                BatchContractError(
                    "generation_job_terminal",
                    "this idempotency key already completed or failed",
                    field="idempotency.key",
                )
            )

        if redis_product_queue is not None:
            try:
                redis_task = redis_product_queue.enqueue_idempotent(
                    {
                        "taskType": "product_batch",
                        "batchContract": contract,
                    },
                    user_id=str(principal["userId"]),
                    idempotency_key=client_idempotency_key,
                    request_sha256=str(contract["idempotency"]["requestSha256"]),
                    task_id=job_id,
                )
            except RedisIdempotencyConflict as exc:
                return batch_contract_error_response(
                    BatchContractError(
                        "idempotency_conflict",
                        str(exc),
                        field="idempotency.key",
                    )
                )
            except RedisQueueError as exc:
                try:
                    accepted_task = redis_product_queue.get(job_id)
                except (RedisTaskNotFound, RedisQueueError):
                    accepted_task = None
                if accepted_task is None:
                    if not bool(debit.get("idempotent")):
                        try:
                            refund_generation_batch(
                                contract,
                                points=int(contract["billing"]["totalPoints"]),
                                reason="enqueue_failed",
                            )
                            update_persisted_generation_job(
                                job_id,
                                status="failed",
                                failed_count=int(contract["billing"]["imageCount"]),
                                error_message="enqueue_failed",
                            )
                        except Exception:
                            app.logger.exception("Generation enqueue compensation failed for %s", job_id)
                    return generation_queue_error_response(RuntimeError(str(exc)))
                redis_task = accepted_task
            submitted_job_id = str(redis_task["task_id"])
            submitted_status = product_task_status(
                str(redis_task["status"]),
                redis_task.get("result") if isinstance(redis_task.get("result"), dict) else {},
            )
        else:
            try:
                job = generation_queue.enqueue(
                    job_id,
                    run_generation_batch_job,
                    contract,
                    progress_callback=local_generation_progress_callback(
                        job_id
                    ),
                    requested=int(contract["billing"]["imageCount"]),
                    metadata={
                        "style": style,
                        "quality": quality,
                        "userId": principal["userId"],
                        "menuUploadId": menu_upload_id,
                        "requestSha256": contract["idempotency"]["requestSha256"],
                        "selectedBackground": selected_background.public_payload(),
                        "batchContract": contract,
                    },
                )
            except RuntimeError as exc:
                accepted_job = generation_queue.get(job_id)
                if accepted_job is None:
                    if not bool(debit.get("idempotent")):
                        try:
                            refund_generation_batch(
                                contract,
                                points=int(contract["billing"]["totalPoints"]),
                                reason="enqueue_failed",
                            )
                            update_persisted_generation_job(
                                job_id,
                                status="failed",
                                failed_count=int(contract["billing"]["imageCount"]),
                                error_message="enqueue_failed",
                            )
                        except Exception:
                            app.logger.exception("Generation enqueue compensation failed for %s", job_id)
                    return generation_queue_error_response(exc)
                job = accepted_job
            submitted_job_id = job.job_id
            submitted_status = job.status

    return jsonify(
        {
            "jobId": submitted_job_id,
            "status": submitted_status,
            "generationBatch": public_generation_batch_payload(contract),
            "account": account_payload(str(principal["userId"])),
            "transaction": debit,
            "idempotent": bool(debit.get("idempotent")),
        }
    )


@app.get("/api/generation-jobs/<job_id>")
def api_generation_job(job_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if postgres_product_runtime_enabled():
        try:
            return jsonify(
                postgres_generation_job_payload(
                    normalized_job_id,
                    principal,
                )
            )
        except (RedisTaskNotFound, PostgresJobNotFound):
            return jsonify({"error": "任务不存在"}), 404
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except (
            FenceMismatch,
            JobStateConflict,
            SettlementConflict,
            WalletIntegrityError,
        ) as exc:
            app.logger.exception(
                "PostgreSQL generation settlement pending for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "任务已结束，但积分结算需要重试。",
                        "code": "generation_settlement_pending",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL generation status unavailable for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "生成任务状态暂时不可用",
                        "code": "postgres_generation_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
    try:
        redis_product_queue = product_redis_queue()
    except (RedisQueueError, ValueError, TypeError) as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))
    if redis_product_queue is not None:
        try:
            task, terminal_record = product_redis_task_or_terminal_record(
                redis_product_queue,
                normalized_job_id,
                principal,
            )
            if task is None:
                assert terminal_record is not None
                return jsonify(
                    persisted_generation_job_payload(terminal_record, principal)
                )
            return jsonify(redis_generation_job_payload(task, principal))
        except RedisTaskNotFound:
            return jsonify({"error": "任务不存在"}), 404
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except billing.BillingError as exc:
            app.logger.exception(
                "Generation settlement failed for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "任务已结束，但积分结算需要重试。",
                        "code": "generation_settlement_pending",
                        "reason": exc.code,
                    }
                ),
                503,
            )
        except RedisQueueError as exc:
            return generation_queue_error_response(RuntimeError(str(exc)))
    refresh_generation_timeouts()
    job = generation_queue.get(normalized_job_id)
    if job is None or not generation_job_owned_by(job, principal):
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(generation_job_payload(job))


@app.get("/api/generation-jobs/<job_id>/manifest")
def api_generation_job_manifest(job_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if postgres_product_runtime_enabled():
        try:
            result_document, _contract = (
                load_postgres_generation_manifest_context(
                    normalized_job_id,
                    principal,
                )
            )
            return jsonify(public_plan_payload(result_document))
        except RedisTaskNotFound:
            return jsonify({"error": "生成结果不存在"}), 404
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except (
            FenceMismatch,
            JobStateConflict,
            SettlementConflict,
            WalletIntegrityError,
        ) as exc:
            app.logger.exception(
                "PostgreSQL generation manifest settlement pending for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "生成结果已完成，但积分结算需要重试。",
                        "code": "generation_settlement_pending",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL generation manifest unavailable for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "生成结果暂时不可用",
                        "code": "postgres_generation_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
    try:
        return jsonify(
            load_generation_result_manifest(normalized_job_id, principal)
        )
    except MenuUploadError as stored_error:
        try:
            redis_product_queue = product_redis_queue()
            if redis_product_queue is None:
                raise stored_error
            task, terminal_record = product_redis_task_or_terminal_record(
                redis_product_queue,
                normalized_job_id,
                principal,
            )
            if task is None:
                assert terminal_record is not None
                persisted = persisted_generation_job_payload(
                    terminal_record,
                    principal,
                )
                if (
                    persisted.get("status") == "completed"
                    and isinstance(persisted.get("result"), dict)
                ):
                    return jsonify(persisted["result"])
                return (
                    jsonify(
                        {
                            "error": "生成结果尚未准备完成",
                            "code": "generation_manifest_not_ready",
                        }
                    ),
                    425,
                )
            payload = redis_generation_job_payload(task, principal)
            if (
                payload.get("status") != "completed"
                or not isinstance(payload.get("result"), dict)
            ):
                return (
                    jsonify(
                        {
                            "error": "生成结果尚未准备完成",
                            "code": "generation_manifest_not_ready",
                        }
                    ),
                    425,
                )
            return jsonify(payload["result"])
        except RedisTaskNotFound:
            return jsonify({"error": "生成结果不存在"}), 404
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except billing.BillingError as exc:
            app.logger.exception(
                "Generation manifest settlement failed for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "生成结果已完成，但积分结算需要重试。",
                        "code": "generation_settlement_pending",
                        "reason": exc.code,
                    }
                ),
                503,
            )
        except (RedisQueueError, ValueError, TypeError):
            return menu_upload_error_response(stored_error)


@app.get("/api/generation-jobs/<job_id>/assets/<asset_id>")
def api_generation_job_asset(job_id: str, asset_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        if postgres_product_runtime_enabled():
            result_document, _contract = (
                load_postgres_generation_manifest_context(
                    normalized_job_id,
                    principal,
                )
            )
        else:
            result_document = load_persisted_generation_manifest_document(
                normalized_job_id,
                principal,
            )
        object_key, raw = generation_delivery_asset(result_document, asset_id)
    except RedisTaskNotFound:
        return jsonify({"error": "生成图片不存在"}), 404
    except MenuUploadError as exc:
        return menu_upload_error_response(exc)
    except (ProductJobStoreError, PostgresRuntimeError) as exc:
        app.logger.exception(
            "PostgreSQL generation asset unavailable for %s",
            normalized_job_id,
        )
        return (
            jsonify(
                {
                    "error": "生成图片暂时不可用",
                    "code": "postgres_generation_unavailable",
                    "reason": type(exc).__name__,
                }
            ),
            503,
        )
    response = send_file(
        io.BytesIO(raw),
        mimetype=object_storage_service.content_type_for_key(object_key),
        download_name=Path(object_key).name,
    )
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@app.post("/api/generation-jobs/<job_id>/cancel")
def api_cancel_generation_job(job_id: str):
    principal, principal_error = generation_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if postgres_product_runtime_enabled():
        try:
            detail = postgres_owned_generation_detail(
                normalized_job_id,
                principal,
            )
            durable_status = str(detail.job.get("status") or "")
            if durable_status in {"succeeded", "failed", "canceled"}:
                existing = postgres_generation_job_payload(
                    normalized_job_id,
                    principal,
                )
                if existing["status"] == "canceled":
                    return jsonify(existing)
                return (
                    jsonify(
                        {
                            "error": "任务已结束，不能取消。",
                            "code": "generation_job_already_finished",
                            "job": existing,
                        }
                    ),
                    409,
                )

            try:
                redis_product_queue = product_redis_queue()
            except (RedisQueueError, ValueError, TypeError) as exc:
                if durable_status != "queued":
                    return generation_queue_error_response(
                        RuntimeError(str(exc))
                    )
                redis_product_queue = None
            task = None
            if redis_product_queue is not None:
                try:
                    task = redis_product_queue.get(normalized_job_id)
                except RedisTaskNotFound:
                    task = None
                if task is not None:
                    redis_batch_contract(task, principal)
                    redis_status = product_task_status(
                        str(task.get("status") or ""),
                        task.get("result")
                        if isinstance(task.get("result"), dict)
                        else {},
                    )
                    if redis_status in {
                        "completed",
                        "failed",
                        "canceled",
                    }:
                        existing = postgres_generation_job_payload(
                            normalized_job_id,
                            principal,
                        )
                        if redis_status == "canceled":
                            return jsonify(
                                {
                                    **existing,
                                    "status": "canceling",
                                    "cancelRequested": True,
                                    "reconciliationPending": True,
                                }
                            )
                        return (
                            jsonify(
                                {
                                    "error": "任务已结束，不能取消。",
                                    "code": "generation_job_already_finished",
                                    "job": existing,
                                }
                            ),
                            409,
                        )
                    redis_product_queue.request_cancel(normalized_job_id)
                elif durable_status == "running":
                    raise RedisQueueError(
                        "running PostgreSQL job has no Redis task"
                    )
            elif durable_status == "running":
                raise RedisQueueError(
                    "running PostgreSQL generation queue is unavailable"
                )

            with postgres_connection() as connection:
                ProductJobStore(connection).request_cancel(
                    job_id=normalized_job_id,
                    owner_user_id=str(principal["userId"]),
                )
            return jsonify(
                postgres_generation_job_payload(
                    normalized_job_id,
                    principal,
                )
            )
        except PostgresJobNotFound:
            return jsonify({"error": "任务不存在"}), 404
        except JobStateConflict:
            try:
                existing = postgres_generation_job_payload(
                    normalized_job_id,
                    principal,
                )
            except (PostgresJobNotFound, RedisTaskNotFound):
                return jsonify({"error": "任务不存在"}), 404
            return (
                jsonify(
                    {
                        "error": "任务已结束，不能取消。",
                        "code": "generation_job_already_finished",
                        "job": existing,
                    }
                ),
                409,
            )
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except (
            SettlementConflict,
            WalletIntegrityError,
            FenceMismatch,
        ) as exc:
            app.logger.exception(
                "PostgreSQL generation cancellation settlement pending for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "任务已取消，但自动退款需要重试。",
                        "code": "generation_cancel_refund_pending",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except (ProductJobStoreError, PostgresRuntimeError) as exc:
            app.logger.exception(
                "PostgreSQL generation cancellation unavailable for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "取消任务暂时不可用",
                        "code": "postgres_generation_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except RedisQueueError as exc:
            return generation_queue_error_response(RuntimeError(str(exc)))
    try:
        redis_product_queue = product_redis_queue()
    except (RedisQueueError, ValueError, TypeError) as exc:
        return generation_queue_error_response(RuntimeError(str(exc)))
    if redis_product_queue is not None:
        try:
            task, terminal_record = product_redis_task_or_terminal_record(
                redis_product_queue,
                normalized_job_id,
                principal,
            )
            if task is None:
                assert terminal_record is not None
                response = persisted_generation_job_payload(
                    terminal_record,
                    principal,
                )
                if response["status"] == "canceled":
                    return jsonify(response)
                return (
                    jsonify(
                        {
                            "error": "任务已结束，不能取消。",
                            "code": "generation_job_already_finished",
                            "job": response,
                        }
                    ),
                    409,
                )
            contract = redis_batch_contract(task, principal)
            current_status = product_task_status(
                str(task.get("status") or ""),
                task.get("result") if isinstance(task.get("result"), dict) else {},
            )
            if current_status in {"completed", "failed"}:
                response = redis_generation_job_payload(task, principal)
                return (
                    jsonify(
                        {
                            "error": "任务已结束，不能取消。",
                            "code": "generation_job_already_finished",
                            "job": response,
                        }
                    ),
                    409,
                )
            if current_status == "canceled":
                return jsonify(redis_generation_job_payload(task, principal))
            requested = redis_product_queue.request_cancel(normalized_job_id)
            response = redis_generation_job_payload(requested, principal)
            if response["status"] == "running":
                response["cancelRequested"] = True
            response["generationBatch"] = public_generation_batch_payload(
                contract,
                refunded_points=(
                    int(contract["billing"]["totalPoints"])
                    if response["status"] == "canceled"
                    else 0
                ),
            )
            return jsonify(response)
        except RedisTaskNotFound:
            return jsonify({"error": "任务不存在"}), 404
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except billing.BillingError as exc:
            app.logger.exception(
                "Generation cancellation settlement failed for %s",
                normalized_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "任务已取消，但自动退款需要人工核对",
                        "code": "generation_cancel_refund_pending",
                        "reason": exc.code,
                    }
                ),
                503,
            )
        except RedisQueueError as exc:
            return generation_queue_error_response(RuntimeError(str(exc)))

    refresh_generation_timeouts()
    job = generation_queue.get(normalized_job_id)
    if job is None or not generation_job_owned_by(job, principal):
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
        canceled = job
    else:
        try:
            canceled = generation_queue.cancel(normalized_job_id, error="user canceled")
        except KeyError:
            return jsonify({"error": "任务不存在"}), 404

    metadata = canceled.metadata or {}
    contract = metadata.get("batchContract")
    if isinstance(contract, dict):
        try:
            refund_generation_batch(
                contract,
                points=int(contract["billing"]["totalPoints"]),
                reason="user_canceled",
            )
            try:
                update_persisted_generation_job(
                    normalized_job_id,
                    status="canceled",
                    error_message="user_canceled",
                )
            except ValueError:
                pass
        except billing.BillingError as exc:
            app.logger.exception("Generation cancellation refund failed for %s", normalized_job_id)
            body, _status = billing.billing_error_response(exc)
            return (
                jsonify(
                    {
                        **body,
                        "error": "任务已取消，但自动退款需要人工核对",
                        "code": "generation_cancel_refund_pending",
                    }
                ),
                503,
            )
        response = generation_job_payload(canceled)
        response["generationBatch"] = public_generation_batch_payload(
            contract,
            refunded_points=int(contract["billing"]["totalPoints"]),
        )
        response["account"] = account_payload(str(principal["userId"]))
        return jsonify(response)
    return jsonify(generation_job_payload(canceled))


@app.post("/api/generate-final")
def api_generate_final():
    payload = request.get_json(silent=True) or {}
    try:
        style = validate_requested_style(str(payload.get("style") or ""))
        selected_background = requested_selected_background(style, payload)
    except SelectedBackgroundError as exc:
        return selected_background_error_response(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    quality = str(payload.get("quality") or "standard")
    if not tencent_ready() and not local_final_fallback_enabled():
        return jsonify({"error": "混元未配置，已停止正式出图，避免生成错误图片或误扣积分。"}), 503
    if tencent_ready() and not generation_write_authorized() and not local_demo_generation_allowed():
        return forbidden("正式生成接口未授权", "generation_write_forbidden")
    plan = build_plan(style, quality, selected_background)
    plan["generation"] = materialize_final_images(
        plan,
        style,
        quality,
        selected_background,
    )
    return jsonify(public_plan_payload(plan))


@app.post("/api/upload-menu")
def upload_menu():
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
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
    try:
        menu_upload_id = persist_menu_upload(
            target,
            original_filename=str(file.filename or target.name),
            content_type=str(file.mimetype or ""),
            menu=menu,
            owner_user_id=str(principal["userId"]),
        )
    except Exception as exc:
        target.unlink(missing_ok=True)
        return jsonify(
            {
                "error": "菜单文件存储失败，请检查对象存储配置",
                "code": "menu_object_storage_failed",
                "detail": type(exc).__name__,
            }
        ), 503
    return jsonify(
        {
            "ok": True,
            "file": target.name,
            "menuUploadId": menu_upload_id,
            "menu": {k: v for k, v in menu.items() if k != "items"},
        }
    )


@app.post("/api/upload-library")
def upload_library():
    if postgres_product_runtime_enabled():
        if not admin_write_authorized():
            return forbidden(
                "图库导入需要管理员权限",
                "admin_write_forbidden",
            )
        idempotency_key, idempotency_error = (
            product_idempotency_key_required()
        )
        if idempotency_error is not None:
            return idempotency_error
        readiness = (
            object_storage_service.assess_object_storage_readiness()
        )
        if (
            not bool(readiness.get("ready"))
            or str(readiness.get("mode") or "") != "remote_private"
        ):
            return jsonify(
                {
                    "error": "正式图库导入需要可用的远程私有对象存储",
                    "code": "library_object_storage_unavailable",
                    "blockingIssues": list(
                        readiness.get("blockingIssues") or []
                    ),
                }
            ), 503
        try:
            spec = library_import_spec()
            images = read_library_import_zip(request.files.get("file"))
            result = persist_postgres_library_import(
                images=images,
                spec=spec,
                actor_user_id=admin_actor_user_id(),
                idempotency_key=idempotency_key,
            )
        except LibraryImportRequestError as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": exc.code,
                }
            ), exc.status
        except (
            product_asset_library_store.InvalidProductAssetInput,
            product_library_import_store.InvalidProductLibraryImport,
        ) as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "invalid_library_import",
                }
            ), 400
        except (
            product_asset_library_store.ProductAssetConflict,
            product_library_import_store.ProductLibraryImportConflict,
        ) as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "code": "library_import_conflict",
                }
            ), 409
        except Exception as exc:
            app.logger.exception("PostgreSQL library import unavailable")
            return jsonify(
                {
                    "error": "图库图片存储失败，请检查数据库和对象存储配置",
                    "code": "library_object_storage_failed",
                    "detail": type(exc).__name__,
                }
            ), 503
        return jsonify({"ok": True, **result})
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "没有收到图库 zip"}), 400
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "请上传 zip 文件"}), 400
    batch = LIBRARY_DIR / f"uploaded_{int(time.time())}_{time.time_ns() % 1_000_000_000:09d}"
    batch.mkdir(parents=True, exist_ok=True)
    persisted_image_ids: list[str] = []
    raw = io.BytesIO(file.read())
    try:
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
                persisted_image_ids.append(
                    persist_uploaded_library_image(
                        target,
                        upload_batch=batch.name,
                        original_member=member.filename,
                        style_id="style-upload",
                    )
                )
    except zipfile.BadZipFile:
        shutil.rmtree(batch, ignore_errors=True)
        return jsonify({"error": "图库 zip 文件无法读取", "code": "invalid_library_zip"}), 400
    except Exception as exc:
        shutil.rmtree(batch, ignore_errors=True)
        return jsonify(
            {
                "error": "图库图片存储失败，请检查对象存储配置",
                "code": "library_object_storage_failed",
                "detail": type(exc).__name__,
            }
        ), 503
    library_images.cache_clear()
    return jsonify(
        {
            "ok": True,
            "uploadedImageCount": len(persisted_image_ids),
            "libraryImageIds": persisted_image_ids,
            "plan": public_plan_payload(build_plan()),
        }
    )


@app.post("/api/export")
def api_export():
    payload = request.get_json(silent=True) or {}
    selected_rows = payload.get("selectedRows") or []
    if not isinstance(selected_rows, list):
        selected_rows = []
    selected_rows = sorted(
        {int(x) for x in selected_rows if str(x).isdigit()}
    )
    scope = str(payload.get("scope", "all"))
    image_format = str(payload.get("format", "jpg")).lower()
    requested_job_id = str(
        payload.get("jobId") or payload.get("generationJobId") or ""
    ).strip()
    platforms: list[str] | str | None = payload.get("platforms") or ["meituan"]
    watermark = (
        payload.get("watermark")
        if isinstance(payload.get("watermark"), dict)
        else None
    )
    generation_context: dict[str, Any] | None = None
    export_already_persisted = False

    if requested_job_id:
        principal, principal_error = generation_request_principal()
        if principal_error is not None:
            return principal_error
        assert principal is not None
        try:
            normalized_job_id = validate_generation_job_id(requested_job_id)
            if postgres_product_runtime_enabled():
                result_document, contract, primary_manifest = (
                    load_postgres_generation_export_context(
                        normalized_job_id,
                        principal,
                    )
                )
            else:
                result_document, contract = (
                    load_persisted_generation_manifest_context(
                        normalized_job_id,
                        principal,
                    )
                )
            platforms = generation_contract_export_platforms(
                contract,
                payload.get("platforms"),
            )
            watermark = generation_contract_export_watermark(contract)
            staging_dir = (
                EXPORT_DIR
                / "_generation_sources"
                / f"{normalized_job_id}_{secrets.token_hex(8)}"
            )
            try:
                export_results = generation_manifest_export_results(
                    result_document,
                    staging_dir,
                )
                revision_manifests: list[dict[str, Any]] = []
                apply_revision_export_overrides(
                    export_results,
                    parent_generation_job_id=normalized_job_id,
                    revision_job_ids=payload.get("revisionJobIds"),
                    principal=principal,
                    staging_dir=staging_dir,
                    source_manifests=revision_manifests,
                )
                if postgres_product_runtime_enabled():
                    assert isinstance(platforms, list)
                    generation_context = (
                        postgres_export_generation_context(
                            contract,
                            principal,
                            primary_manifest=primary_manifest,
                            revision_manifests=revision_manifests,
                            scope=scope,
                            selected_rows=selected_rows,
                            image_format=image_format,
                            platforms=platforms,
                            watermark=watermark,
                        )
                    )
                    selected_set = set(selected_rows)
                    selected_count = sum(
                        1
                        for row_number, _row in enumerate(
                            export_results,
                            start=1,
                        )
                        if scope != "selected"
                        or row_number in selected_set
                    )
                    replay_payload = postgres_existing_export_payload(
                        generation_context,
                        {
                            "rows": selected_count * len(platforms),
                            "images": selected_count * len(platforms),
                            "platforms": platforms,
                            "watermark": bool(
                                isinstance(watermark, dict)
                                and watermark.get("enabled")
                            ),
                        },
                    )
                    if replay_payload is not None:
                        export_payload = replay_payload
                        export_already_persisted = True
                    else:
                        export_payload = export_delivery_zip(
                            export_results,
                            EXPORT_DIR,
                            scope=scope,
                            selected_rows=selected_rows,
                            image_format=image_format,
                            watermark=watermark,
                            platforms=platforms,
                        )
                else:
                    export_payload = export_delivery_zip(
                        export_results,
                        EXPORT_DIR,
                        scope=scope,
                        selected_rows=selected_rows,
                        image_format=image_format,
                        watermark=watermark,
                        platforms=platforms,
                    )
            finally:
                shutil.rmtree(staging_dir, ignore_errors=True)
        except MenuUploadError as exc:
            return menu_upload_error_response(exc)
        except RedisTaskNotFound:
            return jsonify({"error": "生成结果不存在"}), 404
        except BatchContractError as exc:
            return batch_contract_error_response(exc)
        except product_export_store.ProductExportConflict:
            return (
                jsonify(
                    {
                        "error": "该导出请求编号已用于其他导出条件",
                        "code": "export_idempotency_conflict",
                    }
                ),
                409,
            )
        except (
            ProductJobStoreError,
            PostgresRuntimeError,
            product_export_store.ProductExportStoreError,
        ) as exc:
            app.logger.exception(
                "PostgreSQL generation export unavailable for %s",
                requested_job_id,
            )
            return (
                jsonify(
                    {
                        "error": "生成结果暂时不可用",
                        "code": "postgres_generation_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        if product_redis_required():
            return (
                jsonify(
                    {
                        "error": "正式任务编号缺失，不能从临时磁盘导出",
                        "code": "generation_job_id_required",
                    }
                ),
                400,
            )
        quality = str(payload.get("quality", "standard"))
        try:
            style = validate_requested_style(
                str(payload.get("style", "")),
                allow_empty=True,
            )
            selected_background = (
                requested_selected_background(style, payload)
                if style
                else None
            )
        except SelectedBackgroundError as exc:
            return selected_background_error_response(exc)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        plan = build_plan(style, quality, selected_background)
        export_results = prepare_results_for_export(
            plan["results"],
            style,
            selected_background,
        )
        export_payload = export_delivery_zip(
            export_results,
            EXPORT_DIR,
            scope=scope,
            selected_rows=selected_rows,
            image_format=image_format,
            watermark=watermark,
            platforms=platforms,
        )
    if not export_already_persisted:
        try:
            export_payload = object_storage_export_payload(
                export_payload,
                scope=scope,
                platforms=platforms,
                generation_context=generation_context,
            )
        except Exception as exc:
            return jsonify(
                {
                    "error": "导出包存储失败，请检查对象存储配置",
                    "code": "export_object_storage_failed",
                    "detail": type(exc).__name__,
                }
            ), 503
    return jsonify(public_export_payload(export_payload))


@app.get("/media/<path:name>")
def media(name: str):
    target = safe_library_media_path(
        name,
        allowed_prefixes=PUBLIC_MEDIA_PREFIXES,
    )
    if target is None and local_demo_auth_allowed():
        target = safe_library_media_path(
            name,
            allowed_prefixes=PRIVATE_MEDIA_PREFIXES,
        )
    if target is None:
        return jsonify({"error": "图片不存在"}), 404
    return send_file(target)


@app.get("/api/private-media/<path:name>")
def private_media(name: str):
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    target = safe_library_media_path(
        name,
        allowed_prefixes=PRIVATE_MEDIA_PREFIXES,
        require_file=False,
    )
    if target is None:
        return jsonify({"error": "图片不存在"}), 404

    principal_user_id = str(principal["userId"])
    secret = object_access_signing_secret()
    token = str(request.args.get("token") or "")
    token_payload: dict[str, Any] = {}
    if not (principal.get("localDemo") and not secret):
        if not secret:
            return (
                jsonify(
                    {
                        "error": "对象签名密钥未配置",
                        "code": "object_signing_secret_missing",
                    }
                ),
                503,
            )
        context = private_media_access_context(name)
        decision = download_guard.authorize_download(
            asset_record={
                "id": context["assetId"],
                "owner_user_id": principal_user_id,
                "order_id": context["orderId"],
                "allowed_purposes": [asset_security.PREVIEW],
                "allowed_variants": [asset_security.PREVIEW],
            },
            user_context={"id": principal_user_id},
            order_context={"id": context["orderId"]},
            purpose=asset_security.PREVIEW,
            variant=asset_security.PREVIEW,
            token=token,
            secret=secret,
        )
        if not bool(decision.get("allowed")):
            return (
                jsonify(
                    {
                        "error": "私有图片访问未授权",
                        "code": "private_media_forbidden",
                        "reason": decision.get("reason"),
                    }
                ),
                403,
            )
        try:
            token_payload = asset_security.verify_asset_url_token(
                token,
                secret,
            )
        except asset_security.AssetTokenError:
            return (
                jsonify(
                    {
                        "error": "私有图片访问未授权",
                        "code": "private_media_forbidden",
                        "reason": "invalid_token",
                    }
                ),
                403,
            )
    if Path(name).parts[0] in DURABLE_PRIVATE_PREVIEW_PREFIXES:
        menu_upload_id = str(
            token_payload.get("menu_upload_id") or ""
        ).strip()
        try:
            with customer_preview_menu_path(
                None,
                principal,
                menu_upload_id,
            ):
                available = ensure_private_preview_asset(target)
        except PreviewObjectStorageError as exc:
            return preview_object_storage_error_response(exc)
        if not available:
            return jsonify({"error": "图片不存在"}), 404
    if not target.is_file():
        return jsonify({"error": "图片不存在"}), 404
    response = send_file(target)
    response.headers["Cache-Control"] = (
        f"private, max-age={asset_security.PREVIEW_TOKEN_TTL_SECONDS}"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/external-media/<path:name>")
def external_media(name: str):
    path = external_image_path(name)
    if path is None:
        return jsonify({"error": "图片不存在或未授权"}), 404
    return send_file(path)


@app.get("/model-inputs/<path:name>")
def model_inputs(name: str):
    if runtime_environment_label() in {"staging", "production", "prod", "render"}:
        return jsonify({"error": "图片不存在"}), 404
    if not re.fullmatch(r"[a-f0-9]{24}\.jpg", name):
        return jsonify({"error": "图片不存在"}), 404
    return send_from_directory(MODEL_INPUT_DIR, name)


def record_legacy_object_route_audit(**kwargs: Any) -> None:
    if postgres_product_runtime_enabled():
        return
    record_asset_access_audit(**kwargs)


@app.get("/objects/<path:object_key>")
def object_access(object_key: str):
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    principal_user_id = str(principal["userId"])

    secret = object_access_signing_secret()
    if not secret:
        record_legacy_object_route_audit(
            asset_id=object_key,
            action="object_access",
            user_id=principal_user_id,
            asset_type="object",
            allowed=False,
            deny_reason="missing_secret",
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象签名密钥未配置", "code": "object_signing_secret_missing"}), 503

    token = str(request.args.get("token") or "")
    if not token:
        record_legacy_object_route_audit(
            asset_id=object_key,
            action="object_access",
            user_id=principal_user_id,
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
        record_legacy_object_route_audit(
            asset_id=object_key,
            action="object_access",
            user_id=principal_user_id,
            asset_type="object",
            allowed=False,
            deny_reason=reason,
            metadata={"route": "objects"},
        )
        return jsonify({"error": "对象访问未授权", "code": "object_access_forbidden", "reason": reason}), 403

    if payload["object_key"] != requested_key:
        record_legacy_object_route_audit(
            asset_id=requested_key,
            action=asset_action_for_purpose(str(payload.get("purpose") or ""), "object_access"),
            user_id=str(payload.get("user_id") or ""),
            asset_type="object",
            allowed=False,
            deny_reason="object_key_mismatch",
            metadata={"route": "objects", "tokenObjectKey": payload["object_key"]},
        )
        return jsonify({"error": "对象访问未授权", "code": "object_access_forbidden", "reason": "object_key_mismatch"}), 403

    purpose = str(payload.get("purpose") or "")
    variant = str(payload.get("variant") or "")
    durable_export_access = (
        purpose == asset_security.EXPORT
        and postgres_product_runtime_enabled()
    )
    export_nonce_consumer: PostgresExportNonceConsumer | None = None
    export_nonce_status = ""
    if durable_export_access:
        try:
            export_nonce_consumer = postgres_export_nonce_consumer(
                payload=payload,
                requested_key=requested_key,
                owner_user_id=principal_user_id,
                digest_secret=secret,
            )
            nonce_consumer: download_guard.NonceConsumer | None = (
                export_nonce_consumer
            )
            export_nonce_status = (
                export_nonce_consumer.reserve_for_download()
            )
        except (
            product_export_store.InvalidProductExportInput,
            product_export_store.ProductExportNotFound,
        ):
            return (
                jsonify(
                    {
                        "error": "对象访问未授权",
                        "code": "object_access_forbidden",
                        "reason": "export_package_mismatch",
                    }
                ),
                403,
            )
        except (
            product_export_store.ProductExportStoreError,
            PostgresRuntimeError,
        ) as exc:
            app.logger.exception("PostgreSQL export access unavailable")
            return (
                jsonify(
                    {
                        "error": "对象访问校验暂时不可用",
                        "code": "export_access_store_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        except Exception as exc:
            app.logger.exception("PostgreSQL export access unavailable")
            return (
                jsonify(
                    {
                        "error": "对象访问校验暂时不可用",
                        "code": "export_access_store_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
    else:
        try:
            nonce_consumer = asset_nonce_consumer()
        except (RedisQueueError, ValueError, TypeError) as exc:
            app.logger.exception("Asset nonce consumer unavailable")
            return (
                jsonify(
                    {
                        "error": "对象访问校验暂时不可用",
                        "code": "asset_nonce_consumer_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
    if (
        export_nonce_consumer is not None
        and export_nonce_status == EXPORT_ACCESS_REQUEST_REJECTED
    ):
        reason = (
            export_nonce_consumer.previous_denial_reason
            or EXPORT_ACCESS_REQUEST_REJECTED
        )
        if reason == download_guard.REASON_NONCE_CONSUMER_ERROR:
            return (
                jsonify(
                    {
                        "error": "对象访问校验暂时不可用",
                        "code": "export_access_store_unavailable",
                        "reason": reason,
                    }
                ),
                503,
            )
        return (
            jsonify(
                {
                    "error": "该导出请求已被拒绝",
                    "code": "export_request_already_denied",
                    "reason": reason,
                }
            ),
            409,
        )
    if (
        export_nonce_consumer is not None
        and export_nonce_status
        in {
            product_export_store.ACCESS_TOKEN_IN_USE,
            product_export_store.ACCESS_EXPORT_NOT_READY,
        }
    ):
        reason = export_nonce_status
        try:
            export_nonce_consumer.record_preflight_denial(reason)
        except Exception:
            app.logger.exception(
                "PostgreSQL export reservation denial audit unavailable"
            )
        return (
            jsonify(
                {
                    "error": "导出链接正在被使用，请勿并发下载",
                    "code": "export_token_in_use",
                    "reason": reason,
                }
            ),
            409,
        )
    storage = object_storage_service.get_object_storage_service()
    verified_export_path: Path | None = None
    if (
        export_nonce_consumer is not None
        and export_nonce_status
        == product_export_store.NONCE_RESERVED
    ):
        try:
            verified_export_path = verified_object_file(
                storage,
                requested_key,
                expected_sha256=export_nonce_consumer.expected_sha256,
                expected_size=export_nonce_consumer.expected_size,
            )
        except Exception as exc:
            missing = object_storage_service.is_object_not_found_error(exc)
            reason = (
                "object_not_found"
                if missing
                else "object_integrity_mismatch"
                if isinstance(exc, ValueError)
                else "object_storage_unavailable"
            )
            try:
                export_nonce_consumer.release_reservation()
            except Exception:
                app.logger.exception(
                    "PostgreSQL export reservation release unavailable"
                )
            try:
                export_nonce_consumer.record_preflight_denial(reason)
            except Exception:
                app.logger.exception(
                    "PostgreSQL export storage denial audit unavailable"
                )
            if missing:
                return (
                    jsonify(
                        {
                            "error": "对象不存在",
                            "code": "object_not_found",
                        }
                    ),
                    404,
                )
            if isinstance(exc, ValueError):
                return (
                    jsonify(
                        {
                            "error": "导出包完整性校验失败",
                            "code": "export_object_integrity_mismatch",
                        }
                    ),
                    409,
                )
            app.logger.exception("Export object storage unavailable")
            return (
                jsonify(
                    {
                        "error": "对象存储暂时不可用",
                        "code": "object_storage_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )

    decision = download_guard.authorize_download(
        asset_record={
            "asset_id": requested_key,
            "user_id": principal_user_id,
            "allowed_purposes": (purpose,),
            "available_variants": (variant,),
        },
        user_context={"user_id": principal_user_id},
        purpose=purpose,
        variant=variant,
        token=token,
        secret=secret,
        nonce_consumer=nonce_consumer,
        audit_metadata={"route": "objects"},
    )
    if not decision["allowed"]:
        if verified_export_path is not None:
            verified_export_path.unlink(missing_ok=True)
        if export_nonce_consumer is not None:
            try:
                export_nonce_consumer.finalize_denial(
                    str(decision["reason"])
                )
            except (
                product_export_store.ProductExportStoreError,
                PostgresRuntimeError,
            ):
                app.logger.exception(
                    "PostgreSQL export denial audit unavailable"
                )
        else:
            record_legacy_object_route_audit(
                asset_id=requested_key,
                action=str(
                    decision.get("action")
                    or asset_action_for_purpose(
                        purpose,
                        "object_access",
                    )
                ),
                user_id=principal_user_id,
                asset_type="object",
                allowed=False,
                deny_reason=str(decision["reason"]),
                metadata=(
                    decision.get("audit")
                    if isinstance(decision.get("audit"), dict)
                    else {"route": "objects"}
                ),
            )
        if (
            durable_export_access
            and decision["reason"]
            == download_guard.REASON_NONCE_CONSUMER_ERROR
        ):
            return (
                jsonify(
                    {
                        "error": "对象访问校验暂时不可用",
                        "code": "export_access_store_unavailable",
                        "reason": decision["reason"],
                    }
                ),
                503,
            )
        return (
            jsonify(
                {
                    "error": "对象访问未授权",
                    "code": "object_access_forbidden",
                    "reason": decision["reason"],
                }
            ),
            403,
        )

    if (
        export_nonce_consumer is not None
        and verified_export_path is None
    ):
        app.logger.error(
            "Durable export was authorized without verified object bytes"
        )
        return (
            jsonify(
                {
                    "error": "导出包校验状态异常",
                    "code": "export_object_preflight_required",
                }
            ),
            503,
        )
    if verified_export_path is None:
        try:
            payload_bytes = object_storage_service.read_object_bytes_limited(
                storage,
                requested_key,
                (
                    export_package_size_limit()
                    if purpose == asset_security.EXPORT
                    else MAX_AI_ASSET_BYTES
                ),
            )
        except object_storage_service.ObjectStorageReadLimitExceeded:
            record_legacy_object_route_audit(
                asset_id=requested_key,
                action=asset_action_for_purpose(
                    purpose,
                    "object_access",
                ),
                user_id=principal_user_id,
                asset_type="object",
                allowed=False,
                deny_reason="object_too_large",
                metadata={"route": "objects"},
            )
            return (
                jsonify(
                    {
                        "error": "对象超过大小限制",
                        "code": "object_too_large",
                    }
                ),
                413,
            )
        except Exception as exc:
            if not object_storage_service.is_object_not_found_error(exc):
                app.logger.exception("Object storage read unavailable")
                return (
                    jsonify(
                        {
                            "error": "对象存储暂时不可用",
                            "code": "object_storage_unavailable",
                            "reason": type(exc).__name__,
                        }
                    ),
                    503,
                )
            record_legacy_object_route_audit(
                asset_id=requested_key,
                action=asset_action_for_purpose(
                    purpose,
                    "object_access",
                ),
                user_id=principal_user_id,
                asset_type="object",
                allowed=False,
                deny_reason="object_not_found",
                metadata={"route": "objects"},
            )
            return (
                jsonify(
                    {"error": "对象不存在", "code": "object_not_found"}
                ),
                404,
            )
        record_legacy_object_route_audit(
            asset_id=requested_key,
            action=asset_action_for_purpose(
                purpose,
                "object_access",
            ),
            user_id=principal_user_id,
            asset_type="object",
            allowed=True,
            metadata={
                "route": "objects",
                "purpose": purpose,
                "variant": variant,
                "tokenConsumptionStatus": (
                    decision.get("audit", {}).get(
                        "token_consumption_status"
                    )
                    if isinstance(decision.get("audit"), dict)
                    else None
                ),
            },
        )
        response = send_file(
            io.BytesIO(payload_bytes),
            mimetype=object_storage_service.content_type_for_key(
                requested_key
            ),
            download_name=Path(requested_key).name,
        )
    else:
        cleanup_path = verified_export_path

        @after_this_request
        def cleanup_verified_export(response: Response) -> Response:
            cleanup_path.unlink(missing_ok=True)
            return response

        response = send_file(
            verified_export_path,
            mimetype=object_storage_service.content_type_for_key(
                requested_key
            ),
            download_name=Path(requested_key).name,
        )
    response.headers["Cache-Control"] = (
        "private, no-store, max-age=0"
        if asset_security.requires_one_time_consumption(purpose)
        else f"private, max-age={asset_security.PREVIEW_TOKEN_TTL_SECONDS}"
    )
    return response


@app.get("/download/<path:name>")
def download(name: str):
    if postgres_product_runtime_enabled():
        return jsonify(
            {
                "error": "该下载地址已停用，请使用私有对象下载地址",
                "code": "durable_object_download_required",
            }
        ), 410
    principal, principal_error = customer_request_principal()
    if principal_error is not None:
        return principal_error
    assert principal is not None
    principal_user_id = str(principal["userId"])

    relative_name = safe_export_download_name(name)
    if relative_name is None:
        record_asset_access_audit(
            asset_id=str(name or ""),
            action="export",
            user_id=principal_user_id,
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
            user_id=principal_user_id,
            asset_type="export",
            allowed=False,
            deny_reason="missing_secret",
            metadata={"route": "download"},
        )
        return jsonify({"error": "下载签名密钥未配置", "code": "download_signing_secret_missing"}), 503
    if signing_secret:
        try:
            nonce_consumer = asset_nonce_consumer()
        except (RedisQueueError, ValueError, TypeError) as exc:
            app.logger.exception("Download nonce consumer unavailable")
            return (
                jsonify(
                    {
                        "error": "下载校验暂时不可用",
                        "code": "asset_nonce_consumer_unavailable",
                        "reason": type(exc).__name__,
                    }
                ),
                503,
            )
        decision = download_guard.authorize_download(
            asset_record={
                "asset_id": relative_name,
                "user_id": principal_user_id,
                "allowed_purposes": (asset_security.EXPORT,),
                "available_variants": (asset_security.EXPORT,),
            },
            user_context={"user_id": principal_user_id},
            purpose=asset_security.EXPORT,
            variant=asset_security.EXPORT,
            token=request.args.get("token"),
            secret=signing_secret,
            audit_metadata={"route": "download"},
            nonce_consumer=nonce_consumer,
        )
        if not decision["allowed"]:
            record_asset_access_audit(
                asset_id=relative_name,
                action=str(decision.get("action") or "export"),
                user_id=principal_user_id,
                asset_type="export",
                allowed=False,
                deny_reason=str(decision["reason"]),
                metadata=decision.get("audit") if isinstance(decision.get("audit"), dict) else {"route": "download"},
            )
            return jsonify({"error": "下载未授权", "code": "download_forbidden", "reason": decision["reason"]}), 403
        record_asset_access_audit(
            asset_id=relative_name,
            action=str(decision.get("action") or "export"),
            user_id=principal_user_id,
            asset_type="export",
            allowed=True,
            metadata=decision.get("audit") if isinstance(decision.get("audit"), dict) else {"route": "download"},
        )
    else:
        record_asset_access_audit(
            asset_id=relative_name,
            action="export",
            user_id=principal_user_id,
            asset_type="export",
            allowed=True,
            metadata={"route": "download", "localDemo": True},
        )

    response = send_from_directory(
        EXPORT_DIR,
        relative_name,
        as_attachment=True,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


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
            request_authorizer=admin_panel_request_authorizer,
            dashboard_provider=product_admin_dashboard_provider,
            list_provider=product_admin_list_provider,
        )
    )
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port)
