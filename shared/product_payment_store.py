from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from uuid import uuid4

import growth_rules
import payment_rules
from shared import payment_catalog, product_growth_outbox


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_PROVIDERS = frozenset({"alipay", "wechat"})
EVENT_PAYMENT_SUCCEEDED = "payment_succeeded"
EVENT_REFUND_SUCCEEDED = "refund_succeeded"
EVENT_PAYMENT_FAILED = "payment_failed"
EVENT_PAYMENT_CLOSED = "payment_closed"
SUPPORTED_EVENT_KINDS = frozenset(
    {
        EVENT_PAYMENT_SUCCEEDED,
        EVENT_REFUND_SUCCEEDED,
        EVENT_PAYMENT_FAILED,
        EVENT_PAYMENT_CLOSED,
    }
)


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductPaymentStoreError(RuntimeError):
    pass


class InvalidProductPaymentInput(ProductPaymentStoreError, ValueError):
    pass


class ProductPaymentOrderNotFound(ProductPaymentStoreError, LookupError):
    pass


class ProductPaymentOrderConflict(ProductPaymentStoreError):
    pass


class ProductPaymentEventConflict(ProductPaymentStoreError):
    pass


class ProductPaymentStateConflict(ProductPaymentStoreError):
    pass


class ProductPaymentAmountMismatch(ProductPaymentStoreError):
    def __init__(self, *, expected_amount_cents: int, actual_amount_cents: int) -> None:
        super().__init__(
            "payment amount does not match frozen order: "
            f"expected={expected_amount_cents}, actual={actual_amount_cents}"
        )
        self.expected_amount_cents = expected_amount_cents
        self.actual_amount_cents = actual_amount_cents


class InsufficientPaymentRefundBalance(ProductPaymentStoreError):
    def __init__(self, *, available_points: int, required_points: int) -> None:
        super().__init__(
            "insufficient point balance for payment refund: "
            f"available={available_points}, required={required_points}"
        )
        self.available_points = available_points
        self.required_points = required_points


class ProductPaymentWalletIntegrityError(ProductPaymentStoreError):
    pass


@dataclass(frozen=True)
class PaymentOrderCreateResult:
    order: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class PaymentEventApplyResult:
    order: dict[str, Any]
    event: dict[str, Any]
    account: dict[str, Any] | None
    idempotent: bool
    points_credited: int
    points_debited: int


@dataclass(frozen=True)
class _EventPlan:
    target_status: str
    amount_cents: int | None
    points_delta: int
    refunded_amount_cents_after: int
    refunded_points_after: int
    wallet_order_id: str | None


_ORDER_COLUMNS = """
    id, owner_user_id, provider, provider_order_id, idempotency_key,
    package_id, catalog_version, currency, amount_cents, points,
    catalog_snapshot, catalog_snapshot_sha256, provider_payload,
    status, credited_points, refunded_amount_cents, refunded_points,
    credit_point_order_id, version, created_at, updated_at,
    paid_at, refunded_at, closed_at
""".strip()

_EVENT_COLUMNS = """
    id, owner_user_id, order_id, provider, provider_order_id,
    provider_event_id, event_kind, event_type, target_status,
    amount_cents, points_delta, refunded_amount_cents_after,
    refunded_points_after, wallet_order_id, payload, content_sha256,
    created_at
""".strip()

_ACCOUNT_COLUMNS = """
    owner_user_id, balance_points, lifetime_credited_points,
    lifetime_debited_points, lifetime_refunded_points, version,
    metadata, created_at, updated_at
""".strip()

_POINT_ORDER_COLUMNS = """
    id, owner_user_id, order_kind, points, source_order_id,
    source_order_kind, job_id, request_sha256, metadata,
    applied_at, created_at, updated_at
""".strip()

_SELECT_ORDER_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_payment_store:select_order_by_idempotency_for_update */
SELECT {_ORDER_COLUMNS}
FROM product_payment_orders
WHERE owner_user_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_ORDER_BY_ID_FOR_UPDATE_SQL = f"""
/* product_payment_store:select_order_by_id_for_update */
SELECT {_ORDER_COLUMNS}
FROM product_payment_orders
WHERE id = %s
FOR UPDATE
"""

_SELECT_ORDER_BY_PROVIDER_FOR_UPDATE_SQL = f"""
/* product_payment_store:select_order_by_provider_for_update */
SELECT {_ORDER_COLUMNS}
FROM product_payment_orders
WHERE provider = %s AND provider_order_id = %s
FOR UPDATE
"""

_SELECT_ORDER_BY_PROVIDER_SQL = f"""
/* product_payment_store:select_order_by_provider */
SELECT {_ORDER_COLUMNS}
FROM product_payment_orders
WHERE provider = %s AND provider_order_id = %s
"""

_INSERT_ORDER_SQL = f"""
/* product_payment_store:insert_order */
INSERT INTO product_payment_orders (
    id, owner_user_id, provider, provider_order_id, idempotency_key,
    package_id, catalog_version, currency, amount_cents, points,
    catalog_snapshot, catalog_snapshot_sha256, provider_payload
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s::jsonb, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_ORDER_COLUMNS}
"""

_ATTACH_PROVIDER_PAYLOAD_SQL = f"""
/* product_payment_store:attach_provider_payload */
UPDATE product_payment_orders
SET provider_payload = %s::jsonb,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status = 'pending'
  AND version = %s
RETURNING {_ORDER_COLUMNS}
"""

_SELECT_EVENT_FOR_UPDATE_SQL = f"""
/* product_payment_store:select_event_for_update */
SELECT {_EVENT_COLUMNS}
FROM product_payment_events
WHERE provider = %s AND provider_event_id = %s
FOR UPDATE
"""

