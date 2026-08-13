from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import background_catalog  # noqa: E402
import object_storage_service  # noqa: E402
from scripts import build_background_catalog as builder  # noqa: E402


REVIEWER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{1,127}")


def expected_hashes(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        style_value, separator, digest_value = str(value or "").partition("=")
        if not separator:
            raise ValueError("expected hash must use style-id=sha256")
        style_id = background_catalog.style_slot(style_value).style_id
        digest = digest_value.strip().lower()
        if not background_catalog.SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 for {style_id}")
        if style_id in parsed:
            raise ValueError(f"duplicate expected hash for {style_id}")
        parsed[style_id] = digest
    if set(parsed) != set(background_catalog.STYLE_IDS):
        missing = sorted(set(background_catalog.STYLE_IDS) - set(parsed))
        raise ValueError(
            "exactly six expected hashes are required; "
            f"missing={missing}"
        )
    return parsed


def approve_category_manifest(
    category_id: str,
    *,
    expected_sha256: dict[str, str],
    reviewer: str,
    note: str,
    prompt_version: str | None = None,
) -> dict[str, Any]:
    category = background_catalog.normalize_category_id(category_id)
    version = builder.normalize_prompt_version(
        prompt_version or builder.PROMPT_VERSION
    )
    reviewer_id = str(reviewer or "").strip()
    if not REVIEWER_RE.fullmatch(reviewer_id):
        raise ValueError("reviewer must be a stable non-secret identifier")
    review_note = re.sub(r"\s+", " ", str(note or "")).strip()[:500]
    entries = builder.reusable_remote_category_entries(
        category,
        prompt_version=version,
    )
    if entries is None:
        raise RuntimeError("complete current-version remote manifest not found")
    actual_hashes = {
        str(entry["styleId"]): str(entry["sha256"])
        for entry in entries
    }
    if actual_hashes != expected_sha256:
        raise RuntimeError("reviewed SHA-256 set does not match remote manifest")

    document = builder.remote_category_manifest_document(
        category,
        prompt_version=version,
    )
    if document is None:
        raise RuntimeError("remote manifest disappeared before approval")
    reviewed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    approved_assets = []
    for raw_asset in document["assets"]:
        style_id = str(raw_asset["styleId"])
        approved_assets.append(
            {
                **raw_asset,
                "reviewStatus": "approved",
                "reviewedAt": reviewed_at,
                "reviewedBy": reviewer_id,
                "reviewNote": review_note,
            }
        )
    approved = {
        **document,
        "reviewStatus": "approved",
        "reviewedAt": reviewed_at,
        "reviewedBy": reviewer_id,
        "reviewNote": review_note,
        "assets": approved_assets,
        "updatedAt": reviewed_at,
    }
    review_sheet = builder.upload_category_review_sheet(
        entries,
        category_id=category,
        prompt_version=version,
    )
    approved["reviewSheet"] = review_sheet
    key = background_catalog.catalog_manifest_key(
        category,
        version,
        tenant_id=app_module.product_shared_asset_tenant_id(),
    )
    payload = json.dumps(
        approved,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    storage = object_storage_service.get_object_storage_service()
    if storage.put_bytes(payload, object_key=key) != key:
        raise RuntimeError("approved manifest object key mismatch")
    readback = object_storage_service.read_object_bytes_limited(
        storage,
        key,
        2 * 1024 * 1024,
    )
    if hashlib.sha256(readback).digest() != hashlib.sha256(payload).digest():
        raise RuntimeError("approved manifest read-back verification failed")
    verified_entries = builder.reusable_remote_category_entries(
        category,
        prompt_version=version,
    )
    if verified_entries is None or any(
        entry.get("reviewStatus") != "approved"
        for entry in verified_entries
    ):
        raise RuntimeError("approved manifest failed post-write validation")
    return {
        "categoryId": category,
        "promptVersion": version,
        "reviewStatus": "approved",
        "reviewedAt": reviewed_at,
        "reviewedBy": reviewer_id,
        "manifestKey": key,
        "reviewSheetKey": review_sheet["objectKey"],
        "sha256ByStyle": actual_hashes,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Approve one visually reviewed six-slot background manifest. "
            "The write is refused unless all six exact SHA-256 values match."
        )
    )
    parser.add_argument("--category", required=True)
    parser.add_argument(
        "--prompt-version",
        default=builder.DEFAULT_PROMPT_VERSION,
        help="Exact background prompt namespace to review (for example style-background.v14).",
    )
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--expected-sha", action="append", default=[])
    parser.add_argument("--note", default="six-slot visual review passed")
    parser.add_argument("--approve-reviewed", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    hashes = expected_hashes(args.expected_sha)
    try:
        prompt_version = builder.normalize_prompt_version(args.prompt_version)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not args.approve_reviewed:
        raise SystemExit("--approve-reviewed is required for the manifest write")
    result = approve_category_manifest(
        args.category,
        expected_sha256=hashes,
        reviewer=args.reviewer,
        note=args.note,
        prompt_version=prompt_version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
