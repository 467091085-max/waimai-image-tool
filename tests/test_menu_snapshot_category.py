from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

import app as app_module
import background_profiles


ORIGINAL_FILENAME = "运营数据_美滋滋烤肉拌饭（成都店）.xlsx"


class SnapshotStorage:
    def __init__(self, object_key: str, raw: bytes) -> None:
        self.object_key = object_key
        self.raw = raw

    def read_bytes_limited(self, object_key: str, max_bytes: int) -> bytes:
        assert object_key == self.object_key
        assert len(self.raw) <= max_bytes
        return self.raw


@pytest.mark.parametrize("original_filename", [ORIGINAL_FILENAME, ""])
def test_materialized_menu_keeps_filename_category_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original_filename: str,
) -> None:
    source = tmp_path / "payload.xlsx"
    pd.DataFrame(
        [
            {"分类": "加料", "菜品名": "热狗肠加餐A", "价格": 1},
            {"分类": "加料", "菜品名": "热狗肠加餐B", "价格": 2},
            {"分类": "加料", "菜品名": "热狗肠加餐C", "价格": 3},
            {"分类": "加料", "菜品名": "热狗肠加餐D", "价格": 4},
            {"分类": "加料", "菜品名": "热狗肠加餐E", "价格": 5},
            {"分类": "主食", "菜品名": "招牌烤肉饭", "价格": 18},
            {"分类": "主食", "菜品名": "蜜汁烤肉饭", "价格": 19},
        ]
    ).to_excel(source, index=False)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    object_key = (
        "menus/2026/08/01/"
        f"{digest[:16]}_menu_1785600000_{ORIGINAL_FILENAME}"
    )
    storage = SnapshotStorage(object_key, raw)
    monkeypatch.setattr(
        app_module.object_storage_service,
        "get_object_storage_service",
        lambda: storage,
    )
    monkeypatch.setattr(
        app_module,
        "MODEL_INPUT_DIR",
        tmp_path / "model-inputs",
    )

    materialized = app_module.materialize_menu_upload_snapshot(
        {
            "objectKey": object_key,
            "sha256": digest,
            "originalFilename": original_filename,
        }
    )
    menu = app_module.parse_menu(materialized)
    category = background_profiles.menu_background_context(menu)

    assert materialized.parent.name == digest
    assert materialized.name == "运营数据_美滋滋烤肉拌饭(成都店).xlsx"
    assert category["taxonomyId"] == "mixed_rice"
    assert category["selectionReason"] == "store_taxonomy"
    assert category["storeTaxonomyId"] == "mixed_rice"
    assert category["fileTaxonomyId"] == "mixed_rice"
