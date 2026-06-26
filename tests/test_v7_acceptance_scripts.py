from __future__ import annotations

import argparse
import unittest

from scripts import smoke_hunyuan_live
from scripts import smoke_product_flow


class V7AcceptanceScriptTests(unittest.TestCase):
    def test_step_log_keeps_skips_separate_from_failures(self) -> None:
        log = smoke_product_flow.StepLog()
        log.add("passed_gate", True)
        log.add("env_gate", True, skipped=True, reason="Render env is not configured")
        log.add("failed_gate", False, reason="missing real gallery")

        failures = [step for step in log.steps if not step["ok"]]
        skips = [step for step in log.steps if step["status"] == "skipped"]

        self.assertEqual([step["name"] for step in failures], ["failed_gate"])
        self.assertEqual([step["name"] for step in skips], ["env_gate"])

    def test_stdout_summary_exposes_skipped_gates(self) -> None:
        report = {
            "ok": False,
            "baseUrl": "https://waimai-image-tool-1.onrender.com",
            "summary": {"passed": 1, "failed": 1, "skipped": 1, "total": 3},
            "failures": [{"step": "real_gallery_runtime", "reason": "missing real gallery"}],
            "skips": [{"step": "gallery_upload_env", "reason": "GALLERY_UPLOAD_TOKEN missing"}],
            "redFlags": [],
        }

        summary = smoke_product_flow.stdout_summary(report)

        self.assertEqual(summary["skips"], report["skips"])
        self.assertEqual(summary["failures"], report["failures"])

    def test_hunyuan_live_skips_without_creating_job_when_env_missing(self) -> None:
        class MissingEnvClient:
            def __init__(self, _base_url: str = "") -> None:
                self.posted = False

            def get_json(self, _path: str):
                return 200, {
                    "provider": "tencent-hunyuan",
                    "configured": False,
                    "cosReady": False,
                    "missing": ["TENCENTCLOUD_SECRET_ID"],
                }

            def post_json(self, _path: str, _payload: dict):
                self.posted = True
                raise AssertionError("live smoke must not create a job when env is missing")

        original_client = smoke_hunyuan_live.SmokeClient
        try:
            smoke_hunyuan_live.SmokeClient = MissingEnvClient  # type: ignore[assignment]
            result = smoke_hunyuan_live.run_smoke(
                argparse.Namespace(
                    base_url="https://waimai-image-tool-1.onrender.com",
                    live=True,
                    limit=1,
                    style="style-1",
                    quality="standard",
                    dish="招牌辣椒炒肉",
                )
            )
        finally:
            smoke_hunyuan_live.SmokeClient = original_client  # type: ignore[assignment]

        self.assertEqual(result["acceptanceStatus"], "skipped")
        self.assertTrue(result["skipped"])
        self.assertFalse(result["willCreateJob"])
        self.assertFalse(result["willRunProvider"])


if __name__ == "__main__":
    unittest.main()
