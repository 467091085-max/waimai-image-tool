from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
STYLE_IDS = ("style-1", "style-2", "style-3", "style-4", "style-5")
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
)


def safe_part(value: str, fallback: str = "item") -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[/:*?\"<>|\\]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:70] or fallback


def priority(path: Path) -> tuple[int, str]:
    stem = path.stem.lower()
    for skip in SKIP_TERMS:
        if skip in stem:
            return (99, str(path))
    for idx, term in enumerate(PRIORITY_TERMS):
        if term.lower() in stem:
            return (idx, str(path))
    return (len(PRIORITY_TERMS), str(path))


def style_for_item(store: str, dish: str) -> str:
    digest = hashlib.sha1(f"{store}:{dish}".encode("utf-8")).digest()[0]
    return STYLE_IDS[digest % len(STYLE_IDS)]


def iter_images(source: Path) -> list[Path]:
    files = [path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTS]
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
    for source_file in iter_images(source):
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
        style_id = style_for_item(store, dish)
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
