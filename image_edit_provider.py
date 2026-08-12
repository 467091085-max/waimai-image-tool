from __future__ import annotations

import base64
import binascii
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from PIL import Image, ImageOps


DEFAULT_GEMINI_IMAGE_EDIT_MODEL = "gemini-3.1-flash-image"
DEFAULT_GEMINI_INTERACTIONS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)
GEMINI_IMAGE_EDIT_PROMPT_VERSION = "food-refinement.v1"
GEMINI_IMAGE_EDIT_API_SURFACE = "interactions-v1beta"
GEMINI_IMAGE_EDIT_OUTPUT_SIZE = "1K"
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 30 * 1024 * 1024
MAX_OUTPUT_BASE64_CHARS = ((MAX_OUTPUT_BYTES + 2) // 3) * 4 + 256
MAX_PROVIDER_RESPONSE_BYTES = MAX_OUTPUT_BASE64_CHARS + (2 * 1024 * 1024)
MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_SIDE = 12_000
MAX_EDIT_PROMPT_CHARS = 800
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
GEMINI_API_HOST = "generativelanguage.googleapis.com"
GEMINI_INTERACTIONS_PATH = "/v1beta/interactions"


class ImageEditProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _RejectGeminiRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_GEMINI_NO_REDIRECT_OPENER = urllib.request.build_opener(
    _RejectGeminiRedirects()
)


def _gemini_urlopen(request: Any, *, timeout: float) -> Any:
    return _GEMINI_NO_REDIRECT_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class ImageEditResult:
    image_bytes: bytes
    mime_type: str
    provider: str
    model: str
    request_id: str
    latency_seconds: float = 0.0
    source_size: tuple[int, int] | None = None
    provider_size: tuple[int, int] | None = None
    normalized_to_source: bool = False


class ImageEditProvider(Protocol):
    def edit(
        self,
        source_bytes: bytes,
        edit_prompt: str,
        *,
        source_mime_type: str = "image/png",
    ) -> ImageEditResult:
        ...


def gemini_image_edit_config(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = os.environ if env is None else env
    api_key = str(
        values.get("GEMINI_API_KEY")
        or values.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    model = str(
        values.get("GEMINI_IMAGE_EDIT_MODEL")
        or DEFAULT_GEMINI_IMAGE_EDIT_MODEL
    ).strip()
    endpoint = str(
        values.get("GEMINI_INTERACTIONS_URL")
        or DEFAULT_GEMINI_INTERACTIONS_URL
    ).strip()
    timeout_seconds, timeout_valid = _configured_timeout(values)
    return {
        "ready": bool(
            api_key
            and MODEL_RE.fullmatch(model)
            and _valid_gemini_interactions_endpoint(endpoint)
            and timeout_valid
        ),
        "apiKey": api_key,
        "model": model,
        "endpoint": endpoint,
        "timeoutSeconds": timeout_seconds,
        "timeoutValid": timeout_valid,
    }


def gemini_image_edit_provider_snapshot(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    config = gemini_image_edit_config(env)
    return {
        "provider": "google-gemini",
        "model": config["model"],
        "apiSurface": GEMINI_IMAGE_EDIT_API_SURFACE,
        "endpoint": config["endpoint"],
        "promptVersion": GEMINI_IMAGE_EDIT_PROMPT_VERSION,
        "output": {
            "mimeType": "image/jpeg",
            "imageSize": GEMINI_IMAGE_EDIT_OUTPUT_SIZE,
            "aspectRatio": "source-nearest-supported",
            "canvasNormalization": "same-as-source.v1",
        },
    }


def gemini_image_edit_readiness(
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    config = gemini_image_edit_config(env)
    missing = []
    blocking = []
    if not config["apiKey"]:
        missing.append("GEMINI_API_KEY")
        blocking.append("gemini_image_edit_api_key_required")
    if not MODEL_RE.fullmatch(str(config["model"])):
        blocking.append("gemini_image_edit_model_invalid")
    if not _valid_gemini_interactions_endpoint(str(config["endpoint"])):
        blocking.append("gemini_image_edit_endpoint_invalid")
    if not bool(config["timeoutValid"]):
        blocking.append("gemini_image_edit_timeout_invalid")
    return {
        "ready": not blocking,
        "provider": "google-gemini",
        "model": config["model"],
        "apiSurface": GEMINI_IMAGE_EDIT_API_SURFACE,
        "promptVersion": GEMINI_IMAGE_EDIT_PROMPT_VERSION,
        "outputSpec": gemini_image_edit_provider_snapshot(env)["output"],
        "timeoutSeconds": config["timeoutSeconds"],
        "missingConfig": missing,
        "blockingIssues": blocking,
    }


class GeminiImageEditProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_IMAGE_EDIT_MODEL,
        endpoint: str = DEFAULT_GEMINI_INTERACTIONS_URL,
        timeout_seconds: float = 180,
        opener: Callable[..., Any] = _gemini_urlopen,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.endpoint = str(endpoint or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.opener = opener
        if not self.api_key:
            raise ImageEditProviderError(
                "gemini_image_edit_not_configured",
                "Gemini 图片精修未配置",
            )
        if not MODEL_RE.fullmatch(self.model):
            raise ImageEditProviderError(
                "gemini_image_edit_model_invalid",
                "Gemini 图片精修模型配置无效",
            )
        if not _valid_gemini_interactions_endpoint(self.endpoint):
            raise ImageEditProviderError(
                "gemini_image_edit_endpoint_invalid",
                "Gemini 图片精修地址无效",
            )
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds < 1
            or self.timeout_seconds > 900
        ):
            raise ImageEditProviderError(
                "gemini_image_edit_timeout_invalid",
                "Gemini 图片精修超时配置无效",
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        opener: Callable[..., Any] = _gemini_urlopen,
    ) -> "GeminiImageEditProvider":
        values = os.environ if env is None else env
        config = gemini_image_edit_config(values)
        return cls(
            api_key=str(config["apiKey"]),
            model=str(config["model"]),
            endpoint=str(config["endpoint"]),
            timeout_seconds=float(config["timeoutSeconds"]),
            opener=opener,
        )

    def edit(
        self,
        source_bytes: bytes,
        edit_prompt: str,
        *,
        source_mime_type: str = "image/png",
    ) -> ImageEditResult:
        source = _validated_image_bytes(
            source_bytes,
            max_bytes=MAX_SOURCE_BYTES,
            code="invalid_refinement_source",
        )
        prompt = _validated_prompt(edit_prompt)
        mime_type = _normalized_mime_type(source_mime_type)
        source_size = _image_dimensions(source)
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {
                    "type": "text",
                    "text": _commercial_edit_prompt(prompt),
                },
                {
                    "type": "image",
                    "mime_type": mime_type,
                    "data": base64.b64encode(source).decode("ascii"),
                },
            ],
            "response_format": {
                "type": "image",
                "mime_type": "image/jpeg",
                "image_size": GEMINI_IMAGE_EDIT_OUTPUT_SIZE,
                "aspect_ratio": _nearest_supported_aspect_ratio(*source_size),
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        request_started = time.monotonic()
        try:
            with self.opener(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw_response = response.read(
                    MAX_PROVIDER_RESPONSE_BYTES + 1
                )
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            raise ImageEditProviderError(
                "gemini_image_edit_http_error",
                f"Gemini 图片精修失败：{detail}",
                retryable=exc.code == 429 or exc.code >= 500,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageEditProviderError(
                "gemini_image_edit_unavailable",
                "Gemini 图片精修服务暂时不可用",
                retryable=True,
            ) from exc
        if len(raw_response) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ImageEditProviderError(
                "gemini_image_edit_response_too_large",
                "Gemini 图片精修返回内容过大",
                retryable=True,
            )

        try:
            response_payload = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageEditProviderError(
                "gemini_image_edit_invalid_response",
                "Gemini 图片精修返回格式无效",
                retryable=True,
            ) from exc
        if not isinstance(response_payload, dict):
            raise ImageEditProviderError(
                "gemini_image_edit_invalid_response",
                "Gemini 图片精修返回格式无效",
                retryable=True,
            )
        if response_payload.get("error"):
            raise ImageEditProviderError(
                "gemini_image_edit_rejected",
                "Gemini 图片精修请求未完成",
                retryable=False,
            )
        encoded, output_mime = _last_inline_image(response_payload)
        if len(encoded) > MAX_OUTPUT_BASE64_CHARS:
            raise ImageEditProviderError(
                "gemini_image_edit_invalid_image",
                "Gemini 图片精修返回图片过大",
                retryable=True,
            )
        try:
            output = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageEditProviderError(
                "gemini_image_edit_invalid_image",
                "Gemini 图片精修返回图片无效",
                retryable=True,
            ) from exc
        output = _validated_image_bytes(
            output,
            max_bytes=MAX_OUTPUT_BYTES,
            code="gemini_image_edit_invalid_image",
        )
        output, provider_size, normalized_to_source, output_mime = (
            _normalize_output_canvas(
                output,
                output_mime,
                source_size=source_size,
            )
        )
        return ImageEditResult(
            image_bytes=output,
            mime_type=output_mime,
            provider="google-gemini",
            model=self.model,
            request_id=str(response_payload.get("id") or ""),
            latency_seconds=round(time.monotonic() - request_started, 6),
            source_size=source_size,
            provider_size=provider_size,
            normalized_to_source=normalized_to_source,
        )


def _valid_gemini_interactions_endpoint(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == GEMINI_API_HOST
        and port in {None, 443}
        and parsed.path.rstrip("/") == GEMINI_INTERACTIONS_PATH
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _configured_timeout(values: Mapping[str, str]) -> tuple[float, bool]:
    raw = values.get("GEMINI_IMAGE_EDIT_TIMEOUT_SECONDS")
    try:
        timeout = float(180 if raw in (None, "") else raw)
    except (TypeError, ValueError):
        return 180.0, False
    if not math.isfinite(timeout) or timeout < 1 or timeout > 900:
        return timeout, False
    return timeout, True


def _image_dimensions(value: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(value)) as image:
        width, height = image.size
    return int(width), int(height)


_SUPPORTED_ASPECT_RATIOS = (
    ("1:1", 1.0),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("5:4", 5 / 4),
    ("4:5", 4 / 5),
    ("21:9", 21 / 9),
)


def _nearest_supported_aspect_ratio(width: int, height: int) -> str:
    ratio = width / max(1, height)
    return min(
        _SUPPORTED_ASPECT_RATIOS,
        key=lambda item: abs(item[1] - ratio),
    )[0]


def _normalize_output_canvas(
    value: bytes,
    mime_type: str,
    *,
    source_size: tuple[int, int],
) -> tuple[bytes, tuple[int, int], bool, str]:
    clean_mime_type = _normalized_mime_type(mime_type)
    with Image.open(io.BytesIO(value)) as image:
        image.load()
        provider_size = (int(image.width), int(image.height))
        if provider_size == source_size:
            return value, provider_size, False, clean_mime_type
        source_ratio = source_size[0] / max(1, source_size[1])
        provider_ratio = provider_size[0] / max(1, provider_size[1])
        relative_error = abs(provider_ratio - source_ratio) / max(
            source_ratio,
            1e-9,
        )
        if relative_error > 0.02:
            raise ImageEditProviderError(
                "gemini_image_edit_output_aspect_ratio_mismatch",
                "Gemini 图片精修返回比例与源图不一致",
                retryable=False,
            )
        normalized = ImageOps.exif_transpose(image).convert("RGB").resize(
            source_size,
            Image.Resampling.LANCZOS,
        )
        output = io.BytesIO()
        normalized.save(output, "JPEG", quality=95, optimize=True)
    normalized_bytes = output.getvalue()
    _validated_image_bytes(
        normalized_bytes,
        max_bytes=MAX_OUTPUT_BYTES,
        code="gemini_image_edit_invalid_image",
    )
    return normalized_bytes, provider_size, True, "image/jpeg"


def _validated_prompt(value: str) -> str:
    prompt = " ".join(str(value or "").split())
    if not prompt:
        raise ImageEditProviderError(
            "invalid_refinement_prompt",
            "图片精修要求不能为空",
        )
    if len(prompt) > MAX_EDIT_PROMPT_CHARS:
        raise ImageEditProviderError(
            "invalid_refinement_prompt",
            "图片精修要求过长",
        )
    return prompt


def _validated_image_bytes(
    value: bytes,
    *,
    max_bytes: int,
    code: str,
) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > max_bytes:
        raise ImageEditProviderError(code, "图片文件无效")
    try:
        with Image.open(io.BytesIO(value)) as image:
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_SIDE
                or height > MAX_IMAGE_SIDE
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ValueError("image dimensions exceed limit")
            if str(image.format or "").upper() not in {
                "JPEG",
                "PNG",
                "WEBP",
            }:
                raise ValueError("image format is unsupported")
            image.verify()
    except Exception as exc:
        raise ImageEditProviderError(code, "图片文件无效") from exc
    return value


def _normalized_mime_type(value: str) -> str:
    mime_type = str(value or "").strip().lower()
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ImageEditProviderError(
            "invalid_refinement_image_type",
            "图片格式不受支持",
        )
    return mime_type


def _commercial_edit_prompt(user_prompt: str) -> str:
    return (
        "Edit the provided commercial food-delivery product image. "
        "Apply only the requested change to the food or plating. Preserve the "
        "camera angle, full-frame composition, lighting, and background. Do "
        "not add text, logos, borders, inner frames, blurred filler, or a "
        "watermark. Return exactly one photorealistic product image. "
        f"Requested change: {user_prompt}"
    )


def _last_inline_image(payload: dict[str, Any]) -> tuple[str, str]:
    matches: list[tuple[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            block_type = str(value.get("type") or "").lower()
            data = value.get("data")
            mime_type = value.get("mime_type") or value.get("mimeType")
            if (
                block_type == "image"
                and isinstance(data, str)
                and data
                and isinstance(mime_type, str)
            ):
                matches.append((data, mime_type))
            inline = value.get("inlineData") or value.get("inline_data")
            if isinstance(inline, dict):
                inline_data = inline.get("data")
                inline_mime = inline.get("mimeType") or inline.get("mime_type")
                if (
                    isinstance(inline_data, str)
                    and inline_data
                    and isinstance(inline_mime, str)
                ):
                    matches.append((inline_data, inline_mime))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload.get("steps") or payload.get("candidates") or payload)
    if not matches:
        raise ImageEditProviderError(
            "gemini_image_edit_no_image",
            "Gemini 图片精修没有返回图片",
            retryable=True,
        )
    return matches[-1]


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read(65_537)[:65_536])
    except Exception:
        payload = {}
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        message = str(payload["error"].get("message") or "").strip()
        if message:
            return message[:180]
    return f"HTTP {exc.code}"


__all__ = [
    "DEFAULT_GEMINI_IMAGE_EDIT_MODEL",
    "GeminiImageEditProvider",
    "ImageEditProviderError",
    "ImageEditResult",
    "gemini_image_edit_config",
    "gemini_image_edit_readiness",
]
