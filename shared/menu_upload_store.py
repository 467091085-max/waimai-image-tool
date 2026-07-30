from __future__ import annotations

import hmac
import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Protocol, Sequence

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
PARSER_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
OBJECT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
MIME_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)
PRIVATE_SUMMARY_KEYS = frozenset(
    {
        "objectref",
        "objectkey",
        "privateobjectref",
        "privateobjectkey",
        "storageobjectref",
        "storageobjectkey",
    }
)
MAX_SUMMARY_BYTES = 256 * 1024
MAX_SUMMARY_DEPTH = 8
MAX_SUMMARY_NODES = 4096


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    autocommit: bool

    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class MenuUploadStoreError(RuntimeError):
    pass


class InvalidMenuUploadInput(MenuUploadStoreError):
    pass


class MenuUploadNotFound(MenuUploadStoreError):
    pass


class MenuUploadConflict(MenuUploadStoreError):
    pass


class MenuUploadStateConflict(MenuUploadStoreError):
    pass


@dataclass(frozen=True)
class MenuUploadCreateResult:
    record: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class MenuUploadTransitionResult:
    record: dict[str, Any]
    idempotent: bool


_UPLOAD_COLUMNS = """
    id, owner_user_id, object_ref, object_sha256, original_filename,
    content_type, file_size, parser_version, item_count, store_name,
    parsed_summary, status, failure_reason,
    created_at, updated_at, frozen_at
"""

_INSERT_UPLOAD_SQL = f"""
/* menu_upload_store:create */
INSERT INTO product_menu_uploads (
    id, owner_user_id, object_ref, object_sha256, original_filename,
    content_type, file_size, parser_version, item_count, store_name,
    parsed_summary
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s::jsonb
)
ON CONFLICT (id) DO NOTHING
RETURNING {_UPLOAD_COLUMNS}
"""

_SELECT_BY_ID_SQL = f"""
/* menu_upload_store:select_by_id */
SELECT {_UPLOAD_COLUMNS}
FROM product_menu_uploads
WHERE id = %s
"""

_SELECT_OWNED_SQL = f"""
/* menu_upload_store:select_owned */
SELECT {_UPLOAD_COLUMNS}
FROM product_menu_uploads
WHERE id = %s AND owner_user_id = %s
"""

_SELECT_LATEST_OWNED_SQL = f"""
/* menu_upload_store:select_latest_owned */
SELECT {_UPLOAD_COLUMNS}
FROM product_menu_uploads
WHERE owner_user_id = %s
  AND status IN ('parsed', 'frozen')
ORDER BY created_at DESC, id DESC
LIMIT 1
"""

_MARK_FROZEN_SQL = f"""
/* menu_upload_store:mark_frozen */
UPDATE product_menu_uploads
SET status = 'frozen',
    frozen_at = COALESCE(frozen_at, CURRENT_TIMESTAMP),
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status = 'parsed'
RETURNING {_UPLOAD_COLUMNS}
"""

_MARK_FAILED_SQL = f"""
/* menu_upload_store:mark_failed */
UPDATE product_menu_uploads
SET status = 'failed',
    failure_reason = %s,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status = 'parsed'
RETURNING {_UPLOAD_COLUMNS}
"""


