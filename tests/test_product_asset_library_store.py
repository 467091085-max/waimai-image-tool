from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_asset_library_store as library


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "009_product_asset_library_postgres.sql"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.rows: list[dict[str, Any]] = []
        self.closed = False

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        match = re.search(
            r"/\* product_asset_library_store:([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        name = match.group(1)
        placeholder_count = operation.count("%s")
        if placeholder_count != len(parameters):
            raise AssertionError(
                f"{name} expected {placeholder_count} SQL parameters, "
                f"received {len(parameters)}"
            )
        self.connection.calls.append((name, operation, tuple(parameters)))
        responses = self.connection.responses.get(name)
        if not responses:
            raise AssertionError(f"unexpected or exhausted SQL operation: {name}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.rows = list(response)
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = list(self.rows)
        self.rows = []
        return rows

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    def __init__(
        self,
        *,
        commit_error: BaseException | None = None,
        **responses: list[Any],
    ) -> None:
        self.responses = {name: list(items) for name, items in responses.items()}
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.cursors: list[ScriptedCursor] = []
        self.commit_error = commit_error

    def cursor(self) -> ScriptedCursor:
        cursor = ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1


class AutocommitConnection(ScriptedConnection):
    autocommit = True


def combo_components() -> list[dict[str, Any]]:
    return [
        {"name": "鱼香肉丝", "role": "main", "quantity": 1},
        {"name": "米饭", "role": "staple", "quantity": 1, "specification": "大份"},
        {"name": "可乐", "role": "drink", "quantity": 1, "specification": "330ml"},
    ]


def registration_kwargs(**overrides: Any) -> dict[str, Any]:
    tenant_id = str(overrides.get("tenant_id", "tenant-1"))
    idempotency_key = str(
        overrides.get("idempotency_key", "asset-idem-1")
    )
    asset_id = library.tenant_bound_asset_id(tenant_id, idempotency_key)
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "owner_user_id": "owner-1",
        "idempotency_key": idempotency_key,
        "asset_kind": "product",
        "taxonomy_version": "2026-07-30.v2",
        "category_id": "home_stir_fry",
        "category_name": "Home Stir Fry",
        "style_id": "clean_bright",
        "background_asset_id": "bg_exact_1",
        "background_sha256": DIGEST_C,
        "standard_name": "Tomato Egg",
        "aliases": ["Tomato and Egg", "tomato-egg"],
        "match_keywords": ["tomato", "egg"],
        "reuse_scope": "owner",
        "source_kind": "generated",
        "source_provider": "tencent-hunyuan",
        "prompt_version": "dish.v3",
        "model_name": "hy-image",
        "model_version": "v3.0",
        "pipeline_version": "asset-pipeline.v2",
        "original_object_ref": (
            f"ai-assets/{tenant_id}/{asset_id}/original.png"
        ),
        "original_sha256": DIGEST_A,
        "original_size_bytes": 1234,
        "derivative_object_ref": (
            f"ai-assets/{tenant_id}/{asset_id}/meituan.jpg"
        ),
        "derivative_sha256": DIGEST_B,
        "derivative_size_bytes": 987,
        "combo_fingerprint_version": "",
        "combo_components": None,
        "combo_fingerprint_sha256": "",
    }
    values.update(overrides)
    return values


def background_registration_kwargs(**overrides: Any) -> dict[str, Any]:
    tenant_id = str(overrides.get("tenant_id", "tenant-1"))
    idempotency_key = str(
        overrides.get("idempotency_key", "background-idem-1")
    )
    asset_id = library.tenant_bound_asset_id(tenant_id, idempotency_key)
    values = registration_kwargs(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        asset_kind="background",
        category_id="home_stir_fry",
        category_name="Home Stir Fry",
        background_asset_id="",
        background_sha256="",
        standard_name="Clean Bright Background",
        aliases=[],
        match_keywords=[],
        original_object_ref=(
            f"ai-assets/{tenant_id}/{asset_id}/background.png"
        ),
        original_sha256=DIGEST_C,
        derivative_object_ref=None,
        derivative_sha256=None,
        derivative_size_bytes=None,
    )
    values.update(overrides)
    return values


def asset_row(**overrides: Any) -> dict[str, Any]:
    registration_fields = registration_kwargs()
    for key, value in overrides.items():
        if key in registration_fields or key == "asset_id":
            registration_fields[key] = value
    row = library._normalize_registration(  # type: ignore[attr-defined]
        **registration_fields
    )
    row.update(
        {
            "status": "pending_review",
            "review_status": "pending",
            "reviewer_user_id": None,
            "review_note": "",
            "reviewed_at": None,
            "disabled_by_user_id": None,
            "disable_note": "",
            "disabled_at": None,
            "created_at": "2026-07-30T00:00:00Z",
            "updated_at": "2026-07-30T00:00:00Z",
        }
    )
    row.update(overrides)
    return row


def approved_row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": "approved",
        "review_status": "approved",
        "reviewer_user_id": "reviewer-1",
        "review_note": "quality-pass",
        "reviewed_at": "2026-07-30T00:01:00Z",
    }
    values.update(overrides)
    return asset_row(**values)


