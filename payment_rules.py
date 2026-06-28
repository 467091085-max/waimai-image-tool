from __future__ import annotations

import hashlib
import json
from typing import Final


STATUS_PENDING: Final = "pending"
STATUS_PAID: Final = "paid"
STATUS_FAILED: Final = "failed"
STATUS_REFUNDED: Final = "refunded"
STATUS_CLOSED: Final = "closed"

PAYMENT_STATUSES: Final = (
    STATUS_PENDING,
    STATUS_PAID,
    STATUS_FAILED,
    STATUS_REFUNDED,
    STATUS_CLOSED,
)

CENTS_PER_YUAN: Final = 100
POINTS_PER_YUAN: Final = 10

_ALLOWED_TRANSITIONS: Final = {
    STATUS_PENDING: frozenset(
        {
            STATUS_PENDING,
            STATUS_PAID,
            STATUS_FAILED,
            STATUS_CLOSED,
        }
    ),
    STATUS_PAID: frozenset({STATUS_PAID, STATUS_REFUNDED}),
    STATUS_FAILED: frozenset({STATUS_FAILED}),
    STATUS_REFUNDED: frozenset({STATUS_REFUNDED}),
    STATUS_CLOSED: frozenset({STATUS_CLOSED}),
}


def transition_payment_status(current: str, target: str) -> str:
    """Return target when a payment status transition is legal.

    Replaying the same status is allowed as an idempotent no-op. Failed,
    refunded, and closed payments are terminal.
    """
    current_status = _normalize_status(current, "current")
    target_status = _normalize_status(target, "target")

    if target_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise ValueError(
            f"illegal payment status transition: {current_status!r} -> {target_status!r}"
        )
    return target_status


def idempotency_key(provider: str, out_trade_no: str, event_type: str) -> str:
    """Return a stable idempotency key for a provider payment event."""
    payload = {
        "provider": _clean_text(provider, "provider").lower(),
        "out_trade_no": _clean_text(out_trade_no, "out_trade_no"),
        "event_type": _clean_text(event_type, "event_type").lower(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"payment:{hashlib.sha256(encoded).hexdigest()}"


def payment_points(paid_cents: int, point_rate: int = POINTS_PER_YUAN) -> int:
    """Return points earned for cash paid in cents, rounded down."""
    cents = _non_negative_int(paid_cents, "paid_cents")
    rate = _non_negative_int(point_rate, "point_rate")
    return cents * rate // CENTS_PER_YUAN


def refund_points(
    refund_cents: int,
    original_paid_cents: int,
    original_points: int,
) -> int:
    """Return points to reverse for a refund, capped at original_points."""
    refund = _non_negative_int(refund_cents, "refund_cents")
    paid = _non_negative_int(original_paid_cents, "original_paid_cents")
    points = _non_negative_int(original_points, "original_points")

    if refund == 0 or points == 0:
        return 0
    if paid == 0:
        raise ValueError("original_paid_cents must be positive when refund_cents is positive")
    return min(points, refund * points // paid)


def _normalize_status(status: str, name: str) -> str:
    if not isinstance(status, str):
        raise TypeError(f"{name} must be a string")
    normalized = status.strip().lower()
    if normalized not in PAYMENT_STATUSES:
        raise ValueError(f"unsupported payment status for {name}: {status!r}")
    return normalized


def _clean_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must be non-empty")
    return cleaned


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value
