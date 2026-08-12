from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import provider_capacity
from shared.json_limits import (
    InvalidJsonValue,
    JsonSizeLimitExceeded,
    validate_json_size,
)
from shared.prompt_limits import (
    MAX_PROMPT_BYTES as DEFAULT_MAX_PROMPT_BYTES,
    MAX_PROMPT_CHARS as DEFAULT_MAX_PROMPT_CHARS,
    PromptValidationError,
    normalize_prompt,
)


TOKENHUB_IMAGE_LITE_URL = "https://tokenhub.tencentmaas.com/v1/api/image/lite"
TOKENHUB_IMAGE_SUBMIT_URL = "https://tokenhub.tencentmaas.com/v1/api/image/submit"
TOKENHUB_IMAGE_QUERY_URL = "https://tokenhub.tencentmaas.com/v1/api/image/query"
TOKENHUB_HY_V3_URL = (
    "https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v3-generation"
)
MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROMPT_CHARS = DEFAULT_MAX_PROMPT_CHARS
MAX_PROMPT_BYTES = DEFAULT_MAX_PROMPT_BYTES
MAX_PROVIDER_REQUEST_BYTES = 64 * 1024

_SUPPORTED_PROVIDERS = frozenset(
    {"tencent-hunyuan", "tencent-tokenhub", "tokenhub"}
)
_SUPPORTED_MODELS = frozenset({"hy-image-v3", "hy-image-v3.0", "hy-image-lite"})
_SUPPORTED_PROTOCOLS = frozenset(
    {"auto", "legacy-submit-query-v1", "wand-sync-v1"}
)
_SUCCESS_STATUSES = frozenset(
    {"completed", "finish", "finished", "succeeded", "success"}
)
_FAILED_STATUSES = frozenset(
    {"canceled", "cancelled", "error", "fail", "failed"}
)

HttpPost = Callable[[str, Mapping[str, Any], float], Mapping[str, Any]]


class PromptProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class PromptProviderConfigurationError(PromptProviderError):
    pass


@dataclass(frozen=True)
class TokenHubPromptConfig:
    api_key: str = field(repr=False)
    model: str = "hy-image-v3.0"
    request_timeout_seconds: float = 55
    poll_timeout_seconds: float = 120
    poll_interval_seconds: float = 3
    protocol: str = "auto"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "TokenHubPromptConfig":
        values = os.environ if env is None else env
        provider = str(values.get("AI_IMAGE_PROVIDER") or "").strip().lower()
        if provider not in _SUPPORTED_PROVIDERS:
            raise PromptProviderConfigurationError(
                "AI_IMAGE_PROVIDER must be tencent-tokenhub for the prompt worker"
            )
        if _explicitly_false(values.get("TENCENT_TOKENHUB_ENABLED")):
            raise PromptProviderConfigurationError(
                "TENCENT_TOKENHUB_ENABLED disables the configured prompt provider"
            )
        api_key = str(
            values.get("TENCENT_TOKENHUB_API_KEY")
            or values.get("TOKENHUB_API_KEY")
            or values.get("HUNYUAN_TOKENHUB_API_KEY")
            or ""
        ).strip()
        if not api_key:
            raise PromptProviderConfigurationError(
                "TENCENT_TOKENHUB_API_KEY is required for the prompt worker"
            )
        model = str(
            values.get("TENCENT_TOKENHUB_IMAGE_MODEL")
            or values.get("TOKENHUB_IMAGE_MODEL")
            or "hy-image-v3.0"
        ).strip().lower()
        if model not in _SUPPORTED_MODELS:
            raise PromptProviderConfigurationError(
                f"unsupported TokenHub prompt image model: {model or 'missing'}"
            )
        protocol = str(
            values.get("TENCENT_TOKENHUB_PROTOCOL") or "auto"
        ).strip().lower()
        if protocol not in _SUPPORTED_PROTOCOLS:
            raise PromptProviderConfigurationError(
                f"unsupported TokenHub protocol: {protocol or 'missing'}"
            )
        if protocol == "wand-sync-v1" and model != "hy-image-v3":
            raise PromptProviderConfigurationError(
                "wand-sync-v1 requires TENCENT_TOKENHUB_IMAGE_MODEL=hy-image-v3"
            )
        return cls(
            api_key=api_key,
            model=model,
            request_timeout_seconds=_positive_float(
                values.get("TENCENT_REQUEST_TIMEOUT"),
                default=55,
                name="TENCENT_REQUEST_TIMEOUT",
            ),
            poll_timeout_seconds=_positive_float(
                values.get("TENCENT_TOKENHUB_POLL_TIMEOUT"),
                default=120,
                name="TENCENT_TOKENHUB_POLL_TIMEOUT",
            ),
            poll_interval_seconds=_positive_float(
                values.get("TENCENT_TOKENHUB_POLL_INTERVAL"),
                default=3,
                name="TENCENT_TOKENHUB_POLL_INTERVAL",
            ),
            protocol=protocol,
        )

    @property
    def resolved_protocol(self) -> str:
        if self.protocol == "auto":
            return (
                "wand-sync-v1"
                if self.model == "hy-image-v3"
                else "legacy-submit-query-v1"
            )
        return self.protocol