_INSERT_EVENT_SQL = f"""
/* product_payment_store:insert_event */
INSERT INTO product_payment_events (
    owner_user_id, order_id, provider, provider_order_id,
    provider_event_id, event_kind, event_type, target_status,
    amount_cents, points_delta, refunded_amount_cents_after,
    refunded_points_after, wallet_order_id, payload, content_sha256
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s::jsonb, %s
)
ON CONFLICT (provider, provider_event_id) DO NOTHING
RETURNING {_EVENT_COLUMNS}
"""

_INSERT_POINT_ACCOUNT_SQL = f"""
/* product_payment_store:insert_point_account */
INSERT INTO product_point_accounts (owner_user_id, metadata)
VALUES (%s, '{{}}'::jsonb)
ON CONFLICT (owner_user_id) DO NOTHING
RETURNING {_ACCOUNT_COLUMNS}
"""

_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL = f"""
/* product_payment_store:select_point_account_for_update */
SELECT {_ACCOUNT_COLUMNS}
FROM product_point_accounts
WHERE owner_user_id = %s
FOR UPDATE
"""

_INSERT_POINT_ORDER_SQL = f"""
/* product_payment_store:insert_point_order */
INSERT INTO product_point_orders (
    id, owner_user_id, order_kind, points,
    source_order_id, source_order_kind, job_id,
    request_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    NULL, NULL, NULL,
    %s, %s::jsonb
)
ON CONFLICT (id) DO NOTHING
RETURNING {_POINT_ORDER_COLUMNS}
"""

_SELECT_POINT_ORDER_SQL = f"""
/* product_payment_store:select_point_order */
SELECT {_POINT_ORDER_COLUMNS}
FROM product_point_orders
WHERE id = %s
"""

_CREDIT_POINT_ACCOUNT_SQL = f"""
/* product_payment_store:credit_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points + %s,
    lifetime_credited_points = lifetime_credited_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_DEBIT_POINT_ACCOUNT_SQL = f"""
/* product_payment_store:debit_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points - %s,
    lifetime_debited_points = lifetime_debited_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
  AND balance_points >= %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_INSERT_POINT_LEDGER_SQL = """
/* product_payment_store:insert_point_ledger */
INSERT INTO product_point_ledger (
    owner_user_id, order_id, order_kind, points, delta_points,
    balance_after_points, request_sha256, metadata
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
"""

_MARK_ORDER_PAID_SQL = f"""
/* product_payment_store:mark_order_paid */
UPDATE product_payment_orders
SET status = 'paid',
    credited_points = points,
    credit_point_order_id = %s,
    paid_at = COALESCE(paid_at, CURRENT_TIMESTAMP),
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status = 'pending'
  AND version = %s
RETURNING {_ORDER_COLUMNS}
"""

_MARK_ORDER_REFUNDED_SQL = f"""
/* product_payment_store:mark_order_refunded */
UPDATE product_payment_orders
SET status = %s,
    refunded_amount_cents = %s,
    refunded_points = %s,
    refunded_at = COALESCE(refunded_at, CURRENT_TIMESTAMP),
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status IN ('paid', 'partially_refunded')
  AND version = %s
RETURNING {_ORDER_COLUMNS}
"""

_MARK_ORDER_TERMINAL_SQL = f"""
/* product_payment_store:mark_order_terminal */
UPDATE product_payment_orders
SET status = %s,
    closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP),
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND owner_user_id = %s
  AND status = 'pending'
  AND version = %s
