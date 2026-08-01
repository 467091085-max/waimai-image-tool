#!/usr/bin/env python3
"""Repeatable real-menu acceptance flow with a fail-closed paid-provider gate."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterator
from unittest import mock
from urllib.parse import urlencode, urlsplit
import zipfile

from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_MENU_DIR = Path.home() / "Documents" / "menus"
REAL_CONFIRM_ENV = "WAIMAI_E2E_REAL_PROVIDER_CONFIRM"
REAL_CONFIRM_VALUE = "I_ACCEPT_REAL_PROVIDER_CHARGES"
REAL_CALL_BUDGET_ENV = "WAIMAI_E2E_REAL_PROVIDER_MAX_CALLS"
STAGE_NAMES = (
    "preflight",
    "upload",
    "plan",
    "backgrounds",
    "selected-background",
    "free-samples",
    "formal-generation",
    "manifest",
    "export",
)
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
BLOCKED = "BLOCKED"
TERMINAL_JOB_STATUSES = {"completed", "failed", "canceled"}
SECRET_ENV_NAMES = (
    "TENCENT_TOKENHUB_API_KEY",
    "TOKENHUB_API_KEY",
    "HUNYUAN_TOKENHUB_API_KEY",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENT_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
    "TENCENT_SECRET_KEY",
)
ISOLATED_CLEAR_ENV_NAMES = (
    "DATABASE_URL",
    "REDIS_URL",
    "RENDER",
    "RENDER_SERVICE_ID",
    "RENDER_EXTERNAL_URL",
    "OBJECT_SIGNING_SECRET",
    "ASSET_SIGNING_SECRET",
    "DOWNLOAD_SIGNING_SECRET",
    "GENERATION_API_TOKEN",
    "BILLING_API_TOKEN",
    "ADMIN_API_TOKEN",
)


class AcceptanceError(RuntimeError):
    def __init__(
        self,
        stage: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_text(value: Any) -> str:
    text = str(value or "")
    for name in SECRET_ENV_NAMES:
        secret = str(os.environ.get(name) or "").strip()
        if len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)((?:api[_-]?key|secret(?:id|key)?|token)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    return text[:1200]


def safe_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): safe_json_value(item)
            for key, item in value.items()
            if str(key) not in SECRET_ENV_NAMES
        }
    if isinstance(value, list):
        return [safe_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [safe_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return redact_text(value)
    return value


class AcceptanceReport:
    def __init__(self, *, mode: str, report_path: Path) -> None:
        self.path = report_path.resolve()
        self.data: dict[str, Any] = {
            "schemaVersion": 1,
            "startedAt": utc_now(),
            "finishedAt": None,
            "mode": mode,
            "status": "RUNNING",
            "claimBoundary": {
                "deterministic": (
                    "Local deterministic provider-boundary smoke only; "
                    "it is not evidence of a production AI provider call."
                ),
                "real": (
                    "Real provider smoke is valid only after the explicit "
                    "paid-call gate and credential preflight pass."
                ),
            },
            "productionProviderVerified": False,
            "productionDeploymentVerified": False,
            "stages": [],
            "summary": {},
        }

    def add_stage(
        self,
        name: str,
        status: str,
        *,
        elapsed_seconds: float = 0.0,
        details: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        record = {
            "name": name,
            "status": status,
            "elapsedSeconds": round(max(0.0, elapsed_seconds), 3),
            "details": safe_json_value(details or {}),
        }
        if error:
            record["error"] = redact_text(error)
        self.data["stages"].append(record)
        suffix = ""
        if details:
            concise = details.get("summary") or details.get("reason") or ""
            if concise:
                suffix = f" - {redact_text(concise)}"
        if error:
            suffix = f" - {redact_text(error)}"
        print(f"[{status}] {name}{suffix}", flush=True)
        self.write()

    def finish(
        self,
        status: str,
        *,
        summary: dict[str, Any],
        production_provider_verified: bool = False,
    ) -> None:
        self.data["finishedAt"] = utc_now()
        self.data["status"] = status
        self.data["productionProviderVerified"] = bool(
            production_provider_verified
        )
        self.data["summary"] = safe_json_value(summary)
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def default_report_path(mode: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "scripts" / "reports" / f"menu-e2e-{mode}-{timestamp}.json"


@contextmanager
def retained_work_directory(path: Path) -> Iterator[Path]:
    yield path


def resolve_menu_path(menu_value: str, menu_dir_value: str) -> Path:
    if menu_value:
        path = Path(menu_value).expanduser().resolve()
    else:
        menu_dir = Path(menu_dir_value).expanduser().resolve()
        candidates = sorted(
            (
                path
                for path in menu_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".xls", ".xlsx"}
            ),
            key=lambda path: path.name,
        )
        if not candidates:
            raise FileNotFoundError(
                f"no .xls/.xlsx files found under {menu_dir}"
            )
        path = candidates[0]
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() not in {".xls", ".xlsx"}:
        raise ValueError("menu must be an .xls or .xlsx file")
    return path


def parse_real_menu(path: Path) -> dict[str, Any]:
    from menu_parser import parse_menu

    menu = parse_menu(path)
    items = menu.get("items") if isinstance(menu.get("items"), list) else []
    count = int(menu.get("count") or len(items))
    if count <= 0 or not items:
        raise ValueError("menu parser returned no dishes")
    if count != len(items):
        raise ValueError(
            f"menu parser count mismatch: count={count}, items={len(items)}"
        )
    return menu


def menu_evidence(path: Path, menu: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "row": item.get("row"),
            "name": str(item.get("name") or ""),
            "kind": str(item.get("kind") or ""),
            "category": str(item.get("category") or ""),
        }
        for item in menu["items"]
    ]
    return {
        "menuPath": str(path),
        "menuFilename": path.name,
        "menuSha256": file_sha256(path),
        "menuBytes": path.stat().st_size,
        "store": str(menu.get("store") or ""),
        "rowCount": len(rows),
        "dishNames": [row["name"] for row in rows],
        "menuRows": rows,
        "kindCounts": menu.get("kindCounts") or {},
        "categoryDetection": menu.get("categoryDetection") or {},
    }


def env_has_any(*names: str) -> bool:
    return any(str(os.environ.get(name) or "").strip() for name in names)


def env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def estimated_real_provider_calls(row_count: int) -> int:
    # Six backgrounds, then generation plus Mask for six samples and every
    # formal row. The estimate intentionally ignores cache reuse.
    return 6 + (2 * 6) + (2 * max(0, int(row_count)))


def real_provider_preflight(row_count: int) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    confirm_matches = (
        os.environ.get(REAL_CONFIRM_ENV, "").strip() == REAL_CONFIRM_VALUE
    )
    if not confirm_matches:
        reasons.append(
            f"{REAL_CONFIRM_ENV} must equal {REAL_CONFIRM_VALUE}"
        )

    estimated_calls = estimated_real_provider_calls(row_count)
    raw_budget = os.environ.get(REAL_CALL_BUDGET_ENV, "").strip()
    try:
        call_budget = int(raw_budget)
    except (TypeError, ValueError):
        call_budget = 0
    if call_budget < estimated_calls:
        reasons.append(
            f"{REAL_CALL_BUDGET_ENV} must be at least {estimated_calls}"
        )

    tokenhub_ready = env_has_any(
        "TENCENT_TOKENHUB_API_KEY",
        "TOKENHUB_API_KEY",
        "HUNYUAN_TOKENHUB_API_KEY",
    )
    cloud_enabled = env_truthy("TENCENT_HUNYUAN_ENABLED") or env_truthy(
        "TENCENT_AIART_ENABLED"
    )
    cloud_secret_id = env_has_any(
        "TENCENTCLOUD_SECRET_ID",
        "TENCENT_SECRET_ID",
    )
    cloud_secret_key = env_has_any(
        "TENCENTCLOUD_SECRET_KEY",
        "TENCENT_SECRET_KEY",
    )
    cos_bucket = bool(os.environ.get("TENCENT_COS_BUCKET", "").strip())
    if not tokenhub_ready:
        reasons.append("TokenHub API key is not configured")
    if not cloud_enabled:
        reasons.append(
            "TENCENT_HUNYUAN_ENABLED=true is required for the Mask provider"
        )
    if not cloud_secret_id or not cloud_secret_key:
        reasons.append("Tencent Cloud SecretId/SecretKey are incomplete")
    if not cos_bucket:
        reasons.append(
            "TENCENT_COS_BUCKET is required for provider-readable Mask inputs"
        )
    try:
        __import__("qcloud_cos")
        cos_sdk = True
    except ImportError:
        cos_sdk = False
        reasons.append("cos-python-sdk-v5 is not installed")

    return reasons, {
        "confirmationConfigured": confirm_matches,
        "callBudget": call_budget,
        "estimatedMaximumProviderCalls": estimated_calls,
        "credentials": {
            "tokenhubKeyConfigured": tokenhub_ready,
            "cloudApiEnabled": cloud_enabled,
            "cloudSecretIdConfigured": cloud_secret_id,
            "cloudSecretKeyConfigured": cloud_secret_key,
            "cosBucketConfigured": cos_bucket,
            "cosSdkInstalled": cos_sdk,
        },
        "secretsRedacted": True,
    }


@contextmanager
def isolated_environment(
    root: Path,
    *,
    row_count: int,
    mode: str,
) -> Iterator[None]:
    original = dict(os.environ)
    try:
        for name in ISOLATED_CLEAR_ENV_NAMES:
            os.environ.pop(name, None)
        if mode == "deterministic":
            for name in SECRET_ENV_NAMES:
                os.environ.pop(name, None)
            for name in (
                "TENCENT_COS_BUCKET",
                "TENCENT_COS_REGION",
                "TENCENT_COS_PREFIX",
            ):
                os.environ.pop(name, None)
        os.environ.update(
            {
                "APP_ENV": "development",
                "APP_DB_PATH": str(root / "app.db"),
                "STORAGE_DB_PATH": str(root / "app.db"),
                "BILLING_DB_PATH": str(root / "billing.db"),
                "OBJECT_STORAGE_PROVIDER": "local",
                "OBJECT_STORE_DIR": str(root / "objects"),
                "ENABLE_LOCAL_DEMO_AUTH": "true",
                "ENABLE_LOCAL_DEMO_BILLING": "true",
                "ENABLE_LOCAL_DEMO_GENERATION": "true",
                "ENABLE_LOCAL_DEMO_OBJECTS": "true",
                "ENABLE_LOCAL_DEMO_STORAGE": "true",
                "PRODUCT_POSTGRES_ENABLED": "false",
                "ALLOW_LOCAL_BACKGROUND_FALLBACK": "false",
                "ALLOW_LOCAL_PREVIEW_FALLBACK": "false",
                "ALLOW_LOCAL_FINAL_FALLBACK": "false",
                "AI_ASSET_LIBRARY_ENABLED": "false",
                "AI_ASSET_UPLOAD_TO_COS": "false",
                "AI_FIRST_GENERATION": "true",
                "GENERATE_STYLE_BACKGROUNDS_WITH_TENCENT": "true",
                "GENERATE_PREVIEW_SAMPLES_WITH_TENCENT": "true",
                "REQUIRE_SELECTED_BACKGROUND_IDENTITY": "true",
                "DEMO_BALANCE_POINTS": str(
                    max(1880, (max(1, row_count) * 20) + 1000)
                ),
                "TENCENT_HUNYUAN_SYNC_LIMIT": str(max(6, row_count)),
                "FINAL_GENERATION_WORKERS": (
                    "1" if mode == "real" else "2"
                ),
            }
        )
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def configure_app_paths(app_module: Any, root: Path) -> None:
    path_values = {
        "UPLOAD_DIR": root / "uploads",
        "LIBRARY_DIR": root / "library",
        "EXPORT_DIR": root / "exports",
        "MODEL_INPUT_DIR": root / "model_inputs",
        "AI_ASSET_DIR": root / "library" / "_ai_asset_library",
    }
    for name, path in path_values.items():
        path.mkdir(parents=True, exist_ok=True)
        setattr(app_module, name, path)
    app_module.TENCENT_SYNC_LIMIT = max(
        6,
        int(os.environ.get("TENCENT_HUNYUAN_SYNC_LIMIT", "6")),
    )
    app_module.FINAL_GENERATION_WORKERS = max(
        1,
        int(os.environ.get("FINAL_GENERATION_WORKERS", "1")),
    )
    app_module.library_images.cache_clear()
    app_module.app.config.update(TESTING=True)


def deterministic_palette(seed_text: str) -> tuple[tuple[int, int, int], ...]:
    raw = hashlib.sha256(seed_text.encode("utf-8")).digest()
    colors = []
    for offset in (0, 3, 6, 9, 12):
        colors.append(
            (
                45 + (raw[offset] % 180),
                45 + (raw[offset + 1] % 180),
                45 + (raw[offset + 2] % 180),
            )
        )
    return tuple(colors)


def write_deterministic_background(
    target: Path,
    *,
    seed_text: str,
) -> None:
    width, height = 1024, 768
    colors = deterministic_palette(seed_text)
    image = Image.new("RGB", (width, height), colors[0])
    draw = ImageDraw.Draw(image)
    for index in range(24):
        x0 = (index * 83) % width
        y0 = (index * 131) % height
        x1 = min(width, x0 + 180 + ((index * 17) % 210))
        y1 = min(height, y0 + 90 + ((index * 29) % 150))
        draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=22,
            fill=colors[(index + 1) % len(colors)],
        )
    for index in range(14):
        inset = 12 + (index * 25)
        draw.ellipse(
            (inset, inset, width - inset, height - inset),
            outline=colors[(index + 2) % len(colors)],
            width=5,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=94, optimize=True)


def write_deterministic_foreground(
    target: Path,
    *,
    seed_text: str,
) -> None:
    width, height = 1024, 768
    colors = deterministic_palette(seed_text)
    image = Image.new("RGB", (width, height), colors[0])
    draw = ImageDraw.Draw(image)
    draw.ellipse((120, 70, 904, 735), fill=colors[1])
    draw.ellipse((175, 120, 849, 690), fill=colors[2])
    for index in range(26):
        x = 240 + ((index * 97) % 540)
        y = 175 + ((index * 61) % 410)
        radius = 24 + ((index * 7) % 52)
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=colors[(index + 3) % len(colors)],
        )
    draw.arc((200, 130, 824, 710), 10, 170, fill=(248, 246, 238), width=14)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "JPEG", quality=94, optimize=True)


def write_deterministic_mask(target: Path) -> None:
    mask = Image.new("L", (1024, 768), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((120, 70, 904, 735), fill=255)
    target.parent.mkdir(parents=True, exist_ok=True)
    mask.save(target, "PNG", optimize=True)


@contextmanager
def provider_boundary(
    app_module: Any,
    *,
    mode: str,
) -> Iterator[dict[str, int]]:
    counters = {
        "styleBackground": 0,
        "foregroundGeneration": 0,
        "maskExtraction": 0,
    }
    with ExitStack() as stack:
        if mode == "deterministic":
            def fake_style(style_id: str, target: Path) -> dict[str, Any]:
                counters["styleBackground"] += 1
                write_deterministic_background(
                    target,
                    seed_text=f"background:{style_id}",
                )
                return {
                    "provider": "deterministic-local",
                    "action": "DeterministicProviderBackground",
                    "promptType": "style_background",
                    "requestId": f"local-bg-{style_id}",
                    "model": "deterministic-local-v1",
                }

            def fake_foreground(
                row: dict[str, Any],
                style_id: str,
                quality: str | None,
                target: Path,
                selected_background: Any | None = None,
            ) -> dict[str, Any]:
                del selected_background
                counters["foregroundGeneration"] += 1
                write_deterministic_foreground(
                    target,
                    seed_text=(
                        f"foreground:{row.get('row')}:{row.get('name')}:"
                        f"{style_id}:{quality}"
                    ),
                )
                return {
                    "provider": "deterministic-local",
                    "action": "DeterministicProviderForeground",
                    "promptType": (
                        "combo"
                        if row.get("kind") == "套餐/组合"
                        else "text_to_image"
                    ),
                    "requestId": (
                        f"local-fg-{row.get('row')}-"
                        f"{hashlib.sha1(str(row.get('name')).encode()).hexdigest()[:8]}"
                    ),
                    "model": "deterministic-local-v1",
                    "referenceConditioned": False,
                    "backgroundIdentityVerified": False,
                }

            def fake_mask(
                row: dict[str, Any],
                foreground_path: Path,
                target: Path,
            ) -> dict[str, Any]:
                del foreground_path
                counters["maskExtraction"] += 1
                write_deterministic_mask(target)
                return {
                    "provider": "deterministic-local",
                    "action": "DeterministicProviderMask",
                    "requestId": f"local-mask-{row.get('row')}",
                }

            stack.enter_context(
                mock.patch.object(app_module, "tencent_ready", return_value=True)
            )
            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_status_payload",
                    return_value={
                        "provider": "deterministic-local",
                        "configured": True,
                        "tokenhubReady": False,
                        "cloudApiReady": False,
                        "cosReady": False,
                        "missing": [],
                    },
                )
            )
            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_style_background",
                    side_effect=fake_style,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_text_to_image",
                    side_effect=fake_foreground,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_extract_foreground_mask",
                    side_effect=fake_mask,
                )
            )
        else:
            original_style = app_module.tencent_style_background
            original_foreground = app_module.tencent_text_to_image
            original_mask = app_module.tencent_extract_foreground_mask
            call_budget = int(
                os.environ.get(REAL_CALL_BUDGET_ENV, "0") or "0"
            )

            def reserve_real_provider_call(kind: str) -> None:
                attempted = sum(counters.values()) + 1
                if attempted > call_budget:
                    raise RuntimeError(
                        "real provider call budget exhausted before call"
                    )
                counters[kind] += 1

            def counted_style(style_id: str, target: Path) -> dict[str, Any]:
                reserve_real_provider_call("styleBackground")
                return original_style(style_id, target)

            def counted_foreground(*args: Any, **kwargs: Any) -> dict[str, Any]:
                reserve_real_provider_call("foregroundGeneration")
                return original_foreground(*args, **kwargs)

            def counted_mask(*args: Any, **kwargs: Any) -> dict[str, Any]:
                reserve_real_provider_call("maskExtraction")
                return original_mask(*args, **kwargs)

            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_style_background",
                    side_effect=counted_style,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_text_to_image",
                    side_effect=counted_foreground,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    app_module,
                    "tencent_extract_foreground_mask",
                    side_effect=counted_mask,
                )
            )
        yield counters


def response_json(response: Any, stage: str) -> dict[str, Any]:
    body = response.get_json(silent=True)
    if response.status_code < 200 or response.status_code >= 300:
        raise AcceptanceError(
            stage,
            (
                f"HTTP {response.status_code}: "
                f"{redact_text(body if body is not None else response.data)}"
            ),
            details={"httpStatus": response.status_code},
        )
    if not isinstance(body, dict):
        raise AcceptanceError(
            stage,
            f"expected JSON object, got HTTP {response.status_code}",
        )
    return body


def required_text(
    value: Any,
    *,
    stage: str,
    field: str,
) -> str:
    text = str(value or "").strip()
    if not text:
        raise AcceptanceError(stage, f"missing {field}")
    return text


def require_sha256(value: Any, *, stage: str, field: str) -> str:
    digest = required_text(value, stage=stage, field=field).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise AcceptanceError(stage, f"invalid {field}: {digest[:16]}")
    return digest


def fetch_image(
    client: Any,
    url: str,
    *,
    stage: str,
) -> tuple[bytes, tuple[int, int]]:
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        raise AcceptanceError(
            stage,
            "acceptance payload returned an external image URL",
            details={"urlHost": parsed.netloc},
        )
    response = client.get(url)
    if response.status_code != 200:
        raise AcceptanceError(
            stage,
            f"image fetch failed with HTTP {response.status_code}",
            details={"path": parsed.path},
        )
    content_type = str(response.headers.get("Content-Type") or "")
    if not content_type.startswith("image/"):
        raise AcceptanceError(
            stage,
            f"image response has invalid Content-Type {content_type}",
        )
    raw = bytes(response.data)
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            size = image.size
    except Exception as exc:
        raise AcceptanceError(
            stage,
            f"image payload is unreadable: {type(exc).__name__}",
        ) from exc
    return raw, size


def run_stage(
    report: AcceptanceReport,
    name: str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        details = operation()
    except AcceptanceError as exc:
        report.add_stage(
            name,
            FAIL,
            elapsed_seconds=time.monotonic() - started,
            details=exc.details,
            error=str(exc),
        )
        raise
    except Exception as exc:
        report.add_stage(
            name,
            FAIL,
            elapsed_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {redact_text(exc)}",
        )
        raise AcceptanceError(
            name,
            f"{type(exc).__name__}: {redact_text(exc)}",
        ) from exc
    report.add_stage(
        name,
        PASS,
        elapsed_seconds=time.monotonic() - started,
        details=details,
    )
    return details


def skip_remaining_stages(
    report: AcceptanceReport,
    *,
    after_stage: str,
    reason: str,
    status: str = SKIP,
) -> None:
    seen = False
    existing = {
        str(record.get("name") or "")
        for record in report.data.get("stages", [])
    }
    for name in STAGE_NAMES:
        if name == after_stage:
            seen = True
            continue
        if not seen or name in existing:
            continue
        report.add_stage(
            name,
            status,
            details={"reason": reason, "summary": reason},
        )


def manifest_candidate(row: dict[str, Any]) -> dict[str, Any]:
    candidates = row.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return {}
    return candidates[0] if isinstance(candidates[0], dict) else {}


def validate_manifest(
    client: Any,
    manifest: dict[str, Any],
    *,
    expected_names: list[str],
    selected_sha: str,
) -> dict[str, Any]:
    stage = "manifest"
    rows = manifest.get("results")
    if not isinstance(rows, list):
        raise AcceptanceError(stage, "manifest results are missing")
    if len(rows) != len(expected_names):
        raise AcceptanceError(
            stage,
            (
                f"manifest row count mismatch: expected {len(expected_names)}, "
                f"got {len(rows)}"
            ),
        )
    actual_names = [str(row.get("name") or "") for row in rows]
    if Counter(actual_names) != Counter(expected_names):
        raise AcceptanceError(
            stage,
            "manifest dish names do not match the parsed Excel menu",
            details={
                "expectedDishNames": expected_names,
                "actualDishNames": actual_names,
            },
        )

    generation = (
        manifest.get("generation")
        if isinstance(manifest.get("generation"), dict)
        else {}
    )
    succeeded = int(generation.get("succeeded") or 0)
    failed = int(generation.get("failed") or 0)
    pending = int(generation.get("pending") or 0)
    if succeeded != len(rows) or failed != 0 or pending != 0:
        raise AcceptanceError(
            stage,
            (
                "formal generation is incomplete: "
                f"succeeded={succeeded}, failed={failed}, pending={pending}"
            ),
            details={"generation": generation},
        )

    output_sizes: Counter[str] = Counter()
    output_digests: list[str] = []
    background_shas: set[str] = set()
    identity_verified = 0
    foreground_provider_calls = 0
    exact_product_reuses = 0
    preview_reuses = 0
    final_cache_reuses = 0
    asset_reuses = 0
    unclassified_generation_rows = 0
    for row in rows:
        candidate = manifest_candidate(row)
        row_generation = (
            row.get("generation")
            if isinstance(row.get("generation"), dict)
            else {}
        )
        row_status = str(row_generation.get("status") or "")
        row_action = str(row_generation.get("action") or "")
        if row_action == "PreviewReuse":
            preview_reuses += 1
        elif row_action == "ApprovedAssetReuse":
            asset_reuses += 1
        elif row_status in {"cached", "reused"}:
            final_cache_reuses += 1
        else:
            provider_detail = (
                candidate.get("tencent")
                if isinstance(candidate.get("tencent"), dict)
                else {}
            )
            composition = (
                provider_detail.get("composition")
                if isinstance(provider_detail.get("composition"), dict)
                else {}
            )
            if composition.get("foregroundCached") is True:
                exact_product_reuses += 1
            elif composition.get("foregroundCached") is False:
                foreground_provider_calls += 1
            else:
                unclassified_generation_rows += 1
        url = required_text(
            candidate.get("url"),
            stage=stage,
            field=f"asset URL for {row.get('name')}",
        )
        candidate_background_sha = require_sha256(
            candidate.get("backgroundSha256"),
            stage=stage,
            field=f"backgroundSha256 for {row.get('name')}",
        )
        background_shas.add(candidate_background_sha)
        if candidate_background_sha != selected_sha:
            raise AcceptanceError(
                stage,
                f"background SHA mismatch for {row.get('name')}",
            )
        if not (
            candidate.get("backgroundIdentityVerified") is True
            and candidate.get("persistedOutputBackgroundVerified") is True
        ):
            raise AcceptanceError(
                stage,
                f"background identity proof is missing for {row.get('name')}",
            )
        identity_verified += 1
        expected_output_sha = require_sha256(
            candidate.get("outputSha256"),
            stage=stage,
            field=f"outputSha256 for {row.get('name')}",
        )
        raw, size = fetch_image(client, url, stage=stage)
        actual_output_sha = sha256_bytes(raw)
        if actual_output_sha != expected_output_sha:
            raise AcceptanceError(
                stage,
                f"asset SHA mismatch for {row.get('name')}",
            )
        output_digests.append(actual_output_sha)
        output_sizes[f"{size[0]}x{size[1]}"] += 1

    if unclassified_generation_rows:
        raise AcceptanceError(
            stage,
            (
                "formal provider-call evidence is incomplete for "
                f"{unclassified_generation_rows} row(s)"
            ),
        )

    return {
        "summary": (
            f"{len(rows)} formal images; selected background SHA consistent"
        ),
        "rowCount": len(rows),
        "dishNames": actual_names,
        "generation": {
            "succeeded": succeeded,
            "failed": failed,
            "pending": pending,
        },
        "generationEvidence": {
            "foregroundProviderCalls": foreground_provider_calls,
            "exactProductReuseCount": exact_product_reuses,
            "previewReuseCount": preview_reuses,
            "finalCacheReuseCount": final_cache_reuses,
            "approvedAssetReuseCount": asset_reuses,
            "accountedRowCount": (
                foreground_provider_calls
                + exact_product_reuses
                + preview_reuses
                + final_cache_reuses
                + asset_reuses
            ),
        },
        "assetDownloadCount": len(output_digests),
        "assetShaVerifiedCount": len(output_digests),
        "uniqueOutputSha256Count": len(set(output_digests)),
        "selectedBackgroundSha256": selected_sha,
        "manifestBackgroundSha256Values": sorted(background_shas),
        "backgroundIdentityVerifiedCount": identity_verified,
        "outputDimensions": dict(output_sizes),
    }


def validate_export_zip(
    raw_zip: bytes,
    *,
    expected_row_count: int,
    platforms: list[str],
) -> dict[str, Any]:
    expected_dimensions = {
        "meituan": (800, 600),
        "taobao": (800, 800),
        "jd": (800, 800),
    }
    expected_images = expected_row_count * len(platforms)
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        names = archive.namelist()
        if "delivery_report.xlsx" not in names:
            raise AcceptanceError("export", "delivery_report.xlsx is missing")
        image_names = [
            name
            for name in names
            if name.startswith("images/") and not name.endswith("/")
        ]
        if len(image_names) != expected_images:
            raise AcceptanceError(
                "export",
                (
                    f"export image count mismatch: expected {expected_images}, "
                    f"got {len(image_names)}"
                ),
            )
        dimensions: Counter[str] = Counter()
        for name in image_names:
            platform = next(
                (
                    platform_id
                    for platform_id in platforms
                    if f"images/{platform_id}_" in name
                ),
                "",
            )
            if not platform:
                raise AcceptanceError(
                    "export",
                    f"unrecognized platform folder in {name}",
                )
            with Image.open(io.BytesIO(archive.read(name))) as image:
                image.load()
                if image.size != expected_dimensions[platform]:
                    raise AcceptanceError(
                        "export",
                        f"invalid {platform} dimensions for {name}: {image.size}",
                    )
                dimensions[f"{image.width}x{image.height}"] += 1

        import pandas as pd

        report_frame = pd.read_excel(
            io.BytesIO(archive.read("delivery_report.xlsx"))
        )
        if len(report_frame.index) != expected_images:
            raise AcceptanceError(
                "export",
                (
                    "delivery report row count mismatch: "
                    f"expected {expected_images}, got {len(report_frame.index)}"
                ),
            )
    return {
        "zipSha256": sha256_bytes(raw_zip),
        "zipBytes": len(raw_zip),
        "zipEntryCount": len(names),
        "imageEntryCount": len(image_names),
        "deliveryReportRows": expected_images,
        "outputDimensions": dict(dimensions),
    }


def execute_pipeline(
    *,
    app_module: Any,
    menu_path: Path,
    parsed_menu: dict[str, Any],
    report: AcceptanceReport,
    mode: str,
    quality: str,
    platforms: list[str],
    style_index: int,
    timeout_seconds: float,
    expected_category_id: str = "",
    require_approved_catalog: bool = False,
) -> dict[str, Any]:
    client = app_module.app.test_client()
    expected_count = int(parsed_menu["count"])
    expected_names = [
        str(item.get("name") or "") for item in parsed_menu["items"]
    ]
    state: dict[str, Any] = {}

    def upload() -> dict[str, Any]:
        response = client.post(
            "/api/upload-menu",
            data={
                "file": (
                    io.BytesIO(menu_path.read_bytes()),
                    menu_path.name,
                )
            },
            content_type="multipart/form-data",
        )
        body = response_json(response, "upload")
        uploaded_count = int(
            (
                body.get("menu")
                if isinstance(body.get("menu"), dict)
                else {}
            ).get("count")
            or 0
        )
        if uploaded_count != expected_count:
            raise AcceptanceError(
                "upload",
                (
                    f"uploaded menu count mismatch: expected {expected_count}, "
                    f"got {uploaded_count}"
                ),
            )
        state["menuUploadId"] = required_text(
            body.get("menuUploadId"),
            stage="upload",
            field="menuUploadId",
        )
        return {
            "summary": f"uploaded {uploaded_count} dishes",
            "menuUploadId": state["menuUploadId"],
            "rowCount": uploaded_count,
            "dishNames": expected_names,
            "sourceSha256": file_sha256(menu_path),
        }

    run_stage(report, "upload", upload)

    def plan() -> dict[str, Any]:
        query = urlencode(
            {
                "menuUploadId": state["menuUploadId"],
                "quality": quality,
            }
        )
        body = response_json(client.get(f"/api/plan?{query}"), "plan")
        styles = body.get("styles")
        if not isinstance(styles, list) or len(styles) < 6:
            raise AcceptanceError(
                "plan",
                f"expected six background slots, got {len(styles or [])}",
            )
        selected_styles = styles[:6]
        style_ids = [
            required_text(
                style.get("id"),
                stage="plan",
                field="style.id",
            )
            for style in selected_styles
            if isinstance(style, dict)
        ]
        if len(style_ids) != 6 or len(set(style_ids)) != 6:
            raise AcceptanceError(
                "plan",
                "the first six style IDs must be present and unique",
            )
        plan_count = int(
            (
                body.get("menu")
                if isinstance(body.get("menu"), dict)
                else {}
            ).get("count")
            or 0
        )
        if plan_count != expected_count:
            raise AcceptanceError(
                "plan",
                (
                    f"plan row count mismatch: expected {expected_count}, "
                    f"got {plan_count}"
                ),
            )
        category = (
            body.get("category")
            if isinstance(body.get("category"), dict)
            else {}
        )
        actual_category_id = str(category.get("taxonomyId") or "")
        if expected_category_id and actual_category_id != expected_category_id:
            raise AcceptanceError(
                "plan",
                (
                    "menu taxonomy mismatch: expected "
                    f"{expected_category_id}, got {actual_category_id or 'missing'}"
                ),
                details={"category": category},
            )
        pipeline = (
            body.get("pipeline")
            if isinstance(body.get("pipeline"), dict)
            else {}
        )
        if require_approved_catalog and not bool(
            pipeline.get("approvedBackgroundCatalog")
        ):
            raise AcceptanceError(
                "plan",
                "approved background catalog is not enabled",
                details={"pipeline": pipeline},
            )
        state["styleIds"] = style_ids
        state["plan"] = body
        return {
            "summary": f"{plan_count} menu rows and six background slots",
            "rowCount": plan_count,
            "styleCount": len(style_ids),
            "styleIds": style_ids,
            "categoryId": actual_category_id,
            "categoryConfidence": int(category.get("confidence") or 0),
            "approvedBackgroundCatalog": bool(
                pipeline.get("approvedBackgroundCatalog")
            ),
        }

    run_stage(report, "plan", plan)

    def backgrounds() -> dict[str, Any]:
        records = []
        catalog_styles: dict[str, dict[str, Any]] = {}
        if require_approved_catalog:
            query = urlencode({"menuUploadId": state["menuUploadId"]})
            catalog = response_json(
                client.get(f"/api/background-catalog?{query}"),
                "backgrounds",
            )
            if catalog.get("mode") != "approved" or catalog.get("ready") is not True:
                raise AcceptanceError(
                    "backgrounds",
                    "approved six-slot background catalog is not ready",
                    details={"catalog": catalog},
                )
            catalog_category_id = str(catalog.get("categoryId") or "")
            if (
                expected_category_id
                and catalog_category_id != expected_category_id
            ):
                raise AcceptanceError(
                    "backgrounds",
                    (
                        "background catalog taxonomy mismatch: expected "
                        f"{expected_category_id}, got "
                        f"{catalog_category_id or 'missing'}"
                    ),
                    details={"catalog": catalog},
                )
            raw_styles = catalog.get("styles")
            if not isinstance(raw_styles, list) or len(raw_styles) != 6:
                raise AcceptanceError(
                    "backgrounds",
                    "approved catalog must return exactly six styles",
                    details={"catalog": catalog},
                )
            catalog_styles = {
                str(style.get("id") or style.get("styleId") or ""): style
                for style in raw_styles
                if isinstance(style, dict)
            }
        for style_id in state["styleIds"]:
            if require_approved_catalog:
                body = catalog_styles.get(style_id) or {}
            else:
                query = urlencode(
                    {
                        "menuUploadId": state["menuUploadId"],
                        "style": style_id,
                        "generate": 1,
                    }
                )
                body = response_json(
                    client.get(f"/api/style-background?{query}"),
                    "backgrounds",
                )
            sample = (
                body.get("sample")
                if isinstance(body.get("sample"), dict)
                else {}
            )
            generation_status = str(
                sample.get("generationStatus") or ""
            )
            if generation_status not in {"succeeded", "cached"}:
                raise AcceptanceError(
                    "backgrounds",
                    (
                        f"background {style_id} did not succeed: "
                        f"{generation_status or 'missing'}"
                    ),
                    details={"background": sample},
                )
            background_sha = require_sha256(
                sample.get("backgroundSha256"),
                stage="backgrounds",
                field=f"{style_id}.backgroundSha256",
            )
            asset_id = required_text(
                sample.get("backgroundAssetId"),
                stage="backgrounds",
                field=f"{style_id}.backgroundAssetId",
            )
            url = required_text(
                sample.get("url"),
                stage="backgrounds",
                field=f"{style_id}.url",
            )
            raw, size = fetch_image(client, url, stage="backgrounds")
            if sha256_bytes(raw) != background_sha:
                raise AcceptanceError(
                    "backgrounds",
                    f"background file SHA mismatch for {style_id}",
                )
            records.append(
                {
                    "styleId": style_id,
                    "assetId": asset_id,
                    "sha256": background_sha,
                    "size": {"width": size[0], "height": size[1]},
                    "generationStatus": generation_status,
                    "generationAction": sample.get("generationAction"),
                }
            )
        if len(records) != 6 or len({row["sha256"] for row in records}) != 6:
            raise AcceptanceError(
                "backgrounds",
                "six unique generated background images are required",
            )
        state["backgrounds"] = records
        return {
            "summary": (
                "six approved background images retrieved and SHA-verified"
                if require_approved_catalog
                else "six unique background images generated and SHA-verified"
            ),
            "generatedCount": len(records),
            "uniqueSha256Count": len({row["sha256"] for row in records}),
            "source": (
                "approved-background-catalog"
                if require_approved_catalog
                else "provider-generation"
            ),
            "backgrounds": records,
        }

    run_stage(report, "backgrounds", backgrounds)

    def select_background() -> dict[str, Any]:
        if style_index < 0 or style_index >= len(state["backgrounds"]):
            raise AcceptanceError(
                "selected-background",
                f"style index {style_index} is outside 0..5",
            )
        selected = state["backgrounds"][style_index]
        state["selected"] = selected
        return {
            "summary": (
                f"selected {selected['styleId']} with "
                f"SHA {selected['sha256'][:12]}"
            ),
            **selected,
        }

    run_stage(report, "selected-background", select_background)

    def free_samples() -> dict[str, Any]:
        selected = state["selected"]
        records = []
        for index in range(6):
            query = urlencode(
                {
                    "menuUploadId": state["menuUploadId"],
                    "style": selected["styleId"],
                    "index": index,
                    "backgroundAssetId": selected["assetId"],
                    "backgroundSha256": selected["sha256"],
                }
            )
            body = response_json(
                client.get(f"/api/style-preview-sample?{query}"),
                "free-samples",
            )
            sample = (
                body.get("sample")
                if isinstance(body.get("sample"), dict)
                else {}
            )
            generation = (
                sample.get("generation")
                if isinstance(sample.get("generation"), dict)
                else {}
            )
            if generation.get("status") not in {"succeeded", "cached"}:
                raise AcceptanceError(
                    "free-samples",
                    (
                        f"sample {index} did not succeed: "
                        f"{generation.get('status') or 'missing'}"
                    ),
                    details={"sample": sample},
                )
            candidate = (
                sample.get("candidate")
                if isinstance(sample.get("candidate"), dict)
                else {}
            )
            candidate_background_sha = require_sha256(
                candidate.get("backgroundSha256"),
                stage="free-samples",
                field=f"sample[{index}].backgroundSha256",
            )
            if candidate_background_sha != selected["sha256"]:
                raise AcceptanceError(
                    "free-samples",
                    f"sample {index} uses the wrong background SHA",
                )
            if not (
                candidate.get("backgroundIdentityVerified") is True
                and candidate.get("persistedOutputBackgroundVerified") is True
            ):
                raise AcceptanceError(
                    "free-samples",
                    f"sample {index} lacks background identity proof",
                )
            expected_output_sha = require_sha256(
                candidate.get("outputSha256"),
                stage="free-samples",
                field=f"sample[{index}].outputSha256",
            )
            raw, size = fetch_image(
                client,
                required_text(
                    candidate.get("url"),
                    stage="free-samples",
                    field=f"sample[{index}].url",
                ),
                stage="free-samples",
            )
            if sha256_bytes(raw) != expected_output_sha:
                raise AcceptanceError(
                    "free-samples",
                    f"sample {index} output SHA mismatch",
                )
            records.append(
                {
                    "index": index,
                    "row": sample.get("row"),
                    "dishName": sample.get("name"),
                    "outputSha256": expected_output_sha,
                    "backgroundSha256": candidate_background_sha,
                    "size": {"width": size[0], "height": size[1]},
                    "generationAction": generation.get("action"),
                }
            )
        state["samples"] = records
        return {
            "summary": "six free samples generated with the selected background",
            "generatedCount": len(records),
            "dishNames": [row["dishName"] for row in records],
            "selectedBackgroundSha256": selected["sha256"],
            "backgroundShaConsistent": all(
                row["backgroundSha256"] == selected["sha256"]
                for row in records
            ),
            "samples": records,
        }

    run_stage(report, "free-samples", free_samples)

    def formal_generation() -> dict[str, Any]:
        selected = state["selected"]
        account_before = response_json(
            client.get("/api/account"),
            "formal-generation",
        )
        balance_before = int(account_before.get("balance") or 0)
        idempotency_key = (
            f"menu-e2e-{mode}-{file_sha256(menu_path)[:20]}-"
            f"{selected['sha256'][:12]}-{state['menuUploadId'][-8:]}"
        )
        response = client.post(
            "/api/generation-jobs",
            json={
                "style": selected["styleId"],
                "quality": quality,
                "menuUploadId": state["menuUploadId"],
                "backgroundAssetId": selected["assetId"],
                "backgroundSha256": selected["sha256"],
                "platforms": platforms,
                "watermark": {"enabled": False},
                "idempotencyKey": idempotency_key,
            },
        )
        body = response_json(response, "formal-generation")
        job_id = required_text(
            body.get("jobId"),
            stage="formal-generation",
            field="jobId",
        )
        deadline = time.monotonic() + timeout_seconds
        polls = 0
        last_status = str(body.get("status") or "")
        last_body = body
        while last_status not in TERMINAL_JOB_STATUSES:
            if time.monotonic() >= deadline:
                raise AcceptanceError(
                    "formal-generation",
                    (
                        f"job {job_id} timed out after "
                        f"{timeout_seconds:.0f}s in {last_status or 'unknown'}"
                    ),
                    details={"lastJob": last_body},
                )
            time.sleep(0.2 if mode == "deterministic" else 2.0)
            last_body = response_json(
                client.get(f"/api/generation-jobs/{job_id}"),
                "formal-generation",
            )
            polls += 1
            last_status = str(last_body.get("status") or "")
        if last_status != "completed":
            raise AcceptanceError(
                "formal-generation",
                (
                    f"job {job_id} ended as {last_status}: "
                    f"{last_body.get('error') or 'no error detail'}"
                ),
                details={"job": last_body},
            )
        submission_billing = (
            body.get("generationBatch")
            if isinstance(body.get("generationBatch"), dict)
            else {}
        )
        result_payload = (
            last_body.get("result")
            if isinstance(last_body.get("result"), dict)
            else {}
        )
        terminal_billing = (
            last_body.get("generationBatch")
            if isinstance(last_body.get("generationBatch"), dict)
            else result_payload.get("generationBatch")
            if isinstance(result_payload.get("generationBatch"), dict)
            else {}
        )
        charged_points = int(submission_billing.get("chargedPoints") or 0)
        refunded_points = int(terminal_billing.get("refundedPoints") or 0)
        account_after = response_json(
            client.get("/api/account"),
            "formal-generation",
        )
        balance_after = int(account_after.get("balance") or 0)
        if balance_after != balance_before - charged_points + refunded_points:
            raise AcceptanceError(
                "formal-generation",
                "point balance does not match the server-owned debit/refund",
                details={
                    "balanceBefore": balance_before,
                    "balanceAfter": balance_after,
                    "chargedPoints": charged_points,
                    "refundedPoints": refunded_points,
                },
            )
        state["jobId"] = job_id
        state["job"] = last_body
        return {
            "summary": f"formal job completed for {expected_count} dishes",
            "jobId": job_id,
            "status": last_status,
            "pollCount": polls,
            "requestedImageCount": expected_count,
            "balanceBefore": balance_before,
            "balanceAfter": balance_after,
            "chargedPoints": charged_points,
            "refundedPoints": refunded_points,
            "idempotencyKeySha256": sha256_bytes(
                idempotency_key.encode("utf-8")
            ),
        }

    run_stage(report, "formal-generation", formal_generation)

    def manifest() -> dict[str, Any]:
        body = response_json(
            client.get(
                f"/api/generation-jobs/{state['jobId']}/manifest"
            ),
            "manifest",
        )
        state["manifest"] = body
        return validate_manifest(
            client,
            body,
            expected_names=expected_names,
            selected_sha=state["selected"]["sha256"],
        )

    run_stage(report, "manifest", manifest)

    def export() -> dict[str, Any]:
        selected = state["selected"]
        export_idempotency_key = (
            f"export-{state['jobId']}-{file_sha256(menu_path)[:12]}"
        )
        response = client.post(
            "/api/export",
            headers={"Idempotency-Key": export_idempotency_key},
            json={
                "jobId": state["jobId"],
                "menuUploadId": state["menuUploadId"],
                "style": selected["styleId"],
                "backgroundAssetId": selected["assetId"],
                "backgroundSha256": selected["sha256"],
                "scope": "all",
                "format": "jpg",
                "platforms": platforms,
                "watermark": {"enabled": False},
                "quality": quality,
            },
        )
        body = response_json(response, "export")
        expected_images = expected_count * len(platforms)
        if int(body.get("images") or 0) != expected_images:
            raise AcceptanceError(
                "export",
                (
                    f"export API image count mismatch: expected "
                    f"{expected_images}, got {body.get('images')}"
                ),
                details={"export": body},
            )
        download_url = required_text(
            body.get("download"),
            stage="export",
            field="download",
        )
        download_response = client.get(download_url)
        if download_response.status_code != 200:
            raise AcceptanceError(
                "export",
                (
                    f"export download failed with HTTP "
                    f"{download_response.status_code}"
                ),
            )
        zip_evidence = validate_export_zip(
            bytes(download_response.data),
            expected_row_count=expected_count,
            platforms=platforms,
        )
        return {
            "summary": (
                f"ZIP contains {expected_images} platform images and manifest report"
            ),
            "apiRows": int(body.get("rows") or 0),
            "apiImages": int(body.get("images") or 0),
            "platforms": platforms,
            "downloadPath": urlsplit(download_url).path,
            **zip_evidence,
        }

    run_stage(report, "export", export)
    return state


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run upload -> six backgrounds -> selected background -> "
            "formal generation -> manifest/export against a real Excel menu."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("deterministic", "real"),
        default="deterministic",
        help=(
            "deterministic never calls a paid provider; real requires the "
            "explicit environment gates documented in README"
        ),
    )
    parser.add_argument(
        "--menu",
        default="",
        help="exact .xls/.xlsx menu path; defaults to the first sorted file",
    )
    parser.add_argument(
        "--menu-dir",
        default=str(DEFAULT_MENU_DIR),
        help="directory used when --menu is omitted",
    )
    parser.add_argument(
        "--report",
        default="",
        help="JSON report path",
    )
    parser.add_argument(
        "--work-dir",
        default="",
        help="optional isolated runtime directory; temporary by default",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="keep an automatically created runtime directory",
    )
    parser.add_argument(
        "--quality",
        choices=("standard", "premium"),
        default="standard",
    )
    parser.add_argument(
        "--platform",
        action="append",
        dest="platforms",
        choices=("meituan", "taobao", "jd"),
        help="repeat to export multiple platforms; defaults to meituan",
    )
    parser.add_argument(
        "--style-index",
        type=int,
        default=0,
        help="which generated background to select (0..5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="formal generation poll timeout in seconds",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = (
        Path(args.report).expanduser()
        if args.report
        else default_report_path(args.mode)
    )
    report = AcceptanceReport(mode=args.mode, report_path=report_path)
    platforms = list(dict.fromkeys(args.platforms or ["meituan"]))

    preflight_started = time.monotonic()
    try:
        menu_path = resolve_menu_path(args.menu, args.menu_dir)
        parsed_menu = parse_real_menu(menu_path)
        evidence = menu_evidence(menu_path, parsed_menu)
    except Exception as exc:
        report.add_stage(
            "preflight",
            FAIL,
            elapsed_seconds=time.monotonic() - preflight_started,
            error=f"{type(exc).__name__}: {redact_text(exc)}",
        )
        skip_remaining_stages(
            report,
            after_stage="preflight",
            reason="preflight failed before application import",
        )
        report.finish(
            FAIL,
            summary={
                "reason": "menu preflight failed",
                "reportPath": str(report.path),
            },
        )
        print(f"Report: {report.path}")
        return 1

    if args.mode == "real":
        blockers, provider_preflight = real_provider_preflight(
            evidence["rowCount"]
        )
        if blockers:
            report.add_stage(
                "preflight",
                BLOCKED,
                elapsed_seconds=time.monotonic() - preflight_started,
                details={
                    **evidence,
                    **provider_preflight,
                    "reason": "; ".join(blockers),
                    "summary": "real provider gate blocked before any provider call",
                },
            )
            skip_remaining_stages(
                report,
                after_stage="preflight",
                reason="real provider preflight is blocked; zero provider calls made",
            )
            report.finish(
                BLOCKED,
                summary={
                    "menu": evidence,
                    "providerPreflight": provider_preflight,
                    "providerCalls": 0,
                    "reportPath": str(report.path),
                },
            )
            print(f"Report: {report.path}")
            return 2
        preflight_details = {
            **evidence,
            **provider_preflight,
            "providerBoundary": "real-tencent-provider",
            "summary": (
                f"real provider gate passed for {evidence['rowCount']} dishes"
            ),
        }
    else:
        preflight_details = {
            **evidence,
            "providerBoundary": "deterministic-local",
            "paidProviderCallsAllowed": False,
            "estimatedRealProviderCalls": estimated_real_provider_calls(
                evidence["rowCount"]
            ),
            "secretsRedacted": True,
            "summary": (
                f"deterministic local smoke for {evidence['rowCount']} dishes"
            ),
        }
    report.add_stage(
        "preflight",
        PASS,
        elapsed_seconds=time.monotonic() - preflight_started,
        details=preflight_details,
    )

    automatic_work_dir = not bool(args.work_dir)
    if args.work_dir:
        runtime_base = Path(args.work_dir).expanduser().resolve()
        runtime_base.mkdir(parents=True, exist_ok=True)
        runtime_root = Path(
            tempfile.mkdtemp(
                prefix=f"waimai-menu-e2e-{args.mode}-",
                dir=runtime_base,
            )
        ).resolve()
        temp_context: Any = retained_work_directory(runtime_root)
    elif args.keep_work_dir:
        runtime_root = Path(
            tempfile.mkdtemp(prefix=f"waimai-menu-e2e-{args.mode}-")
        ).resolve()
        temp_context = retained_work_directory(runtime_root)
    else:
        temp_directory = tempfile.TemporaryDirectory(
            prefix=f"waimai-menu-e2e-{args.mode}-"
        )
        runtime_root = Path(temp_directory.name).resolve()
        temp_context = temp_directory

    provider_calls: dict[str, int] = {
        "styleBackground": 0,
        "foregroundGeneration": 0,
        "maskExtraction": 0,
    }
    try:
        with temp_context:
            with isolated_environment(
                runtime_root,
                row_count=evidence["rowCount"],
                mode=args.mode,
            ):
                import importlib

                app_module = importlib.import_module("app")
                configure_app_paths(app_module, runtime_root)
                with provider_boundary(
                    app_module,
                    mode=args.mode,
                ) as provider_calls:
                    execute_pipeline(
                        app_module=app_module,
                        menu_path=menu_path,
                        parsed_menu=parsed_menu,
                        report=report,
                        mode=args.mode,
                        quality=args.quality,
                        platforms=platforms,
                        style_index=args.style_index,
                        timeout_seconds=max(1.0, float(args.timeout)),
                    )
    except AcceptanceError as exc:
        skip_remaining_stages(
            report,
            after_stage=exc.stage,
            reason=f"stopped after {exc.stage} failure",
        )
        report.finish(
            FAIL,
            summary={
                "menu": evidence,
                "providerBoundary": (
                    "real-tencent-provider"
                    if args.mode == "real"
                    else "deterministic-local"
                ),
                "providerCalls": provider_calls,
                "failedStage": exc.stage,
                "runtimeWorkDir": (
                    str(runtime_root)
                    if args.keep_work_dir or not automatic_work_dir
                    else "removed"
                ),
                "reportPath": str(report.path),
            },
        )
        print(f"Report: {report.path}")
        return 1
    except Exception as exc:
        existing = [
            record["name"]
            for record in report.data.get("stages", [])
            if record.get("status") == PASS
        ]
        last_stage = existing[-1] if existing else "preflight"
        next_stage = next(
            (
                name
                for name in STAGE_NAMES
                if name not in {
                    record.get("name")
                    for record in report.data.get("stages", [])
                }
            ),
            last_stage,
        )
        if next_stage not in {
            record.get("name") for record in report.data.get("stages", [])
        }:
            report.add_stage(
                next_stage,
                FAIL,
                error=f"{type(exc).__name__}: {redact_text(exc)}",
            )
        skip_remaining_stages(
            report,
            after_stage=next_stage,
            reason=f"stopped after {next_stage} failure",
        )
        report.finish(
            FAIL,
            summary={
                "menu": evidence,
                "providerCalls": provider_calls,
                "failedStage": next_stage,
                "reportPath": str(report.path),
            },
        )
        print(f"Report: {report.path}")
        return 1

    provider_boundary_name = (
        "real-tencent-provider"
        if args.mode == "real"
        else "deterministic-local"
    )
    report.finish(
        PASS,
        production_provider_verified=args.mode == "real",
        summary={
            "menu": evidence,
            "quality": args.quality,
            "platforms": platforms,
            "providerBoundary": provider_boundary_name,
            "providerCalls": provider_calls,
            "deterministicLocalSmokePassed": args.mode == "deterministic",
            "realProviderSmokePassed": args.mode == "real",
            "runtimeWorkDir": (
                str(runtime_root)
                if args.keep_work_dir or not automatic_work_dir
                else "removed"
            ),
            "reportPath": str(report.path),
        },
    )
    print(
        (
            "PASS: real provider smoke completed"
            if args.mode == "real"
            else (
                "PASS: deterministic local smoke completed; "
                "production provider was not verified"
            )
        ),
        flush=True,
    )
    print(f"Report: {report.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
