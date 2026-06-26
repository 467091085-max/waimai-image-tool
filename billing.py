from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "app.db"
DEFAULT_USER_ID = "default"

POINT_RATE = 10
QUALITY_POINTS = {
    "standard": 10,
    "premium": 20,
}
QUALITY_ALIASES = {
    "normal": "standard",
    "regular": "standard",
    "basic": "standard",
    "retouch": "premium",
    "refined": "premium",
    "pro": "premium",
}
CUSTOM_EDIT_POINTS = 15
WATERMARK_POINTS = 50
EXTRA_PLATFORM_POINTS = 100
FREE_SAMPLE_IMAGES = 6

RECHARGE_PACKAGES = {
    49: 500,
    99: 1040,
    299: 3190,
}
CUSTOM_RECHARGE_MIN_POINTS = 100
CUSTOM_RECHARGE_MIN_CASH = CUSTOM_RECHARGE_MIN_POINTS // POINT_RATE
PAYMENT_ORDER_STATUSES = ("pending", "paid", "expired", "refunded", "failed")
PAYMENT_PROVIDERS = ("mock", "wechat", "alipay")
MOCK_PAYMENT_PROVIDER = "mock"


class BillingError(Exception):
    code = "billing_error"
    status_code = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}


class InvalidBillingInput(BillingError):
    code = "invalid_billing_input"


class InvalidRechargePackage(BillingError):
    code = "invalid_recharge_package"


class InsufficientBalance(BillingError):
    code = "insufficient_balance"
    status_code = 402

    def __init__(self, required: int, available: int) -> None:
        super().__init__(
            "Insufficient points balance",
            required=required,
            available=available,
            shortage=max(required - available, 0),
        )


