from __future__ import annotations

import base64
import io
import re
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

PLATFORMS = {
    "meituan": {
        "name": "美团外卖",
        "width": 800,
        "height": 600,
        "aspect": "4:3",
        "maxKB": 5120,
        "formats": ["jpg", "png"],
        "defaultFormat": "jpg",
        "mode": "RGB",
        "default": True,
    },
    "taobao": {
        "name": "淘宝外卖/饿了么",
        "width": 800,
        "height": 800,
        "aspect": "1:1",
        "maxKB": 20480,
        "formats": ["jpg", "png"],
        "defaultFormat": "jpg",
        "mode": "RGB",
        "default": False,
    },
    "jd": {
        "name": "京东外卖/京东秒送",
        "width": 800,
        "height": 800,
        "aspect": "1:1",
        "maxKB": 5120,
        "formats": ["jpg", "jpeg", "png"],
        "defaultFormat": "jpg",
        "mode": "RGB",
        "default": False,
    },
}

EXTRA_PLATFORM_POINTS = 100
REMOTE_IMAGE_TIMEOUT_SECONDS = 10
REMOTE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
REMOTE_IMAGE_CHUNK_BYTES = 64 * 1024
REMOTE_IMAGE_USER_AGENT = "waimai-image-tool/4.0 export-remote-media"
REMOTE_IMAGE_OPENER = build_opener(ProxyHandler({}))

REPORT_COLUMNS = [
    "菜品名",
    "分类",
    "类型",
    "平台",
    "尺寸",
    "文件大小KB",
    "平台上限KB",
    "图片状态",
    "预计积分",
    "品牌水印",
    "交付文件",
]


