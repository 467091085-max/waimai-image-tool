from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path
from typing import Any

import growth_rules
from shared import payment_catalog
from shared import product_growth_outbox as growth_outbox
from shared import product_payment_store as payments


ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "a" * 64


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.rows: list[dict[str, Any]] = []
        self.closed = False

    def execute(self, operation: str, parameters: tuple[Any, ...] = ()) -> None:
        match = re.search(
            r"/\* (product_payment_store|product_growth_outbox):([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        namespace = match.group(1)
        operation_name = match.group(2)
        name = (
            operation_name
            if namespace == "product_payment_store"
            else f"growth_{operation_name}"
        )
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


def order_row(**overrides: Any) -> dict[str, Any]:
    snapshot = payment_catalog.package_snapshot("starter-500")
    row = {
        "id": "pay-order-1",
        "owner_user_id": "user-1",
        "provider": "alipay",
        "provider_order_id": "provider-order-1",
        "idempotency_key": "payment-idem-1",
        "package_id": snapshot["packageId"],
        "catalog_version": snapshot["catalogVersion"],
        "currency": snapshot["currency"],
        "amount_cents": snapshot["amountCents"],
        "points": snapshot["points"],
        "catalog_snapshot": snapshot,
        "catalog_snapshot_sha256": snapshot["snapshotDigest"],
        "provider_payload": {"checkout": "page-pay"},
        "status": "pending",
        "credited_points": 0,
        "refunded_amount_cents": 0,
        "refunded_points": 0,
        "credit_point_order_id": None,
        "version": 0,
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
        "paid_at": None,
        "refunded_at": None,
        "closed_at": None,
    }
    row.update(overrides)
    return row


def account_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "owner_user_id": "user-1",
        "balance_points": 100,
        "lifetime_credited_points": 100,
        "lifetime_debited_points": 0,
        "lifetime_refunded_points": 0,
        "version": 1,
        "metadata": {},
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
    }
    row.update(overrides)
    return row


def point_order_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "payment-credit-order",
        "owner_user_id": "user-1",
        "order_kind": "credit",
        "points": 500,
        "source_order_id": None,
        "source_order_kind": None,
        "job_id": None,
        "request_sha256": DIGEST_A,
        "metadata": {},
        "applied_at": "2026-07-30T00:01:00Z",
        "created_at": "2026-07-30T00:01:00Z",
        "updated_at": "2026-07-30T00:01:00Z",
    }
    row.update(overrides)
    return row


def event_content_sha256(
    *,
    event_kind: str,
    event_type: str,
    provider_event_id: str,
    amount_cents: int | None,
    payload: dict[str, Any],
) -> str:
    return payments._event_content_sha256(  # type: ignore[attr-defined]
        owner_user_id="user-1",
        provider="alipay",
        provider_order_id="provider-order-1",
        provider_event_id=provider_event_id,
        event_kind=event_kind,
        event_type=event_type,
        amount_cents=amount_cents,
        payload=payload,
    )


