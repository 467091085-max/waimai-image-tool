from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request
import zipfile


STYLE_IDS = [f"style-{index}" for index in range(1, 7)]
DEFAULT_PLATFORMS = ["meituan", "taobao", "jd"]
DEFAULT_LOCAL_URL = "http://127.0.0.1:8790"
DEFAULT_RENDER_URL = "https://waimai-image-tool-1.onrender.com"
DEFAULT_REPORT_DIR = Path("data") / "exports" / "acceptance"
LIVE_ENV_VAR = "WAIMAI_ACCEPTANCE_LIVE"
GALLERY_UPLOAD_STATUS_PATH = "/api/admin/gallery-upload/status"
MENU_ROWS = [
    ["分类", "菜品名", "价格", "类型", "套餐内容/规格", "备注"],
    ["热销", "老长沙辣椒炒肉盖码饭", "19.8", "单品", "", "smoke"],
    ["热销", "小炒黄牛肉盖码饭", "25.8", "单品", "", "smoke"],
    ["套餐", "辣椒炒肉+茄子肉末盖码饭", "24.8", "套餐/组合", "辣椒炒肉；茄子肉末；米饭", "smoke"],
]
MOCK_RESULT_MARKERS = {
    "local-demo",
    "localfallback",
    "generated-local",
    "mock",
    "placeholder",
    "demo_store",
}
SEED_RESULT_MARKERS = {
    "seed_",
}


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
        self.opener = request.build_opener(request.ProxyHandler({}))

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
            with self.opener.open(req, timeout=self.timeout) as resp:
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


def create_default_menu_file(directory: str | Path | None = None, suffix: str = ".xlsx") -> Path:
    root = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="waimai_product_menu_"))
    root.mkdir(parents=True, exist_ok=True)
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    if normalized_suffix not in {".xls", ".xlsx"}:
        raise ValueError(f"unsupported smoke menu suffix: {suffix}")
    path = root / f"product_smoke_menu{normalized_suffix}"
    if normalized_suffix == ".xls":
        return create_default_xls_menu(path)
    return create_default_xlsx_menu(path)


def create_default_xlsx_menu(path: Path) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "菜单"
    for row in MENU_ROWS:
        sheet.append(row)
    workbook.save(path)
    return path


def create_default_xls_menu(path: Path) -> Path:
    try:
        import xlwt  # type: ignore[import-untyped]
    except Exception as exc:
        raise RuntimeError("creating .xls smoke menus requires xlwt; install requirements.txt or pass --xls-menu-file") from exc

    workbook = xlwt.Workbook(encoding="utf-8")
    sheet = workbook.add_sheet("菜单")
    for row_index, row in enumerate(MENU_ROWS):
        for col_index, value in enumerate(row):
            sheet.write(row_index, col_index, value)
    workbook.save(str(path))
    return path


