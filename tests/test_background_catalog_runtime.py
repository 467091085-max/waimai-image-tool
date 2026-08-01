from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from unittest import mock

import app as app_module
import background_catalog
import background_profiles
import object_storage_service


def approved_record(style_id: str, *, suffix: str = "") -> dict[str, object]:
    index = int(style_id.rsplit("-", 1)[1])
    return {
        "id": "asset-" + f"{index:040x}"[:-len(suffix) or None] + suffix,
        "taxonomy_version": app_module.TAXONOMY_VERSION,
        "category_id": "light_food",
        "category_name": "轻食/沙拉",
        "style_id": style_id,
        "prompt_version": "style-background.v10",
        "pipeline_version": "style-background.v10",
        "source_provider": "tencent-hunyuan",
        "model_name": "hy-image-v3.0",
        "original_sha256": f"{index:064x}",
        "original_size_bytes": 1234 + index,
        "review_status": "approved",
        "reviewed_at": "2026-08-01T00:00:00Z",
        "created_at": "2026-08-01T00:00:00Z",
    }


@contextmanager
def dummy_connection():
    yield object()


class CatalogStore:
    records: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []

    def __init__(self, _connection: object) -> None:
        pass

    def list_approved_background_catalog(
        self,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        self.calls.append(dict(kwargs))
        return list(self.records)


def light_food_context() -> dict[str, object]:
    return {
        "taxonomyId": "light_food",
        "category": "轻食/沙拉",
        "confidence": 91,
        "selectionReason": "menu_taxonomy_evidence",
    }


def catalog_patches(records: list[dict[str, object]]):
    CatalogStore.records = records
    CatalogStore.calls = []
    return (
        mock.patch.object(
            app_module,
            "active_category_context",
            side_effect=light_food_context,
        ),
        mock.patch.object(
            app_module,
            "postgres_product_runtime_enabled",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "ai_asset_library_enabled",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "current_asset_owner_user_id",
            return_value="customer-1",
        ),
        mock.patch.object(
            app_module,
            "postgres_connection",
            dummy_connection,
        ),
        mock.patch.object(
            app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            CatalogStore,
        ),
    )


def test_complete_approved_runtime_manifest_is_ready() -> None:
    patches = catalog_patches(
        [approved_record(style_id) for style_id in background_catalog.STYLE_IDS]
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        manifest = app_module.approved_background_catalog_manifest()

    assert manifest["ready"] is True
    assert manifest["approvedCount"] == 6
    assert manifest["missingStyleIds"] == []
    assert set(manifest["_recordsByStyle"]) == set(background_catalog.STYLE_IDS)
    assert len(CatalogStore.calls) == 1
    assert CatalogStore.calls[0]["tenant_id"] == "waimai-shared"
    assert CatalogStore.calls[0]["pipeline_version"] == "style-background.v10"


def test_duplicate_approved_runtime_slot_fails_closed() -> None:
    records = [
        approved_record(style_id)
        for style_id in background_catalog.STYLE_IDS
    ]
    records.append(approved_record("style-1", suffix="f"))
    patches = catalog_patches(records)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        manifest = app_module.approved_background_catalog_manifest()

    assert manifest["ready"] is False
    assert manifest["code"] == "background_catalog_incomplete"
    assert manifest["duplicateStyleIds"] == ["style-1"]
    assert "style-1" not in manifest["_recordsByStyle"]


def test_mixed_category_requires_review_without_querying_assets() -> None:
    with (
        mock.patch.object(
            app_module,
            "active_category_context",
            return_value={
                "taxonomyId": "mixed",
                "category": "复合餐饮",
                "confidence": 35,
                "selectionReason": "insufficient_evidence",
            },
        ),
        mock.patch.object(
            app_module,
            "postgres_connection",
        ) as postgres,
    ):
        manifest = app_module.approved_background_catalog_manifest()

    assert manifest["status"] == "classification_review"
    assert manifest["code"] == "background_category_review_required"
    postgres.assert_not_called()


def test_approved_only_style_does_not_fall_back_to_live_generation() -> None:
    with (
        mock.patch.object(
            app_module,
            "approved_background_catalog_enabled",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "approved_background_catalog_manifest",
            return_value={
                "status": "incomplete",
                "ready": False,
                "code": "background_catalog_incomplete",
            },
        ),
        mock.patch.object(app_module, "tencent_style_background") as provider,
        mock.patch.object(
            app_module,
            "style_background_target",
            return_value=app_module.LIBRARY_DIR / "missing.jpg",
        ),
        mock.patch.object(
            app_module,
            "style_background_prompt_metadata",
            return_value={"categoryId": "light_food"},
        ),
    ):
        candidate = app_module.style_sample_candidate(
            "style-1",
            generate=True,
        )

    assert candidate["url"] == ""
    assert candidate["generationAction"] == "CatalogIncomplete"
    assert candidate["generationErrorCode"] == "background_catalog_incomplete"
    provider.assert_not_called()


def cos_manifest_document(
    storage: object_storage_service.ObjectStorageService,
    *,
    review_status: str = "approved",
    tamper_style: str = "",
) -> dict[str, object]:
    assets = []
    for style_id in background_catalog.STYLE_IDS:
        raw = f"background-{style_id}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        prompt = background_profiles.pure_background_prompt(
            "light_food",
            style_id,
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        object_key = background_catalog.catalog_object_key(
            category_id="light_food",
            style_id=style_id,
            prompt_version="style-background.v10",
            prompt_sha256=prompt_sha,
            asset_sha256=digest,
        )
        storage.put_bytes(raw, object_key=object_key)
        assets.append(
            {
                "catalogVersion": background_catalog.CATALOG_VERSION,
                "taxonomyVersion": app_module.TAXONOMY_VERSION,
                "categoryId": "light_food",
                "categoryName": "轻食/沙拉",
                "styleId": style_id,
                "promptVersion": "style-background.v10",
                "promptSha256": (
                    "0" * 64 if style_id == tamper_style else prompt_sha
                ),
                "provider": "tencent-hunyuan",
                "model": "hy-image-v3.0",
                "objectKey": object_key,
                "sha256": digest,
                "fileSize": len(raw),
                "reviewStatus": review_status,
                "createdAt": "2026-08-01T00:00:00Z",
            }
        )
    return {
        "schemaVersion": background_catalog.CATALOG_SCHEMA_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "categoryId": "light_food",
        "categoryName": "轻食/沙拉",
        "promptVersion": "style-background.v10",
        "reviewStatus": review_status,
        "assets": assets,
    }


def cos_manifest_patches(
    storage: object_storage_service.ObjectStorageService,
):
    return (
        mock.patch.object(
            app_module,
            "active_category_context",
            side_effect=light_food_context,
        ),
        mock.patch.object(
            app_module,
            "background_catalog_manifest_backend",
            return_value="object-storage",
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "assess_object_storage_readiness",
            return_value={"ready": True},
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    )


def write_cos_manifest(
    storage: object_storage_service.ObjectStorageService,
    document: dict[str, object],
) -> None:
    storage.put_bytes(
        json.dumps(document, ensure_ascii=False).encode("utf-8"),
        object_key=background_catalog.catalog_manifest_key(
            "light_food",
            "style-background.v10",
        ),
    )


def test_complete_approved_cos_manifest_is_ready(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    write_cos_manifest(storage, cos_manifest_document(storage))
    patches = cos_manifest_patches(storage)

    with patches[0], patches[1], patches[2], patches[3]:
        manifest = app_module.approved_background_catalog_manifest()

    assert manifest["manifestBackend"] == "object-storage"
    assert manifest["ready"] is True
    assert manifest["approvedCount"] == 6
    assert set(manifest["_recordsByStyle"]) == set(
        background_catalog.STYLE_IDS
    )
    assert manifest["_recordsByStyle"]["style-1"][
        "original_object_ref"
    ].startswith("ai-assets/waimai-shared/background-catalog/")


def test_pending_cos_manifest_fails_closed(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    write_cos_manifest(
        storage,
        cos_manifest_document(storage, review_status="pending"),
    )
    patches = cos_manifest_patches(storage)

    with patches[0], patches[1], patches[2], patches[3]:
        manifest = app_module.approved_background_catalog_manifest()

    assert manifest["ready"] is False
    assert manifest["approvedCount"] == 0
    assert manifest["pendingCount"] == 6
    assert manifest["_recordsByStyle"] == {}


def test_tampered_cos_prompt_hash_fails_closed(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    write_cos_manifest(
        storage,
        cos_manifest_document(storage, tamper_style="style-3"),
    )
    patches = cos_manifest_patches(storage)

    with patches[0], patches[1], patches[2], patches[3]:
        manifest = app_module.approved_background_catalog_manifest()

    assert manifest["ready"] is False
    assert manifest["invalidStyleIds"] == ["style-3"]
    assert "style-3" in manifest["missingStyleIds"]
    assert "style-3" not in manifest["_recordsByStyle"]
