from __future__ import annotations

import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from flask import Flask, jsonify, request

from scripts import smoke_product_flow as smoke


class FlaskClientAdapter:
    def __init__(self, app: Flask) -> None:
        self.client = app.test_client()

    def get(self, path: str, query: dict[str, Any] | None = None) -> smoke.ClientResponse:
        resp = self.client.get(path, query_string=query or {})
        return smoke.ClientResponse(resp.status_code, resp.get_data(), dict(resp.headers), path)

    def post_json(self, path: str, payload: dict[str, Any]) -> smoke.ClientResponse:
        resp = self.client.post(path, json=payload)
        return smoke.ClientResponse(resp.status_code, resp.get_data(), dict(resp.headers), path)

    def post_file(self, path: str, file_path: str | Path, field_name: str = "file") -> smoke.ClientResponse:
        file_path = Path(file_path)
        with file_path.open("rb") as handle:
            resp = self.client.post(
                path,
                data={field_name: (handle, file_path.name)},
                content_type="multipart/form-data",
            )
        return smoke.ClientResponse(resp.status_code, resp.get_data(), dict(resp.headers), path)


def style_cards() -> list[dict[str, Any]]:
    return [
        {
            "id": style_id,
            "name": f"Style {index}",
            "source": "library",
            "count": 3,
            "needsGeneratedBackground": False,
            "backgroundJob": {"status": "cached", "provider": "library", "action": "Reuse"},
        }
        for index, style_id in enumerate(smoke.STYLE_IDS, start=1)
    ]


def plan_payload(styles: list[dict[str, Any]] | None = None, selected_style: str = "style-2") -> dict[str, Any]:
    return {
        "selectedStyle": selected_style,
        "styles": styles if styles is not None else style_cards(),
        "summary": {"total": 2, "direct": 0, "review": 2, "missing": 0, "points": 200},
        "pricing": {"standardPoints": 100, "premiumPoints": 200, "customEditPoints": 150, "customEditCash": 15, "previewFreeImages": 6},
        "quote": {"points": 200, "cash": 20, "rate": "1 yuan = 10 points"},
        "results": [
            {
                "row": 1,
                "name": "Smoke Dish",
                "category": "Hot",
                "kind": "single",
                "points": 100,
                "publicStatus": "pending",
                "backgroundAction": "replace",
                "candidates": [{"url": "https://cdn.example.test/source.jpg", "styleId": "style-3"}],
            },
            {
                "row": 2,
                "name": "Smoke Combo",
                "category": "Combo",
                "kind": "套餐/组合",
                "points": 100,
                "publicStatus": "pending",
                "backgroundAction": "combo",
                "candidates": [{"url": "https://cdn.example.test/combo-source.jpg", "styleId": "style-4"}],
            },
        ],
    }