class TokenHubPromptProvider:
    def __init__(
        self,
        config: TokenHubPromptConfig,
        *,
        http_post: HttpPost | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        concurrency_gate: Any | None = None,
    ) -> None:
        self.config = config
        self._http_post = http_post or self._post_json
        self._monotonic = monotonic
        self._sleep = sleep
        self._concurrency_gate = concurrency_gate or (
            provider_capacity.build_provider_concurrency_gate(
                provider_capacity.GenerationCapacity.from_env({}),
                {},
            )
        )

    def generate(self, prompt: str) -> dict[str, Any]:
        try:
            clean_prompt = normalize_prompt(
                prompt,
                max_chars=MAX_PROMPT_CHARS,
                max_bytes=MAX_PROMPT_BYTES,
            )
        except PromptValidationError as exc:
            raise PromptProviderError(str(exc)) from exc
        request_payload = {
            "model": self.config.model,
            "prompt": clean_prompt,
        }
        with self._concurrency_gate.slot():
            return self._generate_once(request_payload)

    def _generate_once(self, request_payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.config.resolved_protocol == "wand-sync-v1":
            response = self._request(
                TOKENHUB_HY_V3_URL,
                {
                    **request_payload,
                    "model": "hy-image-v3",
                    "revise": False,
                },
                timeout=max(
                    self.config.request_timeout_seconds,
                    self.config.poll_timeout_seconds,
                ),
            )
            return self._result(response)
        if self.config.model == "hy-image-lite":
            response = self._request(
                TOKENHUB_IMAGE_LITE_URL,
                request_payload,
                timeout=self.config.request_timeout_seconds,
            )
            return self._result(response)

        submitted = self._request(
            TOKENHUB_IMAGE_SUBMIT_URL,
            request_payload,
            timeout=min(self.config.request_timeout_seconds, 30),
        )
        image_url = _image_url(submitted)
        if image_url:
            return self._result(submitted)
        provider_job_id = _provider_job_id(submitted)
        if not provider_job_id:
            raise PromptProviderError(
                "TokenHub submit response did not include an image URL or job id"
            )

        deadline = self._monotonic() + self.config.poll_timeout_seconds
        latest = submitted
        while self._monotonic() < deadline:
            status = _status(latest)
            if status in _FAILED_STATUSES:
                raise PromptProviderError(
                    f"TokenHub job failed: {_safe_provider_error(latest)}"
                )
            if status in _SUCCESS_STATUSES:
                return self._result(latest, provider_job_id=provider_job_id)
            self._sleep(
                min(
                    self.config.poll_interval_seconds,
                    max(0.001, deadline - self._monotonic()),
                )
            )
            remaining = max(0.001, deadline - self._monotonic())
            latest = self._request(
                TOKENHUB_IMAGE_QUERY_URL,
                {"model": self.config.model, "id": provider_job_id},
                timeout=min(self.config.request_timeout_seconds, remaining, 30),
            )
            if _image_url(latest):
                return self._result(latest, provider_job_id=provider_job_id)

        raise PromptProviderError(
            f"TokenHub job timed out: {provider_job_id} status={_status(latest) or 'unknown'}"
        )

    def _result(
        self,
        response: Mapping[str, Any],
        *,
        provider_job_id: str = "",
    ) -> dict[str, Any]:
        image_url = _image_url(response)
        if not image_url:
            raise PromptProviderError(
                "TokenHub completed response did not include an image URL"
            )
        parsed = urlparse(image_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise PromptProviderError(
                "TokenHub returned an invalid image URL"
            )
        return {
            "image_url": image_url,
            "model": self.config.model,
            "provider": "tencent-tokenhub",
            "provider_job_id": (
                provider_job_id or _provider_job_id(response)
            ),
            "request_id": _request_id(response),
        }

    def _request(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        response = self._http_post(url, payload, timeout)
        if not isinstance(response, Mapping):
            raise PromptProviderError("TokenHub returned a non-object response")
        error = response.get("error")
        if error:
            raise PromptProviderError(
                f"TokenHub request failed: {_safe_provider_error(response)}"
            )
        return response

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> Mapping[str, Any]:
        try:
            validate_json_size(
                payload,
                MAX_PROVIDER_REQUEST_BYTES,
            )
            body = json.dumps(
                dict(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (
            InvalidJsonValue,
            JsonSizeLimitExceeded,
            TypeError,
            UnicodeEncodeError,
        ) as exc:
            raise PromptProviderError(
                "TokenHub request exceeds size limit"
            ) from exc
        if len(body) > MAX_PROVIDER_REQUEST_BYTES:
            raise PromptProviderError(
                "TokenHub request exceeds size limit"
            )
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw_bytes = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_PROVIDER_RESPONSE_BYTES:
                raise PromptProviderError(
                    "TokenHub response exceeds size limit"
                )
            raw = raw_bytes.decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read(65_537)[:65_536].decode(
                "utf-8",
                errors="replace",
            )
            raise PromptProviderError(
                f"TokenHub HTTP {exc.code}: {_safe_http_error(raw)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PromptProviderError(
                f"TokenHub request failed: {exc.reason}"
            ) from exc
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PromptProviderError(
                "TokenHub returned invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise PromptProviderError("TokenHub returned a non-object response")
        return decoded


def _image_url(response: Mapping[str, Any]) -> str:
    data = response.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, Mapping):
                url = str(
                    item.get("url")
                    or item.get("image_url")
                    or ""
                ).strip()
                if url:
                    return url
    output = response.get("output")
    if isinstance(output, Mapping):
        images = output.get("images")
        if isinstance(images, list):
            for item in images:
                if isinstance(item, Mapping):
                    url = str(
                        item.get("url")
                        or item.get("image_url")
                        or ""
                    ).strip()
                    if url:
                        return url
        url = str(
            output.get("url")
            or output.get("image_url")
            or ""
        ).strip()
        if url:
            return url
    return str(
        response.get("url")
        or response.get("image_url")
        or response.get("result_url")
        or ""
    ).strip()


def _provider_job_id(response: Mapping[str, Any]) -> str:
    return str(
        response.get("id")
        or response.get("task_id")
        or response.get("job_id")
        or ""
    ).strip()


def _request_id(response: Mapping[str, Any]) -> str:
    return str(
        response.get("request_id")
        or response.get("RequestId")
        or ""
    ).strip()


def _status(response: Mapping[str, Any]) -> str:
    return str(
        response.get("status")
        or response.get("task_status")
        or ""
    ).strip().lower()


def _safe_provider_error(response: Mapping[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, Mapping):
        code = str(error.get("code") or "error").strip()
        message = str(error.get("message") or "provider error").strip()
        return f"{code}: {message}"[:500]
    if error:
        return str(error)[:500]
    return f"status={_status(response) or 'unknown'}"


def _safe_http_error(raw: str) -> str:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return "provider error response"
    if isinstance(decoded, Mapping):
        return _safe_provider_error(decoded)
    return "provider error response"


def _positive_float(
    value: Any,
    *,
    default: float,
    name: str,
) -> float:
    raw = default if value in (None, "") else value
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise PromptProviderConfigurationError(
            f"{name} must be a positive number"
        ) from exc
    if number <= 0:
        raise PromptProviderConfigurationError(
            f"{name} must be a positive number"
        )
    return number


def _explicitly_false(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }
