from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CATEGORY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STYLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
OBJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
TENANT_ASSET_ID_RE = re.compile(r"^asset-[0-9a-f]{40}$")

ASSET_ID_VERSION = "tenant-idempotency.v1"
COMBO_FINGERPRINT_VERSION = "combo-components.v1"
ASSET_KINDS = frozenset({"product", "background"})
REUSE_SCOPES = frozenset({"owner", "tenant"})
SOURCE_KINDS = frozenset({"generated", "uploaded", "imported"})
REVIEW_DECISIONS = frozenset({"approved", "rejected"})
ASSET_STATUSES = frozenset(
    {"pending_review", "approved", "rejected", "disabled"}
)
COMBO_COMPONENT_ROLES = frozenset(
    {"main", "staple", "side", "drink", "dessert", "condiment", "other"}
)
COMBO_ROLE_ORDER = {
    role: index
    for index, role in enumerate(
        ("main", "staple", "side", "drink", "dessert", "condiment", "other")
    )
}

MAX_ALIAS_COUNT = 64
MAX_KEYWORD_COUNT = 64
MAX_COMBO_COMPONENT_COUNT = 32
MAX_COMBO_COMPONENT_QUANTITY = 99
MAX_NAME_LENGTH = 255
MAX_LIST_ITEM_LENGTH = 255
MAX_REVIEW_NOTE_LENGTH = 2000


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    autocommit: bool

    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductAssetLibraryError(RuntimeError):
    pass


class InvalidProductAssetInput(ProductAssetLibraryError):
    pass


class ProductAssetConflict(ProductAssetLibraryError):
    pass


class ProductAssetNotFound(ProductAssetLibraryError):
    pass


class ProductAssetReviewConflict(ProductAssetLibraryError):
    pass


@dataclass(frozen=True)
class AssetCreateResult:
    record: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class AssetReviewResult:
    record: dict[str, Any]
    idempotent: bool


_ASSET_COLUMNS = """
    id, tenant_id, owner_user_id, idempotency_key, asset_kind,
    taxonomy_version, category_id, category_name, style_id,
    background_asset_id, background_sha256, standard_name,
    normalized_standard_name, aliases, aliases_normalized,
    match_keywords, match_keywords_normalized, combo_fingerprint_version,
    combo_components, combo_fingerprint_sha256, reuse_scope, source_kind,
    source_provider, prompt_version, model_name, model_version,
    pipeline_version, original_object_ref, original_sha256,
    original_size_bytes, derivative_object_ref, derivative_sha256,
    derivative_size_bytes, status, review_status, reviewer_user_id,
    review_note, reviewed_at, disabled_by_user_id, disable_note, disabled_at,
    content_sha256, created_at, updated_at
"""

_INSERT_ASSET_SQL = f"""
/* product_asset_library_store:insert_asset */
INSERT INTO product_asset_library_entries (
    id, tenant_id, owner_user_id, idempotency_key, asset_kind,
    taxonomy_version, category_id, category_name, style_id,
    background_asset_id, background_sha256, standard_name,
    normalized_standard_name, aliases, aliases_normalized,
    match_keywords, match_keywords_normalized, combo_fingerprint_version,
    combo_components, combo_fingerprint_sha256, reuse_scope, source_kind,
    source_provider, prompt_version, model_name, model_version,
    pipeline_version, original_object_ref, original_sha256,
    original_size_bytes, derivative_object_ref, derivative_sha256,
    derivative_size_bytes, content_sha256
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
    %s, %s::jsonb, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s
)
ON CONFLICT DO NOTHING
RETURNING {_ASSET_COLUMNS}
"""

_SELECT_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_asset_library_store:select_by_idempotency_for_update */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE tenant_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_BY_ID_FOR_UPDATE_SQL = f"""
/* product_asset_library_store:select_by_id_for_update */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE id = %s
FOR UPDATE
"""

_SELECT_BY_OBJECT_REF_FOR_UPDATE_SQL = f"""
/* product_asset_library_store:select_by_object_ref_for_update */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE original_object_ref = %s OR derivative_object_ref = %s
FOR UPDATE
"""

_SELECT_OWNED_SQL = f"""
/* product_asset_library_store:select_owned */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE id = %s
  AND tenant_id = %s
  AND owner_user_id = %s
"""

_SELECT_BY_ID_SQL = f"""
/* product_asset_library_store:select_by_id */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE id = %s
"""

_LIST_FOR_ADMIN_SQL = f"""
/* product_asset_library_store:list_for_admin */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE (%s = '' OR status = %s)
ORDER BY created_at DESC, id DESC
LIMIT %s OFFSET %s
"""

_SUMMARIZE_FOR_ADMIN_SQL = """
/* product_asset_library_store:summarize_for_admin */
SELECT status, asset_kind, category_name, COUNT(*) AS asset_count
FROM product_asset_library_entries
GROUP BY status, asset_kind, category_name
ORDER BY status, asset_kind, category_name
"""

_SELECT_FOR_REVIEW_SQL = f"""
/* product_asset_library_store:select_for_review */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE id = %s AND tenant_id = %s
FOR UPDATE
"""

_SELECT_BACKGROUND_FOR_APPROVAL_SQL = f"""
/* product_asset_library_store:select_background_for_approval */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries
WHERE id = %s
FOR UPDATE
"""

