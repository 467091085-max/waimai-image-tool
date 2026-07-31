from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

import app as app_module
import object_storage_service
from matching_engine import split_components


def save_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 90), color).save(path, "PNG")


def menu_row(*, combo: bool = False) -> dict[str, object]:
    if combo:
        return {
            "row": 1,
            "name": "鱼香肉丝套餐",
            "category": "套餐",
            "kind": "套餐/组合",
            "taxonomy": "combo",
            "taxonomyVersion": app_module.TAXONOMY_VERSION,
            "components": ["鱼香肉丝", "米饭", "可乐"],
        }
    return {
        "row": 1,
        "name": "番茄炒蛋",
        "category": "家常菜",
        "kind": "单品",
        "taxonomy": "home_stir_fry",
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "components": [],
    }


def selected_background(path: Path) -> app_module.SelectedBackgroundAsset:
    raw = path.read_bytes()
    return app_module.SelectedBackgroundAsset(
        asset_id="bg_current_menu",
        menu_key="menu-test",
        style_id="style-2",
        sha256=hashlib.sha256(raw).hexdigest(),
        path=path,
        width=120,
        height=90,
        library_asset_id="asset-" + ("b" * 40),
    )


class CapturingStore:
    def __init__(self, _connection: object) -> None:
        self.registration: dict[str, object] | None = None
        self.lookup: dict[str, object] | None = None

    def register_asset(self, **kwargs: object) -> SimpleNamespace:
        self.registration = dict(kwargs)
        return SimpleNamespace(
            created=True,
            record={
                "id": app_module.product_asset_library_store.tenant_bound_asset_id(
                    str(kwargs["tenant_id"]),
                    str(kwargs["idempotency_key"]),
                ),
                "status": "pending_review",
                "review_status": "pending",
                "original_object_ref": kwargs["original_object_ref"],
                "original_sha256": kwargs["original_sha256"],
                "original_size_bytes": kwargs["original_size_bytes"],
                "category_name": kwargs["category_name"],
                "category_id": kwargs["category_id"],
                "style_id": kwargs["style_id"],
                "standard_name": kwargs["standard_name"],
                "pipeline_version": kwargs["pipeline_version"],
            },
        )

    def find_reusable_assets(self, **kwargs: object) -> list[dict[str, object]]:
        self.lookup = dict(kwargs)
        return []


@contextmanager
def dummy_postgres_connection():
    yield object()


def test_reusable_lookup_falls_back_to_shared_tenant_scope() -> None:
    calls: list[dict[str, object]] = []
    shared_record = {
        "id": "asset-" + ("c" * 40),
        "tenant_id": "waimai-shared",
        "asset_kind": "background",
    }

    class SharedFallbackStore:
        def __init__(self, _connection: object) -> None:
            pass

        def find_reusable_assets(
            self,
            **kwargs: object,
        ) -> list[dict[str, object]]:
            calls.append(dict(kwargs))
            return [] if len(calls) == 1 else [shared_record]

    with (
        app_module.active_asset_owner("usr_shared_lookup"),
        mock.patch.object(
            app_module,
            "postgres_product_runtime_enabled",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "active_menu_asset_taxonomy",
            return_value="mixed_rice",
        ),
        mock.patch.object(
            app_module,
            "postgres_connection",
            dummy_postgres_connection,
        ),
        mock.patch.object(
            app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            SharedFallbackStore,
        ),
        mock.patch.object(
            app_module,
            "product_shared_asset_tenant_id",
            return_value="waimai-shared",
        ),
    ):
        result = app_module.postgres_reusable_asset_record(
            kind="category_background",
            style_id="style-upload",
        )

    assert result == shared_record
    assert len(calls) == 2
    assert calls[0]["include_tenant_scope"] is False
    assert calls[1]["tenant_id"] == "waimai-shared"
    assert calls[1]["include_tenant_scope"] is True