def run_product_flow(
    client: Any,
    *,
    menu_file: str | Path | None = None,
    xls_menu_file: str | Path | None = None,
    base_url: str = "",
    style_first: bool = False,
    limit: int = 0,
    live_generate: bool = False,
    materialize_free_samples: bool = False,
    billing_check: bool = True,
) -> dict[str, Any]:
    started_at = utc_now()
    log = StepLog()
    temp_dir = tempfile.TemporaryDirectory(prefix="waimai_product_acceptance_")
    menu_uploads = prepare_menu_uploads(temp_dir.name, menu_file=menu_file, xls_menu_file=xls_menu_file)

    selected_style = ""
    plan: dict[str, Any] | None = None
    job_id = ""
    provider_configured = False
    provider_ready_for_live = False
    provider_fields: dict[str, Any] = {}
    selected_rows: list[int] = []
    selected_row_details: list[dict[str, Any]] = []
    red_flags: list[dict[str, str]] = []
    export_artifacts: list[dict[str, Any]] = []
    upload_results: dict[str, dict[str, Any]] = {}

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
        provider_configured = bool(tencent_data.get("configured"))
        provider_ready_for_live = provider_configured and bool(tencent_data.get("cosReady"))
        provider_fields = compact_fields(
            tencent_data,
            [
                "provider",
                "configured",
                "cosReady",
                "region",
                "styleBackgroundsLive",
                "localFallbackAllowed",
                "fallbackProvider",
                "providerStatus",
                "provider_error",
            ],
        )
        ok = not tencent_status["error"] and "provider" in tencent_data and "configured" in tencent_data
        log.add(
            "tencent_status",
            ok,
            http_status=tencent_status["status"],
            fields=provider_fields,
            reason="" if ok else tencent_status["error"] or "missing provider/configured fields",
        )

        if is_render_like_url(base_url):
            log.add(
                "render_provider_env",
                provider_ready_for_live,
                skipped=not provider_ready_for_live,
                fields={
                    **provider_fields,
                    "missing": tencent_data.get("missing") if isinstance(tencent_data.get("missing"), list) else [],
                },
                reason="" if provider_ready_for_live else "Render Tencent/COS env is not fully configured; live model gates are skipped until env is ready",
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
            fields=compact_fields(
                library_data,
                [
                    "total",
                    "reusable",
                    "stores",
                    "styles",
                    "sources",
                    "externalDirs",
                    "remoteIndex",
                    "remoteImages",
                    "indexImages",
                    "indexSource",
                    "indexError",
                ],
            ),
            reason="" if library_ok else library_status["error"] or "library has no reusable product images",
        )

        if is_render_like_url(base_url):
            remote_images = int_or_zero(library_data.get("remoteImages")) or int_or_zero(library_data.get("indexImages"))
            real_gallery_ok = (
                not library_status["error"]
                and library_data.get("remoteIndex") is True
                and remote_images > 0
            )
            log.add(
                "real_gallery_runtime",
                real_gallery_ok,
                http_status=library_status["status"],
                fields=compact_fields(
                    library_data,
                    [
                        "remoteIndex",
                        "remoteImages",
                        "indexImages",
                        "indexSource",
                        "sources",
                        "externalDirs",
                        "total",
                        "styles",
                    ],
                ),
                reason="" if real_gallery_ok else "Render must read the COS real gallery index; internal seed images are not production evidence",
            )

            upload_env = request_json(client, "GET", GALLERY_UPLOAD_STATUS_PATH)
            upload_env_data = upload_env["data"] if isinstance(upload_env["data"], dict) else {}
            upload_env_ok = not upload_env["error"] and upload_env_data.get("enabled") is True
            upload_env_reachable = not upload_env["error"]
            log.add(
                "gallery_upload_env",
                upload_env_reachable,
                skipped=upload_env_reachable and not upload_env_ok,
                http_status=upload_env["status"],
                fields=compact_fields(
                    upload_env_data,
                    [
                        "enabled",
                        "disabledReason",
                        "cosReady",
                        "bucket",
                        "region",
                        "prefix",
                        "indexUrl",
                        "configuredIndexUrl",
                        "runtimeIndexActive",
                    ],
                ),
                reason="" if upload_env_ok else upload_env["error"] or "Render gallery upload proxy/env is not enabled; upload-live checks are skipped",
            )

        main_upload_ok = False
        main_upload_fields: dict[str, Any] = {}
        main_upload_reason = ""
        for upload_spec in menu_uploads:
            step_name = f"upload_menu:{upload_spec['format']}"
            path = upload_spec.get("path")
            source = str(upload_spec.get("source") or "")
            if path is None:
                log.add(
                    step_name,
                    True,
                    skipped=True,
                    fields={"source": source, "main": upload_spec.get("main")},
                    reason=str(upload_spec.get("error") or "menu fixture unavailable"),
                )
                continue
            menu_path = Path(path)
            if not menu_path.exists():
                upload_ok = False
                upload_status = None
                menu_info: dict[str, Any] = {}
                upload_reason = "menu file does not exist"
            else:
                upload = safe_request(lambda menu_path=menu_path: client.post_file("/api/upload-menu", menu_path, "file"))
                upload_data = parse_response_json(upload["response"]) if upload["response"] is not None else None
                upload_reason = upload["error"] or response_error(upload["response"], upload_data)
                upload_status = upload["response"].status_code if upload["response"] is not None else None
                menu_info = upload_data.get("menu") if isinstance(upload_data, dict) and isinstance(upload_data.get("menu"), dict) else {}
                upload_ok = (
                    not upload_reason
                    and isinstance(upload_data, dict)
                    and upload_data.get("ok") is True
                    and int_or_zero(menu_info.get("count")) > 0
                )
            upload_fields = {
                "menuFile": str(menu_path),
                "source": source,
                "main": upload_spec.get("main"),
                "store": menu_info.get("store"),
                "count": menu_info.get("count"),
                "kindCounts": menu_info.get("kindCounts"),
            }
            upload_results[str(upload_spec["format"])] = {"ok": upload_ok, "fields": upload_fields, "reason": upload_reason}
            log.add(
                step_name,
                upload_ok,
                http_status=upload_status,
                fields=upload_fields,
                reason="" if upload_ok else upload_reason or "upload response did not include ok=true and a non-empty menu",
            )
            if upload_spec.get("main"):
                main_upload_ok = upload_ok
                main_upload_fields = upload_fields
                main_upload_reason = upload_reason

        log.add(
            "upload_menu",
            main_upload_ok,
            fields={**main_upload_fields, "formatsChecked": sorted(upload_results)},
            reason="" if main_upload_ok else main_upload_reason or "main menu upload failed",
        )

        plan_response = request_json(client, "GET", "/api/plan")
        if isinstance(plan_response["data"], dict):
            plan = plan_response["data"]
        styles = plan.get("styles") if isinstance(plan, dict) else []
        results = plan.get("results") if isinstance(plan, dict) else []
        plan_ok = not plan_response["error"] and isinstance(styles, list) and isinstance(results, list) and len(results) > 0
        pricing = plan.get("pricing") if isinstance(plan, dict) and isinstance(plan.get("pricing"), dict) else {}
        quote = plan.get("quote") if isinstance(plan, dict) and isinstance(plan.get("quote"), dict) else {}
        custom_edit_points = int_or_zero(pricing.get("customEditPoints"))
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

        single_modify_contract_ok = (
            plan_ok
            and custom_edit_points > 0
            and int_or_zero(pricing.get("previewFreeImages")) >= 6
            and "customEditCash" in pricing
        )
        log.add(
            "single_modify_contract",
            single_modify_contract_ok,
            fields=compact_fields(pricing, ["previewFreeImages", "customEditPoints", "customEditCash", "freeReworkQuota"]),
            reason="" if single_modify_contract_ok else "pricing must expose six free samples and a custom edit price",
        )

        menu_counts = kind_counts_from_results(results if isinstance(results, list) else [])
        points_value = points_total_from_plan(plan)
        total_count = int_or_zero((plan or {}).get("summary", {}).get("total") if isinstance((plan or {}).get("summary"), dict) else 0) or len(results if isinstance(results, list) else [])
        menu_summary_ok = total_count > 0 and menu_counts["single"] > 0 and menu_counts["combo"] > 0 and points_value is not None
        log.add(
            "menu_summary",
            menu_summary_ok,
            fields={
                "total": total_count,
                "single": menu_counts["single"],
                "combo": menu_counts["combo"],
                "other": menu_counts["other"],
                "points": points_value,
                "summary": (plan or {}).get("summary") if isinstance(plan, dict) else {},
            },
            reason="" if menu_summary_ok else "plan must include single items, combo items, total count, and points",
        )

        preview_contract = result_preview_contract(results if isinstance(results, list) else [])
        preview_contract_ok = (
            plan_ok
            and preview_contract["total"] > 0
            and preview_contract["single"] > 0
            and preview_contract["combo"] > 0
            and preview_contract["rowsWithStatus"] == preview_contract["total"]
            and preview_contract["rowsWithAction"] == preview_contract["total"]
        )
        log.add(
            "result_preview_contract",
            preview_contract_ok,
            fields=preview_contract,
            reason="" if preview_contract_ok else "formal preview rows must be grouped and expose status/action fields",
        )

        if isinstance(styles, list):
            present = [str(style.get("id")) for style in styles if isinstance(style, dict) and style.get("id")]
        else:
            present = []
        missing_styles = [style_id for style_id in STYLE_IDS if style_id not in set(present)]
        style_cards = [
            compact_fields(style, ["id", "name", "source", "count", "needsGeneratedBackground", "backgroundJob"])
            for style in styles
            if isinstance(style, dict)
        ] if isinstance(styles, list) else []
        style_catalog_ok = len(present) >= 6
        log.add(
            "style_catalog",
            style_catalog_ok,
            fields={"expectedCount": 6, "present": present, "fixedIds": STYLE_IDS, "fixedMissing": missing_styles, "cards": style_cards},
            reason="" if style_catalog_ok else f"expected at least 6 style cards, got {len(present)}",
        )

        default_selected_style = choose_style(plan, style_first=style_first)
        selected_style = choose_live_generation_style(results if isinstance(results, list) else [], present, default_selected_style) if live_generate else default_selected_style
        if selected_style:
            log.add(
                "style_selection",
                True,
                fields={"selectedStyle": selected_style, "styleFirst": style_first, "defaultSelectedStyle": default_selected_style, "liveAutoSelected": bool(live_generate and selected_style != default_selected_style)},
            )
        else:
            log.add("style_selection", False, fields={"styleFirst": style_first}, reason="no selectable style in plan")

        selected_preview_sample_count = 0
        preview_style_ids = present[:6] if len(present) >= 6 else STYLE_IDS
        for style_id in preview_style_ids:
            preview = request_json(client, "GET", "/api/style-preview", query={"style": style_id}, retries=2)
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
                and len(samples) >= 6
                and int_or_zero(preview_data.get("previewFreeImages")) >= 6
            )
            if style_id == selected_style and isinstance(samples, list):
                selected_preview_sample_count = len(samples)
                preview_risk = generation_mock_indicators({"samples": samples}, provider_configured=provider_configured)
                if preview_risk:
                    red_flags.append({"step": f"style_preview:{style_id}", "reason": "; ".join(preview_risk)})
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
                reason="" if preview_ok else preview["error"] or "style preview must expose 6 free samples for the style",
            )

        log.add(
            "free_sample_slots",
            selected_preview_sample_count >= 6,
            fields={"selectedStyle": selected_style, "sampleCount": selected_preview_sample_count, "expected": 6},
            reason="" if selected_preview_sample_count >= 6 else "selected style did not expose 6 free sample slots",
        )

        if materialize_free_samples and selected_style:
            generated_samples = []
            for index in range(6):
                sample_response = request_json(client, "GET", "/api/style-preview-sample", query={"style": selected_style, "index": index})
                sample_data = sample_response["data"] if isinstance(sample_response["data"], dict) else {}
                generated_samples.append(
                    {
                        "index": index,
                        "status": sample_response["status"],
                        "error": sample_response["error"],
                        "job": compact_fields(sample_data.get("sample", {}).get("job") if isinstance(sample_data.get("sample"), dict) else {}, ["status", "provider", "action", "error"]),
                        "hasCandidate": bool(find_url(sample_data)),
                    }
                )
            sample_errors = [item for item in generated_samples if item["error"]]
            log.add(
                "free_sample_generation",
                not sample_errors and len(generated_samples) == 6,
                fields={"selectedStyle": selected_style, "samples": generated_samples},
                reason="" if not sample_errors else "; ".join(str(item["error"]) for item in sample_errors[:2]),
            )
        else:
            log.add(
                "free_sample_generation",
                True,
                skipped=True,
                fields={"selectedStyle": selected_style, "materializeFreeSamples": materialize_free_samples},
                reason="not run by default; pass --generate-free-samples to call the six free sample generation endpoints",
            )

        if selected_style:
            selected_rows, selected_row_details = choose_generation_rows(results if isinstance(results, list) else [], selected_style)
            job_request = {
                "style": selected_style,
                "quality": "standard",
                "selectedRows": selected_rows,
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
                    "selectedRows": selected_rows,
                    "selectedRowDetails": selected_row_details,
                    "progress": job.get("progress"),
                    "poll": job_payload.get("poll"),
                },
                reason="" if job_ok else created["error"] or "job was not created with at least one item",
            )
        else:
            log.add("job_create", False, reason="skipped because no style was selected", skipped=True)

        add_billing_acceptance_steps(
            log,
            client,
            enabled=billing_check,
            points_value=points_value,
            custom_edit_points=custom_edit_points,
            selected_style=selected_style,
            selected_row_details=selected_row_details,
        )

        effective_limit = min(max(int_or_zero(limit), 0), max(len(selected_rows), 1), 1)
        if live_generate and job_id and effective_limit > 0 and provider_ready_for_live:
            run_payload = {
                "limit": effective_limit,
                "paid": True,
                "orderId": f"smoke_product_flow_{int(time.time())}",
            }
            run_response = request_json(client, "POST", f"/api/jobs/{job_id}/run", payload=run_payload)
            run_data = run_response["data"] if isinstance(run_response["data"], dict) else {}
            run_job = run_data.get("job") if isinstance(run_data.get("job"), dict) else {}
            if not run_response["error"] and job_id:
                run_job = poll_job_for_formal_result(client, job_id, run_job, provider_configured=provider_configured)
            first_item = formal_job_item(run_job, provider_configured=provider_configured)
            formal_evidence = formal_result_evidence(first_item, provider_configured=provider_configured)
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
                "imageUrl": formal_evidence.get("imageUrl"),
                "authenticity": formal_evidence,
            }
            run_ok = (
                not run_response["error"]
                and bool(run_job)
                and provider_ready_for_live
                and int_or_zero(run_job.get("completedItems")) >= 1
                and str(run_job.get("status") or "") not in {"failed", "cancelled"}
                and bool(formal_evidence.get("imageUrl"))
                and not formal_evidence.get("mockOrSeed")
            )
            if not provider_ready_for_live:
                run_reason = "provider/COS env is not ready; formal generation is blocked before model execution"
            elif formal_evidence.get("mockOrSeed"):
                run_reason = "provider configured=true but formal result looks like seed/mock/local fallback"
                red_flags.append({"step": "job_run_live", "reason": run_reason})
            elif not formal_evidence.get("imageUrl"):
                run_reason = "formal generation did not return a non-empty image URL/path"
            else:
                run_reason = run_response["error"] or str(run_fields.get("error") or "job run failed")
            log.add(
                "job_run_live",
                run_ok,
                http_status=run_response["status"],
                fields=run_fields,
                reason="" if run_ok else run_reason,
            )

        else:
            if live_generate and not provider_ready_for_live:
                skip_reason = "Render Tencent/COS env is not fully configured; skipped live provider run without creating model cost"
            elif not live_generate:
                skip_reason = "not run by default; pass --live-generate --limit 1 to run one formal image"
            else:
                skip_reason = "no runnable job or limit for live formal image"
            log.add(
                "job_run_live",
                True,
                skipped=True,
                fields={
                    "liveGenerate": live_generate,
                    "requestedLimit": limit,
                    "effectiveLimit": effective_limit,
                    "providerReadyForLive": provider_ready_for_live,
                },
                reason=skip_reason,
            )

        single_export_rows = single_export_row_selection(results if isinstance(results, list) else [])
        single_export_result = run_export_check(
            client,
            selected_style=selected_style,
            scope="selected",
            selected_rows=single_export_rows,
            live_generate=live_generate,
        )
        export_artifacts.append(single_export_result.get("artifact", {}))
        log.add(
            "single_image_export",
            bool(single_export_result["ok"]),
            skipped=bool(single_export_result.get("skipped")),
            http_status=single_export_result.get("httpStatus"),
            fields=single_export_result.get("fields"),
            reason=str(single_export_result.get("reason") or ""),
        )

        for scope in ("all", "single", "combo"):
            export_result = run_export_check(
                client,
                selected_style=selected_style,
                scope=scope,
                selected_rows=[],
                live_generate=live_generate,
            )
            export_artifacts.append(export_result.get("artifact", {}))
            log.add(
                f"platform_export:{scope}",
                bool(export_result["ok"]),
                skipped=bool(export_result.get("skipped")),
                http_status=export_result.get("httpStatus"),
                fields=export_result.get("fields"),
                reason=str(export_result.get("reason") or ""),
            )

    finally:
        temp_dir.cleanup()

    failures = [
        {"step": step["name"], "reason": step.get("reason", "")}
        for step in log.steps
        if not step.get("ok")
    ]
    skips = [
        {"step": step["name"], "reason": step.get("reason", "")}
        for step in log.steps
        if step.get("status") == "skipped"
    ]
    finished_at = utc_now()
    return {
        "ok": not failures and not red_flags,
        "baseUrl": base_url,
        "mode": {
            "styleFirst": style_first,
            "liveGenerate": live_generate,
            "materializeFreeSamples": materialize_free_samples,
            "billingCheck": billing_check,
            "limit": limit,
            "effectiveLiveLimit": min(max(int_or_zero(limit), 0), 1),
            "menuFile": str(menu_file) if menu_file else "generated",
            "xlsMenuFile": str(xls_menu_file) if xls_menu_file else "generated",
        },
        "provider": provider_fields,
        "selectedStyle": selected_style,
        "selectedRows": selected_rows,
        "selectedRowDetails": selected_row_details,
        "jobId": job_id,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "summary": {
            "passed": sum(1 for step in log.steps if step.get("ok") and step.get("status") != "skipped"),
            "failed": len(failures),
            "skipped": len(skips),
            "total": len(log.steps),
        },
        "skips": skips,
        "redFlags": red_flags,
        "exports": [artifact for artifact in export_artifacts if artifact],
        "failures": failures,
        "steps": log.steps,
    }


