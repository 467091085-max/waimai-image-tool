from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from library_index import (
    DEFAULT_CLEAN_DIR,
    DEFAULT_WATERMARK_DIR,
    scan_library,
    source_bucket,
    style_id_for_item,
    watermark_state_for_record,
    write_index,
)

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = BASE_DIR / ".env.cos"
DEFAULT_PREFIX = "waimai-gallery"
DEFAULT_OUTPUT = BASE_DIR / "data" / "library_index" / "cos_library_index.jsonl"
DEFAULT_MAX_SIDE = 1200
DEFAULT_QUALITY = 84
INDEX_RELATIVE_KEY = "index/library_index.jsonl"
CONTENT_TYPE_JPEG = "image/jpeg"
CONTENT_TYPE_JSONL = "application/x-ndjson; charset=utf-8"

_KEY_UNSAFE_RE = re.compile(r"[\\/:*?\"<>|\r\n\t]+")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class SyncConfig:
    clean_dir: Path
    watermark_dir: Path
    bucket: str
    region: str
    prefix: str = DEFAULT_PREFIX
    limit: int | None = None
    dry_run: bool = True
    output: Path = DEFAULT_OUTPUT
    max_side: int = DEFAULT_MAX_SIDE
    quality: int = DEFAULT_QUALITY


@dataclass
class PreparedJpeg:
    data: bytes
    width: int
    height: int
    size: int
    sha1: str


def load_env_file(path: str | Path | None, *, override: bool = False) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def load_default_env_file() -> dict[str, str]:
    return load_env_file(DEFAULT_ENV_FILE)


def first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def default_bucket() -> str:
    return first_env("TENCENT_COS_BUCKET", "COS_BUCKET")


def default_region() -> str:
    return first_env("TENCENT_COS_REGION", "TENCENTCLOUD_REGION", "TENCENT_REGION", "COS_REGION", default="ap-guangzhou")


def default_secret_id() -> str:
    return first_env("TENCENTCLOUD_SECRET_ID", "TENCENT_SECRET_ID", "COS_SECRET_ID")


def default_secret_key() -> str:
    return first_env("TENCENTCLOUD_SECRET_KEY", "TENCENT_SECRET_KEY", "COS_SECRET_KEY")


def missing_upload_config(bucket: str, region: str) -> list[str]:
    missing = []
    if not str(bucket or "").strip():
        missing.append("--bucket/TENCENT_COS_BUCKET/COS_BUCKET")
    if not str(region or "").strip():
        missing.append("--region/TENCENT_COS_REGION/COS_REGION")
    if not default_secret_id():
        missing.append("TENCENTCLOUD_SECRET_ID/TENCENT_SECRET_ID/COS_SECRET_ID")
    if not default_secret_key():
        missing.append("TENCENTCLOUD_SECRET_KEY/TENCENT_SECRET_KEY/COS_SECRET_KEY")
    return missing


def validate_upload_config(bucket: str, region: str) -> None:
    missing = missing_upload_config(bucket, region)
    if missing:
        raise RuntimeError(
            "COS upload is disabled because Tencent Cloud configuration is incomplete; "
            f"missing: {', '.join(missing)}. "
            "Run without --no-dry-run to scan and write the JSONL index locally."
        )


def mask_configured_secrets(message: str) -> str:
    masked = str(message)
    for value in {default_secret_id(), default_secret_key()}:
        if value:
            masked = masked.replace(value, "***")
    return masked


def normalize_prefix(prefix: str | None) -> str:
    value = str(prefix or "").strip().strip("/")
    return value or DEFAULT_PREFIX


