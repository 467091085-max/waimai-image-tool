from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = {
    "README.md": ROOT / "README.md",
    "PRODUCT_ACCEPTANCE.md": ROOT / "PRODUCT_ACCEPTANCE.md",
    "DELIVERY_REPORT.md": ROOT / "DELIVERY_REPORT.md",
}


class V7AcceptanceDocsTests(unittest.TestCase):
    def test_docs_include_render_env_and_smoke_release_gates(self) -> None:
        common_terms = [
            "scripts/smoke_product_flow.py",
            "WAIMAI_ACCEPTANCE_LIVE=1",
            "--no-live-generate",
            "--live-generate",
            "--limit 1",
            "TENCENT_HUNYUAN_ENABLED",
            "TENCENTCLOUD_SECRET_ID",
            "TENCENTCLOUD_SECRET_KEY",
            "TENCENT_COS_BUCKET",
            "TENCENT_COS_REGION",
            "COS_LIBRARY_INDEX_URL",
            "/api/library-status",
            "/api/tencent-status",
            "gunicorn app:app",
        ]
        v7_terms = [
            "GALLERY_UPLOAD_TOKEN",
            "remoteIndex",
            "remoteImages",
            "因 Render env 未配置而跳过",
        ]
        for name, path in DOCS.items():
            text = path.read_text(encoding="utf-8")
            required_terms = common_terms + (v7_terms if name != "README.md" else [])
            missing = [term for term in required_terms if term not in text]
            self.assertEqual(missing, [], f"{name} missing Render acceptance terms")

    def test_acceptance_doc_names_v7_e2e_surfaces(self) -> None:
        text = DOCS["PRODUCT_ACCEPTANCE.md"].read_text(encoding="utf-8")
        required_surfaces = [
            "真实图库",
            "上传菜单",
            "6 张背景",
            "6 张免费单品样图",
            "正式生图 job",
            "图片预览",
            "单张修改",
            "平台导出",
            "打包 ZIP",
            "积分扣费",
            "Hunyuan live",
            "library-status",
        ]
        missing = [surface for surface in required_surfaces if surface not in text]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
