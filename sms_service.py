from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


ERR_SMS_PROVIDER_UNAVAILABLE = "sms_provider_unavailable"
ERR_SMS_SEND_FAILED = "sms_send_failed"


class SmsServiceError(RuntimeError):
    """Domain error with a stable code for API adapters."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SmsProviderUnavailable(SmsServiceError):
    def __init__(self, message: str = "SMS provider is unavailable") -> None:
        super().__init__(ERR_SMS_PROVIDER_UNAVAILABLE, message)


class SmsSendError(SmsServiceError):
    def __init__(self, message: str = "failed to send SMS") -> None:
        super().__init__(ERR_SMS_SEND_FAILED, message)


@dataclass(frozen=True)
class SmsSendResult:
    provider: str
    status: str
    message_id: str = ""

    def public_payload(self) -> dict[str, str]:
        payload = {"provider": self.provider, "status": self.status}
        if self.message_id:
            payload["messageId"] = self.message_id
        return payload


class SmsProvider:
    provider_name = "unknown"

    def ensure_available(self) -> None:
        return None

    def send_otp(self, *, phone: str, code: str, ttl_seconds: int, purpose: str = "login") -> SmsSendResult:
        raise NotImplementedError


class DisabledSmsProvider(SmsProvider):
    provider_name = "disabled"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def ensure_available(self) -> None:
        raise SmsProviderUnavailable(self.reason)

    def send_otp(self, *, phone: str, code: str, ttl_seconds: int, purpose: str = "login") -> SmsSendResult:
        self.ensure_available()
        raise SmsProviderUnavailable(self.reason)


class LocalMockSmsProvider(SmsProvider):
    provider_name = "mock"

    def send_otp(self, *, phone: str, code: str, ttl_seconds: int, purpose: str = "login") -> SmsSendResult:
        return SmsSendResult(provider=self.provider_name, status="mocked")


class WebhookSmsProvider(SmsProvider):
    provider_name = "webhook"

    def __init__(self, url: str, *, token: str = "", timeout_seconds: float = 5.0) -> None:
        if not url:
            raise SmsProviderUnavailable("SMS_WEBHOOK_URL is required for webhook SMS provider")
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds

    def send_otp(self, *, phone: str, code: str, ttl_seconds: int, purpose: str = "login") -> SmsSendResult:
        body = {
            "phone": phone,
            "code": code,
            "ttlSeconds": int(ttl_seconds),
            "purpose": purpose,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
            try:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                response_body = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:
            raise SmsSendError(f"SMS webhook returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SmsSendError("SMS webhook request failed") from exc

        if status < 200 or status >= 300:
            raise SmsSendError(f"SMS webhook returned HTTP {status}")

        message_id = _message_id_from_response(response_body)
        return SmsSendResult(provider=self.provider_name, status="sent", message_id=message_id)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "waimai-image-tool/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def provider_from_env(
    env: Mapping[str, str] | None = None,
    *,
    local_demo_enabled: bool = False,
) -> SmsProvider:
    values = env if env is not None else os.environ
    configured = str(values.get("SMS_PROVIDER", "")).strip().lower()
    if not configured:
        if local_demo_enabled:
            return LocalMockSmsProvider()
        return DisabledSmsProvider("SMS provider is not configured")

    if configured in {"disabled", "none", "off"}:
        return DisabledSmsProvider("SMS provider is disabled")

    if configured in {"local", "mock"}:
        if local_demo_enabled:
            return LocalMockSmsProvider()
        return DisabledSmsProvider("local/mock SMS provider is only allowed for local demo requests")

    if configured == "webhook":
        webhook_url = str(values.get("SMS_WEBHOOK_URL", "")).strip()
        if not webhook_url:
            return DisabledSmsProvider("SMS_WEBHOOK_URL is required for webhook SMS provider")
        return WebhookSmsProvider(
            webhook_url,
            token=str(values.get("SMS_WEBHOOK_TOKEN", "")).strip(),
            timeout_seconds=_float_from_env(values, "SMS_WEBHOOK_TIMEOUT", 5.0),
        )

    return DisabledSmsProvider(f"unsupported SMS_PROVIDER: {configured}")


def _message_id_from_response(response_body: bytes) -> str:
    if not response_body:
        return ""
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("messageId", "message_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _float_from_env(values: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(str(values.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
