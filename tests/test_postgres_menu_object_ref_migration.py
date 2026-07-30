from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from shared.menu_upload_store import MenuUploadStore


ROOT = Path(__file__).resolve().parents[1]
BASE_MIGRATION = ROOT / "migrations" / "003_menu_uploads_postgres.sql"
FIX_MIGRATION = (
    ROOT / "migrations" / "012_fix_menu_object_ref_constraint.sql"
)
TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()


def test_fix_migration_replaces_unsupported_bounded_regex() -> None:
    sql = FIX_MIGRATION.read_text(encoding="utf-8")

    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert (
        "DROP CONSTRAINT IF EXISTS "
        "ck_product_menu_uploads_object_ref_relative"
    ) in sql
    assert (
        "ADD CONSTRAINT ck_product_menu_uploads_object_ref_relative"
    ) in sql
    assert "{0,1023}" not in sql
    assert "char_length(object_ref) BETWEEN 1 AND 1024" in sql
    assert "object_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'" in sql


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the real migration test",
)
def test_fix_migration_applies_twice_and_allows_real_menu_insert() -> None:
    if "test" not in TEST_POSTGRES_DSN.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    schema = f"test_menu_ref_fix_{uuid4().hex}"

    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema)
                )
            )
            cursor.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(schema)
                )
            )
            cursor.execute(BASE_MIGRATION.read_text(encoding="utf-8"))
            cursor.execute(FIX_MIGRATION.read_text(encoding="utf-8"))
            cursor.execute(FIX_MIGRATION.read_text(encoding="utf-8"))
            cursor.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname =
                    'ck_product_menu_uploads_object_ref_relative'
                  AND conrelid = 'product_menu_uploads'::regclass
                """
            )
            constraint = str(cursor.fetchone()[0])
            assert "{0,1023}" not in constraint

        connection = psycopg.connect(
            TEST_POSTGRES_DSN,
            autocommit=False,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema)
                    )
                )
            connection.commit()
            store = MenuUploadStore(connection)
            values = {
                "upload_id": "menu-real-1",
                "owner_user_id": "user-real-1",
                "object_ref": "menus/user-real-1/menu-real-1.xlsx",
                "object_sha256": "a" * 64,
                "original_filename": "menu.xlsx",
                "content_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "file_size": 128,
                "parser_version": "menu-parser-v1",
                "item_count": 2,
                "store_name": "Migration Test Store",
                "parsed_summary": {
                    "count": 2,
                    "category": "rice",
                },
            }
            created = store.create_or_get_private_record(**values)
            replayed = store.create_or_get_private_record(**values)

            assert created.created is True
            assert replayed.created is False
            assert created.record["object_ref"] == values["object_ref"]
        finally:
            connection.close()
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        admin.close()
