#!/usr/bin/env python3
"""Run the real customer HTTP flow inside the protected Render staging service."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

import requests

from menu_e2e_acceptance import (
    AcceptanceError,
    AcceptanceReport,
    FAIL,
    PASS,
    execute_pipeline,
    file_sha256,
    menu_evidence,
    parse_real_menu,
    redact_text,
    skip_remaining_stages,
)


CONFIRM_ENV = "WAIMAI_STAGING_E2E_CONFIRM"
CONFIRM_VALUE = "I_ACCEPT_REAL_PROVIDER_CHARGES"
MENU_B64_ENV = "WAIMAI_STAGING_E2E_MENU_B64"
MENU_NAME_ENV = "WAIMAI_STAGING_E2E_MENU_NAME"
EXPECTED_CATEGORY_ENV = "WAIMAI_STAGING_E2E_EXPECT_CATEGORY"
MAX_ROWS_ENV = "WAIMAI_STAGING_E2E_MAX_ROWS"
HTTP_TIMEOUT_ENV = "WAIMAI_STAGING_E2E_HTTP_TIMEOUT"
FLOW_TIMEOUT_ENV = "WAIMAI_STAGING_E2E_FLOW_TIMEOUT"
STYLE_INDEX_ENV = "WAIMAI_STAGING_E2E_STYLE_INDEX"
GET_MAX_ATTEMPTS = 4
GET_RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
GET_RETRY_BASE_DELAY_SECONDS = 0.25


class RemoteResponse:
    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self.status_code = int(response.status_code)
        self.headers = response.headers
        self.data = response.content

    def get_json(self, silent: bool = False) -> Any:
        try:
            return self._response.json()
        except ValueError:
            if silent:
                return None
            raise


class RemoteClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (username, password)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("remote acceptance paths must be absolute")
        return f"{self.base_url}{path}"

    def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> RemoteResponse:
        request_headers = dict(headers or {})
        request_headers.setdefault("Connection", "close")
        for attempt in range(1, GET_MAX_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    self._url(path),
                    headers=request_headers,
                    timeout=self.timeout_seconds,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= GET_MAX_ATTEMPTS:
                    raise
            else:
                if (
                    response.status_code not in GET_RETRY_STATUS_CODES
                    or attempt >= GET_MAX_ATTEMPTS
                ):
                    return RemoteResponse(response)
                response.close()
            time.sleep(GET_RETRY_BASE_DELAY_SECONDS * attempt)
        raise RuntimeError("unreachable GET retry state")

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> RemoteResponse:
        request_data = data
        files = None
        if data and isinstance(data.get("file"), tuple):
            stream, filename = data["file"]
            raw = stream.read() if hasattr(stream, "read") else bytes(stream)
            files = {
                "file": (
                    str(filename),
                    io.BytesIO(raw),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            }
            request_data = {
                key: value for key, value in data.items() if key != "file"
            }
        return RemoteResponse(
            self.session.post(
                self._url(path),
                json=json,
                data=request_data,
                files=files,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        )


def required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def positive_int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name) or default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def wait_for_web(client: RemoteClient, timeout_seconds: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = client.get("/")
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = type(exc).__name__
        time.sleep(1.0)
    raise RuntimeError(f"staging web did not become ready: {last_error}")


def persist_report(path: Path, timestamp: str) -> dict[str, Any]:
    import object_storage_service

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    object_key = (
        "generated/acceptance/render-staging/"
        f"{timestamp}/report-{digest[:16]}.json"
    )
    storage = object_storage_service.get_object_storage_service()
    stored_key = storage.put_bytes(raw, object_key=object_key)
    persisted = object_storage_service.read_object_bytes_limited(
        storage,
        stored_key,
        4 * 1024 * 1024,
    )
    if persisted != raw:
        raise RuntimeError("persisted acceptance report bytes do not match")
    return {
        "objectKey": stored_key,
        "sha256": digest,
        "size": len(raw),
    }


def main() -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = Path(
        f"/tmp/waimai-render-staging-e2e-{timestamp}.json"
    )
    report = AcceptanceReport(mode="staging-real", report_path=report_path)
    active_stage = "preflight"
    try:
        if required_env(CONFIRM_ENV) != CONFIRM_VALUE:
            raise RuntimeError(f"{CONFIRM_ENV} confirmation is invalid")
        if required_env("APP_ENV") != "staging-demo":
            raise RuntimeError("APP_ENV must be staging-demo")
        if str(
            os.environ.get("ALLOW_STAGING_IN_PROCESS_GENERATION") or ""
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError(
                "ALLOW_STAGING_IN_PROCESS_GENERATION must be enabled"
            )

        raw_menu = base64.b64decode(required_env(MENU_B64_ENV), validate=True)
        menu_name = Path(required_env(MENU_NAME_ENV)).name
        if Path(menu_name).suffix.lower() not in {".xls", ".xlsx"}:
            raise RuntimeError("staging E2E menu must be an Excel file")
        menu_path = Path("/tmp") / f"waimai-e2e-{timestamp}-{menu_name}"
        menu_path.write_bytes(raw_menu)
        parsed_menu = parse_real_menu(menu_path)
        evidence = menu_evidence(menu_path, parsed_menu)
        max_rows = positive_int_env(MAX_ROWS_ENV, 0)
        if int(evidence["rowCount"]) > max_rows:
            raise RuntimeError(
                f"menu has {evidence['rowCount']} rows, over the {max_rows} row gate"
            )
        expected_category = required_env(EXPECTED_CATEGORY_ENV)

        port = positive_int_env("PORT", 10000)
        base_url = str(
            os.environ.get("WAIMAI_STAGING_E2E_BASE_URL")
            or f"http://127.0.0.1:{port}"
        ).strip()
        client = RemoteClient(
            base_url,
            username=required_env("STAGING_BASIC_AUTH_USER"),
            password=required_env("STAGING_BASIC_AUTH_PASSWORD"),
            timeout_seconds=float(
                positive_int_env(HTTP_TIMEOUT_ENV, 300)
            ),
        )
        wait_for_web(client)
        report.add_stage(
            "preflight",
            PASS,
            details={
                **evidence,
                "expectedCategoryId": expected_category,
                "baseUrl": "http://127.0.0.1:[render-port]",
                "approvedCatalogRequired": True,
                "paidProviderCallsAllowed": True,
                "secretsRedacted": True,
                "summary": (
                    f"real staging HTTP flow for {evidence['rowCount']} dishes"
                ),
            },
        )

        active_stage = "upload"
        remote_app = SimpleNamespace(
            app=SimpleNamespace(test_client=lambda: client)
        )
        state = execute_pipeline(
            app_module=remote_app,
            menu_path=menu_path,
            parsed_menu=parsed_menu,
            report=report,
            mode="real",
            quality="standard",
            platforms=["meituan"],
            style_index=int(os.environ.get(STYLE_INDEX_ENV, "2")),
            timeout_seconds=float(
                positive_int_env(FLOW_TIMEOUT_ENV, 7200)
            ),
            expected_category_id=expected_category,
            require_approved_catalog=True,
        )
        manifest = state.get("manifest") or {}
        results = (
            manifest.get("results")
            if isinstance(manifest.get("results"), list)
            else []
        )
        combo_count = sum(
            1 for row in results if str(row.get("kind") or "") == "套餐/组合"
        )
        expected_combo_count = int(
            (parsed_menu.get("kindCounts") or {}).get("combo") or 0
        )
        if combo_count != expected_combo_count:
            raise AcceptanceError(
                "manifest",
                (
                    "combo output count mismatch: expected "
                    f"{expected_combo_count}, got {combo_count}"
                ),
            )

        summary = {
            "stagingDeploymentVerified": True,
            "productionDeploymentVerified": False,
            "menu": evidence,
            "categoryId": expected_category,
            "backgroundCount": len(state.get("backgrounds") or []),
            "freeSampleCount": len(state.get("samples") or []),
            "formalImageCount": len(results),
            "comboImageCount": combo_count,
            "jobId": str(state.get("jobId") or ""),
            "selectedBackground": state.get("selected") or {},
            "reportPath": str(report.path),
        }
        report.finish(
            PASS,
            summary=summary,
            production_provider_verified=True,
        )
        artifact = persist_report(report.path, timestamp)
        print(
            "WAIMAI_STAGING_E2E_RESULT="
            + json.dumps(
                {
                    "status": PASS,
                    **summary,
                    "menu": {
                        "filename": evidence["menuFilename"],
                        "sha256": evidence["menuSha256"],
                        "rowCount": evidence["rowCount"],
                    },
                    "reportArtifact": artifact,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        print("WAIMAI_STAGING_E2E_PASS", flush=True)
        return 0
    except Exception as exc:
        if isinstance(exc, AcceptanceError):
            active_stage = exc.stage
        existing_stages = {
            str(item.get("name") or "")
            for item in report.data.get("stages", [])
        }
        if not existing_stages:
            report.add_stage(
                "preflight",
                FAIL,
                error=f"{type(exc).__name__}: {redact_text(exc)}",
            )
            active_stage = "preflight"
        skip_remaining_stages(
            report,
            after_stage=active_stage,
            reason=f"stopped after {active_stage} failure",
        )
        report.finish(
            FAIL,
            summary={
                "reason": f"{type(exc).__name__}: {redact_text(exc)}",
                "reportPath": str(report.path),
                "stagingDeploymentVerified": False,
                "productionDeploymentVerified": False,
            },
        )
        artifact: dict[str, Any] | None = None
        try:
            artifact = persist_report(report.path, timestamp)
        except Exception as persist_exc:
            print(
                "WAIMAI_STAGING_E2E_REPORT_PERSIST_FAILED="
                f"{type(persist_exc).__name__}",
                flush=True,
            )
        print(
            "WAIMAI_STAGING_E2E_RESULT="
            + json.dumps(
                {
                    "status": FAIL,
                    "stage": active_stage,
                    "error": f"{type(exc).__name__}: {redact_text(exc)}",
                    "reportArtifact": artifact,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        print("WAIMAI_STAGING_E2E_FAIL", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
