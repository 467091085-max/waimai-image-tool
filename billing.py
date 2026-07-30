from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from shared import payment_catalog

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "app.db"
DEFAULT_USER_ID = "default"

POINT_RATE = 10
QUALITY_POINTS = {
    "standard": 10,
    "premium": 20,
}
WATERMARK_POINTS = 50
EXTRA_PLATFORM_POINTS = 100

RECHARGE_PACKAGES = payment_catalog.legacy_cash_packages()
CUSTOM_RECHARGE_MIN_CASH = 100


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
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS refund_allocations (
    refund_order_id TEXT PRIMARY KEY,
    source_order_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    points INTEGER NOT NULL CHECK (points > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (refund_order_id) REFERENCES orders(order_id),
    FOREIGN KEY (source_order_id) REFERENCES orders(order_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_refund_allocations_source
ON refund_allocations(source_order_id, user_id);
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
) -> dict[str, Any]:
    return _apply_entry(
        user_id=user_id,
        order_id=order_id,
        kind="credit",
        points=_positive_int(points, "points"),
        db_path=db_path,
        description=description,
        metadata=metadata,
    )


def debit_account(
    user_id: str,
    order_id: str,
    points: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
    description: str = "debit",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _apply_entry(
        user_id=user_id,
        order_id=order_id,
        kind="debit",
        points=_positive_int(points, "points"),
        db_path=db_path,
        description=description,
        metadata=metadata,
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
    db_path: str | os.PathLike[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    points = calculate_image_charge(
        image_count=image_count,
        quality=quality,
        watermark=watermark,
        platforms=platforms,
        platform_count=platform_count,
    )
    return debit_account(
        user_id,
        order_id,
        points,
        db_path=db_path,
        description="image-generation",
        metadata={
            "imageCount": image_count,
            "quality": quality,
            "watermark": bool(watermark),
            "platforms": list(platforms) if platforms is not None else None,
            "platformCount": platform_count,
            **(metadata or {}),
        },
    )


def refund_debit(
    user_id: str,
    source_order_id: str,
    *,
    points: int | None = None,
    refund_order_id: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
    description: str = "generation-refund",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_source_order_id = _clean_id(source_order_id, "source_order_id")
    return _apply_refund(
        user_id=_clean_id(user_id, "user_id"),
        source_order_id=clean_source_order_id,
        refund_order_id=_clean_id(
            refund_order_id or f"refund:{clean_source_order_id}",
            "refund_order_id",
        ),
        points=None if points is None else _positive_int(points, "points"),
        target_total_points=None,
        db_path=db_path,
        description=description,
        metadata=metadata,
    )


def refund_debit_to_total(
    user_id: str,
    source_order_id: str,
    *,
    target_points: int,
    refund_order_prefix: str | None = None,
    db_path: str | os.PathLike[str] | None = None,
    description: str = "generation-refund",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_source_order_id = _clean_id(source_order_id, "source_order_id")
    clean_target = _positive_int(target_points, "target_points")
    prefix = _clean_id(
        refund_order_prefix or f"refund:{clean_source_order_id}",
        "refund_order_prefix",
    )
    return _apply_refund(
        user_id=_clean_id(user_id, "user_id"),
        source_order_id=clean_source_order_id,
        refund_order_id=f"{prefix}:{clean_target}",
        points=None,
        target_total_points=clean_target,
        db_path=db_path,
        description=description,
        metadata=metadata,
    )


def points_for_recharge(cash_amount: int | str | Decimal) -> int:
    cash = _cash_to_int(cash_amount)
    if cash in RECHARGE_PACKAGES:
        return RECHARGE_PACKAGES[cash]
    if cash >= CUSTOM_RECHARGE_MIN_CASH:
        return cash * POINT_RATE
    raise InvalidRechargePackage(
        "Unsupported recharge amount",
        cash=cash,
        supported=list(RECHARGE_PACKAGES),
        customMinCash=CUSTOM_RECHARGE_MIN_CASH,
    )


def calculate_image_charge(
    image_count: int,
    quality: str | int = "standard",
    *,
    watermark: bool = False,
    platforms: list[str] | tuple[str, ...] | None = None,
    platform_count: int | None = None,
) -> int:
    images = _non_negative_int(image_count, "image_count")
    per_image = quality_point_value(quality)
    total_platforms = _platform_count(platforms, platform_count)
    return (images * per_image) + (WATERMARK_POINTS if watermark else 0) + (max(total_platforms - 1, 0) * EXTRA_PLATFORM_POINTS)


def quality_point_value(quality: str | int) -> int:
    if isinstance(quality, int) and not isinstance(quality, bool):
        return _positive_int(quality, "quality")
    key = str(quality or "standard").strip().lower()
    if key not in QUALITY_POINTS:
        raise InvalidBillingInput("Unknown quality option", quality=quality, supported=list(QUALITY_POINTS))
    return QUALITY_POINTS[key]


def recharge_packages_payload() -> list[dict[str, Any]]:
    return [
        {
            "packageId": package["packageId"],
            "name": package["name"],
            "cash": package["amountCents"] // 100,
            "amountCents": package["amountCents"],
            "points": package["points"],
            "bonus": 0,
            "currency": package["currency"],
            "catalogVersion": package["catalogVersion"],
            "snapshotDigest": package["snapshotDigest"],
        }
        for package in payment_catalog.catalog_snapshot()["packages"]
    ]


def pricing_payload() -> dict[str, Any]:
    catalog = payment_catalog.catalog_snapshot()
    return {
        "rate": f"1 yuan = {POINT_RATE} points",
        "qualityPoints": dict(QUALITY_POINTS),
        "watermarkPoints": WATERMARK_POINTS,
        "extraPlatformPoints": EXTRA_PLATFORM_POINTS,
        "customRechargeMinCash": CUSTOM_RECHARGE_MIN_CASH,
        "catalogVersion": catalog["catalogVersion"],
        "catalogCurrency": catalog["currency"],
        "catalogDigest": catalog["catalogDigest"],
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
            "minCash": CUSTOM_RECHARGE_MIN_CASH,
            "rate": POINT_RATE,
        },
        "pricing": pricing_payload(),
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
            )
        result = debit_account(actual_user_id, order_id, points, db_path=db_path)
        return {"ok": True, "transaction": result, "account": account_payload(actual_user_id, db_path)}, 200
    except BillingError as exc:
        return billing_error_response(exc)


def _apply_entry(
    *,
    user_id: str,
    order_id: str,
    kind: str,
    points: int,
    db_path: str | os.PathLike[str] | None,
    description: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    user_id = _clean_id(user_id, "user_id")
    order_id = _clean_id(order_id, "order_id")
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _apply_entry_on_connection(
                conn,
                user_id=user_id,
                order_id=order_id,
                kind=kind,
                points=points,
                description=description,
                metadata=metadata,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _apply_entry_on_connection(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    order_id: str,
    kind: str,
    points: int,
    description: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    _ensure_account(conn, user_id)
    existing = _order_row(conn, order_id)
    if existing:
        return _idempotent_result(conn, existing, user_id, kind, points)

    balance = int(_account_row(conn, user_id)["balance"])
    if kind == "debit" and balance < points:
        raise InsufficientBalance(required=points, available=balance)

    balance_after = balance + points if kind == "credit" else balance - points
    now = _now()
    meta_json = _json(metadata or {})
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
        INSERT INTO ledger (order_id, user_id, direction, points, balance_after, description, created_at, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, user_id, kind, points, balance_after, str(description or ""), now, meta_json),
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
        "createdAt": now,
    }


def _apply_refund(
    *,
    user_id: str,
    source_order_id: str,
    refund_order_id: str,
    points: int | None,
    target_total_points: int | None,
    db_path: str | os.PathLike[str] | None,
    description: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    with open_db(db_path) as conn:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_account(conn, user_id)
            source = _order_row(conn, source_order_id)
            if source is None:
                raise InvalidBillingInput(
                    "Source debit order does not exist",
                    sourceOrderId=source_order_id,
                )
            if source["user_id"] != user_id or source["kind"] != "debit":
                raise InvalidBillingInput(
                    "Source order is not a debit owned by this user",
                    sourceOrderId=source_order_id,
                )
            source_points = int(source["points"])
            _sync_legacy_refund_allocations(
                conn,
                user_id=user_id,
                source_order_id=source_order_id,
            )
            refunded_total = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(points), 0)
                    FROM refund_allocations
                    WHERE source_order_id = ? AND user_id = ?
                    """,
                    (source_order_id, user_id),
                ).fetchone()[0]
            )
            existing_allocation = conn.execute(
                """
                SELECT * FROM refund_allocations WHERE refund_order_id = ?
                """,
                (refund_order_id,),
            ).fetchone()
            if existing_allocation is not None and target_total_points is None:
                expected_points = source_points if points is None else int(points)
                if (
                    existing_allocation["source_order_id"] != source_order_id
                    or existing_allocation["user_id"] != user_id
                    or int(existing_allocation["points"]) != expected_points
                ):
                    raise OrderConflict(
                        "Refund order id belongs to a different source allocation",
                        orderId=refund_order_id,
                    )
                order = _order_row(conn, refund_order_id)
                if order is None:
                    raise OrderConflict(
                        "Refund allocation exists without a billing order",
                        orderId=refund_order_id,
                    )
                result = _idempotent_result(
                    conn,
                    order,
                    user_id,
                    "credit",
                    expected_points,
                )
                conn.commit()
                return {
                    **result,
                    "sourceOrderId": source_order_id,
                    "sourcePoints": source_points,
                    "refundedTotal": refunded_total,
                    "targetPoints": None,
                }
            if target_total_points is not None:
                if target_total_points > source_points:
                    raise InvalidBillingInput(
                        "Refund exceeds source debit",
                        sourceOrderId=source_order_id,
                        requested=target_total_points,
                        maximum=source_points,
                    )
                if refunded_total >= target_total_points:
                    account = _account_row(conn, user_id)
                    conn.commit()
                    return {
                        "ok": True,
                        "idempotent": True,
                        "userId": user_id,
                        "orderId": refund_order_id,
                        "direction": "credit",
                        "points": 0,
                        "balance": int(account["balance"]),
                        "balanceAfter": int(account["balance"]),
                        "sourceOrderId": source_order_id,
                        "sourcePoints": source_points,
                        "refundedTotal": refunded_total,
                        "targetPoints": target_total_points,
                    }
                refund_points = target_total_points - refunded_total
            else:
                refund_points = source_points if points is None else int(points)
                if refunded_total + refund_points > source_points:
                    raise InvalidBillingInput(
                        "Refund exceeds remaining source debit",
                        sourceOrderId=source_order_id,
                        requested=refund_points,
                        refunded=refunded_total,
                        maximum=max(0, source_points - refunded_total),
                    )

            if existing_allocation is not None:
                if (
                    existing_allocation["source_order_id"] != source_order_id
                    or existing_allocation["user_id"] != user_id
                    or int(existing_allocation["points"]) != refund_points
                ):
                    raise OrderConflict(
                        "Refund order id belongs to a different source allocation",
                        orderId=refund_order_id,
                    )
                order = _order_row(conn, refund_order_id)
                if order is None:
                    raise OrderConflict(
                        "Refund allocation exists without a billing order",
                        orderId=refund_order_id,
                    )
                result = _idempotent_result(
                    conn,
                    order,
                    user_id,
                    "credit",
                    refund_points,
                )
                conn.commit()
                return {
                    **result,
                    "sourceOrderId": source_order_id,
                    "sourcePoints": source_points,
                    "refundedTotal": refunded_total,
                    "targetPoints": target_total_points,
                }
            if _order_row(conn, refund_order_id) is not None:
                raise OrderConflict(
                    "Refund order exists without a matching source allocation",
                    orderId=refund_order_id,
                )

            result = _apply_entry_on_connection(
                conn,
                user_id=user_id,
                order_id=refund_order_id,
                kind="credit",
                points=refund_points,
                description=description,
                metadata={
                    "sourceOrderId": source_order_id,
                    "sourcePoints": source_points,
                    **(metadata or {}),
                },
            )
            conn.execute(
                """
                INSERT INTO refund_allocations (
                    refund_order_id, source_order_id, user_id, points, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    refund_order_id,
                    source_order_id,
                    user_id,
                    refund_points,
                    result["createdAt"],
                ),
            )
            conn.commit()
            return {
                **result,
                "sourceOrderId": source_order_id,
                "sourcePoints": source_points,
                "refundedTotal": refunded_total + refund_points,
                "targetPoints": target_total_points,
            }
        except Exception:
            conn.rollback()
            raise


def _sync_legacy_refund_allocations(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    source_order_id: str,
) -> None:
    rows = conn.execute(
        """
        SELECT order_id, points, created_at, metadata
        FROM orders
        WHERE user_id = ? AND kind = 'credit'
        """,
        (user_id,),
    ).fetchall()
    for row in rows:
        try:
            order_metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(order_metadata, dict):
            continue
        if str(order_metadata.get("sourceOrderId") or "") != source_order_id:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO refund_allocations (
                refund_order_id, source_order_id, user_id, points, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["order_id"],
                source_order_id,
                user_id,
                int(row["points"]),
                row["created_at"],
            ),
        )


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
        "createdAt": ledger["created_at"],
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


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


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
