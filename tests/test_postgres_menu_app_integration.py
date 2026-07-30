from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import app as app_module
from shared.menu_upload_store import MenuUploadNotFound


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_file(
        self,
        local_path: Path,
        *,
        prefix: str,
        filename: str,
    ) -> str:
        key = f"{prefix.rstrip('/')}/{filename}"
        self.objects[key] = local_path.read_bytes()
        return key

    def read_bytes(self, object_key: str) -> bytes:
        return self.objects[object_key]

    def read_bytes_limited(self, object_key: str, max_bytes: int) -> bytes:
        raw = self.objects[object_key]
        if len(raw) > max_bytes:
            raise app_module.object_storage_service.ObjectStorageReadLimitExceeded(
                "object exceeds read limit"
            )
        return raw


@contextmanager
def fake_postgres_connection():
    yield object()


def test_persist_menu_upload_uses_shared_postgres_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "menu.xlsx"
    source.write_bytes(b"shared-menu-bytes")
    storage = FakeStorage()
    captured: dict[str, Any] = {}

    class FakeMenuStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def create_or_get_private_record(
            self,
            **kwargs: Any,
        ) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(record={"id": kwargs["upload_id"]})

    monkeypatch.setattr(
        app_module,
        "postgres_product_runtime_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "MenuUploadStore", FakeMenuStore)
    monkeypatch.setattr(
        app_module.object_storage_service,
        "get_object_storage_service",
        lambda: storage,
    )

    upload_id = app_module.persist_menu_upload(
        source,
        original_filename="菜单.xlsx",
        content_type="",
        menu={
            "store": "测试店",
            "count": 2,
            "category": "盖饭",
            "items": [{"name": "牛肉饭"}, {"name": "鸡肉饭"}],
        },
        owner_user_id="user-1",
    )

    assert upload_id == captured["upload_id"]
    assert captured["owner_user_id"] == "user-1"
    assert captured["object_ref"].startswith("menus/")
    assert captured["file_size"] == len(b"shared-menu-bytes")
    assert captured["parser_version"] == str(
        app_module.MENU_PARSER_VERSION
    )
    assert captured["item_count"] == 2
    assert captured["parsed_summary"] == {
        "store": "测试店",
        "count": 2,
        "category": "盖饭",
    }
    assert app_module.MENU_UPLOAD_PRIVATE_METADATA_KEY not in captured[
        "parsed_summary"
    ]
    assert captured["content_type"].endswith("spreadsheetml.sheet")


def test_resolve_postgres_menu_snapshot_checks_owner_sha_and_object(
    monkeypatch,
) -> None:
    raw = b"immutable-menu"
    digest = app_module.hashlib.sha256(raw).hexdigest()
    storage = FakeStorage()
    storage.objects["menus/menu.xlsx"] = raw

    class FakeMenuStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def get_owned_private_record(
            self,
            *,
            upload_id: str,
            owner_user_id: str,
        ) -> dict[str, Any]:
            assert upload_id == "menu_" + ("a" * 32)
            assert owner_user_id == "user-1"
            return {
                "id": upload_id,
                "owner_user_id": owner_user_id,
                "object_ref": "menus/menu.xlsx",
                "object_sha256": digest,
                "original_filename": "菜单.xlsx",
                "parser_version": str(app_module.MENU_PARSER_VERSION),
                "parsed_summary": {"count": 2, "category": "盖饭"},
                "status": "frozen",
            }

    monkeypatch.setattr(
        app_module,
        "postgres_product_runtime_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "MenuUploadStore", FakeMenuStore)
    monkeypatch.setattr(
        app_module.object_storage_service,
        "get_object_storage_service",
        lambda: storage,
    )

    snapshot = app_module.resolve_menu_upload_snapshot(
        "menu_" + ("a" * 32),
        {"userId": "user-1", "internal": False, "localDemo": False},
    )

    assert snapshot == {
        "id": "menu_" + ("a" * 32),
        "objectKey": "menus/menu.xlsx",
        "sha256": digest,
        "parserVersion": app_module.MENU_PARSER_VERSION,
        "ownerUserId": "user-1",
        "originalFilename": "菜单.xlsx",
        "summary": {"count": 2, "category": "盖饭"},
    }


def test_resolve_postgres_menu_snapshot_conceals_cross_owner(
    monkeypatch,
) -> None:
    class FakeMenuStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def get_owned_private_record(self, **_kwargs: Any) -> dict[str, Any]:
            raise MenuUploadNotFound("menu_" + ("b" * 32))

    monkeypatch.setattr(
        app_module,
        "postgres_product_runtime_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        app_module,
        "postgres_connection",
        fake_postgres_connection,
    )
    monkeypatch.setattr(app_module, "MenuUploadStore", FakeMenuStore)

    try:
        app_module.resolve_menu_upload_snapshot(
            "menu_" + ("b" * 32),
            {"userId": "user-2", "internal": False, "localDemo": False},
        )
    except app_module.MenuUploadError as exc:
        assert exc.code == "menu_upload_not_found"
        assert exc.status == 404
    else:
        raise AssertionError("cross-owner upload lookup must be concealed")