def background_row(
    *,
    status: str = "approved",
    **overrides: Any,
) -> dict[str, Any]:
    tenant_id = str(overrides.get("tenant_id", "tenant-1"))
    owner_user_id = str(overrides.get("owner_user_id", "owner-1"))
    idempotency_key = str(
        overrides.get("idempotency_key", "background-idem-1")
    )
    asset_id = library.tenant_bound_asset_id(tenant_id, idempotency_key)
    row = asset_row(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        asset_kind="background",
        category_id="home_stir_fry",
        category_name="Home Stir Fry",
        background_asset_id="",
        background_sha256="",
        standard_name="Clean Bright Background",
        aliases=[],
        match_keywords=[],
        original_object_ref=(
            f"ai-assets/{tenant_id}/{asset_id}/background.png"
        ),
        original_sha256=overrides.get("original_sha256", DIGEST_C),
        derivative_object_ref=None,
        derivative_sha256=None,
        derivative_size_bytes=None,
    )
    if status == "approved":
        row.update(
            status="approved",
            review_status="approved",
            reviewer_user_id="reviewer-1",
            review_note="background-quality-pass",
            reviewed_at="2026-07-30T00:01:00Z",
        )
    elif status == "rejected":
        row.update(
            status="rejected",
            review_status="rejected",
            reviewer_user_id="reviewer-1",
            review_note="background-quality-fail",
            reviewed_at="2026-07-30T00:01:00Z",
        )
    elif status == "disabled":
        row.update(
            status="disabled",
            review_status="approved",
            reviewer_user_id="reviewer-1",
            review_note="background-quality-pass",
            reviewed_at="2026-07-30T00:01:00Z",
            disabled_by_user_id="reviewer-2",
            disable_note="background-disabled",
            disabled_at="2026-07-30T00:02:00Z",
        )
    elif status != "pending_review":
        raise AssertionError(f"unsupported test background status: {status}")
    row.update(overrides)
    return row


def test_migration_has_style_audit_and_global_object_invariants() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()

    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE IF NOT EXISTS product_asset_library_entries" in sql
    assert "style_id TEXT NOT NULL" in sql
    assert "background_asset_id TEXT NOT NULL" in sql
    assert "background_sha256 TEXT NOT NULL" in sql
    assert "ck_product_asset_library_background_binding" in sql
    assert "status IN (" in sql and "'disabled'" in sql
    assert "product_asset_library_status_events" in sql
    assert "from_status = 'approved'" in sql
    assert "to_status = 'disabled'" in sql
    assert "product_asset_library_object_refs" in sql
    assert "object_ref TEXT PRIMARY KEY" in sql
    assert "LIKE ('ai-assets/' || tenant_id || '/%')" in sql
    assert "product_asset_library_claim_object_refs" in sql
    assert "combo_fingerprint_version = 'combo-components.v1'" in sql
    assert "jsonb_array_length(combo_components) >= 2" in sql
    assert "style_id," in sql.split(
        "CREATE INDEX idx_product_asset_library_exact_reuse", 1
    )[1]
    assert "pipeline_version," in sql.split(
        "CREATE INDEX idx_product_asset_library_exact_reuse", 1
    )[1]
    assert "idx_product_asset_library_keywords" in sql
    assert "DROP INDEX IF EXISTS idx_product_asset_library_keywords" in sql
    assert "never used for direct asset reuse" in lowered
    assert "local_path" not in lowered
    assert "file://" not in lowered
    for forbidden in ("api_key", "private_key", "access_token", "amount_cents"):
        assert forbidden not in lowered


def test_store_rejects_autocommit_connection() -> None:
    with pytest.raises(library.InvalidProductAssetInput):
        library.ProductAssetLibraryStore(AutocommitConnection())


def test_asset_id_is_server_derived_and_tenant_bound() -> None:
    first = library.tenant_bound_asset_id("tenant-1", "idem-1")
    assert re.fullmatch(r"asset-[0-9a-f]{40}", first)
    assert first == library.tenant_bound_asset_id("tenant-1", "idem-1")
    assert first != library.tenant_bound_asset_id("tenant-2", "idem-1")

    cursor = ScriptedConnection().cursor()
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="server-derived tenant-bound",
    ):
        library.register_asset(
            cursor,
            **registration_kwargs(asset_id="asset-" + ("0" * 40)),
        )
    assert cursor.connection.calls == []


def test_external_cursor_registration_does_not_commit() -> None:
    inserted = asset_row()
    connection = ScriptedConnection(insert_asset=[[inserted]])
    cursor = connection.cursor()

    result = library.register_asset(cursor, **registration_kwargs())

    assert result.created is True
    assert result.record["id"] == library.tenant_bound_asset_id(
        "tenant-1",
        "asset-idem-1",
    )
    assert result.record["style_id"] == "clean_bright"
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert [call[0] for call in connection.calls] == ["insert_asset"]


