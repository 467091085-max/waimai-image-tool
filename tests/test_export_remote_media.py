from __future__ import annotations

import io
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw

from image_pipeline import PLATFORMS, REPORT_COLUMNS, export_delivery_zip


def encoded_image(image_format: str, color: tuple[int, int, int]) -> tuple[bytes, str]:
    img = Image.new("RGBA", (960, 640), (244, 240, 232, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((100, 80, 860, 560), fill=(*color, 255))
    draw.ellipse((340, 160, 620, 520), fill=(255, 245, 210, 255))

    buf = io.BytesIO()
    normalized = image_format.upper()
    if normalized == "JPG":
        normalized = "JPEG"
    if normalized == "JPEG":
        img.convert("RGB").save(buf, normalized, quality=90)
        return buf.getvalue(), "image/jpeg"
    img.save(buf, normalized)
    return buf.getvalue(), f"image/{image_format.lower()}"


class ImageBytesServer:
    def __init__(self, routes: dict[str, tuple[bytes, str]]) -> None:
        self.routes = routes

        routes_for_handler = routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                route = routes_for_handler.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return

                payload, content_type = route
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "ImageBytesServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{path}"


class ExportRemoteMediaTest(unittest.TestCase):
    def test_export_downloads_remote_jpg_png_and_webp_candidates(self) -> None:
        routes = {
            "/remote.jpg": encoded_image("jpg", (210, 74, 58)),
            "/remote.png": encoded_image("png", (48, 136, 108)),
            "/remote.webp": encoded_image("webp", (68, 102, 190)),
        }

        with tempfile.TemporaryDirectory() as tmp, ImageBytesServer(routes) as server:
            root = Path(tmp)
            plan_results = []
            for index, path in enumerate(routes, start=1):
                ext = path.rsplit(".", 1)[1]
                plan_results.append(
                    {
                        "id": f"remote-{ext}",
                        "row": index,
                        "name": f"远程图片{ext}",
                        "category": "remote",
                        "kind": "单品",
                        "points": index,
                        "backgroundAction": "背景一致，直接复用",
                        "candidates": [
                            {
                                "imageId": f"candidate-{ext}",
                                "path": str(root / f"missing-{ext}.jpg"),
                                "url": server.url(path),
                            }
                        ],
                    }
                )

            result = export_delivery_zip(
                plan_results,
                root / "exports",
                platforms=["meituan", "taobao", "jd"],
                watermark={"enabled": True, "type": "text", "text": "REMOTE", "position": "bottom-right"},
                run_name="remote_media",
            )

            self.assertEqual(result["images"], 9)
            self.assertEqual(result["rows"], 9)
            zip_path = root / "exports" / result["download"].split("/download/", 1)[1]

            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                image_names = sorted(name for name in names if name.startswith("images/") and name.endswith(".jpg"))
                self.assertIn("delivery_report.xlsx", names)
                self.assertEqual(len(image_names), 9)

                report = pd.read_excel(io.BytesIO(zf.read("delivery_report.xlsx")))
                self.assertEqual(list(report.columns), REPORT_COLUMNS)
                self.assertEqual(set(report["图片状态"]), {"已生成"})
                self.assertEqual(set(report["品牌水印"]), {"已添加"})

                for name in image_names:
                    platform_id = name.split("/")[1].split("_", 1)[0]
                    spec = PLATFORMS[platform_id]
                    payload = zf.read(name)
                    img = Image.open(io.BytesIO(payload))
                    self.assertEqual(img.format, "JPEG")
                    self.assertEqual(img.mode, "RGB")
                    self.assertEqual(img.size, (spec["width"], spec["height"]))
                    self.assertLessEqual(len(payload), spec["maxKB"] * 1024)

    def test_remote_download_failure_is_reported_in_export_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ImageBytesServer({}) as server:
            root = Path(tmp)
            result = export_delivery_zip(
                [
                    {
                        "id": "remote-missing",
                        "row": 1,
                        "name": "远程缺失图片",
                        "category": "remote",
                        "kind": "单品",
                        "points": 0,
                        "backgroundAction": "背景一致，直接复用",
                        "candidates": [{"path": str(root / "missing.jpg"), "url": server.url("/missing.jpg")}],
                    }
                ],
                root / "exports",
                platforms=["meituan"],
                run_name="remote_failure",
            )

            self.assertEqual(result["images"], 0)
            self.assertEqual(result["rows"], 1)
            zip_path = root / "exports" / result["download"].split("/download/", 1)[1]

            with zipfile.ZipFile(zip_path) as zf:
                self.assertEqual([name for name in zf.namelist() if name.startswith("images/")], [])
                report = pd.read_excel(io.BytesIO(zf.read("delivery_report.xlsx")))
                self.assertEqual(report.iloc[0]["图片状态"], "图片下载失败")


if __name__ == "__main__":
    unittest.main()
