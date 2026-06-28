from __future__ import annotations

import base64
import io
import re
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

PLATFORMS = {
    "meituan": {"name": "美团外卖", "width": 800, "height": 600, "maxKB": 5120, "default": True},
    "taobao": {"name": "淘宝外卖/饿了么", "width": 800, "height": 800, "maxKB": 20480, "default": False},
    "jd": {"name": "京东外卖/京东秒送", "width": 800, "height": 800, "maxKB": 5120, "default": False},
}

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

MAX_LOGO_DATA_URL_CHARS = 1_500_000
MAX_LOGO_BYTES = 1_000_000
MAX_LOGO_PIXELS = 2_000_000
MAX_EXPORT_IMAGE_BYTES = 25 * 1024 * 1024
MAX_EXPORT_IMAGE_PIXELS = 24_000_000
MAX_EXPORT_IMAGE_SIDE = 12_000
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}
QUALITY_MIN_SIDE = 320
QUALITY_SAMPLE_SIZE = (96, 96)
QUALITY_MIN_PASS_SCORE = 0.65


def safe_filename(name: str) -> str:
    value = unicodedata.normalize("NFKC", str(name))
    value = re.sub(r"[/:*?\"<>|\\]+", "_", value)
    return re.sub(r"\s+", " ", value).strip()[:90] or "file"


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
    return positions.get(position, positions["bottom-right"])


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
    logo.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
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
    mark_type = str(settings.get("type") or "text")
    mark = make_logo_watermark(str(settings.get("logoData") or ""), base.width) if mark_type == "logo" else None
    if mark is None:
        mark = make_text_watermark(str(settings.get("text") or "品牌水印"), base.width, str(settings.get("color") or "black"))
    if mark.width <= 0 or mark.height <= 0:
        return base
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if str(settings.get("pattern") or "corner") == "tile":
        if mark_type == "text":
            mark = mark.rotate(-22, expand=True)
        paste_tiled(overlay, mark)
    else:
        margin = max(24, base.width // 34)
        position = str(settings.get("position") or "bottom-right")
        overlay.alpha_composite(mark, watermark_position(base.size, mark.size, position, margin))
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
    return out or ["meituan"]


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


def save_platform_image(img: Image.Image, target: Path, max_kb: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = max(64, int(max_kb)) * 1024
    rgb = img.convert("RGB")
    for quality in list(range(92, 34, -5)) + [30, 25, 20]:
        rgb.save(target, "JPEG", quality=quality, optimize=True, progressive=True, subsampling=2)
        if target.stat().st_size <= max_bytes:
            return target.stat().st_size
    return target.stat().st_size


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


def assess_generated_asset_quality(source: Image.Image | str | Path) -> dict[str, Any]:
    """Deterministic local quality gate for generated reusable assets."""
    img = _quality_source_image(source)
    width, height = img.size
    sample = img.resize(QUALITY_SAMPLE_SIZE, Image.Resampling.BILINEAR)
    rgb = sample.convert("RGB")
    rgba_pixels = list(sample.getdata())
    rgb_pixels = list(rgb.getdata())
    total = max(1, len(rgb_pixels))

    stat = ImageStat.Stat(rgb)
    mean_stddev = sum(float(value) for value in stat.stddev) / 3.0
    dominant_ratio = _dominant_color_ratio(rgb)
    transparent_ratio = sum(1 for _r, _g, _b, alpha in rgba_pixels if alpha < 245) / total
    edge_pixels = _edge_rgb_pixels(rgb)
    edge_color = _mean_rgb(edge_pixels)
    edge_stddev = _rgb_stddev(edge_pixels)
    content_bbox, content_ratio = _content_bbox_from_edge(rgba_pixels, sample.size, edge_color)

    reasons: list[str] = []
    if min(width, height) < QUALITY_MIN_SIDE:
        reasons.append("too_small")
    if transparent_ratio > 0.02:
        reasons.append("background_not_filled")
    if mean_stddev < 6.0 or dominant_ratio >= 0.985:
        reasons.append("solid_or_placeholder")

    bbox_metrics = {
        "content_area_ratio": round(content_ratio, 4),
        "content_width_ratio": 0.0,
        "content_height_ratio": 0.0,
        "content_margin_min_ratio": 0.0,
    }
    if content_bbox is not None:
        x0, y0, x1, y1 = content_bbox
        sample_w, sample_h = sample.size
        width_ratio = (x1 - x0) / sample_w
        height_ratio = (y1 - y0) / sample_h
        margin_min_ratio = min(x0 / sample_w, y0 / sample_h, (sample_w - x1) / sample_w, (sample_h - y1) / sample_h)
        bbox_metrics.update(
            {
                "content_width_ratio": round(width_ratio, 4),
                "content_height_ratio": round(height_ratio, 4),
                "content_margin_min_ratio": round(margin_min_ratio, 4),
            }
        )
        if edge_stddev < 10.0 and width_ratio <= 0.58 and height_ratio <= 0.58 and margin_min_ratio >= 0.08:
            reasons.append("small_center_frame")
    elif "solid_or_placeholder" not in reasons:
        reasons.append("solid_or_placeholder")

    reasons = _unique_quality_reasons(reasons)
    penalty = {
        "too_small": 0.2,
        "background_not_filled": 0.35,
        "solid_or_placeholder": 0.65,
        "small_center_frame": 0.45,
    }
    score = max(0.0, min(1.0, 1.0 - sum(penalty.get(reason, 0.25) for reason in reasons)))
    score = round(score, 3)
    passed = not reasons and score >= QUALITY_MIN_PASS_SCORE

    return {
        "passed": passed,
        "status": "passed" if passed else "failed",
        "score": score,
        "quality_score": score,
        "reasons": reasons,
        "metrics": {
            "width": width,
            "height": height,
            "mean_stddev": round(mean_stddev, 3),
            "dominant_color_ratio": round(dominant_ratio, 4),
            "transparent_ratio": round(transparent_ratio, 4),
            "edge_stddev": round(edge_stddev, 3),
            **bbox_metrics,
        },
    }


def _quality_source_image(source: Image.Image | str | Path) -> Image.Image:
    if isinstance(source, Image.Image):
        return ImageOps.exif_transpose(source).convert("RGBA")

    path = Path(source)
    with Image.open(path) as img:
        validate_image_bounds(img)
        img.load()
        return ImageOps.exif_transpose(img).convert("RGBA")


def _dominant_color_ratio(img: Image.Image) -> float:
    colors = img.getcolors(maxcolors=img.width * img.height)
    if not colors:
        return 0.0
    return max(count for count, _color in colors) / max(1, img.width * img.height)


def _edge_rgb_pixels(img: Image.Image) -> list[tuple[int, int, int]]:
    band = max(3, int(min(img.size) * 0.08))
    pixels: list[tuple[int, int, int]] = []
    width, height = img.size
    for y in range(height):
        for x in range(width):
            if x < band or x >= width - band or y < band or y >= height - band:
                pixels.append(img.getpixel((x, y)))
    return pixels


def _mean_rgb(pixels: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    total = max(1, len(pixels))
    return (
        sum(pixel[0] for pixel in pixels) / total,
        sum(pixel[1] for pixel in pixels) / total,
        sum(pixel[2] for pixel in pixels) / total,
    )


def _rgb_stddev(pixels: list[tuple[int, int, int]]) -> float:
    if not pixels:
        return 0.0
    mean = _mean_rgb(pixels)
    channel_variance = [
        sum((pixel[index] - mean[index]) ** 2 for pixel in pixels) / len(pixels)
        for index in range(3)
    ]
    return sum(value ** 0.5 for value in channel_variance) / 3.0


def _content_bbox_from_edge(
    rgba_pixels: list[tuple[int, int, int, int]],
    size: tuple[int, int],
    edge_color: tuple[float, float, float],
) -> tuple[tuple[int, int, int, int] | None, float]:
    width, height = size
    mask = []
    for red, green, blue, alpha in rgba_pixels:
        distance = abs(red - edge_color[0]) + abs(green - edge_color[1]) + abs(blue - edge_color[2])
        mask.append(255 if alpha >= 245 and distance >= 45 else 0)
    mask_img = Image.new("L", size)
    mask_img.putdata(mask)
    content_pixels = sum(1 for value in mask if value)
    return mask_img.getbbox(), content_pixels / max(1, width * height)


def _unique_quality_reasons(reasons: list[str]) -> list[str]:
    output: list[str] = []
    for reason in reasons:
        if reason not in output:
            output.append(reason)
    return output


def selected_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    candidates = row.get("candidates")
    if isinstance(candidates, list) and candidates:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            return candidate
    return None


def should_export_row(row: dict[str, Any], row_number: int, selected_rows: set[int], scope: str) -> bool:
    candidate = selected_candidate(row)
    action = row.get("backgroundAction")
    if selected_rows and row_number not in selected_rows:
        return False
    if scope == "direct" and action != "背景一致，直接复用":
        return False
    if scope == "need_bg" and action != "需抠图换背景":
        return False
    if scope == "missing" and candidate is not None:
        return False
    if scope == "single" and row.get("kind") != "单品":
        return False
    if scope == "combo" and row.get("kind") != "套餐/组合":
        return False
    return True


def report_row(
    row: dict[str, Any],
    platform: dict[str, Any] | None = None,
    file_size: int | None = None,
    delivery_file: str = "",
    watermark_enabled: bool = False,
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
            "图片状态": "待补图",
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
    image_format: str = "jpg",
    watermark: dict[str, Any] | None = None,
    platforms: list[str] | str | None = None,
    run_name: str | None = None,
) -> dict[str, Any]:
    export_dir.mkdir(parents=True, exist_ok=True)
    selected = {int(item) for item in selected_rows or []}
    selected_platforms = parse_platforms(platforms)
    watermark_enabled = isinstance(watermark, dict) and bool(watermark.get("enabled"))
    normalized_format = str(image_format or "jpg").lower()
    ext = ".jpg" if normalized_format in {"jpg", "jpeg"} else ".jpg"

    run_dir = export_dir / (run_name or f"export_{int(time.time())}_{time.time_ns() % 1_000_000_000:09d}")
    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    image_count = 0
    for row_number, row in enumerate(plan_results, start=1):
        if not should_export_row(row, row_number, selected, str(scope or "all")):
            continue

        candidate = selected_candidate(row)
        src = Path(str(candidate.get("path", ""))) if candidate else None
        if src is None or not src.is_file():
            rows.append(report_row(row))
            continue

        try:
            raw_img = open_export_image(src)
        except Exception:
            rows.append(report_row(row))
            continue
        with raw_img:
            for platform_id in selected_platforms:
                spec = PLATFORMS[platform_id]
                platform_dir = image_dir / f"{platform_id}_{spec['name']}_{spec['width']}x{spec['height']}"
                target = platform_dir / f"{row_number:03d}_{safe_filename(str(row.get('name') or 'dish'))}{ext}"
                img = fit_to_platform(raw_img, platform_id)
                img = apply_watermark(img, watermark)
                file_size = save_platform_image(img, target, int(spec["maxKB"]))
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
        "watermark": watermark_enabled,
        "download": f"/download/{zip_path.relative_to(export_dir).as_posix()}",
    }