_UPDATE_REVIEW_SQL = f"""
/* product_asset_library_store:update_review */
UPDATE product_asset_library_entries
SET status = %s,
    review_status = %s,
    reviewer_user_id = %s,
    review_note = %s,
    reviewed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND tenant_id = %s
  AND status = 'pending_review'
  AND review_status = 'pending'
RETURNING {_ASSET_COLUMNS}
"""

_UPDATE_DISABLE_SQL = f"""
/* product_asset_library_store:update_disable */
UPDATE product_asset_library_entries
SET status = 'disabled',
    disabled_by_user_id = %s,
    disable_note = %s,
    disabled_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND tenant_id = %s
  AND status = 'approved'
  AND review_status = 'approved'
RETURNING {_ASSET_COLUMNS}
"""

_SELECT_REUSABLE_SQL = f"""
/* product_asset_library_store:select_reusable */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries AS asset
WHERE tenant_id = %s
  AND taxonomy_version = %s
  AND category_id = %s
  AND style_id = %s
  AND asset_kind = %s
  AND pipeline_version = %s
  AND (
        %s <> 'product'
        OR (
            background_asset_id = %s
            AND background_sha256 = %s
        )
  )
  AND status = 'approved'
  AND review_status = 'approved'
  AND (
        asset_kind <> 'product'
        OR EXISTS (
            SELECT 1
            FROM product_asset_library_entries AS selected_background
            WHERE selected_background.id = asset.background_asset_id
              AND selected_background.asset_kind = 'background'
              AND selected_background.tenant_id = asset.tenant_id
              AND selected_background.owner_user_id = asset.owner_user_id
              AND selected_background.original_sha256 = asset.background_sha256
              AND selected_background.status = 'approved'
              AND selected_background.review_status = 'approved'
        )
  )
  AND (
        normalized_standard_name = %s
        OR (
            %s <> 'unknown'
            AND aliases_normalized @> jsonb_build_array(%s::text)
        )
  )
  AND (
        %s <> 'product'
        OR %s <> 'combo'
        OR combo_fingerprint_sha256 = %s
  )
  AND (
        (reuse_scope = 'owner' AND owner_user_id = %s)
        OR (%s AND reuse_scope = 'tenant')
  )
ORDER BY
    CASE
        WHEN reuse_scope = 'owner' AND owner_user_id = %s THEN 0
        ELSE 1
    END,
    created_at DESC,
    id DESC
LIMIT %s
"""

_SELECT_BACKGROUND_CATALOG_SQL = f"""
/* product_asset_library_store:select_background_catalog */
SELECT {_ASSET_COLUMNS}
FROM product_asset_library_entries AS asset
WHERE tenant_id = %s
  AND taxonomy_version = %s
  AND category_id = %s
  AND asset_kind = 'background'
  AND pipeline_version = %s
  AND style_id = ANY(%s::text[])
  AND status = 'approved'
  AND review_status = 'approved'
  AND (
        (reuse_scope = 'owner' AND owner_user_id = %s)
        OR (%s AND reuse_scope = 'tenant')
  )
ORDER BY
    style_id ASC,
    CASE
        WHEN reuse_scope = 'owner' AND owner_user_id = %s THEN 0
        ELSE 1
    END,
    created_at DESC,
    id DESC
"""