RETURNING {_ORDER_COLUMNS}
"""


class ProductPaymentStore:
    """PostgreSQL payment and wallet operations over an injected DB connection.

    The store never opens a connection, never runs a migration, and never verifies
    provider signatures. The caller must pass only provider events that have
    already passed the provider-specific signature and authenticity checks.
    """

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductPaymentInput(
                "ProductPaymentStore requires an autocommit-disabled connection"
            )
        self.connection = connection

    def create_or_get_order(
        self,
        *,
        owner_user_id: str,
        provider: str,
        package_id: str,
        idempotency_key: str,
        order_id: str | None = None,
        provider_order_id: str | None = None,
        provider_payload: Mapping[str, Any] | None = None,
    ) -> PaymentOrderCreateResult:
        """Freeze a server catalog package in one durable live payment order."""

        owner = _identifier(owner_user_id, "owner_user_id")
        clean_provider = _provider(provider)
        idem_key = _identifier(idempotency_key, "idempotency_key")
        requested_order_id = (
            _identifier(order_id, "order_id")
            if order_id is not None
            else None
        )
        requested_provider_order_id = (
            _provider_identifier(provider_order_id, "provider_order_id")
            if provider_order_id is not None
            else None
        )
        snapshot = _server_catalog_snapshot(package_id)
        snapshot_json = _json_object(snapshot, "catalog_snapshot")
        snapshot_sha256 = _sha256(
            snapshot.get("snapshotDigest"),
            "catalog_snapshot.snapshotDigest",
        )
        expected_payload = (
            None
            if provider_payload is None
            else json.loads(_json_object(provider_payload, "provider_payload"))
        )
        provider_payload_json = _json_object(
            expected_payload or {},
            "provider_payload",
        )

        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_ORDER_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
                (owner, idem_key),
            )
            existing = _fetchone_dict(cursor)
            if existing is not None:
                existing = _decode_row(existing)
                _validate_order_replay(
                    existing,
                    owner_user_id=owner,
                    provider=clean_provider,
                    idempotency_key=idem_key,
                    snapshot=snapshot,
                    snapshot_sha256=snapshot_sha256,
                    provider_payload=expected_payload,
                    expected_order_id=requested_order_id,
                    expected_provider_order_id=requested_provider_order_id,
                )
                return PaymentOrderCreateResult(order=existing, created=False)

            identifier = (
                requested_order_id
                if requested_order_id is not None
                else f"pay_{uuid4().hex}"
            )
            provider_identifier = (
                requested_provider_order_id
                if requested_provider_order_id is not None
                else identifier
            )
            cursor.execute(
                _INSERT_ORDER_SQL,
                (
                    identifier,
                    owner,
                    clean_provider,
                    provider_identifier,
                    idem_key,
                    str(snapshot["packageId"]),
                    str(snapshot["catalogVersion"]),
                    str(snapshot["currency"]),
                    int(snapshot["amountCents"]),
                    int(snapshot["points"]),
                    snapshot_json,
                    snapshot_sha256,
                    provider_payload_json,
                ),
            )
            inserted = _fetchone_dict(cursor)
            if inserted is not None:
                return PaymentOrderCreateResult(
                    order=_decode_row(inserted),
                    created=True,
                )

            cursor.execute(
                _SELECT_ORDER_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
                (owner, idem_key),
            )
            raced = _fetchone_dict(cursor)
            if raced is not None:
                raced = _decode_row(raced)
                _validate_order_replay(
                    raced,
                    owner_user_id=owner,
                    provider=clean_provider,
                    idempotency_key=idem_key,
                    snapshot=snapshot,
                    snapshot_sha256=snapshot_sha256,
                    provider_payload=expected_payload,
                    expected_order_id=identifier,
                    expected_provider_order_id=provider_identifier,
                )
                return PaymentOrderCreateResult(order=raced, created=False)

            cursor.execute(_SELECT_ORDER_BY_ID_FOR_UPDATE_SQL, (identifier,))
            conflicting_order = _fetchone_dict(cursor)
            if conflicting_order is not None:
                raise ProductPaymentOrderConflict(
                    f"payment order ID already belongs to another request: {identifier}"
                )
            cursor.execute(
                _SELECT_ORDER_BY_PROVIDER_FOR_UPDATE_SQL,
                (clean_provider, provider_identifier),
            )
            conflicting_provider_order = _fetchone_dict(cursor)
            if conflicting_provider_order is not None:
                raise ProductPaymentOrderConflict(
                    "provider order ID already belongs to another request: "
                    f"{clean_provider}/{provider_identifier}"
                )
            raise ProductPaymentOrderConflict(
                "payment order insert conflicted without a resolvable owner/idempotency row"
            )

    def get_order_by_provider(
        self,
        *,
        provider: str,
        provider_order_id: str,
    ) -> dict[str, Any]:
        clean_provider = _provider(provider)
        provider_order = _provider_identifier(
            provider_order_id,
            "provider_order_id",
        )
        with self._transaction() as cursor:
            cursor.execute(
                _SELECT_ORDER_BY_PROVIDER_SQL,
                (clean_provider, provider_order),
            )
            order = _fetchone_dict(cursor)
            if order is None:
                raise ProductPaymentOrderNotFound(
                    f"payment order not found: {clean_provider}/{provider_order}"
                )
            return _decode_row(order)

    def attach_provider_payload(
        self,
        *,
        owner_user_id: str,
        order_id: str,
        provider_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind a checkout response once while the payment order is pending."""

        owner = _identifier(owner_user_id, "owner_user_id")
        identifier = _identifier(order_id, "order_id")
        payload_json = _json_object(provider_payload, "provider_payload")
        expected_payload = json.loads(payload_json)
        if not expected_payload:
            raise InvalidProductPaymentInput(
                "provider_payload must not be empty"
            )

        with self._transaction() as cursor:
            cursor.execute(_SELECT_ORDER_BY_ID_FOR_UPDATE_SQL, (identifier,))
            order = _fetchone_dict(cursor)
            if order is None or str(order.get("owner_user_id") or "") != owner:
                raise ProductPaymentOrderNotFound(
                    f"payment order not found: {identifier}"
                )
            order = _decode_row(order)
            existing_payload = order.get("provider_payload") or {}
            if existing_payload:
                if existing_payload != expected_payload:
                    raise ProductPaymentOrderConflict(
                        f"payment provider payload is already frozen: {identifier}"
                    )
                return order
            if str(order.get("status") or "") != "pending":
                raise ProductPaymentStateConflict(
                    "payment provider payload can only be attached to a pending order"
                )

            cursor.execute(
                _ATTACH_PROVIDER_PAYLOAD_SQL,
                (
                    payload_json,
                    identifier,
                    owner,
                    int(order["version"]),
                ),
            )
            updated = _fetchone_dict(cursor)
            if updated is None:
                raise ProductPaymentOrderConflict(
                    f"payment provider payload update lost a race: {identifier}"
                )
            return _decode_row(updated)

    def apply_verified_event(
        self,
        *,
        owner_user_id: str,
        provider: str,
        provider_order_id: str,
        provider_event_id: str,
        event_kind: str,
        event_type: str,
        payload: Mapping[str, Any],
        amount_cents: int | None = None,
        transaction_effect: (
            Callable[[CursorLike, PaymentEventApplyResult], None] | None
        ) = None,
    ) -> PaymentEventApplyResult:
        """Atomically persist a verified provider event and attached effects."""

        owner = _identifier(owner_user_id, "owner_user_id")
        clean_provider = _provider(provider)
        provider_order = _provider_identifier(
            provider_order_id,
            "provider_order_id",
        )
        provider_event = _provider_identifier(
            provider_event_id,
            "provider_event_id",
        )
        kind = _event_kind(event_kind)
        clean_event_type = _provider_identifier(event_type, "event_type")
        event_amount = _optional_positive_int(amount_cents, "amount_cents")
        payload_json = _json_object(payload, "payload")
        expected_payload = json.loads(payload_json)
        content_sha256 = _event_content_sha256(
            owner_user_id=owner,
            provider=clean_provider,
            provider_order_id=provider_order,
            provider_event_id=provider_event,
            event_kind=kind,
            event_type=clean_event_type,
            amount_cents=event_amount,
            payload=expected_payload,
        )

        with self._transaction() as cursor:
            def finish(
                result: PaymentEventApplyResult,
            ) -> PaymentEventApplyResult:
                if transaction_effect is not None:
                    transaction_effect(cursor, result)
                return result

            cursor.execute(
                _SELECT_ORDER_BY_PROVIDER_FOR_UPDATE_SQL,
                (clean_provider, provider_order),
            )
            order = _fetchone_dict(cursor)
            if order is None:
                raise ProductPaymentOrderNotFound(
                    f"payment order not found: {clean_provider}/{provider_order}"
                )
            order = _decode_row(order)
            if str(order.get("owner_user_id") or "") != owner:
                raise ProductPaymentOrderNotFound(
                    f"payment order not found: {clean_provider}/{provider_order}"
                )

            cursor.execute(
                _SELECT_EVENT_FOR_UPDATE_SQL,
                (clean_provider, provider_event),
            )
            existing_event = _fetchone_dict(cursor)
            if existing_event is not None:
                existing_event = _decode_row(existing_event)
                _validate_event_replay(
                    existing_event,
                    owner_user_id=owner,
                    order_id=str(order["id"]),
                    provider=clean_provider,
                    provider_order_id=provider_order,
                    provider_event_id=provider_event,
                    event_kind=kind,
                    event_type=clean_event_type,
                    amount_cents=event_amount,
                    payload=expected_payload,
                    content_sha256=content_sha256,
                )
                _validate_replayed_event_integrity(
                    cursor,
                    event=existing_event,
                    order=order,
                )
                _enqueue_growth_event(
                    cursor,
                    event=existing_event,
                    order=order,
                )
                return finish(
                    PaymentEventApplyResult(
                        order=order,
                        event=existing_event,
                        account=None,
                        idempotent=True,
                        points_credited=0,
                        points_debited=0,
                    )
                )

            plan = _event_plan(
                order,
                event_kind=kind,
                amount_cents=event_amount,
                provider=clean_provider,
                provider_event_id=provider_event,
            )
            cursor.execute(
                _INSERT_EVENT_SQL,
                (
                    owner,
                    str(order["id"]),
                    clean_provider,
                    provider_order,
                    provider_event,
                    kind,
                    clean_event_type,
                    plan.target_status,
                    plan.amount_cents,
                    plan.points_delta,
                    plan.refunded_amount_cents_after,
                    plan.refunded_points_after,
                    plan.wallet_order_id,
                    payload_json,
                    content_sha256,
                ),
            )
            inserted_event = _fetchone_dict(cursor)
            if inserted_event is None:
                cursor.execute(
                    _SELECT_EVENT_FOR_UPDATE_SQL,
                    (clean_provider, provider_event),
                )
                raced_event = _fetchone_dict(cursor)
                if raced_event is not None:
                    raced_event = _decode_row(raced_event)
                    _validate_event_replay(
                        raced_event,
                        owner_user_id=owner,
                        order_id=str(order["id"]),
                        provider=clean_provider,
                        provider_order_id=provider_order,
                        provider_event_id=provider_event,
                        event_kind=kind,
                        event_type=clean_event_type,
                        amount_cents=event_amount,
                        payload=expected_payload,
                        content_sha256=content_sha256,
                    )
                    _validate_replayed_event_integrity(
                        cursor,
                        event=raced_event,
                        order=order,
                    )
                    _enqueue_growth_event(
                        cursor,
                        event=raced_event,
                        order=order,
                    )
                    return finish(
                        PaymentEventApplyResult(
                            order=order,
                            event=raced_event,
                            account=None,
                            idempotent=True,
                            points_credited=0,
                            points_debited=0,
                        )
                    )
                raise ProductPaymentEventConflict(
                    "payment event insert conflicted without a resolvable event: "
                    f"{clean_provider}/{provider_event}"
                )
            inserted_event = _decode_row(inserted_event)

            account: dict[str, Any] | None = None
            if plan.points_delta > 0:
                account = self._apply_wallet_credit(
                    cursor,
                    owner_user_id=owner,
                    payment_order=order,
                    provider_event_id=provider_event,
                    event_kind=kind,
                    wallet_order_id=_required_wallet_order_id(plan),
                    points=plan.points_delta,
                    request_sha256=content_sha256,
                )
            elif plan.points_delta < 0:
                account = self._apply_wallet_debit(
                    cursor,
                    owner_user_id=owner,
                    payment_order=order,
                    provider_event_id=provider_event,
                    event_kind=kind,
                    refund_amount_cents=int(plan.amount_cents or 0),
                    wallet_order_id=_required_wallet_order_id(plan),
                    points=-plan.points_delta,
                    request_sha256=content_sha256,
                )

            updated_order = self._apply_order_transition(
                cursor,
                order=order,
                event_kind=kind,
                plan=plan,
            )
            _enqueue_growth_event(
                cursor,
                event=inserted_event,
                order=updated_order,
            )
            return finish(
                PaymentEventApplyResult(
                    order=updated_order,
                    event=inserted_event,
                    account=account,
                    idempotent=False,
                    points_credited=max(plan.points_delta, 0),
                    points_debited=max(-plan.points_delta, 0),
                )
            )

    @staticmethod
    def _apply_wallet_credit(
        cursor: CursorLike,
        *,
        owner_user_id: str,
        payment_order: Mapping[str, Any],
        provider_event_id: str,
        event_kind: str,
        wallet_order_id: str,
        points: int,
        request_sha256: str,
    ) -> dict[str, Any]:
        account = _ensure_account_locked(cursor, owner_user_id)
        metadata_json = _wallet_metadata_json(
            payment_order=payment_order,
            provider_event_id=provider_event_id,
            event_kind=event_kind,
        )
        _insert_new_wallet_order(
            cursor,
            order_id=wallet_order_id,
            owner_user_id=owner_user_id,
            order_kind="credit",
            points=points,
            request_sha256=request_sha256,
            metadata_json=metadata_json,
        )
        cursor.execute(
            _CREDIT_POINT_ACCOUNT_SQL,
            (points, points, owner_user_id),
        )
        credited = _fetchone_dict(cursor)
        if credited is None:
            raise ProductPaymentWalletIntegrityError(
                f"payment credit account disappeared: {owner_user_id}"
            )
        credited = _decode_row(credited)
        cursor.execute(
            _INSERT_POINT_LEDGER_SQL,
            (
                owner_user_id,
                wallet_order_id,
                "credit",
                points,
                points,
                int(credited["balance_points"]),
                request_sha256,
                metadata_json,
            ),
        )
        return credited

    @staticmethod
    def _apply_wallet_debit(
        cursor: CursorLike,
        *,
        owner_user_id: str,
        payment_order: Mapping[str, Any],
        provider_event_id: str,
        event_kind: str,
        refund_amount_cents: int,
        wallet_order_id: str,
        points: int,
        request_sha256: str,
    ) -> dict[str, Any]:
        account = _ensure_account_locked(cursor, owner_user_id)
        available = int(account["balance_points"])
        if available < points:
            raise InsufficientPaymentRefundBalance(
                available_points=available,
                required_points=points,
            )
        metadata_json = _wallet_metadata_json(
            payment_order=payment_order,
            provider_event_id=provider_event_id,
            event_kind=event_kind,
            refund_amount_cents=refund_amount_cents,
        )
        _insert_new_wallet_order(
            cursor,
            order_id=wallet_order_id,
            owner_user_id=owner_user_id,
            order_kind="debit",
            points=points,
            request_sha256=request_sha256,
            metadata_json=metadata_json,
        )
        cursor.execute(
            _DEBIT_POINT_ACCOUNT_SQL,
            (points, points, owner_user_id, points),
        )
        debited = _fetchone_dict(cursor)
        if debited is None:
            raise InsufficientPaymentRefundBalance(
                available_points=available,
                required_points=points,
            )
        debited = _decode_row(debited)
        cursor.execute(
            _INSERT_POINT_LEDGER_SQL,
            (
                owner_user_id,
                wallet_order_id,
                "debit",
                points,
                -points,
                int(debited["balance_points"]),
                request_sha256,
                metadata_json,
            ),
        )
        return debited

    @staticmethod
    def _apply_order_transition(
        cursor: CursorLike,
        *,
        order: Mapping[str, Any],
        event_kind: str,
        plan: _EventPlan,
    ) -> dict[str, Any]:
        order_id = str(order["id"])
        owner = str(order["owner_user_id"])
        version = int(order["version"])
        current_status = str(order["status"])

        if event_kind == EVENT_PAYMENT_SUCCEEDED:
            if current_status == "paid":
                return dict(order)
            cursor.execute(
                _MARK_ORDER_PAID_SQL,
                (
                    _required_wallet_order_id(plan),
                    order_id,
                    owner,
                    version,
                ),
            )
        elif event_kind == EVENT_REFUND_SUCCEEDED:
            cursor.execute(
                _MARK_ORDER_REFUNDED_SQL,
                (
                    plan.target_status,
                    plan.refunded_amount_cents_after,
                    plan.refunded_points_after,
                    order_id,
                    owner,
                    version,
                ),
            )
        else:
            target_status = (
                "failed"
                if event_kind == EVENT_PAYMENT_FAILED
                else "closed"
            )
            if current_status == target_status:
                return dict(order)
            cursor.execute(
                _MARK_ORDER_TERMINAL_SQL,
                (target_status, order_id, owner, version),
            )

        updated = _fetchone_dict(cursor)
        if updated is None:
            raise ProductPaymentWalletIntegrityError(
                f"locked payment order transition was lost: {order_id}"
            )
        return _decode_row(updated)

    @contextmanager
    def _transaction(self) -> Iterator[CursorLike]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        except Exception:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def _event_plan(
    order: Mapping[str, Any],
    *,
    event_kind: str,
    amount_cents: int | None,
    provider: str,
    provider_event_id: str,
) -> _EventPlan:
    status = str(order.get("status") or "")
    order_amount = _positive_int(order.get("amount_cents"), "order.amount_cents")
    order_points = _positive_int(order.get("points"), "order.points")
    refunded_amount = _nonnegative_int(
        order.get("refunded_amount_cents"),
        "order.refunded_amount_cents",
    )
    refunded_points = _nonnegative_int(
        order.get("refunded_points"),
        "order.refunded_points",
    )

    if event_kind == EVENT_PAYMENT_SUCCEEDED:
        if amount_cents is None:
            raise InvalidProductPaymentInput(
                "amount_cents is required for a payment success event"
            )
        if amount_cents != order_amount:
            raise ProductPaymentAmountMismatch(
                expected_amount_cents=order_amount,
                actual_amount_cents=amount_cents,
            )
        if status == "pending":
            points_delta = order_points
        elif status == "paid":
            points_delta = 0
        else:
            raise ProductPaymentStateConflict(
                f"payment success is invalid from status {status!r}"
            )
        return _EventPlan(
            target_status="paid",
            amount_cents=amount_cents,
            points_delta=points_delta,
            refunded_amount_cents_after=0,
            refunded_points_after=0,
            wallet_order_id=(
                _wallet_order_id("credit", str(order["id"]))
                if points_delta
                else None
            ),
        )

    if event_kind == EVENT_REFUND_SUCCEEDED:
        if amount_cents is None:
            raise InvalidProductPaymentInput(
                "amount_cents is required for a refund success event"
            )
        if status not in {"paid", "partially_refunded"}:
            raise ProductPaymentStateConflict(
                f"payment refund is invalid from status {status!r}"
            )
        refunded_amount_after = refunded_amount + amount_cents
        if refunded_amount_after > order_amount:
            raise ProductPaymentAmountMismatch(
                expected_amount_cents=order_amount - refunded_amount,
                actual_amount_cents=amount_cents,
            )
        refunded_points_after = payment_rules.refund_points(
            refunded_amount_after,
            order_amount,
            order_points,
        )
        points_to_debit = refunded_points_after - refunded_points
        if points_to_debit < 0:
            raise ProductPaymentWalletIntegrityError(
                f"payment refund points moved backwards for order {order.get('id')}"
            )
        target_status = (
            "refunded"
            if refunded_amount_after == order_amount
            else "partially_refunded"
        )
        return _EventPlan(
            target_status=target_status,
            amount_cents=amount_cents,
            points_delta=-points_to_debit,
            refunded_amount_cents_after=refunded_amount_after,
            refunded_points_after=refunded_points_after,
            wallet_order_id=(
                _wallet_order_id(
                    "refund",
                    f"{provider}:{provider_event_id}",
                )
                if points_to_debit
                else None
            ),
        )

    if amount_cents is not None:
        raise InvalidProductPaymentInput(
            f"amount_cents is forbidden for {event_kind}"
        )
    target_status = (
        "failed" if event_kind == EVENT_PAYMENT_FAILED else "closed"
    )
    if status not in {"pending", target_status}:
        raise ProductPaymentStateConflict(
            f"{event_kind} is invalid from status {status!r}"
        )
    return _EventPlan(
        target_status=target_status,
        amount_cents=None,
        points_delta=0,
        refunded_amount_cents_after=0,
        refunded_points_after=0,
        wallet_order_id=None,
    )


