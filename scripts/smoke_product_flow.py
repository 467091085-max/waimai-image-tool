from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


STYLE_IDS = [f"style-{index}" for index in range(1, 7)]
DEFAULT_PLATFORMS = ["meituan", "eleme", "jd"]


@dataclass
class ClientResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]
    url: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class UrllibProductClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, query: dict[str, Any] | None = None) -> ClientResponse:
        return self._request("GET", path, query=query)

    def post_json(self, path: str, payload: dict[str, Any]) -> ClientResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request("POST", path, body=body, headers={"Content-Type": "application/json"})

    def post_file(self, path: str, file_path: str | Path, field_name: str = "file") -> ClientResponse:
        file_path = Path(file_path)
        boundary = f"----codex-smoke-product-{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = header + file_path.read_bytes() + footer
        return self._request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> ClientResponse:
        url = self._url(path, query)
        req = request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return ClientResponse(resp.status, resp.read(), dict(resp.headers.items()), url)
        except error.HTTPError as exc:
            return ClientResponse(exc.code, exc.read(), dict(exc.headers.items()), url)

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        if not query:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{parse.urlencode(query, doseq=True)}"


class StepLog:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def add(
        self,
        name: str,
        ok: bool,
        *,
        http_status: int | None = None,
        fields: dict[str, Any] | None = None,
        reason: str = "",
        skipped: bool = False,
    ) -> dict[str, Any]:
        step: dict[str, Any] = {
            "name": name,
            "ok": bool(ok),
            "status": "skipped" if skipped else ("ok" if ok else "fail"),
        }
        if http_status is not None:
            step["httpStatus"] = http_status
        if fields:
            step["fields"] = fields
        if reason:
            step["reason"] = reason
        self.steps.append(step)
        return step


def create_default_menu_file(directory: str | Path | None = None) -> Path:
    from openpyxl import Workbook

    root = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="waimai_product_menu_"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / "product_smoke_menu.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "菜单"
    sheet.append(["分类", "菜品名", "价格", "类型", "套餐内容/规格", "备注"])
    sheet.append(["热销", "老长沙辣椒炒肉盖码饭", "19.8", "单品", "", "smoke"])
    sheet.append(["热销", "小炒黄牛肉盖码饭", "25.8", "单品", "", "smoke"])
    sheet.append(["套餐", "辣椒炒肉+茄子肉末盖码饭", "24.8", "套餐/组合", "辣椒炒肉；茄子肉末；米饭", "smoke"])
    workbook.save(path)
    return path