def test_wrapper_commits_exact_idempotent_replay() -> None:
    existing = asset_row()
    connection = ScriptedConnection(
        insert_asset=[[]],
        select_by_idempotency_for_update=[[existing]],
    )

    result = library.ProductAssetLibraryStore(connection).register_asset(
        **registration_kwargs()
    )

    assert result.created is False
    assert result.record["id"] == existing["id"]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert all(cursor.closed for cursor in connection.cursors)


def test_replay_content_drift_rolls_back_wrapper() -> None:
    existing = asset_row()
    connection = ScriptedConnection(
        insert_asset=[[]],
        select_by_idempotency_for_update=[[existing]],
    )

    with pytest.raises(library.ProductAssetConflict):
        library.ProductAssetLibraryStore(connection).register_asset(
            **registration_kwargs(style_id="dark_wood")
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_unresolved_cross_role_object_conflict_is_explicit() -> None:
    existing = asset_row()
    connection = ScriptedConnection(
        insert_asset=[[]],
        select_by_idempotency_for_update=[[]],
        select_by_id_for_update=[[]],
        select_by_object_ref_for_update=[[existing]],
    )

    with pytest.raises(library.ProductAssetConflict, match="object_ref"):
        library.register_asset(
            connection.cursor(),
            **registration_kwargs(idempotency_key="asset-idem-2"),
        )

    _name, sql, parameters = connection.calls[-1]
    assert "original_object_ref = %s OR derivative_object_ref = %s" in sql
    assert parameters[0] == parameters[1]


@pytest.mark.parametrize(
    ("tenant_id", "object_ref"),
    (
        ("tenant-1", "/ai-assets/tenant-1/a.png"),
        ("tenant-1", "https://cdn.example/a.png"),
        ("tenant-1", "ai-assets/tenant-1/../a.png"),
        ("tenant-1", "ai-assets/tenant-2/a.png"),
        ("tenant-1", "ai-assets/tenant-1//a.png"),
        ("tenant:1", "ai-assets/tenant:1/a.png"),
    ),
)
def test_registration_rejects_non_tenant_private_object_refs(
    tenant_id: str,
    object_ref: str,
) -> None:
    connection = ScriptedConnection()
    with pytest.raises(library.InvalidProductAssetInput):
        library.register_asset(
            connection.cursor(),
            **registration_kwargs(
                tenant_id=tenant_id,
                original_object_ref=object_ref,
            ),
        )
    assert connection.calls == []


def test_style_and_derivative_tuple_fail_closed() -> None:
    cursor = ScriptedConnection().cursor()
    with pytest.raises(library.InvalidProductAssetInput, match="provided together"):
        library.register_asset(
            cursor,
            **registration_kwargs(derivative_sha256=None),
        )
    with pytest.raises(library.InvalidProductAssetInput, match="style_id"):
        library.register_asset(
            cursor,
            **registration_kwargs(style_id=""),
        )
    assert cursor.connection.calls == []


def test_product_registration_requires_exact_background_binding() -> None:
    cursor = ScriptedConnection().cursor()
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="exact selected background",
    ):
        library.register_asset(
            cursor,
            **registration_kwargs(
                background_asset_id="",
                background_sha256="",
            ),
        )
    assert cursor.connection.calls == []


def test_background_registration_rejects_inherited_background_binding() -> None:
    cursor = ScriptedConnection().cursor()
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="cannot reference another background",
    ):
        library.register_asset(
            cursor,
            **registration_kwargs(
                asset_kind="background",
                background_asset_id="bg_exact_1",
                background_sha256=DIGEST_C,
            ),
        )
    assert cursor.connection.calls == []


def test_combo_fingerprint_is_computed_from_versioned_components() -> None:
    canonical, digest = library.combo_components_fingerprint(
        combo_components()
    )
    reordered = list(reversed(combo_components()))
    reordered_canonical, reordered_digest = (
        library.combo_components_fingerprint(reordered)
    )
    assert reordered_canonical == canonical
    assert reordered_digest == digest
    normalized_variant = combo_components()
    normalized_variant[2] = {
        "name": "可 乐",
        "role": "drink",
        "quantity": 1,
        "specification": "330 ML",
    }
    _variant_canonical, variant_digest = (
        library.combo_components_fingerprint(normalized_variant)
    )
    assert variant_digest == digest
    assert canonical[0]["role"] == "main"

    cursor = ScriptedConnection().cursor()
    base = registration_kwargs(
        category_id="combo",
        category_name="Combo",
        standard_name="鱼香肉丝套餐",
        combo_fingerprint_version=library.COMBO_FINGERPRINT_VERSION,
        combo_components=None,
        combo_fingerprint_sha256=digest,
    )
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="server-owned combo_components",
    ):
        library.register_asset(cursor, **base)

    with pytest.raises(
        library.InvalidProductAssetInput,
        match="does not match",
    ):
        library.register_asset(
            cursor,
            **{
                **base,
                "combo_components": combo_components(),
                "combo_fingerprint_sha256": "f" * 64,
            },
        )

    duplicated = combo_components() + [
        {"name": "可乐", "role": "drink", "quantity": 1, "specification": "330ml"}
    ]
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="quantity",
    ):
        library.combo_components_fingerprint(duplicated)

    missing_main = [
        {"name": "米饭", "role": "staple"},
        {"name": "可乐", "role": "drink"},
    ]
    with pytest.raises(library.InvalidProductAssetInput, match="main"):
        library.combo_components_fingerprint(missing_main)