def _ensure_account_locked(
    cursor: CursorLike,
    owner_user_id: str,
) -> dict[str, Any]:
    cursor.execute(_INSERT_POINT_ACCOUNT_SQL, (owner_user_id,))
    _fetchone_dict(cursor)
    cursor.execute(_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL, (owner_user_id,))
    account = _fetchone_dict(cursor)
    if account is None:
        raise ProductPaymentWalletIntegrityError(
            f"point account could not be locked: {owner_user_id}"
        )
    return _decode_row(account)


def _insert_new_wallet_order(
    cursor: CursorLike,
    *,
    order_id: str,
    owner_user_id: str,
    order_kind: str,
    points: int,
    request_sha256: str,
    metadata_json: str,
) -> dict[str, Any]:
    cursor.execute(
        _INSERT_POINT_ORDER_SQL,
        (
            order_id,
            owner_user_id,
            order_kind,
            points,
            request_sha256,
            metadata_json,
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_row(inserted)
    cursor.execute(_SELECT_POINT_ORDER_SQL, (order_id,))
    conflicting = _fetchone_dict(cursor)
    if conflicting is not None:
        raise ProductPaymentWalletIntegrityError(
            f"wallet order already exists before payment event application: {order_id}"
        )
    raise ProductPaymentWalletIntegrityError(
        f"wallet order insert failed without a conflicting row: {order_id}"
    )


def _server_catalog_snapshot(package_id: str) -> dict[str, Any]:
    clean_package_id = _provider_identifier(package_id, "package_id")
    try:
        snapshot = payment_catalog.package_snapshot(clean_package_id)
    except payment_catalog.PaymentCatalogError as exc:
        raise InvalidProductPaymentInput(str(exc)) from exc
    digest = snapshot.get("snapshotDigest")
    unsigned_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshotDigest"
    }
    expected_digest = payment_catalog.stable_snapshot_digest(unsigned_snapshot)
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest,
        expected_digest,
    ):
        raise InvalidProductPaymentInput(
            f"server payment catalog snapshot digest is invalid: {clean_package_id}"
        )
    return dict(snapshot)