def test_postgres_asset_persistence_is_private_pending_and_background_bound(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output.png"
    save_image(output, (90, 130, 60))
    background_path = tmp_path / "background.png"
    save_image(background_path, (25, 80, 135))
    background = selected_background(background_path)
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    created_stores: list[CapturingStore] = []

    def store_factory(connection: object) -> CapturingStore:
        store = CapturingStore(connection)
        created_stores.append(store)
        return store

    metadata = {
        "provider": "tencent-hunyuan",
        "action": "DeterministicBackgroundComposite",
        "tencent": {"model": "hy-image-v3.0"},
        **app_module.selected_background_metadata(background),
    }
    with (
        app_module.active_asset_owner("usr_test_1"),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            app_module,
            "postgres_connection",
            dummy_postgres_connection,
        ),
        mock.patch.object(
            app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            side_effect=store_factory,
        ),
    ):
        result = app_module.persist_postgres_ai_generated_asset(
            kind="product_image",
            source_path=output,
            style_id="style-2",
            metadata=metadata,
            row=menu_row(),
            dish_name="番茄炒蛋",
        )

    assert result["status"] == "pending_review"
    assert result["reviewStatus"] == "pending"
    assert len(created_stores) == 1
    registration = created_stores[0].registration
    assert registration is not None
    assert registration["reuse_scope"] == "owner"
    assert registration["background_asset_id"] == background.library_asset_id
    assert registration["background_sha256"] == background.sha256
    assert registration["pipeline_version"] == (
        f"exact-background.v{app_module.EXACT_BACKGROUND_PIPELINE_VERSION}"
    )
    object_key = str(registration["original_object_ref"])
    assert object_key.startswith("ai-assets/tenant-")
    assert storage.read_bytes(object_key) == output.read_bytes()


def test_postgres_asset_upload_uses_the_fingerprinted_private_snapshot(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output.png"
    save_image(output, (90, 130, 60))
    original_bytes = output.read_bytes()
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    created_stores: list[CapturingStore] = []
    uploaded_sources: list[Path] = []
    real_limited_upload = (
        app_module.object_storage_service.put_object_file_limited
    )

    def store_factory(connection: object) -> CapturingStore:
        store = CapturingStore(connection)
        created_stores.append(store)
        return store

    def mutate_original_before_upload(
        storage_adapter: object,
        source: str | os.PathLike[str],
        *,
        object_key: str,
        max_bytes: int,
    ) -> str:
        uploaded_sources.append(Path(source))
        output.write_bytes(b"x" * len(original_bytes))
        return real_limited_upload(
            storage_adapter,
            source,
            object_key=object_key,
            max_bytes=max_bytes,
        )

    with (
        app_module.active_asset_owner("usr_test_snapshot"),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "put_object_file_limited",
            side_effect=mutate_original_before_upload,
        ),
        mock.patch.object(
            app_module,
            "postgres_connection",
            dummy_postgres_connection,
        ),
        mock.patch.object(
            app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            side_effect=store_factory,
        ),
        mock.patch.object(
            app_module,
            "active_menu_asset_taxonomy",
            return_value="home_stir_fry",
        ),
    ):
        result = app_module.persist_postgres_ai_generated_asset(
            kind="category_background",
            source_path=output,
            style_id="style-2",
            metadata={
                "provider": "tencent-hunyuan",
                "action": "TextToImageLite",
                "tencent": {"model": "hy-image-v3.0"},
            },
            row=None,
            dish_name="背景风格样图",
        )

    assert len(output.read_bytes()) == len(original_bytes)
    assert output.read_bytes() != original_bytes
    assert len(uploaded_sources) == 1
    assert uploaded_sources[0] != output
    registration = created_stores[0].registration
    assert registration is not None
    object_key = str(registration["original_object_ref"])
    assert storage.read_bytes(object_key) == original_bytes
    assert result["sha256"] == hashlib.sha256(original_bytes).hexdigest()