def test_owned_lookup_conceals_other_owner() -> None:
    row = asset_row()
    identifier = row["id"]
    found_connection = ScriptedConnection(select_owned=[[row]])
    found = library.get_owned_asset(
        found_connection.cursor(),
        asset_id=identifier,
        tenant_id="tenant-1",
        owner_user_id="owner-1",
    )
    assert found["id"] == identifier
    assert found_connection.calls[0][2] == (
        identifier,
        "tenant-1",
        "owner-1",
    )

    missing_connection = ScriptedConnection(select_owned=[[]])
    with pytest.raises(library.ProductAssetNotFound):
        library.get_owned_asset(
            missing_connection.cursor(),
            asset_id=identifier,
            tenant_id="tenant-1",
            owner_user_id="owner-2",
        )


def test_admin_lookup_and_list_use_server_validated_inputs() -> None:
    row = asset_row()
    lookup_connection = ScriptedConnection(select_by_id=[[row]])
    found = library.get_asset(
        lookup_connection.cursor(),
        asset_id=row["id"],
    )
    assert found["id"] == row["id"]

    list_connection = ScriptedConnection(list_for_admin=[[row]])
    records = library.list_assets_for_admin(
        list_connection.cursor(),
        status="pending_review",
        limit=25,
        offset=5,
    )
    assert [record["id"] for record in records] == [row["id"]]
    _name, sql, parameters = list_connection.calls[0]
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert parameters == ("pending_review", "pending_review", 25, 5)

    summary_connection = ScriptedConnection(
        summarize_for_admin=[
            [
                {
                    "status": "pending_review",
                    "asset_kind": "product",
                    "category_name": "Home Stir Fry",
                    "asset_count": 3,
                }
            ]
        ]
    )
    summary = library.summarize_assets_for_admin(
        summary_connection.cursor()
    )
    assert summary[0]["asset_count"] == 3

    cursor = ScriptedConnection().cursor()
    with pytest.raises(library.InvalidProductAssetInput):
        library.list_assets_for_admin(cursor, status="pending")
    with pytest.raises(library.InvalidProductAssetInput):
        library.list_assets_for_admin(cursor, offset=-1)
    assert cursor.connection.calls == []


def test_review_is_one_way_and_exact_replay_is_idempotent() -> None:
    background = background_row()
    pending = asset_row(
        background_asset_id=background["id"],
        background_sha256=background["original_sha256"],
    )
    approved = approved_row(
        background_asset_id=background["id"],
        background_sha256=background["original_sha256"],
    )
    identifier = approved["id"]
    first_connection = ScriptedConnection(
        select_for_review=[[pending]],
        select_background_for_approval=[[background]],
        update_review=[[approved]],
    )
    first = library.review_asset(
        first_connection.cursor(),
        asset_id=identifier,
        tenant_id="tenant-1",
        reviewer_user_id="reviewer-1",
        decision="approved",
        review_note="quality-pass",
    )
    assert first.idempotent is False
    assert [call[0] for call in first_connection.calls] == [
        "select_for_review",
        "select_background_for_approval",
        "update_review",
    ]
    _name, background_sql, background_parameters = first_connection.calls[1]
    assert "FOR UPDATE" in background_sql
    assert background_parameters == (background["id"],)

    replay_connection = ScriptedConnection(
        select_for_review=[[approved]],
    )
    replay = library.review_asset(
        replay_connection.cursor(),
        asset_id=identifier,
        tenant_id="tenant-1",
        reviewer_user_id="reviewer-1",
        decision="approved",
        review_note="quality-pass",
    )
    assert replay.idempotent is True

    drift_connection = ScriptedConnection(
        select_for_review=[[approved]],
    )
    with pytest.raises(library.ProductAssetReviewConflict):
        library.review_asset(
            drift_connection.cursor(),
            asset_id=identifier,
            tenant_id="tenant-1",
            reviewer_user_id="reviewer-1",
            decision="rejected",
            review_note="changed",
        )