def _validate_order_replay(
    order: Mapping[str, Any],
    *,
    owner_user_id: str,
    provider: str,
    idempotency_key: str,
    snapshot: Mapping[str, Any],
    snapshot_sha256: str,
    provider_payload: Mapping[str, Any] | None,
    expected_order_id: str | None,
    expected_provider_order_id: str | None,
) -> None:
    checks = (
        str(order.get("owner_user_id") or "") == owner_user_id,
        str(order.get("provider") or "") == provider,
        str(order.get("idempotency_key") or "") == idempotency_key,
        str(order.get("package_id") or "") == str(snapshot["packageId"]),
        str(order.get("catalog_version") or "")
        == str(snapshot["catalogVersion"]),
        str(order.get("currency") or "") == str(snapshot["currency"]),
        int(order.get("amount_cents") or 0) == int(snapshot["amountCents"]),
        int(order.get("points") or 0) == int(snapshot["points"]),
        hmac.compare_digest(
            str(order.get("catalog_snapshot_sha256") or ""),
            snapshot_sha256,
        ),
        (order.get("catalog_snapshot") or {}) == dict(snapshot),
        (
            provider_payload is None
            or (order.get("provider_payload") or {}) == dict(provider_payload)
        ),
        (
            expected_order_id is None
            or str(order.get("id") or "") == expected_order_id
        ),
        (
            expected_provider_order_id is None
            or str(order.get("provider_order_id") or "")
            == expected_provider_order_id
        ),
    )
    if not all(checks):
        raise ProductPaymentOrderConflict(
            "payment idempotency key belongs to a different frozen order request: "
            f"{order.get('id')}"
        )


