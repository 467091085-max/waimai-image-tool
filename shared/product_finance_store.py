from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
MIN_WITHDRAWAL_AMOUNT_CENTS = 10_000
ACCOUNT_STATUSES = frozenset({"active", "suspended", "closed"})
COMMISSION_ORDER_CREATE_STATUSES = frozenset({"pending", "eligible"})
COMMISSION_ORDER_STATUSES = frozenset(
    {"pending", "eligible", "claimed", "settled", "canceled", "refunded"}
)
COMMISSION_ORDER_TRANSITIONS = {
    "pending": frozenset({"eligible"}),
    "eligible": frozenset(),
    "claimed": frozenset(),
    "settled": frozenset(),
    "canceled": frozenset(),
    "refunded": frozenset(),
}
SETTLEMENT_STATUSES = frozenset(
    {"pending", "processing", "paid", "failed", "canceled"}
)
SETTLEMENT_TRANSITIONS = {
    "pending": frozenset({"processing", "paid", "failed", "canceled"}),
    "processing": frozenset({"paid", "failed", "canceled"}),
    "paid": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
}
WITHDRAWAL_STATUSES = frozenset(
    {"pending", "approved", "rejected", "paid", "canceled"}
)
WITHDRAWAL_TRANSITIONS = {
    "pending": frozenset({"approved", "rejected", "canceled"}),
    "approved": frozenset({"paid", "rejected", "canceled"}),
    "rejected": frozenset(),
    "paid": frozenset(),
    "canceled": frozenset(),
}
AUDIT_DOMAINS = frozenset(
    {"points", "payment", "commission", "withdrawal", "risk"}
)
AUDIT_STATUSES = frozenset({"succeeded", "failed", "denied"})


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductFinanceStoreError(RuntimeError):
    pass


class InvalidProductFinanceInput(ProductFinanceStoreError, ValueError):
    pass


class ProductFinanceNotFound(ProductFinanceStoreError, LookupError):
    pass


class ProductFinanceConflict(ProductFinanceStoreError):
    pass


class ProductFinanceStateConflict(ProductFinanceConflict):
    pass


class ProductFinanceAuditConflict(ProductFinanceConflict):
    pass


class InsufficientWithdrawableBalance(ProductFinanceConflict):
    def __init__(
        self,
        *,
        agent_id: str,
        requested_cents: int,
        available_cents: int,
    ) -> None:
        super().__init__(
            "withdrawal amount exceeds available balance: "
            f"agent={agent_id}, requested={requested_cents}, "
            f"available={available_cents}"
        )
        self.agent_id = agent_id
        self.requested_cents = requested_cents
        self.available_cents = available_cents


@dataclass(frozen=True)
class FinanceCreateResult:
    record: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class FinanceMutationResult:
    record: dict[str, Any]
    idempotent: bool


_ACCOUNT_COLUMNS = """
    agent_id, currency, status, earned_cents, clawback_cents,
    reserved_cents, withdrawn_cents, version, metadata, created_at,
    updated_at
""".strip()

_QUALIFIED_ACCOUNT_COLUMNS = """
    a.agent_id, a.currency, a.status, a.earned_cents,
    a.clawback_cents, a.reserved_cents, a.withdrawn_cents,
    a.version, a.metadata, a.created_at, a.updated_at
""".strip()

_COMMISSION_ORDER_COLUMNS = """
    id, source_order_id, agent_id, customer_user_id,
    order_amount_cents, commission_amount_cents, commission_rate_bps,
    refunded_order_amount_cents, reversed_commission_cents, refund_status,
    currency, status, settlement_id, content_sha256, metadata,
    eligible_at, claimed_at, settled_at, last_refunded_at, created_at,
    updated_at
""".strip()

_QUALIFIED_COMMISSION_ORDER_COLUMNS = """
    o.id, o.source_order_id, o.agent_id, o.customer_user_id,
    o.order_amount_cents, o.commission_amount_cents,
    o.commission_rate_bps, o.refunded_order_amount_cents,
    o.reversed_commission_cents, o.refund_status, o.currency, o.status,
    o.settlement_id, o.content_sha256, o.metadata, o.eligible_at,
    o.claimed_at, o.settled_at, o.last_refunded_at, o.created_at,
    o.updated_at,
    i.order_amount_cents AS settlement_order_amount_cents,
    i.commission_amount_cents AS settlement_commission_amount_cents,
    i.original_commission_amount_cents
        AS settlement_original_commission_amount_cents,
    i.reversed_commission_amount_cents
        AS settlement_reversed_commission_amount_cents
""".strip()

_SETTLEMENT_COLUMNS = """
    id, agent_id, idempotency_key, request_sha256, settlement_no,
    total_order_amount_cents, total_commission_amount_cents, order_count,
    currency, status, settlement_account, failure_reason, metadata,
    version, created_at, updated_at, paid_at, released_at
""".strip()

_WITHDRAWAL_COLUMNS = """
    id, agent_id, idempotency_key, request_sha256, amount_cents,
    currency, status, account_snapshot, balance_snapshot, status_reason,
    metadata, version, created_at, updated_at, approved_at, rejected_at,
    paid_at, canceled_at
""".strip()

_AUDIT_COLUMNS = """
    action_id, actor_user_id, action_domain, action, target_type,
    target_id, status, reason, metadata, content_sha256, created_at
""".strip()

_COMMISSION_REFUND_COLUMNS = """
    id, commission_order_id, agent_id, source_order_id, idempotency_key,
    action_id, request_sha256, refund_amount_cents,
    requested_cumulative_refunded_amount_cents,
    cumulative_refunded_amount_cents, commission_reversal_delta_cents,
    cumulative_reversed_commission_cents,
    account_clawback_delta_cents, order_status_before,
    order_status_after, settlement_id, metadata, created_at
""".strip()

_INSERT_ACCOUNT_SQL = f"""
/* product_finance_store:insert_account */
INSERT INTO product_agent_finance_accounts (
    agent_id, currency, status, metadata
) VALUES (%s, %s, %s, %s::jsonb)
ON CONFLICT (agent_id) DO NOTHING
RETURNING {_ACCOUNT_COLUMNS}
"""

_SELECT_ACCOUNT_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_account_for_update */
SELECT {_ACCOUNT_COLUMNS}
FROM product_agent_finance_accounts
WHERE agent_id = %s
FOR UPDATE
"""

_SELECT_ACCOUNT_SQL = f"""
/* product_finance_store:select_account */
SELECT {_ACCOUNT_COLUMNS}
FROM product_agent_finance_accounts
WHERE agent_id = %s
"""

_INSERT_COMMISSION_ORDER_SQL = f"""
/* product_finance_store:insert_commission_order */
INSERT INTO product_commission_orders (
    id, source_order_id, agent_id, customer_user_id,
    order_amount_cents, commission_amount_cents, commission_rate_bps,
    currency, status, content_sha256, metadata, eligible_at
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s::jsonb,
    CASE WHEN %s = 'eligible' THEN CURRENT_TIMESTAMP ELSE NULL END
)
ON CONFLICT DO NOTHING
RETURNING {_COMMISSION_ORDER_COLUMNS}
"""

_SELECT_COMMISSION_ORDER_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_commission_order_for_update */
SELECT {_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders
WHERE id = %s
FOR UPDATE
"""

_SELECT_COMMISSION_ORDER_SQL = f"""
/* product_finance_store:select_commission_order */
SELECT {_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders
WHERE id = %s
"""