def make_mock_app(
    *,
    broken_plan: bool = False,
    styles: list[dict[str, Any]] | None = None,
    live_result: str = "tencent",
) -> tuple[Flask, dict[str, Any]]:
    app = Flask(__name__)
    state: dict[str, Any] = {
        "job_requests": [],
        "run_requests": [],
        "export_requests": [],
        "billing_requests": [],
        "balances": {},
    }

    @app.get("/")
    def index() -> str:
        return "<html><body>waimai smoke</body></html>"

    @app.get("/api/tencent-status")
    def tencent_status():
        return jsonify({"provider": "tencent-hunyuan", "configured": True, "cosReady": True, "styleBackgroundsLive": True})

    @app.get("/api/account")
    def account():
        return jsonify({"account": {"userId": "smoke-user", "balance": 1200}})

    @app.post("/api/recharge")
    def recharge():
        payload = request.get_json() or {}
        user_id = str(payload.get("userId") or "smoke-user")
        points = int(payload.get("points") or 0)
        state["balances"][user_id] = int(state["balances"].get(user_id, 0)) + points
        transaction = {
            "orderId": payload.get("orderId"),
            "eventType": "account_credit",
            "points": points,
            "balance": state["balances"][user_id],
        }
        state["billing_requests"].append({"kind": "recharge", "payload": payload, "transaction": transaction})
        return jsonify({"ok": True, "transaction": transaction, "account": {"userId": user_id, "balance": state["balances"][user_id]}})

    @app.post("/api/debit")
    def debit():
        payload = request.get_json() or {}
        user_id = str(payload.get("userId") or "smoke-user")
        points = int(payload.get("points") or 0)
        balance = int(state["balances"].get(user_id, 0))
        if points <= 0:
            return jsonify({"error": "points required"}), 400
        if balance < points:
            return jsonify({"error": "insufficient balance"}), 402
        state["balances"][user_id] = balance - points
        transaction = {
            "orderId": payload.get("orderId"),
            "eventType": "account_debit",
            "description": payload.get("description"),
            "points": points,
            "balance": state["balances"][user_id],
        }
        state["billing_requests"].append({"kind": "debit", "payload": payload, "transaction": transaction})
        return jsonify({"ok": True, "transaction": transaction, "account": {"userId": user_id, "balance": state["balances"][user_id]}})

    @app.get("/api/library-status")
    def library_status():
        return jsonify(
            {
                "total": 42,
                "reusable": 40,
                "stores": 3,
                "styles": 6,
                "sources": {"seed": 42},
                "externalDirs": ["/mock/library"],
                "remoteIndex": True,
                "remoteImages": 12,
                "indexImages": 12,
                "indexSource": "https://cdn.example.test/library_index.jsonl",
                "indexError": "",
            }
        )

    @app.post("/api/upload-menu")
    def upload_menu():
        assert "file" in request.files
        return jsonify({"ok": True, "file": request.files["file"].filename, "menu": {"store": "Smoke Store", "count": 3, "kindCounts": {"single": 2, "combo": 1}}})

    @app.get("/api/plan")
    def plan():
        if broken_plan:
            return jsonify({"error": "plan exploded"}), 500
        return jsonify(plan_payload(styles=styles))

    @app.get("/api/style-preview")
    def style_preview():
        style = request.args.get("style", "")
        return jsonify(
            {
                "style": style,
                "styleName": style,
                "previewFreeImages": 6,
                "samples": [
                    {
                        "row": index,
                        "status": "pending",
                        "job": {"status": "pending", "provider": "tencent-hunyuan", "action": "Preview"},
                        "candidate": None,
                    }
                    for index in range(1, 7)
                ],
            }
        )

    @app.post("/api/jobs")
    def create_job():
        payload = request.get_json() or {}
        state["job_requests"].append(payload)
        job = {
            "id": "job-smoke-1",
            "status": "created",
            "totalItems": len(payload.get("selectedRows") or [1]),
            "pendingItems": len(payload.get("selectedRows") or [1]),
            "completedItems": 0,
            "failedItems": 0,
            "points": 100,
            "progress": {"percent": 0},
            "items": [{"index": 1, "status": "pending", "dish": "Smoke Dish"}],
        }
        return jsonify({"ok": True, "job": job, "poll": {"url": "/api/jobs/job-smoke-1", "intervalMs": 1500}}), 201

    @app.post("/api/jobs/<job_id>/run")
    def run_job(job_id: str):
        payload = request.get_json() or {}
        state["run_requests"].append({"job_id": job_id, "payload": payload})
        if live_result == "local-fallback":
            item = {
                "index": 1,
                "status": "completed",
                "provider": "local-demo",
                "action": "LocalFallback",
                "result": {"candidate": {"path": "/tmp/generated-local/mock.jpg", "source": "generated-local"}},
            }
        else:
            item = {
                "index": 1,
                "status": "completed",
                "provider": "tencent-hunyuan",
                "action": "ReplaceBackground",
                "result": {"candidate": {"url": "https://cdn.example.test/final.jpg"}},
            }
        job = {
            "id": job_id,
            "status": "succeeded",
            "totalItems": 1,
            "pendingItems": 0,
            "completedItems": 1,
            "failedItems": 0,
            "progress": {"percent": 100},
            "items": [item],
        }
        return jsonify({"ok": True, "job": job, "poll": {"url": f"/api/jobs/{job_id}", "intervalMs": 1500}})

    @app.post("/api/export")
    def export():
        payload = request.get_json() or {}
        state["export_requests"].append(payload)
        return jsonify({"rows": 3, "images": 3, "platforms": payload.get("platforms", []), "watermark": False, "download": "/download/smoke.zip"})

    @app.get("/download/smoke.zip")
    def download_smoke_zip():
        body = BytesIO()
        with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("delivery_report.xlsx", b"mock report")
            zf.writestr("images/meituan/mock.jpg", b"mock image")
        return body.getvalue(), 200, {"Content-Type": "application/zip"}

    return app, state


