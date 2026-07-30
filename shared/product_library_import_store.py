from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Optional, Protocol


IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$"
)
TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ASSET_ID_RE = re.compile(r"^asset-[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OBJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
MAX_IMPORT_ITEMS = 200


class CursorLike(Protocol):
    description: Optional[Sequence[Any]]

    def execute(
        self,
        operation: str,
        parameters: Sequence[Any] = (),
    ) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...


class ProductLibraryImportError(RuntimeError):
    pass


class InvalidProductLibraryImport(
    ProductLibraryImportError,
    ValueError,
):
    pass


class ProductLibraryImportConflict(ProductLibraryImportError):
    pass


@dataclass(frozen=True)
class LibraryImportResult:
    batch: dict[str, Any]
    items: list[dict[str, Any]]
    created: bool


_BATCH_COLUMNS = """
    id, tenant_id, actor_user_id, idempotency_key, request_sha256,
    asset_count, content_sha256, created_at
""".strip()

_ITEM_COLUMNS = """
    batch_id, item_index, member_name, asset_id, object_ref,
    object_sha256, object_size_bytes, created_at
""".strip()

_INSERT_BATCH_SQL = f"""
/* product_library_import_store:insert_batch */
INSERT INTO product_library_import_batches (
    id, tenant_id, actor_user_id, idempotency_key, request_sha256,
    asset_count, content_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
RETURNING {_BATCH_COLUMNS}
"""

_SELECT_BATCH_SQL = f"""
/* product_library_import_store:select_batch */
SELECT {_BATCH_COLUMNS}
FROM product_library_import_batches
WHERE tenant_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_INSERT_ITEM_SQL = f"""
/* product_library_import_store:insert_item */
INSERT INTO product_library_import_items (
    batch_id, item_index, member_name, asset_id, object_ref,
    object_sha256, object_size_bytes
) VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING {_ITEM_COLUMNS}
"""

_SELECT_ITEMS_SQL = f"""
/* product_library_import_store:select_items */
SELECT {_ITEM_COLUMNS}
FROM product_library_import_items
WHERE batch_id = %s
ORDER BY item_index
"""


def stable_library_import_id(
    tenant_id: str,
    idempotency_key: str,
) -> str:
    tenant = _tenant_id(tenant_id)
    key = _identifier(idempotency_key, "idempotency_key")
    digest = hashlib.sha256(
        _canonical_json(
            {
                "version": "library-import.v1",
                "tenant_id": tenant,
                "idempotency_key": key,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"library_import_{digest[:40]}"


def record_import_batch(
    cursor: CursorLike,
    *,
    tenant_id: str,
    actor_user_id: str,
    idempotency_key: str,
    request_sha256: str,
    items: Sequence[Mapping[str, Any]],
) -> LibraryImportResult:
    tenant = _tenant_id(tenant_id)
    actor = _identifier(actor_user_id, "actor_user_id")
    key = _identifier(idempotency_key, "idempotency_key")
    request_digest = _sha256(request_sha256, "request_sha256")
    normalized_items = _normalize_items(items)
    batch_id = stable_library_import_id(tenant, key)
    content = {
        "id": batch_id,
        "tenant_id": tenant,
        "actor_user_id": actor,
        "idempotency_key": key,
        "request_sha256": request_digest,
        "asset_count": len(normalized_items),
        "items": normalized_items,
    }
    content_sha256 = hashlib.sha256(
        _canonical_json(content).encode("utf-8")
    ).hexdigest()

    cursor.execute(
        _INSERT_BATCH_SQL,
        (
            batch_id,
            tenant,
            actor,
            key,
            request_digest,
            len(normalized_items),
            content_sha256,
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        inserted_items: list[dict[str, Any]] = []
        for item_index, item in enumerate(normalized_items):
            cursor.execute(
                _INSERT_ITEM_SQL,
                (
                    batch_id,
                    item_index,
                    item["member_name"],
                    item["asset_id"],
                    item["object_ref"],
                    item["object_sha256"],
                    item["object_size_bytes"],
                ),
            )
            inserted_item = _fetchone_dict(cursor)
            if inserted_item is None:
                raise ProductLibraryImportConflict(
                    "library import item insert returned no row"
                )
            inserted_items.append(inserted_item)
        return LibraryImportResult(
            batch=inserted,
            items=inserted_items,
            created=True,
        )

    cursor.execute(_SELECT_BATCH_SQL, (tenant, key))
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductLibraryImportConflict(
            "library import conflicted without a batch"
        )
    actual_sha256 = str(existing.get("content_sha256") or "")
    if not hmac.compare_digest(actual_sha256, content_sha256):
        raise ProductLibraryImportConflict(
            "library import idempotency payload changed"
        )
    cursor.execute(_SELECT_ITEMS_SQL, (batch_id,))
    existing_items = _fetchall_dicts(cursor)
    if len(existing_items) != len(normalized_items):
        raise ProductLibraryImportConflict(
            "library import replay item count changed"
        )
    return LibraryImportResult(
        batch=existing,
        items=existing_items,
        created=False,
    )


def _normalize_items(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise InvalidProductLibraryImport("items must be a sequence")
    if not 1 <= len(items) <= MAX_IMPORT_ITEMS:
        raise InvalidProductLibraryImport(
            f"items must contain 1 to {MAX_IMPORT_ITEMS} records"
        )
    normalized: list[dict[str, Any]] = []
    member_names: set[str] = set()
    asset_ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise InvalidProductLibraryImport(
                "each import item must be an object"
            )
        member_name = _member_name(item.get("member_name"))
        asset_id = str(item.get("asset_id") or "").strip()
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise InvalidProductLibraryImport("invalid asset_id")
        object_ref = _object_ref(item.get("object_ref"))
        object_sha256 = _sha256(
            item.get("object_sha256"),
            "object_sha256",
        )
        object_size_bytes = _positive_int(
            item.get("object_size_bytes"),
            "object_size_bytes",
        )
        if member_name in member_names:
            raise InvalidProductLibraryImport(
                "duplicate import member_name"
            )
        if asset_id in asset_ids:
            raise InvalidProductLibraryImport(
                "duplicate import asset_id"
            )
        member_names.add(member_name)
        asset_ids.add(asset_id)
        normalized.append(
            {
                "member_name": member_name,
                "asset_id": asset_id,
                "object_ref": object_ref,
                "object_sha256": object_sha256,
                "object_size_bytes": object_size_bytes,
            }
        )
    return normalized


def _fetchone_dict(cursor: CursorLike) -> Optional[dict[str, Any]]:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    columns = _columns(cursor)
    return dict(zip(columns, row))


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    columns = _columns(cursor)
    return [dict(zip(columns, row)) for row in rows]


def _columns(cursor: CursorLike) -> list[str]:
    description = cursor.description or ()
    columns = [
        str(getattr(column, "name", column[0] if column else ""))
        for column in description
    ]
    if not columns:
        raise ProductLibraryImportError(
            "DB-API cursor did not expose row metadata"
        )
    return columns


def _tenant_id(value: Any) -> str:
    text = str(value or "").strip()
    if not TENANT_RE.fullmatch(text):
        raise InvalidProductLibraryImport("invalid tenant_id")
    return text


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(text):
        raise InvalidProductLibraryImport(f"invalid {field}")
    return text


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(text):
        raise InvalidProductLibraryImport(f"invalid {field}")
    return text


def _member_name(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not 1 <= len(text) <= 512
        or "\\" in text
        or "://" in text
        or "//" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise InvalidProductLibraryImport("invalid member_name")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InvalidProductLibraryImport("invalid member_name")
    return path.as_posix()


def _object_ref(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not OBJECT_REF_RE.fullmatch(text)
        or "\\" in text
        or "://" in text
        or "//" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise InvalidProductLibraryImport("invalid object_ref")
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidProductLibraryImport(f"invalid {field}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductLibraryImport(f"invalid {field}") from exc
    if number <= 0:
        raise InvalidProductLibraryImport(f"invalid {field}")
    return number


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "InvalidProductLibraryImport",
    "LibraryImportResult",
    "ProductLibraryImportConflict",
    "ProductLibraryImportError",
    "record_import_batch",
    "stable_library_import_id",
]