@pytest.mark.parametrize(
    "case",
    (
        "missing",
        "wrong-kind",
        "cross-tenant",
        "cross-owner",
        "sha-mismatch",
        "pending_review",
        "rejected",
        "disabled",
    ),
)
def test_product_approval_fails_closed_for_invalid_background(
    case: str,
) -> None:
    background: dict[str, Any] | None
    if case == "missing":
        background = None
        background_asset_id = library.tenant_bound_asset_id(
            "tenant-1",
            "missing-background",
        )
    elif case == "wrong-kind":
        background = approved_row()
        background_asset_id = background["id"]
    elif case == "cross-tenant":
        background = background_row(tenant_id="tenant-2")
        background_asset_id = background["id"]
    elif case == "cross-owner":
        background = background_row(owner_user_id="owner-2")
        background_asset_id = background["id"]
    elif case == "sha-mismatch":
        background = background_row(original_sha256=DIGEST_B)
        background_asset_id = background["id"]
    else:
        background = background_row(status=case)
        background_asset_id = background["id"]

    pending = asset_row(
        background_asset_id=background_asset_id,
        background_sha256=DIGEST_C,
    )
    connection = ScriptedConnection(
        select_for_review=[[pending]],
        select_background_for_approval=[
            [] if background is None else [background]
        ],
    )

    with pytest.raises(
        library.ProductAssetReviewConflict,
        match="existing approved background",
    ):
        library.ProductAssetLibraryStore(connection).review_asset(
            asset_id=pending["id"],
            tenant_id="tenant-1",
            reviewer_user_id="reviewer-1",
            decision="approved",
            review_note="quality-pass",
        )

    assert [call[0] for call in connection.calls] == [
        "select_for_review",
        "select_background_for_approval",
    ]
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_product_rejection_does_not_require_an_approved_background() -> None:
    pending = asset_row()
    rejected = asset_row(
        status="rejected",
        review_status="rejected",
        reviewer_user_id="reviewer-1",
        review_note="quality-fail",
        reviewed_at="2026-07-30T00:01:00Z",
    )
    connection = ScriptedConnection(
        select_for_review=[[pending]],
        update_review=[[rejected]],
    )

    result = library.ProductAssetLibraryStore(connection).review_asset(
        asset_id=pending["id"],
        tenant_id="tenant-1",
        reviewer_user_id="reviewer-1",
        decision="rejected",
        review_note="quality-fail",
    )

    assert result.idempotent is False
    assert result.record["status"] == "rejected"
    assert [call[0] for call in connection.calls] == [
        "select_for_review",
        "update_review",
    ]
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_approved_asset_can_be_disabled_idempotently() -> None:
    disabled = approved_row(
        status="disabled",
        disabled_by_user_id="reviewer-2",
        disable_note="copyright complaint",
        disabled_at="2026-07-30T00:02:00Z",
    )
    identifier = disabled["id"]
    first_connection = ScriptedConnection(update_disable=[[disabled]])
    first = library.disable_asset(
        first_connection.cursor(),
        asset_id=identifier,
        tenant_id="tenant-1",
        disabled_by_user_id="reviewer-2",
        disable_note="copyright complaint",
    )
    assert first.idempotent is False
    assert first.record["reviewer_user_id"] == "reviewer-1"

    replay_connection = ScriptedConnection(
        update_disable=[[]],
        select_for_review=[[disabled]],
    )
    replay = library.disable_asset(
        replay_connection.cursor(),
        asset_id=identifier,
        tenant_id="tenant-1",
        disabled_by_user_id="reviewer-2",
        disable_note="copyright complaint",
    )
    assert replay.idempotent is True

    conflict_connection = ScriptedConnection(
        update_disable=[[]],
        select_for_review=[[disabled]],
    )
    with pytest.raises(library.ProductAssetReviewConflict):
        library.disable_asset(
            conflict_connection.cursor(),
            asset_id=identifier,
            tenant_id="tenant-1",
            disabled_by_user_id="reviewer-3",
            disable_note="different reason",
        )


def test_reuse_query_requires_exact_style_and_excludes_keywords() -> None:
    approved = approved_row()
    connection = ScriptedConnection(select_reusable=[[approved]])
    rows = library.find_reusable_assets(
        connection.cursor(),
        tenant_id="tenant-1",
        owner_user_id="owner-1",
        taxonomy_version="2026-07-30.v2",
        category_id="home_stir_fry",
        style_id="clean_bright",
        standard_name="Tomato Egg",
        asset_kind="product",
        pipeline_version="asset-pipeline.v2",
        background_asset_id="bg_exact_1",
        background_sha256=DIGEST_C,
        include_tenant_scope=False,
    )

    assert [row["id"] for row in rows] == [approved["id"]]
    _name, sql, parameters = connection.calls[0]
    assert "tenant_id = %s" in sql
    assert "taxonomy_version = %s" in sql
    assert "category_id = %s" in sql
    assert "style_id = %s" in sql
    assert "pipeline_version = %s" in sql
    assert "background_asset_id = %s" in sql
    assert "background_sha256 = %s" in sql
    assert "selected_background.status = 'approved'" in sql
    assert "selected_background.review_status = 'approved'" in sql
    assert "selected_background.owner_user_id = asset.owner_user_id" in sql
    assert "selected_background.original_sha256 = asset.background_sha256" in sql
    assert "ILIKE" not in sql.upper()
    where_clause = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "match_keywords_normalized" not in where_clause
    assert parameters[:5] == (
        "tenant-1",
        "2026-07-30.v2",
        "home_stir_fry",
        "clean_bright",
        "product",
    )
    assert parameters[16] is False