class MenuUploadStore:
    """PostgreSQL menu-upload metadata over an injected DB-API connection.

    Methods ending in ``private_record`` return the private object reference.
    Public methods return an allow-listed DTO that cannot expose that reference.
    The store never opens a connection or runs a schema migration.
    """

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidMenuUploadInput(
                "MenuUploadStore requires an autocommit-disabled connection"
            )
        self.connection = connection

    def create_or_get(
        self,
        *,
        upload_id: str,
        owner_user_id: str,
        object_ref: str,
        object_sha256: str,
        original_filename: str,
        content_type: str,
        file_size: int,
        parser_version: str,
        item_count: int,
        store_name: str,
        parsed_summary: Mapping[str, Any],
    ) -> MenuUploadCreateResult:
        result = self.create_or_get_private_record(
            upload_id=upload_id,
            owner_user_id=owner_user_id,
            object_ref=object_ref,
            object_sha256=object_sha256,
            original_filename=original_filename,
            content_type=content_type,
            file_size=file_size,
            parser_version=parser_version,
            item_count=item_count,
            store_name=store_name,
            parsed_summary=parsed_summary,
        )
        return MenuUploadCreateResult(
            record=public_menu_upload_dto(result.record),
            created=result.created,
        )

    def create_or_get_private_record(
        self,
        *,
        upload_id: str,
        owner_user_id: str,
        object_ref: str,
        object_sha256: str,
        original_filename: str,
        content_type: str,
        file_size: int,
        parser_version: str,
        item_count: int,
        store_name: str,
        parsed_summary: Mapping[str, Any],
    ) -> MenuUploadCreateResult:
        normalized = _normalize_create_values(
            upload_id=upload_id,
            owner_user_id=owner_user_id,
            object_ref=object_ref,
            object_sha256=object_sha256,
            original_filename=original_filename,
            content_type=content_type,
            file_size=file_size,
            parser_version=parser_version,
            item_count=item_count,
            store_name=store_name,
            parsed_summary=parsed_summary,
        )
        parameters = (
            normalized["id"],
            normalized["owner_user_id"],
            normalized["object_ref"],
            normalized["object_sha256"],
            normalized["original_filename"],
            normalized["content_type"],
            normalized["file_size"],
            normalized["parser_version"],
            normalized["item_count"],
            normalized["store_name"],
            normalized["parsed_summary_json"],
        )

        with self._transaction() as cursor:
            cursor.execute(_INSERT_UPLOAD_SQL, parameters)
            inserted = _fetchone_dict(cursor)
            if inserted is not None:
                return MenuUploadCreateResult(
                    record=_decode_record(inserted),
                    created=True,
                )

            cursor.execute(_SELECT_BY_ID_SQL, (normalized["id"],))
            existing = _fetchone_dict(cursor)
            if existing is None:
                raise MenuUploadStoreError(
                    "upload id conflict did not resolve to an existing record"
                )
            decoded = _decode_record(existing)
            if not _same_immutable_upload(decoded, normalized):
                raise MenuUploadConflict(
                    f"menu upload id already exists with different content: "
                    f"{normalized['id']}"
                )
            return MenuUploadCreateResult(record=decoded, created=False)

    def get_owned(
        self,
        *,
        upload_id: str,
        owner_user_id: str,
    ) -> dict[str, Any]:
        return public_menu_upload_dto(
            self.get_owned_private_record(
                upload_id=upload_id,
                owner_user_id=owner_user_id,
            )
        )

    def get_owned_private_record(
        self,
        *,
        upload_id: str,
        owner_user_id: str,
    ) -> dict[str, Any]:
        identifier = _identifier(upload_id, "upload_id")
        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_OWNED_SQL, (identifier, owner))
            record = _fetchone_dict(cursor)
            if record is None:
                raise MenuUploadNotFound(identifier)
            return _decode_record(record)

    def latest_owned(
        self,
        *,
        owner_user_id: str,
    ) -> dict[str, Any] | None:
        record = self.latest_owned_private_record(owner_user_id=owner_user_id)
        return public_menu_upload_dto(record) if record is not None else None

    def latest_owned_private_record(
        self,
        *,
        owner_user_id: str,
    ) -> dict[str, Any] | None:
        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_LATEST_OWNED_SQL, (owner,))
            record = _fetchone_dict(cursor)
            return _decode_record(record) if record is not None else None

    def mark_frozen(
        self,
        *,
        upload_id: str,
        owner_user_id: str,
    ) -> MenuUploadTransitionResult:
        identifier = _identifier(upload_id, "upload_id")
        owner = _identifier(owner_user_id, "owner_user_id")
        with self._transaction() as cursor:
            cursor.execute(_MARK_FROZEN_SQL, (identifier, owner))
            transitioned = _fetchone_dict(cursor)
            if transitioned is not None:
                return MenuUploadTransitionResult(
                    record=public_menu_upload_dto(_decode_record(transitioned)),
                    idempotent=False,
                )

            cursor.execute(_SELECT_OWNED_SQL, (identifier, owner))
            current = _fetchone_dict(cursor)
            if current is None:
                raise MenuUploadNotFound(identifier)
            decoded = _decode_record(current)
            if decoded.get("status") == "frozen":
                return MenuUploadTransitionResult(
                    record=public_menu_upload_dto(decoded),
                    idempotent=True,
                )
            raise MenuUploadStateConflict(
                f"only a parsed upload can be frozen: {identifier}"
            )

    def mark_failed(
        self,
        *,
        upload_id: str,
        owner_user_id: str,
        failure_reason: str,
    ) -> MenuUploadTransitionResult:
        identifier = _identifier(upload_id, "upload_id")
        owner = _identifier(owner_user_id, "owner_user_id")
        reason = _required_text(
            failure_reason,
            "failure_reason",
            max_length=2000,
        )
        with self._transaction() as cursor:
            cursor.execute(_MARK_FAILED_SQL, (reason, identifier, owner))
            transitioned = _fetchone_dict(cursor)
            if transitioned is not None:
                return MenuUploadTransitionResult(
                    record=public_menu_upload_dto(_decode_record(transitioned)),
                    idempotent=False,
                )

            cursor.execute(_SELECT_OWNED_SQL, (identifier, owner))
            current = _fetchone_dict(cursor)
            if current is None:
                raise MenuUploadNotFound(identifier)
            decoded = _decode_record(current)
            if (
                decoded.get("status") == "failed"
                and hmac.compare_digest(
                    str(decoded.get("failure_reason") or "").encode("utf-8"),
                    reason.encode("utf-8"),
                )
            ):
                return MenuUploadTransitionResult(
                    record=public_menu_upload_dto(decoded),
                    idempotent=True,
                )
            raise MenuUploadStateConflict(
                f"only a parsed upload can be marked failed: {identifier}"
            )

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