def normalize_base_url(value: str | None) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_LOCAL_URL, "empty base URL resolved to local default"
    lowered = raw.lower().strip("/")
    if lowered in {"local", "localhost", "dev"}:
        return DEFAULT_LOCAL_URL, f"{raw!r} resolved to {DEFAULT_LOCAL_URL}"
    if lowered in {"render", "prod", "production"}:
        return DEFAULT_RENDER_URL, f"{raw!r} resolved to {DEFAULT_RENDER_URL}"
    parsed = parse.urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw.rstrip("/"), ""
    if raw.startswith("127.") or raw.startswith("localhost") or raw.startswith("[::1]"):
        return f"http://{raw}".rstrip("/"), "missing scheme resolved with http:// for local host"
    if "." in raw and " " not in raw:
        return f"https://{raw}".rstrip("/"), "missing scheme resolved with https:// for deployed host"
    raise ValueError(
        f"Invalid --base-url {raw!r}. Use 'local', 'render', or a full URL such as {DEFAULT_LOCAL_URL}."
    )


def is_render_like_url(value: str | None) -> bool:
    parsed = parse.urlparse(str(value or ""))
    host = (parsed.netloc or parsed.path or "").lower()
    return "onrender.com" in host or host == parse.urlparse(DEFAULT_RENDER_URL).netloc


