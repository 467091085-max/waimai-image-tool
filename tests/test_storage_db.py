from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage_db import (
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    LocalObjectStorage,
    create_export,
    create_generation_job,
    create_generation_job_item,
    create_generation_result,
    create_library_image,
    create_menu,
    create_menu_item,
    get_conn,
    get_generation_job,
    get_menu,
    init_db,
    list_exports,
    list_generation_job_items,
    list_generation_jobs,
    list_generation_results,
    list_library_images,
    list_menu_items,
    update_generation_job,
    update_generation_job_item,
    update_library_image,
)


class StorageDbSchemaTests(unittest.TestCase):
    def test_init_db_creates_required_tables_and_version(self) -> None:
        conn = init_db(":memory:")
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            table_names = {row["name"] for row in rows}
            self.assertGreaterEqual(table_names, REQUIRED_TABLES)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
            self.assertIn("menu_id", job_columns)
            library_columns = {row["name"] for row in conn.execute("PRAGMA table_info(library_images)")}
            self.assertGreaterEqual(
                library_columns,
                {"canonical_dish_id", "has_brand_watermark", "has_dish_text", "quality_score", "file_url"},
            )
        finally:
            conn.close()

    def test_get_conn_creates_parent_directory_for_file_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "storage.sqlite3"
            conn = get_conn(db_path)
            try:
                init_db(conn)
                self.assertTrue(db_path.exists())
            finally:
                conn.close()

    def test_init_db_adds_new_columns_to_existing_sqlite_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "storage.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE library_images (
                        id TEXT PRIMARY KEY,
                        object_key TEXT NOT NULL UNIQUE,
                        thumbnail_object_key TEXT NOT NULL DEFAULT '',
                        sha256 TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT 'library',
                        store_name TEXT NOT NULL DEFAULT '',
                        dish_name TEXT NOT NULL,
                        normalized_dish TEXT NOT NULL DEFAULT '',
                        category_path TEXT NOT NULL DEFAULT '',
                        style_id TEXT NOT NULL DEFAULT 'style-upload',
                        width INTEGER NOT NULL DEFAULT 0,
                        height INTEGER NOT NULL DEFAULT 0,
                        file_size INTEGER NOT NULL DEFAULT 0,
                        reusable INTEGER NOT NULL DEFAULT 1 CHECK (reusable IN (0, 1)),
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE generation_jobs (
                        id TEXT PRIMARY KEY,
                        menu_upload_id TEXT,
                        style_id TEXT NOT NULL DEFAULT '',
                        quality TEXT NOT NULL DEFAULT 'standard',
                        status TEXT NOT NULL DEFAULT 'queued',
                        requested_count INTEGER NOT NULL DEFAULT 0,
                        completed_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            migrated = init_db(db_path)
            try:
                library_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(library_images)")}
                job_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(generation_jobs)")}
                self.assertIn("canonical_dish_id", library_columns)
                self.assertIn("has_brand_watermark", library_columns)
                self.assertIn("menu_id", job_columns)
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            finally:
                migrated.close()


class StorageDbRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = init_db(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_menu_create_read_items_and_legacy_upload_mirror(self) -> None:
        menu = create_menu(
            self.conn,
            store_name="测试店",
            original_filename="menu.xlsx",
            object_key="menus/menu.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            file_size=456,
            sha256="abc",
            status="parsed",
            parsed_summary={"count": 2},
            metadata={"source": "unit"},
            items=[
                {"row": 12, "sheet": "菜单", "category": "热销", "name": "辣椒炒肉盖码饭", "price": "19.8", "kind": "单品"},
                {
                    "row": 13,
                    "sheet": "菜单",
                    "category": "套餐",
                    "name": "牛肉+鸡胸双拼能量碗",
                    "price": "27.8",
                    "kind": "套餐",
                    "components": ["牛肉", "鸡胸"],
                },
            ],
        )

        self.assertEqual(menu["store_name"], "测试店")
        self.assertEqual(menu["parsed_summary"], {"count": 2})
        self.assertEqual(menu["metadata"], {"source": "unit"})
        self.assertEqual(len(menu["items"]), 2)
        self.assertEqual(menu["items"][0]["normalized_name"], "辣椒炒肉盖码饭")
        self.assertEqual(menu["items"][1]["components"], ["牛肉", "鸡胸"])

        extra = create_menu_item(
            self.conn,
            menu["id"],
            name="冰红茶",
            category="饮品",
            kind="小食",
            row_number=14,
            sort_order=3,
        )
        self.assertEqual(extra["normalized_name"], "冰红茶")
        self.assertEqual(len(list_menu_items(self.conn, menu["id"])), 3)
        self.assertEqual(get_menu(self.conn, menu["id"])["items"][2]["name"], "冰红茶")

        legacy = self.conn.execute("SELECT * FROM menu_uploads WHERE id = ?", (menu["id"],)).fetchone()
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy["object_key"], "menus/menu.xlsx")

    def test_library_image_create_list_and_update(self) -> None:
        image = create_library_image(
            self.conn,
            object_key="library/2026/06/20/noodle.jpg",
            dish_name="招牌牛肉面",
            store_name="测试店",
            style_id="style-1",
            source="clean",
            file_url="cos://bucket/library/noodle.jpg",
            canonical_dish_id="dish-noodle",
            width=800,
            height=600,
            file_size=12345,
            has_brand_watermark=False,
            has_dish_text=True,
            quality_score=0.92,
            tags=["noodle", "single"],
            metadata={"camera": "demo"},
        )

        self.assertEqual(image["dish_name"], "招牌牛肉面")
        self.assertEqual(image["normalized_dish"], "招牌牛肉面")
        self.assertEqual(image["canonical_dish_id"], "dish-noodle")
        self.assertEqual(image["file_url"], "cos://bucket/library/noodle.jpg")
        self.assertTrue(image["reusable"])
        self.assertFalse(image["has_brand_watermark"])
        self.assertTrue(image["has_dish_text"])
        self.assertEqual(image["quality_score"], 0.92)
        self.assertEqual(image["tags"], ["noodle", "single"])
        self.assertEqual(image["metadata"], {"camera": "demo"})

        listed = list_library_images(self.conn, dish_query="牛肉面", style_id="style-1", reusable=True)
        self.assertEqual([item["id"] for item in listed], [image["id"]])

        updated = update_library_image(
            self.conn,
            image["id"],
            dish_name="红烧牛肉面",
            reusable=False,
            has_brand_watermark=True,
            quality_score=0.2,
            tags=["review"],
            metadata={"review": "watermark"},
        )
        self.assertEqual(updated["dish_name"], "红烧牛肉面")
        self.assertEqual(updated["normalized_dish"], "红烧牛肉面")
        self.assertFalse(updated["reusable"])
        self.assertTrue(updated["has_brand_watermark"])
        self.assertEqual(updated["quality_score"], 0.2)
        self.assertEqual(updated["tags"], ["review"])
        self.assertEqual(updated["metadata"], {"review": "watermark"})
        self.assertEqual(list_library_images(self.conn, reusable=True), [])

    def test_generation_job_create_list_and_status_transitions(self) -> None:
        job = create_generation_job(
            self.conn,
            style_id="style-2",
            quality="premium",
            requested_count=3,
            request={"rows": [1, 2, 3]},
        )

        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["request"], {"rows": [1, 2, 3]})
        self.assertIsNone(job["started_at"])

        running = update_generation_job(self.conn, job["id"], status="running", completed_count=1)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["completed_count"], 1)
        self.assertIsNotNone(running["started_at"])

        succeeded = update_generation_job(
            self.conn,
            job["id"],
            status="succeeded",
            completed_count=3,
            result={"generated": 3},
        )
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["completed_count"], 3)
        self.assertEqual(succeeded["result"], {"generated": 3})
        self.assertIsNotNone(succeeded["completed_at"])

        self.assertEqual([item["id"] for item in list_generation_jobs(self.conn, status="succeeded")], [job["id"]])
        with self.assertRaises(ValueError):
            update_generation_job(self.conn, job["id"], status="running")

    def test_generation_job_items_results_and_exports(self) -> None:
        menu = create_menu(
            self.conn,
            store_name="测试店",
            original_filename="menu.xlsx",
            object_key="menus/menu.xlsx",
            items=[{"row": 8, "name": "香煎鸡胸能量碗", "kind": "单品"}],
        )
        menu_item = menu["items"][0]
        library_image = create_library_image(
            self.conn,
            object_key="library/chicken.jpg",
            dish_name="香煎鸡胸能量碗",
            style_id="style-2",
        )

        job = create_generation_job(
            self.conn,
            menu_id=menu["id"],
            style_id="style-2",
            items=[
                {
                    "menu_item_id": menu_item["id"],
                    "menu_row": menu_item["row_number"],
                    "dish_name": menu_item["name"],
                    "item_kind": menu_item["kind"],
                    "library_image_id": library_image["id"],
                    "action": "reuse",
                }
            ],
        )

        self.assertEqual(job["menu_id"], menu["id"])
        self.assertEqual(job["menu_upload_id"], menu["id"])
        self.assertEqual(job["requested_count"], 1)
        item = list_generation_job_items(self.conn, job_id=job["id"])[0]
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["library_image_id"], library_image["id"])

        running = update_generation_job_item(self.conn, item["id"], status="running")
        self.assertEqual(running["status"], "running")
        self.assertIsNotNone(running["started_at"])
        done = update_generation_job_item(
            self.conn,
            item["id"],
            status="succeeded",
            result={"objectKey": "generated/chicken.jpg"},
        )
        self.assertEqual(done["result"], {"objectKey": "generated/chicken.jpg"})
        self.assertIsNotNone(done["completed_at"])
        counted = get_generation_job(self.conn, job["id"], include_items=True)
        self.assertEqual(counted["completed_count"], 1)
        self.assertEqual(counted["failed_count"], 0)
        self.assertEqual(len(counted["items"]), 1)
        with self.assertRaises(ValueError):
            update_generation_job_item(self.conn, item["id"], status="running")

        result = create_generation_result(
            self.conn,
            job_id=job["id"],
            job_item_id=item["id"],
            object_key="generated/chicken.jpg",
            platform="meituan",
            width=800,
            height=600,
            file_size=3210,
            metadata={"model": "cached"},
        )
        self.assertEqual(result["dish_name"], "香煎鸡胸能量碗")
        self.assertEqual(result["metadata"], {"model": "cached"})
        self.assertEqual([row["id"] for row in list_generation_results(self.conn, job_id=job["id"])], [result["id"]])
        legacy_image = self.conn.execute("SELECT * FROM generated_images WHERE id = ?", (result["id"],)).fetchone()
        self.assertIsNotNone(legacy_image)

        export = create_export(
            self.conn,
            job_id=job["id"],
            object_key="exports/chicken.zip",
            platform="meituan",
            scope="selected",
            image_count=1,
            file_size=4096,
            metadata={"format": "zip"},
        )
        self.assertEqual(export["metadata"], {"format": "zip"})
        self.assertEqual([row["id"] for row in list_exports(self.conn, job_id=job["id"])], [export["id"]])
        legacy_export = self.conn.execute("SELECT * FROM export_packages WHERE id = ?", (export["id"],)).fetchone()
        self.assertIsNotNone(legacy_export)

    def test_foreign_keys_are_enforced(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            create_generation_job(self.conn, menu_upload_id="missing-upload", style_id="style-1")
        with self.assertRaises(sqlite3.IntegrityError):
            create_menu_item(self.conn, "missing-menu", name="不存在菜单项")
        with self.assertRaises(sqlite3.IntegrityError):
            create_generation_job_item(self.conn, "missing-job", dish_name="不存在任务项")
        with self.assertRaises(sqlite3.IntegrityError):
            create_generation_result(self.conn, job_id="missing-job", object_key="generated/missing.jpg")
        with self.assertRaises(sqlite3.IntegrityError):
            create_export(self.conn, job_id="missing-job", object_key="exports/missing.zip")

    def test_foreign_key_cascades_menu_items_and_job_items(self) -> None:
        menu = create_menu(
            self.conn,
            store_name="测试店",
            original_filename="menu.xlsx",
            object_key="menus/menu.xlsx",
            items=[{"row": 1, "name": "番茄炒蛋"}],
        )
        job = create_generation_job(
            self.conn,
            menu_id=menu["id"],
            items=[{"menu_item_id": menu["items"][0]["id"], "dish_name": "番茄炒蛋"}],
        )
        self.conn.execute("DELETE FROM menus WHERE id = ?", (menu["id"],))
        self.assertEqual(list_menu_items(self.conn, menu["id"]), [])
        self.assertIsNone(self.conn.execute("SELECT menu_id FROM generation_jobs WHERE id = ?", (job["id"],)).fetchone()[0])
        self.assertIsNone(
            self.conn.execute("SELECT menu_item_id FROM generation_job_items WHERE job_id = ?", (job["id"],)).fetchone()[0]
        )


class LocalObjectStorageTests(unittest.TestCase):
    def test_put_and_read_bytes_under_object_store_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data" / "object_store"
            store = LocalObjectStorage(root)

            key = store.put_bytes(b"image-bytes", prefix="library/images", filename="菜品图.jpg")
            self.assertFalse(key.startswith("/"))
            self.assertIn("library/images", key)
            self.assertTrue(store.path_for_key(key).is_file())
            self.assertEqual(store.read_bytes(key), b"image-bytes")
            self.assertIn(root.resolve(), store.path_for_key(key).resolve().parents)

    def test_put_file_and_reject_traversal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data" / "object_store"
            source = Path(tmp) / "upload.xlsx"
            source.write_bytes(b"menu")
            store = LocalObjectStorage(root)

            key = store.put_file(source, prefix="menu_uploads")
            self.assertEqual(store.read_bytes(key), b"menu")
            with self.assertRaises(ValueError):
                store.read_bytes("../outside.txt")
            with self.assertRaises(ValueError):
                store.read_bytes("safe/../../outside.txt")
            with self.assertRaises(ValueError):
                store.read_bytes(r"safe\..\outside.txt")
            with self.assertRaises(ValueError):
                store.path_for_key("")


if __name__ == "__main__":
    unittest.main()
