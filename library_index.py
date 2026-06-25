from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from matching_engine import classify_kind as classify_match_kind
from matching_engine import normalize_dish as normalize_match_dish
from matching_engine import semantic_family as semantic_match_family

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CLEAN_DIR = Path("/Users/guiguixiaxia/Documents/cleanpic")
DEFAULT_WATERMARK_DIR = Path("/Users/guiguixiaxia/Documents/watermarkpic")
DEFAULT_INDEX_DIR = BASE_DIR / "data" / "library_index"
DEFAULT_INDEX_PATH = DEFAULT_INDEX_DIR / "library_index.jsonl"
DEFAULT_THUMB_DIR = DEFAULT_INDEX_DIR / "thumbs"
STYLE_IDS = ("style-1", "style-2", "style-3", "style-4", "style-5", "style-6")

IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".psd",
    ".avif",
    ".heic",
    ".heif",
}

COMBO_KEYWORDS = (
    "套餐",
    "组合",
    "双拼",
    "三拼",
    "四拼",
    "多拼",
    "混拼",
    "拼盘",
    "全家福",
    "自选",
    "任选",
    "搭配",
    "搭子",
    "加购",
    "套饭",
    "份餐",
    "分享",
    "combo",
    "set",
)

DRINK_KEYWORDS = (
    "饮品",
    "饮料",
    "可乐",
    "雪碧",
    "芬达",
    "美年达",
    "冰红茶",
    "绿茶",
    "红茶",
    "乌龙茶",
    "柠檬茶",
    "奶茶",
    "豆浆",
    "果汁",
    "椰汁",
    "橙汁",
    "酸梅汤",
    "矿泉水",
    "纯净水",
    "苏打水",
    "咖啡",
    "酸奶",
    "啤酒",
    "汽水",
    "茶饮",
)
SNACK_KEYWORDS = (
    "小吃",
    "小食",
    "甜品",
    "茶叶蛋",
    "卤蛋",
    "煎蛋",
    "锅贴",
    "汤圆",
    "凉菜",
    "卤味",
    "花生米",
    "糍粑",
    "酥肉",
)

PROMO_KEYWORDS = (
    "收藏",
    "关注",
    "门店",
    "福利",
    "宠粉",
    "免费",
    "发票",
    "好评",
    "到店",
    "公告",
)

RAW_KEYWORDS = ("生食", "需自行", "自行煮", "半成品", "冷冻")
LOW_REUSE_KEYWORDS = (
    "米饭",
    "白饭",
    "大米饭",
    "可乐",
    "雪碧",
    "王老吉",
    "矿泉水",
    "纯净水",
    "饮料",
    "饮品",
    "纸巾",
    "餐具",
    "打包",
    "调料",
    "蘸料",
)
PROMPT_IMAGE_KEYWORDS = (
    "勿点",
    "不要点",
    "下单",
    "提示",
    "背景",
    "公告",
    "模板",
    "示例",
    "占位",
    "测试",
)
TEXT_RISK_KEYWORDS = (
    "水印",
    "logo",
    "商标",
    "文字",
    "带字",
    "字样",
    "菜单",
    "海报",
    "价目",
    "价格",
    "电话",
    "热线",
    "扫码",
    "二维码",
    "活动",
    "优惠",
    "满减",
    "立减",
    "特价",
    "买一送一",
    "电子餐饮",
    "发票",
    "关注",
    "收藏",
    "好评",
    "公告",
    "提示",
    "勿点",
)
DISH_TEXT_WATERMARK_KEYWORDS = (
    "菜品名",
    "菜名",
    "品名",
    "文字水印",
    "菜品水印",
    "带字",
    "字样",
    "水印",
)
BRAND_REVIEW_KEYWORDS = (
    "可口可乐",
    "百事",
    "王老吉",
    "美团",
    "饿了么",
    "京东",
    "淘宝",
    "抖音",
    "微信",
    "支付宝",
    "logo",
    "商标",
)
ACTIVITY_REVIEW_KEYWORDS = (
    "活动",
    "优惠",
    "满减",
    "立减",
    "特价",
    "买一送一",
    "第二份",
    "赠",
    "免费",
    "福利",
    "秒杀",
    "团购",
    "折扣",
    "爆款",
    "新品",
)
STAPLE_KEYWORDS = (
    "米饭",
    "白饭",
    "大米饭",
    "一碗米饭",
    "加饭",
)
SIDE_ADDON_KEYWORDS = (
    "小料",
    "调料",
    "蘸料",
    "酱料",
    "辣椒包",
    "醋包",
    "生抽包",
    "陈醋",
    "白糖",
    "蒜粒",
    "大蒜头",
    "香菜沫",
    "腊八蒜",
    "餐具",
    "纸巾",
    "打包",
)
GENERIC_IMAGE_KEYWORDS = PROMPT_IMAGE_KEYWORDS + (
    "门店",
    "欢迎到店",
    "更安全",
    "新鲜食材",
    "收藏",
    "福利",
)
LOW_QUALITY_NAME_KEYWORDS = (
    "低清",
    "低质",
    "模糊",
    "糊图",
    "截图",
    "小图",
    "压缩",
    "临时",
)
LOW_QUALITY_SCORE_THRESHOLD = 0.7
LOW_RESOLUTION_EDGE = 240
LOW_RESOLUTION_AREA = 320 * 320
_WORD_RE = re.compile(r"[\u4e00-\u9fff0-9a-z]+")
_PLUS_RE = re.compile(r"(\+|＋|加|配|搭)")
_PHONE_RE = re.compile(r"(?:1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8}|400[- ]?\d{3}[- ]?\d{4})")


