from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
STYLE_IDS = ("style-1", "style-2", "style-3", "style-4", "style-5", "style-6")
PRIORITY_TERMS = (
    "辣椒炒肉",
    "小炒黄牛肉",
    "黄牛肉",
    "茄子",
    "盖码饭",
    "盖浇饭",
    "木桶饭",
    "一碗香",
    "香干炒肉",
    "炒鸡",
    "米饭",
    "鸡腿",
    "豆角",
    "牛肉",
    "小笼包",
    "豆浆",
    "汤圆",
    "饺",
    "烧麦",
    "汤",
    "粥",
)
SKIP_TERMS = (
    "发票",
    "门店",
    "收藏",
    "下单",
    "承诺",
    "粉丝",
    "福利",
    "更安全",
    "欢迎到店",
    "味道(taste)",
    "勿点",
    "温馨提示",
    "提示",
    "背景",
    "扩背景",
    "不需要",
    "需加购",
    "默认不需要",
    "电子餐饮",
    "票",
    "P图",
    "p图",
    "拷贝",
)
LOW_PRIORITY_TERMS = (
    "米饭",
    "白饭",
    "大米饭",
    "饮料",
    "饮品",
    "可乐",
    "雪碧",
    "美年达",
    "王老吉",
    "豆浆",
    "矿泉水",
    "纯净水",
    "小料",
    "调料",
    "蘸料",
    "辣椒包",
    "醋包",
    "生抽包",
    "白糖",
    "香菜沫",
    "餐具",
    "纸巾",
)


def safe_part(value: str, fallback: str = "item") -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[/:*?\"<>|\\]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:70] or fallback


def should_skip(path: Path) -> bool:
    stem = path.stem.lower()
    normalized = unicodedata.normalize("NFKC", stem)
    if re.search(r"20\d{2}[.\-_年]\d{1,2}[.\-_月]\d{1,2}", normalized):
        return True
    return any(skip.lower() in stem or skip.lower() in normalized for skip in SKIP_TERMS)


def priority(path: Path) -> tuple[int, str]:
    stem = path.stem.lower()
    for skip in SKIP_TERMS:
        if skip in stem:
            return (99, str(path))
    normalized = unicodedata.normalize("NFKC", stem)
    for low_term in LOW_PRIORITY_TERMS:
        if low_term.lower() in stem or low_term.lower() in normalized:
            return (len(PRIORITY_TERMS) + 20, str(path))
    for idx, term in enumerate(PRIORITY_TERMS):
        if term.lower() in stem:
            return (idx, str(path))
    return (len(PRIORITY_TERMS), str(path))


def style_for_item(store: str, dish: str) -> str:
    digest = hashlib.sha1(f"{store}:{dish}".encode("utf-8")).digest()[0]
    return STYLE_IDS[digest % len(STYLE_IDS)]


def background_signature(source: Path) -> str:
    try:
        with Image.open(source) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.thumbnail((96, 96), Image.Resampling.BILINEAR)
            width, height = image.size
            strip = max(2, min(width, height) // 8)
            pixels = []
            for box in (
                (0, 0, width, strip),
                (0, max(0, height - strip), width, height),
                (0, 0, strip, height),
                (max(0, width - strip), 0, width, height),
            ):
                pixels.extend(image.crop(box).getdata())
            if not pixels:
                return hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
            count = len(pixels)
            red = sum(pixel[0] for pixel in pixels) // count
            green = sum(pixel[1] for pixel in pixels) // count
            blue = sum(pixel[2] for pixel in pixels) // count
            return f"{red // 32}-{green // 32}-{blue // 32}"
    except Exception:
        return hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]


def style_map_for_images(files: list[Path]) -> dict[str, str]:
    signatures: dict[str, int] = {}
    for source_file in files:
        signature = background_signature(source_file)
        signatures[signature] = signatures.get(signature, 0) + 1
    ordered = sorted(signatures, key=lambda item: (-signatures[item], item))
    return {signature: STYLE_IDS[index % len(STYLE_IDS)] for index, signature in enumerate(ordered)}


def iter_images(source: Path) -> list[Path]:
    files = [
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS and not should_skip(path)
    ]
    return sorted(files, key=priority)


def convert_image(source: Path, target: Path, max_side: int, quality: int) -> bool:
    try:
        with Image.open(source) as raw:
            img = ImageOps.exif_transpose(raw).convert("RGB")
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, "JPEG", quality=quality, optimize=True, progressive=True)
        return True
    except Exception as exc:
        print(f"SKIP {source}: {exc}")
        return False


def import_seed(source: Path, output: Path, limit: int, max_side: int, quality: int, clean: bool) -> int:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if clean and output.exists():
        for child in output.glob("seed_*"):
            if child.is_dir():
                shutil.rmtree(child)
    count = 0
    seen: set[str] = set()
    source_files = iter_images(source)
    style_map = style_map_for_images(source_files)
    for source_file in source_files:
        if count >= limit:
            break
        rel = source_file.relative_to(source)
        store = rel.parts[0] if len(rel.parts) > 1 else source.name
        dish = safe_part(source_file.stem, "dish")
        key = f"{store}:{dish}"
        if key in seen:
            continue
        seen.add(key)
        store_slug = safe_part(store, "store")
        store_hash = hashlib.sha1(store.encode("utf-8")).hexdigest()[:6]
        style_id = style_map.get(background_signature(source_file), style_for_item(store, dish))
        target = output / f"seed_{store_hash}_{store_slug}" / style_id / f"{dish}.jpg"
        if convert_image(source_file, target, max_side=max_side, quality=quality):
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Import compressed real cleanpic images as deployable seed library.")
    parser.add_argument("--source", default="/Users/guiguixiaxia/Documents/cleanpic")
    parser.add_argument("--output", default="data/library")
    parser.add_argument("--limit", type=int, default=360)
    parser.add_argument("--max-side", type=int, default=900)
    parser.add_argument("--quality", type=int, default=82)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()

    count = import_seed(
        Path(args.source),
        Path(args.output),
        limit=max(1, args.limit),
        max_side=max(320, args.max_side),
        quality=max(45, min(92, args.quality)),
        clean=not args.no_clean,
    )
    print(f"imported={count} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
