from __future__ import annotations

import json
import urllib.error

import pytest

import sms_service


def test_local_demo_uses_mock_provider_without_env() -> None:
    provider = sms_service.provider_from_env({}, local_demo_enabled=True)

    result = provider.send_otp(phone="+8613800138000", code="123456", ttl_seconds=300)

    assert isinstance(provider, sms_service.LocalMockSmsProvider)
    assert result.public_payload() == {"provider": "mock", "status": "mocked"}


def test_missing_provider_is_unavailable_outside_local_demo() -> None:
    provider = sms_service.provider_from_env({}, local_demo_enabled=False)

    with pytest.raises(sms_service.SmsProviderUnavailable) as exc:
        provider.ensure_available()

    assert exc.value.code == sms_service.ERR_SMS_PROVIDER_UNAVAILABLE


def test_mock_provider_is_guarded_outside_local_demo() -> None:
    provider = sms_service.provider_from_env({"SMS_PROVIDER": "mock"}, local_demo_enabled=False)

    with pytest.raises(sms_service.SmsProviderUnavailable) as exc:
        provider.ensure_available()

    assert "local/mock" in str(exc.value)


def test_webhook_provider_posts_otp_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeResponse:
        status = 202

        def read(self) -> bytes:
            return b'{"messageId":"sms_msg_1"}'

        def close(self) -> None:
            return None

    def fake_urlopen(request, timeout):
        calls.append({"request": request, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(sms_service.urllib.request, "urlopen", fake_urlopen)
    provider = sms_service.provider_from_env(
        {
            "SMS_PROVIDER": "webhook",
            "SMS_WEBHOOK_URL": "https://sms.example.test/send",
            "SMS_WEBHOOK_TOKEN": "secret-token",
            "SMS_WEBHOOK_TIMEOUT": "2.5",
        },
        local_demo_enabled=False,
    )

    result = provider.send_otp(phone="+8613800138000", code="654321", ttl_seconds=300)

    assert result.public_payload() == {"provider": "webhook", "status": "sent", "messageId": "sms_msg_1"}
    assert len(calls) == 1
    sent_request = calls[0]["request"]
    assert sent_request.full_url == "https://sms.example.test/send"
    assert calls[0]["timeout"] == 2.5
    assert sent_request.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(sent_request.data.decode("utf-8")) == {
        "phone": "+8613800138000",
        "code": "654321",
        "ttlSeconds": 300,
        "purpose": "login",
    }


def test_webhook_provider_maps_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(sms_service.urllib.request, "urlopen", fake_urlopen)
    provider = sms_service.provider_from_env(
        {"SMS_PROVIDER": "webhook", "SMS_WEBHOOK_URL": "https://sms.example.test/send"},
        local_demo_enabled=False,
    )

    with pytest.raises(sms_service.SmsSendError) as exc:
        provider.send_otp(phone="+8613800138000", code="654321", ttl_seconds=300)

    assert exc.value.code == sms_service.ERR_SMS_SEND_FAILED