@dataclass
class ScanResult:
    records: list[dict[str, Any]]
    errors: list[dict[str, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    roots: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.records)

    def summary(self) -> dict[str, Any]:
        source_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        for record in self.records:
            source = source_bucket(str(record.get("source") or "unknown"))
            source_counts[source] = source_counts.get(source, 0) + 1
            for tag in record.get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        cleaning = cleaning_summary(self.records)
        sha1 = sha1_summary(self.records)
        match_categories = match_category_summary(self.records)
        return {
            "total": self.total,
            "clean": source_counts.get("clean", 0),
            "watermark": source_counts.get("watermark", 0),
            "singleImages": match_categories.get("single", 0),
            "packageImages": match_categories.get("package", 0),
            "snackDrinkImages": match_categories.get("beverage", 0) + match_categories.get("snack", 0),
            "formalImages": match_categories.get("formal", 0),
            "reusable": cleaning["reusable"],
            "referenceOnly": cleaning["referenceOnly"],
            "sha1Deduped": sha1["unique"],
            "sha1Duplicates": sha1["duplicates"],
            "sources": source_counts,
            "tags": tag_counts,
            "matchCategories": match_categories,
            "sha1": sha1,
            "cleaning": cleaning,
            "errors": len(self.errors),
            "elapsedSeconds": round(self.elapsed_seconds, 3),
        }


def normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(_WORD_RE.findall(normalized))


def match_category_for(dish: str, norm: str, tags: Iterable[str] | None = None) -> str:
    tag_set = set(tags or [])
    family = semantic_match_family(dish, norm)
    kind = classify_match_kind(dish)
    if kind == "套餐/组合" or family == "combo" or "package" in tag_set or "combo" in tag_set:
        return "package"
    if family == "beverage" or "drink" in tag_set:
        return "beverage"
    if family == "plain_rice" or "staple" in tag_set:
        return "staple"
    if family == "addon" or "side_addon" in tag_set:
        return "addon"
    if family in {"soup"} or "snack" in tag_set:
        return "snack"
    if family == "service":
        return "other"
    return "single"


def source_bucket(source: str) -> str:
    normalized = normalize(source)
    if "watermark" in normalized or "watermarkpic" in normalized:
        return "watermark"
    if "clean" in normalized or "cleanpic" in normalized:
        return "clean"
    return source or "unknown"


def sha1_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def style_id_for_item(store: str, dish: str, digest: str = "") -> str:
    seed = f"{store}:{dish}:{digest[:12]}"
    bucket = hashlib.sha1(seed.encode("utf-8")).digest()[0] % len(STYLE_IDS)
    return STYLE_IDS[bucket]


def image_roots(
    clean_dir: str | Path | None = None,
    watermark_dir: str | Path | None = None,
    roots: Mapping[str, str | Path] | None = None,
) -> dict[str, Path]:
    if roots is not None:
        return {str(source): Path(path).expanduser() for source, path in roots.items()}
    return {
        "clean": Path(clean_dir).expanduser() if clean_dir is not None else DEFAULT_CLEAN_DIR,
        "watermark": Path(watermark_dir).expanduser() if watermark_dir is not None else DEFAULT_WATERMARK_DIR,
    }


def is_image_path(path: Path, suffixes: Iterable[str] | None = None) -> bool:
    allowed = {item.lower() for item in (suffixes or IMAGE_SUFFIXES)}
    return path.is_file() and not path.name.startswith(".") and path.suffix.lower() in allowed


def iter_image_paths(root: Path, suffixes: Iterable[str] | None = None) -> Iterable[Path]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if is_image_path(path, suffixes):
            yield path


def detect_tags(dish: str, norm: str) -> dict[str, Any]:
    searchable = f"{dish.lower()} {norm}"
    tags = []
    is_combo = bool(_PLUS_RE.search(dish)) or any(keyword in searchable for keyword in COMBO_KEYWORDS)
    is_drink = any(keyword in searchable for keyword in DRINK_KEYWORDS)
    is_snack = any(keyword in searchable for keyword in SNACK_KEYWORDS)
    is_promo = any(keyword in searchable for keyword in PROMO_KEYWORDS)
    is_raw = any(keyword in searchable for keyword in RAW_KEYWORDS)
    is_staple = any(keyword in searchable or normalize(keyword) in norm for keyword in STAPLE_KEYWORDS)
    is_side_addon = any(keyword in searchable or normalize(keyword) in norm for keyword in SIDE_ADDON_KEYWORDS)
    is_generic = any(keyword in searchable or normalize(keyword) in norm for keyword in GENERIC_IMAGE_KEYWORDS)
    is_low_quality_name = any(keyword in searchable or normalize(keyword) in norm for keyword in LOW_QUALITY_NAME_KEYWORDS)
    if is_combo:
        tags.append("combo")
        tags.append("package")
    if is_drink:
        tags.append("drink")
    if is_snack:
        tags.append("snack")
    if is_promo:
        tags.append("promo")
    if is_raw:
        tags.append("raw")
    if is_staple:
        tags.append("staple")
    if is_side_addon:
        tags.append("side_addon")
    if is_generic:
        tags.append("generic")
    if is_low_quality_name:
        tags.append("low_quality")
    return {
        "is_combo": is_combo,
        "is_package": is_combo,
        "is_drink": is_drink,
        "is_snack": is_snack,
        "is_promo": is_promo,
        "is_raw": is_raw,
        "is_staple": is_staple,
        "is_side_addon": is_side_addon,
        "is_generic": is_generic,
        "is_low_quality_name": is_low_quality_name,
        "tags": tags,
    }


def detect_reuse_flags(
    dish: str,
    source: str,
    path: str | Path | None = None,
    relative_path: str | Path | None = None,
    width: int | None = None,
    height: int | None = None,
    tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    dish_text = str(dish).lower()
    filename = Path(str(path)).stem if path is not None else dish
    filename_raw = str(filename).lower()
    review_text = f"{dish_text} {filename_raw}"
    normalized_text = normalize(review_text)
    filename_text = normalize(filename_raw)
    path_text = normalize(str(path or ""))
    tag_set = set(tags or [])
    review_reasons: list[str] = []
    delivery_blockers: list[str] = []
    extra_tags: list[str] = []
    score = 1.0

    has_brand_watermark = source_bucket(source) == "watermark" or "watermarkpic" in path_text
    if has_brand_watermark:
        score -= 0.25
        delivery_blockers.append("brand_watermark")
        review_reasons.append("品牌水印风险：来源或路径为 watermark，仅可参考")

    has_phone = bool(_PHONE_RE.search(review_text))
    has_brand_word = any(keyword.lower() in review_text or normalize(keyword) in normalized_text for keyword in BRAND_REVIEW_KEYWORDS)
    has_activity_word = any(keyword in review_text or normalize(keyword) in normalized_text for keyword in ACTIVITY_REVIEW_KEYWORDS)
    has_text_word = any(keyword.lower() in review_text or normalize(keyword) in normalized_text for keyword in TEXT_RISK_KEYWORDS)
    has_dish_text_watermark = any(
        keyword.lower() in review_text or normalize(keyword) in normalized_text
        for keyword in DISH_TEXT_WATERMARK_KEYWORDS
    )
    has_dish_text = has_dish_text_watermark or has_phone or has_activity_word or has_text_word
    suspected_watermark = has_brand_watermark or has_dish_text_watermark or has_phone or has_brand_word or has_activity_word
    if has_dish_text_watermark:
        score -= 0.08
        review_reasons.append("菜品名文字水印：可复用但需记录并降权")
    if has_phone:
        score -= 0.15
        review_reasons.append("疑似营销文字：包含电话")
    if has_brand_word:
        score -= 0.15
        review_reasons.append("疑似品牌元素：包含明显品牌词")
    if has_activity_word:
        score -= 0.12
        review_reasons.append("疑似营销文字：包含活动词")
    if has_text_word and not (has_dish_text_watermark or has_phone or has_activity_word):
        score -= 0.08
        review_reasons.append("疑似文字覆盖：包含文字/提示词")

    prompt_hits = [keyword for keyword in PROMPT_IMAGE_KEYWORDS if keyword in review_text or normalize(keyword) in filename_text]
    low_reuse_hits = [keyword for keyword in LOW_REUSE_KEYWORDS if keyword in review_text or normalize(keyword) in filename_text]
    if prompt_hits:
        score -= 0.45
        extra_tags.append("generic")
        review_reasons.append(f"低质/泛图：提示/背景类文件名（{','.join(prompt_hits[:3])}）")
    if low_reuse_hits:
        score -= 0.25
        extra_tags.append("generic")
        review_reasons.append(f"降权图：泛词/饮料/主食文件名（{','.join(low_reuse_hits[:3])}）")
    if tag_set & {"drink", "side_addon", "staple"}:
        score -= 0.15
        review_reasons.append("降权图：饮料/主食/小料不作为风格或匹配首选")
    if tag_set & {"promo", "raw", "generic"}:
        score -= 0.18
    if tag_set & {"low_quality"}:
        score -= 0.25

    low_resolution = False
    if width is not None and height is not None:
        low_resolution = min(width, height) < LOW_RESOLUTION_EDGE or (width * height) < LOW_RESOLUTION_AREA
        if low_resolution:
            score -= 0.35
            extra_tags.append("low_quality")
            review_reasons.append(f"低质图：分辨率偏低（{width}x{height}）")

    quality_score = round(max(0.0, min(1.0, score)), 2)
    all_tags = tag_set | set(extra_tags)
    avoid_as_style_card = has_brand_watermark or bool(all_tags & {"drink", "side_addon", "staple", "generic", "promo", "raw", "low_quality"})
    avoid_as_match_primary = has_brand_watermark or bool(all_tags & {"side_addon", "staple", "generic", "promo", "raw", "low_quality"})
    reusable = not has_brand_watermark
    reference_only = not reusable
    style_weight = 0.0 if has_brand_watermark else quality_score
    match_weight = 0.0 if has_brand_watermark else quality_score
    if avoid_as_style_card:
        style_weight = min(style_weight, 0.35)
    if avoid_as_match_primary:
        match_weight = min(match_weight, 0.45)
    return {
        "reusable": reusable,
        "reference_only": reference_only,
        "direct_delivery_allowed": reusable,
        "has_brand_watermark": has_brand_watermark,
        "suspected_watermark": suspected_watermark,
        "has_dish_text_watermark": has_dish_text_watermark,
        "has_dish_text": has_dish_text,
        "low_resolution": low_resolution,
        "avoid_as_style_card": avoid_as_style_card,
        "avoid_as_match_primary": avoid_as_match_primary,
        "style_weight": round(style_weight, 2),
        "match_weight": round(match_weight, 2),
        "delivery_blockers": delivery_blockers,
        "quality_score": quality_score,
        "review_reasons": review_reasons,
        "_extra_tags": extra_tags,
    }


def match_category_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "single": 0,
        "package": 0,
        "beverage": 0,
        "snack": 0,
        "staple": 0,
        "addon": 0,
        "other": 0,
        "formal": 0,
    }
    for record in records:
        dish = str(record.get("dish") or record.get("dishName") or record.get("name") or "")
        norm = str(record.get("norm") or normalize_match_dish(dish) or normalize(dish))
        category = str(record.get("match_category") or match_category_for(dish, norm, record.get("tags") or []))
        counts[category] = counts.get(category, 0) + 1
        reusable = bool(record.get("reusable", False))
        reference_only = bool(record.get("reference_only", not reusable))
        if reusable and not reference_only and category != "other":
            counts["formal"] += 1
    return counts


def cleaning_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    total = 0
    reusable = 0
    reference_only = 0
    watermark_risk = 0
    suspected_watermark = 0
    dish_text_watermark = 0
    needs_review = 0
    low_quality = 0
    generic = 0
    downranked = 0
    for record in records:
        total += 1
        is_reusable = bool(record.get("reusable", False))
        is_reference_only = bool(record.get("reference_only", not is_reusable))
        has_watermark = bool(record.get("has_brand_watermark", False))
        has_suspected_watermark = bool(record.get("suspected_watermark", False))
        has_dish_text_watermark = bool(record.get("has_dish_text_watermark", record.get("has_dish_text", False)))
        has_dish_text = bool(record.get("has_dish_text", False))
        quality_score = record.get("quality_score")
        try:
            score_value = float(quality_score) if quality_score is not None else None
        except (TypeError, ValueError):
            score_value = None
        reasons = record.get("review_reasons") or []
        tags = set(record.get("tags") or [])

        if is_reusable:
            reusable += 1
        if is_reference_only:
            reference_only += 1
        if has_watermark:
            watermark_risk += 1
        if has_suspected_watermark:
            suspected_watermark += 1
        if has_dish_text_watermark:
            dish_text_watermark += 1
        if score_value is not None and score_value < LOW_QUALITY_SCORE_THRESHOLD:
            low_quality += 1
        if tags & {"generic", "drink", "side_addon", "staple"}:
            generic += 1
        if record.get("avoid_as_style_card") or record.get("avoid_as_match_primary"):
            downranked += 1
        if has_watermark or has_suspected_watermark or has_dish_text or reasons or (score_value is not None and score_value < LOW_QUALITY_SCORE_THRESHOLD):
            needs_review += 1
    return {
        "total": total,
        "reusable": reusable,
        "referenceOnly": reference_only,
        "watermarkRisk": watermark_risk,
        "suspectedWatermark": suspected_watermark,
        "dishTextWatermark": dish_text_watermark,
        "needsReview": needs_review,
        "lowQuality": low_quality,
        "genericOrAddon": generic,
        "downranked": downranked,
    }


def sha1_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    total = 0
    for record in records:
        digest = str(record.get("sha1") or "")
        if not digest:
            continue
        total += 1
        counts[digest] = counts.get(digest, 0) + 1
    unique = len(counts)
    duplicate_groups = sum(1 for count in counts.values() if count > 1)
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    return {
        "total": total,
        "unique": unique,
        "deduped": unique,
        "duplicates": duplicates,
        "duplicateGroups": duplicate_groups,
    }


def annotate_sha1_duplicates(records: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(str(record.get("sha1") or ""), []).append(record)
    for digest, group in groups.items():
        if not digest:
            continue
        group.sort(
            key=lambda item: (
                not bool(item.get("reusable", False)),
                source_bucket(str(item.get("source") or "")) != "clean",
                str(item.get("path") or ""),
            )
        )
        primary_path = str(group[0].get("path") or "")
        for index, record in enumerate(group):
            record["sha1_group_size"] = len(group)
            record["sha1_duplicate"] = index > 0
            record["sha1_primary"] = index == 0
            record["sha1_primary_path"] = primary_path


def thumbnail_path_for(thumb_dir: Path, source: str, digest: str) -> Path:
    return thumb_dir / source / digest[:2] / f"{digest}.jpg"


def make_thumbnail(src: Path, target: Path, size: tuple[int, int]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if getattr(img, "is_animated", False):
            img.seek(0)
        if img.mode == "P" and "transparency" in img.info:
            img = img.convert("RGBA")
        img.thumbnail(size, Image.Resampling.LANCZOS)
        if img.mode not in {"RGB", "L"}:
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        img.save(target, format="JPEG", quality=82, optimize=True)


def build_record(
    path: Path,
    root: Path,
    source: str,
    thumb_dir: Path | None = DEFAULT_THUMB_DIR,
    make_thumbs: bool = True,
    thumb_size: tuple[int, int] = (256, 256),
) -> dict[str, Any]:
    rel = path.relative_to(root)
    parts = rel.parts
    store = parts[0] if len(parts) > 1 else root.name
    category_path = "/".join(parts[1:-1]) if len(parts) > 2 else ""
    dish = path.stem.strip()
    norm = normalize_match_dish(dish) or normalize(dish)
    digest = sha1_file(path)
    stat = path.stat()
    thumb_path = thumbnail_path_for(thumb_dir, source, digest) if thumb_dir else None

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        width, height = img.size

    if make_thumbs and thumb_path is not None:
        make_thumbnail(path, thumb_path, thumb_size)

    record = {
        "id": digest[:18],
        "sha1": digest,
        "source": source,
        "source_kind": source_bucket(source),
        "source_root": str(root),
        "store": store,
        "category_path": category_path,
        "dish": dish,
        "norm": norm,
        "canonical_norm": norm,
        "match_family": semantic_match_family(dish, norm),
        "match_kind": classify_match_kind(dish),
        "style_id": style_id_for_item(store, dish, digest),
        "suffix": path.suffix.lower(),
        "size": stat.st_size,
        "width": width,
        "height": height,
        "path": str(path),
        "relative_path": rel.as_posix(),
        "thumb_path": str(thumb_path) if thumb_path else "",
    }
    record.update(detect_tags(dish, norm))
    record["match_category"] = match_category_for(dish, norm, record.get("tags") or [])
    flags = detect_reuse_flags(
        dish=dish,
        source=source,
        path=path,
        relative_path=rel,
        width=width,
        height=height,
        tags=record.get("tags") or [],
    )
    extra_tags = flags.pop("_extra_tags", [])
    if extra_tags:
        record["tags"] = sorted(set(record.get("tags") or []) | set(extra_tags))
        record["is_generic"] = bool(record.get("is_generic")) or "generic" in extra_tags
        record["is_low_quality_name"] = bool(record.get("is_low_quality_name")) or "low_quality" in extra_tags
    record["is_low_quality"] = "low_quality" in set(record.get("tags") or [])
    record.update(flags)
    return record


def scan_library(
    clean_dir: str | Path | None = None,
    watermark_dir: str | Path | None = None,
    roots: Mapping[str, str | Path] | None = None,
    thumb_dir: str | Path | None = DEFAULT_THUMB_DIR,
    make_thumbs: bool = True,
    thumb_size: tuple[int, int] = (256, 256),
    image_suffixes: Iterable[str] | None = None,
) -> ScanResult:
    started = time.perf_counter()
    resolved_roots = image_roots(clean_dir=clean_dir, watermark_dir=watermark_dir, roots=roots)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    resolved_thumb_dir = Path(thumb_dir).expanduser() if thumb_dir is not None else None

    for source, root in resolved_roots.items():
        if not root.exists():
            errors.append({"source": source, "path": str(root), "error": "source directory does not exist"})
            continue
        for path in iter_image_paths(root, image_suffixes):
            try:
                records.append(
                    build_record(
                        path=path,
                        root=root,
                        source=source,
                        thumb_dir=resolved_thumb_dir,
                        make_thumbs=make_thumbs,
                        thumb_size=thumb_size,
                    )
                )
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                errors.append({"source": source, "path": str(path), "error": str(exc)})

    records.sort(key=lambda item: (str(item["source"]), str(item["store"]), str(item["relative_path"])))
    annotate_sha1_duplicates(records)
    return ScanResult(
        records=records,
        errors=errors,
        elapsed_seconds=time.perf_counter() - started,
        roots={source: str(path) for source, path in resolved_roots.items()},
    )


def write_index(records: ScanResult | Iterable[Mapping[str, Any]], output_path: str | Path = DEFAULT_INDEX_PATH) -> Path:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    items = records.records if isinstance(records, ScanResult) else records
    with path.open("w", encoding="utf-8") as handle:
        for record in items:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan local clean/watermarked image libraries into a JSONL index.")
    parser.add_argument("--clean-dir", default=str(DEFAULT_CLEAN_DIR), help="clean source directory")
    parser.add_argument("--watermark-dir", default=str(DEFAULT_WATERMARK_DIR), help="watermarked source directory")
    parser.add_argument("--output", default=str(DEFAULT_INDEX_PATH), help="JSONL index output path")
    parser.add_argument("--thumb-dir", default=str(DEFAULT_THUMB_DIR), help="thumbnail output directory")
    parser.add_argument("--no-thumbs", action="store_true", help="skip thumbnail generation")
    parser.add_argument("--thumb-size", type=int, default=256, help="max thumbnail edge in pixels")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = scan_library(
        clean_dir=args.clean_dir,
        watermark_dir=args.watermark_dir,
        thumb_dir=args.thumb_dir,
        make_thumbs=not args.no_thumbs,
        thumb_size=(args.thumb_size, args.thumb_size),
    )
    index_path = write_index(result, args.output)
    summary = result.summary()
    print(json.dumps({"index": str(index_path), **summary}, ensure_ascii=False, indent=2, sort_keys=True))
    if result.errors:
        print("errors:")
        for error in result.errors[:20]:
            print(f"- {error['source']} {error['path']}: {error['error']}")
        if len(result.errors) > 20:
            print(f"- ... {len(result.errors) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