def event_row(
    *,
    event_kind: str = payments.EVENT_PAYMENT_SUCCEEDED,
    event_type: str = "TRADE_SUCCESS",
    provider_event_id: str = "event-paid-1",
    amount_cents: int | None = 4900,
    points_delta: int = 500,
    target_status: str = "paid",
    refunded_amount_cents_after: int = 0,
    refunded_points_after: int = 0,
    wallet_order_id: str | None = None,
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    event_payload = payload or {"tradeNo": "trade-1"}
    row = {
        "id": 1,
        "owner_user_id": "user-1",
        "order_id": "pay-order-1",
        "provider": "alipay",
        "provider_order_id": "provider-order-1",
        "provider_event_id": provider_event_id,
        "event_kind": event_kind,
        "event_type": event_type,
        "target_status": target_status,
        "amount_cents": amount_cents,
        "points_delta": points_delta,
        "refunded_amount_cents_after": refunded_amount_cents_after,
        "refunded_points_after": refunded_points_after,
        "wallet_order_id": wallet_order_id,
        "payload": event_payload,
        "content_sha256": event_content_sha256(
            event_kind=event_kind,
            event_type=event_type,
            provider_event_id=provider_event_id,
            amount_cents=amount_cents,
            payload=event_payload,
        ),
        "created_at": "2026-07-30T00:01:00Z",
    }
    row.update(overrides)
    return row


def growth_event_row(
    payment_order: dict[str, Any],
    payment_event: dict[str, Any],
) -> dict[str, Any]:
    event_kind = str(payment_event["event_kind"])
    base_payload = {
        "schemaVersion": 1,
        "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
        "orderId": str(payment_order["id"]),
        "customerId": str(payment_order["owner_user_id"]),
        "provider": str(payment_order["provider"]),
        "providerOrderId": str(payment_order["provider_order_id"]),
        "paidCents": int(payment_order["amount_cents"]),
        "points": int(payment_order["points"]),
        "catalogSnapshotSha256": str(
            payment_order["catalog_snapshot_sha256"]
        ),
    }
    if event_kind == payments.EVENT_PAYMENT_SUCCEEDED:
        event_type = growth_outbox.EVENT_FIRST_PAYMENT_REWARD
        dedupe_key = growth_outbox.stable_growth_dedupe_key(
            event_type,
            str(payment_order["id"]),
        )
        payload = {
            **base_payload,
            "requestId": str(payment_order["id"]),
        }
    else:
        event_type = growth_outbox.EVENT_PAYMENT_REFUND
        provider_event_id = str(payment_event["provider_event_id"])
        dedupe_key = growth_outbox.stable_growth_dedupe_key(
            event_type,
            str(payment_order["id"]),
            provider_event_id,
        )
        payload = {
            **base_payload,
            "providerEventId": provider_event_id,
            "requestId": provider_event_id,
            "refundCents": int(payment_event["amount_cents"]),
            "cumulativeRefundedCents": int(
                payment_event["refunded_amount_cents_after"]
            ),
            "cumulativeRefundedPoints": int(
                payment_event["refunded_points_after"]
            ),
        }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "id": growth_outbox._event_id(dedupe_key),  # type: ignore[attr-defined]
        "event_type": event_type,
        "dedupe_key": dedupe_key,
        "payload": payload,
        "payload_sha256": hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest(),
        "status": "pending",
        "max_attempts": 8,
        "attempt_count": 0,
        "available_at": "2026-07-30T00:01:00Z",
        "claimed_by": None,
        "claim_token": None,
        "claimed_until": None,
        "fence": 0,
        "result_payload": {},
        "last_error": "",
        "succeeded_at": None,
        "dead_lettered_at": None,
        "created_at": "2026-07-30T00:01:00Z",
        "updated_at": "2026-07-30T00:01:00Z",
    }


def create_order(
    store: payments.ProductPaymentStore,
    **overrides: Any,
) -> payments.PaymentOrderCreateResult:
    values = {
        "owner_user_id": "user-1",
        "provider": "alipay",
        "package_id": "starter-500",
        "idempotency_key": "payment-idem-1",
        "order_id": "pay-order-1",
        "provider_order_id": "provider-order-1",
        "provider_payload": {"checkout": "page-pay"},
    }
    values.update(overrides)
    return store.create_or_get_order(**values)


