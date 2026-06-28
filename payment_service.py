from __future__ import annotations

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


class FakePaymentProviderForbidden(PaymentServiceError):
    code = "fake_payment_provider_forbidden"
    status_code = 403


class PaymentTransitionError(PaymentServiceError, ValueError):
    code = "payment_transition_error"


def init_payment_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def fake_payment_provider_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if _env_truthy(env.get("ENABLE_LOCAL_DEMO_BILLING"), default=True):
        return True
    if str(env.get("PAYMENT_PROVIDER") or "").strip().lower() == "fake":
        return True
    return _env_truthy(env.get("ALLOW_FAKE_PAYMENT_PROVIDER"), default=False)


def assess_payment_provider_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return an explicit payment-provider readiness report.

    The current runtime implements only the local fake payment provider. Real
    providers are recognized here so deployment checks can fail closed until a
    real SDK adapter and callback-signature verifier are implemented.
    """
    values = os.environ if env is None else env
    raw_provider = _env_value(values, "PAYMENT_PROVIDER")
    provider = _normalize_payment_provider(raw_provider or "fake")
    app_env = _env_value(values, "APP_ENV").lower()
    production_env = app_env in {"production", "prod"}
    local_demo_enabled = _env_truthy(values.get("ENABLE_LOCAL_DEMO_BILLING"), default=True)

    warnings: list[str] = []
    errors: list[str] = []
    missing_config: list[str] = []

    if provider == "fake":
        mode = "local_demo"
        required_config = [] if not production_env and local_demo_enabled else _required_config_items(
            PAYMENT_PROVIDER_SELECTION_CONFIG
        )
        if production_env:
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
        errors.append(f"{provider}_payment_adapter_not_implemented")
        errors.append("real_payment_callback_signature_verification_not_implemented")
        warnings.append("real_payment_credentials_do_not_make_current_adapter_production_ready")
    else:
        mode = "unknown"
        required_config = _required_config_items(PAYMENT_PROVIDER_SELECTION_CONFIG)
        errors.append("unsupported_payment_provider")
        if production_env or not local_demo_enabled:
            errors.append("real_payment_provider_required")

    return {
        "ready": not errors,
        "provider": provider,
        "mode": mode,
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
    return {
        "provider": provider,
        "provider_order_id": provider_order_id,
        "providerOrderId": provider_order_id,
        "payment_url": fake_payment_url(payload) if provider == "fake" else "",
        "paymentUrl": fake_payment_url(payload) if provider == "fake" else "",
    }


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
        normalized_status = status.strip().lower()
        if normalized_status in payment_rules.PAYMENT_STATUSES:
            return normalized_status

    normalized = event_type.strip().lower().replace("-", "_").replace(".", "_")
    aliases = {
        "pay_success": STATUS_PAID,
        "payment_success": STATUS_PAID,
        "paid": STATUS_PAID,
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
    cleaned = _clean_text(provider, "provider").lower()
    if cleaned != "fake":
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