def env_flag_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def prepare_menu_uploads(
    directory: str | Path,
    *,
    menu_file: str | Path | None = None,
    xls_menu_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    main_path = Path(menu_file) if menu_file else create_default_menu_file(root, ".xlsx")
    main_format = main_path.suffix.lower().lstrip(".") or "xlsx"
    if main_format not in {"xls", "xlsx"}:
        main_format = "xlsx"
    main_source = "provided" if menu_file else "generated"

    specs: list[dict[str, Any]] = []

    def generated_or_error(fmt: str) -> dict[str, Any]:
        try:
            path = create_default_menu_file(root, f".{fmt}")
            return {"format": fmt, "path": path, "source": "generated", "main": False, "error": ""}
        except Exception as exc:
            return {"format": fmt, "path": None, "source": "generated", "main": False, "error": str(exc)}

    if main_format == "xls":
        specs.append(generated_or_error("xlsx"))
        specs.append({"format": "xls", "path": main_path, "source": main_source, "main": True, "error": ""})
    else:
        if xls_menu_file:
            specs.append({"format": "xls", "path": Path(xls_menu_file), "source": "provided", "main": False, "error": ""})
        else:
            specs.append(generated_or_error("xls"))
        specs.append({"format": "xlsx", "path": main_path, "source": main_source, "main": True, "error": ""})
    return specs


def kind_counts_from_results(results: list[Any]) -> dict[str, int]:
    counts = {"single": 0, "combo": 0, "other": 0}
    for row in results:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").lower()
        name = str(row.get("name") or "").lower()
        if "套餐" in kind or "组合" in kind or "combo" in kind or "套餐" in name:
            counts["combo"] += 1
        elif "单品" in kind or "single" in kind:
            counts["single"] += 1
        else:
            counts["other"] += 1
    return counts


def result_preview_contract(results: list[Any]) -> dict[str, Any]:
    counts = kind_counts_from_results(results)
    rows_with_status = 0
    rows_with_action = 0
    rows_with_candidate = 0
    first_image_url = ""
    for row in results:
        if not isinstance(row, dict):
            continue
        if str(row.get("publicStatus") or row.get("status") or "").strip():
            rows_with_status += 1
        if str(row.get("backgroundAction") or "").strip():
            rows_with_action += 1
        image_url = find_url(row.get("candidates"))
        if image_url:
            rows_with_candidate += 1
            if not first_image_url:
                first_image_url = image_url
    return {
        "total": len([row for row in results if isinstance(row, dict)]),
        "single": counts["single"],
        "combo": counts["combo"],
        "other": counts["other"],
        "rowsWithStatus": rows_with_status,
        "rowsWithAction": rows_with_action,
        "rowsWithPreviewCandidate": rows_with_candidate,
        "firstImageUrl": first_image_url,
    }


def points_total_from_plan(plan: dict[str, Any] | None) -> int | None:
    if not isinstance(plan, dict):
        return None
    for container_key in ("summary", "quote"):
        container = plan.get(container_key)
        if isinstance(container, dict) and container.get("points") is not None:
            return int_or_zero(container.get("points"))
    results = plan.get("results")
    if isinstance(results, list):
        return sum(int_or_zero(row.get("points")) for row in results if isinstance(row, dict))
    return None


def single_export_row_selection(results: list[Any]) -> list[int]:
    for index, row in enumerate(results, start=1):
        if isinstance(row, dict) and str(row.get("kind") or "") in {"单品", "single"}:
            return [index]
    return [1] if results else []


def add_billing_acceptance_steps(
    log: StepLog,
    client: Any,
    *,
    enabled: bool,
    points_value: int | None,
    custom_edit_points: int,
    selected_style: str,
    selected_row_details: list[dict[str, Any]],
) -> None:
    if not enabled:
        skipped_fields = {"billingCheck": False}
        for step_name in ("billing_recharge", "billing_formal_debit", "single_modify_debit"):
            log.add(step_name, True, skipped=True, fields=skipped_fields, reason="billing check disabled")
        return
    if points_value is None or custom_edit_points <= 0:
        skipped_fields = {"points": points_value, "customEditPoints": custom_edit_points}
        for step_name in ("billing_recharge", "billing_formal_debit", "single_modify_debit"):
            log.add(step_name, True, skipped=True, fields=skipped_fields, reason="billing check skipped because pricing/points were unavailable")
        return

    user_id = f"smoke-render-qa-{uuid.uuid4().hex[:10]}"
    run_id = uuid.uuid4().hex[:12]
    formal_points = max(1, int_or_zero(points_value))
    edit_points = max(1, int_or_zero(custom_edit_points))
    recharge_points = max(100, formal_points + edit_points)
    row_detail = selected_row_details[0] if selected_row_details else {}

    recharge_payload = {
        "userId": user_id,
        "orderId": f"smoke-recharge-{run_id}",
        "points": recharge_points,
    }
    recharge = request_json(client, "POST", "/api/recharge", payload=recharge_payload)
    recharge_data = recharge["data"] if isinstance(recharge["data"], dict) else {}
    recharge_balance = account_balance(recharge_data)
    recharge_ok = not recharge["error"] and recharge_data.get("ok") is True and recharge_balance >= recharge_points
    log.add(
        "billing_recharge",
        recharge_ok,
        http_status=recharge["status"],
        fields={
            "userId": user_id,
            "points": recharge_points,
            "balance": recharge_balance,
            **transaction_fields(recharge_data),
        },
        reason="" if recharge_ok else recharge["error"] or "recharge response did not credit the smoke account",
    )
    if not recharge_ok:
        for step_name in ("billing_formal_debit", "single_modify_debit"):
            log.add(step_name, True, skipped=True, fields={"userId": user_id}, reason="billing recharge failed")
        return

    formal_payload = {
        "userId": user_id,
        "orderId": f"smoke-formal-debit-{run_id}",
        "points": formal_points,
        "description": "正式出图验收扣费",
        "metadata": {"style": selected_style, "source": "v6-render-acceptance"},
    }
    formal = request_json(client, "POST", "/api/debit", payload=formal_payload)
    formal_data = formal["data"] if isinstance(formal["data"], dict) else {}
    formal_balance = account_balance(formal_data)
    formal_ok = (
        not formal["error"]
        and formal_data.get("ok") is True
        and transaction_points(formal_data) == formal_points
        and formal_balance == recharge_balance - formal_points
    )
    log.add(
        "billing_formal_debit",
        formal_ok,
        http_status=formal["status"],
        fields={
            "userId": user_id,
            "points": formal_points,
            "balanceBefore": recharge_balance,
            "balance": formal_balance,
            **transaction_fields(formal_data),
        },
        reason="" if formal_ok else formal["error"] or "formal generation debit did not reduce balance by expected points",
    )
    if not formal_ok:
        log.add("single_modify_debit", True, skipped=True, fields={"userId": user_id}, reason="formal debit failed")
        return

    edit_payload = {
        "userId": user_id,
        "orderId": f"smoke-custom-edit-{run_id}",
        "points": edit_points,
        "description": "自定义修改验收扣费",
        "metadata": {
            "style": selected_style,
            "row": row_detail.get("row") or row_detail.get("index") or 1,
            "dish": row_detail.get("name") or "smoke-dish",
            "source": "v6-render-acceptance",
        },
    }
    edit = request_json(client, "POST", "/api/debit", payload=edit_payload)
    edit_data = edit["data"] if isinstance(edit["data"], dict) else {}
    edit_balance = account_balance(edit_data)
    edit_ok = (
        not edit["error"]
        and edit_data.get("ok") is True
        and transaction_points(edit_data) == edit_points
        and edit_balance == formal_balance - edit_points
    )
    log.add(
        "single_modify_debit",
        edit_ok,
        http_status=edit["status"],
        fields={
            "userId": user_id,
            "points": edit_points,
            "balanceBefore": formal_balance,
            "balance": edit_balance,
            "row": edit_payload["metadata"]["row"],
            "dish": edit_payload["metadata"]["dish"],
            **transaction_fields(edit_data),
        },
        reason="" if edit_ok else edit["error"] or "custom edit debit did not reduce balance by expected points",
    )


def account_balance(data: dict[str, Any]) -> int:
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    return int_or_zero(account.get("balance") if account else transaction.get("balance"))


def transaction_points(data: dict[str, Any]) -> int:
    transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    return int_or_zero(transaction.get("points"))


def transaction_fields(data: dict[str, Any]) -> dict[str, Any]:
    transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    return compact_fields(transaction, ["orderId", "eventType", "description", "idempotent"])


def choose_generation_rows(results: list[Any], selected_style: str) -> tuple[list[int], list[dict[str, Any]]]:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, row in enumerate(results, start=1):
        if not isinstance(row, dict):
            continue
        action = str(row.get("backgroundAction") or row.get("publicStatus") or "")
        kind = str(row.get("kind") or "")
        row_candidates = row.get("candidates") if isinstance(row.get("candidates"), list) else []
        same_style = any(isinstance(candidate, dict) and candidate.get("styleId") == selected_style for candidate in row_candidates)
        score = 0
        is_combo = "套餐" in kind or "组合" in kind
        if not is_combo:
            score += 100
        else:
            score += 10
        if row_needs_model_for_style(row, selected_style):
            score += 80
        if action not in {"背景一致，直接复用", "直接可用", "已生成"}:
            score += 30
        if not row_candidates:
            score += 30
        if not same_style:
            score += 15
        candidates.append((score, index, row))
    if not candidates:
        return [1], []
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    _score, index, row = candidates[0]
    detail = {
        "index": index,
        "row": row.get("row"),
        "name": row.get("name"),
        "kind": row.get("kind"),
        "backgroundAction": row.get("backgroundAction"),
    }
    return [index], [detail]


def run_export_check(
    client: Any,
    *,
    selected_style: str,
    scope: str,
    selected_rows: list[int],
    live_generate: bool,
) -> dict[str, Any]:
    if not selected_style:
        return {"ok": True, "reason": "skipped because no style was selected", "skipped": True, "fields": {"scope": scope}}
    export_payload = {
        "style": selected_style,
        "quality": "standard",
        "scope": scope,
        "selectedRows": selected_rows,
        "platforms": DEFAULT_PLATFORMS,
        "format": "jpg",
        "watermark": {"enabled": False},
    }
    exported = request_json(client, "POST", "/api/export", payload=export_payload)
    export_data = exported["data"] if isinstance(exported["data"], dict) else {}
    export_fields = {
        **compact_fields(export_data, ["rows", "images", "platforms", "watermark", "download", "extraPlatformPoints"]),
        "scope": scope,
    }
    if exported["error"]:
        skippable = exported["status"] == 400 and "可导出" in str(exported["error"]) and (not live_generate or scope == "combo")
        return {
            "ok": bool(skippable),
            "skipped": bool(skippable),
            "httpStatus": exported["status"],
            "fields": export_fields,
            "reason": (
                "live smoke only materialized one image and no combo image was available for this ZIP"
                if live_generate and scope == "combo"
                else "dry-run has no formal image for this export scope; run live smoke to validate this ZIP"
                if skippable
                else exported["error"]
            ),
        }
    zip_check = validate_export_download(client, str(export_data.get("download") or ""))
    export_ok = int_or_zero(export_data.get("images")) > 0 and bool(export_data.get("download")) and zip_check["ok"]
    return {
        "ok": export_ok,
        "httpStatus": exported["status"],
        "fields": {**export_fields, **zip_check["fields"]},
        "artifact": {"scope": scope, **export_fields, **zip_check["fields"]},
        "reason": "" if export_ok else zip_check["reason"] or "export did not return a valid non-empty ZIP",
    }


def validate_export_download(client: Any, download_url: str) -> dict[str, Any]:
    if not download_url:
        return {"ok": False, "fields": {}, "reason": "export response did not include download URL"}
    result = safe_request(lambda: client.get(download_url))
    resp = result["response"]
    if resp is None:
        return {"ok": False, "fields": {"download": download_url}, "reason": result["error"]}
    fields: dict[str, Any] = {"downloadStatus": resp.status_code, "zipBytes": len(resp.body)}
    if not (200 <= resp.status_code < 300):
        return {"ok": False, "fields": fields, "reason": http_reason(resp, parse_response_json(resp))}
    try:
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(resp.body)) as zf:
            names = zf.namelist()
            image_count = sum(1 for name in names if name.startswith("images/") and not name.endswith("/"))
            fields.update(
                {
                    "zipEntries": len(names),
                    "zipImages": image_count,
                    "hasDeliveryReport": "delivery_report.xlsx" in names,
                }
            )
            ok = image_count > 0 and bool(fields["hasDeliveryReport"])
            return {"ok": ok, "fields": fields, "reason": "" if ok else "ZIP is missing images or delivery_report.xlsx"}
    except Exception as exc:
        return {"ok": False, "fields": fields, "reason": f"download is not a readable ZIP: {exc}"}


