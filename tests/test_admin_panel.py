from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Flask

from admin_panel import AdminDependencies, create_admin_blueprint


ROOT = Path(__file__).resolve().parents[1]


def make_app(upload_dir: Path) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )

    images = [
        SimpleNamespace(
            image_id="clean001",
            path=upload_dir / "clean.jpg",
            store="测试门店A",
            dish="辣椒炒肉",
            style_id="style-clean",
            source="clean",
            reusable=True,
        ),
        SimpleNamespace(
            image_id="watermark001",
            path=upload_dir / "watermark.jpg",
            store="测试门店B",
            dish="小炒黄牛肉",
            style_id="style-watermark",
            source="watermark",
            reusable=False,
        ),
        SimpleNamespace(
            image_id="internal001",
            path=upload_dir / "internal.jpg",
            store="演示门店",
            dish="茄子肉末",
            style_id="style-1",
            source="internal",
            reusable=True,
        ),
    ]

    def parse_menu(path: Path | None = None) -> dict[str, Any]:
        if path is not None and path.name == "broken.xlsx":
            raise ValueError("bad workbook")
        return {
            "file": path.name if path else "demo_menu.xlsx",
            "store": "测试门店",
            "count": 3,
            "kindCounts": {"single": 1, "combo": 1, "snack": 1, "total": 3},
            "sheets": [{"sheet": "菜单", "headerRow": 1, "items": 3, "score": 166.5}],
            "errors": [],
            "items": [{"name": "辣椒炒肉"}],
            "demo": path is None,
        }

    app.register_blueprint(
        create_admin_blueprint(
            AdminDependencies(
                library_images=lambda: images,
                media_url_for_path=lambda path: f"/media/{path.name}",
                current_menu_path=lambda: upload_dir / "ok.xlsx",
                parse_menu=parse_menu,
                upload_dir=upload_dir,
            )
        )
    )
    return app


class AdminPanelTests(unittest.TestCase):
    def test_admin_page_loads_without_customer_homepage_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/admin")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("/static/admin.css", body)
        self.assertIn("/static/admin.js", body)
        self.assertNotIn("/static/app.js", body)

    def test_library_sample_returns_counts_samples_and_no_paths_or_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_app(Path(tmp)).test_client()
            response = client.get("/api/admin/library-sample?limit=10")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["summary"]["total"], 3)
        self.assertEqual(data["sources"]["clean"], 1)
        self.assertEqual(data["sources"]["watermark"], 1)
        self.assertEqual(data["sources"]["internal"], 1)
        self.assertEqual({sample["source"] for sample in data["samples"]}, {"clean", "watermark", "internal"})
        for sample in data["samples"]:
            self.assertGreaterEqual(sample.keys(), {"imageId", "dishName", "store", "source", "reusable", "url"})
            self.assertNotIn("path", sample)
        payload = json.dumps(data, ensure_ascii=False).lower()
        self.assertNotIn("secret", payload)

    def test_menu_audit_returns_current_and_upload_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp)
            (upload_dir / "ok.xlsx").write_text("fake", encoding="utf-8")
            (upload_dir / "broken.xlsx").write_text("fake", encoding="utf-8")
            (upload_dir / "ignore.txt").write_text("fake", encoding="utf-8")
            client = make_app(upload_dir).test_client()
            response = client.get("/api/admin/menu-audit")

            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["current"]["available"])
            self.assertNotIn("items", data["current"])
            self.assertEqual(data["audit"]["files"], 2)
            self.assertEqual(data["audit"]["parsed"], 1)
            self.assertEqual(data["audit"]["failed"], 1)
            self.assertEqual(data["audit"]["totalItems"], 3)
            self.assertEqual(data["parser"]["supportedExtensions"], [".xls", ".xlsx"])
            self.assertNotIn(str(upload_dir), json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
