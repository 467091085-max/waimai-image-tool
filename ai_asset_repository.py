from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


MANIFEST_NAME = "manifest.jsonl"
VALID_STATUSES = {"approved", "rejected", "disabled"}
REUSABLE_STATUS = "approved"
REJECTED_STATUS = "rejected"
GENERATION_REQUIRED_STATUS = "generation_required"
QUALITY_STATUS_PASS = "passed"
QUALITY_STATUS_FAIL = "failed"
QUALITY_STATUS_UNKNOWN = "unknown"
FAILED_QUALITY_STATUSES = {QUALITY_STATUS_FAIL, "rejected", "blocked"}
MIN_REUSABLE_QUALITY_SCORE = 0.65

ASSET_FIELDS = (
    "asset_id",
    "kind",
    "category",
    "style_id",
    "product_name",
    "normalized_product_name",
    "keywords",
    "match_names",
    "source",
    "provider",
    "quality",
    "quality_score",
    "quality_status",
    "quality_reasons",
    "object_key",
    "local_path",
    "sha256",
    "created_at",
    "status",
)

CAMEL_TO_SNAKE = {
    "assetId": "asset_id",
    "styleId": "style_id",
    "productName": "product_name",
    "normalizedProductName": "normalized_product_name",
    "matchNames": "match_names",
    "qualityScore": "quality_score",
    "qualityStatus": "quality_status",
    "qualityReasons": "quality_reasons",
    "objectKey": "object_key",
    "localPath": "local_path",
    "localObjectKey": "local_object_key",
    "originalOutputPath": "original_output_path",
    "sourcePath": "source_path",
    "storageProvider": "storage_provider",
    "modelAction": "model_action",
    "promptType": "prompt_type",
    "createdAt": "created_at",
}