class ProductFlowSmokeTests(unittest.TestCase):
    def test_dry_run_uploads_menu_creates_job_and_does_not_run_live_generation(self) -> None:
        app, state = make_mock_app()
        with tempfile.TemporaryDirectory() as tmp:
            menu_file = smoke.create_default_menu_file(tmp)
            report = smoke.run_product_flow(
                FlaskClientAdapter(app),
                menu_file=menu_file,
                base_url="mock://product",
                style_first=True,
                limit=1,
                live_generate=False,
            )

        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["selectedStyle"], "style-1")
        self.assertEqual(state["job_requests"][0]["style"], "style-1")
        self.assertEqual(state["job_requests"][0]["selectedRows"], [1])
        self.assertEqual(state["run_requests"], [])
        self.assertEqual([request["scope"] for request in state["export_requests"]], ["selected", "all", "single", "combo"])
        self.assertEqual([entry["kind"] for entry in state["billing_requests"]], ["recharge", "debit", "debit"])
        self.assertEqual(state["billing_requests"][1]["payload"]["description"], "正式出图验收扣费")
        self.assertEqual(state["billing_requests"][2]["payload"]["description"], "自定义修改验收扣费")
        steps = {step["name"]: step for step in report["steps"]}
        self.assertEqual(steps["job_run_live"]["status"], "skipped")
        self.assertEqual(steps["upload_menu:xls"]["status"], "ok")
        self.assertEqual(steps["upload_menu:xlsx"]["status"], "ok")
        self.assertEqual(steps["menu_summary"]["fields"]["combo"], 1)
        self.assertEqual(steps["single_modify_contract"]["fields"]["customEditPoints"], 150)
        self.assertEqual(steps["result_preview_contract"]["fields"]["rowsWithAction"], 2)
        self.assertEqual(steps["billing_formal_debit"]["fields"]["points"], 200)
        self.assertEqual(steps["single_modify_debit"]["fields"]["points"], 150)
        self.assertEqual(steps["single_image_export"]["status"], "ok")
        self.assertEqual(steps["free_sample_slots"]["fields"]["sampleCount"], 6)
        self.assertEqual(steps["style_catalog"]["fields"]["fixedMissing"], [])
        self.assertEqual(steps["library_status"]["fields"]["remoteIndex"], True)
        self.assertIn("style_preview:style-6", steps)

    def test_live_generate_runs_one_image_and_reports_export_and_image_url(self) -> None:
        app, state = make_mock_app()
        report = smoke.run_product_flow(
            FlaskClientAdapter(app),
            base_url="mock://product",
            style_first=False,
            limit=1,
            live_generate=True,
        )

        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["selectedStyle"], "style-2")
        self.assertEqual(state["run_requests"], [{"job_id": "job-smoke-1", "payload": {"limit": 1, "paid": True, "orderId": state["run_requests"][0]["payload"]["orderId"]}}])
        self.assertEqual(state["export_requests"][0]["platforms"], smoke.DEFAULT_PLATFORMS)
        steps = {step["name"]: step for step in report["steps"]}
        self.assertEqual(steps["job_run_live"]["fields"]["effectiveLimit"], 1)
        self.assertEqual(steps["job_run_live"]["fields"]["imageUrl"], "https://cdn.example.test/final.jpg")
        self.assertEqual(steps["platform_export:all"]["fields"]["images"], 3)
        self.assertEqual(steps["platform_export:single"]["fields"]["zipImages"], 1)
        self.assertEqual(steps["platform_export:combo"]["fields"]["hasDeliveryReport"], True)

    def test_failure_response_is_reported_without_crashing(self) -> None:
        app, _state = make_mock_app(broken_plan=True)
        report = smoke.run_product_flow(
            FlaskClientAdapter(app),
            base_url="mock://product",
            live_generate=False,
        )

        self.assertFalse(report["ok"])
        failures = {item["step"]: item["reason"] for item in report["failures"]}
        self.assertEqual(failures["plan"], "HTTP 500: plan exploded")
        self.assertEqual(failures["style_catalog"], "expected at least 6 style cards, got 0")
        self.assertIn("style_selection", failures)

    def test_missing_required_style_is_a_stable_catalog_failure(self) -> None:
        incomplete_styles = style_cards()[:5]
        app, _state = make_mock_app(styles=incomplete_styles)
        report = smoke.run_product_flow(
            FlaskClientAdapter(app),
            base_url="mock://product",
            live_generate=False,
        )

        self.assertFalse(report["ok"])
        failures = {item["step"]: item["reason"] for item in report["failures"]}
        self.assertEqual(failures["style_catalog"], "expected at least 6 style cards, got 5")

    def test_base_url_aliases_are_normalized_before_http_client_runs(self) -> None:
        local_url, local_note = smoke.normalize_base_url("local")
        render_url, render_note = smoke.normalize_base_url("render")
        bare_url, bare_note = smoke.normalize_base_url("example.onrender.com")

        self.assertEqual(local_url, smoke.DEFAULT_LOCAL_URL)
        self.assertIn("resolved", local_note)
        self.assertEqual(render_url, smoke.DEFAULT_RENDER_URL)
        self.assertIn("resolved", render_note)
        self.assertEqual(bare_url, "https://example.onrender.com")
        self.assertIn("https://", bare_note)

    def test_live_cli_requires_environment_gate_and_limit_one(self) -> None:
        errors = StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), redirect_stderr(errors):
            self.assertEqual(smoke.main(["--base-url", "local", "--live-generate", "--limit", "1"]), 2)
        with mock.patch.dict("os.environ", {smoke.LIVE_ENV_VAR: "1"}, clear=True), redirect_stderr(errors):
            self.assertEqual(smoke.main(["--base-url", "local", "--live-generate", "--limit", "2"]), 2)

    def test_provider_configured_but_local_fallback_result_is_red_flagged(self) -> None:
        app, _state = make_mock_app(live_result="local-fallback")
        report = smoke.run_product_flow(
            FlaskClientAdapter(app),
            base_url="mock://product",
            limit=1,
            live_generate=True,
        )

        self.assertFalse(report["ok"])
        failures = {item["step"]: item["reason"] for item in report["failures"]}
        self.assertIn("seed/mock/local fallback", failures["job_run_live"])
        self.assertEqual(report["redFlags"][0]["step"], "job_run_live")
        markdown = smoke.render_markdown_report(report)
        self.assertIn('style="color:red"', markdown)

    def test_formal_evidence_reads_nested_generation_candidate(self) -> None:
        item = {
            "status": "completed",
            "payload": {
                "generation": {
                    "status": "succeeded",
                    "provider": "tencent-hunyuan",
                    "action": "ReplaceBackground",
                    "candidate": {
                        "source": "tencent-ReplaceBackground",
                        "url": "/media/_ai_outputs/style-1/standard/0003_农家一碗香盖码饭.jpg",
                    },
                }
            },
        }

        evidence = smoke.formal_result_evidence(item, provider_configured=True)

        self.assertEqual(evidence["imageUrl"], "/media/_ai_outputs/style-1/standard/0003_农家一碗香盖码饭.jpg")
        self.assertFalse(evidence["mockOrSeed"])


if __name__ == "__main__":
    unittest.main()
