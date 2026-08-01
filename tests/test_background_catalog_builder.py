from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import object_storage_service
from scripts import build_background_catalog as builder


def save_test_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1024, 768), (215, 220, 205)).save(
        path,
        "JPEG",
        quality=92,
    )


def test_builder_default_plan_is_exactly_240_assets(capsys) -> None:
    assert builder.main([]) == 0

    output = capsys.readouterr().out
    assert '"categoryCount": 40' in output
    assert '"styleCount": 6' in output
    assert '"plannedAssetCount": 240' in output


def test_generate_entry_is_pending_and_prompt_bound(tmp_path: Path) -> None:
    target = tmp_path / "light_food" / "style-1.jpg"

    with (
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TextToImageLite",
                "_Model": "hy-image-v3.0",
                "RequestId": "request-1",
            },
        ),
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_output_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
    ):
        entry = builder.generate_entry(
            category_id="light_food",
            style_id="style-1",
            image_path=target,
            attempts=1,
        )

    assert entry["reviewStatus"] == "pending"
    assert entry["categoryId"] == "light_food"
    assert entry["styleId"] == "style-1"
    assert entry["promptVersion"] == "style-background.v8"
    assert len(entry["promptSha256"]) == 64
    assert entry["width"] == 1024
    assert entry["height"] == 768
    assert entry["objectKey"].startswith(
        "ai-assets/waimai-shared/background-catalog/"
    )


@contextmanager
def dummy_postgres_connection():
    yield object()


class CapturingStore:
    calls: list[dict[str, object]] = []

    def __init__(self, _connection: object) -> None:
        pass

    def register_asset(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            created=True,
            record={
                "id": "asset-" + ("a" * 40),
                "status": "pending_review",
                "review_status": "pending",
            },
        )


def test_register_entry_writes_shared_private_object_pending(
    tmp_path: Path,
) -> None:
    target = tmp_path / "light_food" / "style-1.jpg"
    save_test_image(target)
    fingerprint = builder.app_module.image_file_fingerprint(target)
    prompt = builder.background_profiles.pure_background_prompt(
        "light_food",
        "style-1",
    )
    prompt_sha = builder.hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    entry = {
        "catalogVersion": builder.background_catalog.CATALOG_VERSION,
        "taxonomyVersion": builder.app_module.TAXONOMY_VERSION,
        "categoryId": "light_food",
        "categoryName": "轻食/沙拉",
        "styleId": "style-1",
        "styleSlotName": "暖色纯色棚拍",
        "promptSha256": prompt_sha,
        "provider": "tencent-hunyuan",
        "model": "hy-image-v3.0",
        "sha256": fingerprint["sha256"],
        "objectKey": builder.background_catalog.catalog_object_key(
            category_id="light_food",
            style_id="style-1",
            prompt_version=builder.PROMPT_VERSION,
            prompt_sha256=prompt_sha,
            asset_sha256=fingerprint["sha256"],
        ),
    }
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    CapturingStore.calls = []

    with (
        mock.patch.object(
            builder.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            builder.app_module,
            "postgres_connection",
            dummy_postgres_connection,
        ),
        mock.patch.object(
            builder.app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            CapturingStore,
        ),
    ):
        registered = builder.register_pending_entry(
            entry,
            target,
            actor_user_id="background-catalog-builder",
        )

    assert registered["registered"] is True
    assert registered["reviewStatus"] == "pending"
    assert storage.read_bytes(str(entry["objectKey"])) == target.read_bytes()
    assert len(CapturingStore.calls) == 1
    call = CapturingStore.calls[0]
    assert call["tenant_id"] == "waimai-shared"
    assert call["reuse_scope"] == "tenant"
    assert call["asset_kind"] == "background"
    assert call["category_id"] == "light_food"
    assert call["style_id"] == "style-1"
    assert call["pipeline_version"] == "style-background.v8"