def run_product_flow(
    client: Any,
    *,
    menu_file: str | Path | None = None,
    base_url: str = "",
    style_first: bool = False,
    limit: int = 0,
    live_generate: bool = False,
) -> dict[str, Any]:
    started_at = utc_now()
    log = StepLog()
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if menu_file is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="waimai_product_acceptance_")
        menu_path = create_default_menu_file(temp_dir.name)
        menu_source = "generated"
    else:
        menu_path = Path(menu_file)
        menu_source = "provided"

    selected_style = ""
    plan: dict[str, Any] | None = None
    job_id = ""
    job_payload: dict[str, Any] | None = None

    try:
        home = safe_request(lambda: client.get("/"))
        if home["response"] is None:
            log.add("home", False, reason=home["error"])
        else:
            resp = home["response"]
            ok = 200 <= resp.status_code < 400 and len(resp.body) > 0
            log.add(
                "home",
                ok,
                http_status=resp.status_code,
                fields={"bytes": len(resp.body), "contentType": header_value(resp, "Content-Type")},
                reason="" if ok else http_reason(resp, None),
            )

        tencent_status = request_json(client, "GET", "/api/tencent-status")
        tencent_data = tencent_status["data"] if isinstance(tencent_status["data"], dict) else {}
        ok = not tencent_status["error"] and "provider" in tencent_data and "configured" in tencent_data
        log.add(
            "tencent_status",
            ok,
            http_status=tencent_status["status"],
            fields=compact_fields(
                tencent_data,
                ["provider", "configured", "cosReady", "region", "styleBackgroundsLive", "localFallbackAllowed"],
            ),
            reason="" if ok else tencent_status["error"] or "missing provider/configured fields",
        )

        account_status = request_json(client, "GET", "/api/account")
        account_data = account_status["data"] if isinstance(account_status["data"], dict) else {}
        log.add(
            "account_status",
            not account_status["error"] and isinstance(account_data, dict),
            http_status=account_status["status"],
            fields=compact_fields(account_data.get("account") if isinstance(account_data.get("account"), dict) else account_data, ["balance", "userId"]),
            reason=account_status["error"],
        )

        library_status = request_json(client, "GET", "/api/library-status")
        library_data = library_status["data"] if isinstance(library_status["data"], dict) else {}
        library_ok = (
            not library_status["error"]
            and int_or_zero(library_data.get("total")) > 0
            and int_or_zero(library_data.get("styles")) >= 1
        )
        log.add(
            "library_status",
            library_ok,
            http_status=library_status["status"],
            fields=compact_fields(library_data, ["total", "reusable", "stores", "styles", "sources", "externalDirs"]),
            reason="" if library_ok else library_status["error"] or "library has no reusable product images",
        )

        if not menu_path.exists():
            log.add("upload_menu", False, fields={"menuFile": str(menu_path), "source": menu_source}, reason="menu file does not exist")
        else:
            upload = safe_request(lambda: client.post_file("/api/upload-menu", menu_path, "file"))
            upload_data = parse_response_json(upload["response"]) if upload["response"] is not None else None
            upload_error = upload["error"] or response_error(upload["response"], upload_data)
            menu_info = upload_data.get("menu") if isinstance(upload_data, dict) and isinstance(upload_data.get("menu"), dict) else {}
            upload_ok = not upload_error and isinstance(upload_data, dict) and upload_data.get("ok") is True and int_or_zero(menu_info.get("count")) > 0
            log.add(
                "upload_menu",
                upload_ok,
                http_status=upload["response"].status_code if upload["response"] is not None else None,
                fields={
                    "menuFile": str(menu_path),
                    "source": menu_source,
                    "store": menu_info.get("store"),
                    "count": menu_info.get("count"),
                    "kindCounts": menu_info.get("kindCounts"),
                },
                reason="" if upload_ok else upload_error or "upload response did not include ok=true and a non-empty menu",
            )

        plan_response = request_json(client, "GET", "/api/plan")
        if isinstance(plan_response["data"], dict):
            plan = plan_response["data"]
        styles = plan.get("styles") if isinstance(plan, dict) else []
        results = plan.get("results") if isinstance(plan, dict) else []
        plan_ok = not plan_response["error"] and isinstance(styles, list) and isinstance(results, list) and len(results) > 0
        pricing = plan.get("pricing") if isinstance(plan, dict) and isinstance(plan.get("pricing"), dict) else {}
        quote = plan.get("quote") if isinstance(plan, dict) and isinstance(plan.get("quote"), dict) else {}
        plan_fields = {
            "styleCount": len(styles) if isinstance(styles, list) else 0,
            "resultCount": len(results) if isinstance(results, list) else 0,
            "selectedStyle": plan.get("selectedStyle") if isinstance(plan, dict) else "",
            "summary": plan.get("summary") if isinstance(plan, dict) else {},
            "pricing": compact_fields(pricing, ["standardPoints", "premiumPoints", "customEditPoints"]),
            "quote": compact_fields(quote, ["points", "cash", "rate"]),
        }
        log.add(
            "plan",
            plan_ok,
            http_status=plan_response["status"],
            fields=plan_fields,
            reason="" if plan_ok else plan_response["error"] or "plan is missing styles or menu results",
        )

        if isinstance(styles, list):
            present = [str(style.get("id")) for style in styles if isinstance(style, dict) and style.get("id")]
        else:
            present = []
        missing_styles = [style_id for style_id in STYLE_IDS if style_id not in set(present)]
        style_cards = [
            compact_fields(style, ["id", "name", "source", "count", "needsGeneratedBackground", "backgroundJob"])
            for style in styles
            if isinstance(style, dict) and style.get("id") in STYLE_IDS
        ] if isinstance(styles, list) else []
        log.add(
            "style_catalog",
            not missing_styles,
            fields={"expected": STYLE_IDS, "present": present, "missing": missing_styles, "cards": style_cards},
            reason="" if not missing_styles else f"missing required styles: {', '.join(missing_styles)}",
        )

        selected_style = choose_style(plan, style_first=style_first)
        if selected_style:
            log.add(
                "style_selection",
                True,
                fields={"selectedStyle": selected_style, "styleFirst": style_first},
            )
        else:
            log.add("style_selection", False, fields={"styleFirst": style_first}, reason="no selectable style in plan")

        for style_id in STYLE_IDS:
            preview = request_json(client, "GET", "/api/style-preview", query={"style": style_id})
            preview_data = preview["data"] if isinstance(preview["data"], dict) else {}
            samples = preview_data.get("samples")
            sample_jobs = []
            if isinstance(samples, list):
                for sample in samples[:6]:
                    job = sample.get("job") if isinstance(sample, dict) and isinstance(sample.get("job"), dict) else {}
                    sample_jobs.append(compact_fields(job, ["status", "provider", "action", "error"]))
            preview_ok = (
                not preview["error"]
                and preview_data.get("style") == style_id
                and isinstance(samples, list)
            )
            log.add(
                f"style_preview:{style_id}",
                preview_ok,
                http_status=preview["status"],
                fields={
                    "style": preview_data.get("style"),
                    "styleName": preview_data.get("styleName"),
                    "sampleCount": len(samples) if isinstance(samples, list) else 0,
                    "previewFreeImages": preview_data.get("previewFreeImages"),
                    "sampleJobs": sample_jobs,
                },
                reason="" if preview_ok else preview["error"] or "style preview response missing style/samples",
            )

        if selected_style:
            job_request = {
                "style": selected_style,
                "quality": "standard",
                "selectedRows": [1],
                "platforms": DEFAULT_PLATFORMS,
                "watermark": {"enabled": False},
            }
            created = request_json(client, "POST", "/api/jobs", payload=job_request)
            job_payload = created["data"] if isinstance(created["data"], dict) else {}
            job = job_payload.get("job") if isinstance(job_payload.get("job"), dict) else {}
            job_id = str(job.get("id") or "")
            job_ok = not created["error"] and bool(job_id) and int_or_zero(job.get("totalItems")) >= 1
            log.add(
                "job_create",
                job_ok,
                http_status=created["status"],
                fields={
                    "jobId": job_id,
                    "status": job.get("status"),
                    "totalItems": job.get("totalItems"),
                    "pendingItems": job.get("pendingItems"),
                    "points": job.get("points"),
                    "progress": job.get("progress"),
                    "poll": job_payload.get("poll"),
                },
                reason="" if job_ok else created["error"] or "job was not created with at least one item",
            )
        else:
            log.add("job_create", False, reason="skipped because no style was selected", skipped=True)

        effective_limit = min(max(int_or_zero(limit), 0), 1)
        if live_generate and job_id and effective_limit > 0:
            run_payload = {
                "limit": effective_limit,
                "paid": True,
                "orderId": f"smoke_product_flow_{int(time.time())}",
            }
            run_response = request_json(client, "POST", f"/api/jobs/{job_id}/run", payload=run_payload)
            run_data = run_response["data"] if isinstance(run_response["data"], dict) else {}
            run_job = run_data.get("job") if isinstance(run_data.get("job"), dict) else {}
            first_item = first_job_item(run_job)
            run_fields = {
                "jobId": job_id,
                "requestedLimit": limit,
                "effectiveLimit": effective_limit,
                "jobStatus": run_job.get("status"),
                "progress": run_job.get("progress"),
                "completedItems": run_job.get("completedItems"),
                "failedItems": run_job.get("failedItems"),
                "pendingItems": run_job.get("pendingItems"),
                "itemStatus": first_item.get("status"),
                "provider": first_item.get("provider"),
                "action": first_item.get("action"),
                "error": first_item.get("error") or first_item.get("providerError"),
                "imageUrl": extract_image_url(first_item),
            }
            run_ok = not run_response["error"] and bool(run_job) and str(run_job.get("status") or "") not in {"failed", "cancelled"}
            log.add(
                "job_run_live",
                run_ok,
                http_status=run_response["status"],
                fields=run_fields,
                reason="" if run_ok else run_response["error"] or str(run_fields.get("error") or "job run failed"),
            )

            if run_ok:
                export_payload = {
                    "style": selected_style,
                    "quality": "standard",
                    "selectedRows": [1],
                    "platforms": DEFAULT_PLATFORMS,
                    "format": "jpg",
                    "watermark": {"enabled": False},
                }
                exported = request_json(client, "POST", "/api/export", payload=export_payload)
                export_data = exported["data"] if isinstance(exported["data"], dict) else {}
                export_ok = not exported["error"] and int_or_zero(export_data.get("images")) > 0 and bool(export_data.get("download"))
                log.add(
                    "platform_export",
                    export_ok,
                    http_status=exported["status"],
                    fields=compact_fields(export_data, ["rows", "images", "platforms", "watermark", "download"]),
                    reason="" if export_ok else exported["error"] or "export did not return images/download",
                )
            else:
                log.add("platform_export", True, skipped=True, reason="skipped because live generation did not complete")
        else:
            log.add(
                "job_run_live",
                True,
                skipped=True,
                fields={"liveGenerate": live_generate, "requestedLimit": limit, "effectiveLimit": effective_limit},
                reason="not run by default; pass --live-generate --limit 1 to run one formal image",
            )
            log.add(
                "platform_export",
                True,
                skipped=True,
                reason="requires a successful live formal image in this smoke flow",
            )

    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    failures = [
        {"step": step["name"], "reason": step.get("reason", "")}
        for step in log.steps
        if not step.get("ok")
    ]
    finished_at = utc_now()
    return {
        "ok": not failures,
        "baseUrl": base_url,
        "mode": {
            "styleFirst": style_first,
            "liveGenerate": live_generate,
            "limit": limit,
            "effectiveLiveLimit": min(max(int_or_zero(limit), 0), 1),
            "menuFile": str(menu_file) if menu_file else "generated",
        },
        "selectedStyle": selected_style,
        "jobId": job_id,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "summary": {
            "passed": sum(1 for step in log.steps if step.get("ok") and step.get("status") != "skipped"),
            "failed": len(failures),
            "skipped": sum(1 for step in log.steps if step.get("status") == "skipped"),
            "total": len(log.steps),
        },
        "failures": failures,
        "steps": log.steps,
    }


