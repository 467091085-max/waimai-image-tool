from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from shared import product_asset_library_store
from shared import product_library_import_store as import_store


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    ROOT / "migrations" / "009_product_asset_library_postgres.sql",
    ROOT / "migrations" / "011_product_admin_security_postgres.sql",
    ROOT / "migrations" / "013_product_library_imports_postgres.sql",
)
TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()


def test_stable_id_and_input_validation_are_fail_closed() -> None:
    first = import_store.stable_library_import_id(
        "waimai-shared",
        "import-request-1",
    )
    second = import_store.stable_library_import_id(
        "waimai-shared",
        "import-request-1",
    )
    assert first == second
    assert first.startswith("library_import_")

    class NoSqlCursor:
        description = None

        def execute(self, *_args, **_kwargs):
            raise AssertionError("invalid input reached SQL")

    with pytest.raises(import_store.InvalidProductLibraryImport):
        import_store.record_import_batch(
            NoSqlCursor(),
            tenant_id="waimai-shared",
            actor_user_id="service:admin",
            idempotency_key="import-request-1",
            request_sha256="a" * 64,
            items=[
                {
                    "member_name": "../image.jpg",
                    "asset_id": "asset-" + ("1" * 40),
                    "object_ref": (
                        "ai-assets/waimai-shared/"
                        + "asset-"
                        + ("1" * 40)
                        + "/original.jpg"
                    ),
                    "object_sha256": "b" * 64,
                    "object_size_bytes": 10,
                }
            ],
        )


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the real import-store test",
)
def test_real_postgres_import_batch_is_atomic_replayable_and_immutable() -> None:
    if "test" not in TEST_POSTGRES_DSN.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    schema = f"test_library_import_{uuid4().hex}"
    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        connection = psycopg.connect(TEST_POSTGRES_DSN, autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema)
                    )
                )
            connection.commit()
            with connection.cursor() as cursor:
                for migration in MIGRATIONS:
                    source = migration.read_text(encoding="utf-8")
                    cursor.execute(source)
                    cursor.execute(source)
            connection.commit()

            tenant_id = "waimai-shared"
            idempotency_key = "asset-import-item-1"
            asset_id = product_asset_library_store.tenant_bound_asset_id(
                tenant_id,
                idempotency_key,
            )
            object_ref = (
                f"ai-assets/{tenant_id}/{asset_id}/original.jpg"
            )
            item = {
                "member_name": "backgrounds/rice.jpg",
                "asset_id": asset_id,
                "object_ref": object_ref,
                "object_sha256": "b" * 64,
                "object_size_bytes": 128,
            }

            with connection.cursor() as cursor:
                asset = product_asset_library_store.register_asset(
                    cursor,
                    tenant_id=tenant_id,
                    owner_user_id="service:admin-api-token",
                    idempotency_key=idempotency_key,
                    asset_kind="background",
                    taxonomy_version="2026-07-30.v2",
                    category_id="mixed_rice",
                    category_name="炒饭/拌饭",
                    style_id="style-upload",
                    standard_name="炒饭/拌饭背景",
                    aliases=["炒饭背景"],
                    match_keywords=["炒饭", "背景"],
                    reuse_scope="tenant",
                    source_kind="imported",
                    source_provider="admin-upload",
                    prompt_version="style-background.v2",
                    model_name="manual-upload",
                    model_version="manual.v1",
                    pipeline_version="style-background.v2",
                    original_object_ref=object_ref,
                    original_sha256="b" * 64,
                    original_size_bytes=128,
                )
                created = import_store.record_import_batch(
                    cursor,
                    tenant_id=tenant_id,
                    actor_user_id="service:admin-api-token",
                    idempotency_key="library-import-request-1",
                    request_sha256="a" * 64,
                    items=[item],
                )
            connection.commit()
            assert asset.created is True
            assert created.created is True

            with connection.cursor() as cursor:
                replay = import_store.record_import_batch(
                    cursor,
                    tenant_id=tenant_id,
                    actor_user_id="service:admin-api-token",
                    idempotency_key="library-import-request-1",
                    request_sha256="a" * 64,
                    items=[item],
                )
            connection.commit()
            assert replay.created is False
            assert replay.items[0]["asset_id"] == asset_id

            with pytest.raises(
                import_store.ProductLibraryImportConflict
            ):
                with connection.cursor() as cursor:
                    import_store.record_import_batch(
                        cursor,
                        tenant_id=tenant_id,
                        actor_user_id="service:admin-api-token",
                        idempotency_key="library-import-request-1",
                        request_sha256="c" * 64,
                        items=[item],
                    )
            connection.rollback()

            with pytest.raises(psycopg.Error) as exc_info:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM product_library_import_batches
                        WHERE id = %s
                        """,
                        (created.batch["id"],),
                    )
            assert exc_info.value.sqlstate == "55000"
            connection.rollback()

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*)
                         FROM product_library_import_batches),
                        (SELECT COUNT(*)
                         FROM product_library_import_items),
                        (SELECT COUNT(*)
                         FROM product_asset_library_entries)
                    """
                )
                counts = cursor.fetchone()
            connection.rollback()
            assert counts == (1, 1, 1)
        finally:
            connection.rollback()
            connection.close()
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        admin.close()
