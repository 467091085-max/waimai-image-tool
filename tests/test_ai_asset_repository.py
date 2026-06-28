from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_asset_repository import AIAssetRepository, GENERATION_REQUIRED_STATUS


def asset_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "asset_id": "asset-1",
        "kind": "product_image",
        "category": "轻食健康餐",
        "style_id": "style-1",
        "product_name": "牛油果鸡胸沙拉",
        "normalized_product_name": "牛油果鸡胸沙拉",
        "keywords": ["牛油果", "鸡胸", "沙拉"],
        "match_names": ["牛油果鸡胸沙拉", "鸡胸沙拉"],
        "source": "TextToImageLite",
        "provider": "tencent-hunyuan",
        "quality": "standard",
        "quality_score": 0.82,
        "quality_status": "passed",
        "quality_reasons": [],
        "object_key": "products/light/style-1/salad.jpg",
        "local_path": "/tmp/salad.jpg",
        "sha256": "a" * 64,
        "created_at": "2026-06-28T00:00:00Z",
        "status": "approved",
    }
    record.update(overrides)
    if "product_name" in overrides and "normalized_product_name" not in overrides:
        record["normalized_product_name"] = overrides["product_name"]
    return record


class AIAssetRepositoryTest(unittest.TestCase):
    def test_upsert_dedups_by_identity_and_rewrites_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.jsonl"
            repo = AIAssetRepository(manifest)

            repo.upsert(asset_record())
            updated = repo.upsert(
                asset_record(
                    asset_id="asset-duplicate",
                    product_name="新版牛油果鸡胸沙拉",
                    keywords=["牛油果", "鸡胸", "新版"],
                    quality_score=0.95,
                    created_at="2026-06-28T01:00:00Z",
                )
            )

            records = repo.list_assets()
            lines = manifest.read_text(encoding="utf-8").splitlines()

        self.assertEqual(updated["asset_id"], "asset-1")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(records[0]["product_name"], "新版牛油果鸡胸沙拉")
        self.assertEqual(records[0]["quality_score"], 0.95)
        self.assertEqual(json.loads(lines[0])["asset_id"], "asset-1")

    def test_list_assets_filters_by_kind_category_style_status_and_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            repo.upsert(asset_record(asset_id="salad", product_name="牛油果鸡胸沙拉", keywords=["鸡胸", "沙拉"], sha256="1" * 64))
            repo.upsert(
                asset_record(
                    asset_id="noodle",
                    category="粉面米线",
                    product_name="桂林米粉",
                    keywords=["米粉"],
                    object_key="products/noodle.jpg",
                    local_path="/tmp/noodle.jpg",
                    sha256="2" * 64,
                )
            )
            repo.upsert(
                asset_record(
                    asset_id="background",
                    kind="category_background",
                    status="disabled",
                    product_name="背景风格样图",
                    keywords=["背景"],
                    object_key="backgrounds/light/style-1/bg.jpg",
                    local_path="/tmp/bg.jpg",
                    sha256="3" * 64,
                )
            )

            approved_light = repo.list_assets(category="轻食健康餐", style_id="style-1", status="approved")
            backgrounds = repo.list_assets(kind="category_background")
            keyword_matches = repo.filter_assets(keyword="米粉")
            provider_matches = repo.list_assets(provider="tencent-hunyuan", quality="standard", source="TextToImageLite")

        self.assertEqual([record["asset_id"] for record in approved_light], ["salad"])
        self.assertEqual([record["asset_id"] for record in backgrounds], ["background"])
        self.assertEqual([record["asset_id"] for record in keyword_matches], ["noodle"])
        self.assertEqual({record["asset_id"] for record in provider_matches}, {"salad", "noodle", "background"})

    def test_mark_status_controls_reusable_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            repo.upsert(asset_record(asset_id="salad"))

            rejected = repo.reject("salad")
            rejected_matches = repo.find_reusable(category="轻食健康餐", style_id="style-1", product_name="鸡胸沙拉")
            disabled = repo.disable("salad")
            approved = repo.approve("salad")
            approved_matches = repo.find_reusable(category="轻食健康餐", style_id="style-1", product_name="鸡胸沙拉")

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected_matches, [])
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual([record["asset_id"] for record in approved_matches], ["salad"])

        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            repo.upsert(asset_record(asset_id="salad"))
            with self.assertRaises(ValueError):
                repo.mark_status("salad", "pending")

    def test_mark_status_persists_quality_note_and_allows_manual_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            repo.upsert(
                asset_record(
                    asset_id="manual-review",
                    status="rejected",
                    quality_score=0.28,
                    quality_status="failed",
                    quality_reasons=["solid_or_placeholder"],
                )
            )

            updated = repo.approve("manual-review", quality_note="人工复核通过")
            matches = repo.find_reusable(
                category="轻食健康餐",
                style_id="style-1",
                product_name="牛油果鸡胸沙拉",
                keywords=["鸡胸"],
            )
            stored = AIAssetRepository(Path(tmp) / "manifest.jsonl").get("manual-review")

        self.assertEqual(updated["status"], "approved")
        self.assertEqual(updated["quality_status"], "manual_approved")
        self.assertEqual(updated["quality_reasons"], ["solid_or_placeholder", "人工复核通过"])
        self.assertEqual(stored["quality_reasons"], ["solid_or_placeholder", "人工复核通过"])
        self.assertEqual([record["asset_id"] for record in matches], ["manual-review"])

    def test_find_reusable_matches_category_style_name_and_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            repo.upsert(
                asset_record(
                    asset_id="best",
                    product_name="牛油果鸡胸沙拉",
                    keywords=["牛油果", "鸡胸", "沙拉"],
                    match_names=["牛油果鸡胸沙拉", "鸡胸沙拉"],
                    quality_score=0.9,
                    sha256="1" * 64,
                )
            )
            repo.upsert(
                asset_record(
                    asset_id="wrong-food",
                    product_name="番茄意面",
                    keywords=["番茄", "意面"],
                    match_names=["番茄意面"],
                    quality_score=1.0,
                    object_key="products/pasta.jpg",
                    local_path="/tmp/pasta.jpg",
                    sha256="2" * 64,
                )
            )
            repo.upsert(
                asset_record(
                    asset_id="wrong-style",
                    style_id="style-2",
                    product_name="鸡胸沙拉",
                    object_key="products/salad-style-2.jpg",
                    local_path="/tmp/salad-style-2.jpg",
                    sha256="3" * 64,
                )
            )
            repo.upsert(
                asset_record(
                    asset_id="rejected-match",
                    status="rejected",
                    product_name="鸡胸沙拉",
                    object_key="products/rejected.jpg",
                    local_path="/tmp/rejected.jpg",
                    sha256="4" * 64,
                )
            )

            matches = repo.find_reusable(
                category="轻食健康餐",
                style_id="style-1",
                product_name="鸡胸沙拉",
                keywords=["鸡胸"],
            )

        self.assertEqual([record["asset_id"] for record in matches], ["best"])

    def test_normalizes_app_manifest_aliases_into_searchable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            record = repo.upsert(
                {
                    "assetId": "app-asset",
                    "kind": "product_image",
                    "category": "轻食健康餐",
                    "styleId": "style-1",
                    "productName": "牛油果鸡胸沙拉",
                    "normalizedProductName": "牛油果鸡胸沙拉",
                    "keywords": ["牛油果", "鸡胸", "沙拉"],
                    "matchNames": ["牛油果鸡胸沙拉", "鸡胸沙拉"],
                    "quality": "premium",
                    "provider": "tencent-hunyuan",
                    "modelAction": "TextToImageLite",
                    "objectKey": "ai-assets/products/salad.jpg",
                    "localPath": "/tmp/salad.jpg",
                    "sha256": "5" * 64,
                    "createdAt": "2026-06-28T02:00:00Z",
                }
            )

            matches = repo.find_reusable(
                category="轻食健康餐",
                style_id="style-1",
                product_name="鸡胸沙拉",
                keywords=["牛油果"],
            )

        self.assertEqual(record["asset_id"], "app-asset")
        self.assertEqual(record["style_id"], "style-1")
        self.assertEqual(record["product_name"], "牛油果鸡胸沙拉")
        self.assertEqual(record["source"], "TextToImageLite")
        self.assertEqual(record["provider"], "tencent-hunyuan")
        self.assertEqual(record["quality"], "premium")
        self.assertEqual([match["asset_id"] for match in matches], ["app-asset"])

    def test_failed_quality_generated_asset_is_rejected_and_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "solid.jpg"
            image_path.write_bytes(b"not-used-by-repository")
            repo = AIAssetRepository(root / "manifest.jsonl")

            record = repo.upsert_generated_asset(
                kind="product_image",
                local_path=image_path,
                category="轻食健康餐",
                style_id="style-1",
                product_name="牛油果鸡胸沙拉",
                keywords=["鸡胸", "沙拉"],
                match_names=["牛油果鸡胸沙拉"],
                quality="standard",
                quality_report={"passed": False, "score": 0.35, "reasons": ["solid_or_placeholder"]},
            )

            matches = repo.find_reusable(
                category="轻食健康餐",
                style_id="style-1",
                product_name="牛油果鸡胸沙拉",
                keywords=["鸡胸"],
            )

        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["quality_status"], "failed")
        self.assertEqual(record["quality_score"], 0.35)
        self.assertEqual(record["quality_reasons"], ["solid_or_placeholder"])
        self.assertEqual(matches, [])

    def test_select_reusable_returns_generation_required_when_name_keyword_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIAssetRepository(Path(tmp) / "manifest.jsonl")
            repo.upsert(
                asset_record(
                    asset_id="wrong-food",
                    product_name="番茄意面",
                    keywords=["番茄", "意面"],
                    match_names=["番茄意面"],
                    quality_score=1.0,
                    object_key="products/pasta.jpg",
                    local_path="/tmp/pasta.jpg",
                    sha256="6" * 64,
                )
            )

            decision = repo.select_reusable_asset(
                category="轻食健康餐",
                style_id="style-1",
                product_name="牛油果鸡胸沙拉",
                keywords=["牛油果", "鸡胸"],
                kind="product_image",
            )

        self.assertEqual(decision["status"], GENERATION_REQUIRED_STATUS)
        self.assertTrue(decision["generation_required"])
        self.assertIsNone(decision["asset"])
        self.assertEqual(decision["matches"], [])


if __name__ == "__main__":
    unittest.main()