class ProductPaymentStoreTests(unittest.TestCase):
    def test_store_rejects_autocommit_connections(self) -> None:
        with self.assertRaises(payments.InvalidProductPaymentInput):
            payments.ProductPaymentStore(AutocommitConnection())

    def test_create_order_freezes_server_catalog_values(self) -> None:
        inserted = order_row()
        connection = ScriptedConnection(
            select_order_by_idempotency_for_update=[[]],
            insert_order=[[inserted]],
        )

        result = create_order(payments.ProductPaymentStore(connection))

        self.assertTrue(result.created)
        self.assertEqual(result.order["amount_cents"], 4900)
        self.assertEqual(result.order["points"], 500)
        self.assertEqual(result.order["catalog_snapshot"]["packageId"], "starter-500")
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_idempotency_for_update",
                "insert_order",
            ],
        )
        insert_params = connection.calls[1][2]
        self.assertEqual(insert_params[8], 4900)
        self.assertEqual(insert_params[9], 500)
        self.assertEqual(insert_params[11], inserted["catalog_snapshot_sha256"])
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

    def test_idempotent_order_replay_is_exact_and_provider_drift_rolls_back(self) -> None:
        existing = order_row()
        exact_connection = ScriptedConnection(
            select_order_by_idempotency_for_update=[[existing]],
        )

        exact = create_order(payments.ProductPaymentStore(exact_connection))

        self.assertFalse(exact.created)
        self.assertEqual(
            [call[0] for call in exact_connection.calls],
            ["select_order_by_idempotency_for_update"],
        )
        self.assertEqual(exact_connection.commits, 1)

        drift_connection = ScriptedConnection(
            select_order_by_idempotency_for_update=[[existing]],
        )
        with self.assertRaises(payments.ProductPaymentOrderConflict):
            create_order(
                payments.ProductPaymentStore(drift_connection),
                provider="wechat",
            )
        self.assertEqual(drift_connection.commits, 0)
        self.assertEqual(drift_connection.rollbacks, 1)

        provider_order_drift = ScriptedConnection(
            select_order_by_idempotency_for_update=[[existing]],
        )
        with self.assertRaises(payments.ProductPaymentOrderConflict):
            create_order(
                payments.ProductPaymentStore(provider_order_drift),
                provider_order_id="provider-order-drifted",
            )
        self.assertEqual(provider_order_drift.rollbacks, 1)

    def test_order_insert_race_resolves_only_the_same_frozen_request(self) -> None:
        existing = order_row()
        connection = ScriptedConnection(
            select_order_by_idempotency_for_update=[[], [existing]],
            insert_order=[[]],
        )

        result = create_order(payments.ProductPaymentStore(connection))

        self.assertFalse(result.created)
        self.assertEqual(result.order["id"], "pay-order-1")
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_idempotency_for_update",
                "insert_order",
                "select_order_by_idempotency_for_update",
            ],
        )
        self.assertEqual(connection.commits, 1)

    def test_order_replay_can_omit_already_attached_provider_payload(self) -> None:
        existing = order_row(provider_payload={"payment_url": "https://pay.test/1"})
        connection = ScriptedConnection(
            select_order_by_idempotency_for_update=[[existing]],
        )

        result = create_order(
            payments.ProductPaymentStore(connection),
            provider_payload=None,
        )

        self.assertFalse(result.created)
        self.assertEqual(
            result.order["provider_payload"],
            {"payment_url": "https://pay.test/1"},
        )
        self.assertEqual(connection.commits, 1)

    def test_get_order_and_attach_provider_payload_are_owner_safe_and_idempotent(
        self,
    ) -> None:
        pending = order_row(provider_payload={})
        attached = order_row(
            provider_payload={"payment_url": "https://pay.test/1"},
            version=1,
        )
        connection = ScriptedConnection(
            select_order_by_provider=[[pending]],
            select_order_by_id_for_update=[[pending], [attached], [attached]],
            attach_provider_payload=[[attached]],
        )
        store = payments.ProductPaymentStore(connection)

        found = store.get_order_by_provider(
            provider="alipay",
            provider_order_id="provider-order-1",
        )
        first = store.attach_provider_payload(
            owner_user_id="user-1",
            order_id="pay-order-1",
            provider_payload={"payment_url": "https://pay.test/1"},
        )
        replay = store.attach_provider_payload(
            owner_user_id="user-1",
            order_id="pay-order-1",
            provider_payload={"payment_url": "https://pay.test/1"},
        )

        self.assertEqual(found["id"], "pay-order-1")
        self.assertEqual(first["version"], 1)
        self.assertEqual(replay["provider_payload"], first["provider_payload"])
        with self.assertRaises(payments.ProductPaymentOrderConflict):
            store.attach_provider_payload(
                owner_user_id="user-1",
                order_id="pay-order-1",
                provider_payload={"payment_url": "https://pay.test/drift"},
            )
        self.assertEqual(connection.commits, 3)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_provider",
                "select_order_by_id_for_update",
                "attach_provider_payload",
                "select_order_by_id_for_update",
                "select_order_by_id_for_update",
            ],
        )

    def test_attach_provider_payload_conceals_wrong_owner(self) -> None:
        connection = ScriptedConnection(
            select_order_by_id_for_update=[[order_row()]],
        )

        with self.assertRaises(payments.ProductPaymentOrderNotFound):
            payments.ProductPaymentStore(connection).attach_provider_payload(
                owner_user_id="user-2",
                order_id="pay-order-1",
                provider_payload={"payment_url": "https://pay.test/1"},
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_payment_success_event_credits_wallet_in_one_ordered_transaction(self) -> None:
        pending = order_row()
        wallet_order_id = payments._wallet_order_id(  # type: ignore[attr-defined]
            "credit",
            pending["id"],
        )
        payload = {"tradeNo": "trade-1"}
        inserted_event = event_row(
            payload=payload,
            wallet_order_id=wallet_order_id,
        )
        credited_account = account_row(
            balance_points=600,
            lifetime_credited_points=600,
            version=2,
        )
        paid = order_row(
            status="paid",
            credited_points=500,
            credit_point_order_id=wallet_order_id,
            version=1,
            paid_at="2026-07-30T00:01:00Z",
        )
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[pending]],
            select_event_for_update=[[]],
            insert_event=[[inserted_event]],
            insert_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            insert_point_order=[
                [
                    point_order_row(
                        id=wallet_order_id,
                        request_sha256=inserted_event["content_sha256"],
                    )
                ]
            ],
            credit_point_account=[[credited_account]],
            insert_point_ledger=[[]],
            mark_order_paid=[[paid]],
            growth_insert_event=[[growth_event_row(paid, inserted_event)]],
        )
        effects: list[tuple[str, bool]] = []

        result = payments.ProductPaymentStore(connection).apply_verified_event(
            owner_user_id="user-1",
            provider="alipay",
            provider_order_id="provider-order-1",
            provider_event_id="event-paid-1",
            event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
            event_type="TRADE_SUCCESS",
            payload=payload,
            amount_cents=4900,
            transaction_effect=lambda _, current: effects.append(
                (str(current.order["status"]), current.idempotent)
            ),
        )

        self.assertEqual(effects, [("paid", False)])
        self.assertFalse(result.idempotent)
        self.assertEqual(result.points_credited, 500)
        self.assertEqual(result.points_debited, 0)
        self.assertEqual(result.order["status"], "paid")
        self.assertEqual(result.account["balance_points"], 600)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_provider_for_update",
                "select_event_for_update",
                "insert_event",
                "insert_point_account",
                "select_point_account_for_update",
                "insert_point_order",
                "credit_point_account",
                "insert_point_ledger",
                "mark_order_paid",
                "growth_insert_event",
            ],
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

    def test_growth_outbox_failure_rolls_back_payment_and_wallet_transaction(
        self,
    ) -> None:
        pending = order_row()
        wallet_order_id = payments._wallet_order_id(  # type: ignore[attr-defined]
            "credit",
            pending["id"],
        )
        payload = {"tradeNo": "trade-outbox-failure"}
        inserted_event = event_row(
            payload=payload,
            wallet_order_id=wallet_order_id,
        )
        paid = order_row(
            status="paid",
            credited_points=500,
            credit_point_order_id=wallet_order_id,
            version=1,
            paid_at="2026-07-30T00:01:00Z",
        )
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[pending]],
            select_event_for_update=[[]],
            insert_event=[[inserted_event]],
            insert_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            insert_point_order=[
                [
                    point_order_row(
                        id=wallet_order_id,
                        request_sha256=inserted_event["content_sha256"],
                    )
                ]
            ],
            credit_point_account=[
                [
                    account_row(
                        balance_points=600,
                        lifetime_credited_points=600,
                        version=2,
                    )
                ]
            ],
            insert_point_ledger=[[]],
            mark_order_paid=[[paid]],
            growth_insert_event=[
                RuntimeError("growth outbox unavailable")
            ],
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "growth outbox unavailable",
        ):
            payments.ProductPaymentStore(connection).apply_verified_event(
                owner_user_id="user-1",
                provider="alipay",
                provider_order_id="provider-order-1",
                provider_event_id="event-paid-1",
                event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
                event_type="TRADE_SUCCESS",
                payload=payload,
                amount_cents=4900,
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.calls[-1][0], "growth_insert_event")

    def test_exact_event_replay_is_idempotent_without_wallet_mutation(self) -> None:
        payload = {"tradeNo": "trade-1"}
        wallet_order_id = payments._wallet_order_id(  # type: ignore[attr-defined]
            "credit",
            "pay-order-1",
        )
        paid = order_row(
            status="paid",
            credited_points=500,
            credit_point_order_id=wallet_order_id,
            version=1,
            paid_at="2026-07-30T00:01:00Z",
        )
        existing_event = event_row(
            payload=payload,
            wallet_order_id=wallet_order_id,
        )
        wallet_metadata = {
            "catalogSnapshotSha256": paid["catalog_snapshot_sha256"],
            "eventKind": payments.EVENT_PAYMENT_SUCCEEDED,
            "paymentOrderId": paid["id"],
            "provider": paid["provider"],
            "providerEventId": "event-paid-1",
        }
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[paid]],
            select_event_for_update=[[existing_event]],
            select_point_order=[
                [
                    point_order_row(
                        id=wallet_order_id,
                        request_sha256=existing_event["content_sha256"],
                        metadata=wallet_metadata,
                    )
                ]
            ],
            growth_insert_event=[
                [growth_event_row(paid, existing_event)]
            ],
        )
        effects: list[tuple[str, bool]] = []

        result = payments.ProductPaymentStore(connection).apply_verified_event(
            owner_user_id="user-1",
            provider="alipay",
            provider_order_id="provider-order-1",
            provider_event_id="event-paid-1",
            event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
            event_type="TRADE_SUCCESS",
            payload=payload,
            amount_cents=4900,
            transaction_effect=lambda _, current: effects.append(
                (str(current.order["status"]), current.idempotent)
            ),
        )

        self.assertEqual(effects, [("paid", True)])
        self.assertTrue(result.idempotent)
        self.assertEqual(result.points_credited, 0)
        self.assertEqual(result.points_debited, 0)
        self.assertIsNone(result.account)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_provider_for_update",
                "select_event_for_update",
                "select_point_order",
                "growth_insert_event",
            ],
        )
        self.assertEqual(connection.commits, 1)

    def test_event_replay_with_missing_wallet_order_fails_closed(self) -> None:
        payload = {"tradeNo": "trade-1"}
        wallet_order_id = payments._wallet_order_id(  # type: ignore[attr-defined]
            "credit",
            "pay-order-1",
        )
        paid = order_row(
            status="paid",
            credited_points=500,
            credit_point_order_id=wallet_order_id,
            version=1,
            paid_at="2026-07-30T00:01:00Z",
        )
        existing_event = event_row(
            payload=payload,
            wallet_order_id=wallet_order_id,
        )
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[paid]],
            select_event_for_update=[[existing_event]],
            select_point_order=[[]],
        )

        with self.assertRaises(payments.ProductPaymentWalletIntegrityError):
            payments.ProductPaymentStore(connection).apply_verified_event(
                owner_user_id="user-1",
                provider="alipay",
                provider_order_id="provider-order-1",
                provider_event_id="event-paid-1",
                event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
                event_type="TRADE_SUCCESS",
                payload=payload,
                amount_cents=4900,
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_event_id_content_drift_fails_closed_before_wallet_write(self) -> None:
        existing_event = event_row(payload={"tradeNo": "original"})
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[order_row()]],
            select_event_for_update=[[existing_event]],
        )

        with self.assertRaises(payments.ProductPaymentEventConflict):
            payments.ProductPaymentStore(connection).apply_verified_event(
                owner_user_id="user-1",
                provider="alipay",
                provider_order_id="provider-order-1",
                provider_event_id="event-paid-1",
                event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
                event_type="TRADE_SUCCESS",
                payload={"tradeNo": "drifted"},
                amount_cents=4900,
            )

        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_provider_for_update",
                "select_event_for_update",
            ],
        )

    def test_partial_refund_debits_only_cumulative_point_delta(self) -> None:
        paid = order_row(
            status="paid",
            credited_points=500,
            credit_point_order_id="payment-credit-order",
            version=1,
            paid_at="2026-07-30T00:01:00Z",
        )
        payload = {"refundNo": "refund-1"}
        provider_event_id = "event-refund-1"
        wallet_order_id = payments._wallet_order_id(  # type: ignore[attr-defined]
            "refund",
            f"alipay:{provider_event_id}",
        )
        inserted_event = event_row(
            event_kind=payments.EVENT_REFUND_SUCCEEDED,
            event_type="REFUND_SUCCESS",
            provider_event_id=provider_event_id,
            amount_cents=2450,
            points_delta=-250,
            target_status="partially_refunded",
            refunded_amount_cents_after=2450,
            refunded_points_after=250,
            wallet_order_id=wallet_order_id,
            payload=payload,
        )
        partial = order_row(
            status="partially_refunded",
            credited_points=500,
            refunded_amount_cents=2450,
            refunded_points=250,
            credit_point_order_id="payment-credit-order",
            version=2,
            paid_at="2026-07-30T00:01:00Z",
            refunded_at="2026-07-30T00:02:00Z",
        )
        debited = account_row(
            balance_points=250,
            lifetime_credited_points=500,
            lifetime_debited_points=250,
            version=3,
        )
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[paid]],
            select_event_for_update=[[]],
            insert_event=[[inserted_event]],
            insert_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=500, lifetime_credited_points=500)]
            ],
            insert_point_order=[
                [
                    point_order_row(
                        id=wallet_order_id,
                        order_kind="debit",
                        points=250,
                        request_sha256=inserted_event["content_sha256"],
                    )
                ]
            ],
            debit_point_account=[[debited]],
            insert_point_ledger=[[]],
            mark_order_refunded=[[partial]],
            growth_insert_event=[
                [growth_event_row(partial, inserted_event)]
            ],
        )

        result = payments.ProductPaymentStore(connection).apply_verified_event(
            owner_user_id="user-1",
            provider="alipay",
            provider_order_id="provider-order-1",
            provider_event_id=provider_event_id,
            event_kind=payments.EVENT_REFUND_SUCCEEDED,
            event_type="REFUND_SUCCESS",
            payload=payload,
            amount_cents=2450,
        )

        self.assertEqual(result.order["status"], "partially_refunded")
        self.assertEqual(result.points_debited, 250)
        self.assertEqual(result.account["balance_points"], 250)
        mark_params = next(
            call[2]
            for call in connection.calls
            if call[0] == "mark_order_refunded"
        )
        self.assertEqual(mark_params[:3], ("partially_refunded", 2450, 250))
        self.assertEqual(connection.commits, 1)

    def test_final_refund_uses_remaining_points_and_marks_refunded(self) -> None:
        partial = order_row(
            status="partially_refunded",
            credited_points=500,
            refunded_amount_cents=2450,
            refunded_points=250,
            credit_point_order_id="payment-credit-order",
            version=2,
            paid_at="2026-07-30T00:01:00Z",
            refunded_at="2026-07-30T00:02:00Z",
        )
        payload = {"refundNo": "refund-2"}
        provider_event_id = "event-refund-2"
        wallet_order_id = payments._wallet_order_id(  # type: ignore[attr-defined]
            "refund",
            f"alipay:{provider_event_id}",
        )
        inserted_event = event_row(
            event_kind=payments.EVENT_REFUND_SUCCEEDED,
            event_type="REFUND_SUCCESS",
            provider_event_id=provider_event_id,
            amount_cents=2450,
            points_delta=-250,
            target_status="refunded",
            refunded_amount_cents_after=4900,
            refunded_points_after=500,
            wallet_order_id=wallet_order_id,
            payload=payload,
        )
        refunded = order_row(
            status="refunded",
            credited_points=500,
            refunded_amount_cents=4900,
            refunded_points=500,
            credit_point_order_id="payment-credit-order",
            version=3,
            paid_at="2026-07-30T00:01:00Z",
            refunded_at="2026-07-30T00:02:00Z",
        )
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[partial]],
            select_event_for_update=[[]],
            insert_event=[[inserted_event]],
            insert_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=250, lifetime_credited_points=500)]
            ],
            insert_point_order=[
                [
                    point_order_row(
                        id=wallet_order_id,
                        order_kind="debit",
                        points=250,
                        request_sha256=inserted_event["content_sha256"],
                    )
                ]
            ],
            debit_point_account=[
                [
                    account_row(
                        balance_points=0,
                        lifetime_credited_points=500,
                        lifetime_debited_points=500,
                        version=4,
                    )
                ]
            ],
            insert_point_ledger=[[]],
            mark_order_refunded=[[refunded]],
            growth_insert_event=[
                [growth_event_row(refunded, inserted_event)]
            ],
        )

        result = payments.ProductPaymentStore(connection).apply_verified_event(
            owner_user_id="user-1",
            provider="alipay",
            provider_order_id="provider-order-1",
            provider_event_id=provider_event_id,
            event_kind=payments.EVENT_REFUND_SUCCEEDED,
            event_type="REFUND_SUCCESS",
            payload=payload,
            amount_cents=2450,
        )

        self.assertEqual(result.order["status"], "refunded")
        self.assertEqual(result.order["refunded_points"], 500)
        self.assertEqual(result.points_debited, 250)
        mark_params = next(
            call[2]
            for call in connection.calls
            if call[0] == "mark_order_refunded"
        )
        self.assertEqual(mark_params[:3], ("refunded", 4900, 500))

    def test_refund_with_insufficient_balance_rolls_back_event_and_order(self) -> None:
        paid = order_row(
            status="paid",
            credited_points=500,
            credit_point_order_id="payment-credit-order",
            version=1,
            paid_at="2026-07-30T00:01:00Z",
        )
        payload = {"refundNo": "refund-1"}
        inserted_event = event_row(
            event_kind=payments.EVENT_REFUND_SUCCEEDED,
            event_type="REFUND_SUCCESS",
            provider_event_id="event-refund-1",
            amount_cents=2450,
            points_delta=-250,
            target_status="partially_refunded",
            refunded_amount_cents_after=2450,
            refunded_points_after=250,
            wallet_order_id=payments._wallet_order_id(  # type: ignore[attr-defined]
                "refund",
                "alipay:event-refund-1",
            ),
            payload=payload,
        )
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[paid]],
            select_event_for_update=[[]],
            insert_event=[[inserted_event]],
            insert_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=100, lifetime_credited_points=500)]
            ],
        )

        with self.assertRaises(
            payments.InsufficientPaymentRefundBalance
        ) as captured:
            payments.ProductPaymentStore(connection).apply_verified_event(
                owner_user_id="user-1",
                provider="alipay",
                provider_order_id="provider-order-1",
                provider_event_id="event-refund-1",
                event_kind=payments.EVENT_REFUND_SUCCEEDED,
                event_type="REFUND_SUCCESS",
                payload=payload,
                amount_cents=2450,
            )

        self.assertEqual(captured.exception.available_points, 100)
        self.assertEqual(captured.exception.required_points, 250)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_order_by_provider_for_update",
                "select_event_for_update",
                "insert_event",
                "insert_point_account",
                "select_point_account_for_update",
            ],
        )
        self.assertNotIn(
            "mark_order_refunded",
            [call[0] for call in connection.calls],
        )

    def test_wrong_owner_is_concealed_before_event_write(self) -> None:
        connection = ScriptedConnection(
            select_order_by_provider_for_update=[[order_row()]],
        )

        with self.assertRaises(payments.ProductPaymentOrderNotFound):
            payments.ProductPaymentStore(connection).apply_verified_event(
                owner_user_id="user-2",
                provider="alipay",
                provider_order_id="provider-order-1",
                provider_event_id="event-paid-1",
                event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
                event_type="TRADE_SUCCESS",
                payload={"tradeNo": "trade-1"},
                amount_cents=4900,
            )

        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            [call[0] for call in connection.calls],
            ["select_order_by_provider_for_update"],
        )

    def test_commit_failure_rolls_back_the_whole_order_creation(self) -> None:
        connection = ScriptedConnection(
            commit_error=RuntimeError("commit failed"),
            select_order_by_idempotency_for_update=[[]],
            insert_order=[[order_row()]],
        )

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            create_order(payments.ProductPaymentStore(connection))

        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.cursors[0].closed)

    def test_payment_migration_uses_postgresql_and_transactional_wallet_links(self) -> None:
        migration = (
            ROOT / "migrations" / "004_product_payments_postgres.sql"
        ).read_text(encoding="utf-8")

        self.assertTrue(migration.startswith("BEGIN;"))
        self.assertTrue(migration.rstrip().endswith("COMMIT;"))
        self.assertIn("JSONB", migration)
        self.assertIn("TIMESTAMPTZ", migration)
        self.assertIn("DEFERRABLE INITIALLY DEFERRED", migration)
        self.assertIn("REFERENCES product_point_orders", migration)
        self.assertIn("partially_refunded", migration)
        self.assertNotIn("AUTOINCREMENT", migration)
        self.assertNotIn("VALUES (?,", migration)


if __name__ == "__main__":
    unittest.main()