def choose_style(plan: dict[str, Any] | None, *, style_first: bool) -> str:
    if not isinstance(plan, dict):
        return ""
    styles = plan.get("styles")
    first_style = ""
    if isinstance(styles, list) and styles:
        first = styles[0]
        if isinstance(first, dict):
            first_style = str(first.get("id") or "")
    if style_first:
        return first_style
    return str(plan.get("selectedStyle") or first_style or "")


def request_json(
    client: Any,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    method = method.upper()
    if method == "GET":
        result = safe_request(lambda: client.get(path, query=query))
    elif method == "POST":
        result = safe_request(lambda: client.post_json(path, payload or {}))
    else:
        raise ValueError(f"unsupported method: {method}")
    resp = result["response"]
    if resp is None:
        return {"status": None, "data": None, "error": result["error"]}
    data = parse_response_json(resp)
    parse_error = "" if data is not None else "invalid JSON response"
    error_message = response_error(resp, data) or parse_error
    return {"status": resp.status_code, "data": data, "error": error_message}


def safe_request(fn: Any) -> dict[str, Any]:
    try:
        return {"response": fn(), "error": ""}
    except Exception as exc:
        return {"response": None, "error": f"request failed: {exc}"}


def parse_response_json(resp: ClientResponse | None) -> Any:
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def response_error(resp: ClientResponse | None, data: Any) -> str:
    if resp is None:
        return "no response"
    if 200 <= resp.status_code < 300:
        return ""
    return http_reason(resp, data)


def http_reason(resp: ClientResponse, data: Any) -> str:
    detail = ""
    if isinstance(data, dict):
        detail = str(data.get("error") or data.get("message") or data.get("code") or "")
    if not detail:
        detail = resp.text.strip().replace("\n", " ")[:240]
    return f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"


def header_value(resp: ClientResponse, key: str) -> str:
    for header_key, value in resp.headers.items():
        if header_key.lower() == key.lower():
            return value
    return ""


def compact_fields(source: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: source.get(key) for key in keys if key in source}


def int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def first_job_item(job: dict[str, Any]) -> dict[str, Any]:
    items = job.get("items") if isinstance(job, dict) else []
    if isinstance(items, list) and items:
        item = items[0]
        if isinstance(item, dict):
            return item
    return {}


def extract_image_url(item: dict[str, Any]) -> str:
    candidates: list[Any] = [
        item.get("result"),
        item.get("generation"),
        item.get("generationResult"),
        item.get("payload"),
    ]
    for candidate in candidates:
        found = find_url(candidate)
        if found:
            return found
    return ""


def find_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("url", "imageUrl", "imageURL", "download", "path"):
            item = value.get(key)
            if isinstance(item, str) and item:
                return item
        for key in ("candidate", "payload", "row", "generation", "result", "generationResult"):
            found = find_url(value.get(key))
            if found:
                return found
        nested = value.get("candidates")
        if isinstance(nested, list):
            for item in nested:
                found = find_url(item)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = find_url(item)
            if found:
                return found
    return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run V4 product acceptance smoke flow.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8790", help="Local or deployed waimai-image-tool base URL.")
    parser.add_argument("--menu-file", help="Excel menu file to upload. If omitted, a small smoke menu is generated.")
    parser.add_argument("--style-first", action="store_true", help="Use the first returned style card instead of selectedStyle.")
    parser.add_argument("--limit", type=int, default=0, help="Formal live generation limit. Kept at 0 unless --live-generate is set.")
    parser.add_argument("--live-generate", dest="live_generate", action="store_true", help="Actually run one formal image when --limit is greater than 0.")
    parser.add_argument("--no-live-generate", dest="live_generate", action="store_false", help="Dry-run only; never call /api/jobs/<id>/run.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds.")
    parser.set_defaults(live_generate=False)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = UrllibProductClient(args.base_url, timeout=args.timeout)
    report = run_product_flow(
        client,
        menu_file=args.menu_file,
        base_url=args.base_url,
        style_first=args.style_first,
        limit=args.limit,
        live_generate=args.live_generate,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