def formal_result_evidence(item: dict[str, Any], *, provider_configured: bool) -> dict[str, Any]:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
    generation_result = result.get("generationResult") if isinstance(result.get("generationResult"), dict) else {}
    candidate = (
        result.get("candidate")
        if isinstance(result.get("candidate"), dict)
        else generation.get("candidate")
        if isinstance(generation.get("candidate"), dict)
        else generation_result.get("candidate")
        if isinstance(generation_result.get("candidate"), dict)
        else {}
    )
    evidence = {
        "providerConfigured": provider_configured,
        "provider": first_non_empty(item.get("provider"), result.get("provider"), generation.get("provider"), generation_result.get("provider")),
        "action": first_non_empty(item.get("action"), result.get("action"), generation.get("action"), generation_result.get("action")),
        "status": first_non_empty(item.get("status"), result.get("status"), generation.get("status"), generation_result.get("status")),
        "source": first_non_empty(candidate.get("source"), generation.get("source"), generation_result.get("source")),
        "imageUrl": extract_image_url(item),
        "indicators": [],
        "mockOrSeed": False,
    }
    indicators = generation_mock_indicators(
        {
            "provider": evidence["provider"],
            "action": evidence["action"],
            "status": evidence["status"],
            "source": evidence["source"],
            "imageUrl": evidence["imageUrl"],
            "candidate": candidate,
        },
        provider_configured=provider_configured,
    )
    evidence["indicators"] = indicators
    evidence["mockOrSeed"] = bool(indicators)
    return evidence