def test_combo_asset_uses_complete_server_owned_components() -> None:
    fields = app_module.product_asset_registration_fields(
        kind="product_image",
        row=menu_row(combo=True),
        dish_name="鱼香肉丝套餐",
    )

    assert fields["category_id"] == "combo"
    components = fields["combo_components"]
    assert isinstance(components, list)
    assert {component["name"] for component in components} == {
        "鱼香肉丝",
        "米饭",
        "可乐",
    }
    assert any(component["role"] == "main" for component in components)
    assert any(component["role"] == "staple" for component in components)
    assert any(component["role"] == "drink" for component in components)


def test_combo_asset_fingerprint_preserves_quantity_and_specification() -> None:
    cases = {
        "炸鸡x1+可乐套餐": (1, ""),
        "炸鸡x2+可乐套餐": (2, ""),
        "炸鸡(大份)+可乐套餐": (1, "大份"),
        "炸鸡(小份)+可乐套餐": (1, "小份"),
    }
    fingerprints: dict[str, str] = {}

    for name, expected in cases.items():
        row = {
            "row": 1,
            "name": name,
            "category": "套餐",
            "kind": "套餐/组合",
            "taxonomy": "combo",
            "taxonomyVersion": app_module.TAXONOMY_VERSION,
            "components": split_components(name),
        }
        components = app_module.product_asset_combo_components(row)
        chicken = next(
            component
            for component in components
            if component["name"] == "炸鸡"
        )
        assert chicken["quantity"] == expected[0]
        assert chicken.get("specification", "") == expected[1]
        _canonical, fingerprint = (
            app_module.product_asset_library_store
            .combo_components_fingerprint(components)
        )
        fingerprints[name] = fingerprint

    assert len(set(fingerprints.values())) == len(cases)


def test_combo_asset_fingerprint_aggregates_identical_components() -> None:
    name = "炸鸡+炸鸡+可乐套餐"
    components = app_module.product_asset_combo_components(
        {
            "row": 1,
            "name": name,
            "category": "套餐",
            "kind": "套餐/组合",
            "taxonomy": "combo",
            "taxonomyVersion": app_module.TAXONOMY_VERSION,
            "components": split_components(name),
        }
    )

    chicken = next(
        component
        for component in components
        if component["name"] == "炸鸡"
    )
    assert chicken["quantity"] == 2


