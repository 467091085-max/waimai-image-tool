from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SMOKE_DISH = "招牌辣椒炒肉"
SMOKE_PROMPT_NOTE = "同菜名、统一背景、外卖主图、无文字/水印/logo/价格、不裁切主体"
LIVE_ENV_VAR = "WAIMAI_ACCEPTANCE_LIVE"


def smoke_item(dish: str = SMOKE_DISH) -> dict[str, Any]:
    return {
        "row": 1,
        "category": "hunyuan-live-smoke",
        "name": dish,
        "kind": "单品",
        "components": [],
        "candidates": [],
        "backgroundAction": "智能补图",
        "publicStatus": "待正式生成",
        "points": 0,
    }


class SmokeClient:
    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self._flask_client = None

    def _local_client(self):
        if self._flask_client is None:
            import app as app_module

            self._flask_client = app_module.app.test_client()
        return self._flask_client

    def get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        if not self.base_url:
            response = self._local_client().get(path)
            return response.status_code, response.get_json(silent=True) or {}
        req = urllib.request.Request(f"{self.base_url}{path}", headers={"Accept": "application/json"})
        return self._open(req)

    def post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self.base_url:
            response = self._local_client().post(path, json=payload)
            return response.status_code, response.get_json(silent=True) or {}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=raw,
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        return self._open(req)

    def absolute_url(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith(("http://", "https://")):
            return value
        if self.base_url and value.startswith("/"):
            return f"{self.base_url}{value}"
        return value

    @staticmethod
    def _open(req: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                raw = response.read().decode("utf-8")
                return int(response.status), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except Exception:
                body = {"error": raw[:500]}
            return int(exc.code), body


def provider_status(value: str) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"queued", "running", "succeeded", "failed", "partial"}:
        return clean
    if clean in {"completed", "reused", "cached", "skipped"}:
        return "succeeded"
    if clean == "fallback":
        return "partial"
    if clean in {"pending", "waiting", "waiting_for_provider", "limited"}:
        return "queued"
    if clean == "partially_failed":
        return "partial"
    return "failed" if clean else "queued"


def require_ok(status_code: int, body: dict[str, Any], label: str) -> None:
    if 200 <= status_code < 300:
        return
    message = body.get("error") or body.get("message") or body
    raise RuntimeError(f"{label} failed HTTP {status_code}: {message}")


def result_url_from_item(client: SmokeClient, item: dict[str, Any]) -> str:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    generation_result = result.get("generationResult") if isinstance(result.get("generationResult"), dict) else {}
    generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
    for container in (generation_result, generation):
        candidate = container.get("candidate") if isinstance(container.get("candidate"), dict) else {}
        url = str(candidate.get("url") or "")
        if url:
            return client.absolute_url(url)
        path = str(container.get("path") or candidate.get("path") or "")
        if path:
            return path
    return ""


def summarize(client: SmokeClient, status_body: dict[str, Any], job_body: dict[str, Any] | None = None) -> dict[str, Any]:
    job = (job_body or {}).get("job") if isinstance((job_body or {}).get("job"), dict) else {}
    items = job.get("items") if isinstance(job.get("items"), list) else []
    item = items[0] if items and isinstance(items[0], dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    generation_result = result.get("generationResult") if isinstance(result.get("generationResult"), dict) else {}
    generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
    provider = str(item.get("provider") or generation_result.get("provider") or generation.get("provider") or status_body.get("provider") or "")
    status = str(job.get("status") or item.get("status") or status_body.get("status") or "")
    provider_error = str(
        item.get("provider_error")
        or item.get("providerError")
        or generation_result.get("provider_error")
        or generation_result.get("providerError")
        or generation.get("provider_error")
        or generation.get("providerError")
        or status_body.get("provider_error")
        or ""
    )
    error = provider_error or str(item.get("error") or generation_result.get("error") or generation.get("error") or job.get("error") or "")
    return {
        "provider": provider,
        "status": provider_status(status),
        "jobStatus": status,
        "itemStatus": item.get("status") or "",
        "providerStatus": provider_status(str(generation_result.get("providerStatus") or generation.get("providerStatus") or status)),
        "error": error,
        "provider_error": provider_error,
        "retryable": bool(item.get("retryable") or generation_result.get("retryable") or generation.get("retryable") or status_body.get("retryable")),
        "refund_required": bool(
            item.get("refund_required")
            or item.get("refundRequired")
            or generation_result.get("refund_required")
            or generation_result.get("refundRequired")
            or generation.get("refund_required")
            or generation.get("refundRequired")
        ),
        "result_url": result_url_from_item(client, item),
        "jobId": job.get("id") or "",
        "jobUrl": f"/api/jobs/{job['id']}" if job.get("id") else "",
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    client = SmokeClient(args.base_url)
    status_code, status_body = client.get_json("/api/tencent-status")
    require_ok(status_code, status_body, "/api/tencent-status")
    env_ready = bool(status_body.get("configured")) and bool(status_body.get("cosReady"))
    missing = status_body.get("missing") if isinstance(status_body.get("missing"), list) else []

    create_payload = {
        "style": args.style,
        "quality": args.quality,
        "paid": True,
        "orderId": f"hunyuan-smoke-{int(time.time())}",
        "points": 0,
        "items": [smoke_item(args.dish)],
    }
    if not args.live:
        summary = summarize(client, status_body)
        return {
            "mode": "dry-run",
            "live": False,
            "acceptanceStatus": "passed" if env_ready else "skipped",
            "skipped": not env_ready,
            "skipReason": "" if env_ready else "Tencent/COS Render env is not fully configured; live provider evidence is unavailable",
            "willCreateJob": False,
            "willRunProvider": False,
            "statusEndpoint": "/api/tencent-status",
            "provider": summary["provider"],
            "status": summary["status"],
            "providerStatus": summary["providerStatus"],
            "configured": bool(status_body.get("configured")),
            "cosReady": bool(status_body.get("cosReady")),
            "missing": missing,
            "error": summary["error"],
            "provider_error": summary["provider_error"],
            "retryable": summary["retryable"],
            "refund_required": summary["refund_required"],
            "result_url": "",
            "fixedDish": args.dish,
            "promptContract": SMOKE_PROMPT_NOTE,
            "liveCommandRequired": "--live --limit 1",
            "createPayload": create_payload,
        }

    if not env_ready:
        summary = summarize(client, status_body)
        return {
            "mode": "live",
            "live": True,
            "acceptanceStatus": "skipped",
            "skipped": True,
            "skipReason": "Tencent/COS Render env is not fully configured; skipped before creating a paid provider job",
            "willCreateJob": False,
            "willRunProvider": False,
            "provider": summary["provider"],
            "status": "skipped",
            "providerStatus": summary["providerStatus"],
            "configured": bool(status_body.get("configured")),
            "cosReady": bool(status_body.get("cosReady")),
            "missing": missing,
            "error": summary["error"],
            "provider_error": summary["provider_error"],
            "retryable": summary["retryable"],
            "refund_required": summary["refund_required"],
            "result_url": "",
            "fixedDish": args.dish,
            "promptContract": SMOKE_PROMPT_NOTE,
            "createPayload": create_payload,
        }

    create_status, create_body = client.post_json("/api/jobs", create_payload)
    require_ok(create_status, create_body, "/api/jobs")
    job = create_body.get("job") if isinstance(create_body.get("job"), dict) else {}
    job_id = str(job.get("id") or "")
    if not job_id:
        raise RuntimeError("/api/jobs did not return job.id")
    run_status, run_body = client.post_json(f"/api/jobs/{job_id}/run", {"limit": args.limit, "paid": True})
    require_ok(run_status, run_body, f"/api/jobs/{job_id}/run")
    summary = summarize(client, status_body, run_body)
    summary.update(
        {
            "mode": "live",
            "live": True,
            "acceptanceStatus": "passed" if summary.get("status") in {"succeeded", "partial"} and summary.get("result_url") else "failed",
            "skipped": False,
            "skipReason": "",
            "limit": args.limit,
            "configured": bool(status_body.get("configured")),
            "cosReady": bool(status_body.get("cosReady")),
            "willRunProvider": env_ready,
            "missing": missing,
            "fixedDish": args.dish,
            "promptContract": SMOKE_PROMPT_NOTE,
        }
    )
    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run/live smoke for Tencent Hunyuan generation job flow.")
    parser.add_argument("--base-url", default="", help="Existing app base URL. Empty uses Flask test client in-process.")
    parser.add_argument("--live", action="store_true", help="Actually create and run one provider-backed job.")
    parser.add_argument("--limit", type=int, default=0, help="Live mode must be exactly 1.")
    parser.add_argument("--style", default="style-1")
    parser.add_argument("--quality", default="standard", choices=["standard", "premium", "normal"])
    parser.add_argument("--dish", default=SMOKE_DISH)
    args = parser.parse_args(argv)
    if args.live and args.limit != 1:
        parser.error("真实调用必须显式传 --live --limit 1，避免批量消耗额度")
    if args.live and str(os.environ.get(LIVE_ENV_VAR) or "").strip().lower() not in {"1", "true", "yes", "on"}:
        parser.error(f"真实调用还必须设置 {LIVE_ENV_VAR}=1，避免误消耗混元额度")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = run_smoke(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    status = str(result.get("acceptanceStatus") or result.get("status") or "").lower()
    ok = status not in {"failed", "fail", "error"}
    print(json.dumps({"ok": ok, **result}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