def _validate_event_replay(
    event: Mapping[str, Any],
    *,
    owner_user_id: str,
    order_id: str,
    provider: str,
    provider_order_id: str,
    provider_event_id: str,
    event_kind: str,
    event_type: str,
    amount_cents: int | None,
    payload: Mapping[str, Any],
    content_sha256: str,
) -> None:
    persisted_amount = event.get("amount_cents")
    checks = (
        str(event.get("owner_user_id") or "") == owner_user_id,
        str(event.get("order_id") or "") == order_id,
        str(event.get("provider") or "") == provider,
        str(event.get("provider_order_id") or "") == provider_order_id,
        str(event.get("provider_event_id") or "") == provider_event_id,
        str(event.get("event_kind") or "") == event_kind,
        str(event.get("event_type") or "") == event_type,
        (
            persisted_amount is None
            if amount_cents is None
            else int(persisted_amount or 0) == amount_cents
        ),
        (event.get("payload") or {}) == dict(payload),
        hmac.compare_digest(
            str(event.get("content_sha256") or ""),
            content_sha256,
        ),
    )
    if not all(checks):
        raise ProductPaymentEventConflict(
            "provider event ID was replayed with different content: "
            f"{provider}/{provider_event_id}"
        )


def _validate_replayed_event_integrity(
    cursor: CursorLike,
    *,
    event: Mapping[str, Any],
    order: Mapping[str, Any],
) -> None:
    event_kind = str(event.get("event_kind") or "")
    target_status = str(event.get("target_status") or "")
    points_delta = int(event.get("points_delta") or 0)
    wallet_order_id = event.get("wallet_order_id")
    order_status = str(order.get("status") or "")

    if event_kind == EVENT_PAYMENT_SUCCEEDED:
        if (
            order_status not in {"paid", "partially_refunded", "refunded"}
            or int(order.get("credited_points") or 0)
            != int(order.get("points") or 0)
        ):
            raise ProductPaymentWalletIntegrityError(
                f"replayed payment success has no durable credit: {event.get('id')}"
            )
        if points_delta > 0 and str(order.get("credit_point_order_id") or "") != str(
            wallet_order_id or ""
        ):
            raise ProductPaymentWalletIntegrityError(
                f"replayed payment credit order drifted: {event.get('id')}"
            )
    elif event_kind == EVENT_REFUND_SUCCEEDED:
        if (
            order_status not in {"partially_refunded", "refunded"}
            or int(order.get("refunded_amount_cents") or 0)
            < int(event.get("refunded_amount_cents_after") or 0)
            or int(order.get("refunded_points") or 0)
            < int(event.get("refunded_points_after") or 0)
        ):
            raise ProductPaymentWalletIntegrityError(
                f"replayed payment refund has no durable cumulative state: {event.get('id')}"
            )
    elif order_status != target_status:
        raise ProductPaymentWalletIntegrityError(
            f"replayed terminal payment event state drifted: {event.get('id')}"
        )

    if points_delta == 0:
        if wallet_order_id is not None:
            raise ProductPaymentWalletIntegrityError(
                f"zero-delta payment event references a wallet order: {event.get('id')}"
            )
        return
    if not wallet_order_id:
        raise ProductPaymentWalletIntegrityError(
            f"payment event wallet order is missing: {event.get('id')}"
        )

    cursor.execute(_SELECT_POINT_ORDER_SQL, (str(wallet_order_id),))
    wallet_order = _fetchone_dict(cursor)
    if wallet_order is None:
        raise ProductPaymentWalletIntegrityError(
            f"payment event wallet order is missing: {wallet_order_id}"
        )
    wallet_order = _decode_row(wallet_order)
    expected_kind = "credit" if points_delta > 0 else "debit"
    expected_metadata = json.loads(
        _wallet_metadata_json(
            payment_order=order,
            provider_event_id=str(event["provider_event_id"]),
            event_kind=event_kind,
            refund_amount_cents=(
                int(event["amount_cents"])
                if event_kind == EVENT_REFUND_SUCCEEDED
                else None
            ),
        )
    )
    checks = (
        str(wallet_order.get("owner_user_id") or "")
        == str(order.get("owner_user_id") or ""),
        str(wallet_order.get("order_kind") or "") == expected_kind,
        int(wallet_order.get("points") or 0) == abs(points_delta),
        wallet_order.get("source_order_id") is None,
        wallet_order.get("source_order_kind") is None,
        wallet_order.get("job_id") is None,
        hmac.compare_digest(
            str(wallet_order.get("request_sha256") or ""),
            str(event.get("content_sha256") or ""),
        ),
        (wallet_order.get("metadata") or {}) == expected_metadata,
    )
    if not all(checks):
        raise ProductPaymentWalletIntegrityError(
            f"payment event wallet order content drifted: {wallet_order_id}"
        )


