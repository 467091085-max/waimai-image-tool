from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from library_index import DEFAULT_CLEAN_DIR, DEFAULT_WATERMARK_DIR, scan_library
from scripts.sync_gallery_to_cos import build_index_record, prepare_jpeg


DEFAULT_BASE_URL = "https://waimai-image-tool-1.onrender.com"


def request_json(base_url: str, path: str, payload: dict[str, Any] | None = None, token: str = "", timeout: int = 120) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    if token:
        headers["X-Gallery-Upload-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else {}
            body["_httpStatus"] = resp.status
            return body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:500]}
        body["_httpStatus"] = exc.code
        return body


def batched(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    size = max(1, int(size))
    return [items[index : index + size] for index in range(0, len(items), size)]


def upload_status_ready(status: dict[str, Any], *, require_enabled: bool) -> bool:
    if status.get("error"):
        return False
    if not status.get("cosReady") or not status.get("bucket") or not status.get("region"):
        return False
    return bool(status.get("enabled")) if require_enabled else True


def wait_for_upload_status(base_url: str, *, require_enabled: bool, wait_seconds: int, interval_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, int(wait_seconds))
    attempt = 0
    last = {}
    while True:
        attempt += 1
        status = request_json(base_url, "/api/admin/gallery-upload/status")
        last = status
        if upload_status_ready(status, require_enabled=require_enabled):
            return status
        if time.monotonic() >= deadline:
            return last
        print(
            json.dumps(
                {
                    "waiting": "gallery-upload-status",
                    "attempt": attempt,
                    "enabled": status.get("enabled"),
                    "cosReady": status.get("cosReady"),
                    "httpStatus": status.get("_httpStatus"),
                    "error": status.get("error", ""),
                },
                ensure_ascii=False,
            )
        )
        time.sleep(max(1, int(interval_seconds)))


def verify_remote_library(base_url: str, expected_min_images: int, attempts: int = 8, interval_seconds: int = 4) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        status = request_json(base_url, "/api/library-status")
        last = status
        remote_index = bool(status.get("remoteIndex"))
        index_images = int(status.get("indexImages") or 0)
        remote_images = int(status.get("remoteImages") or 0)
        if remote_index and index_images >= expected_min_images and remote_images >= min(expected_min_images, index_images):
            return {"ok": True, "attempt": attempt, "status": status}
        if attempt < attempts:
            time.sleep(max(1, int(interval_seconds)))
    return {"ok": False, "status": last}


def build_upload_items(
    records: list[dict[str, Any]], bucket: str, region: str, prefix: str, max_side: int, quality: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for record in records:
        path = Path(str(record.get("path") or ""))
        try:
            prepared = prepare_jpeg(path, max_side=max_side, quality=quality)
            index_record = build_index_record(record, prepared, bucket=bucket, region=region, prefix=prefix)
            items.append(
                {
                    "record": index_record,
                    "image": base64.b64encode(prepared.data).decode("ascii"),
                    "contentType": "image/jpeg",
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "path": str(path),
                    "dish": str(record.get("dish") or record.get("dish_name") or ""),
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
    return items, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Push local cleanpic/watermarkpic gallery to Render, then let Render upload to COS.")
    parser.add_argument("--base-url", default=os.environ.get("WAIMAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.environ.get("GALLERY_UPLOAD_TOKEN", ""))
    parser.add_argument("--clean-dir", default=str(DEFAULT_CLEAN_DIR))
    parser.add_argument("--watermark-dir", default=str(DEFAULT_WATERMARK_DIR))
    parser.add_argument("--session", default=f"mac-gallery-{int(time.time())}")
    parser.add_argument("--offset", type=int, default=0, help="skip this many prepared upload items before sending batches")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-side", type=int, default=1200)
    parser.add_argument("--quality", type=int, default=84)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="scan and prepare records, but do not upload batches")
    parser.add_argument("--wait-ready", type=int, default=0, help="seconds to wait for Render upload token/COS status before failing")
    parser.add_argument("--wait-interval", type=int, default=8)
    parser.add_argument("--verify-library", action="store_true", help="after publishing, poll /api/library-status for remoteIndex=true")
    args = parser.parse_args()

    if not args.token and not args.dry_run:
        print(json.dumps({"ok": False, "error": "missing --token or GALLERY_UPLOAD_TOKEN"}, ensure_ascii=False), file=sys.stderr)
        return 2

    status = wait_for_upload_status(
        args.base_url,
        require_enabled=not args.dry_run,
        wait_seconds=args.wait_ready,
        interval_seconds=args.wait_interval,
    )
    if status.get("error"):
        print(json.dumps({"ok": False, "stage": "status", "status": status}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    bucket = str(status.get("bucket") or "")
    region = str(status.get("region") or "")
    prefix = str(status.get("prefix") or "waimai-gallery")
    if not bucket or not region:
        print(json.dumps({"ok": False, "error": "remote COS status is not ready", "status": status}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    if not args.dry_run and not status.get("enabled"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "remote gallery upload is disabled; set GALLERY_UPLOAD_TOKEN in Render and wait for deploy",
                    "status": {k: status.get(k) for k in ("enabled", "cosReady", "bucket", "region", "prefix", "indexUrl")},
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    scan = scan_library(clean_dir=Path(args.clean_dir), watermark_dir=Path(args.watermark_dir), thumb_dir=None, make_thumbs=False)
    records = scan.records[: args.limit] if args.limit and args.limit > 0 and args.offset <= 0 else scan.records
    items, prepare_errors = build_upload_items(records, bucket=bucket, region=region, prefix=prefix, max_side=args.max_side, quality=args.quality)
    prepared_before_offset = len(items)
    if args.offset and args.offset > 0:
        items = items[args.offset :]
        if args.limit and args.limit > 0:
            items = items[: args.limit]
    summary: dict[str, Any] = {
        "ok": True,
        "baseUrl": args.base_url,
        "session": args.session,
        "scanned": scan.total,
        "offset": max(0, int(args.offset)),
        "preparedBeforeOffset": prepared_before_offset,
        "prepared": len(items),
        "prepareErrors": prepare_errors,
        "dryRun": bool(args.dry_run),
        "remoteStatus": {k: status.get(k) for k in ("enabled", "cosReady", "bucket", "region", "prefix", "indexUrl")},
        "uploaded": 0,
        "errors": [],
    }
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    for index, chunk in enumerate(batched(items, args.batch_size), start=1):
        response = request_json(
            args.base_url,
            "/api/admin/gallery-upload/batch",
            {"session": args.session, "records": chunk},
            token=args.token,
            timeout=180,
        )
        summary["uploaded"] += int(response.get("uploaded") or 0)
        if response.get("errors"):
            summary["errors"].extend(response["errors"])
        print(
            json.dumps(
                {
                    "batch": index,
                    "uploaded": response.get("uploaded"),
                    "sessionRecords": response.get("sessionRecords"),
                    "httpStatus": response.get("_httpStatus"),
                    "error": response.get("error", ""),
                    "code": response.get("code", ""),
                    "errors": response.get("errors", [])[:2],
                },
                ensure_ascii=False,
            )
        )
        if response.get("_httpStatus", 200) >= 400:
            summary["ok"] = False
            break

    if args.publish and summary["ok"]:
        published = request_json(args.base_url, "/api/admin/gallery-upload/publish", {"session": args.session}, token=args.token, timeout=180)
        summary["publish"] = published
        summary["ok"] = bool(published.get("ok"))
        if summary["ok"] and args.verify_library:
            summary["libraryVerification"] = verify_remote_library(args.base_url, max(1, int(summary.get("uploaded") or 0)))
            summary["ok"] = bool(summary["libraryVerification"].get("ok"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