def test_background_catalog_query_returns_all_approved_exact_slots() -> None:
    rows = [
        background_row(
            tenant_id="waimai-shared",
            idempotency_key=f"background-{index}",
            style_id=f"style-{index}",
            taxonomy_version="2026-07-30.v2",
            category_id="light_food",
            pipeline_version="style-background.v8",
            reuse_scope="tenant",
        )
        for index in range(1, 7)
    ]
    connection = ScriptedConnection(select_background_catalog=[rows])

    result = library.list_approved_background_catalog(
        connection.cursor(),
        tenant_id="waimai-shared",
        owner_user_id="customer-1",
        taxonomy_version="2026-07-30.v2",
        category_id="light_food",
        pipeline_version="style-background.v8",
        style_ids=[f"style-{index}" for index in range(1, 7)],
        include_tenant_scope=True,
    )

    assert [row["style_id"] for row in result] == [
        f"style-{index}" for index in range(1, 7)
    ]
    name, sql, parameters = connection.calls[0]
    assert name == "select_background_catalog"
    assert "asset_kind = 'background'" in sql
    assert "status = 'approved'" in sql
    assert "review_status = 'approved'" in sql
    assert "style_id = ANY(%s::text[])" in sql
    assert parameters[:5] == (
        "waimai-shared",
        "2026-07-30.v2",
        "light_food",
        "style-background.v8",
        [f"style-{index}" for index in range(1, 7)],
    )


def test_background_catalog_query_rejects_duplicate_style_ids() -> None:
    connection = ScriptedConnection()
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="must be unique",
    ):
        library.list_approved_background_catalog(
            connection.cursor(),
            tenant_id="waimai-shared",
            owner_user_id="customer-1",
            taxonomy_version="2026-07-30.v2",
            category_id="light_food",
            pipeline_version="style-background.v8",
            style_ids=["style-1", "style-1"],
            include_tenant_scope=True,
        )
    assert connection.calls == []


def test_combo_reuse_requires_components_not_a_bare_digest() -> None:
    cursor = ScriptedConnection().cursor()
    with pytest.raises(
        library.InvalidProductAssetInput,
        match="combo_components",
    ):
        library.find_reusable_assets(
            cursor,
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="combo",
            style_id="clean_bright",
            standard_name="Family Combo",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id="bg_exact_1",
            background_sha256=DIGEST_C,
            combo_fingerprint_version=library.COMBO_FINGERPRINT_VERSION,
            combo_fingerprint_sha256="d" * 64,
        )
    assert cursor.connection.calls == []