def public_menu_upload_dto(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the customer-safe allow-listed representation of an upload."""

    decoded = _decode_record(dict(record))
    fields = (
        "id",
        "original_filename",
        "content_type",
        "file_size",
        "parser_version",
        "item_count",
        "store_name",
        "status",
        "created_at",
        "updated_at",
        "frozen_at",
    )
    public = {field: decoded.get(field) for field in fields}
    public["parsed_summary"] = _public_summary(
        decoded.get("parsed_summary"),
        private_object_ref=str(decoded.get("object_ref") or ""),
    )
    return public


def _normalize_create_values(
    *,
    upload_id: Any,
    owner_user_id: Any,
    object_ref: Any,
    object_sha256: Any,
    original_filename: Any,
    content_type: Any,
    file_size: Any,
    parser_version: Any,
    item_count: Any,
    store_name: Any,
    parsed_summary: Any,
) -> dict[str, Any]:
    safe_object_ref = _relative_object_ref(object_ref)
    summary, summary_json = _json_mapping(parsed_summary, "parsed_summary")
    if _json_contains_string(summary, safe_object_ref):
        raise InvalidMenuUploadInput(
            "parsed_summary must not contain the private object_ref"
        )
    return {
        "id": _identifier(upload_id, "upload_id"),
        "owner_user_id": _identifier(owner_user_id, "owner_user_id"),
        "object_ref": safe_object_ref,
        "object_sha256": _sha256(object_sha256, "object_sha256"),
        "original_filename": _filename(original_filename),
        "content_type": _content_type(content_type),
        "file_size": _nonnegative_int(
            file_size,
            "file_size",
            maximum=(2**63) - 1,
        ),
        "parser_version": _parser_version(parser_version),
        "item_count": _nonnegative_int(
            item_count,
            "item_count",
            maximum=(2**31) - 1,
        ),
        "store_name": _optional_text(store_name, "store_name", max_length=255),
        "parsed_summary": summary,
        "parsed_summary_json": summary_json,
    }


def _same_immutable_upload(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    try:
        existing_summary = _canonical_json(existing.get("parsed_summary"))
        expected_summary = _canonical_json(expected["parsed_summary"])
        scalar_fields = (
            "id",
            "owner_user_id",
            "object_ref",
            "original_filename",
            "content_type",
            "parser_version",
            "store_name",
        )
        if any(
            str(existing.get(field)) != str(expected[field])
            for field in scalar_fields
        ):
            return False
        if int(existing.get("file_size")) != int(expected["file_size"]):
            return False
        if int(existing.get("item_count")) != int(expected["item_count"]):
            return False
        return hmac.compare_digest(
            str(existing.get("object_sha256") or ""),
            str(expected["object_sha256"]),
        ) and hmac.compare_digest(
            existing_summary.encode("utf-8"),
            expected_summary.encode("utf-8"),
        )
    except (TypeError, ValueError, InvalidMenuUploadInput):
        return False


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    names = [
        str(column[0] if isinstance(column, Sequence) else column.name)
        for column in description
    ]
    if not names or len(names) != len(row):
        raise MenuUploadStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(names, row))


def _decode_record(record: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(record)
    summary = decoded.get("parsed_summary")
    if isinstance(summary, str):
        try:
            decoded["parsed_summary"] = json.loads(summary)
        except json.JSONDecodeError:
            pass
    return decoded


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidMenuUploadInput(f"{field} must be a string")
    text = value.strip()
    if text != value or not IDENTIFIER_RE.fullmatch(text):
        raise InvalidMenuUploadInput(f"{field} has an invalid format")
    return text


def _relative_object_ref(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidMenuUploadInput("object_ref must be a string")
    text = value
    if (
        not text
        or text != text.strip()
        or not OBJECT_REF_RE.fullmatch(text)
        or text.startswith("/")
        or "\\" in text
        or "://" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise InvalidMenuUploadInput(
            "object_ref must be a safe relative object key"
        )
    segments = text.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise InvalidMenuUploadInput(
            "object_ref must be a safe relative object key"
        )
    return text


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise InvalidMenuUploadInput(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _filename(value: Any) -> str:
    text = _required_text(value, "original_filename", max_length=255)
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise InvalidMenuUploadInput(
            "original_filename must not contain a path"
        )
    return text


def _content_type(value: Any) -> str:
    text = _required_text(value, "content_type", max_length=255).lower()
    if not MIME_TYPE_RE.fullmatch(text):
        raise InvalidMenuUploadInput("content_type must be a valid MIME type")
    return text


def _parser_version(value: Any) -> str:
    if not isinstance(value, str) or not PARSER_VERSION_RE.fullmatch(value):
        raise InvalidMenuUploadInput("parser_version has an invalid format")
    return value


def _required_text(
    value: Any,
    field: str,
    *,
    max_length: int,
) -> str:
    if not isinstance(value, str):
        raise InvalidMenuUploadInput(f"{field} must be a string")
    text = value.strip()
    if (
        not text
        or text != value
        or len(text) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise InvalidMenuUploadInput(f"{field} has an invalid format")
    return text


def _optional_text(
    value: Any,
    field: str,
    *,
    max_length: int,
) -> str:
    if not isinstance(value, str):
        raise InvalidMenuUploadInput(f"{field} must be a string")
    text = value.strip()
    if (
        text != value
        or len(text) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise InvalidMenuUploadInput(f"{field} has an invalid format")
    return text


def _nonnegative_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMenuUploadInput(f"{field} must be an integer")
    if value < 0:
        raise InvalidMenuUploadInput(f"{field} must be nonnegative")
    if value > maximum:
        raise InvalidMenuUploadInput(f"{field} is too large")
    return value


def _json_mapping(
    value: Any,
    field: str,
) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise InvalidMenuUploadInput(f"{field} must be a mapping")
    normalized = dict(value)
    nodes = [0]
    _validate_json_value(normalized, field, depth=0, nodes=nodes)
    serialized = _canonical_json(normalized)
    if len(serialized.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise InvalidMenuUploadInput(f"{field} is too large")
    return json.loads(serialized), serialized


def _validate_json_value(
    value: Any,
    field: str,
    *,
    depth: int,
    nodes: list[int],
) -> None:
    nodes[0] += 1
    if nodes[0] > MAX_SUMMARY_NODES:
        raise InvalidMenuUploadInput(f"{field} has too many values")
    if depth > MAX_SUMMARY_DEPTH:
        raise InvalidMenuUploadInput(f"{field} is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidMenuUploadInput(f"{field} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise InvalidMenuUploadInput(
                    f"{field} contains an invalid object key"
                )
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized_key in PRIVATE_SUMMARY_KEYS:
                raise InvalidMenuUploadInput(
                    f"{field} must not contain private object references"
                )
            _validate_json_value(
                child,
                field,
                depth=depth + 1,
                nodes=nodes,
            )
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_json_value(
                child,
                field,
                depth=depth + 1,
                nodes=nodes,
            )
        return
    raise InvalidMenuUploadInput(f"{field} must be JSON serializable")


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
        raise InvalidMenuUploadInput(
            "parsed_summary must be JSON serializable"
        ) from exc


def _json_contains_string(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return hmac.compare_digest(
            value.encode("utf-8"),
            expected.encode("utf-8"),
        )
    if isinstance(value, Mapping):
        return any(
            _json_contains_string(child, expected)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(_json_contains_string(child, expected) for child in value)
    return False


def _public_summary(
    value: Any,
    *,
    private_object_ref: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    def sanitize(child: Any) -> Any:
        if isinstance(child, Mapping):
            sanitized: dict[str, Any] = {}
            for key, nested in child.items():
                if not isinstance(key, str):
                    continue
                normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized_key in PRIVATE_SUMMARY_KEYS:
                    continue
                if (
                    private_object_ref
                    and isinstance(nested, str)
                    and hmac.compare_digest(
                        nested.encode("utf-8"),
                        private_object_ref.encode("utf-8"),
                    )
                ):
                    continue
                sanitized[key] = sanitize(nested)
            return sanitized
        if isinstance(child, list):
            return [
                sanitize(item)
                for item in child
                if not (
                    private_object_ref
                    and isinstance(item, str)
                    and hmac.compare_digest(
                        item.encode("utf-8"),
                        private_object_ref.encode("utf-8"),
                    )
                )
            ]
        return child

    return sanitize(value)
