from __future__ import annotations

import importlib
import io
import os
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
from PIL import Image

import object_storage_service
from shared import product_asset_library_store


TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = tuple(sorted((ROOT / "migrations").glob("*.sql")))


def image_zip(
    *,
    member_name: str = "backgrounds/rice.jpg",
    color: tuple[int, int, int] = (220, 80, 40),
) -> io.BytesIO:
    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 12), color=color).save(
        image_buffer,
        format="JPEG",
    )
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(
        archive_buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(member_name, image_buffer.getvalue())
    archive_buffer.seek(0)
    return archive_buffer


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the library import protocol",
)
def test_live_library_import_is_atomic_replayable_and_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "test" not in TEST_POSTGRES_DSN.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    schema = f"test_library_import_http_{uuid4().hex}"
    sqlite_path = tmp_path / "must-not-exist.sqlite3"
    object_root = tmp_path / "objects"
    storage = object_storage_service.ObjectStorageService(object_root)

    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        def connect_schema() -> Any:
            connection = psycopg.connect(
                TEST_POSTGRES_DSN,
                autocommit=False,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema)
                    )
                )
            connection.commit()
            return connection

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                for migration in MIGRATIONS:
                    cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

        @contextmanager
        def postgres_connection_for_test(
            *_: Any,
            **__: Any,
        ) -> Iterator[Any]:
            current = connect_schema()
            try:
                yield current
            except BaseException:
                current.rollback()
                raise
            finally:
                current.close()

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("PRODUCT_POSTGRES_ENABLED", "true")
        monkeypatch.setenv("DATABASE_URL", TEST_POSTGRES_DSN)
        monkeypatch.setenv("ADMIN_API_TOKEN", "library-admin-token")
        monkeypatch.setenv("STORAGE_DB_PATH", str(sqlite_path))
        monkeypatch.setenv(
            "PRODUCT_SHARED_ASSET_TENANT_ID",
            "waimai-shared",
        )

        app_module = importlib.import_module("app")
        app_module = importlib.reload(app_module)
        monkeypatch.setattr(
            app_module,
            "postgres_connection",
            postgres_connection_for_test,
        )
        monkeypatch.setattr(
            app_module.object_storage_service,
            "get_object_storage_service",
            lambda: storage,
        )
        monkeypatch.setattr(
            app_module.object_storage_service,
            "assess_object_storage_readiness",
            lambda: {
                "ready": True,
                "provider": "cos",
                "mode": "remote_private",
                "blockingIssues": [],
                "warnings": [],
            },
        )

        def sqlite_forbidden(*_: Any, **__: Any) -> Any:
            raise AssertionError("live library import touched SQLite")

        monkeypatch.setattr(
            app_module,
            "product_db_conn",
            sqlite_forbidden,
        )
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        headers = {
            "X-Admin-Token": "library-admin-token",
            "Idempotency-Key": "library-import-http-1",
        }
        form = {
            "assetKind": "background",
            "categoryId": "mixed_rice",
            "styleId": "style-upload",
        }

        created = client.post(
            "/api/upload-library",
            data={
                **form,
                "file": (image_zip(), "library.zip"),
            },
            content_type="multipart/form-data",
            headers=headers,
        )
        assert created.status_code == 200
        created_payload = created.get_json()
        assert created_payload["ok"] is True
        assert created_payload["idempotent"] is False
        assert created_payload["assetCount"] == 1
        asset_id = created_payload["assets"][0]["assetId"]
        assert created_payload["assets"][0]["status"] == "pending"

        replay = client.post(
            "/api/upload-library",
            data={
                **form,
                "file": (image_zip(), "library.zip"),
            },
            content_type="multipart/form-data",
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.get_json()["idempotent"] is True
        assert replay.get_json()["assets"][0]["assetId"] == asset_id

        changed = client.post(
            "/api/upload-library",
            data={
                **form,
                "file": (
                    image_zip(color=(30, 140, 210)),
                    "library.zip",
                ),
            },
            content_type="multipart/form-data",
            headers=headers,
        )
        assert changed.status_code == 409
        assert changed.get_json()["code"] == "library_import_conflict"

        approved = client.post(
            f"/api/admin/actions/ai-assets/{asset_id}/status",
            json={"status": "approved", "qualityNote": "verified"},
            headers={"X-Admin-Token": "library-admin-token"},
        )
        assert approved.status_code == 200

        connection = connect_schema()
        try:
            store = product_asset_library_store.ProductAssetLibraryStore(
                connection
            )
            reusable = store.find_reusable_assets(
                tenant_id="waimai-shared",
                owner_user_id="another-user",
                taxonomy_version="2026-07-30.v2",
                category_id="mixed_rice",
                style_id="style-upload",
                standard_name="炒饭/拌饭背景",
                asset_kind="background",
                    pipeline_version=app_module.product_asset_pipeline_version(
                        "category_background"
                    ),
                include_tenant_scope=True,
                limit=1,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*)
                         FROM product_asset_library_entries),
                        (SELECT COUNT(*)
                         FROM product_library_import_batches),
                        (SELECT COUNT(*)
                         FROM product_library_import_items)
                    """
                )
                counts = cursor.fetchone()
            connection.rollback()
        finally:
            connection.close()
        assert counts == (1, 1, 1)
        assert reusable[0]["id"] == asset_id
        assert len(storage.list_prefix("ai-assets/waimai-shared")) == 1
        assert not sqlite_path.exists()

        existing_keys = set(
            storage.list_prefix("ai-assets/waimai-shared")
        )
        original_postgres = app_module.postgres_connection

        @contextmanager
        def unavailable_postgres(
            *_: Any,
            **__: Any,
        ) -> Iterator[Any]:
            raise RuntimeError("database unavailable")
            yield

        monkeypatch.setattr(
            app_module,
            "postgres_connection",
            unavailable_postgres,
        )
        failed = client.post(
            "/api/upload-library",
            data={
                **form,
                "file": (
                    image_zip(member_name="backgrounds/new.jpg"),
                    "library.zip",
                ),
            },
            content_type="multipart/form-data",
            headers={
                "X-Admin-Token": "library-admin-token",
                "Idempotency-Key": "library-import-http-2",
            },
        )
        assert failed.status_code == 503
        assert set(
            storage.list_prefix("ai-assets/waimai-shared")
        ) == existing_keys
        monkeypatch.setattr(
            app_module,
            "postgres_connection",
            original_postgres,
        )
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        admin.close()
