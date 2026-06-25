from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from image_pipeline import export_delivery_zip  # noqa: E402


def placeholder_image(path: Path, size: tuple[int, int], color: tuple[int, int, int], label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (246, 244, 238))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((18, 18, size[0] - 18, size[1] - 18), radius=26, fill=color, outline=(35, 35, 35), width=5)
    draw.ellipse((size[0] // 3, size[1] // 5, size[0] * 2 // 3, size[1] * 4 // 5), fill=(255, 246, 210))
    draw.text((32, 32), label, fill=(20, 24, 30))
    img.save(path, "PNG")


def placeholder_plan(source_dir: Path) -> list[dict[str, Any]]:
    specs = [
        ("dish-001", "招牌/测试菜", "单品", (1200, 700), (215, 73, 52)),
        ("dish-002", "招牌/测试菜", "套餐/组合", (720, 960), (46, 137, 108)),
        ("dish-003", "饮品:酸梅汤", "其他", (900, 900), (68, 102, 190)),
    ]
    rows = []
    for index, (row_id, name, kind, size, color) in enumerate(specs, start=1):
        path = source_dir / f"{row_id}.png"
        placeholder_image(path, size, color, name)
        rows.append(
            {
                "id": row_id,
                "row": index,
                "name": name,
                "category": "smoke",
                "kind": kind,
                "points": 0,
                "backgroundAction": "背景一致，直接复用",
                "candidates": [{"imageId": f"image-{row_id}", "path": str(path)}],
            }
        )
    return rows


def run_smoke_export(export_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="waimai_export_smoke_"))
    target_export_dir = Path(export_dir) if export_dir is not None else root / "exports"
    result = export_delivery_zip(
        placeholder_plan(root / "sources"),
        target_export_dir,
        platforms=["meituan", "taobao", "jd"],
        watermark={"enabled": True, "type": "text", "text": "SMOKE", "position": "bottom-right"},
        run_name="smoke_export",
    )
    zip_path = target_export_dir / result["download"].split("/download/", 1)[1]
    result["zipPath"] = str(zip_path)
    result["zipBytes"] = zip_path.stat().st_size if zip_path.exists() else 0
    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            result["zipEntries"] = len(names)
            result["zipImages"] = sum(1 for name in names if name.startswith("images/") and not name.endswith("/"))
            result["hasDeliveryReport"] = "delivery_report.xlsx" in names
    return result


if __name__ == "__main__":
    print(json.dumps(run_smoke_export(), ensure_ascii=False, indent=2))
