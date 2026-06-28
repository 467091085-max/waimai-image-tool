from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


class GenerationProviderNotConfigured(RuntimeError):
    pass


def generate_image(task_payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Dispatch image generation for worker processes only.

    Production must configure a real provider adapter. A local mock provider is
    intentionally gated by env so API servers cannot silently fake generated
    images.
    """
    values = os.environ if env is None else env
    provider = str(values.get("AI_IMAGE_PROVIDER") or "").strip().lower()
    if provider == "mock" and str(values.get("ALLOW_MOCK_GENERATION") or "").strip().lower() in {"1", "true", "yes"}:
        return _mock_generate(task_payload, values)
    raise GenerationProviderNotConfigured("AI_IMAGE_PROVIDER is not configured for worker image generation")


def _mock_generate(task_payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    output_dir = Path(str(env.get("SAAS_MOCK_OUTPUT_DIR") or "data/saas-mock")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(task_payload.get("task_id") or task_payload.get("id") or "mock")
    filename = f"{_safe_name(task_id)}.txt"
    target = output_dir / filename
    prompt = str(task_payload.get("prompt") or "")
    target.write_text(f"mock image for: {prompt}\n", encoding="utf-8")
    return {
        "image_url": target.as_uri(),
        "provider": "mock",
        "local_path": str(target),
    }


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "mock"
