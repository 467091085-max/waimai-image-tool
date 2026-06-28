from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping
from urllib.parse import urlencode
from uuid import uuid4

import payment_rules
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


STATUS_PENDING = payment_rules.STATUS_PENDING
STATUS_PAID = payment_rules.STATUS_PAID
STATUS_FAILED = payment_rules.STATUS_FAILED
STATUS_REFUNDED = payment_rules.STATUS_REFUNDED
STATUS_CLOSED = payment_rules.STATUS_CLOSED

SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_order_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    points INTEGER NOT NULL CHECK (points > 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'closed', 'refunded')),
    idempotency_key TEXT UNIQUE,
    provider_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    paid_at TEXT,
    closed_at TEXT,
    refunded_at TEXT,
    UNIQUE (provider, provider_order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_orders_user_created
    ON payment_orders (user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_payment_orders_status_created
    ON payment_orders (status, created_at);

CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    provider_order_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_status TEXT NOT NULL CHECK (target_status IN ('pending', 'paid', 'failed', 'closed', 'refunded')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (provider, provider_event_id),
    FOREIGN KEY (order_id) REFERENCES payment_orders(order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_events_order_created
    ON payment_events (order_id, created_at);
"""

SIGNATURE_KEYS = frozenset({"signature", "sign", "hmac", "sig"})
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})
REAL_PAYMENT_PROVIDERS = frozenset({"wechat", "alipay"})
LIVE_PAYMENT_ENVIRONMENTS = frozenset({"production", "prod", "staging", "render"})
PAYMENT_PROVIDER_ALIASES = {
    "wechatpay": "wechat",
    "weixin": "wechat",
    "weixinpay": "wechat",
    "wxpay": "wechat",
    "ali-pay": "alipay",
    "ali_pay": "alipay",
}
PAYMENT_WEBHOOK_SECRET_ENV_NAMES = ("PAYMENT_WEBHOOK_SECRET", "FAKE_PAYMENT_WEBHOOK_SECRET")
PAYMENT_PROVIDER_SELECTION_CONFIG = (
    {
        "key": "payment_provider",
        "env": ("PAYMENT_PROVIDER",),
        "allowedValues": ("wechat", "alipay"),
    },
)
PAYMENT_PROVIDER_REQUIRED_CONFIG = {
    "wechat": PAYMENT_PROVIDER_SELECTION_CONFIG
    + (
        {"key": "wechat_app_id", "env": ("WECHAT_PAY_APP_ID", "WECHAT_APP_ID")},
        {"key": "wechat_merchant_id", "env": ("WECHAT_PAY_MCH_ID", "WECHAT_MCH_ID")},
        {"key": "wechat_api_v3_key", "env": ("WECHAT_PAY_API_V3_KEY", "WECHAT_PAY_API_KEY")},
        {
            "key": "wechat_private_key",
            "env": ("WECHAT_PAY_PRIVATE_KEY", "WECHAT_PAY_PRIVATE_KEY_PATH"),
        },
        {
            "key": "wechat_certificate_serial",
            "env": ("WECHAT_PAY_CERT_SERIAL_NO", "WECHAT_PAY_SERIAL_NO"),
        },
        {
            "key": "wechat_platform_certificate",
            "env": ("WECHAT_PAY_PLATFORM_CERT", "WECHAT_PAY_PLATFORM_CERT_PATH"),
        },
        {
            "key": "payment_notify_url",
            "env": ("PAYMENT_NOTIFY_URL", "PAYMENT_CALLBACK_URL", "WECHAT_PAY_NOTIFY_URL"),
        },
    ),
    "alipay": PAYMENT_PROVIDER_SELECTION_CONFIG
    + (
        {"key": "alipay_app_id", "env": ("ALIPAY_APP_ID",)},
        {"key": "alipay_private_key", "env": ("ALIPAY_PRIVATE_KEY", "ALIPAY_PRIVATE_KEY_PATH")},
        {"key": "alipay_public_key", "env": ("ALIPAY_PUBLIC_KEY", "ALIPAY_PUBLIC_KEY_PATH")},
        {
            "key": "payment_notify_url",
            "env": ("PAYMENT_NOTIFY_URL", "PAYMENT_CALLBACK_URL", "ALIPAY_NOTIFY_URL"),
        },
    ),
}
ALIPAY_GATEWAY_URL = "https://openapi.alipay.com/gateway.do"
ALIPAY_SANDBOX_GATEWAY_URL = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
ALIPAY_PRIVATE_KEY_ENV_NAMES = ("ALIPAY_PRIVATE_KEY", "ALIPAY_PRIVATE_KEY_PATH")
ALIPAY_PUBLIC_KEY_ENV_NAMES = ("ALIPAY_PUBLIC_KEY", "ALIPAY_PUBLIC_KEY_PATH")
ALIPAY_NOTIFY_URL_ENV_NAMES = ("PAYMENT_NOTIFY_URL", "PAYMENT_CALLBACK_URL", "ALIPAY_NOTIFY_URL")
ALIPAY_RETURN_URL_ENV_NAMES = ("PAYMENT_RETURN_URL", "ALIPAY_RETURN_URL")
ALIPAY_SUCCESS_STATUSES = frozenset({"TRADE_SUCCESS", "TRADE_FINISHED"})
ALIPAY_CLOSED_STATUSES = frozenset({"TRADE_CLOSED"})
ALIPAY_PENDING_STATUSES = frozenset({"WAIT_BUYER_PAY"})


class PaymentServiceError(Exception):
    code = "payment_service_error"
    status_code = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}


class InvalidPaymentInput(PaymentServiceError, ValueError):
    code = "invalid_payment_input"


class PaymentOrderConflict(PaymentServiceError):
    code = "payment_order_conflict"


class PaymentOrderNotFound(PaymentServiceError, LookupError):
    code = "payment_order_not_found"


class PaymentSignatureError(PaymentServiceError):
    code = "payment_signature_error"


class PaymentProviderUnavailable(PaymentServiceError):
    code = "payment_provider_unavailable"
    status_code = 503


class PaymentAdapterNotImplemented(PaymentProviderUnavailable):
    code = "payment_adapter_not_implemented"


class PaymentProviderConfigError(PaymentProviderUnavailable):
    code = "payment_provider_config_error"


class FakePaymentProviderForbidden(PaymentServiceError):
    code = "fake_payment_provider_forbidden"
    status_code = 403


class PaymentTransitionError(PaymentServiceError, ValueError):
    code = "payment_transition_error"


def init_payment_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def fake_payment_provider_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if _runtime_environment_label(env) in LIVE_PAYMENT_ENVIRONMENTS:
        return False
    if _env_truthy(env.get("ENABLE_LOCAL_DEMO_BILLING"), default=True):
        return True
    if str(env.get("PAYMENT_PROVIDER") or "").strip().lower() == "fake":
        return True
    return _env_truthy(env.get("ALLOW_FAKE_PAYMENT_PROVIDER"), default=False)


def provider_readiness_for(provider: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = dict(os.environ if env is None else env)
    values["PAYMENT_PROVIDER"] = _clean_provider(provider)
    return assess_payment_provider_readiness(values)


def ensure_payment_checkout_available(provider: str, env: Mapping[str, str] | None = None) -> None:
    clean_provider = _clean_provider(provider)
    values = os.environ if env is None else env
    if clean_provider == "fake":
        if fake_payment_provider_enabled(values):
            return
        raise PaymentProviderUnavailable(
            "fake 支付 provider 未启用，不能创建 fake 支付订单",
            provider="fake",
            required="PAYMENT_PROVIDER=fake or ALLOW_FAKE_PAYMENT_PROVIDER=true",
        )

    readiness = provider_readiness_for(clean_provider, values)
    missing_config = list(readiness.get("missingConfig") or [])
    details = {
        "provider": clean_provider,
        "mode": readiness.get("mode"),
        "requiredConfig": readiness.get("requiredConfig") or [],
        "missingConfig": missing_config,
        "blockingIssues": readiness.get("blockingIssues") or [],
    }
    if missing_config:
        raise PaymentProviderUnavailable("真实支付 provider 配置不完整", **details)
    if readiness.get("ready"):
        return
    if clean_provider == "wechat":
        raise PaymentAdapterNotImplemented("真实支付 provider adapter 尚未接入", **details)
    raise PaymentProviderUnavailable("真实支付 provider 不可用", **details)


def assess_payment_provider_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return an explicit payment-provider readiness report.

    The current runtime implements only the local fake payment provider. Real
    providers are recognized here so deployment checks can fail closed until a
    real SDK adapter and callback-signature verifier are implemented.
    """
    values = os.environ if env is None else env
    raw_provider = _env_value(values, "PAYMENT_PROVIDER")
    provider = _normalize_payment_provider(raw_provider or "fake")
    app_env = _runtime_environment_label(values)
    live_env = app_env in LIVE_PAYMENT_ENVIRONMENTS
    local_demo_enabled = _env_truthy(values.get("ENABLE_LOCAL_DEMO_BILLING"), default=True)

    warnings: list[str] = []
    errors: list[str] = []
    missing_config: list[str] = []

    if provider == "fake":
        mode = "local_demo"
        required_config = [] if not live_env and local_demo_enabled else _required_config_items(
            PAYMENT_PROVIDER_SELECTION_CONFIG
        )
        if live_env:
            errors.append("fake_payment_provider_forbidden_in_live_environment")
        if app_env in {"production", "prod"}:
            errors.append("fake_payment_provider_forbidden_in_production")
        if not local_demo_enabled:
            errors.append("local_demo_billing_disabled")
        if errors:
            errors.insert(0, "real_payment_provider_required")
        else:
            warnings.append("fake_payment_provider_is_for_development_only")
            if not _first_env_value(values, PAYMENT_WEBHOOK_SECRET_ENV_NAMES):
                warnings.append("fake_payment_webhook_secret_not_configured")
            if not raw_provider:
                warnings.append("payment_provider_not_configured_defaulting_to_fake")
    elif provider in REAL_PAYMENT_PROVIDERS:
        mode = "real_provider"
        required_config = _required_config_items(PAYMENT_PROVIDER_REQUIRED_CONFIG[provider])
        missing_config = _missing_required_config(values, required_config)
        if missing_config:
            errors.append("payment_provider_credentials_required")
        if provider == "wechat":
            errors.append("wechat_payment_adapter_not_implemented")
            errors.append("real_payment_callback_signature_verification_not_implemented")
            warnings.append("real_payment_credentials_do_not_make_current_adapter_production_ready")
        elif not missing_config:
            try:
                _load_alipay_private_key(values)
                _load_alipay_public_key(values)
            except PaymentProviderConfigError as exc:
                errors.append("alipay_payment_key_invalid")
                warnings.append(str(exc.message))
            else:
                warnings.append("alipay_page_pay_adapter_enabled")
    else:
        mode = "unknown"
        required_config = _required_config_items(PAYMENT_PROVIDER_SELECTION_CONFIG)
        errors.append("unsupported_payment_provider")
        if live_env or not local_demo_enabled:
            errors.append("real_payment_provider_required")

    return {
        "ready": not errors,
        "provider": provider,
        "mode": mode,
        "appEnv": app_env,
        "warnings": warnings,
        "errors": errors,
        "blockingIssues": list(errors),
        "requiredConfig": required_config,
        "missingConfig": missing_config,
    }


def create_payment_order(
    conn: sqlite3.Connection,
    user_id: str,
    amount_cents: int,
    points: int,
    provider: str = "fake",
    order_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    init_payment_schema(conn)
    provider = _clean_provider(provider)
    user_id = _clean_text(user_id, "user_id")
    amount_cents = _positive_int(amount_cents, "amount_cents")
    points = _positive_int(points, "points")
    order_id = _clean_text(order_id, "order_id") if order_id is not None else f"pay_{uuid4().hex}"
    idempotency_key = _optional_clean_text(idempotency_key, "idempotency_key")

    with _transaction(conn):
        if idempotency_key:
            existing = _fetch_order_by_idempotency_key(conn, idempotency_key)
            if existing:
                return _order_payload(existing, idempotent=True)

        existing = _fetch_order(conn, order_id)
        if existing:
            _ensure_same_order_request(
                existing,
                user_id=user_id,
                provider=provider,
                amount_cents=amount_cents,
                points=points,
                idempotency_key=idempotency_key,
            )
            return _order_payload(existing, idempotent=True)

        now = _now()
        provider_order_id = order_id
        conn.execute(
            """
            INSERT INTO payment_orders (
                order_id, user_id, provider, provider_order_id, amount_cents, points,
                status, idempotency_key, provider_payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                order_id,
                user_id,
                provider,
                provider_order_id,
                amount_cents,
                points,
                STATUS_PENDING,
                idempotency_key,
                now,
                now,
            ),
        )
        order = _fetch_order(conn, order_id)
        if order is None:
            raise PaymentOrderNotFound("Payment order was not created", orderId=order_id)
        return _order_payload(order, idempotent=False)


def attach_payment_provider_payload(
    conn: sqlite3.Connection,
    order_id: str,
    provider_payload: Mapping[str, Any],
) -> dict[str, Any]:
    init_payment_schema(conn)
    order_id = _clean_text(order_id, "order_id")
    payload_json = _json(dict(provider_payload))
    with _transaction(conn):
        conn.execute(
            "UPDATE payment_orders SET provider_payload_json = ?, updated_at = ? WHERE order_id = ?",
            (payload_json, _now(), order_id),
        )
        order = _fetch_order(conn, order_id)
        if order is None:
            raise PaymentOrderNotFound("Payment order not found", orderId=order_id)
        return _order_payload(order, idempotent=False)


def fake_payment_url(order: dict[str, Any] | sqlite3.Row) -> str:
    payload = _coerce_mapping(order)
    provider_order_id = _clean_text(
        str(payload.get("provider_order_id") or payload.get("providerOrderId") or payload.get("order_id") or ""),
        "provider_order_id",
    )
    amount_cents = int(payload.get("amount_cents") or payload.get("amountCents") or 0)
    points = int(payload.get("points") or 0)
    return "fakepay://checkout?" + urlencode(
        {
            "provider_order_id": provider_order_id,
            "amount_cents": amount_cents,
            "points": points,
        }
    )


def payment_instructions(order: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    payload = _coerce_mapping(order)
    provider = str(payload.get("provider") or "fake")
    provider_order_id = str(
        payload.get("provider_order_id") or payload.get("providerOrderId") or payload.get("order_id") or ""
    )
    instructions = {
        "provider": provider,
        "provider_order_id": provider_order_id,
        "providerOrderId": provider_order_id,
        "payment_url": fake_payment_url(payload) if provider == "fake" else "",
        "paymentUrl": fake_payment_url(payload) if provider == "fake" else "",
    }
    provider_payload = _provider_payload(payload)
    if provider == "alipay" and provider_payload:
        payment_url = str(provider_payload.get("payment_url") or provider_payload.get("paymentUrl") or "")
        instructions.update(
            {
                "payment_url": payment_url,
                "paymentUrl": payment_url,
                "method": str(provider_payload.get("method") or "GET"),
                "gateway": str(provider_payload.get("gateway") or ALIPAY_GATEWAY_URL),
                "checkout": provider_payload,
            }
        )
    return instructions


def create_payment_checkout(
    order: dict[str, Any] | sqlite3.Row,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload = _coerce_mapping(order)
    provider = _clean_provider(str(payload.get("provider") or "fake"))
    if provider == "fake":
        return payment_instructions(payload)
    if provider == "alipay":
        return _create_alipay_page_pay_checkout(payload, os.environ if env is None else env)
    raise PaymentAdapterNotImplemented(
        "真实支付 provider adapter 尚未接入",
        provider=provider,
        blockingIssues=[f"{provider}_payment_adapter_not_implemented"],
    )


def fake_callback_signature(
    provider: str,
    provider_order_id: str,
    event_type: str,
    payload: dict[str, Any] | None,
    secret: str,
) -> str:
    message = _fake_signature_messages(provider, provider_order_id, event_type, payload)[0]
    return hmac.new(str(secret).encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def handle_payment_callback(
    conn: sqlite3.Connection,
    provider: str,
    provider_order_id: str,
    event_type: str,
    payload: dict[str, Any] | None,
    secret: str = "",
) -> dict[str, Any]:
    init_payment_schema(conn)
    provider = _clean_provider(provider)
    provider_order_id = _clean_text(provider_order_id, "provider_order_id")
    event_type = _clean_text(event_type, "event_type")
    payload = _payload_dict(payload)

    _verify_callback_signature(provider, provider_order_id, event_type, payload, secret)
    target_status = _target_status(event_type, payload)
    provider_event_id = _provider_event_id(provider, provider_order_id, event_type, payload)

    return _record_payment_event(
        conn,
        provider=provider,
        provider_order_id=provider_order_id,
        event_type=event_type,
        payload=payload,
        target_status=target_status,
        provider_event_id=provider_event_id,
    )


def reconcile_payment_event(
    conn: sqlite3.Connection,
    provider: str,
    provider_order_id: str,
    target_status: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    """Record a manually verified payment event without provider signature checks."""
    init_payment_schema(conn)
    provider = _clean_provider(provider)
    provider_order_id = _clean_text(provider_order_id, "provider_order_id")
    target_status = _clean_payment_status(target_status, "target_status")
    event_type = _optional_clean_text(event_type, "event_type") or f"manual_{target_status}"
    payload = _payload_dict(payload)
    provider_event_id = (
        _clean_text(event_id, "event_id")
        if event_id is not None and str(event_id).strip()
        else _provider_event_id(provider, provider_order_id, event_type, payload)
    )

    return _record_payment_event(
        conn,
        provider=provider,
        provider_order_id=provider_order_id,
        event_type=event_type,
        payload=payload,
        target_status=target_status,
        provider_event_id=provider_event_id,
    )


def _record_payment_event(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_order_id: str,
    event_type: str,
    payload: dict[str, Any],
    target_status: str,
    provider_event_id: str,
) -> dict[str, Any]:
    provider = _clean_provider(provider)
    provider_order_id = _clean_text(provider_order_id, "provider_order_id")
    event_type = _clean_text(event_type, "event_type")
    target_status = _clean_payment_status(target_status, "target_status")
    provider_event_id = _clean_text(provider_event_id, "provider_event_id")
    payload = _payload_dict(payload)

    with _transaction(conn):
        existing_event = _fetch_event_by_provider_event_id(conn, provider, provider_event_id)
        if existing_event:
            order = _fetch_order_by_provider_order_id(conn, provider, provider_order_id)
            if order is None:
                raise PaymentOrderNotFound(
                    "Payment order not found for duplicate event",
                    provider=provider,
                    providerOrderId=provider_order_id,
                    eventId=provider_event_id,
                )
            return _callback_payload(
                order,
                event=existing_event,
                event_type=event_type,
                target_status=existing_event["target_status"],
                previous_status=order["status"],
                idempotent=True,
                points_to_credit=0,
                points_to_refund=0,
            )

        order = _fetch_order_by_provider_order_id(conn, provider, provider_order_id)
        if order is None:
            raise PaymentOrderNotFound(
                "Payment order not found",
                provider=provider,
                providerOrderId=provider_order_id,
            )

        previous_status = str(order["status"])
        try:
            payment_rules.transition_payment_status(previous_status, target_status)
        except ValueError as exc:
            raise PaymentTransitionError(
                str(exc),
                orderId=order["order_id"],
                currentStatus=previous_status,
                targetStatus=target_status,
            ) from exc

        points_to_credit = int(order["points"]) if previous_status != STATUS_PAID and target_status == STATUS_PAID else 0
        points_to_refund = (
            _points_to_refund(payload, amount_cents=int(order["amount_cents"]), points=int(order["points"]))
            if previous_status != STATUS_REFUNDED and target_status == STATUS_REFUNDED
            else 0
        )

        now = _now()
        conn.execute(
            """
            INSERT INTO payment_events (
                provider, provider_event_id, provider_order_id, order_id, event_type,
                target_status, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                provider_event_id,
                provider_order_id,
                order["order_id"],
                event_type,
                target_status,
                _json(payload),
                now,
            ),
        )
        event = _fetch_event_by_provider_event_id(conn, provider, provider_event_id)
        if event is None:
            raise PaymentServiceError("Payment event was not created", eventId=provider_event_id)

        if target_status != previous_status:
            updates = ["status = ?", "updated_at = ?"]
            params: list[Any] = [target_status, now]
            if target_status == STATUS_PAID:
                updates.append("paid_at = COALESCE(paid_at, ?)")
                params.append(now)
            elif target_status in {STATUS_FAILED, STATUS_CLOSED}:
                updates.append("closed_at = COALESCE(closed_at, ?)")
                params.append(now)
            elif target_status == STATUS_REFUNDED:
                updates.append("refunded_at = COALESCE(refunded_at, ?)")
                params.append(now)
            params.append(order["order_id"])
            conn.execute(
                f"UPDATE payment_orders SET {', '.join(updates)} WHERE order_id = ?",
                tuple(params),
            )
            order = _fetch_order(conn, str(order["order_id"]))
            if order is None:
                raise PaymentOrderNotFound("Payment order disappeared", orderId=str(order["order_id"]))

        return _callback_payload(
            order,
            event=event,
            event_type=event_type,
            target_status=target_status,
            previous_status=previous_status,
            idempotent=False,
            points_to_credit=points_to_credit,
            points_to_refund=points_to_refund,
        )


def _order_payload(order: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
    provider_payload = _json_loads(str(order.get("provider_payload_json") or "{}"))
    payload = {
        "ok": True,
        "idempotent": idempotent,
        "order_id": order["order_id"],
        "orderId": order["order_id"],
        "user_id": order["user_id"],
        "userId": order["user_id"],
        "provider": order["provider"],
        "provider_order_id": order["provider_order_id"],
        "providerOrderId": order["provider_order_id"],
        "amount_cents": int(order["amount_cents"]),
        "amountCents": int(order["amount_cents"]),
        "points": int(order["points"]),
        "status": order["status"],
        "idempotency_key": order["idempotency_key"],
        "idempotencyKey": order["idempotency_key"],
        "created_at": order["created_at"],
        "createdAt": order["created_at"],
        "updated_at": order["updated_at"],
        "updatedAt": order["updated_at"],
        "paid_at": order["paid_at"],
        "paidAt": order["paid_at"],
        "closed_at": order["closed_at"],
        "closedAt": order["closed_at"],
        "refunded_at": order["refunded_at"],
        "refundedAt": order["refunded_at"],
        "provider_payload": provider_payload,
        "providerPayload": provider_payload,
    }
    if order["provider"] == "fake":
        payload["payment_url"] = fake_payment_url(payload)
        payload["paymentUrl"] = payload["payment_url"]
    return payload


def _callback_payload(
    order: dict[str, Any],
    *,
    event: dict[str, Any],
    event_type: str,
    target_status: str,
    previous_status: str,
    idempotent: bool,
    points_to_credit: int,
    points_to_refund: int,
) -> dict[str, Any]:
    order_payload = _order_payload(order, idempotent=False)
    return {
        "ok": True,
        "idempotent": idempotent,
        "event_id": event["provider_event_id"],
        "eventId": event["provider_event_id"],
        "event_type": event_type,
        "eventType": event_type,
        "status": order["status"],
        "target_status": target_status,
        "targetStatus": target_status,
        "previous_status": previous_status,
        "previousStatus": previous_status,
        "order_id": order["order_id"],
        "orderId": order["order_id"],
        "provider_order_id": order["provider_order_id"],
        "providerOrderId": order["provider_order_id"],
        "points": int(order["points"]),
        "points_to_credit": points_to_credit,
        "pointsToCredit": points_to_credit,
        "points_to_refund": points_to_refund,
        "pointsToRefund": points_to_refund,
        "order": order_payload,
    }


def _ensure_same_order_request(
    existing: dict[str, Any],
    *,
    user_id: str,
    provider: str,
    amount_cents: int,
    points: int,
    idempotency_key: str | None,
) -> None:
    if (
        existing["user_id"] == user_id
        and existing["provider"] == provider
        and int(existing["amount_cents"]) == amount_cents
        and int(existing["points"]) == points
        and (idempotency_key is None or existing["idempotency_key"] == idempotency_key)
    ):
        return
    raise PaymentOrderConflict(
        "Payment order already exists for a different request",
        orderId=existing["order_id"],
    )


def _fetch_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any] | None:
    return _fetchone_dict(conn.execute("SELECT * FROM payment_orders WHERE order_id = ?", (order_id,)))


def _fetch_order_by_idempotency_key(conn: sqlite3.Connection, idempotency_key: str) -> dict[str, Any] | None:
    return _fetchone_dict(
        conn.execute("SELECT * FROM payment_orders WHERE idempotency_key = ?", (idempotency_key,))
    )


def _fetch_order_by_provider_order_id(
    conn: sqlite3.Connection,
    provider: str,
    provider_order_id: str,
) -> dict[str, Any] | None:
    return _fetchone_dict(
        conn.execute(
            "SELECT * FROM payment_orders WHERE provider = ? AND provider_order_id = ?",
            (provider, provider_order_id),
        )
    )


def _fetch_event_by_provider_event_id(
    conn: sqlite3.Connection,
    provider: str,
    provider_event_id: str,
) -> dict[str, Any] | None:
    return _fetchone_dict(
        conn.execute(
            "SELECT * FROM payment_events WHERE provider = ? AND provider_event_id = ?",
            (provider, provider_event_id),
        )
    )


def _fetchone_dict(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row))


def _target_status(event_type: str, payload: dict[str, Any]) -> str:
    status = payload.get("status") or payload.get("payment_status") or payload.get("paymentStatus")
    if isinstance(status, str) and status.strip():
        return _clean_payment_status(status, "status")

    normalized = event_type.strip().lower().replace("-", "_").replace(".", "_")
    aliases = {
        "pay_success": STATUS_PAID,
        "payment_success": STATUS_PAID,
        "paid": STATUS_PAID,
        "pending": STATUS_PENDING,
        "success": STATUS_PAID,
        "succeeded": STATUS_PAID,
        "pay_failed": STATUS_FAILED,
        "payment_failed": STATUS_FAILED,
        "failed": STATUS_FAILED,
        "fail": STATUS_FAILED,
        "closed": STATUS_CLOSED,
        "close": STATUS_CLOSED,
        "canceled": STATUS_CLOSED,
        "cancelled": STATUS_CLOSED,
        "cancel": STATUS_CLOSED,
        "refund": STATUS_REFUNDED,
        "refunded": STATUS_REFUNDED,
        "refund_success": STATUS_REFUNDED,
        "payment_refunded": STATUS_REFUNDED,
    }
    if normalized not in aliases:
        raise InvalidPaymentInput("Unsupported payment event type", eventType=event_type)
    return aliases[normalized]


def _clean_payment_status(status: str, name: str) -> str:
    normalized_status = _clean_text(status, name).lower()
    if normalized_status not in payment_rules.PAYMENT_STATUSES:
        raise InvalidPaymentInput("Unsupported payment status", **{name: status})
    return normalized_status


def _points_to_refund(payload: dict[str, Any], *, amount_cents: int, points: int) -> int:
    refund_cents = (
        payload.get("refund_cents")
        if "refund_cents" in payload
        else payload.get("refundCents", payload.get("refund_amount_cents", payload.get("amount_cents")))
    )
    if refund_cents is None:
        return points
    return payment_rules.refund_points(_non_negative_int(refund_cents, "refund_cents"), amount_cents, points)


def _provider_event_id(
    provider: str,
    provider_order_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    for key in ("event_id", "eventId", "id", "notify_id", "notifyId"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return payment_rules.idempotency_key(provider, provider_order_id, event_type)


def _verify_callback_signature(
    provider: str,
    provider_order_id: str,
    event_type: str,
    payload: dict[str, Any],
    secret: str,
) -> None:
    if provider == "alipay":
        _verify_alipay_callback_signature(payload, secret)
        return
    if provider != "fake":
        raise InvalidPaymentInput("Unsupported payment provider", provider=provider)
    if not secret:
        raise PaymentSignatureError("Missing payment callback signing secret", provider=provider)

    signature = _payload_signature(payload)
    if not signature:
        raise PaymentSignatureError("Missing payment callback signature", provider=provider)

    expected = {
        hmac.new(str(secret).encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        for message in _fake_signature_messages(provider, provider_order_id, event_type, payload)
    }
    if not any(hmac.compare_digest(signature.lower(), item.lower()) for item in expected):
        raise PaymentSignatureError("Invalid payment callback signature", provider=provider)


def _create_alipay_page_pay_checkout(order: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    app_id = _first_env_value(env, ("ALIPAY_APP_ID",))
    notify_url = _first_env_value(env, ALIPAY_NOTIFY_URL_ENV_NAMES)
    return_url = _first_env_value(env, ALIPAY_RETURN_URL_ENV_NAMES)
    gateway = _alipay_gateway_url(env)
    private_key = _load_alipay_private_key(env)
    provider_order_id = _clean_text(
        str(order.get("provider_order_id") or order.get("providerOrderId") or order.get("order_id") or ""),
        "provider_order_id",
    )
    amount_cents = _positive_int(int(order.get("amount_cents") or order.get("amountCents") or 0), "amount_cents")
    points = _positive_int(int(order.get("points") or 0), "points")
    biz_content = _json(
        {
            "body": f"{points} points",
            "out_trade_no": provider_order_id,
            "product_code": "FAST_INSTANT_TRADE_PAY",
            "subject": f"外卖菜品图积分充值 {points} 积分",
            "total_amount": _alipay_amount(amount_cents),
        }
    )
    params: dict[str, str] = {
        "app_id": app_id,
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": notify_url,
        "biz_content": biz_content,
    }
    if return_url:
        params["return_url"] = return_url
    params["sign"] = _alipay_rsa2_sign(_alipay_signing_string(params), private_key)
    payment_url = f"{gateway}?{urlencode(params)}"
    return {
        "provider": "alipay",
        "provider_order_id": provider_order_id,
        "providerOrderId": provider_order_id,
        "payment_url": payment_url,
        "paymentUrl": payment_url,
        "gateway": gateway,
        "method": "GET",
        "apiMethod": "alipay.trade.page.pay",
        "signType": "RSA2",
        "amountCents": amount_cents,
        "points": points,
    }


def alipay_callback_event_type(payload: Mapping[str, Any]) -> str:
    status = str(payload.get("trade_status") or payload.get("tradeStatus") or "").strip().upper()
    if status in ALIPAY_SUCCESS_STATUSES:
        return "pay_success"
    if status in ALIPAY_CLOSED_STATUSES:
        return "closed"
    if status in ALIPAY_PENDING_STATUSES:
        return "pending"
    raise InvalidPaymentInput("Unsupported Alipay trade status", tradeStatus=status)


def alipay_provider_order_id(payload: Mapping[str, Any]) -> str:
    return _clean_text(str(payload.get("out_trade_no") or payload.get("outTradeNo") or ""), "out_trade_no")


def alipay_public_key(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    _load_alipay_public_key(values)
    return _read_env_or_file(values, ALIPAY_PUBLIC_KEY_ENV_NAMES, "alipay_public_key")


def _verify_alipay_callback_signature(payload: Mapping[str, Any], public_key_text: str) -> None:
    if not public_key_text:
        raise PaymentSignatureError("Missing Alipay public key", provider="alipay")
    signature = str(payload.get("sign") or "").strip()
    if not signature:
        raise PaymentSignatureError("Missing payment callback signature", provider="alipay")
    public_key = _deserialize_alipay_public_key(public_key_text)
    candidates = {
        _alipay_signing_string(payload, exclude_sign_type=False),
        _alipay_signing_string(payload, exclude_sign_type=True),
    }
    signature_bytes = base64.b64decode(signature)
    for message in candidates:
        try:
            public_key.verify(
                signature_bytes,
                message.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return
        except InvalidSignature:
            continue
    raise PaymentSignatureError("Invalid payment callback signature", provider="alipay")


def _alipay_rsa2_sign(message: str, private_key: rsa.RSAPrivateKey) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _alipay_signing_string(params: Mapping[str, Any], *, exclude_sign_type: bool = False) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if key == "sign" or (exclude_sign_type and key == "sign_type"):
            continue
        if value is None:
            continue
        text = str(value)
        if text == "":
            continue
        pairs.append((str(key), text))
    return "&".join(f"{key}={value}" for key, value in sorted(pairs, key=lambda item: item[0]))


def _load_alipay_private_key(env: Mapping[str, str]) -> rsa.RSAPrivateKey:
    try:
        return serialization.load_pem_private_key(
            _normalize_private_pem(_read_env_or_file(env, ALIPAY_PRIVATE_KEY_ENV_NAMES, "alipay_private_key")).encode("utf-8"),
            password=None,
        )
    except Exception as exc:
        raise PaymentProviderConfigError("支付宝私钥无效", provider="alipay", key="alipay_private_key") from exc


def _load_alipay_public_key(env: Mapping[str, str]):
    return _deserialize_alipay_public_key(_read_env_or_file(env, ALIPAY_PUBLIC_KEY_ENV_NAMES, "alipay_public_key"))


def _deserialize_alipay_public_key(value: str):
    try:
        return serialization.load_pem_public_key(_normalize_public_pem(value).encode("utf-8"))
    except Exception as exc:
        raise PaymentProviderConfigError("支付宝公钥无效", provider="alipay", key="alipay_public_key") from exc


def _read_env_or_file(env: Mapping[str, str], names: tuple[str, ...], key: str) -> str:
    direct = _env_value(env, names[0])
    if direct:
        return direct
    if len(names) > 1:
        path = _env_value(env, names[1])
        if path:
            try:
                return open(path, "r", encoding="utf-8").read().strip()
            except OSError as exc:
                raise PaymentProviderConfigError(f"{key} 文件不可读", provider="alipay", key=key, path=path) from exc
    raise PaymentProviderConfigError(f"{key} 未配置", provider="alipay", key=key)


def _normalize_public_pem(value: str) -> str:
    text = str(value or "").strip().replace("\\n", "\n")
    if "-----BEGIN" in text:
        return text
    compact = "".join(text.split())
    if not compact:
        return text
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(
        compact[index : index + 64] for index in range(0, len(compact), 64)
    ) + "\n-----END PUBLIC KEY-----"


def _normalize_private_pem(value: str) -> str:
    text = str(value or "").strip().replace("\\n", "\n")
    if "-----BEGIN" in text:
        return text
    compact = "".join(text.split())
    if not compact:
        return text
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(
        compact[index : index + 64] for index in range(0, len(compact), 64)
    ) + "\n-----END PRIVATE KEY-----"


def _alipay_gateway_url(env: Mapping[str, str]) -> str:
    explicit = _env_value(env, "ALIPAY_GATEWAY_URL")
    if explicit:
        return explicit
    if _env_truthy(env.get("ALIPAY_SANDBOX"), default=False):
        return ALIPAY_SANDBOX_GATEWAY_URL
    return ALIPAY_GATEWAY_URL


def _alipay_amount(amount_cents: int) -> str:
    cents = _positive_int(amount_cents, "amount_cents")
    return f"{cents // 100}.{cents % 100:02d}"


def _provider_payload(order: Mapping[str, Any]) -> dict[str, Any]:
    value = order.get("provider_payload")
    if isinstance(value, dict):
        return dict(value)
    value = order.get("providerPayload")
    if isinstance(value, dict):
        return dict(value)
    value = order.get("provider_payload_json")
    if value is not None:
        return _json_loads(str(value))
    return {}


def _fake_signature_messages(
    provider: str,
    provider_order_id: str,
    event_type: str,
    payload: dict[str, Any] | None,
) -> tuple[str, ...]:
    body = _payload_without_signature(_payload_dict(payload))
    body_json = _json(body)
    envelope = _json(
        {
            "event_type": _clean_text(event_type, "event_type").lower(),
            "payload": body,
            "provider": _clean_provider(provider),
            "provider_order_id": _clean_text(provider_order_id, "provider_order_id"),
        }
    )
    return (
        envelope,
        body_json,
        f"{_clean_provider(provider)}:{_clean_text(provider_order_id, 'provider_order_id')}:{_clean_text(event_type, 'event_type').lower()}:{body_json}",
        f"{_clean_text(provider_order_id, 'provider_order_id')}:{_clean_text(event_type, 'event_type').lower()}:{body_json}",
    )


def _payload_signature(payload: dict[str, Any]) -> str:
    for key in SIGNATURE_KEYS:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _payload_without_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in SIGNATURE_KEYS}


def _payload_dict(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise InvalidPaymentInput("Payment callback payload must be a dict")
    return dict(payload)


def _json_loads(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _coerce_mapping(order: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    if isinstance(order, sqlite3.Row):
        return dict(order)
    if isinstance(order, dict):
        return dict(order)
    raise InvalidPaymentInput("Payment order must be a mapping")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        if started:
            conn.rollback()
        raise
    else:
        if started:
            conn.commit()


def _clean_provider(provider: str) -> str:
    cleaned = _normalize_payment_provider(_clean_text(provider, "provider"))
    if cleaned != "fake" and cleaned not in REAL_PAYMENT_PROVIDERS:
        raise InvalidPaymentInput("Unsupported payment provider", provider=provider)
    return cleaned


def _env_truthy(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    cleaned = str(value).strip().lower()
    if not cleaned:
        return default
    if cleaned in TRUE_ENV_VALUES:
        return True
    if cleaned in FALSE_ENV_VALUES:
        return False
    return default


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if value is None:
        return ""
    return str(value).strip()


def _runtime_environment_label(env: Mapping[str, str]) -> str:
    app_env = _env_value(env, "APP_ENV").lower()
    if app_env:
        return app_env
    if _render_runtime_detected(env):
        return "render"
    return "development"


def _render_runtime_detected(env: Mapping[str, str]) -> bool:
    if any(_env_value(env, name) for name in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL")):
        return True
    return ".onrender.com" in _env_value(env, "PUBLIC_BASE_URL").lower()


def _first_env_value(env: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = _env_value(env, name)
        if value:
            return value
    return ""


def _normalize_payment_provider(provider: str) -> str:
    cleaned = str(provider or "").strip().lower()
    return PAYMENT_PROVIDER_ALIASES.get(cleaned, cleaned)


def _required_config_items(config: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in config:
        copied = {
            "key": str(item["key"]),
            "env": list(item["env"]),
        }
        allowed_values = item.get("allowedValues")
        if allowed_values:
            copied["allowedValues"] = list(allowed_values)
        items.append(copied)
    return items


def _missing_required_config(env: Mapping[str, str], required_config: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for item in required_config:
        names = tuple(str(name) for name in item.get("env", ()))
        if not names or _first_env_value(env, names):
            continue
        missing.append(str(item["key"]))
    return missing


def _clean_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise InvalidPaymentInput(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise InvalidPaymentInput(f"{name} must be non-empty")
    return cleaned


def _optional_clean_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _clean_text(value, name)


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPaymentInput(f"{name} must be an integer")
    if value <= 0:
        raise InvalidPaymentInput(f"{name} must be positive")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPaymentInput(f"{name} must be an integer")
    if value < 0:
        raise InvalidPaymentInput(f"{name} must be non-negative")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