def test_wrapper_rolls_back_when_commit_fails() -> None:
    inserted = asset_row()
    connection = ScriptedConnection(
        commit_error=RuntimeError("commit failed"),
        insert_asset=[[inserted]],
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        library.ProductAssetLibraryStore(connection).register_asset(
            **registration_kwargs()
        )
    assert connection.commits == 1
    assert connection.rollbacks == 1


def test_name_normalization_is_deterministic_without_fuzzy_matching() -> None:
    assert library.normalize_asset_name("  TOMATO-Egg  ".strip()) == "tomatoegg"
    assert library.normalize_asset_name("ＡＢＣ 123") == "abc123"
    with pytest.raises(library.InvalidProductAssetInput):
        library.normalize_asset_name("---")


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_asset_library_p1_invariants() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_product_asset_library_{uuid4().hex}"

    admin_connection = psycopg.connect(dsn, autocommit=True)
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin_connection.close()

    def connect() -> Any:
        connection = psycopg.connect(dsn, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    connection = connect()
    try:
        migration_sql = MIGRATION.read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute(migration_sql)
            cursor.execute(migration_sql)

        store = library.ProductAssetLibraryStore(connection)
        background_result = store.register_asset(
            **background_registration_kwargs()
        )
        background_id = background_result.record["id"]
        store.review_asset(
            asset_id=background_id,
            tenant_id="tenant-1",
            reviewer_user_id="reviewer-1",
            decision="approved",
            review_note="background-quality-pass",
        )
        product_kwargs = registration_kwargs(
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
        )

        barrier = threading.Barrier(2)
        race_results: list[tuple[str, Any]] = []

        def register_same_asset() -> None:
            race_connection = connect()
            try:
                barrier.wait(timeout=10)
                result = library.ProductAssetLibraryStore(
                    race_connection
                ).register_asset(**product_kwargs)
                race_results.append(("success", result.created))
            except Exception as exc:  # noqa: BLE001 - assertion inspects type
                race_results.append(("error", exc))
            finally:
                race_connection.close()

        threads = [
            threading.Thread(target=register_same_asset)
            for _index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        assert sorted(kind for kind, _value in race_results) == [
            "success",
            "success",
        ], repr(race_results)
        assert sorted(value for _kind, value in race_results) == [False, True]

        first_id = library.tenant_bound_asset_id(
            "tenant-1",
            "asset-idem-1",
        )
        store.review_asset(
            asset_id=first_id,
            tenant_id="tenant-1",
            reviewer_user_id="reviewer-1",
            decision="approved",
            review_note="quality-pass",
        )
        exact = store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="home_stir_fry",
            style_id="clean_bright",
            standard_name="Tomato Egg",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
        )
        assert [row["id"] for row in exact] == [first_id]
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="home_stir_fry",
            style_id="clean_bright",
            standard_name="Tomato Egg",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_B,
        ) == []
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="home_stir_fry",
            style_id="clean_bright",
            standard_name="Tomato Egg",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id="bg_other",
            background_sha256=DIGEST_C,
        ) == []
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="home_stir_fry",
            style_id="dark_wood",
            standard_name="Tomato Egg",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
        ) == []
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="home_stir_fry",
            style_id="clean_bright",
            standard_name="tomato",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
        ) == []

        def create_guard_background(
            label: str,
            *,
            tenant_id: str = "tenant-1",
            owner_user_id: str = "owner-1",
            final_status: str = "approved",
        ) -> dict[str, Any]:
            created = store.register_asset(
                **background_registration_kwargs(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    idempotency_key=f"guard-background-{label}",
                )
            )
            if final_status == "pending_review":
                return created.record
            decision = (
                "rejected" if final_status == "rejected" else "approved"
            )
            store.review_asset(
                asset_id=created.record["id"],
                tenant_id=tenant_id,
                reviewer_user_id="reviewer-guard",
                decision=decision,
                review_note=f"guard-{decision}",
            )
            if final_status == "disabled":
                store.disable_asset(
                    asset_id=created.record["id"],
                    tenant_id=tenant_id,
                    disabled_by_user_id="reviewer-guard",
                    disable_note="guard-disabled",
                )
            return created.record

        cross_tenant_background = create_guard_background(
            "cross-tenant",
            tenant_id="tenant-2",
        )
        cross_owner_background = create_guard_background(
            "cross-owner",
            owner_user_id="owner-2",
        )
        pending_background = create_guard_background(
            "pending",
            final_status="pending_review",
        )
        rejected_background = create_guard_background(
            "rejected",
            final_status="rejected",
        )
        disabled_background = create_guard_background(
            "disabled",
            final_status="disabled",
        )
        invalid_bindings = (
            (
                "missing",
                library.tenant_bound_asset_id(
                    "tenant-1",
                    "missing-real-background",
                ),
                DIGEST_C,
            ),
            ("wrong-kind", first_id, DIGEST_A),
            (
                "cross-tenant",
                cross_tenant_background["id"],
                DIGEST_C,
            ),
            (
                "cross-owner",
                cross_owner_background["id"],
                DIGEST_C,
            ),
            ("sha-mismatch", background_id, DIGEST_B),
            ("pending", pending_background["id"], DIGEST_C),
            ("rejected", rejected_background["id"], DIGEST_C),
            ("disabled", disabled_background["id"], DIGEST_C),
        )
        for label, bound_background_id, bound_background_sha256 in (
            invalid_bindings
        ):
            product = store.register_asset(
                **registration_kwargs(
                    idempotency_key=f"guard-product-{label}",
                    background_asset_id=bound_background_id,
                    background_sha256=bound_background_sha256,
                    standard_name=f"Guard Product {label}",
                    aliases=[],
                    match_keywords=[],
                )
            )
            with pytest.raises(
                library.ProductAssetReviewConflict,
                match="existing approved background",
            ):
                store.review_asset(
                    asset_id=product.record["id"],
                    tenant_id="tenant-1",
                    reviewer_user_id="reviewer-guard",
                    decision="approved",
                    review_note="must-fail-closed",
                )
            unchanged = store.get_asset(asset_id=product.record["id"])
            assert unchanged["status"] == "pending_review"
            assert unchanged["review_status"] == "pending"

        concurrent_background = create_guard_background("concurrent")
        concurrent_product = store.register_asset(
            **registration_kwargs(
                idempotency_key="guard-product-concurrent",
                background_asset_id=concurrent_background["id"],
                background_sha256=DIGEST_C,
                standard_name="Guard Product Concurrent",
                aliases=[],
                match_keywords=[],
            )
        )
        concurrency_barrier = threading.Barrier(2)
        approval_results: list[Any] = []

        def approve_while_background_disable_is_uncommitted() -> None:
            approval_connection = connect()
            try:
                concurrency_barrier.wait(timeout=10)
                library.ProductAssetLibraryStore(
                    approval_connection
                ).review_asset(
                    asset_id=concurrent_product.record["id"],
                    tenant_id="tenant-1",
                    reviewer_user_id="reviewer-concurrent",
                    decision="approved",
                    review_note="must-observe-disable",
                )
                approval_results.append("approved")
            except Exception as exc:  # noqa: BLE001 - assertion inspects type
                approval_results.append(exc)
            finally:
                approval_connection.close()

        with connection.cursor() as cursor:
            library.disable_asset(
                cursor,
                asset_id=concurrent_background["id"],
                tenant_id="tenant-1",
                disabled_by_user_id="reviewer-concurrent",
                disable_note="concurrent-disable",
            )
        approval_thread = threading.Thread(
            target=approve_while_background_disable_is_uncommitted
        )
        approval_thread.start()
        concurrency_barrier.wait(timeout=10)
        time.sleep(0.1)
        connection.commit()
        approval_thread.join(timeout=20)
        assert not approval_thread.is_alive()
        assert len(approval_results) == 1
        assert isinstance(
            approval_results[0],
            library.ProductAssetReviewConflict,
        )
        unchanged = store.get_asset(asset_id=concurrent_product.record["id"])
        assert unchanged["status"] == "pending_review"
        assert unchanged["review_status"] == "pending"

        store.disable_asset(
            asset_id=first_id,
            tenant_id="tenant-1",
            disabled_by_user_id="reviewer-2",
            disable_note="copyright complaint",
        )
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="home_stir_fry",
            style_id="clean_bright",
            standard_name="Tomato Egg",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
        ) == []
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT from_status, to_status, actor_user_id, event_note
                FROM product_asset_library_status_events
                WHERE asset_id = %s
                ORDER BY event_id
                """,
                (first_id,),
            )
            assert cursor.fetchall() == [
                (
                    "pending_review",
                    "approved",
                    "reviewer-1",
                    "quality-pass",
                ),
                (
                    "approved",
                    "disabled",
                    "reviewer-2",
                    "copyright complaint",
                ),
            ]
        connection.commit()

        with pytest.raises(
            library.InvalidProductAssetInput,
            match="ai-assets/tenant-2/",
        ):
            store.register_asset(
                **registration_kwargs(
                    tenant_id="tenant-2",
                    idempotency_key="tenant-2-invalid-ref",
                    original_object_ref=(
                        registration_kwargs()["original_object_ref"]
                    ),
                )
            )
        assert library.tenant_bound_asset_id(
            "tenant-1",
            "same-idempotency-key",
        ) != library.tenant_bound_asset_id(
            "tenant-2",
            "same-idempotency-key",
        )

        second_kwargs = registration_kwargs(
            idempotency_key="asset-idem-2",
            standard_name="Second Dish",
            aliases=[],
            match_keywords=[],
            original_sha256=DIGEST_C,
            derivative_object_ref=(
                registration_kwargs()["original_object_ref"]
            ),
            derivative_sha256=DIGEST_A,
            derivative_size_bytes=1234,
        )
        with pytest.raises(library.ProductAssetConflict, match="object_ref"):
            store.register_asset(**second_kwargs)
        second_id = library.tenant_bound_asset_id(
            "tenant-1",
            "asset-idem-2",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM product_asset_library_object_refs
                WHERE asset_id = %s
                """,
                (second_id,),
            )
            assert cursor.fetchone()[0] == 0
        connection.commit()

        combo_kwargs = registration_kwargs(
            idempotency_key="asset-combo-idem-1",
            category_id="combo",
            category_name="Combo",
            standard_name="鱼香肉丝套餐",
            aliases=[],
            match_keywords=["套餐"],
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
            combo_fingerprint_version=library.COMBO_FINGERPRINT_VERSION,
            combo_components=combo_components(),
            original_sha256="d" * 64,
            derivative_object_ref=None,
            derivative_sha256=None,
            derivative_size_bytes=None,
        )
        combo_result = store.register_asset(**combo_kwargs)
        store.review_asset(
            asset_id=combo_result.record["id"],
            tenant_id="tenant-1",
            reviewer_user_id="reviewer-1",
            decision="approved",
        )
        combo_match = store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="combo",
            style_id="clean_bright",
            standard_name="鱼香肉丝套餐",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
            combo_fingerprint_version=library.COMBO_FINGERPRINT_VERSION,
            combo_components=list(reversed(combo_components())),
        )
        assert [row["id"] for row in combo_match] == [
            combo_result.record["id"]
        ]
        missing_drink = [
            component
            for component in combo_components()
            if component["role"] != "drink"
        ]
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="combo",
            style_id="clean_bright",
            standard_name="鱼香肉丝套餐",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
            combo_fingerprint_version=library.COMBO_FINGERPRINT_VERSION,
            combo_components=missing_drink,
        ) == []
        store.disable_asset(
            asset_id=background_id,
            tenant_id="tenant-1",
            disabled_by_user_id="reviewer-2",
            disable_note="background withdrawn",
        )
        assert store.find_reusable_assets(
            tenant_id="tenant-1",
            owner_user_id="owner-1",
            taxonomy_version="2026-07-30.v2",
            category_id="combo",
            style_id="clean_bright",
            standard_name="鱼香肉丝套餐",
            asset_kind="product",
            pipeline_version="asset-pipeline.v2",
            background_asset_id=background_id,
            background_sha256=DIGEST_C,
            combo_fingerprint_version=library.COMBO_FINGERPRINT_VERSION,
            combo_components=combo_components(),
        ) == []

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT object_ref, object_role
                FROM product_asset_library_object_refs
                ORDER BY object_ref
                """
            )
            object_rows = cursor.fetchall()
            assert len({row[0] for row in object_rows}) == len(object_rows)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM product_asset_library_entries
                WHERE style_id = 'clean_bright'
                """
            )
            assert cursor.fetchone()[0] == 18
        connection.commit()
    finally:
        connection.rollback()
        connection.close()
        cleanup_connection = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup_connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cleanup_connection.close()