def _wallet_metadata_json(
    *,
    payment_order: Mapping[str, Any],
    provider_event_id: str,
    event_kind: str,
    refund_amount_cents: int | None = None,
) -> str:
    metadata: dict[str, Any] = {
        "catalogSnapshotSha256": str(
            payment_order["catalog_snapshot_sha256"]
        ),
        "eventKind": event_kind,
        "paymentOrderId": str(payment_order["id"]),
        "provider": str(payment_order["provider"]),
        "providerEventId": provider_event_id,
    }
    if refund_amount_cents is not None:
        metadata["refundAmountCents"] = refund_amount_cents
    return _json_object(metadata, "wallet_metadata")


def _enqueue_growth_event(
    cursor: CursorLike,
    *,
    event: Mapping[str, Any],
    order: Mapping[str, Any],
) -> None:
    event_kind = str(event.get("event_kind") or "")
    base_payload = {
        "schemaVersion": 1,
        "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
        "orderId": str(order["id"]),
        "customerId": str(order["owner_user_id"]),
        "provider": str(order["provider"]),
        "providerOrderId": str(order["provider_order_id"]),
        "paidCents": int(order["amount_cents"]),
        "points": int(order["points"]),
        "catalogSnapshotSha256": str(
            order["catalog_snapshot_sha256"]
        ),
    }
    if event_kind == EVENT_PAYMENT_SUCCEEDED:
        event_type = product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD
        dedupe_key = product_growth_outbox.stable_growth_dedupe_key(
            event_type,
            str(order["id"]),
        )
        payload = {
            **base_payload,
            "requestId": str(order["id"]),
        }
    elif event_kind == EVENT_REFUND_SUCCEEDED:
        event_type = product_growth_outbox.EVENT_PAYMENT_REFUND
        provider_event_id = str(event["provider_event_id"])
        dedupe_key = product_growth_outbox.stable_growth_dedupe_key(
            event_type,
            str(order["id"]),
            provider_event_id,
        )
        payload = {
            **base_payload,
            "providerEventId": provider_event_id,
            "requestId": provider_event_id,
            "refundCents": int(event["amount_cents"]),
            "cumulativeRefundedCents": int(
                event["refunded_amount_cents_after"]
            ),
            "cumulativeRefundedPoints": int(
                event["refunded_points_after"]
            ),
        }
    else:
        return
    product_growth_outbox.enqueue_growth_event(
        cursor,
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload,
    )