@dataclass(frozen=True)
class AIAssetRecord:
    asset_id: str
    kind: str
    category: str
    style_id: str
    product_name: str
    normalized_product_name: str
    keywords: list[str]
    match_names: list[str]
    quality_score: float
    object_key: str
    local_path: str
    sha256: str
    created_at: str
    status: str = REUSABLE_STATUS
    source: str = "generated"
    provider: str = "unknown"
    quality: str = "unknown"
    quality_status: str = QUALITY_STATUS_UNKNOWN
    quality_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AIAssetRepository:
    """Local JSONL-backed repository for reusable AI-generated image assets."""

    def __init__(self, manifest_path: str | Path):
        path = Path(manifest_path)
        self.manifest_path = path / MANIFEST_NAME if path.suffix == "" else path
        self._lock = threading.Lock()

    @classmethod
    def from_root(cls, root: str | Path) -> "AIAssetRepository":
        return cls(Path(root) / MANIFEST_NAME)

    def upsert(self, record: AIAssetRecord | Mapping[str, Any]) -> dict[str, Any]:
        raw = _mapping_from_record(record)
        had_asset_id = bool(_value_from_aliases(raw, "asset_id"))
        had_created_at = bool(_value_from_aliases(raw, "created_at"))
        had_status = bool(_value_from_aliases(raw, "status"))
        normalized = normalize_asset_record(raw)

        with self._lock:
            records = self._read_records_unlocked()
            index = _find_duplicate_index(records, normalized)
            if index is None:
                records.append(normalized)
            else:
                existing = records[index]
                if not had_asset_id or normalized["asset_id"] != existing["asset_id"]:
                    normalized["asset_id"] = existing["asset_id"]
                if not had_created_at:
                    normalized["created_at"] = existing["created_at"]
                if not had_status and normalized["quality_status"] not in FAILED_QUALITY_STATUSES:
                    normalized["status"] = existing["status"]
                records[index] = normalized
            self._write_records_unlocked(records)
        return dict(normalized)

    def get(self, asset_id: str) -> dict[str, Any] | None:
        target = str(asset_id or "").strip()
        if not target:
            return None
        for record in self.list_assets():
            if record["asset_id"] == target:
                return record
        return None

    def list_assets(
        self,
        *,
        kind: str | None = None,
        category: str | None = None,
        style_id: str | None = None,
        status: str | Sequence[str] | None = None,
        product_name: str | None = None,
        keyword: str | None = None,
        sha256: str | None = None,
        source: str | None = None,
        provider: str | None = None,
        quality: str | None = None,
    ) -> list[dict[str, Any]]:
        statuses = _status_set(status)
        category_norm = normalize_text(category) if category else ""
        product_norm = normalize_text(product_name) if product_name else ""
        keyword_norm = normalize_text(keyword) if keyword else ""
        source_norm = normalize_text(source) if source else ""
        provider_norm = normalize_text(provider) if provider else ""
        quality_norm = normalize_text(quality) if quality else ""

        with self._lock:
            records = self._read_records_unlocked()

        filtered: list[dict[str, Any]] = []
        for record in records:
            if kind is not None and record["kind"] != kind:
                continue
            if category_norm and normalize_text(record["category"]) != category_norm:
                continue
            if style_id is not None and record["style_id"] != style_id:
                continue
            if statuses is not None and record["status"] not in statuses:
                continue
            if sha256 is not None and record["sha256"] != sha256:
                continue
            if source_norm and normalize_text(record.get("source")) != source_norm:
                continue
            if provider_norm and normalize_text(record.get("provider")) != provider_norm:
                continue
            if quality_norm and normalize_text(record.get("quality")) != quality_norm:
                continue
            if product_norm and product_norm not in _record_search_text(record):
                continue
            if keyword_norm and keyword_norm not in _record_search_text(record):
                continue
            filtered.append(dict(record))
        return filtered

    def filter_assets(self, **filters: Any) -> list[dict[str, Any]]:
        return self.list_assets(**filters)

    def list(self, **filters: Any) -> list[dict[str, Any]]:
        return self.list_assets(**filters)

    def filter(self, **filters: Any) -> list[dict[str, Any]]:
        return self.list_assets(**filters)

    def find_reusable(
        self,
        *,
        category: str | None = None,
        style_id: str | None = None,
        product_name: str | None = None,
        keywords: str | Sequence[str] | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query_terms = _query_terms(product_name=product_name, keywords=keywords)
        if not query_terms:
            return []

        candidates = self.list_assets(kind=kind, category=category, style_id=style_id, status=REUSABLE_STATUS)
        ranked: list[tuple[float, dict[str, Any]]] = []

        for record in candidates:
            match_score = _match_score(record, product_name=product_name, query_terms=query_terms)
            if query_terms and match_score <= 0:
                continue
            total_score = match_score + _quality_score(record)
            ranked.append((total_score, record))

        ranked.sort(key=lambda item: (item[0], item[1]["quality_score"], item[1]["created_at"]), reverse=True)
        return [record for _, record in ranked[: max(limit, 0)]]

    def select_reusable_asset(
        self,
        *,
        category: str | None = None,
        style_id: str | None = None,
        product_name: str | None = None,
        keywords: str | Sequence[str] | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        matches = self.find_reusable(
            category=category,
            style_id=style_id,
            product_name=product_name,
            keywords=keywords,
            kind=kind,
            limit=5,
        )
        if matches:
            return {
                "status": "reusable_found",
                "generation_required": False,
                "asset": matches[0],
                "matches": matches,
                "reason": "matched_by_name_keyword_category",
            }

        reason = "missing_name_or_keywords" if not _query_terms(product_name=product_name, keywords=keywords) else "no_reusable_asset_match"
        return {
            "status": GENERATION_REQUIRED_STATUS,
            "generation_required": True,
            "asset": None,
            "matches": [],
            "reason": reason,
        }

    def choose_reusable_asset(self, **filters: Any) -> dict[str, Any]:
        return self.select_reusable_asset(**filters)

    def upsert_generated_asset(
        self,
        *,
        kind: str,
        local_path: str | Path,
        category: str = "",
        style_id: str = "",
        product_name: str = "",
        keywords: str | Sequence[str] | None = None,
        match_names: str | Sequence[str] | None = None,
        source: str = "generated",
        provider: str = "tencent-hunyuan",
        quality: str = "unknown",
        quality_report: Mapping[str, Any] | None = None,
        object_key: str = "",
        sha256: str = "",
        status: str | None = None,
    ) -> dict[str, Any]:
        path = Path(local_path)
        report = _normalize_quality_report(quality_report)
        clean_status = str(status or "").strip().lower()
        if not clean_status:
            clean_status = REJECTED_STATUS if report["quality_status"] in FAILED_QUALITY_STATUSES else REUSABLE_STATUS

        record = {
            "kind": kind,
            "category": category,
            "style_id": style_id,
            "product_name": product_name,
            "keywords": keywords or [],
            "match_names": match_names or [],
            "source": source,
            "provider": provider,
            "quality": quality,
            "quality_score": report["quality_score"],
            "quality_status": report["quality_status"],
            "quality_reasons": report["quality_reasons"],
            "object_key": object_key,
            "local_path": str(path),
            "sha256": sha256 or (sha256_file(path) if path.is_file() else ""),
            "status": clean_status,
        }
        return self.upsert(record)

    def mark_status(self, asset_id: str, status: str, *, quality_note: str = "") -> dict[str, Any]:
        clean_status = str(status or "").strip().lower()
        if clean_status not in VALID_STATUSES:
            raise ValueError(f"invalid AI asset status: {status}")

        with self._lock:
            records = self._read_records_unlocked()
            for index, record in enumerate(records):
                if record["asset_id"] == asset_id:
                    updated = dict(record)
                    updated["status"] = clean_status
                    note = _clean_string(quality_note)
                    if note:
                        updated["quality_reasons"] = _unique_strings([*updated.get("quality_reasons", []), note])
                    if clean_status == REUSABLE_STATUS and updated.get("quality_status") in FAILED_QUALITY_STATUSES:
                        updated["quality_status"] = "manual_approved"
                    records[index] = updated
                    self._write_records_unlocked(records)
                    return dict(updated)
        raise KeyError(asset_id)

    def approve(self, asset_id: str, *, quality_note: str = "") -> dict[str, Any]:
        return self.mark_status(asset_id, "approved", quality_note=quality_note)

    def reject(self, asset_id: str, *, quality_note: str = "") -> dict[str, Any]:
        return self.mark_status(asset_id, "rejected", quality_note=quality_note)

    def disable(self, asset_id: str, *, quality_note: str = "") -> dict[str, Any]:
        return self.mark_status(asset_id, "disabled", quality_note=quality_note)

    def _read_records_unlocked(self) -> list[dict[str, Any]]:
        if not self.manifest_path.exists():
            return []

        records: list[dict[str, Any]] = []
        with self.manifest_path.open("r", encoding="utf-8") as file_obj:
            for line in file_obj:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, Mapping):
                    continue
                try:
                    record = normalize_asset_record(payload)
                except ValueError:
                    continue
                index = _find_duplicate_index(records, record)
                if index is None:
                    records.append(record)
                else:
                    if not record.get("asset_id"):
                        record["asset_id"] = records[index]["asset_id"]
                    records[index] = record
        return records

    def _write_records_unlocked(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.manifest_path.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            for record in records:
                normalized = normalize_asset_record(record)
                tmp.write(json.dumps(_ordered_record(normalized), ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp_path, self.manifest_path)


def normalize_asset_record(record: AIAssetRecord | Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping_from_record(record)
    aliased = _with_snake_case_aliases(raw)
    generation = aliased.get("generation") if isinstance(aliased.get("generation"), Mapping) else {}
    quality_report = _normalize_quality_report(aliased.get("quality_report") if isinstance(aliased.get("quality_report"), Mapping) else None)

    product_name = _clean_string(aliased.get("product_name"))
    normalized_product_name = _clean_string(aliased.get("normalized_product_name")) or normalize_text(product_name)
    category = _clean_string(aliased.get("category"))
    keywords = _coerce_string_list(aliased.get("keywords"))
    match_names = _coerce_string_list(aliased.get("match_names"))
    source = (
        _clean_string(aliased.get("source"))
        or _clean_string(aliased.get("model_action"))
        or _clean_string(generation.get("action"))
        or _clean_string(aliased.get("source_path"))
        or _clean_string(aliased.get("original_output_path"))
        or "generated"
    )
    provider = _clean_string(aliased.get("provider")) or _clean_string(generation.get("provider")) or "unknown"
    quality = _clean_string(aliased.get("quality")) or "unknown"
    quality_score = _coerce_float(aliased.get("quality_score"))
    if quality_report["quality_score"]:
        quality_score = quality_report["quality_score"]
    quality_status = _clean_string(aliased.get("quality_status")).lower() or quality_report["quality_status"]
    quality_reasons = _coerce_string_list(aliased.get("quality_reasons") or quality_report["quality_reasons"])

    if product_name and product_name not in match_names:
        match_names.insert(0, product_name)
    if normalized_product_name and normalized_product_name not in match_names:
        match_names.append(normalized_product_name)
    if not keywords:
        keywords = _derive_keywords(category=category, product_name=product_name, match_names=match_names)

    normalized = {
        "asset_id": _clean_string(aliased.get("asset_id")),
        "kind": _clean_string(aliased.get("kind")),
        "category": category,
        "style_id": _clean_string(aliased.get("style_id")),
        "product_name": product_name,
        "normalized_product_name": normalized_product_name,
        "keywords": _unique_strings(keywords),
        "match_names": _unique_strings(match_names),
        "source": source,
        "provider": provider,
        "quality": quality,
        "quality_score": quality_score,
        "quality_status": quality_status or QUALITY_STATUS_UNKNOWN,
        "quality_reasons": _unique_strings(quality_reasons),
        "object_key": _clean_string(aliased.get("object_key")),
        "local_path": _clean_string(aliased.get("local_path")),
        "sha256": _clean_string(aliased.get("sha256")).lower(),
        "created_at": _clean_string(aliased.get("created_at")) or utc_now_iso(),
        "status": _clean_string(aliased.get("status")).lower() or REUSABLE_STATUS,
    }
    if normalized["quality_status"] in FAILED_QUALITY_STATUSES:
        normalized["status"] = REJECTED_STATUS

    if not normalized["kind"]:
        raise ValueError("AI asset kind is required")
    if normalized["status"] not in VALID_STATUSES:
        raise ValueError(f"invalid AI asset status: {normalized['status']}")
    if not normalized["asset_id"]:
        normalized["asset_id"] = stable_asset_id(normalized)
    return _ordered_record(normalized)


def stable_asset_id(record: Mapping[str, Any]) -> str:
    parts = [
        _clean_string(record.get("kind")),
        _clean_string(record.get("category")),
        _clean_string(record.get("style_id")),
        normalize_text(record.get("product_name")),
        _clean_string(record.get("sha256")),
        _clean_string(record.get("object_key")),
        _clean_string(record.get("local_path")),
    ]
    source = "|".join(parts)
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:20]


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,80}[）)]", "", text)
    return "".join(re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mapping_from_record(record: AIAssetRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(record, AIAssetRecord):
        return record.to_dict()
    return dict(record)


def _with_snake_case_aliases(record: Mapping[str, Any]) -> dict[str, Any]:
    aliased = dict(record)
    for camel, snake in CAMEL_TO_SNAKE.items():
        if snake not in aliased and camel in aliased:
            aliased[snake] = aliased[camel]
    if "quality_score" not in aliased and _looks_numeric(aliased.get("quality")):
        aliased["quality_score"] = aliased["quality"]
    return aliased


def _value_from_aliases(record: Mapping[str, Any], snake_name: str) -> Any:
    if snake_name in record:
        return record[snake_name]
    for camel, snake in CAMEL_TO_SNAKE.items():
        if snake == snake_name and camel in record:
            return record[camel]
    return None


def _ordered_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in ASSET_FIELDS}


def _clean_string(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value or "").strip()


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，\n]+", value)
        return _unique_strings(part.strip() for part in parts)
    if isinstance(value, Iterable):
        return _unique_strings(str(item).strip() for item in value)
    return []


def _unique_strings(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = normalize_text(text) or text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _derive_keywords(*, category: str, product_name: str, match_names: Sequence[str]) -> list[str]:
    chunks = [category, product_name, *match_names]
    keywords: list[str] = []
    for chunk in chunks:
        text = str(chunk or "").strip()
        if not text:
            continue
        keywords.append(text)
        keywords.extend(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", text))
        normalized = normalize_text(text)
        if normalized:
            keywords.append(normalized)
    return _unique_strings(keywords)[:32]


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _looks_numeric(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _normalize_quality_report(report: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        return {
            "quality_score": 0.0,
            "quality_status": QUALITY_STATUS_UNKNOWN,
            "quality_reasons": [],
        }

    raw_score = report.get("quality_score", report.get("score"))
    score = _coerce_float(raw_score)
    passed = report.get("passed")
    status = _clean_string(report.get("quality_status") or report.get("status")).lower()
    reasons = _coerce_string_list(report.get("quality_reasons") or report.get("reasons") or report.get("fail_reasons"))

    if not status:
        if isinstance(passed, bool):
            status = QUALITY_STATUS_PASS if passed else QUALITY_STATUS_FAIL
        elif raw_score not in (None, ""):
            status = QUALITY_STATUS_PASS if score >= MIN_REUSABLE_QUALITY_SCORE else QUALITY_STATUS_FAIL
        elif reasons:
            status = QUALITY_STATUS_FAIL
        else:
            status = QUALITY_STATUS_UNKNOWN
    if isinstance(passed, bool) and raw_score in (None, ""):
        score = 1.0 if passed else 0.0
    if status in FAILED_QUALITY_STATUSES and not reasons:
        reasons = ["quality_guard_failed"]

    return {
        "quality_score": score,
        "quality_status": status,
        "quality_reasons": _unique_strings(reasons),
    }


def _status_set(status: str | Sequence[str] | None) -> set[str] | None:
    if status is None:
        return None
    if isinstance(status, str):
        statuses = {status}
    else:
        statuses = {str(item) for item in status}
    clean = {item.strip().lower() for item in statuses if str(item).strip()}
    invalid = clean - VALID_STATUSES
    if invalid:
        raise ValueError(f"invalid AI asset status: {sorted(invalid)[0]}")
    return clean


def _identity_keys(record: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("asset_id", "sha256", "object_key", "local_path"):
        value = _clean_string(record.get(field))
        if value:
            keys.add(f"{field}:{value.lower() if field == 'sha256' else value}")
    return keys


def _find_duplicate_index(records: Sequence[Mapping[str, Any]], incoming: Mapping[str, Any]) -> int | None:
    incoming_keys = _identity_keys(incoming)
    if not incoming_keys:
        return None
    for index, record in enumerate(records):
        if incoming_keys & _identity_keys(record):
            return index
    return None


def _record_search_text(record: Mapping[str, Any]) -> str:
    parts = [
        record.get("product_name"),
        record.get("normalized_product_name"),
        record.get("category"),
        *record.get("keywords", []),
        *record.get("match_names", []),
    ]
    return " ".join(normalize_text(part) for part in parts if part)


def _query_terms(*, product_name: str | None, keywords: str | Sequence[str] | None) -> list[str]:
    terms: list[str] = []
    if product_name:
        terms.append(product_name)
        terms.extend(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", product_name))
    if isinstance(keywords, str):
        terms.extend(re.split(r"[,，\s]+", keywords))
    elif keywords:
        terms.extend(str(keyword) for keyword in keywords)
    return _unique_strings(normalize_text(term) for term in terms if normalize_text(term))


def _match_score(record: Mapping[str, Any], *, product_name: str | None, query_terms: Sequence[str]) -> float:
    if not product_name and not query_terms:
        return 0.0

    product_norm = normalize_text(product_name)
    record_name = normalize_text(record.get("product_name"))
    normalized_name = normalize_text(record.get("normalized_product_name"))
    match_names = [normalize_text(value) for value in record.get("match_names", [])]
    keywords = [normalize_text(value) for value in record.get("keywords", [])]
    searchable = [record_name, normalized_name, *match_names, *keywords]
    searchable = [value for value in searchable if value]

    score = 0.0
    if product_norm:
        if product_norm in {record_name, normalized_name, *match_names}:
            score += 100.0
        elif any(product_norm in value or value in product_norm for value in searchable):
            score += 45.0

    for term in query_terms:
        if term in keywords:
            score += 18.0
        elif any(term in value or value in term for value in searchable):
            score += 10.0
    return score


def _quality_score(record: Mapping[str, Any]) -> float:
    return _coerce_float(record.get("quality_score"))
