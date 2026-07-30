from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_finance_store as finance


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "008_product_finance_postgres.sql"
DIGEST_A = "a" * 64


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.rows: list[dict[str, Any]] = []
        self.closed = False

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        match = re.search(
            r"/\* product_finance_store:([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        name = match.group(1)
        if "?" in operation:
            raise AssertionError(f"{name} contains a SQLite placeholder")
        placeholder_count = operation.count("%s")
        if placeholder_count != len(parameters):
            raise AssertionError(
                f"{name} expected {placeholder_count} SQL parameters, "
                f"received {len(parameters)}"
            )
        self.connection.calls.append((name, operation, tuple(parameters)))
        responses = self.connection.responses.get(name)
        if not responses:
            raise AssertionError(f"unexpected or exhausted SQL operation: {name}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.rows = list(response)
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = list(self.rows)
        self.rows = []
        return rows

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    def __init__(
        self,
        *,
        commit_error: BaseException | None = None,
        **responses: list[Any],
    ) -> None:
        self.responses = {name: list(items) for name, items in responses.items()}
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.cursors: list[ScriptedCursor] = []
        self.commit_error = commit_error

    def cursor(self) -> ScriptedCursor:
        cursor = ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1


class AutocommitConnection(ScriptedConnection):
    autocommit = True


def account_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "agent_id": "agent-1",
        "currency": "CNY",
        "status": "active",
        "earned_cents": 40_000,
        "clawback_cents": 0,
        "reserved_cents": 0,
        "withdrawn_cents": 0,
        "version": 1,
        "metadata": {},
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
    }
    row.update(overrides)
    return row


def commission_order_row(
    identifier: str = "commission-1",
    **overrides: Any,
) -> dict[str, Any]:
    row = {
        "id": identifier,
        "source_order_id": f"payment-{identifier}",
        "agent_id": "agent-1",
        "customer_user_id": "customer-1",
        "order_amount_cents": 100_000,
        "commission_amount_cents": 20_000,
        "commission_rate_bps": 2000,
        "refunded_order_amount_cents": 0,
        "reversed_commission_cents": 0,
        "refund_status": "none",
        "currency": "CNY",
        "status": "eligible",
        "settlement_id": None,
        "content_sha256": DIGEST_A,
        "metadata": {},
        "eligible_at": "2026-07-30T00:01:00Z",
        "claimed_at": None,
        "settled_at": None,
        "last_refunded_at": None,
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:01:00Z",
        "settlement_order_amount_cents": 100_000,
        "settlement_commission_amount_cents": 20_000,
        "settlement_original_commission_amount_cents": 20_000,
        "settlement_reversed_commission_amount_cents": 0,
    }
    row.update(overrides)
    return row


def settlement_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "settlement-1",
        "agent_id": "agent-1",
        "idempotency_key": "settlement-idem-1",
        "request_sha256": DIGEST_A,
        "settlement_no": "SET-1",
        "total_order_amount_cents": 200_000,
        "total_commission_amount_cents": 40_000,
        "order_count": 2,
        "currency": "CNY",
        "status": "pending",
        "settlement_account": {"type": "alipay", "account": "masked"},
        "failure_reason": "",
        "metadata": {},
        "version": 0,
        "created_at": "2026-07-30T00:02:00Z",
        "updated_at": "2026-07-30T00:02:00Z",
        "paid_at": None,
        "released_at": None,
    }
    row.update(overrides)
    return row


def withdrawal_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "withdrawal-1",
        "agent_id": "agent-1",
        "idempotency_key": "withdrawal-idem-1",
        "request_sha256": DIGEST_A,
        "amount_cents": 30_000,
        "currency": "CNY",
        "status": "pending",
        "account_snapshot": {"type": "bank", "tail": "1234"},
        "balance_snapshot": {},
        "status_reason": "",
        "metadata": {},
        "version": 0,
        "created_at": "2026-07-30T00:03:00Z",
        "updated_at": "2026-07-30T00:03:00Z",
        "approved_at": None,
        "rejected_at": None,
        "paid_at": None,
        "canceled_at": None,
    }
    row.update(overrides)
    return row