def tenant_bound_asset_id(tenant_id: str, idempotency_key: str) -> str:
    """Derive the only accepted asset id from server-owned tenant context."""

    tenant = _tenant_id(tenant_id)
    key = _identifier(idempotency_key, "idempotency_key")
    digest = hashlib.sha256(
        _canonical_json(
            {
                "version": ASSET_ID_VERSION,
                "tenant_id": tenant,
                "idempotency_key": key,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"asset-{digest[:40]}"


def canonicalize_combo_components(
    components: Sequence[Mapping[str, Any]],
    *,
    version: str = COMBO_FINGERPRINT_VERSION,
) -> list[dict[str, Any]]:
    """Normalize a complete server-owned combo component set."""

    if version != COMBO_FINGERPRINT_VERSION:
        raise InvalidProductAssetInput(
            f"combo_fingerprint_version must be {COMBO_FINGERPRINT_VERSION}"
        )
    if (
        isinstance(components, (str, bytes))
        or not isinstance(components, Sequence)
    ):
        raise InvalidProductAssetInput(
            "combo_components must be a sequence of component mappings"
        )
    if not 2 <= len(components) <= MAX_COMBO_COMPONENT_COUNT:
        raise InvalidProductAssetInput(
            "combo_components must contain between 2 and 32 components"
        )

    canonical: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    for component in components:
        if not isinstance(component, Mapping):
            raise InvalidProductAssetInput(
                "each combo component must be a mapping"
            )
        allowed_fields = {"name", "role", "quantity", "specification"}
        unknown_fields = set(component) - allowed_fields
        if unknown_fields:
            raise InvalidProductAssetInput(
                "combo component contains unsupported fields"
            )
        name = _required_text(
            component.get("name"),
            "combo_component.name",
            max_length=MAX_NAME_LENGTH,
        )
        role = _choice(
            component.get("role"),
            COMBO_COMPONENT_ROLES,
            "combo_component.role",
        )
        quantity = component.get("quantity", 1)
        clean_quantity = _bounded_positive_int(
            quantity,
            "combo_component.quantity",
            maximum=MAX_COMBO_COMPONENT_QUANTITY,
        )
        specification = _optional_text(
            component.get("specification", ""),
            "combo_component.specification",
            max_length=MAX_NAME_LENGTH,
        )
        normalized_name = normalize_asset_name(name)
        normalized_specification = _normalize_optional_component_text(
            specification
        )
        identity = (role, normalized_name, normalized_specification)
        if identity in identities:
            raise InvalidProductAssetInput(
                "duplicate combo components must use quantity instead"
            )
        identities.add(identity)
        canonical.append(
            {
                "role": role,
                "name": name,
                "normalized_name": normalized_name,
                "quantity": clean_quantity,
                "specification": specification,
                "normalized_specification": normalized_specification,
            }
        )

    if not any(component["role"] == "main" for component in canonical):
        raise InvalidProductAssetInput(
            "combo_components must include at least one main component"
        )

    canonical.sort(
        key=lambda component: (
            COMBO_ROLE_ORDER[component["role"]],
            component["normalized_name"],
            component["normalized_specification"],
            component["quantity"],
        )
    )
    return canonical


def combo_components_fingerprint(
    components: Sequence[Mapping[str, Any]],
    *,
    version: str = COMBO_FINGERPRINT_VERSION,
) -> tuple[list[dict[str, Any]], str]:
    canonical = canonicalize_combo_components(components, version=version)
    fingerprint_components = [
        {
            "role": component["role"],
            "normalized_name": component["normalized_name"],
            "quantity": component["quantity"],
            "normalized_specification": (
                component["normalized_specification"]
            ),
        }
        for component in canonical
    ]
    digest = hashlib.sha256(
        _canonical_json(
            {"version": version, "components": fingerprint_components}
        ).encode("utf-8")
    ).hexdigest()
    return canonical, digest


def register_asset(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    asset_kind: str,
    taxonomy_version: str,
    category_id: str,
    category_name: str,
    style_id: str,
    background_asset_id: str = "",
    background_sha256: str = "",
    standard_name: str,
    aliases: Sequence[str] | None,
    match_keywords: Sequence[str] | None,
    reuse_scope: str,
    source_kind: str,
    source_provider: str,
    prompt_version: str,
    model_name: str,
    model_version: str,
    pipeline_version: str,
    original_object_ref: str,
    original_sha256: str,
    original_size_bytes: int,
    derivative_object_ref: str | None = None,
    derivative_sha256: str | None = None,
    derivative_size_bytes: int | None = None,
    combo_fingerprint_version: str = "",
    combo_components: Sequence[Mapping[str, Any]] | None = None,
    combo_fingerprint_sha256: str = "",
    asset_id: str | None = None,
) -> AssetCreateResult:
    """Insert an immutable asset registration in a caller-owned transaction."""

    normalized = _normalize_registration(
        asset_id=asset_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        asset_kind=asset_kind,
        taxonomy_version=taxonomy_version,
        category_id=category_id,
        category_name=category_name,
        style_id=style_id,
        background_asset_id=background_asset_id,
        background_sha256=background_sha256,
        standard_name=standard_name,
        aliases=aliases,
        match_keywords=match_keywords,
        reuse_scope=reuse_scope,
        source_kind=source_kind,
        source_provider=source_provider,
        prompt_version=prompt_version,
        model_name=model_name,
        model_version=model_version,
        pipeline_version=pipeline_version,
        original_object_ref=original_object_ref,
        original_sha256=original_sha256,
        original_size_bytes=original_size_bytes,
        derivative_object_ref=derivative_object_ref,
        derivative_sha256=derivative_sha256,
        derivative_size_bytes=derivative_size_bytes,
        combo_fingerprint_version=combo_fingerprint_version,
        combo_components=combo_components,
        combo_fingerprint_sha256=combo_fingerprint_sha256,
    )
    parameters = (
        normalized["id"],
        normalized["tenant_id"],
        normalized["owner_user_id"],
        normalized["idempotency_key"],
        normalized["asset_kind"],
        normalized["taxonomy_version"],
        normalized["category_id"],
        normalized["category_name"],
        normalized["style_id"],
        normalized["background_asset_id"],
        normalized["background_sha256"],
        normalized["standard_name"],
        normalized["normalized_standard_name"],
        _canonical_json(normalized["aliases"]),
        _canonical_json(normalized["aliases_normalized"]),
        _canonical_json(normalized["match_keywords"]),
        _canonical_json(normalized["match_keywords_normalized"]),
        normalized["combo_fingerprint_version"],
        _canonical_json(normalized["combo_components"]),
        normalized["combo_fingerprint_sha256"],
        normalized["reuse_scope"],
        normalized["source_kind"],
        normalized["source_provider"],
        normalized["prompt_version"],
        normalized["model_name"],
        normalized["model_version"],
        normalized["pipeline_version"],
        normalized["original_object_ref"],
        normalized["original_sha256"],
        normalized["original_size_bytes"],
        normalized["derivative_object_ref"],
        normalized["derivative_sha256"],
        normalized["derivative_size_bytes"],
        normalized["content_sha256"],
    )

    try:
        cursor.execute(_INSERT_ASSET_SQL, parameters)
    except Exception as exc:
        if _is_object_ref_conflict(exc):
            raise ProductAssetConflict(
                "object_ref is already registered by another asset or role"
            ) from exc
        raise
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return AssetCreateResult(record=_decode_record(inserted), created=True)

    cursor.execute(
        _SELECT_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (normalized["tenant_id"], normalized["idempotency_key"]),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        decoded = _decode_record(existing)
        _require_exact_replay(decoded, normalized)
        return AssetCreateResult(record=decoded, created=False)

    cursor.execute(_SELECT_BY_ID_FOR_UPDATE_SQL, (normalized["id"],))
    existing = _fetchone_dict(cursor)
    if existing is not None:
        raise ProductAssetConflict(
            f"tenant-bound asset id already exists: {normalized['id']}"
        )

    refs = [normalized["original_object_ref"]]
    if normalized["derivative_object_ref"] is not None:
        refs.append(normalized["derivative_object_ref"])
    for object_ref in refs:
        cursor.execute(
            _SELECT_BY_OBJECT_REF_FOR_UPDATE_SQL,
            (object_ref, object_ref),
        )
        if _fetchone_dict(cursor) is not None:
            raise ProductAssetConflict(
                "object_ref is already registered by another asset or role"
            )

    raise ProductAssetConflict(
        "asset registration conflicted without a resolvable existing row"
    )


def get_owned_asset(
    cursor: CursorLike,
    *,
    asset_id: str,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    identifier = _tenant_asset_id(asset_id)
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_OWNED_SQL, (identifier, tenant, owner))
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductAssetNotFound(identifier)
    return _decode_record(row)


def get_asset(
    cursor: CursorLike,
    *,
    asset_id: str,
) -> dict[str, Any]:
    identifier = _tenant_asset_id(asset_id)
    cursor.execute(_SELECT_BY_ID_SQL, (identifier,))
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductAssetNotFound(identifier)
    return _decode_record(row)


def list_assets_for_admin(
    cursor: CursorLike,
    *,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clean_status = str(status or "").strip().lower()
    if clean_status and clean_status not in ASSET_STATUSES:
        raise InvalidProductAssetInput("status has an invalid value")
    clean_limit = _bounded_positive_int(limit, "limit", maximum=100)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidProductAssetInput(
            "offset must be a non-negative integer"
        )
    if offset > 1_000_000:
        raise InvalidProductAssetInput("offset is too large")
    cursor.execute(
        _LIST_FOR_ADMIN_SQL,
        (
            clean_status,
            clean_status,
            clean_limit,
            offset,
        ),
    )
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


def summarize_assets_for_admin(
    cursor: CursorLike,
) -> list[dict[str, Any]]:
    cursor.execute(_SUMMARIZE_FOR_ADMIN_SQL)
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


def review_asset(
    cursor: CursorLike,
    *,
    asset_id: str,
    tenant_id: str,
    reviewer_user_id: str,
    decision: str,
    review_note: str = "",
) -> AssetReviewResult:
    identifier = _tenant_asset_id(asset_id)
    tenant = _tenant_id(tenant_id)
    reviewer = _identifier(reviewer_user_id, "reviewer_user_id")
    clean_decision = _choice(decision, REVIEW_DECISIONS, "decision")
    note = _optional_text(
        review_note,
        "review_note",
        max_length=MAX_REVIEW_NOTE_LENGTH,
    )

    cursor.execute(_SELECT_FOR_REVIEW_SQL, (identifier, tenant))
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductAssetNotFound(identifier)
    decoded = _decode_record(existing)

    if (
        str(decoded.get("status") or "") != "pending_review"
        or str(decoded.get("review_status") or "") != "pending"
    ):
        if (
            str(decoded.get("status") or "") == clean_decision
            and str(decoded.get("review_status") or "") == clean_decision
            and _constant_text_equal(
                decoded.get("reviewer_user_id"),
                reviewer,
            )
            and _constant_text_equal(decoded.get("review_note"), note)
        ):
            return AssetReviewResult(record=decoded, idempotent=True)
        raise ProductAssetReviewConflict(
            f"asset review is already finalized: {identifier}"
        )

    if (
        clean_decision == "approved"
        and str(decoded.get("asset_kind") or "") == "product"
    ):
        _require_approved_product_background(cursor, decoded)

    cursor.execute(
        _UPDATE_REVIEW_SQL,
        (
            clean_decision,
            clean_decision,
            reviewer,
            note,
            identifier,
            tenant,
        ),
    )
    updated = _fetchone_dict(cursor)
    if updated is not None:
        return AssetReviewResult(
            record=_decode_record(updated),
            idempotent=False,
        )
    raise ProductAssetReviewConflict(
        f"asset review state changed unexpectedly: {identifier}"
    )


def disable_asset(
    cursor: CursorLike,
    *,
    asset_id: str,
    tenant_id: str,
    disabled_by_user_id: str,
    disable_note: str,
) -> AssetReviewResult:
    """Disable an approved asset while preserving approval audit fields."""

    identifier = _tenant_asset_id(asset_id)
    tenant = _tenant_id(tenant_id)
    actor = _identifier(disabled_by_user_id, "disabled_by_user_id")
    note = _required_text(
        disable_note,
        "disable_note",
        max_length=MAX_REVIEW_NOTE_LENGTH,
    )
    cursor.execute(
        _UPDATE_DISABLE_SQL,
        (actor, note, identifier, tenant),
    )
    updated = _fetchone_dict(cursor)
    if updated is not None:
        return AssetReviewResult(
            record=_decode_record(updated),
            idempotent=False,
        )

    cursor.execute(_SELECT_FOR_REVIEW_SQL, (identifier, tenant))
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductAssetNotFound(identifier)
    decoded = _decode_record(existing)
    if (
        str(decoded.get("status") or "") == "disabled"
        and str(decoded.get("review_status") or "") == "approved"
        and _constant_text_equal(decoded.get("disabled_by_user_id"), actor)
        and _constant_text_equal(decoded.get("disable_note"), note)
    ):
        return AssetReviewResult(record=decoded, idempotent=True)
    raise ProductAssetReviewConflict(
        f"asset cannot be disabled from its current state: {identifier}"
    )


def find_reusable_assets(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    taxonomy_version: str,
    category_id: str,
    style_id: str,
    background_asset_id: str = "",
    background_sha256: str = "",
    standard_name: str,
    asset_kind: str,
    pipeline_version: str,
    include_tenant_scope: bool = False,
    combo_fingerprint_version: str = "",
    combo_components: Sequence[Mapping[str, Any]] | None = None,
    combo_fingerprint_sha256: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return exact approved matches; keywords are metadata, never match input."""

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    taxonomy = _version(taxonomy_version, "taxonomy_version")
    category = _category_id(category_id)
    style = _style_id(style_id)
    kind = _choice(asset_kind, ASSET_KINDS, "asset_kind")
    pipeline = _version(pipeline_version, "pipeline_version")
    (
        selected_background_asset_id,
        selected_background_sha256,
    ) = _normalize_background_binding(
        asset_kind=kind,
        background_asset_id=background_asset_id,
        background_sha256=background_sha256,
    )
    normalized_name = normalize_asset_name(
        _required_text(
            standard_name,
            "standard_name",
            max_length=MAX_NAME_LENGTH,
        )
    )
    if not isinstance(include_tenant_scope, bool):
        raise InvalidProductAssetInput(
            "include_tenant_scope must be a boolean"
        )
    clean_limit = _bounded_positive_int(limit, "limit", maximum=100)
    (
        _clean_combo_version,
        _canonical_components,
        fingerprint,
    ) = _normalize_combo_contract(
        asset_kind=kind,
        category_id=category,
        combo_fingerprint_version=combo_fingerprint_version,
        combo_components=combo_components,
        combo_fingerprint_sha256=combo_fingerprint_sha256,
    )

    cursor.execute(
        _SELECT_REUSABLE_SQL,
        (
            tenant,
            taxonomy,
            category,
            style,
            kind,
            pipeline,
            kind,
            selected_background_asset_id,
            selected_background_sha256,
            normalized_name,
            category,
            normalized_name,
            kind,
            category,
            fingerprint,
            owner,
            include_tenant_scope,
            owner,
            clean_limit,
        ),
    )
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


def list_approved_background_catalog(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    taxonomy_version: str,
    category_id: str,
    pipeline_version: str,
    style_ids: Sequence[str],
    include_tenant_scope: bool = False,
) -> list[dict[str, Any]]:
    """Return all approved records for an exact category catalog."""

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    taxonomy = _version(taxonomy_version, "taxonomy_version")
    category = _category_id(category_id)
    pipeline = _version(pipeline_version, "pipeline_version")
    if isinstance(style_ids, (str, bytes)):
        raise InvalidProductAssetInput("style_ids must be a sequence")
    styles = tuple(_style_id(style_id) for style_id in style_ids)
    if not styles or len(styles) > 100:
        raise InvalidProductAssetInput("style_ids count is invalid")
    if len(set(styles)) != len(styles):
        raise InvalidProductAssetInput("style_ids must be unique")
    if not isinstance(include_tenant_scope, bool):
        raise InvalidProductAssetInput(
            "include_tenant_scope must be a boolean"
        )

    cursor.execute(
        _SELECT_BACKGROUND_CATALOG_SQL,
        (
            tenant,
            taxonomy,
            category,
            pipeline,
            list(styles),
            owner,
            include_tenant_scope,
            owner,
        ),
    )
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


class ProductAssetLibraryStore:
    """Short-transaction wrapper around the external-cursor asset APIs."""

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductAssetInput(
                "ProductAssetLibraryStore requires an "
                "autocommit-disabled connection"
            )
        self.connection = connection

    def register_asset(self, **kwargs: Any) -> AssetCreateResult:
        with self._transaction() as cursor:
            return register_asset(cursor, **kwargs)

    def get_owned_asset(self, **kwargs: Any) -> dict[str, Any]:
        with self._transaction() as cursor:
            return get_owned_asset(cursor, **kwargs)

    def get_asset(self, **kwargs: Any) -> dict[str, Any]:
        with self._transaction() as cursor:
            return get_asset(cursor, **kwargs)

    def list_assets_for_admin(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_assets_for_admin(cursor, **kwargs)

    def summarize_assets_for_admin(self) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return summarize_assets_for_admin(cursor)

    def review_asset(self, **kwargs: Any) -> AssetReviewResult:
        with self._transaction() as cursor:
            return review_asset(cursor, **kwargs)

    def disable_asset(self, **kwargs: Any) -> AssetReviewResult:
        with self._transaction() as cursor:
            return disable_asset(cursor, **kwargs)

    def find_reusable_assets(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return find_reusable_assets(cursor, **kwargs)

    def list_approved_background_catalog(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_approved_background_catalog(cursor, **kwargs)

    @contextmanager
    def _transaction(self) -> Iterator[CursorLike]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        except Exception:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def normalize_asset_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    normalized = "".join(character for character in text if character.isalnum())
    if not normalized:
        raise InvalidProductAssetInput(
            "asset name must contain letters or numbers"
        )
    if len(normalized) > MAX_NAME_LENGTH:
        raise InvalidProductAssetInput("normalized asset name is too long")
    return normalized


def _normalize_registration(**values: Any) -> dict[str, Any]:
    tenant_id = _tenant_id(values["tenant_id"])
    owner_user_id = _identifier(values["owner_user_id"], "owner_user_id")
    idempotency_key = _identifier(
        values["idempotency_key"],
        "idempotency_key",
    )
    asset_id = tenant_bound_asset_id(tenant_id, idempotency_key)
    supplied_asset_id = values.get("asset_id")
    if supplied_asset_id not in (None, ""):
        clean_supplied_id = _tenant_asset_id(supplied_asset_id)
        if not _constant_text_equal(clean_supplied_id, asset_id):
            raise InvalidProductAssetInput(
                "asset_id must equal the server-derived tenant-bound id"
            )

    asset_kind = _choice(values["asset_kind"], ASSET_KINDS, "asset_kind")
    taxonomy_version = _version(
        values["taxonomy_version"],
        "taxonomy_version",
    )
    category_id = _category_id(values["category_id"])
    category_name = _required_text(
        values["category_name"],
        "category_name",
        max_length=MAX_NAME_LENGTH,
    )
    style_id = _style_id(values["style_id"])
    (
        background_asset_id,
        background_sha256,
    ) = _normalize_background_binding(
        asset_kind=asset_kind,
        background_asset_id=values.get("background_asset_id", ""),
        background_sha256=values.get("background_sha256", ""),
    )
    standard_name = _required_text(
        values["standard_name"],
        "standard_name",
        max_length=MAX_NAME_LENGTH,
    )
    normalized_standard_name = normalize_asset_name(standard_name)
    aliases, aliases_normalized = _normalized_string_list(
        values.get("aliases"),
        "aliases",
        maximum=MAX_ALIAS_COUNT,
    )
    match_keywords, match_keywords_normalized = _normalized_string_list(
        values.get("match_keywords"),
        "match_keywords",
        maximum=MAX_KEYWORD_COUNT,
    )
    reuse_scope = _choice(
        values["reuse_scope"],
        REUSE_SCOPES,
        "reuse_scope",
    )
    source_kind = _choice(
        values["source_kind"],
        SOURCE_KINDS,
        "source_kind",
    )
    source_provider = _required_text(
        values["source_provider"],
        "source_provider",
        max_length=128,
    )
    prompt_version = _version(values["prompt_version"], "prompt_version")
    model_name = _required_text(
        values["model_name"],
        "model_name",
        max_length=128,
    )
    model_version = _version(values["model_version"], "model_version")
    pipeline_version = _version(
        values["pipeline_version"],
        "pipeline_version",
    )
    original_object_ref = _private_object_ref(
        values["original_object_ref"],
        "original_object_ref",
        tenant_id=tenant_id,
    )
    original_sha256 = _sha256(
        values["original_sha256"],
        "original_sha256",
    )
    original_size_bytes = _bounded_positive_int(
        values["original_size_bytes"],
        "original_size_bytes",
        maximum=(2**63) - 1,
    )
    (
        derivative_object_ref,
        derivative_sha256,
        derivative_size_bytes,
    ) = _derivative_object(
        values.get("derivative_object_ref"),
        values.get("derivative_sha256"),
        values.get("derivative_size_bytes"),
        tenant_id=tenant_id,
    )
    if derivative_object_ref == original_object_ref:
        raise InvalidProductAssetInput(
            "derivative_object_ref must differ from original_object_ref"
        )
    (
        combo_fingerprint_version,
        combo_components,
        combo_fingerprint,
    ) = _normalize_combo_contract(
        asset_kind=asset_kind,
        category_id=category_id,
        combo_fingerprint_version=values.get(
            "combo_fingerprint_version",
            "",
        ),
        combo_components=values.get("combo_components"),
        combo_fingerprint_sha256=values.get(
            "combo_fingerprint_sha256",
            "",
        ),
    )

    content = {
        "id": asset_id,
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
        "idempotency_key": idempotency_key,
        "asset_kind": asset_kind,
        "taxonomy_version": taxonomy_version,
        "category_id": category_id,
        "category_name": category_name,
        "style_id": style_id,
        "background_asset_id": background_asset_id,
        "background_sha256": background_sha256,
        "standard_name": standard_name,
        "normalized_standard_name": normalized_standard_name,
        "aliases": aliases,
        "aliases_normalized": aliases_normalized,
        "match_keywords": match_keywords,
        "match_keywords_normalized": match_keywords_normalized,
        "combo_fingerprint_version": combo_fingerprint_version,
        "combo_components": combo_components,
        "combo_fingerprint_sha256": combo_fingerprint,
        "reuse_scope": reuse_scope,
        "source_kind": source_kind,
        "source_provider": source_provider,
        "prompt_version": prompt_version,
        "model_name": model_name,
        "model_version": model_version,
        "pipeline_version": pipeline_version,
        "original_object_ref": original_object_ref,
        "original_sha256": original_sha256,
        "original_size_bytes": original_size_bytes,
        "derivative_object_ref": derivative_object_ref,
        "derivative_sha256": derivative_sha256,
        "derivative_size_bytes": derivative_size_bytes,
    }
    content["content_sha256"] = hashlib.sha256(
        _canonical_json(content).encode("utf-8")
    ).hexdigest()
    return content


def _normalize_background_binding(
    *,
    asset_kind: str,
    background_asset_id: Any,
    background_sha256: Any,
) -> tuple[str, str]:
    raw_asset_id = str(background_asset_id or "").strip()
    raw_sha256 = str(background_sha256 or "").strip().lower()
    if asset_kind == "background":
        if raw_asset_id or raw_sha256:
            raise InvalidProductAssetInput(
                "background assets cannot reference another background"
            )
        return "", ""
    if not raw_asset_id or not raw_sha256:
        raise InvalidProductAssetInput(
            "product assets require an exact selected background binding"
        )
    return (
        _identifier(raw_asset_id, "background_asset_id"),
        _sha256(raw_sha256, "background_sha256"),
    )


def _normalize_combo_contract(
    *,
    asset_kind: str,
    category_id: str,
    combo_fingerprint_version: Any,
    combo_components: Any,
    combo_fingerprint_sha256: Any,
) -> tuple[str, list[dict[str, Any]], str]:
    is_combo = asset_kind == "product" and category_id == "combo"
    if not is_combo:
        if combo_fingerprint_version not in (None, ""):
            raise InvalidProductAssetInput(
                "combo fingerprint data is only valid for combo products"
            )
        if combo_components not in (None, (), []):
            raise InvalidProductAssetInput(
                "combo fingerprint data is only valid for combo products"
            )
        if combo_fingerprint_sha256 not in (None, ""):
            raise InvalidProductAssetInput(
                "combo fingerprint data is only valid for combo products"
            )
        return "", [], ""

    if combo_fingerprint_version != COMBO_FINGERPRINT_VERSION:
        raise InvalidProductAssetInput(
            f"combo_fingerprint_version must be {COMBO_FINGERPRINT_VERSION}"
        )
    if combo_components is None:
        raise InvalidProductAssetInput(
            "combo product reuse requires server-owned combo_components"
        )
    canonical, computed = combo_components_fingerprint(
        combo_components,
        version=combo_fingerprint_version,
    )
    supplied = _optional_sha256(
        combo_fingerprint_sha256,
        "combo_fingerprint_sha256",
    )
    if supplied and not _constant_text_equal(supplied, computed):
        raise InvalidProductAssetInput(
            "combo_fingerprint_sha256 does not match combo_components"
        )
    return combo_fingerprint_version, canonical, computed


def _require_exact_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    if str(existing.get("id") or "") != str(expected["id"]):
        raise ProductAssetConflict(
            "idempotency key belongs to a different tenant-bound asset id"
        )
    if str(existing.get("tenant_id") or "") != str(expected["tenant_id"]):
        raise ProductAssetConflict(
            "idempotency replay crossed the tenant boundary"
        )
    if not _constant_text_equal(
        existing.get("content_sha256"),
        expected["content_sha256"],
    ):
        raise ProductAssetConflict(
            "idempotency key was replayed with different asset content"
        )


def _derivative_object(
    object_ref: Any,
    sha256: Any,
    size_bytes: Any,
    *,
    tenant_id: str,
) -> tuple[str | None, str | None, int | None]:
    values = (object_ref, sha256, size_bytes)
    missing = tuple(value is None for value in values)
    if all(missing):
        return None, None, None
    if any(missing):
        raise InvalidProductAssetInput(
            "derivative object_ref, SHA-256, and size must be provided together"
        )
    return (
        _private_object_ref(
            object_ref,
            "derivative_object_ref",
            tenant_id=tenant_id,
        ),
        _sha256(sha256, "derivative_sha256"),
        _bounded_positive_int(
            size_bytes,
            "derivative_size_bytes",
            maximum=(2**63) - 1,
        ),
    )


def _normalized_string_list(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> tuple[list[str], list[str]]:
    if value is None:
        return [], []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidProductAssetInput(f"{field} must be a sequence of strings")
    if len(value) > maximum:
        raise InvalidProductAssetInput(f"{field} has too many values")

    by_normalized: dict[str, str] = {}
    for item in value:
        text = _required_text(
            item,
            field,
            max_length=MAX_LIST_ITEM_LENGTH,
        )
        normalized = normalize_asset_name(text)
        by_normalized.setdefault(normalized, text)

    ordered = sorted(by_normalized.items())
    return (
        [text for _normalized, text in ordered],
        [normalized for normalized, _text in ordered],
    )


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(cursor, row)


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def _row_to_dict(cursor: CursorLike, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    names = [
        str(column[0] if isinstance(column, Sequence) else column.name)
        for column in description
    ]
    if not names or len(names) != len(row):
        raise ProductAssetLibraryError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(names, row))


def _decode_record(record: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(record)
    for field in (
        "aliases",
        "aliases_normalized",
        "match_keywords",
        "match_keywords_normalized",
        "combo_components",
    ):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                decoded[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput(f"{field} must be a string")
    if value != value.strip() or not IDENTIFIER_RE.fullmatch(value):
        raise InvalidProductAssetInput(f"{field} has an invalid format")
    return value


def _tenant_id(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput("tenant_id must be a string")
    if value != value.strip() or not TENANT_ID_RE.fullmatch(value):
        raise InvalidProductAssetInput("tenant_id has an invalid format")
    return value


def _tenant_asset_id(value: Any) -> str:
    if not isinstance(value, str) or not TENANT_ASSET_ID_RE.fullmatch(value):
        raise InvalidProductAssetInput(
            "asset_id must be a server-derived tenant-bound id"
        )
    return value


def _category_id(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput("category_id must be a string")
    if value != value.strip() or not CATEGORY_ID_RE.fullmatch(value):
        raise InvalidProductAssetInput("category_id has an invalid format")
    return value


def _style_id(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput("style_id must be a string")
    if value != value.strip() or not STYLE_ID_RE.fullmatch(value):
        raise InvalidProductAssetInput("style_id has an invalid format")
    return value


def _version(value: Any, field: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise InvalidProductAssetInput(f"{field} has an invalid format")
    return value


def _choice(value: Any, choices: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise InvalidProductAssetInput(
            f"{field} must be one of: {', '.join(sorted(choices))}"
        )
    return value


def _required_text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput(f"{field} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or _has_control_character(value)
    ):
        raise InvalidProductAssetInput(f"{field} has an invalid format")
    return value


def _optional_text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput(f"{field} must be a string")
    if (
        value != value.strip()
        or len(value) > max_length
        or _has_control_character(value)
    ):
        raise InvalidProductAssetInput(f"{field} has an invalid format")
    return value


def _normalize_optional_component_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).lower()
    return "".join(character for character in text if character.isalnum())


def _has_control_character(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        for character in value
    )


def _private_object_ref(
    value: Any,
    field: str,
    *,
    tenant_id: str,
) -> str:
    if not isinstance(value, str):
        raise InvalidProductAssetInput(f"{field} must be a string")
    required_prefix = f"ai-assets/{tenant_id}/"
    if (
        not value.startswith(required_prefix)
        or len(value) <= len(required_prefix)
        or value != value.strip()
        or not OBJECT_REF_RE.fullmatch(value)
        or value.startswith("/")
        or "\\" in value
        or "://" in value
        or _has_control_character(value)
    ):
        raise InvalidProductAssetInput(
            f"{field} must be under {required_prefix}"
        )
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise InvalidProductAssetInput(
            f"{field} must be under {required_prefix}"
        )
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise InvalidProductAssetInput(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _optional_sha256(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    return _sha256(value, field)


def _bounded_positive_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidProductAssetInput(f"{field} must be an integer")
    if value <= 0:
        raise InvalidProductAssetInput(f"{field} must be positive")
    if value > maximum:
        raise InvalidProductAssetInput(f"{field} is too large")
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidProductAssetInput(
            "asset registration must be JSON serializable"
        ) from exc


def _constant_text_equal(left: Any, right: Any) -> bool:
    return hmac.compare_digest(
        str(left or "").encode("utf-8"),
        str(right or "").encode("utf-8"),
    )


def _require_approved_product_background(
    cursor: CursorLike,
    product: Mapping[str, Any],
) -> None:
    background_asset_id = str(product.get("background_asset_id") or "")
    cursor.execute(
        _SELECT_BACKGROUND_FOR_APPROVAL_SQL,
        (background_asset_id,),
    )
    background = _fetchone_dict(cursor)
    if background is None:
        raise ProductAssetReviewConflict(
            "product approval requires an existing approved background "
            "with matching tenant, owner, and SHA-256"
        )
    decoded = _decode_record(background)
    if not (
        str(decoded.get("asset_kind") or "") == "background"
        and _constant_text_equal(
            decoded.get("tenant_id"),
            product.get("tenant_id"),
        )
        and _constant_text_equal(
            decoded.get("owner_user_id"),
            product.get("owner_user_id"),
        )
        and _constant_text_equal(
            decoded.get("original_sha256"),
            product.get("background_sha256"),
        )
        and str(decoded.get("status") or "") == "approved"
        and str(decoded.get("review_status") or "") == "approved"
    ):
        raise ProductAssetReviewConflict(
            "product approval requires an existing approved background "
            "with matching tenant, owner, and SHA-256"
        )


def _is_object_ref_conflict(exc: BaseException) -> bool:
    sqlstate = str(
        getattr(exc, "sqlstate", "")
        or getattr(exc, "pgcode", "")
        or ""
    )
    message = str(exc).lower()
    return sqlstate == "23505" and (
        "object_ref" in message
        or "product_asset_library_object_refs" in message
        or "uq_product_asset_library_original_ref" in message
        or "uq_product_asset_library_derivative_ref" in message
    )


__all__ = [
    "ASSET_ID_VERSION",
    "AssetCreateResult",
    "AssetReviewResult",
    "COMBO_FINGERPRINT_VERSION",
    "InvalidProductAssetInput",
    "ProductAssetConflict",
    "ProductAssetLibraryError",
    "ProductAssetLibraryStore",
    "ProductAssetNotFound",
    "ProductAssetReviewConflict",
    "canonicalize_combo_components",
    "combo_components_fingerprint",
    "disable_asset",
    "find_reusable_assets",
    "get_owned_asset",
    "normalize_asset_name",
    "register_asset",
    "review_asset",
    "tenant_bound_asset_id",
]
