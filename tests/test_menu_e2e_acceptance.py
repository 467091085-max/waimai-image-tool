from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import provider_capacity
from scripts import menu_e2e_acceptance as acceptance


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "menu_e2e_acceptance.py"
REAL_CONFIRM_ENV = "WAIMAI_E2E_REAL_PROVIDER_CONFIRM"
REAL_CALL_BUDGET_ENV = "WAIMAI_E2E_REAL_PROVIDER_MAX_CALLS"
PROVIDER_SECRET_ENV_NAMES = (
    "TENCENT_TOKENHUB_API_KEY",
    "TOKENHUB_API_KEY",
    "HUNYUAN_TOKENHUB_API_KEY",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENT_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
    "TENCENT_SECRET_KEY",
)


def write_menu(path: Path) -> None:
    pd.DataFrame(
        [
            {
                "分类": "热销",
                "菜品名": "黑椒牛肉饭",
                "价格": 22.8,
                "类型": "单品",
                "套餐内容/规格": "",
            },
            {
                "分类": "套餐",
                "菜品名": "鸡腿饭可乐套餐",
                "价格": 29.8,
                "类型": "套餐/组合",
                "套餐内容/规格": "鸡腿饭；可乐",
            },
        ]
    ).to_excel(path, index=False)


def clean_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop(REAL_CONFIRM_ENV, None)
    env.pop(REAL_CALL_BUDGET_ENV, None)
    for name in PROVIDER_SECRET_ENV_NAMES:
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def run_acceptance(
    *,
    mode: str,
    menu: Path,
    report: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            mode,
            "--menu",
            str(menu),
            "--report",
            str(report),
            "--timeout",
            "120",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def stage(report: dict, name: str) -> dict:
    return next(item for item in report["stages"] if item["name"] == name)


def test_deterministic_acceptance_runs_all_routes_without_production_claim(
    tmp_path: Path,
) -> None:
    menu = tmp_path / "真实菜单.xlsx"
    report_path = tmp_path / "deterministic-report.json"
    write_menu(menu)

    completed = run_acceptance(
        mode="deterministic",
        menu=menu,
        report=report_path,
        env=clean_subprocess_env(),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["productionProviderVerified"] is False
    assert report["productionDeploymentVerified"] is False
    assert all(item["status"] == "PASS" for item in report["stages"])
    assert report["summary"]["providerBoundary"] == "deterministic-local"
    assert report["summary"]["deterministicLocalSmokePassed"] is True
    assert report["summary"]["realProviderSmokePassed"] is False
    assert report["summary"]["menu"]["rowCount"] == 2
    assert report["summary"]["providerCalls"]["styleBackground"] == 6
    assert report["summary"]["providerCalls"]["foregroundGeneration"] >= 6
    assert report["summary"]["providerCalls"]["maskExtraction"] >= 6
    assert stage(report, "backgrounds")["details"]["generatedCount"] == 6
    assert stage(report, "backgrounds")["details"]["uniqueSha256Count"] == 6
    assert stage(report, "free-samples")["details"]["generatedCount"] == 6
    assert (
        stage(report, "free-samples")["details"]["backgroundShaConsistent"]
        is True
    )
    manifest = stage(report, "manifest")["details"]
    assert manifest["rowCount"] == 2
    assert manifest["generation"] == {
        "failed": 0,
        "pending": 0,
        "succeeded": 2,
    }
    assert manifest["generationEvidence"]["accountedRowCount"] == 2
    assert manifest["generationEvidence"]["foregroundProviderCalls"] <= 2
    assert manifest["assetShaVerifiedCount"] == 2
    assert manifest["backgroundIdentityVerifiedCount"] == 2
    assert len(manifest["manifestBackgroundSha256Values"]) == 1
    export = stage(report, "export")["details"]
    assert export["apiImages"] == 2
    assert export["imageEntryCount"] == 2
    assert export["deliveryReportRows"] == 2
    assert export["outputDimensions"] == {"800x600": 2}
    assert "production provider was not verified" in completed.stdout


def test_real_mode_blocks_before_app_import_and_redacts_credentials(
    tmp_path: Path,
) -> None:
    menu = tmp_path / "真实菜单.xlsx"
    report_path = tmp_path / "real-blocked-report.json"
    write_menu(menu)
    env = clean_subprocess_env()
    sentinel = "SENTINEL_PROVIDER_SECRET_MUST_NOT_LEAK"
    env.update(
        {
            "TENCENT_TOKENHUB_API_KEY": sentinel,
            "TENCENTCLOUD_SECRET_ID": sentinel,
            "TENCENTCLOUD_SECRET_KEY": sentinel,
            "TENCENT_HUNYUAN_ENABLED": "true",
            "TENCENT_COS_BUCKET": "acceptance-test-bucket",
            REAL_CALL_BUDGET_ENV: "9999",
        }
    )

    completed = run_acceptance(
        mode="real",
        menu=menu,
        report=report_path,
        env=env,
    )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    raw_report = report_path.read_text(encoding="utf-8")
    assert sentinel not in raw_report
    report = json.loads(raw_report)
    assert report["status"] == "BLOCKED"
    assert report["productionProviderVerified"] is False
    assert report["productionDeploymentVerified"] is False
    assert report["summary"]["providerCalls"] == 0
    assert stage(report, "preflight")["status"] == "BLOCKED"
    assert stage(report, "preflight")["details"]["confirmationConfigured"] is False
    assert all(
        stage(report, name)["status"] == "SKIP"
        for name in (
            "upload",
            "plan",
            "backgrounds",
            "selected-background",
            "free-samples",
            "formal-generation",
            "manifest",
            "export",
        )
    )
    assert "zero provider calls made" in completed.stdout


def test_real_hundred_image_report_emits_capacity_evidence_contract(
    tmp_path: Path,
) -> None:
    report = acceptance.AcceptanceReport(
        mode="real",
        report_path=tmp_path / "real-100-report.json",
    )
    report.add_stage(
        "formal-generation",
        acceptance.PASS,
        elapsed_seconds=1700,
        details={
            "jobId": "real-paid-job-100",
            "peakProviderConcurrency": 10,
        },
    )
    report.add_stage(
        "manifest",
        acceptance.PASS,
        elapsed_seconds=100,
        details={"outputManifestSha256": "a" * 64},
    )

    evidence = acceptance.write_capacity_evidence(
        report,
        row_count=100,
        provider_model="hy-image-v3",
    )

    assert evidence is not None
    capacity = provider_capacity.GenerationCapacity.from_env(
        {
            "TENCENT_TOKENHUB_VERIFIED_CONCURRENCY": "10",
            "TENCENT_TOKENHUB_MEASURED_P95_SECONDS": "30",
            "GENERATION_TARGET_BATCH_EVIDENCE_FILE": evidence["path"],
            "GENERATION_TARGET_BATCH_EVIDENCE_SHA256": evidence["sha256"],
        }
    )
    contract = capacity.public_contract()
    assert contract["productionTargetVerified"] is True
    assert contract["targetBatchEvidence"]["runId"] == "real-paid-job-100"


def test_real_acceptance_environment_allows_ten_row_workers(
    tmp_path: Path,
) -> None:
    with acceptance.isolated_environment(
        tmp_path,
        row_count=100,
        mode="real",
    ):
        assert acceptance.os.environ["FINAL_GENERATION_WORKERS"] == "10"

    with acceptance.isolated_environment(
        tmp_path,
        row_count=100,
        mode="deterministic",
    ):
        assert acceptance.os.environ["FINAL_GENERATION_WORKERS"] == "2"
