from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage_db import (
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    LocalObjectStorage,
    create_generation_job,
    create_library_image,
    get_conn,
    init_db,
    list_generation_jobs,
    list_library_images,
    update_generation_job,
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


class StorageDbRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = init_db(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_library_image_create_list_and_update(self) -> None:
        image = create_library_image(
            self.conn,
            object_key="library/2026/06/20/noodle.jpg",
            dish_name="招牌牛肉面",
            store_name="测试店",
            style_id="style-1",
            source="clean",
            width=800,
            height=600,
            file_size=12345,
            tags=["noodle", "single"],
            metadata={"camera": "demo"},
        )

        self.assertEqual(image["dish_name"], "招牌牛肉面")
        self.assertEqual(image["normalized_dish"], "招牌牛肉面")
        self.assertTrue(image["reusable"])
        self.assertEqual(image["tags"], ["noodle", "single"])
        self.assertEqual(image["metadata"], {"camera": "demo"})

        listed = list_library_images(self.conn, dish_query="牛肉面", style_id="style-1", reusable=True)
        self.assertEqual([item["id"] for item in listed], [image["id"]])

        updated = update_library_image(
            self.conn,
            image["id"],
            dish_name="红烧牛肉面",
            reusable=False,
            tags=["review"],
            metadata={"review": "watermark"},
        )
        self.assertEqual(updated["dish_name"], "红烧牛肉面")
        self.assertEqual(updated["normalized_dish"], "红烧牛肉面")
        self.assertFalse(updated["reusable"])
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

    def test_foreign_keys_are_enforced(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            create_generation_job(self.conn, menu_upload_id="missing-upload", style_id="style-1")


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


if __name__ == "__main__":
    unittest.main()