def generation_mock_indicators(value: Any, *, provider_configured: bool) -> list[str]:
    if not provider_configured:
        return []
    flattened = [item.lower() for item in flatten_strings(value)]
    indicators = []
    for marker in sorted(MOCK_RESULT_MARKERS):
        if any(marker in item for item in flattened):
            indicators.append(f"mock/local marker: {marker}")
    for marker in sorted(SEED_RESULT_MARKERS):
        if any(marker in item for item in flattened):
            indicators.append(f"seed marker: {marker}")
    return sorted(set(indicators))


def flatten_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(flatten_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(flatten_strings(item))
        return out
    if isinstance(value, str):
        return [value]
    return []


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


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


def row_needs_model_for_style(row: dict[str, Any], style_id: str) -> bool:
    action = str(row.get("backgroundAction") or row.get("publicStatus") or "")
    candidates = row.get("candidates") if isinstance(row.get("candidates"), list) else []
    same_style = any(isinstance(candidate, dict) and candidate.get("styleId") == style_id for candidate in candidates)
    if not candidates:
        return True
    if action in {"需要定制/生成", "智能补图", "需抠图换背景", "需去水印/重绘", "套餐组合生成"}:
        return True
    if action == "智能统一风格":
        return not same_style
    return not same_style and action not in {"背景一致，直接复用", "直接可用", "已生成"}


def choose_live_generation_style(results: list[Any], style_ids: list[str], fallback: str) -> str:
    usable_styles = [style_id for style_id in style_ids if style_id] or STYLE_IDS
    best_score = -1
    best_style = fallback or (usable_styles[0] if usable_styles else "")
    for style_id in usable_styles:
        score = 0
        for row in results:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            if "套餐" in kind or "组合" in kind:
                continue
            if not row_needs_model_for_style(row, style_id):
                continue
            action = str(row.get("backgroundAction") or row.get("publicStatus") or "")
            row_candidates = row.get("candidates") if isinstance(row.get("candidates"), list) else []
            score += 100
            if not row_candidates:
                score += 80
            if action in {"智能补图", "需要定制/生成", "需去水印/重绘"}:
                score += 60
            elif action in {"需抠图换背景", "智能统一风格"}:
                score += 40
        if score > best_score or (score == best_score and style_id == fallback):
            best_score = score
            best_style = style_id
    return best_style


def request_json(
    client: Any,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    method = method.upper()
    attempts = max(1, int(retries) + 1)
    last: dict[str, Any] = {"status": None, "data": None, "error": "request was not attempted"}
    for attempt in range(attempts):
        if method == "GET":
            result = safe_request(lambda: client.get(path, query=query))
        elif method == "POST":
            result = safe_request(lambda: client.post_json(path, payload or {}))
        else:
            raise ValueError(f"unsupported method: {method}")
        resp = result["response"]
        if resp is None:
            last = {"status": None, "data": None, "error": result["error"]}
        else:
            data = parse_response_json(resp)
            parse_error = "" if data is not None else "invalid JSON response"
            error_message = response_error(resp, data) or parse_error
            last = {"status": resp.status_code, "data": data, "error": error_message}
            if not error_message:
                return last
        if attempt < attempts - 1 and should_retry_response(last):
            time.sleep(1.5 * (attempt + 1))
            continue
        return last
    return last


def should_retry_response(result: dict[str, Any]) -> bool:
    status = result.get("status")
    if status is None:
        return True
    try:
        return int(status) >= 500
    except Exception:
        return False


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


def formal_job_item(job: dict[str, Any], *, provider_configured: bool) -> dict[str, Any]:
    items = job.get("items") if isinstance(job, dict) else []
    if isinstance(items, list):
        completed = []
        for item in items:
            if not isinstance(item, dict):
                continue
            evidence = formal_result_evidence(item, provider_configured=provider_configured)
            if evidence.get("imageUrl") and not evidence.get("mockOrSeed"):
                return item
            if str(item.get("status") or "").lower() in {"completed", "succeeded"}:
                completed.append(item)
        if completed:
            return completed[0]
    return first_job_item(job)


def poll_job_for_formal_result(client: Any, job_id: str, initial_job: dict[str, Any], *, provider_configured: bool) -> dict[str, Any]:
    job = initial_job if isinstance(initial_job, dict) else {}
    terminal_statuses = {"completed", "succeeded", "partial", "partially_failed", "failed", "cancelled", "refunded"}
    deadline = time.monotonic() + 210
    attempt = 0
    while True:
        item = formal_job_item(job, provider_configured=provider_configured)
        evidence = formal_result_evidence(item, provider_configured=provider_configured)
        job_status = str(job.get("status") or "").lower()
        item_status = str(item.get("status") or "").lower()
        has_model_result = bool(evidence.get("imageUrl")) and not evidence.get("mockOrSeed")
        if has_model_result and (item_status in {"completed", "succeeded"} or int_or_zero(job.get("completedItems")) >= 1 or job_status in terminal_statuses):
            return job
        if job_status in terminal_statuses:
            return job
        if time.monotonic() >= deadline:
            return job
        if attempt:
            time.sleep(3)
        refreshed = request_json(client, "GET", f"/api/jobs/{job_id}", retries=1)
        refreshed_data = refreshed["data"] if isinstance(refreshed["data"], dict) else {}
        refreshed_job = refreshed_data.get("job") if isinstance(refreshed_data.get("job"), dict) else {}
        if refreshed_job:
            job = refreshed_job
        attempt += 1


def extract_image_url(item: dict[str, Any]) -> str:
    found = find_url(item)
    if found:
        return found
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


def safe_report_name(base_url: str) -> str:
    parsed = parse.urlparse(base_url)
    host = parsed.netloc or parsed.path or "local"
    host = re.sub(r"[^A-Za-z0-9_.-]+", "_", host).strip("_") or "local"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"product_acceptance_{host}_{stamp}"


def write_acceptance_artifacts(
    report: dict[str, Any],
    *,
    report_dir: str | Path | None = None,
    json_output: str | Path | None = None,
    markdown_output: str | Path | None = None,
) -> dict[str, str]:
    target_dir = Path(report_dir or DEFAULT_REPORT_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    base_name = safe_report_name(str(report.get("baseUrl") or "local"))
    json_path = Path(json_output) if json_output else target_dir / f"{base_name}.json"
    markdown_path = Path(markdown_output) if markdown_output else target_dir / f"{base_name}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return dict(report["artifacts"])


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    mode = report.get("mode") if isinstance(report.get("mode"), dict) else {}
    provider = report.get("provider") if isinstance(report.get("provider"), dict) else {}
    failures = report.get("failures") if isinstance(report.get("failures"), list) else []
    red_flags = report.get("redFlags") if isinstance(report.get("redFlags"), list) else []
    skips = report.get("skips") if isinstance(report.get("skips"), list) else []
    lines = [
        "# Product Acceptance Report",
        "",
        f"- Result: {'PASS' if report.get('ok') else 'FAIL'}",
        f"- Base URL: `{report.get('baseUrl')}`",
        f"- Window: `{report.get('startedAt')}` to `{report.get('finishedAt')}`",
        f"- Mode: liveGenerate=`{mode.get('liveGenerate')}`, limit=`{mode.get('limit')}`, materializeFreeSamples=`{mode.get('materializeFreeSamples')}`",
        f"- Provider: `{provider.get('provider')}` configured=`{provider.get('configured')}` cosReady=`{provider.get('cosReady')}`",
        f"- Summary: passed `{summary.get('passed')}`, failed `{summary.get('failed')}`, skipped `{summary.get('skipped')}`, total `{summary.get('total')}`",
        "",
    ]
    if red_flags:
        lines.extend(["## Red Flags", ""])
        for flag in red_flags:
            lines.append(f"- <span style=\"color:red\"><strong>{flag.get('step')}</strong>: {flag.get('reason')}</span>")
        lines.append("")
    if failures:
        lines.extend(["## Blocking Failures", ""])
        for failure in failures:
            lines.append(f"- `{failure.get('step')}`: {failure.get('reason')}")
        lines.append("")
    else:
        lines.extend(["## Blocking Failures", "", "- None", ""])
    if skips:
        lines.extend(["## Skipped Gates", ""])
        for skipped in skips:
            lines.append(f"- `{skipped.get('step')}`: {skipped.get('reason')}")
        lines.append("")
    lines.extend(
        [
            "## Gate Matrix",
            "",
            "| Step | Status | Key Evidence | Reason |",
            "|---|---:|---|---|",
        ]
    )
    for step in report.get("steps", []):
        if not isinstance(step, dict):
            continue
        fields = step.get("fields") if isinstance(step.get("fields"), dict) else {}
        key_fields = compact_markdown_fields(fields)
        lines.append(
            f"| `{step.get('name')}` | {step.get('status')} | {key_fields} | {escape_markdown_cell(str(step.get('reason') or ''))} |"
        )
    exports = report.get("exports") if isinstance(report.get("exports"), list) else []
    if exports:
        lines.extend(["", "## Export Artifacts", ""])
        for artifact in exports:
            if isinstance(artifact, dict):
                lines.append(
                    f"- `{artifact.get('scope')}`: images=`{artifact.get('images')}`, zipImages=`{artifact.get('zipImages')}`, download=`{artifact.get('download')}`"
                )
    if not mode.get("liveGenerate"):
        lines.extend(
            [
                "",
                "## Live Generation Note",
                "",
                "This run used `--no-live-generate`, so formal model output authenticity is intentionally skipped. Run `--live-generate --limit 1` after Tencent/COS credentials are confirmed.",
            ]
        )
    elif provider.get("configured") is False:
        lines.extend(
            [
                "",
                "## Provider Blocker",
                "",
                "Tencent provider is not configured, so the flow is blocked before formal generation can produce a real image.",
            ]
        )
    return "\n".join(lines) + "\n"


def compact_markdown_fields(fields: dict[str, Any]) -> str:
    keep = [
        "count",
        "kindCounts",
        "total",
        "single",
        "combo",
        "points",
        "styleCount",
        "sampleCount",
        "selectedStyle",
        "selectedRows",
        "jobId",
        "jobStatus",
        "completedItems",
        "imageUrl",
        "scope",
        "images",
        "zipImages",
        "download",
        "remoteIndex",
        "remoteImages",
        "indexImages",
        "indexSource",
        "sources",
        "enabled",
        "cosReady",
        "bucket",
        "indexUrl",
    ]
    compact = {key: fields.get(key) for key in keep if key in fields and fields.get(key) not in (None, "", [])}
    if not compact:
        return ""
    return escape_markdown_cell(json.dumps(compact, ensure_ascii=False, default=str)[:420])


def escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def stdout_summary(report: dict[str, Any], artifacts: dict[str, str] | None = None, base_url_note: str = "") -> dict[str, Any]:
    payload = {
        "ok": report.get("ok"),
        "baseUrl": report.get("baseUrl"),
        "summary": report.get("summary"),
        "failures": report.get("failures"),
        "skips": report.get("skips"),
        "redFlags": report.get("redFlags"),
        "artifacts": artifacts or {},
    }
    if base_url_note:
        payload["baseUrlNote"] = base_url_note
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run product acceptance smoke flow without noisy logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""Examples:
  python3 scripts/smoke_product_flow.py --base-url local --no-live-generate
  python3 scripts/smoke_product_flow.py --base-url render --no-live-generate
  {LIVE_ENV_VAR}=1 python3 scripts/smoke_product_flow.py --base-url https://your-render-app.onrender.com --live-generate --limit 1

Base URL aliases:
  local  -> {DEFAULT_LOCAL_URL}
  render -> {DEFAULT_RENDER_URL}
""",
    )
    parser.add_argument("--base-url", default=DEFAULT_LOCAL_URL, help="Use 'local', 'render', or a full http(s) URL.")
    parser.add_argument("--menu-file", help="Excel menu file to upload. If omitted, a small smoke menu is generated.")
    parser.add_argument("--xls-menu-file", help="Optional real .xls menu for the .xls upload gate. If omitted, one is generated.")
    parser.add_argument("--style-first", action="store_true", help="Use the first returned style card instead of selectedStyle.")
    parser.add_argument("--limit", type=int, default=0, help="Formal live generation limit. Kept at 0 unless --live-generate is set.")
    parser.add_argument("--live-generate", dest="live_generate", action="store_true", help="Actually run one formal image when --limit is greater than 0.")
    parser.add_argument("--no-live-generate", dest="live_generate", action="store_false", help="Dry-run only; never call /api/jobs/<id>/run.")
    parser.add_argument("--generate-free-samples", action="store_true", help="Call the six free preview sample endpoints for the selected style.")
    parser.add_argument("--skip-billing-check", action="store_true", help="Skip smoke recharge/debit checks against the local ledger API.")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Directory for JSON and Markdown acceptance artifacts.")
    parser.add_argument("--json-output", help="Explicit JSON report path.")
    parser.add_argument("--markdown-output", help="Explicit Markdown report path.")
    parser.add_argument("--stdout", choices=["summary", "full"], default="summary", help="Print concise summary JSON or full report JSON.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds.")
    parser.set_defaults(live_generate=False)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base_url, base_url_note = normalize_base_url(args.base_url)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.live_generate:
        if args.limit != 1:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "live generation requires --limit 1 to avoid accidental batch quota usage",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        if not env_flag_enabled(LIVE_ENV_VAR):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"set {LIVE_ENV_VAR}=1 and pass --live-generate --limit 1 to run provider-backed live checks",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
    client = UrllibProductClient(base_url, timeout=args.timeout)
    report = run_product_flow(
        client,
        menu_file=args.menu_file,
        xls_menu_file=args.xls_menu_file,
        base_url=base_url,
        style_first=args.style_first,
        limit=args.limit,
        live_generate=args.live_generate,
        materialize_free_samples=args.generate_free_samples,
        billing_check=not args.skip_billing_check,
    )
    artifacts = write_acceptance_artifacts(
        report,
        report_dir=args.report_dir,
        json_output=args.json_output,
        markdown_output=args.markdown_output,
    )
    report["artifacts"] = artifacts
    if args.stdout == "full":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(stdout_summary(report, artifacts, base_url_note), ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