_SELECT_COMMISSION_ORDER_BY_SOURCE_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_commission_order_by_source_for_update */
SELECT {_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders
WHERE agent_id = %s AND source_order_id = %s
FOR UPDATE
"""

_LIST_COMMISSION_ORDERS_SQL = f"""
/* product_finance_store:list_commission_orders */
SELECT {_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders
WHERE (%s = '' OR agent_id = %s)
  AND (%s = '' OR status = %s)
ORDER BY created_at, id
LIMIT %s
"""

_MARK_COMMISSION_ORDER_ELIGIBLE_SQL = f"""
/* product_finance_store:mark_commission_order_eligible */
UPDATE product_commission_orders
SET status = 'eligible',
    eligible_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND agent_id = %s
  AND status = 'pending'
  AND settlement_id IS NULL
  AND commission_amount_cents > reversed_commission_cents
RETURNING {_COMMISSION_ORDER_COLUMNS}
"""

_LOCK_RELEASE_ACTION_SQL = """
/* product_finance_store:lock_release_action */
SELECT pg_advisory_xact_lock(hashtextextended(%s, 0)) AS locked
"""

_SELECT_RELEASE_ACCOUNTS_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_release_accounts_for_update */
SELECT {_QUALIFIED_ACCOUNT_COLUMNS}
FROM product_agent_finance_accounts AS a
WHERE a.status = 'active'
  AND (%s = '' OR a.agent_id = %s)
  AND EXISTS (
      SELECT 1
      FROM product_commission_orders AS o
      WHERE o.agent_id = a.agent_id
        AND o.status = 'pending'
        AND o.settlement_id IS NULL
        AND o.commission_amount_cents > o.reversed_commission_cents
        AND o.created_at <= (
            CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
        )
  )
ORDER BY a.agent_id
LIMIT %s
FOR UPDATE OF a
"""

_SELECT_PENDING_COMMISSION_ORDERS_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_pending_commission_orders_for_update */
SELECT {_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders
WHERE agent_id = ANY(%s)
  AND status = 'pending'
  AND settlement_id IS NULL
  AND commission_amount_cents > reversed_commission_cents
  AND created_at <= (
      CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
  )
ORDER BY created_at, id
LIMIT %s
FOR UPDATE SKIP LOCKED
"""

_MARK_SELECTED_COMMISSION_ORDERS_ELIGIBLE_SQL = f"""
/* product_finance_store:mark_selected_commission_orders_eligible */
UPDATE product_commission_orders
SET status = 'eligible',
    eligible_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = ANY(%s)
  AND status = 'pending'
  AND settlement_id IS NULL
  AND commission_amount_cents > reversed_commission_cents
  AND created_at <= (
      CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
  )
RETURNING {_COMMISSION_ORDER_COLUMNS}
"""

_APPLY_COMMISSION_ORDER_REFUND_SQL = f"""
/* product_finance_store:apply_commission_order_refund */
UPDATE product_commission_orders
SET refunded_order_amount_cents = %s,
    reversed_commission_cents = %s,
    refund_status = %s,
    status = %s,
    last_refunded_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND agent_id = %s
  AND status = %s
  AND refunded_order_amount_cents = %s
  AND reversed_commission_cents = %s
RETURNING {_COMMISSION_ORDER_COLUMNS}
"""

_SELECT_SETTLEMENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_settlement_by_idempotency_for_update */
SELECT {_SETTLEMENT_COLUMNS}
FROM product_commission_settlements
WHERE agent_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_SETTLEMENT_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_settlement_for_update */
SELECT {_SETTLEMENT_COLUMNS}
FROM product_commission_settlements
WHERE id = %s
FOR UPDATE
"""

_SELECT_SETTLEMENT_SQL = f"""
/* product_finance_store:select_settlement */
SELECT {_SETTLEMENT_COLUMNS}
FROM product_commission_settlements
WHERE id = %s
"""

_LIST_SETTLEMENTS_SQL = f"""
/* product_finance_store:list_settlements */
SELECT {_SETTLEMENT_COLUMNS}
FROM product_commission_settlements
WHERE (%s = '' OR agent_id = %s)
  AND (%s = '' OR status = %s)
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_SELECT_ORDERS_FOR_SETTLEMENT_SQL = f"""
/* product_finance_store:select_orders_for_settlement */
SELECT {_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders
WHERE id = ANY(%s)
ORDER BY id
FOR UPDATE
"""

_INSERT_SETTLEMENT_SQL = f"""
/* product_finance_store:insert_settlement */
INSERT INTO product_commission_settlements (
    id, agent_id, idempotency_key, request_sha256, settlement_no,
    total_order_amount_cents, total_commission_amount_cents, order_count,
    currency, settlement_account, metadata
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s::jsonb, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_SETTLEMENT_COLUMNS}
"""

_CLAIM_COMMISSION_ORDERS_SQL = """
/* product_finance_store:claim_commission_orders */
UPDATE product_commission_orders
SET status = 'claimed',
    settlement_id = %s,
    claimed_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE id = ANY(%s)
  AND agent_id = %s
  AND status = 'eligible'
  AND settlement_id IS NULL
  AND commission_amount_cents > reversed_commission_cents
RETURNING id
"""

_INSERT_SETTLEMENT_ITEM_SQL = """
/* product_finance_store:insert_settlement_item */
INSERT INTO product_commission_settlement_items (
    settlement_id, commission_order_id, source_order_id,
    order_amount_cents, commission_amount_cents,
    original_commission_amount_cents,
    reversed_commission_amount_cents, currency
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_SELECT_SETTLEMENT_ITEMS_SQL = """
/* product_finance_store:select_settlement_items */
SELECT
    settlement_id, commission_order_id, source_order_id,
    order_amount_cents, commission_amount_cents,
    original_commission_amount_cents,
    reversed_commission_amount_cents, currency, created_at
FROM product_commission_settlement_items
WHERE settlement_id = %s
ORDER BY commission_order_id
"""

_SELECT_SETTLEMENT_ORDERS_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_settlement_orders_for_update */
SELECT {_QUALIFIED_COMMISSION_ORDER_COLUMNS}
FROM product_commission_orders AS o
JOIN product_commission_settlement_items AS i
  ON i.commission_order_id = o.id
WHERE i.settlement_id = %s
ORDER BY o.id
FOR UPDATE OF o
"""

_MARK_SETTLEMENT_ORDERS_PAID_SQL = """
/* product_finance_store:mark_settlement_orders_paid */
UPDATE product_commission_orders
SET status = 'settled',
    settled_at = CURRENT_TIMESTAMP,
    updated_at = CURRENT_TIMESTAMP
WHERE settlement_id = %s
  AND status = 'claimed'
RETURNING id
"""

_RELEASE_SETTLEMENT_ORDERS_SQL = """
/* product_finance_store:release_settlement_orders */
UPDATE product_commission_orders
SET status = CASE
        WHEN reversed_commission_cents = commission_amount_cents
            THEN 'refunded'
        ELSE 'eligible'
    END,
    settlement_id = NULL,
    claimed_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE settlement_id = %s
  AND status = 'claimed'
RETURNING id
"""

_UPDATE_SETTLEMENT_STATUS_SQL = f"""
/* product_finance_store:update_settlement_status */
UPDATE product_commission_settlements
SET status = %s,
    failure_reason = %s,
    metadata = %s::jsonb,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP,
    paid_at = CASE
        WHEN %s = 'paid' THEN CURRENT_TIMESTAMP
        ELSE paid_at
    END,
    released_at = CASE
        WHEN %s IN ('failed', 'canceled') THEN CURRENT_TIMESTAMP
        ELSE released_at
    END
WHERE id = %s
  AND status = %s
  AND version = %s
RETURNING {_SETTLEMENT_COLUMNS}
"""

_CREDIT_AGENT_EARNINGS_SQL = f"""
/* product_finance_store:credit_agent_earnings */
UPDATE product_agent_finance_accounts
SET earned_cents = earned_cents + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE agent_id = %s
  AND currency = %s
  AND version = %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_INCREASE_AGENT_CLAWBACK_SQL = f"""
/* product_finance_store:increase_agent_clawback */
UPDATE product_agent_finance_accounts
SET clawback_cents = clawback_cents + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE agent_id = %s
  AND currency = %s
  AND version = %s
  AND clawback_cents + %s <= earned_cents
RETURNING {_ACCOUNT_COLUMNS}
"""

_SELECT_WITHDRAWAL_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_withdrawal_by_idempotency_for_update */
SELECT {_WITHDRAWAL_COLUMNS}
FROM product_agent_withdrawals
WHERE agent_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_WITHDRAWAL_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_withdrawal_for_update */
SELECT {_WITHDRAWAL_COLUMNS}
FROM product_agent_withdrawals
WHERE id = %s
FOR UPDATE
"""

_SELECT_WITHDRAWAL_SQL = f"""
/* product_finance_store:select_withdrawal */
SELECT {_WITHDRAWAL_COLUMNS}
FROM product_agent_withdrawals
WHERE id = %s
"""

_LIST_WITHDRAWALS_SQL = f"""
/* product_finance_store:list_withdrawals */
SELECT {_WITHDRAWAL_COLUMNS}
FROM product_agent_withdrawals
WHERE (%s = '' OR agent_id = %s)
  AND (%s = '' OR status = %s)
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_INSERT_WITHDRAWAL_SQL = f"""
/* product_finance_store:insert_withdrawal */
INSERT INTO product_agent_withdrawals (
    id, agent_id, idempotency_key, request_sha256, amount_cents,
    currency, account_snapshot, balance_snapshot, metadata
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s::jsonb, %s::jsonb, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_WITHDRAWAL_COLUMNS}
"""

_RESERVE_WITHDRAWAL_SQL = f"""
/* product_finance_store:reserve_withdrawal */
UPDATE product_agent_finance_accounts
SET reserved_cents = reserved_cents + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE agent_id = %s
  AND status = 'active'
  AND currency = %s
  AND version = %s
  AND earned_cents - clawback_cents
        - reserved_cents - withdrawn_cents >= %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_RELEASE_WITHDRAWAL_SQL = f"""
/* product_finance_store:release_withdrawal */
UPDATE product_agent_finance_accounts
SET reserved_cents = reserved_cents - %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE agent_id = %s
  AND currency = %s
  AND version = %s
  AND reserved_cents >= %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_PAY_WITHDRAWAL_SQL = f"""
/* product_finance_store:pay_withdrawal */
UPDATE product_agent_finance_accounts
SET reserved_cents = reserved_cents - %s,
    withdrawn_cents = withdrawn_cents + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE agent_id = %s
  AND currency = %s
  AND version = %s
  AND reserved_cents >= %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_UPDATE_WITHDRAWAL_STATUS_SQL = f"""
/* product_finance_store:update_withdrawal_status */
UPDATE product_agent_withdrawals
SET status = %s,
    status_reason = %s,
    metadata = %s::jsonb,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP,
    approved_at = CASE
        WHEN %s = 'approved' THEN COALESCE(approved_at, CURRENT_TIMESTAMP)
        ELSE approved_at
    END,
    rejected_at = CASE
        WHEN %s = 'rejected' THEN COALESCE(rejected_at, CURRENT_TIMESTAMP)
        ELSE rejected_at
    END,
    paid_at = CASE
        WHEN %s = 'paid' THEN COALESCE(paid_at, CURRENT_TIMESTAMP)
        ELSE paid_at
    END,
    canceled_at = CASE
        WHEN %s = 'canceled' THEN COALESCE(canceled_at, CURRENT_TIMESTAMP)
        ELSE canceled_at
    END
WHERE id = %s
  AND status = %s
  AND version = %s
RETURNING {_WITHDRAWAL_COLUMNS}
"""

_INSERT_LEDGER_SQL = """
/* product_finance_store:insert_ledger */
INSERT INTO product_agent_finance_ledger (
    id, agent_id, currency, entry_kind, source_type, source_id,
    earned_delta_cents, clawback_delta_cents, reserved_delta_cents,
    withdrawn_delta_cents,
    balance_after, content_sha256, metadata
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s::jsonb, %s, %s::jsonb
)
ON CONFLICT (entry_kind, source_type, source_id) DO NOTHING
RETURNING
    id, agent_id, currency, entry_kind, source_type, source_id,
    earned_delta_cents, clawback_delta_cents, reserved_delta_cents,
    withdrawn_delta_cents,
    balance_after, content_sha256, metadata, created_at
"""

_SELECT_LEDGER_BY_SOURCE_FOR_UPDATE_SQL = """
/* product_finance_store:select_ledger_by_source_for_update */
SELECT
    id, agent_id, currency, entry_kind, source_type, source_id,
    earned_delta_cents, clawback_delta_cents, reserved_delta_cents,
    withdrawn_delta_cents,
    balance_after, content_sha256, metadata, created_at
FROM product_agent_finance_ledger
WHERE entry_kind = %s AND source_type = %s AND source_id = %s
FOR UPDATE
"""

_INSERT_COMMISSION_REFUND_SQL = f"""
/* product_finance_store:insert_commission_refund */
INSERT INTO product_commission_refunds (
    id, commission_order_id, agent_id, source_order_id, idempotency_key,
    action_id, request_sha256, refund_amount_cents,
    requested_cumulative_refunded_amount_cents,
    cumulative_refunded_amount_cents, commission_reversal_delta_cents,
    cumulative_reversed_commission_cents,
    account_clawback_delta_cents, order_status_before,
    order_status_after, settlement_id, metadata
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s,
    %s, %s,
    %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_COMMISSION_REFUND_COLUMNS}
"""

_SELECT_COMMISSION_REFUND_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_commission_refund_for_update */
SELECT {_COMMISSION_REFUND_COLUMNS}
FROM product_commission_refunds
WHERE id = %s
FOR UPDATE
"""

_SELECT_COMMISSION_REFUND_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_commission_refund_by_idempotency_for_update */
SELECT {_COMMISSION_REFUND_COLUMNS}
FROM product_commission_refunds
WHERE agent_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_COMMISSION_REFUND_SQL = f"""
/* product_finance_store:select_commission_refund */
SELECT {_COMMISSION_REFUND_COLUMNS}
FROM product_commission_refunds
WHERE id = %s
"""

_LIST_COMMISSION_REFUNDS_SQL = f"""
/* product_finance_store:list_commission_refunds */
SELECT {_COMMISSION_REFUND_COLUMNS}
FROM product_commission_refunds
WHERE (%s = '' OR agent_id = %s)
  AND (%s = '' OR commission_order_id = %s)
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_INSERT_AUDIT_SQL = f"""
/* product_finance_store:insert_audit */
INSERT INTO product_finance_audit_events (
    action_id, actor_user_id, action_domain, action, target_type,
    target_id, status, reason, metadata, content_sha256
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s::jsonb, %s
)
ON CONFLICT (action_id) DO NOTHING
RETURNING {_AUDIT_COLUMNS}
"""

_SELECT_AUDIT_FOR_UPDATE_SQL = f"""
/* product_finance_store:select_audit_for_update */
SELECT {_AUDIT_COLUMNS}
FROM product_finance_audit_events
WHERE action_id = %s
FOR UPDATE
"""


def ensure_agent_finance_account(
    cursor: CursorLike,
    *,
    agent_id: str,
    currency: str = "CNY",
    status: str = "active",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceCreateResult:
    """Create the stable per-agent lock row using a caller-owned transaction."""

    agent = _identifier(agent_id, "agent_id")
    clean_currency = _currency(currency)
    clean_status = _choice(status, ACCOUNT_STATUSES, "account status")
    metadata_json = _json_object(metadata or {}, "metadata")
    cursor.execute(
        _INSERT_ACCOUNT_SQL,
        (agent, clean_currency, clean_status, metadata_json),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return FinanceCreateResult(record=_decode_row(inserted), created=True)

    cursor.execute(_SELECT_ACCOUNT_FOR_UPDATE_SQL, (agent,))
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductFinanceConflict(
            f"agent finance account insert conflicted without a row: {agent}"
        )
    existing = _decode_row(existing)
    if str(existing["currency"]) != clean_currency:
        raise ProductFinanceConflict(
            f"agent finance account currency is frozen: {agent}"
        )
    return FinanceCreateResult(record=existing, created=False)


def record_commission_order(
    cursor: CursorLike,
    *,
    commission_order_id: str,
    source_order_id: str,
    agent_id: str,
    customer_user_id: str,
    order_amount_cents: int,
    commission_amount_cents: int,
    commission_rate_bps: int,
    currency: str = "CNY",
    status: str = "eligible",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceCreateResult:
    """Record one immutable commission obligation in an existing transaction."""

    identifier = _identifier(commission_order_id, "commission_order_id")
    source = _identifier(source_order_id, "source_order_id")
    agent = _identifier(agent_id, "agent_id")
    customer = _optional_identifier(customer_user_id, "customer_user_id")
    order_amount = _positive_int(order_amount_cents, "order_amount_cents")
    commission_amount = _positive_int(
        commission_amount_cents,
        "commission_amount_cents",
    )
    if commission_amount > order_amount:
        raise InvalidProductFinanceInput(
            "commission_amount_cents must not exceed order_amount_cents"
        )
    rate_bps = _bounded_positive_int(
        commission_rate_bps,
        "commission_rate_bps",
        maximum=10_000,
    )
    clean_currency = _currency(currency)
    clean_status = _choice(
        status,
        COMMISSION_ORDER_CREATE_STATUSES,
        "commission order status",
    )
    metadata_object = _json_object_value(metadata or {}, "metadata")
    content_sha256 = _content_sha256(
        {
            "id": identifier,
            "sourceOrderId": source,
            "agentId": agent,
            "customerUserId": customer,
            "orderAmountCents": order_amount,
            "commissionAmountCents": commission_amount,
            "commissionRateBps": rate_bps,
            "currency": clean_currency,
            "status": clean_status,
            "metadata": metadata_object,
        }
    )

    account = _locked_account(cursor, agent)
    if str(account["status"]) != "active":
        raise ProductFinanceStateConflict(
            f"agent finance account is not active: {agent}"
        )
    if str(account["currency"]) != clean_currency:
        raise ProductFinanceConflict(
            f"commission currency does not match agent account: {agent}"
        )

    cursor.execute(
        _INSERT_COMMISSION_ORDER_SQL,
        (
            identifier,
            source,
            agent,
            customer,
            order_amount,
            commission_amount,
            rate_bps,
            clean_currency,
            clean_status,
            content_sha256,
            _canonical_json(metadata_object),
            clean_status,
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return FinanceCreateResult(record=_decode_row(inserted), created=True)

    cursor.execute(_SELECT_COMMISSION_ORDER_FOR_UPDATE_SQL, (identifier,))
    existing = _fetchone_dict(cursor)
    if existing is None:
        cursor.execute(
            _SELECT_COMMISSION_ORDER_BY_SOURCE_FOR_UPDATE_SQL,
            (agent, source),
        )
        conflicting = _fetchone_dict(cursor)
        if conflicting is not None:
            raise ProductFinanceConflict(
                "source order already belongs to another commission order: "
                f"{agent}/{source}"
            )
        raise ProductFinanceConflict(
            "commission order insert conflicted without a resolvable row: "
            f"{identifier}"
        )
    existing = _decode_row(existing)
    _require_digest_match(
        existing.get("content_sha256"),
        content_sha256,
        "commission order",
        identifier,
    )
    return FinanceCreateResult(record=existing, created=False)


def transition_commission_order(
    cursor: CursorLike,
    *,
    commission_order_id: str,
    target_status: str,
    action_id: str,
    actor_user_id: str,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceMutationResult:
    """Advance one commission order while holding account then order locks."""

    identifier = _identifier(
        commission_order_id,
        "commission_order_id",
    )
    target = _choice(
        target_status,
        COMMISSION_ORDER_STATUSES,
        "commission order status",
    )
    action_identifier = _identifier(action_id, "action_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    request_metadata = _json_object_value(metadata or {}, "metadata")
    audit_action = f"commission_order.status.{target}"
    audit_metadata = {
        "targetStatus": target,
        "metadata": request_metadata,
    }
    audit_digest = _audit_content_sha256(
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action=audit_action,
        target_type="commission_order",
        target_id=identifier,
        status="succeeded",
        reason=str(reason or ""),
        metadata=audit_metadata,
    )

    cursor.execute(_SELECT_COMMISSION_ORDER_SQL, (identifier,))
    snapshot_row = _fetchone_dict(cursor)
    if snapshot_row is None:
        raise ProductFinanceNotFound(
            f"commission order not found: {identifier}"
        )
    snapshot = _decode_row(snapshot_row)
    agent = str(snapshot["agent_id"])
    _locked_account(cursor, agent)
    cursor.execute(_SELECT_COMMISSION_ORDER_FOR_UPDATE_SQL, (identifier,))
    locked_row = _fetchone_dict(cursor)
    if locked_row is None:
        raise ProductFinanceNotFound(
            f"commission order not found: {identifier}"
        )
    order = _decode_row(locked_row)
    if str(order["agent_id"]) != agent:
        raise ProductFinanceConflict(
            f"commission order owner changed while locking: {identifier}"
        )

    replayed_audit = _audit_by_action_id(cursor, action_identifier)
    if replayed_audit is not None:
        _require_digest_match(
            replayed_audit.get("content_sha256"),
            audit_digest,
            "finance audit action",
            action_identifier,
            audit_conflict=True,
        )
        return FinanceMutationResult(record=order, idempotent=True)

    current = str(order["status"])
    if target not in COMMISSION_ORDER_TRANSITIONS[current]:
        raise ProductFinanceStateConflict(
            f"invalid commission order status transition: "
            f"{current} -> {target}"
        )
    cursor.execute(
        _MARK_COMMISSION_ORDER_ELIGIBLE_SQL,
        (identifier, agent),
    )
    updated = _fetchone_dict(cursor)
    if updated is None:
        raise ProductFinanceConflict(
            f"commission order transition lost its row lock: {identifier}"
        )
    record_finance_audit(
        cursor,
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action=audit_action,
        target_type="commission_order",
        target_id=identifier,
        status="succeeded",
        reason=reason,
        metadata=audit_metadata,
    )
    return FinanceMutationResult(
        record=_decode_row(updated),
        idempotent=False,
    )


def release_eligible_commissions(
    cursor: CursorLike,
    *,
    action_id: str,
    actor_user_id: str,
    agent_id: str = "",
    min_age_days: int = 7,
    limit: int = 500,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceMutationResult:
    """Release an auditable, server-timed, bounded T+N commission batch."""

    action_identifier = _identifier(action_id, "action_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    agent = (
        _identifier(agent_id, "agent_id")
        if str(agent_id or "").strip()
        else ""
    )
    age_days = _nonnegative_int(min_age_days, "min_age_days")
    if age_days > 3650:
        raise InvalidProductFinanceInput(
            "min_age_days must not exceed 3650"
        )
    clean_limit = _bounded_positive_int(
        limit,
        "limit",
        maximum=1000,
    )
    request_metadata = _json_object_value(metadata or {}, "metadata")
    clean_reason = str(reason or "")
    target_id = agent or "all-agents"
    request = {
        "agentId": agent,
        "minAgeDays": age_days,
        "limit": clean_limit,
        "reason": clean_reason,
        "metadata": request_metadata,
        "cutoffSource": "database_current_timestamp",
    }
    request_sha256 = _content_sha256(request)

    cursor.execute(_LOCK_RELEASE_ACTION_SQL, (action_identifier,))
    if _fetchone_dict(cursor) is None:
        raise ProductFinanceConflict(
            f"commission release action lock failed: {action_identifier}"
        )
    replayed_audit = _audit_by_action_id(cursor, action_identifier)
    if replayed_audit is not None:
        result = _release_result_from_audit(
            replayed_audit,
            action_id=action_identifier,
            actor_user_id=actor,
            target_id=target_id,
            request_sha256=request_sha256,
        )
        return FinanceMutationResult(record=result, idempotent=True)

    cursor.execute(
        _SELECT_RELEASE_ACCOUNTS_FOR_UPDATE_SQL,
        (agent, agent, age_days, clean_limit),
    )
    accounts = [_decode_row(row) for row in _fetchall_dicts(cursor)]
    account_ids = [str(row["agent_id"]) for row in accounts]
    candidates: list[dict[str, Any]] = []
    if account_ids:
        cursor.execute(
            _SELECT_PENDING_COMMISSION_ORDERS_FOR_UPDATE_SQL,
            (account_ids, age_days, clean_limit),
        )
        candidates = [
            _decode_row(row) for row in _fetchall_dicts(cursor)
        ]

    released: list[dict[str, Any]] = []
    if candidates:
        candidate_ids = [str(row["id"]) for row in candidates]
        cursor.execute(
            _MARK_SELECTED_COMMISSION_ORDERS_ELIGIBLE_SQL,
            (candidate_ids, age_days),
        )
        released = [
            _decode_row(row) for row in _fetchall_dicts(cursor)
        ]
        released_ids = {str(row["id"]) for row in released}
        if (
            cursor.rowcount != len(candidate_ids)
            or released_ids != set(candidate_ids)
        ):
            raise ProductFinanceConflict(
                "commission release count changed inside the transaction: "
                f"expected={len(candidate_ids)}, actual={cursor.rowcount}"
            )

    result = {
        "released": len(released),
        "commissionAmountCents": sum(
            int(row["commission_amount_cents"])
            - int(row.get("reversed_commission_cents") or 0)
            for row in released
        ),
        "orderIds": [str(row["id"]) for row in released],
        "agentId": agent,
        "minAgeDays": age_days,
        "limit": clean_limit,
        "cutoffSource": "database_current_timestamp",
    }
    audit_metadata = {
        "requestSha256": request_sha256,
        "request": request,
        "result": result,
    }
    record_finance_audit(
        cursor,
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action="commission_order.release_eligible",
        target_type="commission_release_batch",
        target_id=target_id,
        status="succeeded",
        reason=clean_reason,
        metadata=audit_metadata,
    )
    return FinanceMutationResult(record=result, idempotent=False)


def apply_commission_refund(
    cursor: CursorLike,
    *,
    refund_id: str,
    commission_order_id: str,
    refund_amount_cents: int,
    action_id: str,
    actor_user_id: str,
    idempotency_key: str = "",
    cumulative_refunded_amount_cents: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> FinanceMutationResult:
    """Apply one bounded refund adjustment and any settled clawback."""

    identifier = _identifier(refund_id, "refund_id")
    order_identifier = _identifier(
        commission_order_id,
        "commission_order_id",
    )
    refund_amount = _positive_int(
        refund_amount_cents,
        "refund_amount_cents",
    )
    action_identifier = _identifier(action_id, "action_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    idem_key = _identifier(
        idempotency_key or identifier,
        "idempotency_key",
    )
    requested_cumulative = (
        None
        if cumulative_refunded_amount_cents is None
        else _positive_int(
            cumulative_refunded_amount_cents,
            "cumulative_refunded_amount_cents",
        )
    )
    metadata_object = _json_object_value(metadata or {}, "metadata")
    request_content = {
        "refundId": identifier,
        "commissionOrderId": order_identifier,
        "refundAmountCents": refund_amount,
        "cumulativeRefundedAmountCents": requested_cumulative,
        "actionId": action_identifier,
        "actorUserId": actor,
        "idempotencyKey": idem_key,
        "metadata": metadata_object,
    }
    request_sha256 = _content_sha256(request_content)
    audit_metadata = {
        "refundId": identifier,
        "refundAmountCents": refund_amount,
        "cumulativeRefundedAmountCents": requested_cumulative,
        "idempotencyKey": idem_key,
        "metadata": metadata_object,
    }
    audit_digest = _audit_content_sha256(
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action="commission.refund.apply",
        target_type="commission_order",
        target_id=order_identifier,
        status="succeeded",
        reason="",
        metadata=audit_metadata,
    )

    cursor.execute(_SELECT_COMMISSION_ORDER_SQL, (order_identifier,))
    snapshot_row = _fetchone_dict(cursor)
    if snapshot_row is None:
        raise ProductFinanceNotFound(
            f"commission order not found: {order_identifier}"
        )
    snapshot = _decode_row(snapshot_row)
    agent = str(snapshot["agent_id"])
    account = _locked_account(cursor, agent)
    cursor.execute(
        _SELECT_COMMISSION_ORDER_FOR_UPDATE_SQL,
        (order_identifier,),
    )
    locked_row = _fetchone_dict(cursor)
    if locked_row is None:
        raise ProductFinanceNotFound(
            f"commission order not found: {order_identifier}"
        )
    order = _decode_row(locked_row)
    if str(order["agent_id"]) != agent:
        raise ProductFinanceConflict(
            f"commission order owner changed while locking: {order_identifier}"
        )

    existing = _commission_refund_by_idempotency(
        cursor,
        agent,
        idem_key,
    )
    if existing is not None:
        _validate_create_replay(
            existing,
            expected_id=identifier,
            expected_digest=request_sha256,
            label="commission refund",
        )
        if str(existing.get("action_id") or "") != action_identifier:
            raise ProductFinanceConflict(
                "commission refund idempotency key belongs to another "
                f"audit action: {idem_key}"
            )
        replayed_audit = _audit_by_action_id(cursor, action_identifier)
        if replayed_audit is None:
            raise ProductFinanceConflict(
                f"commission refund has no immutable audit: {identifier}"
            )
        _require_digest_match(
            replayed_audit.get("content_sha256"),
            audit_digest,
            "finance audit action",
            action_identifier,
            audit_conflict=True,
        )
        return FinanceMutationResult(record=existing, idempotent=True)

    replayed_audit = _audit_by_action_id(cursor, action_identifier)
    if replayed_audit is not None:
        _require_digest_match(
            replayed_audit.get("content_sha256"),
            audit_digest,
            "finance audit action",
            action_identifier,
            audit_conflict=True,
        )
        raise ProductFinanceConflict(
            f"commission refund audit exists without its adjustment: "
            f"{action_identifier}"
        )

    order_amount = int(order["order_amount_cents"])
    original_commission = int(order["commission_amount_cents"])
    previous_refunded = int(
        order.get("refunded_order_amount_cents") or 0
    )
    previous_reversed = int(
        order.get("reversed_commission_cents") or 0
    )
    if refund_amount > order_amount:
        raise InvalidProductFinanceInput(
            "refund_amount_cents must not exceed the original order amount"
        )
    if requested_cumulative is None:
        cumulative_refunded = previous_refunded + refund_amount
        if cumulative_refunded > order_amount:
            raise ProductFinanceConflict(
                "commission refund would exceed the original order amount: "
                f"order={order_identifier}"
            )
    else:
        if requested_cumulative > order_amount:
            raise ProductFinanceConflict(
                "cumulative commission refund exceeds the original "
                f"order amount: order={order_identifier}"
            )
        cumulative_refunded = max(
            previous_refunded,
            requested_cumulative,
        )

    net_order_amount = order_amount - cumulative_refunded
    net_commission = (
        original_commission * net_order_amount // order_amount
    )
    cumulative_reversed = original_commission - net_commission
    if cumulative_reversed < previous_reversed:
        raise ProductFinanceConflict(
            f"commission reversal moved backwards: {order_identifier}"
        )
    reversal_delta = cumulative_reversed - previous_reversed
    refund_status = (
        "full"
        if cumulative_reversed == original_commission
        else "partial"
    )
    status_before = str(order["status"])
    status_after = (
        "refunded"
        if (
            cumulative_reversed == original_commission
            and status_before in {"pending", "eligible"}
        )
        else status_before
    )
    account_clawback_delta = (
        reversal_delta if status_before == "settled" else 0
    )
    settlement_id = str(order.get("settlement_id") or "") or None

    cursor.execute(
        _INSERT_COMMISSION_REFUND_SQL,
        (
            identifier,
            order_identifier,
            agent,
            str(order["source_order_id"]),
            idem_key,
            action_identifier,
            request_sha256,
            refund_amount,
            requested_cumulative,
            cumulative_refunded,
            reversal_delta,
            cumulative_reversed,
            account_clawback_delta,
            status_before,
            status_after,
            settlement_id,
            _canonical_json(metadata_object),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is None:
        raced = _commission_refund_by_idempotency(
            cursor,
            agent,
            idem_key,
        )
        if raced is not None:
            _validate_create_replay(
                raced,
                expected_id=identifier,
                expected_digest=request_sha256,
                label="commission refund",
            )
            return FinanceMutationResult(record=raced, idempotent=True)
        cursor.execute(
            _SELECT_COMMISSION_REFUND_FOR_UPDATE_SQL,
            (identifier,),
        )
        if _fetchone_dict(cursor) is not None:
            raise ProductFinanceConflict(
                f"commission refund ID belongs to another request: "
                f"{identifier}"
            )
        raise ProductFinanceConflict(
            "commission refund insert conflicted without a resolvable row: "
            f"{identifier}"
        )

    cursor.execute(
        _APPLY_COMMISSION_ORDER_REFUND_SQL,
        (
            cumulative_refunded,
            cumulative_reversed,
            refund_status,
            status_after,
            order_identifier,
            agent,
            status_before,
            previous_refunded,
            previous_reversed,
        ),
    )
    updated_order_row = _fetchone_dict(cursor)
    if updated_order_row is None:
        raise ProductFinanceConflict(
            f"commission refund lost its order row lock: {order_identifier}"
        )
    updated_order = _decode_row(updated_order_row)

    updated_account: dict[str, Any] | None = None
    if account_clawback_delta:
        cursor.execute(
            _INCREASE_AGENT_CLAWBACK_SQL,
            (
                account_clawback_delta,
                agent,
                str(order["currency"]),
                int(account["version"]),
                account_clawback_delta,
            ),
        )
        updated_account_row = _fetchone_dict(cursor)
        if updated_account_row is None:
            raise ProductFinanceConflict(
                "settled commission clawback exceeds credited earnings: "
                f"{order_identifier}"
            )
        updated_account = _decode_row(updated_account_row)
        _insert_finance_ledger(
            cursor,
            agent_id=agent,
            currency=str(order["currency"]),
            entry_kind="commission_clawback",
            source_type="commission_refund",
            source_id=identifier,
            earned_delta_cents=0,
            reserved_delta_cents=0,
            withdrawn_delta_cents=0,
            clawback_delta_cents=account_clawback_delta,
            balance_after=withdrawable_balance(updated_account),
            metadata={
                "commissionOrderId": order_identifier,
                "refundId": identifier,
                "cumulativeReversedCommissionCents": cumulative_reversed,
            },
        )

    record_finance_audit(
        cursor,
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action="commission.refund.apply",
        target_type="commission_order",
        target_id=order_identifier,
        status="succeeded",
        metadata=audit_metadata,
    )
    record = _decode_row(inserted)
    record["commission_order"] = updated_order
    if updated_account is not None:
        record["finance_account"] = updated_account
    return FinanceMutationResult(record=record, idempotent=False)


def create_commission_settlement(
    cursor: CursorLike,
    *,
    settlement_id: str,
    agent_id: str,
    idempotency_key: str,
    commission_order_ids: Sequence[str],
    settlement_account: Mapping[str, Any],
    settlement_no: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceCreateResult:
    """Atomically claim every requested commission order or claim none."""

    identifier = _identifier(settlement_id, "settlement_id")
    agent = _identifier(agent_id, "agent_id")
    idem_key = _identifier(idempotency_key, "idempotency_key")
    order_ids = _unique_identifiers(
        commission_order_ids,
        "commission_order_ids",
    )
    account_snapshot = _nonempty_json_object(
        settlement_account,
        "settlement_account",
    )
    metadata_object = _json_object_value(metadata or {}, "metadata")
    clean_settlement_no = (
        _settlement_number(settlement_no)
        if str(settlement_no or "").strip()
        else f"SET-{hashlib.sha256(identifier.encode()).hexdigest()[:20]}"
    )
    request_sha256 = _content_sha256(
        {
            "agentId": agent,
            "commissionOrderIds": sorted(order_ids),
            "settlementAccount": account_snapshot,
            "settlementNo": clean_settlement_no,
            "metadata": metadata_object,
        }
    )

    existing = _settlement_by_idempotency(cursor, agent, idem_key)
    if existing is not None:
        _validate_create_replay(
            existing,
            expected_id=identifier,
            expected_digest=request_sha256,
            label="commission settlement",
        )
        return FinanceCreateResult(record=existing, created=False)

    account = _locked_account(cursor, agent)
    if str(account["status"]) != "active":
        raise ProductFinanceStateConflict(
            f"agent finance account is not active: {agent}"
        )

    existing = _settlement_by_idempotency(cursor, agent, idem_key)
    if existing is not None:
        _validate_create_replay(
            existing,
            expected_id=identifier,
            expected_digest=request_sha256,
            label="commission settlement",
        )
        return FinanceCreateResult(record=existing, created=False)

    cursor.execute(_SELECT_ORDERS_FOR_SETTLEMENT_SQL, (order_ids,))
    orders = [_decode_row(row) for row in _fetchall_dicts(cursor)]
    found_ids = {str(row["id"]) for row in orders}
    if found_ids != set(order_ids):
        missing = sorted(set(order_ids) - found_ids)
        raise ProductFinanceNotFound(
            f"commission orders not found: {','.join(missing)}"
        )
    invalid = [
        str(row["id"])
        for row in orders
        if str(row["agent_id"]) != agent
        or str(row["status"]) != "eligible"
        or row.get("settlement_id") is not None
        or int(row["commission_amount_cents"])
            <= int(row.get("reversed_commission_cents") or 0)
    ]
    if invalid:
        raise ProductFinanceConflict(
            "commission orders are not eligible for settlement: "
            f"{','.join(sorted(invalid))}"
        )
    currencies = {str(row["currency"]) for row in orders}
    if currencies != {str(account["currency"])}:
        raise ProductFinanceConflict(
            f"commission settlement has mixed or mismatched currencies: {agent}"
        )

    order_amount = sum(int(row["order_amount_cents"]) for row in orders)
    commission_amount = sum(
        int(row["commission_amount_cents"])
        - int(row.get("reversed_commission_cents") or 0)
        for row in orders
    )
    currency = str(account["currency"])
    cursor.execute(
        _INSERT_SETTLEMENT_SQL,
        (
            identifier,
            agent,
            idem_key,
            request_sha256,
            clean_settlement_no,
            order_amount,
            commission_amount,
            len(orders),
            currency,
            _canonical_json(account_snapshot),
            _canonical_json(metadata_object),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is None:
        raced = _settlement_by_idempotency(cursor, agent, idem_key)
        if raced is not None:
            _validate_create_replay(
                raced,
                expected_id=identifier,
                expected_digest=request_sha256,
                label="commission settlement",
            )
            return FinanceCreateResult(record=raced, created=False)
        cursor.execute(_SELECT_SETTLEMENT_FOR_UPDATE_SQL, (identifier,))
        if _fetchone_dict(cursor) is not None:
            raise ProductFinanceConflict(
                f"settlement ID already belongs to another request: {identifier}"
            )
        raise ProductFinanceConflict(
            "settlement insert conflicted without a resolvable row: "
            f"{identifier}"
        )

    cursor.execute(
        _CLAIM_COMMISSION_ORDERS_SQL,
        (identifier, order_ids, agent),
    )
    claimed = _fetchall_dicts(cursor)
    claimed_ids = {str(row["id"]) for row in claimed}
    if cursor.rowcount != len(order_ids) or claimed_ids != set(order_ids):
        raise ProductFinanceConflict(
            "commission order claim count changed inside the transaction: "
            f"expected={len(order_ids)}, actual={cursor.rowcount}"
        )

    for row in orders:
        cursor.execute(
            _INSERT_SETTLEMENT_ITEM_SQL,
            (
                identifier,
                str(row["id"]),
                str(row["source_order_id"]),
                int(row["order_amount_cents"]),
                int(row["commission_amount_cents"])
                - int(row.get("reversed_commission_cents") or 0),
                int(row["commission_amount_cents"]),
                int(row.get("reversed_commission_cents") or 0),
                str(row["currency"]),
            ),
        )
    return FinanceCreateResult(record=_decode_row(inserted), created=True)


def transition_commission_settlement(
    cursor: CursorLike,
    *,
    settlement_id: str,
    target_status: str,
    action_id: str,
    actor_user_id: str,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceMutationResult:
    """Move a settlement with exact order-count checks and durable audit."""

    identifier = _identifier(settlement_id, "settlement_id")
    target = _choice(
        target_status,
        SETTLEMENT_STATUSES,
        "settlement status",
    )
    action_identifier = _identifier(action_id, "action_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    request_metadata = _json_object_value(metadata or {}, "metadata")
    audit_action = f"settlement.status.{target}"
    audit_metadata = {"targetStatus": target, "metadata": request_metadata}
    audit_digest = _audit_content_sha256(
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action=audit_action,
        target_type="commission_settlement",
        target_id=identifier,
        status="succeeded",
        reason=str(reason or ""),
        metadata=audit_metadata,
    )

    cursor.execute(_SELECT_SETTLEMENT_FOR_UPDATE_SQL, (identifier,))
    settlement_row = _fetchone_dict(cursor)
    if settlement_row is None:
        raise ProductFinanceNotFound(
            f"commission settlement not found: {identifier}"
        )
    settlement = _decode_row(settlement_row)
    replayed_audit = _audit_by_action_id(cursor, action_identifier)
    if replayed_audit is not None:
        _require_digest_match(
            replayed_audit.get("content_sha256"),
            audit_digest,
            "finance audit action",
            action_identifier,
            audit_conflict=True,
        )
        return FinanceMutationResult(record=settlement, idempotent=True)

    current = str(settlement["status"])
    if target not in SETTLEMENT_TRANSITIONS[current]:
        raise ProductFinanceStateConflict(
            f"invalid settlement status transition: {current} -> {target}"
        )

    account = _locked_account(cursor, str(settlement["agent_id"]))
    cursor.execute(_SELECT_SETTLEMENT_ORDERS_FOR_UPDATE_SQL, (identifier,))
    orders = [_decode_row(row) for row in _fetchall_dicts(cursor)]
    _validate_frozen_settlement_orders(settlement, orders)
    updated_account: dict[str, Any] | None = None

    if target == "paid":
        post_claim_clawback = sum(
            int(row.get("reversed_commission_cents") or 0)
            - int(
                row.get(
                    "settlement_reversed_commission_amount_cents"
                )
                or 0
            )
            for row in orders
        )
        if post_claim_clawback < 0:
            raise ProductFinanceConflict(
                "settlement commission reversal moved behind its frozen "
                f"snapshot: {identifier}"
            )
        cursor.execute(_MARK_SETTLEMENT_ORDERS_PAID_SQL, (identifier,))
        settled_rows = _fetchall_dicts(cursor)
        if (
            cursor.rowcount != int(settlement["order_count"])
            or len(settled_rows) != int(settlement["order_count"])
        ):
            raise ProductFinanceConflict(
                "settlement order completion count changed inside the transaction"
            )
        cursor.execute(
            _CREDIT_AGENT_EARNINGS_SQL,
            (
                int(settlement["total_commission_amount_cents"]),
                str(settlement["agent_id"]),
                str(settlement["currency"]),
                int(account["version"]),
            ),
        )
        updated_row = _fetchone_dict(cursor)
        if updated_row is None:
            raise ProductFinanceConflict(
                f"agent earnings credit lost its row lock: {settlement['agent_id']}"
            )
        updated_account = _decode_row(updated_row)
        _insert_finance_ledger(
            cursor,
            agent_id=str(settlement["agent_id"]),
            currency=str(settlement["currency"]),
            entry_kind="commission_credit",
            source_type="settlement",
            source_id=identifier,
            earned_delta_cents=int(
                settlement["total_commission_amount_cents"]
            ),
            reserved_delta_cents=0,
            withdrawn_delta_cents=0,
            balance_after=withdrawable_balance(updated_account),
            metadata={"settlementId": identifier},
        )
        if post_claim_clawback:
            cursor.execute(
                _INCREASE_AGENT_CLAWBACK_SQL,
                (
                    post_claim_clawback,
                    str(settlement["agent_id"]),
                    str(settlement["currency"]),
                    int(updated_account["version"]),
                    post_claim_clawback,
                ),
            )
            clawback_row = _fetchone_dict(cursor)
            if clawback_row is None:
                raise ProductFinanceConflict(
                    "settlement refund clawback exceeds credited earnings: "
                    f"{identifier}"
                )
            updated_account = _decode_row(clawback_row)
            _insert_finance_ledger(
                cursor,
                agent_id=str(settlement["agent_id"]),
                currency=str(settlement["currency"]),
                entry_kind="commission_clawback",
                source_type="settlement",
                source_id=identifier,
                earned_delta_cents=0,
                reserved_delta_cents=0,
                withdrawn_delta_cents=0,
                clawback_delta_cents=post_claim_clawback,
                balance_after=withdrawable_balance(updated_account),
                metadata={
                    "settlementId": identifier,
                    "reason": "refund_after_settlement_claim",
                },
            )
    elif target in {"failed", "canceled"}:
        cursor.execute(_RELEASE_SETTLEMENT_ORDERS_SQL, (identifier,))
        released_rows = _fetchall_dicts(cursor)
        if (
            cursor.rowcount != int(settlement["order_count"])
            or len(released_rows) != int(settlement["order_count"])
        ):
            raise ProductFinanceConflict(
                "settlement order release count changed inside the transaction"
            )

    merged_metadata = _append_status_history(
        settlement.get("metadata"),
        current=current,
        target=target,
        reason=reason,
        metadata=request_metadata,
    )
    cursor.execute(
        _UPDATE_SETTLEMENT_STATUS_SQL,
        (
            target,
            str(reason or ""),
            _canonical_json(merged_metadata),
            target,
            target,
            identifier,
            current,
            int(settlement["version"]),
        ),
    )
    updated = _fetchone_dict(cursor)
    if updated is None:
        raise ProductFinanceConflict(
            f"settlement status update lost its row lock: {identifier}"
        )
    record_finance_audit(
        cursor,
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="commission",
        action=audit_action,
        target_type="commission_settlement",
        target_id=identifier,
        status="succeeded",
        reason=reason,
        metadata=audit_metadata,
    )
    record = _decode_row(updated)
    if updated_account is not None:
        record["finance_account"] = updated_account
    return FinanceMutationResult(record=record, idempotent=False)


def create_withdrawal_request(
    cursor: CursorLike,
    *,
    withdrawal_id: str,
    agent_id: str,
    idempotency_key: str,
    amount_cents: int,
    account_snapshot: Mapping[str, Any],
    currency: str = "CNY",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceCreateResult:
    """Reserve withdrawable funds while holding the stable agent row lock."""

    identifier = _identifier(withdrawal_id, "withdrawal_id")
    agent = _identifier(agent_id, "agent_id")
    idem_key = _identifier(idempotency_key, "idempotency_key")
    amount = _positive_int(amount_cents, "amount_cents")
    if amount < MIN_WITHDRAWAL_AMOUNT_CENTS:
        raise InvalidProductFinanceInput(
            "withdrawal amount is below minimum: "
            f"minimum={MIN_WITHDRAWAL_AMOUNT_CENTS}, actual={amount}"
        )
    clean_currency = _currency(currency)
    payout_account = _nonempty_json_object(
        account_snapshot,
        "account_snapshot",
    )
    metadata_object = _json_object_value(metadata or {}, "metadata")
    request_sha256 = _content_sha256(
        {
            "agentId": agent,
            "amountCents": amount,
            "currency": clean_currency,
            "accountSnapshot": payout_account,
            "metadata": metadata_object,
        }
    )

    existing = _withdrawal_by_idempotency(cursor, agent, idem_key)
    if existing is not None:
        _validate_create_replay(
            existing,
            expected_id=identifier,
            expected_digest=request_sha256,
            label="withdrawal",
        )
        return FinanceCreateResult(record=existing, created=False)

    account = _locked_account(cursor, agent)
    if str(account["status"]) != "active":
        raise ProductFinanceStateConflict(
            f"agent finance account is not active: {agent}"
        )
    if str(account["currency"]) != clean_currency:
        raise ProductFinanceConflict(
            f"withdrawal currency does not match agent account: {agent}"
        )

    existing = _withdrawal_by_idempotency(cursor, agent, idem_key)
    if existing is not None:
        _validate_create_replay(
            existing,
            expected_id=identifier,
            expected_digest=request_sha256,
            label="withdrawal",
        )
        return FinanceCreateResult(record=existing, created=False)

    before = withdrawable_balance(account)
    available = int(before["available_cents"])
    if amount > available:
        raise InsufficientWithdrawableBalance(
            agent_id=agent,
            requested_cents=amount,
            available_cents=available,
        )
    after = {
        **before,
        "reserved_cents": int(before["reserved_cents"]) + amount,
        "available_cents": available - amount,
        "requested_cents": amount,
    }
    cursor.execute(
        _INSERT_WITHDRAWAL_SQL,
        (
            identifier,
            agent,
            idem_key,
            request_sha256,
            amount,
            clean_currency,
            _canonical_json(payout_account),
            _canonical_json({"before": before, "after": after}),
            _canonical_json(metadata_object),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is None:
        raced = _withdrawal_by_idempotency(cursor, agent, idem_key)
        if raced is not None:
            _validate_create_replay(
                raced,
                expected_id=identifier,
                expected_digest=request_sha256,
                label="withdrawal",
            )
            return FinanceCreateResult(record=raced, created=False)
        cursor.execute(_SELECT_WITHDRAWAL_FOR_UPDATE_SQL, (identifier,))
        if _fetchone_dict(cursor) is not None:
            raise ProductFinanceConflict(
                f"withdrawal ID already belongs to another request: {identifier}"
            )
        raise ProductFinanceConflict(
            "withdrawal insert conflicted without a resolvable row: "
            f"{identifier}"
        )

    cursor.execute(
        _RESERVE_WITHDRAWAL_SQL,
        (
            amount,
            agent,
            clean_currency,
            int(account["version"]),
            amount,
        ),
    )
    reserved_row = _fetchone_dict(cursor)
    if reserved_row is None:
        raise InsufficientWithdrawableBalance(
            agent_id=agent,
            requested_cents=amount,
            available_cents=available,
        )
    reserved_account = _decode_row(reserved_row)
    _insert_finance_ledger(
        cursor,
        agent_id=agent,
        currency=clean_currency,
        entry_kind="withdrawal_reserve",
        source_type="withdrawal",
        source_id=identifier,
        earned_delta_cents=0,
        reserved_delta_cents=amount,
        withdrawn_delta_cents=0,
        balance_after=withdrawable_balance(reserved_account),
        metadata={"withdrawalId": identifier},
    )
    record = _decode_row(inserted)
    record["finance_account"] = reserved_account
    return FinanceCreateResult(record=record, created=True)


def review_withdrawal_request(
    cursor: CursorLike,
    *,
    withdrawal_id: str,
    target_status: str,
    action_id: str,
    actor_user_id: str,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceMutationResult:
    """Review or pay a withdrawal, with the audit in the same transaction."""

    identifier = _identifier(withdrawal_id, "withdrawal_id")
    target = _choice(target_status, WITHDRAWAL_STATUSES, "withdrawal status")
    action_identifier = _identifier(action_id, "action_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    request_metadata = _json_object_value(metadata or {}, "metadata")
    audit_action = f"withdrawal.status.{target}"
    audit_metadata = {"targetStatus": target, "metadata": request_metadata}
    audit_digest = _audit_content_sha256(
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="withdrawal",
        action=audit_action,
        target_type="agent_withdrawal",
        target_id=identifier,
        status="succeeded",
        reason=str(reason or ""),
        metadata=audit_metadata,
    )

    cursor.execute(_SELECT_WITHDRAWAL_FOR_UPDATE_SQL, (identifier,))
    withdrawal_row = _fetchone_dict(cursor)
    if withdrawal_row is None:
        raise ProductFinanceNotFound(f"withdrawal not found: {identifier}")
    withdrawal = _decode_row(withdrawal_row)
    replayed_audit = _audit_by_action_id(cursor, action_identifier)
    if replayed_audit is not None:
        _require_digest_match(
            replayed_audit.get("content_sha256"),
            audit_digest,
            "finance audit action",
            action_identifier,
            audit_conflict=True,
        )
        return FinanceMutationResult(record=withdrawal, idempotent=True)

    current = str(withdrawal["status"])
    if target not in WITHDRAWAL_TRANSITIONS[current]:
        raise ProductFinanceStateConflict(
            f"invalid withdrawal status transition: {current} -> {target}"
        )
    account = _locked_account(cursor, str(withdrawal["agent_id"]))
    amount = int(withdrawal["amount_cents"])
    currency = str(withdrawal["currency"])
    updated_account = account

    if target in {"rejected", "canceled"}:
        cursor.execute(
            _RELEASE_WITHDRAWAL_SQL,
            (
                amount,
                str(withdrawal["agent_id"]),
                currency,
                int(account["version"]),
                amount,
            ),
        )
        released_row = _fetchone_dict(cursor)
        if released_row is None:
            raise ProductFinanceConflict(
                f"withdrawal reservation is missing: {identifier}"
            )
        updated_account = _decode_row(released_row)
        _insert_finance_ledger(
            cursor,
            agent_id=str(withdrawal["agent_id"]),
            currency=currency,
            entry_kind="withdrawal_release",
            source_type="withdrawal",
            source_id=identifier,
            earned_delta_cents=0,
            reserved_delta_cents=-amount,
            withdrawn_delta_cents=0,
            balance_after=withdrawable_balance(updated_account),
            metadata={"withdrawalId": identifier, "status": target},
        )
    elif target == "paid":
        cursor.execute(
            _PAY_WITHDRAWAL_SQL,
            (
                amount,
                amount,
                str(withdrawal["agent_id"]),
                currency,
                int(account["version"]),
                amount,
            ),
        )
        paid_row = _fetchone_dict(cursor)
        if paid_row is None:
            raise ProductFinanceConflict(
                f"withdrawal reservation is missing: {identifier}"
            )
        updated_account = _decode_row(paid_row)
        _insert_finance_ledger(
            cursor,
            agent_id=str(withdrawal["agent_id"]),
            currency=currency,
            entry_kind="withdrawal_paid",
            source_type="withdrawal",
            source_id=identifier,
            earned_delta_cents=0,
            reserved_delta_cents=-amount,
            withdrawn_delta_cents=amount,
            balance_after=withdrawable_balance(updated_account),
            metadata={"withdrawalId": identifier},
        )

    merged_metadata = _append_status_history(
        withdrawal.get("metadata"),
        current=current,
        target=target,
        reason=reason,
        metadata=request_metadata,
    )
    cursor.execute(
        _UPDATE_WITHDRAWAL_STATUS_SQL,
        (
            target,
            str(reason or ""),
            _canonical_json(merged_metadata),
            target,
            target,
            target,
            target,
            identifier,
            current,
            int(withdrawal["version"]),
        ),
    )
    updated = _fetchone_dict(cursor)
    if updated is None:
        raise ProductFinanceConflict(
            f"withdrawal status update lost its row lock: {identifier}"
        )
    record_finance_audit(
        cursor,
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain="withdrawal",
        action=audit_action,
        target_type="agent_withdrawal",
        target_id=identifier,
        status="succeeded",
        reason=reason,
        metadata=audit_metadata,
    )
    record = _decode_row(updated)
    record["finance_account"] = updated_account
    return FinanceMutationResult(record=record, idempotent=False)


def record_finance_audit(
    cursor: CursorLike,
    *,
    action_id: str,
    actor_user_id: str,
    action_domain: str,
    action: str,
    target_type: str,
    target_id: str,
    status: str = "succeeded",
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FinanceCreateResult:
    """Insert an immutable, exact-replay admin finance/risk audit action."""

    action_identifier = _identifier(action_id, "action_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    domain = _choice(action_domain, AUDIT_DOMAINS, "audit domain")
    clean_action = _identifier(action, "action")
    clean_target_type = _identifier(target_type, "target_type")
    clean_target_id = _identifier(target_id, "target_id")
    clean_status = _choice(status, AUDIT_STATUSES, "audit status")
    clean_reason = str(reason or "")
    metadata_object = _json_object_value(metadata or {}, "metadata")
    content_sha256 = _audit_content_sha256(
        action_id=action_identifier,
        actor_user_id=actor,
        action_domain=domain,
        action=clean_action,
        target_type=clean_target_type,
        target_id=clean_target_id,
        status=clean_status,
        reason=clean_reason,
        metadata=metadata_object,
    )
    cursor.execute(
        _INSERT_AUDIT_SQL,
        (
            action_identifier,
            actor,
            domain,
            clean_action,
            clean_target_type,
            clean_target_id,
            clean_status,
            clean_reason,
            _canonical_json(metadata_object),
            content_sha256,
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return FinanceCreateResult(record=_decode_row(inserted), created=True)

    existing = _audit_by_action_id(cursor, action_identifier)
    if existing is None:
        raise ProductFinanceAuditConflict(
            "audit insert conflicted without a resolvable action: "
            f"{action_identifier}"
        )
    _require_digest_match(
        existing.get("content_sha256"),
        content_sha256,
        "finance audit action",
        action_identifier,
        audit_conflict=True,
    )
    return FinanceCreateResult(record=existing, created=False)


def withdrawable_balance(account: Mapping[str, Any]) -> dict[str, Any]:
    earned = _nonnegative_int(account.get("earned_cents"), "earned_cents")
    clawback = _nonnegative_int(
        account.get("clawback_cents") or 0,
        "clawback_cents",
    )
    reserved = _nonnegative_int(
        account.get("reserved_cents"),
        "reserved_cents",
    )
    withdrawn = _nonnegative_int(
        account.get("withdrawn_cents"),
        "withdrawn_cents",
    )
    gross_available = earned - reserved - withdrawn
    if gross_available < 0 or clawback > earned:
        raise ProductFinanceConflict(
            "agent finance account violates the withdrawable balance invariant"
        )
    available = max(gross_available - clawback, 0)
    liability = max(clawback - gross_available, 0)
    return {
        "agent_id": str(account.get("agent_id") or ""),
        "currency": str(account.get("currency") or ""),
        "status": str(account.get("status") or ""),
        "earned_cents": earned,
        "clawback_cents": clawback,
        "reserved_cents": reserved,
        "withdrawn_cents": withdrawn,
        "available_cents": available,
        "liability_cents": liability,
        "version": int(account.get("version") or 0),
    }


class ProductFinanceStore:
    """Transactional PostgreSQL finance operations over an injected connection."""

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductFinanceInput(
                "ProductFinanceStore requires an autocommit-disabled connection"
            )
        self.connection = connection

    def ensure_agent_account(self, **kwargs: Any) -> FinanceCreateResult:
        with self._transaction() as cursor:
            return ensure_agent_finance_account(cursor, **kwargs)

    def record_commission_order(self, **kwargs: Any) -> FinanceCreateResult:
        with self._transaction() as cursor:
            return record_commission_order(cursor, **kwargs)

    def transition_commission_order(
        self,
        **kwargs: Any,
    ) -> FinanceMutationResult:
        with self._transaction() as cursor:
            return transition_commission_order(cursor, **kwargs)

    def release_eligible_commissions(
        self,
        **kwargs: Any,
    ) -> FinanceMutationResult:
        with self._transaction() as cursor:
            return release_eligible_commissions(cursor, **kwargs)

    def apply_commission_refund(
        self,
        **kwargs: Any,
    ) -> FinanceMutationResult:
        with self._transaction() as cursor:
            return apply_commission_refund(cursor, **kwargs)

    def create_commission_settlement(
        self,
        **kwargs: Any,
    ) -> FinanceCreateResult:
        with self._transaction() as cursor:
            return create_commission_settlement(cursor, **kwargs)

    def transition_commission_settlement(
        self,
        **kwargs: Any,
    ) -> FinanceMutationResult:
        with self._transaction() as cursor:
            return transition_commission_settlement(cursor, **kwargs)

    def create_withdrawal(self, **kwargs: Any) -> FinanceCreateResult:
        with self._transaction() as cursor:
            return create_withdrawal_request(cursor, **kwargs)

    def review_withdrawal(self, **kwargs: Any) -> FinanceMutationResult:
        with self._transaction() as cursor:
            return review_withdrawal_request(cursor, **kwargs)

    def record_audit(self, **kwargs: Any) -> FinanceCreateResult:
        with self._transaction() as cursor:
            return record_finance_audit(cursor, **kwargs)

    def get_withdrawable_balance(self, *, agent_id: str) -> dict[str, Any]:
        agent = _identifier(agent_id, "agent_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_ACCOUNT_SQL, (agent,))
            row = _fetchone_dict(cursor)
            if row is None:
                raise ProductFinanceNotFound(
                    f"agent finance account not found: {agent}"
                )
            return withdrawable_balance(_decode_row(row))

    def get_agent_balance(self, *, agent_id: str) -> dict[str, Any]:
        return self.get_withdrawable_balance(agent_id=agent_id)

    def get_commission_order(
        self,
        *,
        commission_order_id: str,
    ) -> dict[str, Any]:
        identifier = _identifier(
            commission_order_id,
            "commission_order_id",
        )
        with self._transaction() as cursor:
            cursor.execute(_SELECT_COMMISSION_ORDER_SQL, (identifier,))
            row = _fetchone_dict(cursor)
            if row is None:
                raise ProductFinanceNotFound(
                    f"commission order not found: {identifier}"
                )
            return _decode_row(row)

    def get_settlement(self, *, settlement_id: str) -> dict[str, Any]:
        identifier = _identifier(settlement_id, "settlement_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_SETTLEMENT_SQL, (identifier,))
            row = _fetchone_dict(cursor)
            if row is None:
                raise ProductFinanceNotFound(
                    f"commission settlement not found: {identifier}"
                )
            record = _decode_row(row)
            cursor.execute(_SELECT_SETTLEMENT_ITEMS_SQL, (identifier,))
            record["items"] = [
                _decode_row(item) for item in _fetchall_dicts(cursor)
            ]
            return record

    def list_commission_orders(
        self,
        *,
        agent_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        agent = (
            _identifier(agent_id, "agent_id")
            if str(agent_id or "").strip()
            else ""
        )
        clean_status = str(status or "").strip().lower()
        if clean_status and clean_status not in COMMISSION_ORDER_STATUSES:
            raise InvalidProductFinanceInput(
                "commission order status has an invalid value"
            )
        clean_limit = _bounded_positive_int(
            limit,
            "limit",
            maximum=1000,
        )
        with self._transaction() as cursor:
            cursor.execute(
                _LIST_COMMISSION_ORDERS_SQL,
                (
                    agent,
                    agent,
                    clean_status,
                    clean_status,
                    clean_limit,
                ),
            )
            return [
                _decode_row(row)
                for row in _fetchall_dicts(cursor)
            ]

    def get_commission_refund(
        self,
        *,
        refund_id: str,
    ) -> dict[str, Any]:
        identifier = _identifier(refund_id, "refund_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_COMMISSION_REFUND_SQL, (identifier,))
            row = _fetchone_dict(cursor)
            if row is None:
                raise ProductFinanceNotFound(
                    f"commission refund not found: {identifier}"
                )
            return _decode_row(row)

    def list_commission_refunds(
        self,
        *,
        agent_id: str = "",
        commission_order_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        agent = (
            _identifier(agent_id, "agent_id")
            if str(agent_id or "").strip()
            else ""
        )
        order = (
            _identifier(
                commission_order_id,
                "commission_order_id",
            )
            if str(commission_order_id or "").strip()
            else ""
        )
        clean_limit = _bounded_positive_int(
            limit,
            "limit",
            maximum=1000,
        )
        with self._transaction() as cursor:
            cursor.execute(
                _LIST_COMMISSION_REFUNDS_SQL,
                (agent, agent, order, order, clean_limit),
            )
            return [
                _decode_row(row)
                for row in _fetchall_dicts(cursor)
            ]

    def list_settlements(
        self,
        *,
        agent_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        agent = (
            _identifier(agent_id, "agent_id")
            if str(agent_id or "").strip()
            else ""
        )
        clean_status = str(status or "").strip().lower()
        if clean_status and clean_status not in SETTLEMENT_STATUSES:
            raise InvalidProductFinanceInput(
                "settlement status has an invalid value"
            )
        clean_limit = _bounded_positive_int(
            limit,
            "limit",
            maximum=1000,
        )
        with self._transaction() as cursor:
            cursor.execute(
                _LIST_SETTLEMENTS_SQL,
                (
                    agent,
                    agent,
                    clean_status,
                    clean_status,
                    clean_limit,
                ),
            )
            return [
                _decode_row(row)
                for row in _fetchall_dicts(cursor)
            ]

    def get_withdrawal(self, *, withdrawal_id: str) -> dict[str, Any]:
        identifier = _identifier(withdrawal_id, "withdrawal_id")
        with self._transaction() as cursor:
            cursor.execute(_SELECT_WITHDRAWAL_SQL, (identifier,))
            row = _fetchone_dict(cursor)
            if row is None:
                raise ProductFinanceNotFound(
                    f"withdrawal not found: {identifier}"
                )
            return _decode_row(row)

    def list_withdrawals(
        self,
        *,
        agent_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        agent = (
            _identifier(agent_id, "agent_id")
            if str(agent_id or "").strip()
            else ""
        )
        clean_status = str(status or "").strip().lower()
        if clean_status and clean_status not in WITHDRAWAL_STATUSES:
            raise InvalidProductFinanceInput(
                "withdrawal status has an invalid value"
            )
        clean_limit = _bounded_positive_int(
            limit,
            "limit",
            maximum=1000,
        )
        with self._transaction() as cursor:
            cursor.execute(
                _LIST_WITHDRAWALS_SQL,
                (
                    agent,
                    agent,
                    clean_status,
                    clean_status,
                    clean_limit,
                ),
            )
            return [
                _decode_row(row)
                for row in _fetchall_dicts(cursor)
            ]

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


def _locked_account(cursor: CursorLike, agent_id: str) -> dict[str, Any]:
    cursor.execute(_SELECT_ACCOUNT_FOR_UPDATE_SQL, (agent_id,))
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductFinanceNotFound(
            f"agent finance account not found: {agent_id}"
        )
    return _decode_row(row)


def _settlement_by_idempotency(
    cursor: CursorLike,
    agent_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    cursor.execute(
        _SELECT_SETTLEMENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (agent_id, idempotency_key),
    )
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_row(row)


def _commission_refund_by_idempotency(
    cursor: CursorLike,
    agent_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    cursor.execute(
        _SELECT_COMMISSION_REFUND_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (agent_id, idempotency_key),
    )
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_row(row)


def _withdrawal_by_idempotency(
    cursor: CursorLike,
    agent_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    cursor.execute(
        _SELECT_WITHDRAWAL_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (agent_id, idempotency_key),
    )
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_row(row)


def _audit_by_action_id(
    cursor: CursorLike,
    action_id: str,
) -> dict[str, Any] | None:
    cursor.execute(_SELECT_AUDIT_FOR_UPDATE_SQL, (action_id,))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_row(row)


def _validate_create_replay(
    existing: Mapping[str, Any],
    *,
    expected_id: str,
    expected_digest: str,
    label: str,
) -> None:
    if str(existing.get("id") or "") != expected_id:
        raise ProductFinanceConflict(
            f"{label} idempotency key belongs to another ID: "
            f"{existing.get('id')}"
        )
    _require_digest_match(
        existing.get("request_sha256"),
        expected_digest,
        label,
        expected_id,
    )


def _release_result_from_audit(
    audit: Mapping[str, Any],
    *,
    action_id: str,
    actor_user_id: str,
    target_id: str,
    request_sha256: str,
) -> dict[str, Any]:
    expected = {
        "action_id": action_id,
        "actor_user_id": actor_user_id,
        "action_domain": "commission",
        "action": "commission_order.release_eligible",
        "target_type": "commission_release_batch",
        "target_id": target_id,
        "status": "succeeded",
    }
    for field, value in expected.items():
        if str(audit.get(field) or "") != value:
            raise ProductFinanceAuditConflict(
                f"commission release action identity changed: {action_id}"
            )
    metadata = audit.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ProductFinanceAuditConflict(
            f"commission release audit metadata is invalid: {action_id}"
        )
    _require_digest_match(
        metadata.get("requestSha256"),
        request_sha256,
        "commission release request",
        action_id,
        audit_conflict=True,
    )
    result = metadata.get("result")
    if not isinstance(result, Mapping):
        raise ProductFinanceAuditConflict(
            f"commission release audit has no result: {action_id}"
        )
    return dict(result)


def _validate_frozen_settlement_orders(
    settlement: Mapping[str, Any],
    orders: Sequence[Mapping[str, Any]],
) -> None:
    expected_count = int(settlement["order_count"])
    if len(orders) != expected_count:
        raise ProductFinanceConflict(
            "settlement item count does not match its frozen order count"
        )
    order_amount = sum(
        int(row["settlement_order_amount_cents"]) for row in orders
    )
    commission_amount = sum(
        int(row["settlement_commission_amount_cents"]) for row in orders
    )
    if order_amount != int(settlement["total_order_amount_cents"]):
        raise ProductFinanceConflict(
            "settlement order amount does not match frozen total"
        )
    if commission_amount != int(
        settlement["total_commission_amount_cents"]
    ):
        raise ProductFinanceConflict(
            "settlement commission amount does not match frozen total"
        )
    invalid = [
        str(row["id"])
        for row in orders
        if str(row["status"]) != "claimed"
        or str(row.get("settlement_id") or "") != str(settlement["id"])
        or int(row["commission_amount_cents"])
            != int(row["settlement_original_commission_amount_cents"])
        or int(row.get("reversed_commission_cents") or 0)
            < int(
                row.get(
                    "settlement_reversed_commission_amount_cents"
                )
                or 0
            )
        or int(row["settlement_commission_amount_cents"])
            != (
                int(row["settlement_original_commission_amount_cents"])
                - int(
                    row.get(
                        "settlement_reversed_commission_amount_cents"
                    )
                    or 0
                )
            )
    ]
    if invalid:
        raise ProductFinanceConflict(
            "settlement no longer owns all frozen commission orders: "
            f"{','.join(invalid)}"
        )


def _insert_finance_ledger(
    cursor: CursorLike,
    *,
    agent_id: str,
    currency: str,
    entry_kind: str,
    source_type: str,
    source_id: str,
    earned_delta_cents: int,
    reserved_delta_cents: int,
    withdrawn_delta_cents: int,
    balance_after: Mapping[str, Any],
    metadata: Mapping[str, Any],
    clawback_delta_cents: int = 0,
) -> dict[str, Any]:
    identity = f"{entry_kind}:{source_type}:{source_id}"
    ledger_id = f"fin_{hashlib.sha256(identity.encode()).hexdigest()[:48]}"
    content = {
        "id": ledger_id,
        "agentId": agent_id,
        "currency": currency,
        "entryKind": entry_kind,
        "sourceType": source_type,
        "sourceId": source_id,
        "earnedDeltaCents": earned_delta_cents,
        "clawbackDeltaCents": clawback_delta_cents,
        "reservedDeltaCents": reserved_delta_cents,
        "withdrawnDeltaCents": withdrawn_delta_cents,
        "balanceAfter": dict(balance_after),
        "metadata": dict(metadata),
    }
    digest = _content_sha256(content)
    cursor.execute(
        _INSERT_LEDGER_SQL,
        (
            ledger_id,
            agent_id,
            currency,
            entry_kind,
            source_type,
            source_id,
            earned_delta_cents,
            clawback_delta_cents,
            reserved_delta_cents,
            withdrawn_delta_cents,
            _canonical_json(balance_after),
            digest,
            _canonical_json(metadata),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_row(inserted)
    cursor.execute(
        _SELECT_LEDGER_BY_SOURCE_FOR_UPDATE_SQL,
        (entry_kind, source_type, source_id),
    )
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductFinanceConflict(
            f"finance ledger insert conflicted without a row: {identity}"
        )
    decoded = _decode_row(existing)
    _require_digest_match(
        decoded.get("content_sha256"),
        digest,
        "finance ledger",
        identity,
    )
    return decoded


def _append_status_history(
    raw_metadata: Any,
    *,
    current: str,
    target: str,
    reason: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    history = payload.get("statusHistory")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": current,
            "to": target,
            "reason": str(reason or ""),
            "metadata": dict(metadata),
        }
    )
    payload["statusHistory"] = history
    return payload


def _audit_content_sha256(
    *,
    action_id: str,
    actor_user_id: str,
    action_domain: str,
    action: str,
    target_type: str,
    target_id: str,
    status: str,
    reason: str,
    metadata: Mapping[str, Any],
) -> str:
    return _content_sha256(
        {
            "actionId": action_id,
            "actorUserId": actor_user_id,
            "actionDomain": action_domain,
            "action": action,
            "targetType": target_type,
            "targetId": target_id,
            "status": status,
            "reason": reason,
            "metadata": dict(metadata),
        }
    )


def _content_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest_match(
    actual: Any,
    expected: str,
    label: str,
    identifier: str,
    *,
    audit_conflict: bool = False,
) -> None:
    if hmac.compare_digest(str(actual or ""), expected):
        return
    error_type = (
        ProductFinanceAuditConflict
        if audit_conflict
        else ProductFinanceConflict
    )
    raise error_type(f"{label} replay content changed: {identifier}")


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    columns = [
        str(getattr(column, "name", column[0] if column else ""))
        for column in description
    ]
    if not columns or len(columns) != len(row):
        raise ProductFinanceStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(columns, row))


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    description = cursor.description or ()
    columns = [
        str(getattr(column, "name", column[0] if column else ""))
        for column in description
    ]
    if not columns:
        raise ProductFinanceStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return [dict(zip(columns, row)) for row in rows]


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in (
        "metadata",
        "settlement_account",
        "account_snapshot",
        "balance_snapshot",
        "balance_after",
    ):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                decoded[field] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ProductFinanceStoreError(
                    f"database returned invalid JSON for {field}"
                ) from exc
    return decoded


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(value: Mapping[str, Any], field: str) -> str:
    return _canonical_json(_json_object_value(value, field))


def _json_object_value(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidProductFinanceInput(f"{field} must be an object")
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise InvalidProductFinanceInput(
            f"{field} must be JSON serializable"
        ) from exc


def _nonempty_json_object(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    payload = _json_object_value(value, field)
    if not payload:
        raise InvalidProductFinanceInput(f"{field} must not be empty")
    return payload


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(text):
        raise InvalidProductFinanceInput(f"invalid {field}: {value}")
    return text


def _optional_identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    return "" if not text else _identifier(text, field)


def _settlement_number(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}", text):
        raise InvalidProductFinanceInput(f"invalid settlement_no: {value}")
    return text


def _unique_identifiers(values: Sequence[str], field: str) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidProductFinanceInput(f"{field} must be a sequence")
    clean = [_identifier(value, f"{field}[]") for value in values]
    if not clean:
        raise InvalidProductFinanceInput(f"{field} must not be empty")
    if len(set(clean)) != len(clean):
        raise InvalidProductFinanceInput(f"{field} contains duplicates")
    return sorted(clean)


def _currency(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", text):
        raise InvalidProductFinanceInput(f"invalid currency: {value}")
    return text


def _choice(value: Any, allowed: frozenset[str], field: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise InvalidProductFinanceInput(f"invalid {field}: {value}")
    return text


def _positive_int(value: Any, field: str) -> int:
    number = _nonnegative_int(value, field)
    if number <= 0:
        raise InvalidProductFinanceInput(f"{field} must be positive")
    return number


def _bounded_positive_int(value: Any, field: str, *, maximum: int) -> int:
    number = _positive_int(value, field)
    if number > maximum:
        raise InvalidProductFinanceInput(
            f"{field} must not exceed {maximum}"
        )
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidProductFinanceInput(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductFinanceInput(
            f"{field} must be an integer"
        ) from exc
    if number < 0:
        raise InvalidProductFinanceInput(f"{field} must be non-negative")
    return number