def audit_row(
    *,
    action_id: str = "action-1",
    actor_user_id: str = "admin-1",
    action_domain: str = "payment",
    action: str = "payment.reconcile",
    target_type: str = "payment_order",
    target_id: str = "payment-1",
    status: str = "succeeded",
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = metadata or {}
    digest = finance._audit_content_sha256(  # type: ignore[attr-defined]
        action_id=action_id,
        actor_user_id=actor_user_id,
        action_domain=action_domain,
        action=action,
        target_type=target_type,
        target_id=target_id,
        status=status,
        reason=reason,
        metadata=payload,
    )
    return {
        "action_id": action_id,
        "actor_user_id": actor_user_id,
        "action_domain": action_domain,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "status": status,
        "reason": reason,
        "metadata": payload,
        "content_sha256": digest,
        "created_at": "2026-07-30T00:04:00Z",
    }


def ledger_row(
    *,
    entry_kind: str,
    source_type: str,
    source_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    row = {
        "id": f"fin-{source_id}",
        "agent_id": "agent-1",
        "currency": "CNY",
        "entry_kind": entry_kind,
        "source_type": source_type,
        "source_id": source_id,
        "earned_delta_cents": 0,
        "clawback_delta_cents": 0,
        "reserved_delta_cents": 0,
        "withdrawn_delta_cents": 0,
        "balance_after": {},
        "content_sha256": DIGEST_A,
        "metadata": {},
        "created_at": "2026-07-30T00:04:00Z",
    }
    row.update(overrides)
    return row


def commission_refund_row(
    *,
    identifier: str = "refund-1",
    commission_order_id: str = "commission-1",
    idempotency_key: str = "refund-idem-1",
    action_id: str = "refund-action-1",
    request_sha256: str = DIGEST_A,
    **overrides: Any,
) -> dict[str, Any]:
    row = {
        "id": identifier,
        "commission_order_id": commission_order_id,
        "agent_id": "agent-1",
        "source_order_id": f"payment-{commission_order_id}",
        "idempotency_key": idempotency_key,
        "action_id": action_id,
        "request_sha256": request_sha256,
        "refund_amount_cents": 50_000,
        "requested_cumulative_refunded_amount_cents": 50_000,
        "cumulative_refunded_amount_cents": 50_000,
        "commission_reversal_delta_cents": 10_000,
        "cumulative_reversed_commission_cents": 10_000,
        "account_clawback_delta_cents": 0,
        "order_status_before": "eligible",
        "order_status_after": "eligible",
        "settlement_id": None,
        "metadata": {},
        "created_at": "2026-07-30T00:04:00Z",
    }
    row.update(overrides)
    return row


def commission_refund_request_sha256(
    *,
    refund_amount_cents: int = 50_000,
    cumulative_refunded_amount_cents: int | None = 50_000,
) -> str:
    return finance._content_sha256(  # type: ignore[attr-defined]
        {
            "refundId": "refund-1",
            "commissionOrderId": "commission-1",
            "refundAmountCents": refund_amount_cents,
            "cumulativeRefundedAmountCents": (
                cumulative_refunded_amount_cents
            ),
            "actionId": "refund-action-1",
            "actorUserId": "payment-worker",
            "idempotencyKey": "refund-idem-1",
            "metadata": {},
        }
    )


def settlement_create_kwargs() -> dict[str, Any]:
    return {
        "settlement_id": "settlement-1",
        "agent_id": "agent-1",
        "idempotency_key": "settlement-idem-1",
        "commission_order_ids": ["commission-1", "commission-2"],
        "settlement_account": {"type": "alipay", "account": "masked"},
        "settlement_no": "SET-1",
    }


def withdrawal_create_kwargs() -> dict[str, Any]:
    return {
        "withdrawal_id": "withdrawal-1",
        "agent_id": "agent-1",
        "idempotency_key": "withdrawal-idem-1",
        "amount_cents": 30_000,
        "account_snapshot": {"type": "bank", "tail": "1234"},
    }


def test_migration_is_self_contained_and_uses_cents_and_fail_closed_states() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    for table in (
        "product_agent_finance_accounts",
        "product_commission_orders",
        "product_commission_settlements",
        "product_commission_settlement_items",
        "product_agent_withdrawals",
        "product_agent_finance_ledger",
        "product_finance_audit_events",
        "product_commission_refunds",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "amount_cents BIGINT" in sql
    assert "total_commission_amount_cents BIGINT" in sql
    assert "reserved_cents + withdrawn_cents <= earned_cents" in sql
    assert "clawback_cents BIGINT" in sql
    assert "refunded_order_amount_cents BIGINT" in sql
    assert "commission_reversal_delta_cents BIGINT" in sql
    assert "PRIMARY KEY (settlement_id, commission_order_id)" in sql
    assert (
        "DROP CONSTRAINT IF EXISTS "
        "uq_product_commission_settlement_item_order"
    ) in sql
    assert "UNIQUE (agent_id, idempotency_key)" in sql
    assert "fk_product_commission_order_settlement" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "'points'," in sql
    assert "'payment'," in sql
    assert "'withdrawal'," in sql
    assert "'risk'" in sql
    assert "product_finance_reject_immutable_mutation" in sql
    assert "trg_product_commission_refund_immutable" in sql


def test_store_rejects_autocommit_connections() -> None:
    with pytest.raises(finance.InvalidProductFinanceInput):
        finance.ProductFinanceStore(AutocommitConnection())


def test_external_cursor_audit_api_does_not_commit_caller_transaction() -> None:
    inserted = audit_row()
    connection = ScriptedConnection(insert_audit=[[inserted]])
    cursor = connection.cursor()

    result = finance.record_finance_audit(
        cursor,
        action_id="action-1",
        actor_user_id="admin-1",
        action_domain="payment",
        action="payment.reconcile",
        target_type="payment_order",
        target_id="payment-1",
    )

    assert result.created is True
    assert result.record["content_sha256"] == inserted["content_sha256"]
    assert connection.commits == 0
    assert connection.rollbacks == 0


def test_audit_replay_is_exact_and_payload_drift_fails_closed() -> None:
    existing = audit_row(metadata={"providerEventId": "event-1"})
    exact_connection = ScriptedConnection(
        insert_audit=[[]],
        select_audit_for_update=[[existing]],
    )
    exact = finance.ProductFinanceStore(exact_connection).record_audit(
        action_id="action-1",
        actor_user_id="admin-1",
        action_domain="payment",
        action="payment.reconcile",
        target_type="payment_order",
        target_id="payment-1",
        metadata={"providerEventId": "event-1"},
    )
    assert exact.created is False

    drift_connection = ScriptedConnection(
        insert_audit=[[]],
        select_audit_for_update=[[existing]],
    )
    with pytest.raises(finance.ProductFinanceAuditConflict):
        finance.ProductFinanceStore(drift_connection).record_audit(
            action_id="action-1",
            actor_user_id="admin-1",
            action_domain="payment",
            action="payment.reconcile",
            target_type="payment_order",
            target_id="payment-1",
            metadata={"providerEventId": "event-2"},
        )
    assert drift_connection.rollbacks == 1


def test_ensure_agent_account_is_idempotent_but_currency_is_frozen() -> None:
    inserted = account_row()
    create_connection = ScriptedConnection(insert_account=[[inserted]])
    created = finance.ProductFinanceStore(
        create_connection
    ).ensure_agent_account(agent_id="agent-1")
    assert created.created is True

    replay_connection = ScriptedConnection(
        insert_account=[[]],
        select_account_for_update=[[inserted]],
    )
    replay = finance.ProductFinanceStore(
        replay_connection
    ).ensure_agent_account(agent_id="agent-1")
    assert replay.created is False

    conflict_connection = ScriptedConnection(
        insert_account=[[]],
        select_account_for_update=[[inserted]],
    )
    with pytest.raises(finance.ProductFinanceConflict):
        finance.ProductFinanceStore(
            conflict_connection
        ).ensure_agent_account(agent_id="agent-1", currency="USD")


def test_pending_commission_becomes_eligible_once_with_immutable_audit() -> None:
    pending = commission_order_row(
        status="pending",
        eligible_at=None,
    )
    eligible = commission_order_row()
    audit = audit_row(
        action_id="commission-eligible-action",
        action_domain="commission",
        action="commission_order.status.eligible",
        target_type="commission_order",
        target_id="commission-1",
        metadata={"targetStatus": "eligible", "metadata": {}},
    )
    connection = ScriptedConnection(
        select_commission_order=[[pending]],
        select_account_for_update=[[account_row()]],
        select_commission_order_for_update=[[pending]],
        select_audit_for_update=[[]],
        mark_commission_order_eligible=[[eligible]],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).transition_commission_order(
        commission_order_id="commission-1",
        target_status="eligible",
        action_id="commission-eligible-action",
        actor_user_id="finance-worker",
    )

    assert result.idempotent is False
    assert result.record["status"] == "eligible"
    names = [call[0] for call in connection.calls]
    assert names.index("select_account_for_update") < names.index(
        "select_commission_order_for_update"
    )
    assert names[-1] == "insert_audit"


def test_commission_status_action_replay_never_moves_the_order_twice() -> None:
    eligible = commission_order_row()
    existing_audit = audit_row(
        action_id="commission-eligible-action",
        actor_user_id="finance-worker",
        action_domain="commission",
        action="commission_order.status.eligible",
        target_type="commission_order",
        target_id="commission-1",
        metadata={"targetStatus": "eligible", "metadata": {}},
    )
    connection = ScriptedConnection(
        select_commission_order=[[eligible]],
        select_account_for_update=[[account_row()]],
        select_commission_order_for_update=[[eligible]],
        select_audit_for_update=[[existing_audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).transition_commission_order(
        commission_order_id="commission-1",
        target_status="eligible",
        action_id="commission-eligible-action",
        actor_user_id="finance-worker",
    )

    assert result.idempotent is True
    assert "mark_commission_order_eligible" not in [
        call[0] for call in connection.calls
    ]


def test_bulk_release_is_bounded_server_timed_locked_and_exactly_replayable() -> None:
    pending = commission_order_row(
        status="pending",
        eligible_at=None,
    )
    eligible = commission_order_row()
    request = {
        "agentId": "agent-1",
        "minAgeDays": 7,
        "limit": 10,
        "reason": "",
        "metadata": {},
        "cutoffSource": "database_current_timestamp",
    }
    request_sha256 = finance._content_sha256(request)  # type: ignore[attr-defined]
    result_payload = {
        "released": 1,
        "commissionAmountCents": 20_000,
        "orderIds": ["commission-1"],
        "agentId": "agent-1",
        "minAgeDays": 7,
        "limit": 10,
        "cutoffSource": "database_current_timestamp",
    }
    audit_metadata = {
        "requestSha256": request_sha256,
        "request": request,
        "result": result_payload,
    }
    audit = audit_row(
        action_id="release-action-1",
        actor_user_id="finance-worker",
        action_domain="commission",
        action="commission_order.release_eligible",
        target_type="commission_release_batch",
        target_id="agent-1",
        metadata=audit_metadata,
    )
    connection = ScriptedConnection(
        lock_release_action=[[{"locked": None}]],
        select_audit_for_update=[[]],
        select_release_accounts_for_update=[[account_row()]],
        select_pending_commission_orders_for_update=[[pending]],
        mark_selected_commission_orders_eligible=[[eligible]],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).release_eligible_commissions(
        action_id="release-action-1",
        actor_user_id="finance-worker",
        agent_id="agent-1",
        min_age_days=7,
        limit=10,
    )

    assert result.idempotent is False
    assert result.record == result_payload
    names = [call[0] for call in connection.calls]
    assert names.index("select_release_accounts_for_update") < names.index(
        "select_pending_commission_orders_for_update"
    )
    pending_sql = next(
        sql
        for name, sql, _parameters in connection.calls
        if name == "select_pending_commission_orders_for_update"
    )
    assert "CURRENT_TIMESTAMP" in pending_sql
    assert "FOR UPDATE SKIP LOCKED" in pending_sql
    assert "now" not in inspect.signature(
        finance.release_eligible_commissions
    ).parameters

    replay_connection = ScriptedConnection(
        lock_release_action=[[{"locked": None}]],
        select_audit_for_update=[[audit]],
    )
    replay = finance.ProductFinanceStore(
        replay_connection
    ).release_eligible_commissions(
        action_id="release-action-1",
        actor_user_id="finance-worker",
        agent_id="agent-1",
        min_age_days=7,
        limit=10,
    )
    assert replay.idempotent is True
    assert replay.record == result_payload
    assert [call[0] for call in replay_connection.calls] == [
        "lock_release_action",
        "select_audit_for_update",
    ]

    drift_connection = ScriptedConnection(
        lock_release_action=[[{"locked": None}]],
        select_audit_for_update=[[audit]],
    )
    with pytest.raises(finance.ProductFinanceAuditConflict):
        finance.ProductFinanceStore(
            drift_connection
        ).release_eligible_commissions(
            action_id="release-action-1",
            actor_user_id="finance-worker",
            agent_id="agent-1",
            min_age_days=7,
            limit=11,
        )


@pytest.mark.parametrize(
    ("min_age_days", "limit"),
    [(-1, 10), (3651, 10), (7, 0), (7, 1001)],
)
def test_bulk_release_rejects_unbounded_inputs_before_sql(
    min_age_days: int,
    limit: int,
) -> None:
    connection = ScriptedConnection()
    with pytest.raises(finance.InvalidProductFinanceInput):
        finance.ProductFinanceStore(
            connection
        ).release_eligible_commissions(
            action_id="release-action-1",
            actor_user_id="finance-worker",
            min_age_days=min_age_days,
            limit=limit,
        )
    assert connection.calls == []


def test_partial_refund_reduces_only_future_net_commission() -> None:
    original = commission_order_row()
    adjusted = commission_order_row(
        refunded_order_amount_cents=50_000,
        reversed_commission_cents=10_000,
        refund_status="partial",
        last_refunded_at="2026-07-30T00:04:00Z",
    )
    refund = commission_refund_row()
    audit = audit_row(
        action_id="refund-action-1",
        actor_user_id="payment-worker",
        action_domain="commission",
        action="commission.refund.apply",
        target_type="commission_order",
        target_id="commission-1",
        metadata={
            "refundId": "refund-1",
            "refundAmountCents": 50_000,
            "cumulativeRefundedAmountCents": 50_000,
            "idempotencyKey": "refund-idem-1",
            "metadata": {},
        },
    )
    connection = ScriptedConnection(
        select_commission_order=[[original]],
        select_account_for_update=[[account_row()]],
        select_commission_order_for_update=[[original]],
        select_commission_refund_by_idempotency_for_update=[[]],
        select_audit_for_update=[[]],
        insert_commission_refund=[[refund]],
        apply_commission_order_refund=[[adjusted]],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).apply_commission_refund(
        refund_id="refund-1",
        commission_order_id="commission-1",
        refund_amount_cents=50_000,
        cumulative_refunded_amount_cents=50_000,
        idempotency_key="refund-idem-1",
        action_id="refund-action-1",
        actor_user_id="payment-worker",
    )

    assert result.idempotent is False
    assert result.record["commission_order"]["status"] == "eligible"
    assert (
        result.record["commission_order"]["reversed_commission_cents"]
        == 10_000
    )
    assert "increase_agent_clawback" not in [
        call[0] for call in connection.calls
    ]


def test_refund_idempotency_requires_the_exact_same_payload_and_action() -> None:
    adjusted = commission_order_row(
        refunded_order_amount_cents=50_000,
        reversed_commission_cents=10_000,
        refund_status="partial",
        last_refunded_at="2026-07-30T00:04:00Z",
    )
    existing = commission_refund_row(
        request_sha256=commission_refund_request_sha256(),
    )
    existing_audit = audit_row(
        action_id="refund-action-1",
        actor_user_id="payment-worker",
        action_domain="commission",
        action="commission.refund.apply",
        target_type="commission_order",
        target_id="commission-1",
        metadata={
            "refundId": "refund-1",
            "refundAmountCents": 50_000,
            "cumulativeRefundedAmountCents": 50_000,
            "idempotencyKey": "refund-idem-1",
            "metadata": {},
        },
    )
    exact_connection = ScriptedConnection(
        select_commission_order=[[adjusted]],
        select_account_for_update=[[account_row()]],
        select_commission_order_for_update=[[adjusted]],
        select_commission_refund_by_idempotency_for_update=[[existing]],
        select_audit_for_update=[[existing_audit]],
    )
    exact = finance.ProductFinanceStore(
        exact_connection
    ).apply_commission_refund(
        refund_id="refund-1",
        commission_order_id="commission-1",
        refund_amount_cents=50_000,
        cumulative_refunded_amount_cents=50_000,
        idempotency_key="refund-idem-1",
        action_id="refund-action-1",
        actor_user_id="payment-worker",
    )
    assert exact.idempotent is True
    assert "apply_commission_order_refund" not in [
        call[0] for call in exact_connection.calls
    ]

    drift_connection = ScriptedConnection(
        select_commission_order=[[adjusted]],
        select_account_for_update=[[account_row()]],
        select_commission_order_for_update=[[adjusted]],
        select_commission_refund_by_idempotency_for_update=[[existing]],
    )
    with pytest.raises(finance.ProductFinanceConflict, match="content changed"):
        finance.ProductFinanceStore(
            drift_connection
        ).apply_commission_refund(
            refund_id="refund-1",
            commission_order_id="commission-1",
            refund_amount_cents=49_999,
            cumulative_refunded_amount_cents=50_000,
            idempotency_key="refund-idem-1",
            action_id="refund-action-1",
            actor_user_id="payment-worker",
        )
    assert drift_connection.rollbacks == 1


def test_settled_refund_creates_audited_clawback_and_withdrawal_liability() -> None:
    settled = commission_order_row(
        status="settled",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
        settled_at="2026-07-30T00:03:00Z",
    )
    adjusted = commission_order_row(
        status="settled",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
        settled_at="2026-07-30T00:03:00Z",
        refunded_order_amount_cents=100_000,
        reversed_commission_cents=20_000,
        refund_status="full",
        last_refunded_at="2026-07-30T00:04:00Z",
    )
    refund = commission_refund_row(
        refund_amount_cents=100_000,
        requested_cumulative_refunded_amount_cents=100_000,
        cumulative_refunded_amount_cents=100_000,
        commission_reversal_delta_cents=20_000,
        cumulative_reversed_commission_cents=20_000,
        account_clawback_delta_cents=20_000,
        order_status_before="settled",
        order_status_after="settled",
        settlement_id="settlement-1",
    )
    before_account = account_row(
        earned_cents=40_000,
        withdrawn_cents=30_000,
        version=4,
    )
    after_account = account_row(
        earned_cents=40_000,
        clawback_cents=20_000,
        withdrawn_cents=30_000,
        version=5,
    )
    audit = audit_row(
        action_id="refund-action-1",
        actor_user_id="payment-worker",
        action_domain="commission",
        action="commission.refund.apply",
        target_type="commission_order",
        target_id="commission-1",
        metadata={
            "refundId": "refund-1",
            "refundAmountCents": 100_000,
            "cumulativeRefundedAmountCents": 100_000,
            "idempotencyKey": "refund-idem-1",
            "metadata": {},
        },
    )
    connection = ScriptedConnection(
        select_commission_order=[[settled]],
        select_account_for_update=[[before_account]],
        select_commission_order_for_update=[[settled]],
        select_commission_refund_by_idempotency_for_update=[[]],
        select_audit_for_update=[[]],
        insert_commission_refund=[[refund]],
        apply_commission_order_refund=[[adjusted]],
        increase_agent_clawback=[[after_account]],
        insert_ledger=[
            [
                ledger_row(
                    entry_kind="commission_clawback",
                    source_type="commission_refund",
                    source_id="refund-1",
                    clawback_delta_cents=20_000,
                )
            ]
        ],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).apply_commission_refund(
        refund_id="refund-1",
        commission_order_id="commission-1",
        refund_amount_cents=100_000,
        cumulative_refunded_amount_cents=100_000,
        idempotency_key="refund-idem-1",
        action_id="refund-action-1",
        actor_user_id="payment-worker",
    )

    balance = finance.withdrawable_balance(
        result.record["finance_account"]
    )
    assert balance["available_cents"] == 0
    assert balance["liability_cents"] == 10_000
    assert [call[0] for call in connection.calls][-2:] == [
        "insert_ledger",
        "insert_audit",
    ]


def test_incremental_refund_cannot_exceed_original_order_amount() -> None:
    partially_refunded = commission_order_row(
        refunded_order_amount_cents=60_000,
        reversed_commission_cents=12_000,
        refund_status="partial",
        last_refunded_at="2026-07-30T00:04:00Z",
    )
    connection = ScriptedConnection(
        select_commission_order=[[partially_refunded]],
        select_account_for_update=[[account_row()]],
        select_commission_order_for_update=[[partially_refunded]],
        select_commission_refund_by_idempotency_for_update=[[]],
        select_audit_for_update=[[]],
    )

    with pytest.raises(finance.ProductFinanceConflict, match="exceed"):
        finance.ProductFinanceStore(
            connection
        ).apply_commission_refund(
            refund_id="refund-2",
            commission_order_id="commission-1",
            refund_amount_cents=60_000,
            idempotency_key="refund-idem-2",
            action_id="refund-action-2",
            actor_user_id="payment-worker",
        )

    assert connection.rollbacks == 1
    assert "insert_commission_refund" not in [
        call[0] for call in connection.calls
    ]


def test_commission_settlement_claims_every_order_and_checks_rowcount() -> None:
    first = commission_order_row("commission-1")
    second = commission_order_row("commission-2")
    connection = ScriptedConnection(
        select_settlement_by_idempotency_for_update=[[], []],
        select_account_for_update=[[account_row()]],
        select_orders_for_settlement=[[first, second]],
        insert_settlement=[[settlement_row()]],
        claim_commission_orders=[
            [{"id": "commission-1"}, {"id": "commission-2"}]
        ],
        insert_settlement_item=[[], []],
    )

    result = finance.ProductFinanceStore(
        connection
    ).create_commission_settlement(**settlement_create_kwargs())

    assert result.created is True
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert [call[0] for call in connection.calls] == [
        "select_settlement_by_idempotency_for_update",
        "select_account_for_update",
        "select_settlement_by_idempotency_for_update",
        "select_orders_for_settlement",
        "insert_settlement",
        "claim_commission_orders",
        "insert_settlement_item",
        "insert_settlement_item",
    ]


def test_partial_commission_claim_rolls_back_instead_of_empty_settlement() -> None:
    first = commission_order_row("commission-1")
    second = commission_order_row("commission-2")
    connection = ScriptedConnection(
        select_settlement_by_idempotency_for_update=[[], []],
        select_account_for_update=[[account_row()]],
        select_orders_for_settlement=[[first, second]],
        insert_settlement=[[settlement_row()]],
        claim_commission_orders=[[{"id": "commission-1"}]],
    )

    with pytest.raises(finance.ProductFinanceConflict, match="claim count"):
        finance.ProductFinanceStore(
            connection
        ).create_commission_settlement(**settlement_create_kwargs())

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert "insert_settlement_item" not in [
        call[0] for call in connection.calls
    ]


@pytest.mark.parametrize("order_ids", [[], ["commission-1", "commission-1"]])
def test_empty_or_duplicate_settlement_inputs_fail_before_sql(
    order_ids: list[str],
) -> None:
    connection = ScriptedConnection()
    values = settlement_create_kwargs()
    values["commission_order_ids"] = order_ids

    with pytest.raises(finance.InvalidProductFinanceInput):
        finance.ProductFinanceStore(
            connection
        ).create_commission_settlement(**values)

    assert connection.calls == []
    assert connection.rollbacks == 1


def test_paid_settlement_locks_account_before_orders_and_credits_once() -> None:
    pending = settlement_row(status="processing", version=1)
    first = commission_order_row(
        "commission-1",
        status="claimed",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
    )
    second = commission_order_row(
        "commission-2",
        status="claimed",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
    )
    paid = settlement_row(
        status="paid",
        version=2,
        paid_at="2026-07-30T00:05:00Z",
    )
    audit = audit_row(
        action_id="settlement-paid-action",
        action_domain="commission",
        action="settlement.status.paid",
        target_type="commission_settlement",
        target_id="settlement-1",
        metadata={"targetStatus": "paid", "metadata": {}},
    )
    connection = ScriptedConnection(
        select_settlement_for_update=[[pending]],
        select_audit_for_update=[[]],
        select_account_for_update=[
            [account_row(earned_cents=0, version=0)]
        ],
        select_settlement_orders_for_update=[[first, second]],
        mark_settlement_orders_paid=[
            [{"id": "commission-1"}, {"id": "commission-2"}]
        ],
        credit_agent_earnings=[
            [account_row(earned_cents=40_000, version=1)]
        ],
        insert_ledger=[
            [
                ledger_row(
                    entry_kind="commission_credit",
                    source_type="settlement",
                    source_id="settlement-1",
                    earned_delta_cents=40_000,
                )
            ]
        ],
        update_settlement_status=[[paid]],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).transition_commission_settlement(
        settlement_id="settlement-1",
        target_status="paid",
        action_id="settlement-paid-action",
        actor_user_id="admin-1",
    )

    assert result.idempotent is False
    names = [call[0] for call in connection.calls]
    assert names.index("select_account_for_update") < names.index(
        "select_settlement_orders_for_update"
    )
    assert result.record["finance_account"]["earned_cents"] == 40_000
    assert connection.commits == 1


def test_paid_settlement_requires_all_frozen_rows_to_update() -> None:
    pending = settlement_row(status="processing", version=1)
    first = commission_order_row(
        "commission-1",
        status="claimed",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
    )
    second = commission_order_row(
        "commission-2",
        status="claimed",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
    )
    connection = ScriptedConnection(
        select_settlement_for_update=[[pending]],
        select_audit_for_update=[[]],
        select_account_for_update=[[account_row()]],
        select_settlement_orders_for_update=[[first, second]],
        mark_settlement_orders_paid=[[{"id": "commission-1"}]],
    )

    with pytest.raises(finance.ProductFinanceConflict, match="completion count"):
        finance.ProductFinanceStore(
            connection
        ).transition_commission_settlement(
            settlement_id="settlement-1",
            target_status="paid",
            action_id="settlement-paid-action",
            actor_user_id="admin-1",
        )

    assert connection.rollbacks == 1
    assert "credit_agent_earnings" not in [
        call[0] for call in connection.calls
    ]


def test_paid_settlement_nets_refunds_that_arrived_after_claim() -> None:
    processing = settlement_row(
        status="processing",
        version=1,
        total_order_amount_cents=100_000,
        total_commission_amount_cents=20_000,
        order_count=1,
    )
    claimed = commission_order_row(
        status="claimed",
        settlement_id="settlement-1",
        claimed_at="2026-07-30T00:02:00Z",
        refunded_order_amount_cents=50_000,
        reversed_commission_cents=10_000,
        refund_status="partial",
        last_refunded_at="2026-07-30T00:03:00Z",
    )
    paid = settlement_row(
        status="paid",
        version=2,
        total_order_amount_cents=100_000,
        total_commission_amount_cents=20_000,
        order_count=1,
        paid_at="2026-07-30T00:05:00Z",
    )
    audit = audit_row(
        action_id="settlement-paid-action",
        action_domain="commission",
        action="settlement.status.paid",
        target_type="commission_settlement",
        target_id="settlement-1",
        metadata={"targetStatus": "paid", "metadata": {}},
    )
    connection = ScriptedConnection(
        select_settlement_for_update=[[processing]],
        select_audit_for_update=[[]],
        select_account_for_update=[
            [account_row(earned_cents=0, version=0)]
        ],
        select_settlement_orders_for_update=[[claimed]],
        mark_settlement_orders_paid=[[{"id": "commission-1"}]],
        credit_agent_earnings=[
            [account_row(earned_cents=20_000, version=1)]
        ],
        increase_agent_clawback=[
            [
                account_row(
                    earned_cents=20_000,
                    clawback_cents=10_000,
                    version=2,
                )
            ]
        ],
        insert_ledger=[
            [
                ledger_row(
                    entry_kind="commission_credit",
                    source_type="settlement",
                    source_id="settlement-1",
                    earned_delta_cents=20_000,
                )
            ],
            [
                ledger_row(
                    entry_kind="commission_clawback",
                    source_type="settlement",
                    source_id="settlement-1",
                    clawback_delta_cents=10_000,
                )
            ],
        ],
        update_settlement_status=[[paid]],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(
        connection
    ).transition_commission_settlement(
        settlement_id="settlement-1",
        target_status="paid",
        action_id="settlement-paid-action",
        actor_user_id="admin-1",
    )

    assert result.record["finance_account"]["earned_cents"] == 20_000
    assert result.record["finance_account"]["clawback_cents"] == 10_000
    assert finance.withdrawable_balance(
        result.record["finance_account"]
    )["available_cents"] == 10_000
    assert [
        parameters[6:8]
        for name, _sql, parameters in connection.calls
        if name == "insert_ledger"
    ] == [(20_000, 0), (0, 10_000)]


def test_withdrawal_reserves_balance_while_holding_agent_lock() -> None:
    inserted = withdrawal_row()
    reserved = account_row(reserved_cents=30_000, version=2)
    connection = ScriptedConnection(
        select_withdrawal_by_idempotency_for_update=[[], []],
        select_account_for_update=[[account_row()]],
        insert_withdrawal=[[inserted]],
        reserve_withdrawal=[[reserved]],
        insert_ledger=[
            [
                ledger_row(
                    entry_kind="withdrawal_reserve",
                    source_type="withdrawal",
                    source_id="withdrawal-1",
                    reserved_delta_cents=30_000,
                )
            ]
        ],
    )

    result = finance.ProductFinanceStore(connection).create_withdrawal(
        **withdrawal_create_kwargs()
    )

    assert result.created is True
    assert result.record["finance_account"]["reserved_cents"] == 30_000
    assert [call[0] for call in connection.calls].index(
        "select_account_for_update"
    ) < [call[0] for call in connection.calls].index("insert_withdrawal")
    assert connection.commits == 1


def test_withdrawal_rejects_insufficient_balance_before_insert() -> None:
    connection = ScriptedConnection(
        select_withdrawal_by_idempotency_for_update=[[], []],
        select_account_for_update=[[account_row(earned_cents=20_000)]],
    )

    with pytest.raises(finance.InsufficientWithdrawableBalance) as error:
        finance.ProductFinanceStore(connection).create_withdrawal(
            **withdrawal_create_kwargs()
        )

    assert error.value.available_cents == 20_000
    assert connection.rollbacks == 1
    assert "insert_withdrawal" not in [call[0] for call in connection.calls]


def test_approved_withdrawal_can_be_paid_and_audited_atomically() -> None:
    approved = withdrawal_row(
        status="approved",
        version=1,
        approved_at="2026-07-30T00:04:00Z",
    )
    paid = withdrawal_row(
        status="paid",
        version=2,
        approved_at="2026-07-30T00:04:00Z",
        paid_at="2026-07-30T00:05:00Z",
    )
    paid_account = account_row(
        reserved_cents=0,
        withdrawn_cents=30_000,
        version=3,
    )
    audit = audit_row(
        action_id="withdrawal-paid-action",
        action_domain="withdrawal",
        action="withdrawal.status.paid",
        target_type="agent_withdrawal",
        target_id="withdrawal-1",
        metadata={"targetStatus": "paid", "metadata": {}},
    )
    connection = ScriptedConnection(
        select_withdrawal_for_update=[[approved]],
        select_audit_for_update=[[]],
        select_account_for_update=[
            [account_row(reserved_cents=30_000, version=2)]
        ],
        pay_withdrawal=[[paid_account]],
        insert_ledger=[
            [
                ledger_row(
                    entry_kind="withdrawal_paid",
                    source_type="withdrawal",
                    source_id="withdrawal-1",
                    reserved_delta_cents=-30_000,
                    withdrawn_delta_cents=30_000,
                )
            ]
        ],
        update_withdrawal_status=[[paid]],
        insert_audit=[[audit]],
    )

    result = finance.ProductFinanceStore(connection).review_withdrawal(
        withdrawal_id="withdrawal-1",
        target_status="paid",
        action_id="withdrawal-paid-action",
        actor_user_id="admin-1",
    )

    assert result.idempotent is False
    assert result.record["status"] == "paid"
    assert result.record["finance_account"]["withdrawn_cents"] == 30_000
    assert connection.commits == 1


def test_withdrawal_state_machine_is_fail_closed() -> None:
    connection = ScriptedConnection(
        select_withdrawal_for_update=[[withdrawal_row(status="pending")]],
        select_audit_for_update=[[]],
    )

    with pytest.raises(finance.ProductFinanceStateConflict):
        finance.ProductFinanceStore(connection).review_withdrawal(
            withdrawal_id="withdrawal-1",
            target_status="paid",
            action_id="withdrawal-paid-action",
            actor_user_id="admin-1",
        )

    assert connection.rollbacks == 1
    assert "select_account_for_update" not in [
        call[0] for call in connection.calls
    ]


def test_review_action_replay_is_idempotent_and_does_not_move_balance_twice() -> None:
    request_metadata = {"targetStatus": "paid", "metadata": {}}
    existing_audit = audit_row(
        action_id="withdrawal-paid-action",
        action_domain="withdrawal",
        action="withdrawal.status.paid",
        target_type="agent_withdrawal",
        target_id="withdrawal-1",
        metadata=request_metadata,
    )
    connection = ScriptedConnection(
        select_withdrawal_for_update=[
            [
                withdrawal_row(
                    status="paid",
                    version=2,
                    approved_at="2026-07-30T00:04:00Z",
                    paid_at="2026-07-30T00:05:00Z",
                )
            ]
        ],
        select_audit_for_update=[[existing_audit]],
    )

    result = finance.ProductFinanceStore(connection).review_withdrawal(
        withdrawal_id="withdrawal-1",
        target_status="paid",
        action_id="withdrawal-paid-action",
        actor_user_id="admin-1",
    )

    assert result.idempotent is True
    assert [call[0] for call in connection.calls] == [
        "select_withdrawal_for_update",
        "select_audit_for_update",
    ]


def test_withdrawable_balance_detects_corrupt_account_invariant() -> None:
    assert finance.withdrawable_balance(account_row())["available_cents"] == 40_000
    legacy = account_row()
    legacy.pop("clawback_cents")
    assert finance.withdrawable_balance(legacy)["clawback_cents"] == 0
    debt = finance.withdrawable_balance(
        account_row(
            earned_cents=40_000,
            clawback_cents=20_000,
            withdrawn_cents=30_000,
        )
    )
    assert debt["available_cents"] == 0
    assert debt["liability_cents"] == 10_000
    with pytest.raises(finance.ProductFinanceConflict):
        finance.withdrawable_balance(
            account_row(
                earned_cents=10_000,
                reserved_cents=8_000,
                withdrawn_cents=3_000,
            )
        )
    with pytest.raises(finance.ProductFinanceConflict):
        finance.withdrawable_balance(
            account_row(earned_cents=10_000, clawback_cents=10_001)
        )


def test_finance_lists_apply_exact_agent_status_and_limit_filters() -> None:
    commission = commission_order_row(status="settled")
    refund = commission_refund_row()
    settlement = settlement_row(status="paid")
    withdrawal = withdrawal_row(status="approved")
    connection = ScriptedConnection(
        list_commission_orders=[[commission]],
        list_commission_refunds=[[refund]],
        list_settlements=[[settlement]],
        list_withdrawals=[[withdrawal]],
    )
    store = finance.ProductFinanceStore(connection)

    assert store.list_commission_orders(
        agent_id="agent-1",
        status="SETTLED",
        limit=17,
    ) == [commission]
    assert store.list_commission_refunds(
        agent_id="agent-1",
        commission_order_id="commission-1",
        limit=19,
    ) == [refund]
    assert store.list_settlements(
        agent_id="agent-1",
        status="PAID",
        limit=13,
    ) == [settlement]
    assert store.list_withdrawals(
        agent_id="agent-1",
        status="APPROVED",
        limit=11,
    ) == [withdrawal]

    assert [
        (name, parameters)
        for name, _sql, parameters in connection.calls
    ] == [
        (
            "list_commission_orders",
            ("agent-1", "agent-1", "settled", "settled", 17),
        ),
        (
            "list_commission_refunds",
            ("agent-1", "agent-1", "commission-1", "commission-1", 19),
        ),
        (
            "list_settlements",
            ("agent-1", "agent-1", "paid", "paid", 13),
        ),
        (
            "list_withdrawals",
            ("agent-1", "agent-1", "approved", "approved", 11),
        ),
    ]
    assert connection.commits == 4
    assert connection.rollbacks == 0


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("list_commission_orders", {"status": "invented"}),
        ("list_settlements", {"status": "invented"}),
        ("list_withdrawals", {"status": "invented"}),
        ("list_commission_refunds", {"limit": 0}),
        (
            "list_commission_refunds",
            {"commission_order_id": "contains whitespace"},
        ),
        ("list_commission_orders", {"limit": 0}),
        ("list_settlements", {"limit": 1001}),
        ("list_withdrawals", {"agent_id": "contains whitespace"}),
    ],
)
def test_finance_lists_reject_invalid_filters_before_sql(
    method_name: str,
    kwargs: dict[str, Any],
) -> None:
    connection = ScriptedConnection()
    method = getattr(finance.ProductFinanceStore(connection), method_name)

    with pytest.raises(finance.InvalidProductFinanceInput):
        method(**kwargs)

    assert connection.calls == []
    assert connection.commits == 0
    assert connection.rollbacks == 0


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_finance_protocol_and_concurrency() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_product_finance_{uuid4().hex}"

    admin_connection = psycopg.connect(dsn, autocommit=True)
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin_connection.close()

    def connect() -> Any:
        connection = psycopg.connect(dsn, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))

        store = finance.ProductFinanceStore(connection)
        store.ensure_agent_account(agent_id="agent-real-1")
        for index, commission_cents in (
            (1, 20_000),
            (2, 20_000),
            (3, 10_000),
        ):
            store.record_commission_order(
                commission_order_id=f"commission-real-{index}",
                source_order_id=f"payment-real-{index}",
                agent_id="agent-real-1",
                customer_user_id=f"customer-real-{index}",
                order_amount_cents=100_000,
                commission_amount_cents=commission_cents,
                commission_rate_bps=commission_cents // 10,
            )

        settlement_barrier = threading.Barrier(2)
        settlement_results: list[tuple[str, Any]] = []

        def race_settlement(index: int) -> None:
            race_connection = connect()
            try:
                settlement_barrier.wait(timeout=10)
                result = finance.ProductFinanceStore(
                    race_connection
                ).create_commission_settlement(
                    settlement_id=f"settlement-race-{index}",
                    agent_id="agent-real-1",
                    idempotency_key=f"settlement-race-idem-{index}",
                    commission_order_ids=["commission-real-3"],
                    settlement_account={"type": "alipay", "tail": "0001"},
                    settlement_no=f"SET-RACE-{index}",
                )
                settlement_results.append(("success", result.record["id"]))
            except Exception as exc:  # noqa: BLE001 - assertion inspects type
                settlement_results.append(("error", exc))
            finally:
                race_connection.close()

        settlement_threads = [
            threading.Thread(target=race_settlement, args=(index,))
            for index in (1, 2)
        ]
        for thread in settlement_threads:
            thread.start()
        for thread in settlement_threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        settlement_successes = [
            value for kind, value in settlement_results if kind == "success"
        ]
        settlement_errors = [
            value for kind, value in settlement_results if kind == "error"
        ]
        assert len(settlement_successes) == 1
        assert len(settlement_errors) == 1
        assert isinstance(
            settlement_errors[0],
            finance.ProductFinanceConflict,
        )

        settlement = store.create_commission_settlement(
            settlement_id="settlement-real-main",
            agent_id="agent-real-1",
            idempotency_key="settlement-real-main-idem",
            commission_order_ids=[
                "commission-real-1",
                "commission-real-2",
            ],
            settlement_account={"type": "alipay", "tail": "0001"},
            settlement_no="SET-REAL-MAIN",
        )
        assert settlement.created is True
        store.transition_commission_settlement(
            settlement_id="settlement-real-main",
            target_status="processing",
            action_id="action-settlement-processing-real",
            actor_user_id="admin-real-1",
        )
        store.transition_commission_settlement(
            settlement_id="settlement-real-main",
            target_status="paid",
            action_id="action-settlement-paid-real",
            actor_user_id="admin-real-1",
        )
        assert store.get_withdrawable_balance(
            agent_id="agent-real-1"
        )["available_cents"] == 40_000

        withdrawal_barrier = threading.Barrier(2)
        withdrawal_results: list[tuple[str, Any]] = []

        def race_withdrawal(index: int) -> None:
            race_connection = connect()
            try:
                withdrawal_barrier.wait(timeout=10)
                result = finance.ProductFinanceStore(
                    race_connection
                ).create_withdrawal(
                    withdrawal_id=f"withdrawal-race-{index}",
                    agent_id="agent-real-1",
                    idempotency_key=f"withdrawal-race-idem-{index}",
                    amount_cents=30_000,
                    account_snapshot={
                        "type": "bank",
                        "tail": f"{index:04d}",
                    },
                )
                withdrawal_results.append(("success", result.record["id"]))
            except Exception as exc:  # noqa: BLE001 - assertion inspects type
                withdrawal_results.append(("error", exc))
            finally:
                race_connection.close()

        withdrawal_threads = [
            threading.Thread(target=race_withdrawal, args=(index,))
            for index in (1, 2)
        ]
        for thread in withdrawal_threads:
            thread.start()
        for thread in withdrawal_threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        withdrawal_successes = [
            value for kind, value in withdrawal_results if kind == "success"
        ]
        withdrawal_errors = [
            value for kind, value in withdrawal_results if kind == "error"
        ]
        assert len(withdrawal_successes) == 1
        assert len(withdrawal_errors) == 1
        assert isinstance(
            withdrawal_errors[0],
            finance.InsufficientWithdrawableBalance,
        )

        winning_withdrawal = str(withdrawal_successes[0])
        store.review_withdrawal(
            withdrawal_id=winning_withdrawal,
            target_status="approved",
            action_id="action-withdrawal-approved-real",
            actor_user_id="finance-real-1",
        )
        store.review_withdrawal(
            withdrawal_id=winning_withdrawal,
            target_status="paid",
            action_id="action-withdrawal-paid-real",
            actor_user_id="finance-real-1",
        )
        balance = store.get_withdrawable_balance(agent_id="agent-real-1")
        assert balance == {
            "agent_id": "agent-real-1",
            "currency": "CNY",
            "status": "active",
            "earned_cents": 40_000,
            "clawback_cents": 0,
            "reserved_cents": 0,
            "withdrawn_cents": 30_000,
            "available_cents": 10_000,
            "liability_cents": 0,
            "version": balance["version"],
        }
        assert [
            row["id"]
            for row in store.list_commission_orders(
                agent_id="agent-real-1",
                status="settled",
            )
        ] == ["commission-real-1", "commission-real-2"]
        assert len(
            store.list_settlements(agent_id="agent-real-1")
        ) == 2
        assert [
            row["id"]
            for row in store.list_withdrawals(
                agent_id="agent-real-1",
                status="paid",
            )
        ] == [winning_withdrawal]

        first_audit = store.record_audit(
            action_id="action-payment-reconcile-real",
            actor_user_id="finance-real-1",
            action_domain="payment",
            action="payment.reconcile",
            target_type="payment_order",
            target_id="payment-real-1",
            metadata={"providerEventId": "provider-event-real-1"},
        )
        replay_audit = store.record_audit(
            action_id="action-payment-reconcile-real",
            actor_user_id="finance-real-1",
            action_domain="payment",
            action="payment.reconcile",
            target_type="payment_order",
            target_id="payment-real-1",
            metadata={"providerEventId": "provider-event-real-1"},
        )
        assert first_audit.created is True
        assert replay_audit.created is False
        with pytest.raises(finance.ProductFinanceAuditConflict):
            store.record_audit(
                action_id="action-payment-reconcile-real",
                actor_user_id="finance-real-1",
                action_domain="payment",
                action="payment.reconcile",
                target_type="payment_order",
                target_id="payment-real-1",
                metadata={"providerEventId": "provider-event-changed"},
            )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(order_count), 0)
                FROM product_commission_settlements
                """
            )
            settlement_count, frozen_order_count = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM product_commission_settlement_items
                """
            )
            item_count = cursor.fetchone()[0]
        connection.commit()
        assert settlement_count == 2
        assert frozen_order_count == item_count == 3
    finally:
        connection.rollback()
        connection.close()
        cleanup_connection = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup_connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cleanup_connection.close()


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_commission_refund_lifecycle_and_concurrency() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_product_finance_refund_{uuid4().hex}"

    admin_connection = psycopg.connect(dsn, autocommit=True)
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin_connection.close()

    def connect() -> Any:
        connection = psycopg.connect(dsn, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        return connection

    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))

        store = finance.ProductFinanceStore(connection)
        store.ensure_agent_account(agent_id="agent-refund-real")
        for suffix in ("old", "new"):
            store.record_commission_order(
                commission_order_id=f"commission-release-real-{suffix}",
                source_order_id=f"payment-release-real-{suffix}",
                agent_id="agent-refund-real",
                customer_user_id=f"customer-release-real-{suffix}",
                order_amount_cents=10_000,
                commission_amount_cents=2_000,
                commission_rate_bps=2000,
                status="pending",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE product_commission_orders
                SET created_at = CURRENT_TIMESTAMP - INTERVAL '8 days',
                    updated_at = CURRENT_TIMESTAMP - INTERVAL '8 days'
                WHERE id = 'commission-release-real-old'
                """
            )
        connection.commit()
        release = store.release_eligible_commissions(
            action_id="action-release-eligible-real",
            actor_user_id="finance-worker-real",
            agent_id="agent-refund-real",
            min_age_days=7,
            limit=10,
        )
        replayed_release = store.release_eligible_commissions(
            action_id="action-release-eligible-real",
            actor_user_id="finance-worker-real",
            agent_id="agent-refund-real",
            min_age_days=7,
            limit=10,
        )
        assert release.record["orderIds"] == [
            "commission-release-real-old"
        ]
        assert replayed_release.idempotent is True
        assert store.get_commission_order(
            commission_order_id="commission-release-real-old"
        )["status"] == "eligible"
        assert store.get_commission_order(
            commission_order_id="commission-release-real-new"
        )["status"] == "pending"
        store.create_commission_settlement(
            settlement_id="settlement-retry-real-1",
            agent_id="agent-refund-real",
            idempotency_key="settlement-retry-idem-real-1",
            commission_order_ids=["commission-release-real-old"],
            settlement_account={"type": "alipay", "tail": "0001"},
            settlement_no="SET-RETRY-REAL-1",
        )
        store.transition_commission_settlement(
            settlement_id="settlement-retry-real-1",
            target_status="failed",
            action_id="action-settlement-retry-failed-real-1",
            actor_user_id="finance-worker-real",
        )
        retried_settlement = store.create_commission_settlement(
            settlement_id="settlement-retry-real-2",
            agent_id="agent-refund-real",
            idempotency_key="settlement-retry-idem-real-2",
            commission_order_ids=["commission-release-real-old"],
            settlement_account={"type": "alipay", "tail": "0001"},
            settlement_no="SET-RETRY-REAL-2",
        )
        assert retried_settlement.created is True

        store.record_commission_order(
            commission_order_id="commission-refund-real-1",
            source_order_id="payment-refund-real-1",
            agent_id="agent-refund-real",
            customer_user_id="customer-refund-real-1",
            order_amount_cents=100_000,
            commission_amount_cents=20_000,
            commission_rate_bps=2000,
            status="pending",
        )
        eligible = store.transition_commission_order(
            commission_order_id="commission-refund-real-1",
            target_status="eligible",
            action_id="action-commission-eligible-real",
            actor_user_id="finance-worker-real",
        )
        replayed_eligible = store.transition_commission_order(
            commission_order_id="commission-refund-real-1",
            target_status="eligible",
            action_id="action-commission-eligible-real",
            actor_user_id="finance-worker-real",
        )
        assert eligible.idempotent is False
        assert replayed_eligible.idempotent is True

        first_refund = store.apply_commission_refund(
            refund_id="commission-refund-event-real-1",
            commission_order_id="commission-refund-real-1",
            refund_amount_cents=25_000,
            cumulative_refunded_amount_cents=25_000,
            idempotency_key="commission-refund-idem-real-1",
            action_id="action-commission-refund-real-1",
            actor_user_id="payment-worker-real",
        )
        replayed_refund = store.apply_commission_refund(
            refund_id="commission-refund-event-real-1",
            commission_order_id="commission-refund-real-1",
            refund_amount_cents=25_000,
            cumulative_refunded_amount_cents=25_000,
            idempotency_key="commission-refund-idem-real-1",
            action_id="action-commission-refund-real-1",
            actor_user_id="payment-worker-real",
        )
        assert first_refund.idempotent is False
        assert replayed_refund.idempotent is True
        assert (
            first_refund.record["commission_order"][
                "reversed_commission_cents"
            ]
            == 5_000
        )
        with pytest.raises(finance.ProductFinanceConflict):
            store.apply_commission_refund(
                refund_id="commission-refund-event-real-1",
                commission_order_id="commission-refund-real-1",
                refund_amount_cents=25_001,
                cumulative_refunded_amount_cents=25_000,
                idempotency_key="commission-refund-idem-real-1",
                action_id="action-commission-refund-real-1",
                actor_user_id="payment-worker-real",
            )

        settlement = store.create_commission_settlement(
            settlement_id="settlement-refund-real-1",
            agent_id="agent-refund-real",
            idempotency_key="settlement-refund-idem-real-1",
            commission_order_ids=["commission-refund-real-1"],
            settlement_account={"type": "alipay", "tail": "0001"},
            settlement_no="SET-REFUND-REAL-1",
        )
        assert settlement.record["total_commission_amount_cents"] == 15_000
        frozen = store.get_settlement(
            settlement_id="settlement-refund-real-1"
        )["items"][0]
        assert frozen["original_commission_amount_cents"] == 20_000
        assert frozen["reversed_commission_amount_cents"] == 5_000
        assert frozen["commission_amount_cents"] == 15_000

        claimed_refund = store.apply_commission_refund(
            refund_id="commission-refund-event-real-2",
            commission_order_id="commission-refund-real-1",
            refund_amount_cents=25_000,
            cumulative_refunded_amount_cents=50_000,
            idempotency_key="commission-refund-idem-real-2",
            action_id="action-commission-refund-real-2",
            actor_user_id="payment-worker-real",
        )
        assert (
            claimed_refund.record["commission_order"]["status"]
            == "claimed"
        )
        store.transition_commission_settlement(
            settlement_id="settlement-refund-real-1",
            target_status="processing",
            action_id="action-settlement-processing-refund-real-1",
            actor_user_id="finance-worker-real",
        )
        store.transition_commission_settlement(
            settlement_id="settlement-refund-real-1",
            target_status="paid",
            action_id="action-settlement-paid-refund-real-1",
            actor_user_id="finance-worker-real",
        )
        balance = store.get_agent_balance(agent_id="agent-refund-real")
        assert balance["earned_cents"] == 15_000
        assert balance["clawback_cents"] == 5_000
        assert balance["available_cents"] == 10_000

        withdrawal = store.create_withdrawal(
            withdrawal_id="withdrawal-refund-real-1",
            agent_id="agent-refund-real",
            idempotency_key="withdrawal-refund-idem-real-1",
            amount_cents=10_000,
            account_snapshot={"type": "bank", "tail": "0001"},
        )
        store.review_withdrawal(
            withdrawal_id=withdrawal.record["id"],
            target_status="approved",
            action_id="action-withdrawal-approved-refund-real-1",
            actor_user_id="finance-worker-real",
        )
        store.review_withdrawal(
            withdrawal_id=withdrawal.record["id"],
            target_status="paid",
            action_id="action-withdrawal-paid-refund-real-1",
            actor_user_id="finance-worker-real",
        )

        settled_refund = store.apply_commission_refund(
            refund_id="commission-refund-event-real-3",
            commission_order_id="commission-refund-real-1",
            refund_amount_cents=50_000,
            cumulative_refunded_amount_cents=100_000,
            idempotency_key="commission-refund-idem-real-3",
            action_id="action-commission-refund-real-3",
            actor_user_id="payment-worker-real",
        )
        assert (
            settled_refund.record["commission_order"]["status"]
            == "settled"
        )
        balance = store.get_agent_balance(agent_id="agent-refund-real")
        assert balance["earned_cents"] == 15_000
        assert balance["clawback_cents"] == 15_000
        assert balance["withdrawn_cents"] == 10_000
        assert balance["available_cents"] == 0
        assert balance["liability_cents"] == 10_000
        with pytest.raises(finance.InsufficientWithdrawableBalance):
            store.create_withdrawal(
                withdrawal_id="withdrawal-refund-real-blocked",
                agent_id="agent-refund-real",
                idempotency_key="withdrawal-refund-idem-real-blocked",
                amount_cents=10_000,
                account_snapshot={"type": "bank", "tail": "0002"},
            )

        store.record_commission_order(
            commission_order_id="commission-refund-real-2",
            source_order_id="payment-refund-real-2",
            agent_id="agent-refund-real",
            customer_user_id="customer-refund-real-2",
            order_amount_cents=100_000,
            commission_amount_cents=20_000,
            commission_rate_bps=2000,
        )
        store.create_commission_settlement(
            settlement_id="settlement-refund-real-2",
            agent_id="agent-refund-real",
            idempotency_key="settlement-refund-idem-real-2",
            commission_order_ids=["commission-refund-real-2"],
            settlement_account={"type": "alipay", "tail": "0001"},
            settlement_no="SET-REFUND-REAL-2",
        )
        store.transition_commission_settlement(
            settlement_id="settlement-refund-real-2",
            target_status="paid",
            action_id="action-settlement-paid-refund-real-2",
            actor_user_id="finance-worker-real",
        )

        refund_barrier = threading.Barrier(2)
        refund_results: list[tuple[str, Any]] = []

        def race_refund(index: int) -> None:
            race_connection = connect()
            try:
                refund_barrier.wait(timeout=10)
                result = finance.ProductFinanceStore(
                    race_connection
                ).apply_commission_refund(
                    refund_id=f"commission-refund-race-{index}",
                    commission_order_id="commission-refund-real-2",
                    refund_amount_cents=60_000,
                    idempotency_key=f"commission-refund-race-idem-{index}",
                    action_id=f"action-commission-refund-race-{index}",
                    actor_user_id="payment-worker-real",
                )
                refund_results.append(("success", result.record["id"]))
            except Exception as exc:  # noqa: BLE001 - assertion inspects type
                refund_results.append(("error", exc))
            finally:
                race_connection.close()

        refund_threads = [
            threading.Thread(target=race_refund, args=(index,))
            for index in (1, 2)
        ]
        for thread in refund_threads:
            thread.start()
        for thread in refund_threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        refund_successes = [
            value for kind, value in refund_results if kind == "success"
        ]
        refund_errors = [
            value for kind, value in refund_results if kind == "error"
        ]
        assert len(refund_successes) == 1
        assert len(refund_errors) == 1
        assert isinstance(
            refund_errors[0],
            finance.ProductFinanceConflict,
        )
        raced_order = store.get_commission_order(
            commission_order_id="commission-refund-real-2"
        )
        assert raced_order["refunded_order_amount_cents"] == 60_000
        assert raced_order["reversed_commission_cents"] == 12_000
        assert len(
            store.list_commission_refunds(
                commission_order_id="commission-refund-real-2"
            )
        ) == 1
        balance = store.get_agent_balance(agent_id="agent-refund-real")
        assert balance["earned_cents"] == 35_000
        assert balance["clawback_cents"] == 27_000
        assert balance["withdrawn_cents"] == 10_000
        assert balance["available_cents"] == 0
        assert balance["liability_cents"] == 2_000

        immutable_updates = (
            """
            UPDATE product_finance_audit_events
            SET reason = 'tampered'
            WHERE action_id = 'action-commission-refund-real-3'
            """,
            """
            UPDATE product_agent_finance_ledger
            SET metadata = '{"tampered":true}'::jsonb
            WHERE entry_kind = 'commission_clawback'
            """,
            """
            DELETE FROM product_commission_refunds
            WHERE id = 'commission-refund-event-real-3'
            """,
        )
        for statement in immutable_updates:
            with pytest.raises(psycopg.Error) as error:
                with connection.cursor() as cursor:
                    cursor.execute(statement)
            assert error.value.sqlstate == "55000"
            connection.rollback()
    finally:
        connection.rollback()
        connection.close()
        cleanup_connection = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup_connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cleanup_connection.close()