def safe_key_part(value: str, fallback: str = "item", max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _KEY_UNSAFE_RE.sub("_", text)
    text = _SPACE_RE.sub("_", text).strip(" ._")
    text = re.sub(r"_+", "_", text)
    return text[:max_len] or fallback


def index_key(prefix: str = DEFAULT_PREFIX) -> str:
    return f"{normalize_prefix(prefix)}/{INDEX_RELATIVE_KEY}"


def cos_key_for_record(record: dict[str, Any], prefix: str = DEFAULT_PREFIX) -> str:
    source = source_bucket(str(record.get("source") or "unknown"))
    source_part = safe_key_part(source, "unknown")
    store_part = safe_key_part(str(record.get("store") or ""), "store")
    digest = str(record.get("sha1") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", digest):
        seed = f"{record.get('source')}:{record.get('store')}:{record.get('relative_path')}:{record.get('id')}"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"{normalize_prefix(prefix)}/{source_part}/{store_part}/{digest}.jpg"


def public_url_for_key(bucket: str, region: str, key: str) -> str:
    bucket = str(bucket or "").strip()
    region = str(region or "").strip()
    if not bucket or not region or not key:
        return ""
    quoted_key = urllib.parse.quote(key, safe="/")
    return f"https://{bucket}.cos.{region}.myqcloud.com/{quoted_key}"


def rgb_image(raw: Image.Image) -> Image.Image:
    if getattr(raw, "is_animated", False):
        raw.seek(0)
    image = ImageOps.exif_transpose(raw)
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return image.convert("RGB")


def prepare_jpeg(path: str | Path, max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_QUALITY) -> PreparedJpeg:
    quality = max(45, min(95, int(quality)))
    max_side = max(0, int(max_side))
    with Image.open(Path(path)) as raw:
        image = rgb_image(raw)
        if max_side and max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    data = output.getvalue()
    return PreparedJpeg(
        data=data,
        width=image.width,
        height=image.height,
        size=len(data),
        sha1=hashlib.sha1(data).hexdigest(),
    )


def build_index_record(
    record: dict[str, Any],
    prepared: PreparedJpeg,
    bucket: str,
    region: str,
    prefix: str = DEFAULT_PREFIX,
) -> dict[str, Any]:
    key = cos_key_for_record(record, prefix)
    public_url = public_url_for_key(bucket, region, key)
    source = source_bucket(str(record.get("source") or "unknown"))
    style_id = str(record.get("style_id") or style_id_for_item(str(record.get("store") or ""), str(record.get("dish") or ""), str(record.get("sha1") or "")))
    dish = str(record.get("dish") or record.get("name") or "")
    category = str(record.get("category_path") or record.get("category") or "")
    canonical = str(record.get("canonical") or record.get("canonical_dish") or record.get("norm") or dish)
    watermark = watermark_state_for_record(record)
    return {
        "id": record.get("id"),
        "canonical": canonical,
        "canonical_dish": canonical,
        "canonical_norm": record.get("canonical_norm") or record.get("norm") or canonical,
        "dish": dish,
        "name": dish,
        "norm": record.get("norm"),
        "store": record.get("store"),
        "category": category,
        "category_path": category,
        "style": style_id,
        "background": str(record.get("background") or record.get("background_id") or style_id),
        "background_id": str(record.get("background_id") or record.get("background") or style_id),
        "source": source,
        "source_kind": source,
        "reusable": bool(record.get("reusable")),
        "reference_only": bool(record.get("reference_only")),
        "direct_delivery_allowed": bool(record.get("direct_delivery_allowed")),
        "style_id": style_id,
        "match_family": record.get("match_family"),
        "match_kind": record.get("match_kind"),
        "match_category": record.get("match_category"),
        "local_path": str(record.get("local_path") or record.get("path") or ""),
        "relative_path": record.get("relative_path"),
        "cos_key": key,
        "cos_bucket": bucket,
        "cos_region": region,
        "url": public_url,
        "public_url": public_url,
        "watermark": watermark,
        "watermark_state": watermark,
        "width": prepared.width,
        "height": prepared.height,
        "original_width": record.get("width"),
        "original_height": record.get("height"),
        "original_size": record.get("size"),
        "object_size": prepared.size,
        "sha1": record.get("sha1"),
        "processed_sha1": prepared.sha1,
        "tags": sorted(set(record.get("tags") or [])),
        "quality_score": record.get("quality_score"),
        "has_brand_watermark": bool(record.get("has_brand_watermark")),
        "has_dish_text_watermark": bool(record.get("has_dish_text_watermark")),
        "has_dish_text": bool(record.get("has_dish_text")),
        "suspected_watermark": bool(record.get("suspected_watermark")),
        "avoid_as_style_card": bool(record.get("avoid_as_style_card")),
        "avoid_as_match_primary": bool(record.get("avoid_as_match_primary")),
        "style_weight": record.get("style_weight"),
        "match_weight": record.get("match_weight"),
        "is_combo": bool(record.get("is_combo")),
        "is_drink": bool(record.get("is_drink")),
        "is_promo": bool(record.get("is_promo")),
        "is_raw": bool(record.get("is_raw")),
        "is_staple": bool(record.get("is_staple")),
        "is_side_addon": bool(record.get("is_side_addon")),
        "is_generic": bool(record.get("is_generic")),
        "is_low_quality": bool(record.get("is_low_quality")),
        "low_resolution": bool(record.get("low_resolution")),
        "delivery_blockers": list(record.get("delivery_blockers") or []),
        "review_reasons": list(record.get("review_reasons") or []),
        "sha1_group_size": record.get("sha1_group_size", 1),
        "sha1_duplicate": bool(record.get("sha1_duplicate")),
        "sha1_primary": bool(record.get("sha1_primary", True)),
    }


def create_cos_client(region: str):
    secret_id = default_secret_id()
    secret_key = default_secret_key()
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except Exception as exc:
        raise RuntimeError("COS upload requires cos-python-sdk-v5") from exc
    config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key, Scheme="https")
    return CosS3Client(config)


def upload_bytes(client: Any, bucket: str, key: str, data: bytes, content_type: str) -> None:
    client.put_object(Bucket=bucket, Body=io.BytesIO(data), Key=key, ContentType=content_type)


def summary_path_for(output: Path) -> Path:
    return output.with_suffix(".summary.json")


def write_summary(summary: dict[str, Any], output: Path) -> Path:
    summary_path = summary_path_for(output)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary["summary"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary_path


def sync_gallery(config: SyncConfig) -> dict[str, Any]:
    prefix = normalize_prefix(config.prefix)
    output = Path(config.output).expanduser()
    bucket = str(config.bucket or "").strip()
    region = str(config.region or "").strip() or "ap-guangzhou"
    limit = config.limit if config.limit and config.limit > 0 else None
    if not config.dry_run:
        validate_upload_config(bucket, region)
    result = scan_library(
        clean_dir=config.clean_dir,
        watermark_dir=config.watermark_dir,
        thumb_dir=None,
        make_thumbs=False,
    )
    records = result.records[:limit] if limit else result.records
    client = None
    if not config.dry_run:
        client = create_cos_client(region)

    index_records: list[dict[str, Any]] = []
    sync_errors: list[dict[str, str]] = []
    uploaded_images = 0
    for record in records:
        try:
            prepared = prepare_jpeg(
                Path(str(record.get("path") or "")),
                max_side=config.max_side,
                quality=config.quality,
            )
            index_record = build_index_record(record, prepared, bucket=bucket, region=region, prefix=prefix)
            if client is not None:
                upload_bytes(client, bucket, str(index_record["cos_key"]), prepared.data, CONTENT_TYPE_JPEG)
                uploaded_images += 1
            index_records.append(index_record)
        except Exception as exc:
            sync_errors.append(
                {
                    "source": str(record.get("source") or ""),
                    "relative_path": str(record.get("relative_path") or ""),
                    "error": mask_configured_secrets(str(exc)),
                }
            )

    index_path = write_index(index_records, output)
    index_uploaded = False
    if client is not None:
        upload_bytes(client, bucket, index_key(prefix), index_path.read_bytes(), CONTENT_TYPE_JSONL)
        index_uploaded = True

    scan_summary = result.summary()
    fatal_errors = bool(sync_errors) and not config.dry_run
    reusable_images = int(scan_summary.get("reusable") or 0)
    watermarked_reference_images = int(
        scan_summary.get("watermark")
        or (scan_summary.get("cleaning") or {}).get("watermarkRisk")
        or (scan_summary.get("cleaning") or {}).get("referenceOnly")
        or 0
    )
    processed_reusable_images = sum(1 for record in index_records if bool(record.get("reusable")))
    processed_watermarked_reference_images = sum(
        1
        for record in index_records
        if bool(record.get("has_brand_watermark")) or str(record.get("source")) == "watermark" or bool(record.get("reference_only"))
    )
    limit_skipped = max(0, result.total - len(records))
    dry_run_upload_skipped = len(index_records) if config.dry_run else 0
    skipped_images = limit_skipped + dry_run_upload_skipped
    error_images = len(result.errors) + len(sync_errors)
    sync_counts = {
        "totalImages": result.total,
        "processedImages": len(index_records),
        "reusableImages": reusable_images,
        "watermarkedReferenceImages": watermarked_reference_images,
        "processedReusableImages": processed_reusable_images,
        "processedWatermarkedReferenceImages": processed_watermarked_reference_images,
        "uploadedSuccess": uploaded_images,
        "uploadSkipped": dry_run_upload_skipped,
        "limitSkipped": limit_skipped,
        "skippedImages": skipped_images,
        "errorImages": error_images,
    }
    summary = {
        "ok": not fatal_errors,
        "dryRun": bool(config.dry_run),
        "bucket": bucket,
        "region": region,
        "prefix": prefix,
        "indexKey": index_key(prefix),
        "indexUrl": public_url_for_key(bucket, region, index_key(prefix)),
        "renderEnv": {
            "COS_LIBRARY_INDEX_URL": public_url_for_key(bucket, region, index_key(prefix)),
            "TENCENT_COS_BUCKET": bucket,
            "TENCENT_COS_REGION": region,
        },
        "output": str(index_path),
        "maxSide": max(0, int(config.max_side)),
        "quality": max(45, min(95, int(config.quality))),
        "limit": limit or 0,
        "scannedTotal": result.total,
        "indexedTotal": len(index_records),
        "totalImages": result.total,
        "reusableImages": reusable_images,
        "watermarkedReferenceImages": watermarked_reference_images,
        "uploadedSuccess": uploaded_images,
        "skippedImages": skipped_images,
        "errorImages": error_images,
        "uploadedImages": uploaded_images,
        "wouldUploadImages": len(index_records) if config.dry_run else 0,
        "indexUploaded": index_uploaded,
        "scanErrorCount": len(result.errors),
        "syncErrorCount": len(sync_errors),
        "scanErrors": result.errors[:20],
        "syncErrors": sync_errors[:20],
        "sync": sync_counts,
        "scan": scan_summary,
    }
    write_summary(summary, index_path)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help=argparse.SUPPRESS)
    env_args, _remaining = env_parser.parse_known_args(argv)
    load_env_file(env_args.env_file)

    parser = argparse.ArgumentParser(description="Sync cleanpic/watermarkpic gallery images to Tencent COS and emit JSONL index.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="local env file to load before reading COS settings; default .env.cos")
    parser.add_argument("--clean-dir", default=str(DEFAULT_CLEAN_DIR), help="local cleanpic directory")
    parser.add_argument("--watermark-dir", default=str(DEFAULT_WATERMARK_DIR), help="local watermarkpic directory")
    parser.add_argument("--bucket", default=default_bucket(), help="Tencent COS bucket, e.g. waimai-image-tool-125xxxx")
    parser.add_argument("--region", default=default_region(), help="Tencent COS region, e.g. ap-guangzhou")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="COS key prefix")
    parser.add_argument("--limit", type=int, default=0, help="max images to index/upload; 0 means all")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="local JSONL index output path")
    parser.add_argument("--max-side", type=int, default=DEFAULT_MAX_SIDE, help="max uploaded JPEG edge; 0 disables resizing")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="uploaded JPEG quality, clamped to 45..95")
    dry_run_group = parser.add_mutually_exclusive_group()
    dry_run_group.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="do not upload to COS")
    dry_run_group.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="upload images and index to COS")
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> SyncConfig:
    return SyncConfig(
        clean_dir=Path(args.clean_dir).expanduser(),
        watermark_dir=Path(args.watermark_dir).expanduser(),
        bucket=str(args.bucket or "").strip(),
        region=str(args.region or "").strip() or "ap-guangzhou",
        prefix=str(args.prefix or DEFAULT_PREFIX),
        limit=int(args.limit or 0),
        dry_run=bool(args.dry_run),
        output=Path(args.output).expanduser(),
        max_side=int(args.max_side),
        quality=int(args.quality),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = sync_gallery(config_from_args(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": mask_configured_secrets(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
