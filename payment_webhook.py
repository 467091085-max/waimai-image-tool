from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Mapping

import billing

MOCK_SIGNATURE = "mock-valid"


def handle_payment_webhook(
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], int]:
    provider = str(payload.get("provider") or billing.MOCK_PAYMENT_PROVIDER)
    try:
        provider = _provider(provider)
    except billing.BillingError as exc:
        return billing.billing_error_response(exc)

    if provider != billing.MOCK_PAYMENT_PROVIDER and not billing.payment_provider_configured(provider):
        return {
            "ok": False,
            "code": "payment_provider_not_configured",
            "status": "not_configured",
            "provider": provider,
            "message": "Real payment credentials are not configured; use mock provider for readiness testing.",
        }, 501

    signature = _header(headers or {}, "X-Payment-Signature") or _header(headers or {}, "X-Mock-Signature")
    verification = verify_payment_webhook_signature(provider, payload, signature=signature)
    if not verification["valid"]:
        event_id = str(payload.get("eventId") or payload.get("event_id") or _default_event_id(provider, payload))
        order_id = str(payload.get("paymentOrderId") or payload.get("payment_order_id") or "")
        if order_id:
            result = billing.process_payment_webhook_event(
                event_id=event_id,
                payment_order_id=order_id,
                status=str(payload.get("status") or "failed"),
                provider=provider,
                signature_valid=False,
                provider_trade_id=str(payload.get("providerTradeId") or payload.get("tradeId") or ""),
                db_path=db_path,
                metadata={"rawPayload": dict(payload), "signatureMode": verification["mode"]},
            )
            return {**result, "code": "payment_signature_invalid"}, 401
        return {
            "ok": False,
            "code": "payment_signature_invalid",
            "status": "signature_invalid",
            "provider": provider,
            "message": verification["message"],
        }, 401

    result = billing.process_payment_webhook_event(
        event_id=str(payload.get("eventId") or payload.get("event_id") or _default_event_id(provider, payload)),
        payment_order_id=str(payload.get("paymentOrderId") or payload.get("payment_order_id") or ""),
        status=str(payload.get("status") or "paid"),
        provider=provider,
        signature_valid=True,
        provider_trade_id=str(payload.get("providerTradeId") or payload.get("tradeId") or ""),
        db_path=db_path,
        metadata={"rawPayload": dict(payload), "signatureMode": verification["mode"]},
    )
    return result, 200 if result.get("ok") else 409


def verify_payment_webhook_signature(
    provider: str,
    payload: Mapping[str, Any],
    *,
    signature: str | None,
) -> dict[str, Any]:
    provider = _provider(provider)
    if provider == billing.MOCK_PAYMENT_PROVIDER:
        return {
            "valid": signature == MOCK_SIGNATURE or payload.get("mockSignature") == MOCK_SIGNATURE,
            "mode": "mock-stub",
            "message": "mock signature accepted" if signature == MOCK_SIGNATURE else "mock signature missing or invalid",
        }

    secret = os.environ.get("PAYMENT_WEBHOOK_STUB_SECRET", "")
    if not secret:
        return {
            "valid": False,
            "mode": "stub-unconfigured",
            "message": "Payment webhook signature secret is not configured.",
        }

    expected = hmac.new(
        secret.encode("utf-8"),
        str(payload.get("paymentOrderId") or payload.get("payment_order_id") or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "valid": hmac.compare_digest(str(signature or ""), expected),
        "mode": "stub-hmac",
        "message": "stub signature accepted" if signature == expected else "stub signature invalid",
    }


def _provider(provider: str) -> str:
    return billing._payment_provider(provider)


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


def _default_event_id(provider: str, payload: Mapping[str, Any]) -> str:
    order_id = str(payload.get("paymentOrderId") or payload.get("payment_order_id") or "missing")
    status = str(payload.get("status") or "paid")
    trade_id = str(payload.get("providerTradeId") or payload.get("tradeId") or "")
    return f"{provider}:{order_id}:{status}:{trade_id or 'event'}"