def safe_filename(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name))
    value = re.sub(r"[\x00-\x1f/:*?\"<>|\\]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "LPT1", "LPT2", "LPT3"}:
        value = f"{value}_"
    return value[:90] or "file"


def font(size: int) -> ImageFont.ImageFont:
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
    x, y = positions.get(position, positions["bottom-right"])
    if mw + (margin * 2) <= bw:
        x = max(margin, min(x, bw - mw - margin))
    else:
        x = max(0, (bw - mw) // 2)
    if mh + (margin * 2) <= bh:
        y = max(margin, min(y, bh - mh - margin))
    else:
        y = max(0, (bh - mh) // 2)
    return x, y


def watermark_text_fill(color: str) -> tuple[int, int, int, int]:
    return (255, 255, 255, 190) if str(color or "").lower() == "white" else (24, 32, 42, 175)


def make_text_watermark(text: str, width: int, color: str = "black") -> Image.Image:
    label = str(text or "品牌水印").strip()[:24] or "品牌水印"
    mark_font = font(max(24, width // 28))
    probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    tw, th = text_size(draw, label, mark_font)
    pad_x = max(4, width // 160)
    pad_y = max(3, width // 220)
    mark = Image.new("RGBA", (tw + pad_x * 2, th + pad_y * 2), (0, 0, 0, 0))
    mark_draw = ImageDraw.Draw(mark)
    mark_draw.text((pad_x, pad_y), label, fill=watermark_text_fill(color), font=mark_font)
    return mark


def make_logo_watermark(data_url: str, width: int) -> Image.Image | None:
    payload = str(data_url or "")
    if "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload)
        logo = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    max_w = max(90, width // 5)
    max_h = max(60, width // 8)
    logo.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    return logo


def fit_watermark_to_safe_area(mark: Image.Image, base_size: tuple[int, int], margin: int) -> Image.Image:
    max_w = max(1, base_size[0] - (margin * 2))
    max_h = max(1, base_size[1] - (margin * 2))
    if mark.width <= max_w and mark.height <= max_h:
        return mark
    fitted = mark.copy()
    fitted.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    return fitted


def paste_tiled(overlay: Image.Image, mark: Image.Image, margin: int = 0) -> None:
    gap_x = max(40, mark.width // 2)
    gap_y = max(38, mark.height)
    bottom = max(margin, overlay.height - margin - mark.height)
    right = max(margin, overlay.width - margin - mark.width)
    y = margin
    row = 0
    while y <= bottom:
        offset = 0 if row % 2 == 0 else (mark.width + gap_x) // 2
        x = margin + offset
        while x <= right:
            overlay.alpha_composite(mark, (x, y))
            x += mark.width + gap_x
        y += mark.height + gap_y
        row += 1


def apply_watermark(img: Image.Image, settings: dict[str, Any] | None) -> Image.Image:
    if not isinstance(settings, dict) or not settings.get("enabled"):
        return img
    base = img.convert("RGBA")
    mark_type = str(settings.get("type") or "text")
    mark = make_logo_watermark(str(settings.get("logoData") or ""), base.width) if mark_type == "logo" else None
    if mark is None:
        mark = make_text_watermark(str(settings.get("text") or "品牌水印"), base.width, str(settings.get("color") or "black"))
    if mark.width <= 0 or mark.height <= 0:
        return base
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    margin = max(24, base.width // 34)
    mark = fit_watermark_to_safe_area(mark, base.size, margin)
    if str(settings.get("pattern") or "corner") == "tile":
        if mark_type == "text":
            mark = mark.rotate(-22, expand=True)
            mark = fit_watermark_to_safe_area(mark, base.size, margin)
        paste_tiled(overlay, mark, margin)
    else:
        position = str(settings.get("position") or "bottom-right")
        overlay.alpha_composite(mark, watermark_position(base.size, mark.size, position, margin))
    return Image.alpha_composite(base, overlay)


def parse_platforms(value: Any, *, default: list[str] | tuple[str, ...] | None = ("meituan",)) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        value = list(default or [])
    out = []
    for item in value:
        key = str(item)
        if key in PLATFORMS and key not in out:
            out.append(key)
    return out or list(default or [])


def require_platforms(value: Any) -> list[str]:
    selected = parse_platforms(value, default=[])
    if not selected:
        raise ValueError("请至少选择一个导出平台")
    return selected


def platform_extra_points(platforms: list[str] | str | None, extra_points: int = EXTRA_PLATFORM_POINTS) -> int:
    selected = parse_platforms(platforms, default=[])
    return max(len(selected) - 1, 0) * int(extra_points)


def normalize_image_format(image_format: str | None, platform_id: str) -> tuple[str, str, str]:
    spec = PLATFORMS.get(platform_id, PLATFORMS["meituan"])
    requested = str(image_format or spec.get("defaultFormat") or "jpg").lower().strip().lstrip(".")
    if requested in {"image/jpg", "image/jpeg", "image/png"}:
        requested = requested.split("/", 1)[1]
    allowed = {str(item).lower() for item in spec.get("formats", ["jpg"])}
    requested_is_supported_jpeg = requested in {"jpg", "jpeg"} and bool(allowed.intersection({"jpg", "jpeg"}))
    if requested == "png" and "png" not in allowed:
        requested = str(spec.get("defaultFormat") or "jpg").lower()
    elif requested in {"jpg", "jpeg"} and not requested_is_supported_jpeg:
        requested = str(spec.get("defaultFormat") or "jpg").lower()
    elif requested not in allowed and not requested_is_supported_jpeg:
        requested = str(spec.get("defaultFormat") or "jpg").lower()
    if requested == "png":
        return "png", ".png", "PNG"
    if requested == "jpeg" and "jpeg" in allowed:
        return "jpeg", ".jpeg", "JPEG"
    return "jpg", ".jpg", "JPEG"


def platform_folder_name(platform_id: str, spec: dict[str, Any]) -> str:
    return f"{platform_id}_{safe_filename(str(spec['name']))}_{spec['width']}x{spec['height']}"


def edge_background(img: Image.Image) -> tuple[int, int, int, int]:
    rgba = img.convert("RGBA")
    samples = [
        rgba.getpixel((0, 0)),
        rgba.getpixel((rgba.width - 1, 0)),
        rgba.getpixel((0, rgba.height - 1)),
        rgba.getpixel((rgba.width - 1, rgba.height - 1)),
    ]
    return tuple(int(sum(pixel[i] for pixel in samples) / len(samples)) for i in range(4))


def expanded_bbox(bbox: tuple[int, int, int, int], size: tuple[int, int], padding_ratio: float = 0.04) -> tuple[int, int, int, int]:
    width, height = size
    pad = max(8, int(max(width, height) * padding_ratio))
    left, top, right, bottom = bbox
    return max(0, left - pad), max(0, top - pad), min(width, right + pad), min(height, bottom + pad)


def subject_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    rgba = img.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox and alpha_bbox != (0, 0, rgba.width, rgba.height):
        return expanded_bbox(alpha_bbox, rgba.size)

    background = Image.new("RGBA", rgba.size, edge_background(rgba))
    diff = ImageChops.difference(rgba, background).convert("L")
    mask = diff.point(lambda value: 255 if value > 22 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    if bbox == (0, 0, rgba.width, rgba.height):
        return None
    bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    image_area = rgba.width * rgba.height
    if bbox_area < image_area * 0.02:
        return None
    return expanded_bbox(bbox, rgba.size)


def crop_to_subject(img: Image.Image) -> Image.Image:
    bbox = subject_bbox(img)
    if not bbox:
        return img
    left, top, right, bottom = bbox
    if (right - left) <= 1 or (bottom - top) <= 1:
        return img
    return img.crop(bbox)


def fit_to_platform(img: Image.Image, platform_id: str, *, crop: bool = True) -> Image.Image:
    spec = PLATFORMS.get(platform_id, PLATFORMS["meituan"])
    target = (int(spec["width"]), int(spec["height"]))
    src = img.convert("RGBA")
    subject = crop_to_subject(src) if crop else src
    fitted = ImageOps.contain(subject, target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", target, edge_background(src))
    canvas.alpha_composite(fitted, ((target[0] - fitted.width) // 2, (target[1] - fitted.height) // 2))
    return canvas


def prepare_platform_image(img: Image.Image, platform_id: str, watermark: dict[str, Any] | None = None) -> Image.Image:
    fitted = fit_to_platform(img, platform_id)
    return apply_watermark(fitted, watermark)


def save_platform_image(img: Image.Image, target: Path, max_kb: int, image_format: str = "jpg") -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = max(64, int(max_kb)) * 1024
    rgb = img.convert("RGB")
    if str(image_format).lower() == "png":
        rgb.save(target, "PNG", optimize=True, compress_level=9)
        if target.stat().st_size > max_bytes:
            rgb.quantize(colors=256).convert("RGB").save(target, "PNG", optimize=True, compress_level=9)
        return target.stat().st_size

    for quality in list(range(92, 34, -5)) + [30, 25, 20, 15, 10, 5]:
        rgb.save(target, "JPEG", quality=quality, optimize=True, progressive=True, subsampling=2)
        if target.stat().st_size <= max_bytes:
            return target.stat().st_size
    return target.stat().st_size


class RemoteImageDownloadError(Exception):
    pass


def is_http_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def download_remote_image(url: str, *, timeout: int = REMOTE_IMAGE_TIMEOUT_SECONDS, max_bytes: int = REMOTE_IMAGE_MAX_BYTES) -> bytes:
    request = Request(
        str(url),
        headers={
            "User-Agent": REMOTE_IMAGE_USER_AGENT,
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8",
        },
    )
    try:
        with REMOTE_IMAGE_OPENER.open(request, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            if status and int(status) >= 400:
                raise RemoteImageDownloadError(f"HTTP {status}")

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise RemoteImageDownloadError("remote image is too large")
                except ValueError:
                    pass

            payload = io.BytesIO()
            total = 0
            while True:
                chunk = response.read(REMOTE_IMAGE_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RemoteImageDownloadError("remote image is too large")
                payload.write(chunk)
            return payload.getvalue()
    except RemoteImageDownloadError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RemoteImageDownloadError(str(exc)) from exc


def open_image_file(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).copy()


def open_image_bytes(payload: bytes) -> Image.Image:
    with Image.open(io.BytesIO(payload)) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB").copy()


def load_candidate_image(candidate: dict[str, Any] | None) -> tuple[Image.Image | None, str]:
    if not candidate:
        return None, "待补图"

    path_value = str(candidate.get("path") or "").strip()
    if path_value:
        src = Path(path_value)
        if src.is_file():
            try:
                return open_image_file(src), ""
            except Exception:
                return None, "图片无效"

    url = str(candidate.get("url") or "").strip()
    if is_http_url(url):
        try:
            payload = download_remote_image(url)
        except RemoteImageDownloadError:
            return None, "图片下载失败"
        try:
            return open_image_bytes(payload), ""
        except Exception:
            return None, "图片无效"

    return None, "待补图"


def selected_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    candidates = row.get("candidates")
    if isinstance(candidates, list) and candidates:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            return candidate
    return None


def normalize_selected_rows(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    selected: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            selected.add(int(item))
        except (TypeError, ValueError):
            continue
    return selected


def normalize_selected_ids(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def row_identity_values(row: dict[str, Any], row_number: int) -> set[str]:
    values = {str(row_number)}
    for key in ("row", "id", "itemId", "rowId", "dishId", "skuId"):
        value = row.get(key)
        if value is not None and str(value).strip():
            values.add(str(value).strip())
    candidate = selected_candidate(row)
    if candidate:
        for key in ("imageId", "id", "objectKey"):
            value = candidate.get(key)
            if value is not None and str(value).strip():
                values.add(str(value).strip())
    return values


def selected_filter_matches(row: dict[str, Any], row_number: int, selected_rows: set[int], selected_ids: set[str]) -> bool:
    row_number_values = {row_number}
    try:
        row_number_values.add(int(row.get("row")))
    except (TypeError, ValueError):
        pass
    if selected_rows and row_number_values.intersection(selected_rows):
        return True
    if selected_ids and row_identity_values(row, row_number).intersection(selected_ids):
        return True
    return False


def should_export_row(
    row: dict[str, Any],
    row_number: int,
    selected_rows: set[int],
    scope: str,
    selected_ids: set[str] | None = None,
) -> bool:
    selected_ids = selected_ids or set()
    candidate = selected_candidate(row)
    action = row.get("backgroundAction")
    normalized_scope = str(scope or "all")
    if selected_rows or selected_ids:
        if not selected_filter_matches(row, row_number, selected_rows, selected_ids):
            return False
    elif normalized_scope == "selected":
        return False
    if normalized_scope == "direct" and action != "背景一致，直接复用":
        return False
    if normalized_scope == "need_bg" and action != "需抠图换背景":
        return False
    if normalized_scope == "missing" and candidate is not None:
        return False
    if normalized_scope == "single" and row.get("kind") != "单品":
        return False
    if normalized_scope == "combo" and row.get("kind") != "套餐/组合":
        return False
    if normalized_scope == "other" and row.get("kind") in {"单品", "套餐/组合"}:
        return False
    return True


def unique_filename(name: str, ext: str, used_names: set[str]) -> str:
    stem = safe_filename(name or "dish")
    suffix = ext if ext.startswith(".") else f".{ext}"
    candidate = f"{stem}{suffix}"
    counter = 2
    while candidate.lower() in used_names:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(candidate.lower())
    return candidate


def report_row(
    row: dict[str, Any],
    platform: dict[str, Any] | None = None,
    file_size: int | None = None,
    delivery_file: str = "",
    watermark_enabled: bool = False,
    status: str = "待补图",
) -> dict[str, Any]:
    if platform is None:
        data = {
            "菜品名": row.get("name", ""),
            "分类": row.get("category", ""),
            "类型": row.get("kind", ""),
            "平台": "",
            "尺寸": "",
            "文件大小KB": "",
            "平台上限KB": "",
            "图片状态": status,
            "预计积分": row.get("points", ""),
            "品牌水印": "未添加",
            "交付文件": "",
        }
    else:
        data = {
            "菜品名": row.get("name", ""),
            "分类": row.get("category", ""),
            "类型": row.get("kind", ""),
            "平台": platform["name"],
            "尺寸": f"{platform['width']}x{platform['height']}",
            "文件大小KB": round(int(file_size or 0) / 1024, 1),
            "平台上限KB": platform["maxKB"],
            "图片状态": "已生成",
            "预计积分": row.get("points", ""),
            "品牌水印": "已添加" if watermark_enabled else "未添加",
            "交付文件": delivery_file,
        }
    return {column: data.get(column, "") for column in REPORT_COLUMNS}


def export_delivery_zip(
    plan_results: list[dict[str, Any]],
    export_dir: Path,
    scope: str = "all",
    selected_rows: list[int] | None = None,
    selected_ids: list[str] | None = None,
    image_format: str = "jpg",
    watermark: dict[str, Any] | None = None,
    platforms: list[str] | str | None = None,
    run_name: str | None = None,
) -> dict[str, Any]:
    export_dir.mkdir(parents=True, exist_ok=True)
    selected = normalize_selected_rows(selected_rows)
    selected_id_set = normalize_selected_ids(selected_ids)
    selected_platforms = parse_platforms(platforms)
    watermark_enabled = isinstance(watermark, dict) and bool(watermark.get("enabled"))

    safe_run_name = safe_filename(run_name) if run_name else f"export_{int(time.time())}_{time.time_ns() % 1_000_000_000:09d}"
    run_dir = export_dir / safe_run_name
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    image_count = 0
    used_names_by_platform: dict[str, set[str]] = {}
    for row_number, row in enumerate(plan_results, start=1):
        if not should_export_row(row, row_number, selected, str(scope or "all"), selected_id_set):
            continue

        raw_img, image_status = load_candidate_image(selected_candidate(row))
        if raw_img is None:
            rows.append(report_row(row, status=image_status))
            continue

        for platform_id in selected_platforms:
            spec = PLATFORMS[platform_id]
            normalized_format, ext, _ = normalize_image_format(image_format, platform_id)
            platform_dir = image_dir / platform_folder_name(platform_id, spec)
            used_names = used_names_by_platform.setdefault(platform_id, set())
            target = platform_dir / unique_filename(str(row.get("name") or "dish"), ext, used_names)
            img = prepare_platform_image(raw_img, platform_id, watermark)
            file_size = save_platform_image(img, target, int(spec["maxKB"]), normalized_format)
            image_count += 1
            delivery_file = f"{platform_dir.name}/{target.name}"
            rows.append(report_row(row, spec, file_size, delivery_file, watermark_enabled))

    report = run_dir / "delivery_report.xlsx"
    pd.DataFrame(rows, columns=REPORT_COLUMNS).to_excel(report, index=False)

    zip_path = run_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(report, report.name)
        for file in sorted(image_dir.rglob("*")):
            if file.is_file():
                zf.write(file, f"images/{file.relative_to(image_dir).as_posix()}")

    return {
        "rows": len(rows),
        "images": image_count,
        "platforms": selected_platforms,
        "extraPlatformPoints": platform_extra_points(selected_platforms),
        "watermark": watermark_enabled,
        "download": f"/download/{zip_path.relative_to(export_dir).as_posix()}",
    }
