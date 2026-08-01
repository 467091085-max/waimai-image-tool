#!/usr/bin/env python3
"""Run the real customer HTTP flow inside the protected Render staging service."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable
import urllib.parse

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
RUN_ID_ENV = "WAIMAI_STAGING_E2E_RUN_ID"
KEEPALIVE_URL_ENV = "WAIMAI_STAGING_E2E_KEEPALIVE_URL"
KEEPALIVE_INTERVAL_ENV = "WAIMAI_STAGING_E2E_KEEPALIVE_INTERVAL"
INSTANCE_NONCE_PATH = Path("/tmp/waimai-staging-e2e-instance-nonce")
GET_MAX_ATTEMPTS = 4
GET_RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
GET_RETRY_BASE_DELAY_SECONDS = 0.25
RETRYABLE_GET_PATH_PREFIXES = ("/api/generation-jobs/",)


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
        self.request_guard: Callable[[], None] | None = None

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
        if self.request_guard is not None:
            self.request_guard()
        retry_allowed = path.startswith(RETRYABLE_GET_PATH_PREFIXES)
        max_attempts = GET_MAX_ATTEMPTS if retry_allowed else 1
        request_headers = dict(headers or {})
        if retry_allowed:
            request_headers.setdefault("Connection", "close")
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.get(
                    self._url(path),
                    headers=request_headers,
                    timeout=self.timeout_seconds,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= max_attempts:
                    raise
            else:
                if (
                    response.status_code not in GET_RETRY_STATUS_CODES
                    or attempt >= max_attempts
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
        if self.request_guard is not None:
            self.request_guard()
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


def https_origin(value: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port not in {None, 443}
    ):
        raise RuntimeError("staging keep-alive URL must be an HTTPS origin")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        raise RuntimeError("staging keep-alive host must be public")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise RuntimeError("staging keep-alive host must be public")
    return f"https://{hostname}/", hostname


def staging_keepalive_origin() -> tuple[str, str]:
    render_url = str(os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
    if not render_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is required for keep-alive")
    render_origin, render_hostname = https_origin(render_url)
    requested = str(
        os.environ.get(KEEPALIVE_URL_ENV) or render_url
    ).strip()
    origin, hostname = https_origin(requested)
    if origin != render_origin or hostname != render_hostname:
        raise RuntimeError(
            "staging keep-alive host must match the configured public service"
        )
    return origin, hostname


def staging_loopback_origin(port: int) -> str:
    requested = str(
        os.environ.get("WAIMAI_STAGING_E2E_BASE_URL")
        or f"http://127.0.0.1:{port}"
    ).strip()
    parsed = urllib.parse.urlsplit(requested)
    if (
        parsed.scheme.lower() != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port != port
    ):
        raise RuntimeError(
            "staging E2E base URL must be the current loopback service"
        )
    return f"http://127.0.0.1:{port}"


def create_instance_nonce() -> str:
    nonce = secrets.token_hex(32)
    INSTANCE_NONCE_PATH.write_text(nonce, encoding="ascii")
    INSTANCE_NONCE_PATH.chmod(0o600)
    return nonce


def e2e_run_record_key(run_id: str) -> str:
    normalized = str(run_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", normalized):
        raise RuntimeError("WAIMAI_STAGING_E2E_RUN_ID is invalid")
    return (
        "generated/acceptance/render-staging/runs/"
        f"{normalized}/run.json"
    )


def claim_e2e_run(
    run_id: str,
    *,
    instance_nonce: str,
    menu_sha256: str,
    expected_category: str,
) -> tuple[str, dict[str, Any]]:
    import object_storage_service

    object_key = e2e_run_record_key(run_id)
    storage = object_storage_service.get_object_storage_service()
    existing = storage.read_bytes_if_exists(object_key)
    if existing is not None:
        try:
            existing_record = json.loads(existing.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            existing_record = {}
        status = str(
            existing_record.get("status")
            if isinstance(existing_record, dict)
            else ""
        ) or "unknown"
        raise RuntimeError(
            f"staging E2E run is already claimed ({status})"
        )
    record = {
        "schemaVersion": 1,
        "runId": run_id,
        "status": "claimed",
        "claimedAt": datetime.now(timezone.utc).isoformat(),
        "instanceNonceSha256": hashlib.sha256(
            instance_nonce.encode("ascii")
        ).hexdigest(),
        "menuSha256": menu_sha256,
        "expectedCategoryId": expected_category,
        "renderServiceId": str(
            os.environ.get("RENDER_SERVICE_ID") or ""
        ),
        "renderInstanceId": str(
            os.environ.get("RENDER_INSTANCE_ID") or ""
        ),
        "renderGitCommit": str(
            os.environ.get("RENDER_GIT_COMMIT") or ""
        ),
    }
    raw = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    storage.put_bytes(raw, object_key=object_key)
    readback = object_storage_service.read_object_bytes_limited(
        storage,
        object_key,
        64 * 1024,
    )
    if readback != raw:
        raise RuntimeError("staging E2E run claim read-back mismatch")
    return object_key, record


def finalize_e2e_run_claim(
    object_key: str,
    claimed_record: dict[str, Any],
    *,
    status: str,
    report_artifact: dict[str, Any] | None,
) -> None:
    import object_storage_service

    storage = object_storage_service.get_object_storage_service()
    current_raw = object_storage_service.read_object_bytes_limited(
        storage,
        object_key,
        64 * 1024,
    )
    current = json.loads(current_raw.decode("utf-8"))
    if not (
        isinstance(current, dict)
        and hmac.compare_digest(
            str(current.get("instanceNonceSha256") or ""),
            str(claimed_record.get("instanceNonceSha256") or ""),
        )
    ):
        raise RuntimeError("staging E2E run claim ownership changed")
    final_record = {
        **claimed_record,
        "status": status,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "reportArtifact": report_artifact,
    }
    raw = json.dumps(
        final_record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    storage.put_bytes(raw, object_key=object_key)
    if object_storage_service.read_object_bytes_limited(
        storage,
        object_key,
        64 * 1024,
    ) != raw:
        raise RuntimeError("staging E2E final run record mismatch")


class PublicKeepAlive:
    def __init__(
        self,
        origin: str,
        *,
        hostname: str,
        username: str,
        password: str,
        instance_nonce: str,
        interval_seconds: float,
        timeout_seconds: float = 20.0,
        startup_timeout_seconds: float = 180.0,
    ) -> None:
        self.origin = origin
        self.hostname = hostname
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.startup_timeout_seconds = max(
            0.0,
            float(startup_timeout_seconds),
        )
        self.instance_nonce = instance_nonce
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (username, password)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_success: float | None = None
        self._consecutive_failures = 0
        self._probe_count = 0

    def _probe(self) -> None:
        response = self.session.get(
            urllib.parse.urljoin(
                self.origin,
                "/api/staging-e2e-instance",
            ),
            timeout=self.timeout_seconds,
            allow_redirects=False,
            headers={
                "User-Agent": "waimai-staging-e2e-keepalive/1",
                "X-Waimai-Staging-Instance": self.instance_nonce,
            },
        )
        try:
            if response.status_code != 200:
                raise RuntimeError(
                    f"public keep-alive returned HTTP {response.status_code}"
                )
            payload = response.json()
            if not (
                isinstance(payload, dict)
                and payload.get("instanceMatched") is True
            ):
                raise RuntimeError(
                    "public keep-alive reached a different Render instance"
                )
        finally:
            response.close()
        with self._lock:
            self._last_success = time.monotonic()
            self._consecutive_failures = 0
            self._probe_count += 1

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._probe()
            except Exception as exc:
                with self._lock:
                    self._consecutive_failures += 1
                    failure_count = self._consecutive_failures
                print(
                    "WAIMAI_STAGING_E2E_KEEPALIVE_WARNING="
                    f"{type(exc).__name__}:{failure_count}",
                    flush=True,
                )

    def start(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            try:
                self._probe()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)
        self._thread = threading.Thread(
            target=self._run,
            name="render-public-keepalive",
            daemon=True,
        )
        self._thread.start()
        print(
            "WAIMAI_STAGING_E2E_KEEPALIVE_READY=" + self.hostname,
            flush=True,
        )

    def assert_healthy(self) -> None:
        with self._lock:
            last_success = self._last_success
            consecutive_failures = self._consecutive_failures
        stale_after = min(10 * 60.0, self.interval_seconds * 4.0)
        if (
            last_success is None
            or consecutive_failures >= 3
            or time.monotonic() - last_success > stale_after
        ):
            raise RuntimeError("public keep-alive is no longer healthy")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_seconds + 1.0)
        self.session.close()

    def report(self) -> dict[str, Any]:
        with self._lock:
            return {
                "hostname": self.hostname,
                "instanceMatched": self._last_success is not None,
                "probeCount": self._probe_count,
                "consecutiveFailures": self._consecutive_failures,
            }


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


def verify_runtime_generation_capacity(
    client: RemoteClient,
    required_images: int,
) -> dict[str, Any]:
    response = client.get("/api/tencent-status")
    if response.status_code != 200:
        raise AcceptanceError(
            "preflight",
            f"provider status returned HTTP {response.status_code}",
        )
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict) or not payload.get("configured"):
        raise AcceptanceError(
            "preflight",
            "paid Tencent image generation is not configured",
        )
    try:
        sync_limit = int(payload.get("syncLimit"))
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(
            "preflight",
            "provider status has an invalid syncLimit",
        ) from exc
    if sync_limit < required_images:
        raise AcceptanceError(
            "preflight",
            (
                "runtime TENCENT_HUNYUAN_SYNC_LIMIT is too low: "
                f"required {required_images}, got {sync_limit}"
            ),
        )
    return payload


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
    run_claim_key = ""
    run_claim_record: dict[str, Any] = {}
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
        run_id = required_env(RUN_ID_ENV)
        instance_nonce = create_instance_nonce()

        port = positive_int_env("PORT", 10000)
        base_url = staging_loopback_origin(port)
        username = required_env("STAGING_BASIC_AUTH_USER")
        password = required_env("STAGING_BASIC_AUTH_PASSWORD")
        client = RemoteClient(
            base_url,
            username=username,
            password=password,
            timeout_seconds=float(
                positive_int_env(HTTP_TIMEOUT_ENV, 300)
            ),
        )
        wait_for_web(client)
        keepalive_origin, keepalive_hostname = staging_keepalive_origin()
        keepalive = PublicKeepAlive(
            keepalive_origin,
            hostname=keepalive_hostname,
            username=username,
            password=password,
            instance_nonce=instance_nonce,
            interval_seconds=float(
                positive_int_env(KEEPALIVE_INTERVAL_ENV, 120)
            ),
        )
        keepalive.start()
        client.request_guard = keepalive.assert_healthy
        try:
            provider_status = verify_runtime_generation_capacity(
                client,
                int(evidence["rowCount"]),
            )
            run_claim_key, run_claim_record = claim_e2e_run(
                run_id,
                instance_nonce=instance_nonce,
                menu_sha256=str(evidence["menuSha256"]),
                expected_category=expected_category,
            )
            report.add_stage(
                "preflight",
                PASS,
                details={
                    **evidence,
                    "expectedCategoryId": expected_category,
                    "baseUrl": "http://127.0.0.1:[render-port]",
                    "publicKeepAlive": keepalive.report(),
                    "approvedCatalogRequired": True,
                    "paidProviderCallsAllowed": True,
                    "runtimeSyncLimit": int(provider_status["syncLimit"]),
                    "runIdSha256": hashlib.sha256(
                        run_id.encode("utf-8")
                    ).hexdigest(),
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
            keepalive.assert_healthy()
            keepalive_evidence = keepalive.report()
        finally:
            client.request_guard = None
            keepalive.stop()
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
            "publicKeepAlive": keepalive_evidence,
            "runIdSha256": hashlib.sha256(
                run_id.encode("utf-8")
            ).hexdigest(),
            "reportPath": str(report.path),
        }
        report.finish(
            PASS,
            summary=summary,
            production_provider_verified=True,
        )
        artifact = persist_report(report.path, timestamp)
        try:
            finalize_e2e_run_claim(
                run_claim_key,
                run_claim_record,
                status=PASS,
                report_artifact=artifact,
            )
        except Exception as claim_exc:
            print(
                "WAIMAI_STAGING_E2E_RUN_FINALIZE_WARNING="
                f"{type(claim_exc).__name__}",
                flush=True,
            )
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
        if run_claim_key and run_claim_record:
            try:
                finalize_e2e_run_claim(
                    run_claim_key,
                    run_claim_record,
                    status=FAIL,
                    report_artifact=artifact,
                )
            except Exception as claim_exc:
                print(
                    "WAIMAI_STAGING_E2E_RUN_FINALIZE_WARNING="
                    f"{type(claim_exc).__name__}",
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