def test_approved_asset_materialization_verifies_private_object_sha(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    save_image(source, (110, 70, 40))
    background_path = tmp_path / "background.png"
    save_image(background_path, (20, 60, 130))
    background = selected_background(background_path)
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    object_key = (
        "ai-assets/tenant-test/"
        f"asset-{'c' * 40}/original.png"
    )
    storage.put_file(source, object_key=object_key)
    record = {
        "id": "asset-" + ("c" * 40),
        "original_object_ref": object_key,
        "original_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "original_size_bytes": source.stat().st_size,
    }
    target = tmp_path / "preview.png"

    with (
        mock.patch.object(
            app_module,
            "postgres_reusable_asset_record",
            return_value=record,
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            app_module,
            "current_menu_cache_key",
            return_value="menu-test",
        ),
    ):
        reused = app_module.materialize_reusable_product_asset(
            menu_row(),
            "style-2",
            background,
            "standard",
            target,
        )

    assert reused is not None
    candidate, metadata = reused
    assert target.read_bytes() == source.read_bytes()
    assert candidate["aiProvider"] == "asset-library"
    assert metadata["assetRecordId"] == record["id"]
    assert app_module.verified_exact_output_metadata(
        metadata,
        target,
        background,
    )

    target.unlink()
    tampered = {**record, "original_sha256": "f" * 64}
    with (
        mock.patch.object(
            app_module,
            "postgres_reusable_asset_record",
            return_value=tampered,
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        pytest.raises(
            app_module.ProductAssetRuntimeError,
            match="asset_object_sha256_mismatch",
        ),
    ):
        app_module.materialize_reusable_product_asset(
            menu_row(),
            "style-2",
            background,
            "standard",
            target,
        )
    assert not target.exists()


def test_preview_stops_before_paid_provider_when_asset_store_is_unavailable(
    tmp_path: Path,
) -> None:
    background_path = tmp_path / "background.png"
    save_image(background_path, (20, 60, 130))
    background = selected_background(background_path)
    provider = mock.Mock()

    with (
        mock.patch.object(app_module, "LIBRARY_DIR", tmp_path),
        mock.patch.object(
            app_module,
            "current_menu_cache_key",
            return_value="menu-test",
        ),
        mock.patch.object(
            app_module,
            "postgres_product_runtime_enabled",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "materialize_reusable_product_asset",
            side_effect=app_module.ProductAssetRuntimeError(
                "asset_library_unavailable"
            ),
        ),
        mock.patch.object(
            app_module,
            "tencent_exact_background_image",
            provider,
        ),
    ):
        candidate, generation = app_module.materialize_preview_candidate(
            menu_row(),
            "style-2",
            "standard",
            background,
        )

    assert candidate is None
    assert generation["action"] == "AssetLibraryUnavailable"
    assert generation["errorCode"] == "asset_library_unavailable"
    provider.assert_not_called()


def admin_asset_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "asset-" + ("d" * 40),
        "tenant_id": "tenant-test",
        "owner_user_id": "usr-test",
        "asset_kind": "product",
        "taxonomy_version": app_module.TAXONOMY_VERSION,
        "category_id": "home_stir_fry",
        "category_name": "家常小炒",
        "style_id": "style-2",
        "background_asset_id": "asset-" + ("b" * 40),
        "background_sha256": "a" * 64,
        "standard_name": "番茄炒蛋",
        "aliases": ["西红柿炒鸡蛋"],
        "match_keywords": ["番茄", "鸡蛋"],
        "status": "pending_review",
        "review_status": "pending",
        "review_note": "",
        "source_provider": "tencent-hunyuan",
        "model_name": "hy-image-v3.0",
        "model_version": "hy-image-v3.0",
        "prompt_version": "dish-generation.v1",
        "pipeline_version": "exact-background.v1",
        "original_object_ref": (
            "ai-assets/tenant-test/"
            f"asset-{'d' * 40}/original.png"
        ),
        "original_sha256": "e" * 64,
        "original_size_bytes": 1234,
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
    }
    row.update(overrides)
    return row


class FakeAdminAssetStore:
    def __init__(self) -> None:
        self.list_kwargs: dict[str, object] | None = None
        self.review_kwargs: dict[str, object] | None = None

    def list_assets_for_admin(
        self,
        **kwargs: object,
    ) -> list[dict[str, object]]:
        self.list_kwargs = dict(kwargs)
        return [admin_asset_row()]

    def summarize_assets_for_admin(self) -> list[dict[str, object]]:
        return [
            {
                "status": "pending_review",
                "asset_kind": "product",
                "category_name": "家常小炒",
                "asset_count": 1,
            }
        ]

    def get_asset(self, *, asset_id: str) -> dict[str, object]:
        assert asset_id == "asset-" + ("d" * 40)
        return admin_asset_row()

    def review_asset(self, **kwargs: object) -> SimpleNamespace:
        self.review_kwargs = dict(kwargs)
        return SimpleNamespace(
            idempotent=False,
            record=admin_asset_row(
                status="approved",
                review_status="approved",
                reviewer_user_id=kwargs["reviewer_user_id"],
                review_note=kwargs["review_note"],
            ),
        )


def test_postgres_admin_asset_list_and_review_hide_object_keys() -> None:
    fake_store = FakeAdminAssetStore()
    client = app_module.app.test_client()
    asset_id = "asset-" + ("d" * 40)

    with (
        mock.patch.object(
            app_module,
            "admin_panel_request_authorizer",
            return_value=app_module.AdminAuthorization(
                authenticated=True,
                allowed=True,
            ),
        ),
        mock.patch.object(
            app_module,
            "admin_ai_asset_status_authorized",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "admin_actor_user_id",
            return_value="reviewer-1",
        ),
        mock.patch.object(
            app_module,
            "postgres_product_runtime_enabled",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "postgres_connection",
            dummy_postgres_connection,
        ),
        mock.patch.object(
            app_module.product_asset_library_store,
            "ProductAssetLibraryStore",
            return_value=fake_store,
        ),
        mock.patch.object(
            app_module,
            "object_access_signing_secret",
            return_value="",
        ),
    ):
        listing = client.get(
            "/api/admin/ai-assets?status=pending&limit=25&offset=0"
        )
        review = client.post(
            f"/api/admin/actions/ai-assets/{asset_id}/status",
            json={
                "status": "approved",
                "qualityNote": "quality pass",
            },
        )

    assert listing.status_code == 200
    listing_body = listing.get_json()
    assert listing_body["summary"]["pending"] == 1
    assert listing_body["pagination"]["returned"] == 1
    assert "objectKey" not in listing_body["assets"][0]
    assert "originalObjectRef" not in listing_body["assets"][0]
    assert fake_store.list_kwargs == {
        "status": "pending_review",
        "limit": 25,
        "offset": 0,
    }

    assert review.status_code == 200
    assert review.get_json()["asset"]["status"] == "approved"
    assert fake_store.review_kwargs == {
        "asset_id": asset_id,
        "tenant_id": "tenant-test",
        "reviewer_user_id": "reviewer-1",
        "decision": "approved",
        "review_note": "quality pass",
    }


def test_postgres_admin_asset_review_is_one_way() -> None:
    client = app_module.app.test_client()
    asset_id = "asset-" + ("d" * 40)
    with (
        mock.patch.object(
            app_module,
            "admin_ai_asset_status_authorized",
            return_value=True,
        ),
        mock.patch.object(
            app_module,
            "postgres_product_runtime_enabled",
            return_value=True,
        ),
    ):
        pending = client.post(
            f"/api/admin/actions/ai-assets/{asset_id}/status",
            json={"status": "pending"},
        )
        disable_without_reason = client.post(
            f"/api/admin/actions/ai-assets/{asset_id}/status",
            json={"status": "disabled"},
        )

    assert pending.status_code == 409
    assert disable_without_reason.status_code == 400


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_reviewed_asset_reuse_protocol(
    tmp_path: Path,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_product_asset_runtime_{hashlib.sha1(str(tmp_path).encode()).hexdigest()[:16]}"
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin.close()

    @contextmanager
    def scoped_connection():
        connection = psycopg.connect(dsn, autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()
            yield connection
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    background_path = tmp_path / "background.png"
    product_path = tmp_path / "product.png"
    save_image(background_path, (20, 60, 130))
    save_image(product_path, (120, 80, 40))
    owner_user_id = "usr_real_asset_1"
    tenant_id = app_module.product_asset_tenant_id(owner_user_id)

    try:
        migration = (
            Path(app_module.__file__).resolve().parent
            / "migrations"
            / "009_product_asset_library_postgres.sql"
        ).read_text(encoding="utf-8")
        with scoped_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(migration)
                cursor.execute(migration)
            connection.commit()

        common_patches = (
            mock.patch.object(
                app_module.object_storage_service,
                "get_object_storage_service",
                return_value=storage,
            ),
            mock.patch.object(
                app_module,
                "postgres_connection",
                scoped_connection,
            ),
            mock.patch.object(
                app_module,
                "postgres_product_runtime_enabled",
                return_value=True,
            ),
            mock.patch.object(
                app_module,
                "active_menu_asset_taxonomy",
                return_value="home_stir_fry",
            ),
        )
        with (
            app_module.active_asset_owner(owner_user_id),
            common_patches[0],
            common_patches[1],
            common_patches[2],
            common_patches[3],
        ):
            background_record = (
                app_module.persist_postgres_ai_generated_asset(
                    kind="category_background",
                    source_path=background_path,
                    style_id="style-2",
                    metadata={
                        "provider": "tencent-hunyuan",
                        "action": "TextToImageLite",
                        "tencent": {"model": "hy-image-v3.0"},
                    },
                    row=None,
                    dish_name="背景风格样图",
                )
            )
            assert (
                app_module.postgres_reusable_asset_record(
                    kind="category_background",
                    style_id="style-2",
                )
                is None
            )

        with scoped_connection() as connection:
            app_module.product_asset_library_store.ProductAssetLibraryStore(
                connection
            ).review_asset(
                asset_id=background_record["assetId"],
                tenant_id=tenant_id,
                reviewer_user_id="reviewer-1",
                decision="approved",
                review_note="background quality pass",
            )

        background = selected_background(background_path)
        background = app_module.SelectedBackgroundAsset(
            **{
                **background.__dict__,
                "library_asset_id": background_record["assetId"],
            }
        )
        product_metadata = {
            "provider": "tencent-hunyuan",
            "action": "DeterministicBackgroundComposite",
            "tencent": {"model": "hy-image-v3.0"},
            **app_module.selected_background_metadata(background),
        }
        with (
            app_module.active_asset_owner(owner_user_id),
            mock.patch.object(
                app_module.object_storage_service,
                "get_object_storage_service",
                return_value=storage,
            ),
            mock.patch.object(
                app_module,
                "postgres_connection",
                scoped_connection,
            ),
            mock.patch.object(
                app_module,
                "postgres_product_runtime_enabled",
                return_value=True,
            ),
        ):
            product_record = app_module.persist_postgres_ai_generated_asset(
                kind="product_image",
                source_path=product_path,
                style_id="style-2",
                metadata=product_metadata,
                row=menu_row(),
                dish_name="番茄炒蛋",
            )
            assert (
                app_module.postgres_reusable_asset_record(
                    kind="product_image",
                    style_id="style-2",
                    row=menu_row(),
                    selected_background=background,
                )
                is None
            )

        with scoped_connection() as connection:
            app_module.product_asset_library_store.ProductAssetLibraryStore(
                connection
            ).review_asset(
                asset_id=product_record["assetId"],
                tenant_id=tenant_id,
                reviewer_user_id="reviewer-1",
                decision="approved",
                review_note="product quality pass",
            )

        target = tmp_path / "reused.png"
        with (
            app_module.active_asset_owner(owner_user_id),
            mock.patch.object(
                app_module.object_storage_service,
                "get_object_storage_service",
                return_value=storage,
            ),
            mock.patch.object(
                app_module,
                "postgres_connection",
                scoped_connection,
            ),
            mock.patch.object(
                app_module,
                "postgres_product_runtime_enabled",
                return_value=True,
            ),
        ):
            reusable = app_module.postgres_reusable_asset_record(
                kind="product_image",
                style_id="style-2",
                row=menu_row(),
                selected_background=background,
            )
            assert reusable is not None
            reused = app_module.materialize_reusable_product_asset(
                menu_row(),
                "style-2",
                background,
                "standard",
                target,
            )
            assert reused is not None
            assert target.read_bytes() == product_path.read_bytes()

            wrong_background = app_module.SelectedBackgroundAsset(
                **{
                    **background.__dict__,
                    "sha256": "f" * 64,
                }
            )
            assert (
                app_module.postgres_reusable_asset_record(
                    kind="product_image",
                    style_id="style-2",
                    row=menu_row(),
                    selected_background=wrong_background,
                )
                is None
            )

            storage.put_bytes(
                b"tampered",
                object_key=str(product_record["objectKey"]),
            )
            with pytest.raises(
                app_module.ProductAssetRuntimeError,
                match="asset_object_size_mismatch",
            ):
                app_module.materialize_reusable_product_asset(
                    menu_row(),
                    "style-2",
                    background,
                    "standard",
                    tmp_path / "tampered.png",
                )
    finally:
        cleanup = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
                )
        finally:
            cleanup.close()