class OrderConflict(BillingError):
    code = "order_conflict"
    status_code = 409


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('credit', 'debit')),
    points INTEGER NOT NULL CHECK (points > 0),
    status TEXT NOT NULL CHECK (status IN ('succeeded')),
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('credit', 'debit')),
    points INTEGER NOT NULL CHECK (points > 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    event_type TEXT NOT NULL DEFAULT 'ledger_entry',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    source_order_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    points INTEGER NOT NULL CHECK (points > 0),
    failed_images INTEGER NOT NULL DEFAULT 0 CHECK (failed_images >= 0),
    status TEXT NOT NULL CHECK (status IN ('succeeded')),
    ledger_order_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (ledger_order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS billing_tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    order_id TEXT,
    job_id TEXT,
    status TEXT NOT NULL,
    image_count INTEGER NOT NULL DEFAULT 0 CHECK (image_count >= 0),
    failed_images INTEGER NOT NULL DEFAULT 0 CHECK (failed_images >= 0),
    refunded_points INTEGER NOT NULL DEFAULT 0 CHECK (refunded_points >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_refunds_user_created ON refunds(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_billing_tasks_user_updated ON billing_tasks(user_id, updated_at);

CREATE TABLE IF NOT EXISTS payment_orders (
    payment_order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'mock',
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'expired', 'refunded', 'failed')),
    cash_cents INTEGER NOT NULL CHECK (cash_cents > 0),
    points INTEGER NOT NULL CHECK (points > 0),
    billing_order_id TEXT UNIQUE,
    provider_trade_id TEXT,
    checkout_url TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    paid_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (billing_order_id) REFERENCES orders(order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_orders_user_created ON payment_orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_payment_orders_status ON payment_orders(status);

CREATE TABLE IF NOT EXISTS payment_webhook_events (
    event_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    payment_order_id TEXT,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    signature_valid INTEGER NOT NULL CHECK (signature_valid IN (0, 1)),
    processed INTEGER NOT NULL CHECK (processed IN (0, 1)),
    idempotent INTEGER NOT NULL CHECK (idempotent IN (0, 1)),
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (payment_order_id) REFERENCES payment_orders(payment_order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_webhook_events_order_created
    ON payment_webhook_events(payment_order_id, created_at);
"""


def resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    configured = os.environ.get("BILLING_DB_PATH") or os.environ.get("APP_DB_PATH")
    return Path(configured) if configured else DEFAULT_DB_PATH


@contextmanager
def open_db(db_path: str | os.PathLike[str] | None = None) -> Iterable[sqlite3.Connection]:
    path = resolve_db_path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | os.PathLike[str] | None = None) -> Path:
    path = resolve_db_path(db_path)
    with open_db(path) as conn:
        _ensure_schema(conn)
        conn.commit()
    return path


def get_account(
    user_id: str = DEFAULT_USER_ID,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    user_id = _clean_id(user_id, "user_id")
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_account(conn, user_id)
            account = _account_row(conn, user_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "userId": user_id,
        "balance": int(account["balance"]),
        "updatedAt": account["updated_at"],
    }


def credit_account(
    user_id: str,
    order_id: str,
    points: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
    description: str = "credit",
    metadata: dict[str, Any] | None = None,
    event_type: str = "manual_credit",
) -> dict[str, Any]:
    return _apply_entry(
        user_id=user_id,
        order_id=order_id,
        kind="credit",
        points=_positive_int(points, "points"),
        db_path=db_path,
        description=description,
        metadata=metadata,
        event_type=event_type,
    )


def debit_account(
    user_id: str,
    order_id: str,
    points: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
    description: str = "debit",
    metadata: dict[str, Any] | None = None,
    event_type: str = "manual_debit",
) -> dict[str, Any]:
    return _apply_entry(
        user_id=user_id,
        order_id=order_id,
        kind="debit",
        points=_positive_int(points, "points"),
        db_path=db_path,
        description=description,
        metadata=metadata,
        event_type=event_type,
    )


def credit_recharge(
    user_id: str,
    order_id: str,
    cash_amount: int | str | Decimal,
    *,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cash = _cash_to_int(cash_amount)
    points = points_for_recharge(cash)
    order_metadata = {
        "type": "recharge",
        "cash": cash,
        "package": cash in RECHARGE_PACKAGES,
        **(metadata or {}),
    }
    return credit_account(
        user_id,
        order_id,
        points,
        db_path=db_path,
        description=f"recharge:{cash}",
        metadata=order_metadata,
        event_type="recharge_credit",
    )


def credit_custom_recharge(
    user_id: str,
    order_id: str,
    points: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recharge_points = _positive_int(points, "points")
    if recharge_points < CUSTOM_RECHARGE_MIN_POINTS:
        raise InvalidRechargePackage(
            "Custom recharge points below minimum",
            points=recharge_points,
            customMinPoints=CUSTOM_RECHARGE_MIN_POINTS,
        )
    return credit_account(
        user_id,
        order_id,
        recharge_points,
        db_path=db_path,
        description="custom-recharge",
        metadata={
            "type": "recharge",
            "custom": True,
            "cash": round(recharge_points / POINT_RATE, 2),
            **(metadata or {}),
        },
        event_type="custom_recharge_credit",
    )


def debit_image_charge(
    user_id: str,
    order_id: str,
    *,
    image_count: int,
    quality: str | int = "standard",
    watermark: bool = False,
    platforms: list[str] | tuple[str, ...] | None = None,
    platform_count: int | None = None,
    free_sample_count: int = 0,
    custom_edit_count: int = 0,
    rework_count: int = 0,
    free_rework_quota: int = 0,
    fixed_fee_points: int = 0,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    breakdown = calculate_image_charge_breakdown(
        image_count=image_count,
        quality=quality,
        watermark=watermark,
        platforms=platforms,
        platform_count=platform_count,
        free_sample_count=free_sample_count,
        custom_edit_count=custom_edit_count,
        rework_count=rework_count,
        free_rework_quota=free_rework_quota,
        fixed_fee_points=fixed_fee_points,
    )
    points = int(breakdown["total"])
    ledger_event_type = (
        "generation_retry_charge"
        if int(breakdown.get("chargeableReworks", 0) or 0) > 0 or bool((metadata or {}).get("retry"))
        else "generation_output_charge"
    )
    if points == 0:
        return {
            "ok": True,
            "idempotent": False,
            "noCharge": True,
            "userId": _clean_id(user_id, "user_id"),
            "orderId": _clean_id(order_id, "order_id"),
            "direction": "none",
            "points": 0,
            "balance": get_account(user_id, db_path=db_path)["balance"],
            "balanceAfter": get_account(user_id, db_path=db_path)["balance"],
            "ledgerId": None,
            "eventType": ledger_event_type,
            "createdAt": _now(),
            "breakdown": breakdown,
        }
    return debit_account(
        user_id,
        order_id,
        points,
        db_path=db_path,
        description="image-generation-confirmed",
        metadata={
            "type": "generation_charge",
            "eventType": ledger_event_type,
            "imageCount": image_count,
            "quality": quality,
            "watermark": bool(watermark),
            "platforms": list(platforms) if platforms is not None else None,
            "platformCount": platform_count,
            "freeSampleCount": free_sample_count,
            "customEditCount": custom_edit_count,
            "reworkCount": rework_count,
            "freeReworkQuota": free_rework_quota,
            "fixedFeePoints": fixed_fee_points,
            "breakdown": breakdown,
            **(metadata or {}),
        },
        event_type=ledger_event_type,
    )


def points_for_recharge(cash_amount: int | str | Decimal) -> int:
    cash = _cash_to_int(cash_amount)
    if cash in RECHARGE_PACKAGES:
        return RECHARGE_PACKAGES[cash]
    points = cash * POINT_RATE
    if points >= CUSTOM_RECHARGE_MIN_POINTS:
        return points
    raise InvalidRechargePackage(
        "Unsupported recharge amount",
        cash=cash,
        supported=list(RECHARGE_PACKAGES),
        customMinCash=CUSTOM_RECHARGE_MIN_CASH,
        customMinPoints=CUSTOM_RECHARGE_MIN_POINTS,
    )


def calculate_image_charge(
    image_count: int,
    quality: str | int = "standard",
    *,
    watermark: bool = False,
    platforms: list[str] | tuple[str, ...] | None = None,
    platform_count: int | None = None,
    free_sample_count: int = 0,
    custom_edit_count: int = 0,
    rework_count: int = 0,
    free_rework_quota: int = 0,
    fixed_fee_points: int = 0,
) -> int:
    return int(
        calculate_image_charge_breakdown(
            image_count=image_count,
            quality=quality,
            watermark=watermark,
            platforms=platforms,
            platform_count=platform_count,
            free_sample_count=free_sample_count,
            custom_edit_count=custom_edit_count,
            rework_count=rework_count,
            free_rework_quota=free_rework_quota,
            fixed_fee_points=fixed_fee_points,
        )["total"]
    )


def calculate_image_charge_breakdown(
    image_count: int,
    quality: str | int = "standard",
    *,
    watermark: bool = False,
    platforms: list[str] | tuple[str, ...] | None = None,
    platform_count: int | None = None,
    free_sample_count: int = 0,
    custom_edit_count: int = 0,
    rework_count: int = 0,
    free_rework_quota: int = 0,
    fixed_fee_points: int = 0,
) -> dict[str, Any]:
    images = _non_negative_int(image_count, "image_count")
    per_image = quality_point_value(quality)
    free_samples = min(images, _non_negative_int(free_sample_count, "free_sample_count"))
    chargeable_images = max(images - free_samples, 0)
    edits = _non_negative_int(custom_edit_count, "custom_edit_count")
    reworks = _non_negative_int(rework_count, "rework_count")
    free_reworks = min(reworks, _non_negative_int(free_rework_quota, "free_rework_quota"))
    chargeable_reworks = max(reworks - free_reworks, 0)
    total_platforms = _platform_count(platforms, platform_count)
    platform_extra_count = max(total_platforms - 1, 0)
    image_points = chargeable_images * per_image
    custom_edit_points = edits * CUSTOM_EDIT_POINTS
    rework_points = chargeable_reworks * per_image
    watermark_points = WATERMARK_POINTS if watermark else 0
    platform_points = platform_extra_count * EXTRA_PLATFORM_POINTS
    fixed_points = _non_negative_int(fixed_fee_points, "fixed_fee_points")
    total = image_points + custom_edit_points + rework_points + watermark_points + platform_points + fixed_points
    return {
        "quality": str(quality or "standard"),
        "perImagePoints": per_image,
        "imageCount": images,
        "freeSampleCount": free_samples,
        "chargeableImages": chargeable_images,
        "imagePoints": image_points,
        "customEditCount": edits,
        "customEditUnitPoints": CUSTOM_EDIT_POINTS,
        "customEditPoints": custom_edit_points,
        "reworkCount": reworks,
        "freeReworkQuota": free_reworks,
        "chargeableReworks": chargeable_reworks,
        "reworkPoints": rework_points,
        "watermark": bool(watermark),
        "watermarkPoints": watermark_points,
        "platformCount": total_platforms,
        "extraPlatformCount": platform_extra_count,
        "extraPlatformPoints": platform_points,
        "fixedFeePoints": fixed_points,
        "total": total,
    }


def quality_point_value(quality: str | int) -> int:
    if isinstance(quality, int) and not isinstance(quality, bool):
        return _positive_int(quality, "quality")
    key = str(quality or "standard").strip().lower()
    key = QUALITY_ALIASES.get(key, key)
    if key not in QUALITY_POINTS:
        raise InvalidBillingInput("Unknown quality option", quality=quality, supported=list(QUALITY_POINTS))
    return QUALITY_POINTS[key]


def recharge_packages_payload() -> list[dict[str, Any]]:
    names = {
        49: "Starter 49",
        99: "Store 99",
        299: "Team 299",
    }
    return [
        {
            "name": names[cash],
            "cash": cash,
            "basePoints": cash * POINT_RATE,
            "points": points,
            "bonus": max(points - (cash * POINT_RATE), 0),
        }
        for cash, points in sorted(RECHARGE_PACKAGES.items())
    ]


def pricing_payload() -> dict[str, Any]:
    return {
        "rate": f"1 yuan = {POINT_RATE} points",
        "qualityPoints": dict(QUALITY_POINTS),
        "standardImagePoints": QUALITY_POINTS["standard"],
        "premiumImagePoints": QUALITY_POINTS["premium"],
        "customEditPoints": CUSTOM_EDIT_POINTS,
        "watermarkPoints": WATERMARK_POINTS,
        "extraPlatformPoints": EXTRA_PLATFORM_POINTS,
        "firstPlatformPoints": 0,
        "freeSampleImages": FREE_SAMPLE_IMAGES,
        "customRechargeMinPoints": CUSTOM_RECHARGE_MIN_POINTS,
        "customRechargeMinCash": CUSTOM_RECHARGE_MIN_CASH,
    }


def account_payload(
    user_id: str = DEFAULT_USER_ID,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    account = get_account(user_id=user_id, db_path=db_path)
    return {
        **account,
        "rate": f"1 yuan = {POINT_RATE} points",
        "packages": recharge_packages_payload(),
        "customRecharge": {
            "minPoints": CUSTOM_RECHARGE_MIN_POINTS,
            "minCash": CUSTOM_RECHARGE_MIN_CASH,
            "rate": POINT_RATE,
        },
        "pricing": pricing_payload(),
    }


def create_payment_order(
    user_id: str,
    payment_order_id: str | None = None,
    *,
    cash_amount: int | str | Decimal | None = None,
    points: int | None = None,
    provider: str = MOCK_PAYMENT_PROVIDER,
    expires_in_seconds: int = 30 * 60,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_user_id = _clean_id(user_id, "user_id")
    clean_provider = _payment_provider(provider)
    clean_order_id = _clean_id(payment_order_id or f"pay_{uuid.uuid4().hex[:20]}", "payment_order_id")
    recharge_points, cash_cents, package_cash = _payment_recharge_amounts(
        cash_amount=cash_amount,
        points=points,
    )
    provider_configured = payment_provider_configured(clean_provider)
    now = _now()
    expires_at = _now_plus_seconds(_non_negative_int(expires_in_seconds, "expires_in_seconds"))
    meta = {
        "type": "payment_order",
        "recharge": True,
        "package": package_cash in RECHARGE_PACKAGES if package_cash is not None else False,
        "custom": package_cash is None or package_cash not in RECHARGE_PACKAGES,
        "providerConfigured": provider_configured,
        **(metadata or {}),
    }
    checkout_url = _payment_checkout_url(clean_provider, clean_order_id, configured=provider_configured)

    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_account(conn, clean_user_id)
            existing = _payment_order_row(conn, clean_order_id)
            if existing:
                if (
                    existing["user_id"] != clean_user_id
                    or existing["provider"] != clean_provider
                    or int(existing["cash_cents"]) != cash_cents
                    or int(existing["points"]) != recharge_points
                ):
                    raise OrderConflict(
                        "Payment order id already belongs to a different payment operation",
                        paymentOrderId=clean_order_id,
                        existingUserId=existing["user_id"],
                        existingProvider=existing["provider"],
                        existingCashCents=int(existing["cash_cents"]),
                        existingPoints=int(existing["points"]),
                    )
                conn.commit()
                return {
                    "ok": True,
                    "idempotent": True,
                    "paymentOrder": _payment_order_payload(existing),
                    "readiness": payment_readiness_payload(),
                }

            conn.execute(
                """
                INSERT INTO payment_orders (
                    payment_order_id,
                    user_id,
                    provider,
                    status,
                    cash_cents,
                    points,
                    checkout_url,
                    expires_at,
                    created_at,
                    updated_at,
                    metadata
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_order_id,
                    clean_user_id,
                    clean_provider,
                    cash_cents,
                    recharge_points,
                    checkout_url,
                    expires_at,
                    now,
                    now,
                    _json(meta),
                ),
            )
            row = _payment_order_row(conn, clean_order_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "ok": True,
        "idempotent": False,
        "paymentOrder": _payment_order_payload(row),
        "readiness": payment_readiness_payload(),
    }


def create_payment_order_api_payload(
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    db_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        result = create_payment_order(
            str(payload.get("userId") or payload.get("user_id") or user_id),
            str(payload.get("paymentOrderId") or payload.get("payment_order_id") or "") or None,
            cash_amount=payload.get("cash", payload.get("cashAmount", payload.get("amount"))),
            points=payload.get("points"),
            provider=str(payload.get("provider") or MOCK_PAYMENT_PROVIDER),
            db_path=db_path,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
        return result, 200
    except BillingError as exc:
        return billing_error_response(exc)


def process_payment_webhook_event(
    *,
    event_id: str,
    payment_order_id: str,
    status: str,
    provider: str = MOCK_PAYMENT_PROVIDER,
    signature_valid: bool = True,
    provider_trade_id: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_event_id = _clean_id(event_id, "event_id")
    clean_order_id = _clean_id(payment_order_id, "payment_order_id")
    clean_provider = _payment_provider(provider)
    clean_status = _payment_status(status)
    trade_id = str(provider_trade_id or "").strip() or None
    now = _now()
    meta = dict(metadata or {})

    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_event = _payment_webhook_event_row(conn, clean_event_id)
            if existing_event:
                order = _payment_order_row(conn, clean_order_id)
                conn.commit()
                return {
                    "ok": bool(existing_event["processed"]),
                    "idempotent": True,
                    "event": _payment_webhook_event_payload(existing_event),
                    "paymentOrder": _payment_order_payload(order) if order else None,
                }

            order = _payment_order_row(conn, clean_order_id)
            if order is None:
                event = _insert_payment_webhook_event(
                    conn=conn,
                    event_id=clean_event_id,
                    provider=clean_provider,
                    payment_order_id=None,
                    event_type="payment_order_missing",
                    status=clean_status,
                    signature_valid=bool(signature_valid),
                    processed=False,
                    idempotent=False,
                    message="Payment order not found",
                    metadata=meta,
                    created_at=now,
                )
                conn.commit()
                return {
                    "ok": False,
                    "code": "payment_order_not_found",
                    "error": "Payment order not found",
                    "idempotent": False,
                    "event": _payment_webhook_event_payload(event),
                }

            if order["provider"] != clean_provider:
                event = _insert_payment_webhook_event(
                    conn=conn,
                    event_id=clean_event_id,
                    provider=clean_provider,
                    payment_order_id=clean_order_id,
                    event_type="payment_provider_mismatch",
                    status=clean_status,
                    signature_valid=bool(signature_valid),
                    processed=False,
                    idempotent=False,
                    message="Webhook provider does not match payment order provider",
                    metadata=meta,
                    created_at=now,
                )
                conn.commit()
                return {
                    "ok": False,
                    "code": "payment_provider_mismatch",
                    "error": "Webhook provider does not match payment order provider",
                    "idempotent": False,
                    "event": _payment_webhook_event_payload(event),
                    "paymentOrder": _payment_order_payload(order),
                }

            if not signature_valid:
                event = _insert_payment_webhook_event(
                    conn=conn,
                    event_id=clean_event_id,
                    provider=clean_provider,
                    payment_order_id=clean_order_id,
                    event_type="payment_signature_invalid",
                    status=clean_status,
                    signature_valid=False,
                    processed=False,
                    idempotent=False,
                    message="Webhook signature verification failed",
                    metadata=meta,
                    created_at=now,
                )
                conn.commit()
                return {
                    "ok": False,
                    "code": "payment_signature_invalid",
                    "error": "Webhook signature verification failed",
                    "idempotent": False,
                    "event": _payment_webhook_event_payload(event),
                    "paymentOrder": _payment_order_payload(order),
                }

            if clean_status == "paid":
                result = _mark_payment_order_paid(
                    conn=conn,
                    order=order,
                    event_id=clean_event_id,
                    provider_trade_id=trade_id,
                    now=now,
                    metadata=meta,
                )
            else:
                result = _mark_payment_order_terminal(
                    conn=conn,
                    order=order,
                    status=clean_status,
                    provider_trade_id=trade_id,
                    now=now,
                    metadata=meta,
                )

            event = _insert_payment_webhook_event(
                conn=conn,
                event_id=clean_event_id,
                provider=clean_provider,
                payment_order_id=clean_order_id,
                event_type=result["eventType"],
                status=clean_status,
                signature_valid=True,
                processed=bool(result["processed"]),
                idempotent=bool(result["idempotent"]),
                message=str(result.get("message") or ""),
                metadata={**meta, "transaction": result.get("transaction")},
                created_at=now,
            )
            updated_order = _payment_order_row(conn, clean_order_id)
            account = _account_row(conn, order["user_id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "ok": bool(result["processed"]),
        "idempotent": bool(result["idempotent"]),
        "event": _payment_webhook_event_payload(event),
        "paymentOrder": _payment_order_payload(updated_order),
        "transaction": result.get("transaction"),
        "account": {
            "userId": account["user_id"],
            "balance": int(account["balance"]),
            "updatedAt": account["updated_at"],
        },
    }


def payment_provider_configured(provider: str) -> bool:
    clean_provider = _payment_provider(provider)
    if clean_provider == MOCK_PAYMENT_PROVIDER:
        return True
    if clean_provider == "wechat":
        return all(
            os.environ.get(name)
            for name in (
                "WECHAT_PAY_MCH_ID",
                "WECHAT_PAY_CERT_SERIAL_NO",
                "WECHAT_PAY_PRIVATE_KEY",
                "WECHAT_PAY_API_V3_KEY",
            )
        )
    if clean_provider == "alipay":
        return all(
            os.environ.get(name)
            for name in (
                "ALIPAY_APP_ID",
                "ALIPAY_PRIVATE_KEY",
                "ALIPAY_PUBLIC_KEY",
            )
        )
    return False


def payment_readiness_payload() -> dict[str, Any]:
    configured = {provider: payment_provider_configured(provider) for provider in PAYMENT_PROVIDERS}
    real_configured = any(configured[provider] for provider in ("wechat", "alipay"))
    return {
        "mode": "mock" if not real_configured else "configured",
        "realPaymentConfigured": real_configured,
        "mockProviderEnabled": configured[MOCK_PAYMENT_PROVIDER],
        "supportedProviders": list(PAYMENT_PROVIDERS),
        "providerConfigured": configured,
        "statuses": list(PAYMENT_ORDER_STATUSES),
        "webhookSignature": "mock-stub",
        "message": (
            "Real WeChat/Alipay credentials are configured."
            if real_configured
            else "Real WeChat/Alipay credentials are not configured; mock payment orders and stub webhooks are available."
        ),
    }


def billing_error_response(exc: BillingError) -> tuple[dict[str, Any], int]:
    return {"ok": False, **exc.to_dict()}, exc.status_code


def recharge_api_payload(
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    db_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        actual_user_id = str(payload.get("userId") or payload.get("user_id") or user_id)
        order_id = str(payload.get("orderId") or payload.get("order_id") or "")
        if payload.get("points") is not None:
            result = credit_custom_recharge(
                actual_user_id,
                order_id,
                payload.get("points"),
                db_path=db_path,
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )
        else:
            cash = payload.get("cash", payload.get("cashAmount", payload.get("amount")))
            result = credit_recharge(actual_user_id, order_id, cash, db_path=db_path)
        return {"ok": True, "transaction": result, "account": account_payload(actual_user_id, db_path)}, 200
    except BillingError as exc:
        return billing_error_response(exc)


def debit_api_payload(
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    db_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        actual_user_id = str(payload.get("userId") or payload.get("user_id") or user_id)
        order_id = str(payload.get("orderId") or payload.get("order_id") or "")
        if "points" in payload:
            points = _positive_int(payload["points"], "points")
        else:
            points = calculate_image_charge(
                image_count=payload.get("imageCount", payload.get("images", 0)),
                quality=payload.get("quality", "standard"),
                watermark=bool(payload.get("watermark", False)),
                platforms=payload.get("platforms"),
                platform_count=payload.get("platformCount"),
                free_sample_count=payload.get("freeSampleCount", 0),
                custom_edit_count=payload.get("customEditCount", 0),
                rework_count=payload.get("reworkCount", 0),
                free_rework_quota=payload.get("freeReworkQuota", 0),
                fixed_fee_points=payload.get("fixedFeePoints", 0),
            )
        if points == 0:
            result = {
                "ok": True,
                "idempotent": False,
                "noCharge": True,
                "userId": actual_user_id,
                "orderId": order_id,
                "direction": "none",
                "points": 0,
                "balance": get_account(actual_user_id, db_path=db_path)["balance"],
                "balanceAfter": get_account(actual_user_id, db_path=db_path)["balance"],
                "ledgerId": None,
                "createdAt": _now(),
            }
        else:
            result = debit_account(actual_user_id, order_id, points, db_path=db_path)
        return {"ok": True, "transaction": result, "account": account_payload(actual_user_id, db_path)}, 200
    except BillingError as exc:
        return billing_error_response(exc)


def confirm_generation_charge(
    user_id: str,
    order_id: str,
    *,
    image_count: int,
    quality: str | int = "standard",
    watermark: bool = False,
    platforms: list[str] | tuple[str, ...] | None = None,
    platform_count: int | None = None,
    free_sample_count: int = 0,
    custom_edit_count: int = 0,
    rework_count: int = 0,
    free_rework_quota: int = 0,
    fixed_fee_points: int = 0,
    job_id: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = debit_image_charge(
        user_id,
        order_id,
        image_count=image_count,
        quality=quality,
        watermark=watermark,
        platforms=platforms,
        platform_count=platform_count,
        free_sample_count=free_sample_count,
        custom_edit_count=custom_edit_count,
        rework_count=rework_count,
        free_rework_quota=free_rework_quota,
        fixed_fee_points=fixed_fee_points,
        db_path=db_path,
        metadata={"jobId": job_id, **(metadata or {})},
    )
    record_generation_task(
        task_id=job_id or order_id,
        user_id=user_id,
        order_id=order_id,
        job_id=job_id,
        status="confirmed",
        image_count=image_count,
        db_path=db_path,
        metadata={"charge": result, **(metadata or {})},
    )
    return result


def record_generation_task(
    *,
    task_id: str,
    user_id: str,
    status: str,
    order_id: str | None = None,
    job_id: str | None = None,
    image_count: int = 0,
    failed_images: int = 0,
    refunded_points: int = 0,
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_task_id = _clean_id(task_id, "task_id")
    clean_user_id = _clean_id(user_id, "user_id")
    clean_status = str(status or "").strip() or "unknown"
    clean_order_id = str(order_id or "").strip() or None
    clean_job_id = str(job_id or "").strip() or None
    images = _non_negative_int(image_count, "image_count")
    failed = _non_negative_int(failed_images, "failed_images")
    refunded = _non_negative_int(refunded_points, "refunded_points")
    now = _now()
    meta_json = _json(metadata or {})
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_account(conn, clean_user_id)
            existing = conn.execute(
                "SELECT created_at FROM billing_tasks WHERE task_id = ?",
                (clean_task_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO billing_tasks (
                    task_id,
                    user_id,
                    order_id,
                    job_id,
                    status,
                    image_count,
                    failed_images,
                    refunded_points,
                    created_at,
                    updated_at,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_task_id,
                    clean_user_id,
                    clean_order_id,
                    clean_job_id,
                    clean_status,
                    images,
                    failed,
                    refunded,
                    created_at,
                    now,
                    meta_json,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "ok": True,
        "taskId": clean_task_id,
        "userId": clean_user_id,
        "orderId": clean_order_id,
        "jobId": clean_job_id,
        "status": clean_status,
        "imageCount": images,
        "failedImages": failed,
        "refundedPoints": refunded,
        "createdAt": created_at,
        "updatedAt": now,
        "metadata": metadata or {},
    }


def record_generation_failure(
    user_id: str,
    source_order_id: str,
    *,
    failed_images: int,
    quality: str | int = "standard",
    refund_id: str | None = None,
    refund_points: int | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    reason: str = "generation failed",
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_user_id = _clean_id(user_id, "user_id")
    clean_source_order_id = _clean_id(source_order_id, "source_order_id")
    failed = _positive_int(failed_images, "failed_images")
    points = (
        _positive_int(refund_points, "refund_points")
        if refund_points is not None
        else calculate_image_charge(image_count=failed, quality=quality)
    )
    clean_refund_id = _clean_id(refund_id or f"refund:{clean_source_order_id}:{failed}", "refund_id")
    ledger_order_id = f"refund:{clean_refund_id}"
    meta = {
        "type": "refund",
        "eventType": "generation_failure_refund",
        "failure": True,
        "sourceOrderId": clean_source_order_id,
        "failedImages": failed,
        "quality": quality,
        **(metadata or {}),
    }
    transaction = credit_account(
        clean_user_id,
        ledger_order_id,
        points,
        db_path=db_path,
        description="generation-failure-refund",
        metadata=meta,
        event_type="generation_failure_refund",
    )
    now = transaction["createdAt"]
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_account(conn, clean_user_id)
            existing = conn.execute(
                "SELECT * FROM refunds WHERE refund_id = ?",
                (clean_refund_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO refunds (
                        refund_id,
                        source_order_id,
                        user_id,
                        points,
                        failed_images,
                        status,
                        ledger_order_id,
                        reason,
                        created_at,
                        metadata
                    ) VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, ?)
                    """,
                    (
                        clean_refund_id,
                        clean_source_order_id,
                        clean_user_id,
                        points,
                        failed,
                        ledger_order_id,
                        str(reason or ""),
                        now,
                        _json(meta),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    task = record_generation_task(
        task_id=task_id or job_id or clean_source_order_id,
        user_id=clean_user_id,
        order_id=clean_source_order_id,
        job_id=job_id,
        status="failed_refunded",
        failed_images=failed,
        refunded_points=points,
        db_path=db_path,
        metadata={"refundId": clean_refund_id, "reason": reason, **(metadata or {})},
    )
    return {
        "ok": True,
        "refund": {
            "refundId": clean_refund_id,
            "sourceOrderId": clean_source_order_id,
            "ledgerOrderId": ledger_order_id,
            "userId": clean_user_id,
            "points": points,
            "failedImages": failed,
            "status": "succeeded",
            "reason": str(reason or ""),
            "createdAt": now,
            "metadata": meta,
        },
        "transaction": transaction,
        "task": task,
    }


def admin_billing_payload(
    db_path: str | os.PathLike[str] | None = None,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    try:
        return _admin_billing_payload(db_path=db_path, limit=limit)
    except BillingError as exc:
        return {"ok": False, **exc.to_dict()}
    except Exception as exc:
        return {
            "ok": False,
            "error": "Billing admin payload unavailable",
            "code": "billing_admin_unavailable",
            "details": {"type": type(exc).__name__, "message": str(exc)},
            "summary": _empty_admin_summary(),
            "accounts": [],
            "orders": [],
            "ledger": [],
            "refunds": [],
            "tasks": [],
            "paymentOrders": [],
            "paymentWebhooks": [],
            "paymentReadiness": payment_readiness_payload(),
        }


def _admin_billing_payload(
    db_path: str | os.PathLike[str] | None = None,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    capped_limit = max(1, min(200, int(limit)))
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        accounts = [
            _account_payload(row)
            for row in conn.execute(
                """
                SELECT user_id, balance, created_at, updated_at
                FROM accounts
                ORDER BY updated_at DESC, user_id ASC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        orders = [
            _order_payload(row)
            for row in conn.execute(
                """
                SELECT *
                FROM orders
                ORDER BY created_at DESC, order_id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        ledger = [
            _ledger_payload(row)
            for row in conn.execute(
                """
                SELECT *
                FROM ledger
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        refunds = [
            _refund_payload(row)
            for row in conn.execute(
                """
                SELECT *
                FROM refunds
                ORDER BY created_at DESC, refund_id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        tasks = [
            _billing_task_payload(row)
            for row in conn.execute(
                """
                SELECT *
                FROM billing_tasks
                ORDER BY updated_at DESC, task_id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        payment_orders = [
            _payment_order_payload(row)
            for row in conn.execute(
                """
                SELECT *
                FROM payment_orders
                ORDER BY updated_at DESC, created_at DESC, payment_order_id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        payment_webhooks = [
            _payment_webhook_event_payload(row)
            for row in conn.execute(
                """
                SELECT *
                FROM payment_webhook_events
                ORDER BY created_at DESC, event_id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        ]
        summary = _billing_summary(conn)
    return {
        "ok": True,
        "summary": summary,
        "pricing": pricing_payload(),
        "packages": recharge_packages_payload(),
        "paymentReadiness": payment_readiness_payload(),
        "accounts": accounts,
        "orders": orders,
        "ledger": ledger,
        "refunds": refunds,
        "tasks": tasks,
        "paymentOrders": payment_orders,
        "paymentWebhooks": payment_webhooks,
    }


def _billing_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    account_row = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(balance), 0) AS balance FROM accounts"
    ).fetchone()
    order_count = int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
    ledger_count = int(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
    credit_points = int(conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM ledger WHERE direction = 'credit'"
    ).fetchone()[0])
    debit_points = int(conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM ledger WHERE direction = 'debit'"
    ).fetchone()[0])
    recharge_points = int(conn.execute(
        """
        SELECT COALESCE(SUM(points), 0)
        FROM ledger
        WHERE direction = 'credit'
          AND (description LIKE 'recharge:%' OR description = 'custom-recharge')
        """
    ).fetchone()[0])
    refund_row = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(points), 0) AS points, COALESCE(SUM(failed_images), 0) AS failed_images FROM refunds"
    ).fetchone()
    task_counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM billing_tasks GROUP BY status"
        ).fetchall()
    }
    payment_counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM payment_orders GROUP BY status"
        ).fetchall()
    }
    webhook_count = int(conn.execute("SELECT COUNT(*) FROM payment_webhook_events").fetchone()[0])
    return {
        "accountCount": int(account_row["count"]),
        "totalBalance": int(account_row["balance"]),
        "orderCount": order_count,
        "ledgerCount": ledger_count,
        "creditPoints": credit_points,
        "debitPoints": debit_points,
        "rechargePoints": recharge_points,
        "spentPoints": debit_points,
        "refundCount": int(refund_row["count"]),
        "refundPoints": int(refund_row["points"]),
        "failedImagesRefunded": int(refund_row["failed_images"]),
        "taskCount": sum(task_counts.values()),
        "taskStatusCounts": task_counts,
        "paymentOrderCount": sum(payment_counts.values()),
        "paymentStatusCounts": payment_counts,
        "pendingPaymentOrders": payment_counts.get("pending", 0),
        "paidPaymentOrders": payment_counts.get("paid", 0),
        "expiredPaymentOrders": payment_counts.get("expired", 0),
        "refundedPaymentOrders": payment_counts.get("refunded", 0),
        "failedPaymentOrders": payment_counts.get("failed", 0),
        "paymentWebhookEventCount": webhook_count,
    }


def _empty_admin_summary() -> dict[str, Any]:
    return {
        "accountCount": 0,
        "totalBalance": 0,
        "orderCount": 0,
        "ledgerCount": 0,
        "creditPoints": 0,
        "debitPoints": 0,
        "rechargePoints": 0,
        "spentPoints": 0,
        "refundCount": 0,
        "refundPoints": 0,
        "failedImagesRefunded": 0,
        "taskCount": 0,
        "taskStatusCounts": {},
        "paymentOrderCount": 0,
        "paymentStatusCounts": {},
        "pendingPaymentOrders": 0,
        "paidPaymentOrders": 0,
        "expiredPaymentOrders": 0,
        "refundedPaymentOrders": 0,
        "failedPaymentOrders": 0,
        "paymentWebhookEventCount": 0,
    }


def _account_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "userId": row["user_id"],
        "balance": int(row["balance"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _order_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "orderId": row["order_id"],
        "userId": row["user_id"],
        "kind": row["kind"],
        "points": int(row["points"]),
        "status": row["status"],
        "createdAt": row["created_at"],
        "metadata": _json_loads(row["metadata"], {}),
    }


def _ledger_payload(row: sqlite3.Row) -> dict[str, Any]:
    points = int(row["points"])
    direction = row["direction"]
    return {
        "id": int(row["id"]),
        "orderId": row["order_id"],
        "userId": row["user_id"],
        "direction": direction,
        "points": points,
        "signedPoints": points if direction == "credit" else -points,
        "balanceAfter": int(row["balance_after"]),
        "eventType": row["event_type"],
        "description": row["description"],
        "createdAt": row["created_at"],
        "metadata": _json_loads(row["metadata"], {}),
    }


def _refund_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "refundId": row["refund_id"],
        "sourceOrderId": row["source_order_id"],
        "ledgerOrderId": row["ledger_order_id"],
        "userId": row["user_id"],
        "points": int(row["points"]),
        "failedImages": int(row["failed_images"]),
        "status": row["status"],
        "reason": row["reason"],
        "createdAt": row["created_at"],
        "metadata": _json_loads(row["metadata"], {}),
    }


def _billing_task_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "taskId": row["task_id"],
        "userId": row["user_id"],
        "orderId": row["order_id"],
        "jobId": row["job_id"],
        "status": row["status"],
        "imageCount": int(row["image_count"]),
        "failedImages": int(row["failed_images"]),
        "refundedPoints": int(row["refunded_points"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "metadata": _json_loads(row["metadata"], {}),
    }


def _payment_recharge_amounts(
    *,
    cash_amount: int | str | Decimal | None,
    points: int | None,
) -> tuple[int, int, int | None]:
    if points is not None:
        recharge_points = _positive_int(points, "points")
        if recharge_points < CUSTOM_RECHARGE_MIN_POINTS:
            raise InvalidRechargePackage(
                "Custom recharge points below minimum",
                points=recharge_points,
                customMinPoints=CUSTOM_RECHARGE_MIN_POINTS,
            )
        cash_cents = (recharge_points * 100) // POINT_RATE
        if cash_amount is not None:
            cash = _cash_to_int(cash_amount)
            if cash * POINT_RATE != recharge_points:
                raise InvalidRechargePackage(
                    "Recharge cash amount does not match custom points",
                    cash=cash,
                    points=recharge_points,
                )
            return recharge_points, cash * 100, cash
        return recharge_points, cash_cents, None

    cash = _cash_to_int(cash_amount)
    return points_for_recharge(cash), cash * 100, cash


def _payment_provider(provider: str) -> str:
    clean = str(provider or MOCK_PAYMENT_PROVIDER).strip().lower()
    if clean not in PAYMENT_PROVIDERS:
        raise InvalidBillingInput("Unsupported payment provider", provider=provider, supported=list(PAYMENT_PROVIDERS))
    return clean


def _payment_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean not in PAYMENT_ORDER_STATUSES:
        raise InvalidBillingInput("Unsupported payment order status", status=status, supported=list(PAYMENT_ORDER_STATUSES))
    return clean


def _payment_checkout_url(provider: str, payment_order_id: str, *, configured: bool) -> str:
    if provider == MOCK_PAYMENT_PROVIDER:
        return f"mock://payment/{payment_order_id}"
    if configured:
        return f"{provider}://payment/{payment_order_id}"
    return f"{provider}://not-configured/{payment_order_id}"


def _payment_order_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    cash_cents = int(row["cash_cents"])
    metadata = _json_loads(row["metadata"], {})
    return {
        "paymentOrderId": row["payment_order_id"],
        "userId": row["user_id"],
        "provider": row["provider"],
        "providerConfigured": payment_provider_configured(row["provider"]),
        "status": row["status"],
        "cashCents": cash_cents,
        "cashAmount": round(cash_cents / 100, 2),
        "cash": cash_cents // 100 if cash_cents % 100 == 0 else round(cash_cents / 100, 2),
        "points": int(row["points"]),
        "billingOrderId": row["billing_order_id"],
        "providerTradeId": row["provider_trade_id"],
        "checkoutUrl": row["checkout_url"],
        "expiresAt": row["expires_at"],
        "paidAt": row["paid_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "metadata": metadata,
    }


def _payment_webhook_event_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "eventId": row["event_id"],
        "provider": row["provider"],
        "paymentOrderId": row["payment_order_id"],
        "eventType": row["event_type"],
        "status": row["status"],
        "signatureValid": bool(row["signature_valid"]),
        "processed": bool(row["processed"]),
        "idempotent": bool(row["idempotent"]),
        "message": row["message"],
        "createdAt": row["created_at"],
        "metadata": _json_loads(row["metadata"], {}),
    }


def _mark_payment_order_paid(
    *,
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    event_id: str,
    provider_trade_id: str | None,
    now: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    current_status = str(order["status"])
    if current_status == "paid":
        return {
            "processed": True,
            "idempotent": True,
            "eventType": "payment_paid_duplicate",
            "message": "Payment order already paid",
            "transaction": None,
        }
    if current_status != "pending":
        return {
            "processed": False,
            "idempotent": False,
            "eventType": "payment_paid_invalid_transition",
            "message": f"Cannot mark {current_status} payment order as paid",
            "transaction": None,
        }

    payment_order_id = order["payment_order_id"]
    billing_order_id = order["billing_order_id"] or f"payment:{payment_order_id}"
    order_metadata = _json_loads(order["metadata"], {})
    ledger_meta = {
        **order_metadata,
        **metadata,
        "type": "recharge",
        "eventType": "payment_recharge_credit",
        "paymentOrderId": payment_order_id,
        "provider": order["provider"],
        "providerTradeId": provider_trade_id,
        "webhookEventId": event_id,
        "cashCents": int(order["cash_cents"]),
    }
    transaction = _apply_entry_in_conn(
        conn=conn,
        user_id=order["user_id"],
        order_id=billing_order_id,
        kind="credit",
        points=int(order["points"]),
        description="payment-recharge-paid",
        metadata=ledger_meta,
        event_type="payment_recharge_credit",
    )
    updated_meta = {
        **order_metadata,
        **metadata,
        "paidByWebhookEventId": event_id,
        "providerTradeId": provider_trade_id,
    }
    conn.execute(
        """
        UPDATE payment_orders
        SET status = 'paid',
            billing_order_id = ?,
            provider_trade_id = COALESCE(?, provider_trade_id),
            paid_at = ?,
            updated_at = ?,
            metadata = ?
        WHERE payment_order_id = ?
        """,
        (billing_order_id, provider_trade_id, now, now, _json(updated_meta), payment_order_id),
    )
    return {
        "processed": True,
        "idempotent": bool(transaction["idempotent"]),
        "eventType": "payment_paid",
        "message": "Payment order settled and points credited",
        "transaction": transaction,
    }


def _mark_payment_order_terminal(
    *,
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    status: str,
    provider_trade_id: str | None,
    now: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if status == "pending":
        return {
            "processed": False,
            "idempotent": False,
            "eventType": "payment_pending_ignored",
            "message": "Pending webhook does not settle a payment order",
            "transaction": None,
        }

    current_status = str(order["status"])
    if current_status == status:
        return {
            "processed": True,
            "idempotent": True,
            "eventType": f"payment_{status}_duplicate",
            "message": f"Payment order already {status}",
            "transaction": None,
        }
    if current_status == "paid" and status in {"failed", "expired"}:
        return {
            "processed": False,
            "idempotent": False,
            "eventType": f"payment_{status}_invalid_transition",
            "message": f"Cannot mark paid payment order as {status}",
            "transaction": None,
        }
    if current_status in {"failed", "expired", "refunded"} and current_status != status:
        return {
            "processed": False,
            "idempotent": False,
            "eventType": f"payment_{status}_invalid_transition",
            "message": f"Cannot mark {current_status} payment order as {status}",
            "transaction": None,
        }

    order_metadata = _json_loads(order["metadata"], {})
    updated_meta = {
        **order_metadata,
        **metadata,
        "terminalWebhookStatus": status,
        "providerTradeId": provider_trade_id,
    }
    conn.execute(
        """
        UPDATE payment_orders
        SET status = ?,
            provider_trade_id = COALESCE(?, provider_trade_id),
            updated_at = ?,
            metadata = ?
        WHERE payment_order_id = ?
        """,
        (status, provider_trade_id, now, _json(updated_meta), order["payment_order_id"]),
    )
    return {
        "processed": True,
        "idempotent": False,
        "eventType": f"payment_{status}",
        "message": f"Payment order marked {status}",
        "transaction": None,
    }


def _insert_payment_webhook_event(
    *,
    conn: sqlite3.Connection,
    event_id: str,
    provider: str,
    payment_order_id: str | None,
    event_type: str,
    status: str,
    signature_valid: bool,
    processed: bool,
    idempotent: bool,
    message: str,
    metadata: dict[str, Any],
    created_at: str,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO payment_webhook_events (
            event_id,
            provider,
            payment_order_id,
            event_type,
            status,
            signature_valid,
            processed,
            idempotent,
            message,
            created_at,
            metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            provider,
            payment_order_id,
            event_type,
            status,
            1 if signature_valid else 0,
            1 if processed else 0,
            1 if idempotent else 0,
            str(message or ""),
            created_at,
            _json(metadata),
        ),
    )
    return _payment_webhook_event_row(conn, event_id)


def _payment_order_row(conn: sqlite3.Connection, payment_order_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM payment_orders WHERE payment_order_id = ?",
        (payment_order_id,),
    ).fetchone()


def _payment_webhook_event_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM payment_webhook_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()


def _apply_entry(
    *,
    user_id: str,
    order_id: str,
    kind: str,
    points: int,
    db_path: str | os.PathLike[str] | None,
    description: str,
    metadata: dict[str, Any] | None,
    event_type: str,
) -> dict[str, Any]:
    user_id = _clean_id(user_id, "user_id")
    order_id = _clean_id(order_id, "order_id")
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _apply_entry_in_conn(
                conn=conn,
                user_id=user_id,
                order_id=order_id,
                kind=kind,
                points=points,
                description=description,
                metadata=metadata,
                event_type=event_type,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _apply_entry_in_conn(
    *,
    conn: sqlite3.Connection,
    user_id: str,
    order_id: str,
    kind: str,
    points: int,
    description: str,
    metadata: dict[str, Any] | None,
    event_type: str,
) -> dict[str, Any]:
    event_type = str(event_type or "").strip() or "ledger_entry"
    _ensure_account(conn, user_id)
    existing = _order_row(conn, order_id)
    if existing:
        return _idempotent_result(conn, existing, user_id, kind, points)

    balance = int(_account_row(conn, user_id)["balance"])
    if kind == "debit" and balance < points:
        raise InsufficientBalance(required=points, available=balance)

    balance_after = balance + points if kind == "credit" else balance - points
    now = _now()
    meta = dict(metadata or {})
    meta.setdefault("eventType", event_type)
    meta_json = _json(meta)
    conn.execute(
        "UPDATE accounts SET balance = ?, updated_at = ? WHERE user_id = ?",
        (balance_after, now, user_id),
    )
    conn.execute(
        """
        INSERT INTO orders (order_id, user_id, kind, points, status, created_at, metadata)
        VALUES (?, ?, ?, ?, 'succeeded', ?, ?)
        """,
        (order_id, user_id, kind, points, now, meta_json),
    )
    cursor = conn.execute(
        """
        INSERT INTO ledger (
            order_id,
            user_id,
            direction,
            points,
            balance_after,
            event_type,
            description,
            created_at,
            metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, user_id, kind, points, balance_after, event_type, str(description or ""), now, meta_json),
    )
    return {
        "ok": True,
        "idempotent": False,
        "userId": user_id,
        "orderId": order_id,
        "direction": kind,
        "points": points,
        "balance": balance_after,
        "balanceAfter": balance_after,
        "ledgerId": cursor.lastrowid,
        "eventType": event_type,
        "createdAt": now,
    }


def _idempotent_result(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    user_id: str,
    kind: str,
    points: int,
) -> dict[str, Any]:
    if order["user_id"] != user_id or order["kind"] != kind or int(order["points"]) != points:
        raise OrderConflict(
            "Order id already belongs to a different billing operation",
            orderId=order["order_id"],
            existingUserId=order["user_id"],
            existingKind=order["kind"],
            existingPoints=int(order["points"]),
        )
    ledger = conn.execute("SELECT * FROM ledger WHERE order_id = ?", (order["order_id"],)).fetchone()
    if ledger is None:
        raise OrderConflict("Order exists without a ledger entry", orderId=order["order_id"])
    account = _account_row(conn, user_id)
    return {
        "ok": True,
        "idempotent": True,
        "userId": user_id,
        "orderId": order["order_id"],
        "direction": order["kind"],
        "points": int(order["points"]),
        "balance": int(account["balance"]),
        "balanceAfter": int(ledger["balance_after"]),
        "ledgerId": int(ledger["id"]),
        "eventType": ledger["event_type"],
        "createdAt": ledger["created_at"],
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "ledger", "event_type", "TEXT NOT NULL DEFAULT 'ledger_entry'")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_account(conn: sqlite3.Connection, user_id: str) -> None:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, created_at, metadata) VALUES (?, ?, '{}')",
        (user_id, now),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (user_id, balance, created_at, updated_at)
        VALUES (?, 0, ?, ?)
        """,
        (user_id, now, now),
    )


def _account_row(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise InvalidBillingInput("Account does not exist", userId=user_id)
    return row


def _order_row(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()


def _platform_count(
    platforms: list[str] | tuple[str, ...] | None,
    platform_count: int | None,
) -> int:
    if platforms is not None:
        return len({str(platform).strip() for platform in platforms if str(platform).strip()})
    if platform_count is None:
        return 1
    return _non_negative_int(platform_count, "platform_count")


def _cash_to_int(value: int | str | Decimal | None) -> int:
    if value is None or isinstance(value, bool):
        raise InvalidRechargePackage("Recharge cash amount is required")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidRechargePackage("Recharge cash amount must be numeric", value=value) from exc
    if amount != amount.to_integral_value() or amount <= 0:
        raise InvalidRechargePackage("Recharge cash amount must be a positive whole number", value=value)
    return int(amount)


def _clean_id(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidBillingInput(f"{field} is required", field=field)
    return text


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidBillingInput(f"{field} must be a positive integer", field=field, value=value)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidBillingInput(f"{field} must be a positive integer", field=field, value=value) from exc
    if number <= 0:
        raise InvalidBillingInput(f"{field} must be a positive integer", field=field, value=value)
    return number


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidBillingInput(f"{field} must be a non-negative integer", field=field, value=value)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidBillingInput(f"{field} must be a non-negative integer", field=field, value=value) from exc
    if number < 0:
        raise InvalidBillingInput(f"{field} must be a non-negative integer", field=field, value=value)
    return number


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | bytes | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_plus_seconds(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))