def _event_content_sha256(
    *,
    owner_user_id: str,
    provider: str,
    provider_order_id: str,
    provider_event_id: str,
    event_kind: str,
    event_type: str,
    amount_cents: int | None,
    payload: Mapping[str, Any],
) -> str:
    canonical = _json_object(
        {
            "amountCents": amount_cents,
            "eventKind": event_kind,
            "eventType": event_type,
            "ownerUserId": owner_user_id,
            "payload": dict(payload),
            "provider": provider,
            "providerEventId": provider_event_id,
            "providerOrderId": provider_order_id,
        },
        "event_content",
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _wallet_order_id(kind: str, source: str) -> str:
    digest = hashlib.sha256(f"{kind}:{source}".encode("utf-8")).hexdigest()
    return f"payment_{kind}_{digest[:48]}"


def _required_wallet_order_id(plan: _EventPlan) -> str:
    if not plan.wallet_order_id:
        raise ProductPaymentWalletIntegrityError(
            "payment event expected a wallet order ID"
        )
    return plan.wallet_order_id


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    names = [
        str(item[0] if isinstance(item, (tuple, list)) else getattr(item, "name", ""))
        for item in description
    ]
    if not names or len(names) != len(row):
        raise ProductPaymentStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(names, row))


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in ("catalog_snapshot", "provider_payload", "payload", "metadata"):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ProductPaymentStoreError(
                    f"database returned invalid JSON for {field}"
                ) from exc
            decoded[field] = parsed
    return decoded


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductPaymentInput(f"{field} must be a string")
    cleaned = value.strip()
    if not IDENTIFIER_RE.fullmatch(cleaned):
        raise InvalidProductPaymentInput(f"{field} is invalid")
    return cleaned


def _provider_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductPaymentInput(f"{field} must be a string")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in cleaned)
    ):
        raise InvalidProductPaymentInput(f"{field} is invalid")
    return cleaned


def _provider(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductPaymentInput("provider must be a string")
    cleaned = value.strip().lower()
    aliases = {
        "ali-pay": "alipay",
        "ali_pay": "alipay",
        "wechatpay": "wechat",
        "weixinpay": "wechat",
        "wxpay": "wechat",
    }
    cleaned = aliases.get(cleaned, cleaned)
    if cleaned not in SUPPORTED_PROVIDERS:
        raise InvalidProductPaymentInput(
            f"unsupported live payment provider: {cleaned or '<empty>'}"
        )
    return cleaned


def _event_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductPaymentInput("event_kind must be a string")
    cleaned = value.strip().lower()
    if cleaned not in SUPPORTED_EVENT_KINDS:
        raise InvalidProductPaymentInput(
            f"unsupported payment event kind: {cleaned or '<empty>'}"
        )
    return cleaned


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise InvalidProductPaymentInput(f"{field} must be a lowercase SHA-256")
    return value


def _json_object(value: Mapping[str, Any], field: str) -> str:
    if not isinstance(value, Mapping):
        raise InvalidProductPaymentInput(f"{field} must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidProductPaymentInput(
            f"{field} must contain JSON-compatible values"
        ) from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise InvalidProductPaymentInput(f"{field} must be an object")
    return encoded


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidProductPaymentInput(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProductPaymentWalletIntegrityError(
            f"{field} must be a non-negative integer"
        )
    return value
