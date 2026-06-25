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


def build_upload_items(records: list[dict[str, Any]], bucket: str, region: str, prefix: str, max_side: int, quality: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for record in records:
        prepared = prepare_jpeg(Path(str(record.get("path") or "")), max_side=max_side, quality=quality)
        index_record = build_index_record(record, prepared, bucket=bucket, region=region, prefix=prefix)
        items.append(
            {
                "record": index_record,
                "image": base64.b64encode(prepared.data).decode("ascii"),
                "contentType": "image/jpeg",
            }
        )
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Push local cleanpic/watermarkpic gallery to Render, then let Render upload to COS.")
    parser.add_argument("--base-url", default=os.environ.get("WAIMAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.environ.get("GALLERY_UPLOAD_TOKEN", ""))
    parser.add_argument("--clean-dir", default=str(DEFAULT_CLEAN_DIR))
    parser.add_argument("--watermark-dir", default=str(DEFAULT_WATERMARK_DIR))
    parser.add_argument("--session", default=f"mac-gallery-{int(time.time())}")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-side", type=int, default=1200)
    parser.add_argument("--quality", type=int, default=84)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="scan and prepare records, but do not upload batches")
    args = parser.parse_args()

    if not args.token and not args.dry_run:
        print(json.dumps({"ok": False, "error": "missing --token or GALLERY_UPLOAD_TOKEN"}, ensure_ascii=False), file=sys.stderr)
        return 2

    status = request_json(args.base_url, "/api/admin/gallery-upload/status")
    if status.get("error"):
        print(json.dumps({"ok": False, "stage": "status", "status": status}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    bucket = str(status.get("bucket") or "")
    region = str(status.get("region") or "")
    prefix = str(status.get("prefix") or "waimai-gallery")
    if not bucket or not region:
        print(json.dumps({"ok": False, "error": "remote COS status is not ready", "status": status}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    scan = scan_library(clean_dir=Path(args.clean_dir), watermark_dir=Path(args.watermark_dir), thumb_dir=None, make_thumbs=False)
    records = scan.records[: args.limit] if args.limit and args.limit > 0 else scan.records
    items = build_upload_items(records, bucket=bucket, region=region, prefix=prefix, max_side=args.max_side, quality=args.quality)
    summary: dict[str, Any] = {
        "ok": True,
        "baseUrl": args.base_url,
        "session": args.session,
        "scanned": scan.total,
        "prepared": len(items),
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
        print(json.dumps({"batch": index, "uploaded": response.get("uploaded"), "sessionRecords": response.get("sessionRecords"), "errors": response.get("errors", [])[:2]}, ensure_ascii=False))
        if response.get("_httpStatus", 200) >= 400:
            summary["ok"] = False
            break

    if args.publish and summary["ok"]:
        published = request_json(args.base_url, "/api/admin/gallery-upload/publish", {"session": args.session}, token=args.token, timeout=180)
        summary["publish"] = published
        summary["ok"] = bool(published.get("ok"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
