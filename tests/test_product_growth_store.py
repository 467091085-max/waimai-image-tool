from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_growth_store as growth
from shared import product_payment_store as payments


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "007_product_growth_business_postgres.sql"
STORE_SOURCE = ROOT / "shared" / "product_growth_store.py"
PAYMENT_STORE_SOURCE = ROOT / "shared" / "product_payment_store.py"
POSTGRES_MIGRATIONS = (
    "001_product_generation_postgres.sql",
    "002_product_wallet_postgres.sql",
    "004_product_payments_postgres.sql",
    "006_product_growth_outbox_postgres.sql",
    "007_product_growth_business_postgres.sql",
)


class RecordingCursor:
    def __init__(self, **responses: list[list[dict[str, Any]]]) -> None:
        self.description = None
        self.rowcount = -1
        self.responses = {name: list(rows) for name, rows in responses.items()}
        self.rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def execute(
        self,
        operation: str,
        parameters: tuple[Any, ...] = (),
    ) -> None:
        match = re.search(
            r"/\* product_growth_store:([a-z_]+) \*/",
            operation,
        )
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        name = match.group(1)
        if operation.count("%s") != len(parameters):
            raise AssertionError(
                f"{name} expected {operation.count('%s')} parameters, "
                f"received {len(parameters)}"
            )
        self.calls.append((name, operation, tuple(parameters)))
        queued = self.responses.get(name)
        if not queued:
            raise AssertionError(f"unexpected SQL operation: {name}")
        self.rows = list(queued.pop(0))
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        return None if not self.rows else self.rows.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = list(self.rows)
        self.rows.clear()
        return rows

    def close(self) -> None:
        return None


class AutocommitConnection:
    autocommit = True


def test_migration_defines_growth_boundaries_and_durable_reward_debt() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in (
        "product_growth_subject_locks",
        "product_growth_agents",
        "product_growth_agent_bindings",
        "product_growth_invite_codes",
        "product_growth_invite_relations",
        "product_growth_business_events",
        "product_growth_reward_grants",
        "product_growth_reward_reversal_states",
        "product_growth_reward_debts",
        "product_growth_reward_debt_recoveries",
        "product_growth_reward_reversals",
        "product_growth_audit_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

    assert "CHECK (commission_depth = 1)" in sql
    assert "CHECK (relation_depth = 1)" in sql
    assert "UNIQUE (tenant_id, customer_user_id)" in sql
    assert "UNIQUE (tenant_id, invitee_user_id)" in sql
    assert "UNIQUE (tenant_id, code_sha256)" in sql
    assert "UNIQUE (tenant_id, source_event_id)" in sql
    assert "UNIQUE (tenant_id, event_type, business_key)" in sql
    assert "code_sha256 TEXT NOT NULL" in sql
    assert "plaintext" not in sql.lower()
    assert "consumer.first_recharge_refund" in sql
    assert (
        "outstanding_points\n"
        "                = lifetime_assessed_points - lifetime_recovered_points"
        in sql
    )
    assert "recovered_points + outstanding_points = reversed_points" in sql
    assert "wallet_order_id TEXT," in sql
    assert "debt_offset_points + net_wallet_points = points" in sql
    assert "product_growth_reward_debt_recoveries" in sql
    assert "product_growth_reject_immutable_mutation" in sql
    assert "trg_product_growth_agent_bindings_immutable" in sql
    assert "trg_product_growth_invite_relations_immutable" in sql
    assert "trg_product_growth_debt_recoveries_immutable" in sql
    assert "DROP TRIGGER IF EXISTS" in sql
    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")


def test_cursor_api_signatures_expose_atomic_integration_contract() -> None:
    invite = inspect.signature(growth.accept_consumer_invite)
    refund = inspect.signature(growth.process_first_recharge_refund)
    classify = inspect.signature(growth.classify_agent_payment)

    assert tuple(invite.parameters)[:2] == ("cursor", "tenant_id")
    assert invite.parameters["invite_code"].default is inspect.Parameter.empty
    assert invite.parameters["inviter_user_id"].default is None
    assert tuple(refund.parameters)[:2] == ("cursor", "tenant_id")
    assert "cumulative_refunded_cents" in refund.parameters
    assert "cumulative_refunded_points" in refund.parameters
    assert tuple(classify.parameters)[:2] == ("cursor", "tenant_id")

    source = STORE_SOURCE.read_text(encoding="utf-8")
    assert "ORDER BY paid_at, created_at, id" in source
    assert "FOR UPDATE" in source
    assert "ownerDebtOutstandingPointsAfter" in source
    assert "pointsRecovered" in source
    assert "grossRewardPoints" in source
    assert "debtOffsetPoints" in source
    assert "netWalletPoints" in source


def test_growth_reward_debt_split_is_bounded_and_growth_only() -> None:
    assert growth._growth_reward_split(49, 80) == (49, 0)
    assert growth._growth_reward_split(100, 24) == (24, 76)
    assert growth._growth_reward_split(20, 0) == (0, 20)

    payment_source = PAYMENT_STORE_SOURCE.read_text(encoding="utf-8")
    assert "product_growth_reward_debts" not in payment_source
    assert "recover_reward_debt" not in payment_source


def test_owner_safe_reads_and_invite_resolution_are_tenant_scoped() -> None:
    invite_code = "tenant-safe-code"
    digest = hashlib.sha256(invite_code.encode("utf-8")).hexdigest()
    cursor = RecordingCursor(
        select_agent_by_user=[
            [
                {
                    "id": "growth_agent_" + "a" * 40,
                    "tenant_id": "tenant-a",
                    "owner_user_id": "agent-user",
                }
            ]
        ],
        select_invite_code_by_digest=[
            [
                {
                    "id": "growth_invite_code_" + "b" * 40,
                    "tenant_id": "tenant-a",
                    "owner_user_id": "inviter-user",
                    "code_sha256": digest,
                }
            ]
        ],
        select_reward_debt=[
            [
                {
                    "tenant_id": "tenant-a",
                    "owner_user_id": "inviter-user",
                    "outstanding_points": 24,
                }
            ]
        ],
        list_reward_debt_recoveries_by_owner=[
            [
                {
                    "id": "growth_debt_recovery_" + "c" * 40,
                    "tenant_id": "tenant-a",
                    "owner_user_id": "inviter-user",
                    "gross_reward_points": 49,
                    "recovered_points": 24,
                    "net_wallet_points": 25,
                }
            ]
        ],
    )

    agent = growth.get_agent_by_user(
        cursor,
        tenant_id="tenant-a",
        owner_user_id="agent-user",
    )
    resolved = growth.resolve_consumer_invite_code(
        cursor,
        tenant_id="tenant-a",
        invite_code=invite_code,
    )
    debt = growth.get_reward_debt(
        cursor,
        tenant_id="tenant-a",
        owner_user_id="inviter-user",
    )
    recoveries = growth.list_reward_debt_recoveries(
        cursor,
        tenant_id="tenant-a",
        owner_user_id="inviter-user",
    )

    assert agent is not None and agent["tenant_id"] == "tenant-a"
    assert resolved is not None and resolved["owner_user_id"] == "inviter-user"
    assert debt is not None and debt["outstanding_points"] == 24
    assert recoveries[0]["recovered_points"] == 24
    assert cursor.calls[0][2] == ("tenant-a", "agent-user")
    assert cursor.calls[1][2] == ("tenant-a", digest)
    assert invite_code not in cursor.calls[1][2]
    assert cursor.calls[2][2] == ("tenant-a", "inviter-user")
    assert cursor.calls[3][2] == ("tenant-a", "inviter-user", 100)


def test_growth_ids_are_deterministic_and_tenant_separated() -> None:
    first = growth.stable_growth_id(
        "growth_event",
        "tenant-a",
        "consumer.first_recharge_reward",
        "pay-1",
    )
    replay = growth.stable_growth_id(
        "growth_event",
        "tenant-a",
        "consumer.first_recharge_reward",
        "pay-1",
    )
    other_tenant = growth.stable_growth_id(
        "growth_event",
        "tenant-b",
        "consumer.first_recharge_reward",
        "pay-1",
    )

    assert first == replay
    assert first != other_tenant
    assert re.fullmatch(r"growth_event_[0-9a-f]{40}", first)


def test_transaction_wrapper_rejects_autocommit_connections() -> None:
    with pytest.raises(growth.InvalidProductGrowthInput):
        growth.ProductGrowthStore(AutocommitConnection())  # type: ignore[arg-type]


def _set_payment_chronology(
    connection: Any,
    *,
    order_id: str,
    created_at: datetime,
    paid_at: datetime,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE product_payment_orders
            SET created_at = %s, paid_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (created_at, paid_at, paid_at, order_id),
        )
    connection.commit()


def _create_paid_order(
    connection: Any,
    *,
    owner_user_id: str,
    order_id: str,
    provider_order_id: str,
    provider_event_id: str,
    created_at: datetime,
    paid_at: datetime,
) -> dict[str, Any]:
    store = payments.ProductPaymentStore(connection)
    store.create_or_get_order(
        owner_user_id=owner_user_id,
        provider="alipay",
        package_id="starter-500",
        idempotency_key=f"create-{order_id}",
        order_id=order_id,
        provider_order_id=provider_order_id,
    )
    result = store.apply_verified_event(
        owner_user_id=owner_user_id,
        provider="alipay",
        provider_order_id=provider_order_id,
        provider_event_id=provider_event_id,
        event_kind=payments.EVENT_PAYMENT_SUCCEEDED,
        event_type="TRADE_SUCCESS",
        payload={"verified": True, "orderId": order_id},
        amount_cents=4900,
    )
    _set_payment_chronology(
        connection,
        order_id=order_id,
        created_at=created_at,
        paid_at=paid_at,
    )
    return result.order


def _spend_all_points(
    connection: Any,
    *,
    owner_user_id: str,
) -> int:
    order_id = f"test_spend_{uuid4().hex}"
    request_sha256 = hashlib.sha256(order_id.encode("utf-8")).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT balance_points
            FROM product_point_accounts
            WHERE owner_user_id = %s
            FOR UPDATE
            """,
            (owner_user_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        points = int(row[0])
        assert points > 0
        cursor.execute(
            """
            INSERT INTO product_point_orders (
                id, owner_user_id, order_kind, points,
                request_sha256, metadata
            ) VALUES (%s, %s, 'debit', %s, %s, '{}'::jsonb)
            """,
            (order_id, owner_user_id, points, request_sha256),
        )
        cursor.execute(
            """
            UPDATE product_point_accounts
            SET balance_points = balance_points - %s,
                lifetime_debited_points = lifetime_debited_points + %s,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE owner_user_id = %s AND balance_points = %s
            RETURNING balance_points
            """,
            (points, points, owner_user_id, points),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            INSERT INTO product_point_ledger (
                owner_user_id, order_id, order_kind, points,
                delta_points, balance_after_points,
                request_sha256, metadata
            ) VALUES (
                %s, %s, 'debit', %s,
                %s, 0, %s, '{}'::jsonb
            )
            """,
            (
                owner_user_id,
                order_id,
                points,
                -points,
                request_sha256,
            ),
        )
    connection.commit()
    return points


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_growth_protocol_concurrency_and_refund_debt() -> None:
    dsn = str(os.environ["TEST_POSTGRES_DSN"]).strip()
    if "test" not in dsn.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")

    psycopg = pytest.importorskip("psycopg")
    sql_module = pytest.importorskip("psycopg.sql")
    schema = f"test_product_growth_{uuid4().hex}"

    admin = psycopg.connect(dsn, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql_module.SQL("CREATE SCHEMA {}").format(
                    sql_module.Identifier(schema)
                )
            )

        def connect() -> Any:
            connection = psycopg.connect(dsn, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute(
                    sql_module.SQL("SET search_path TO {}").format(
                        sql_module.Identifier(schema)
                    )
                )
            connection.commit()
            return connection

        connection = connect()
        try:
            with connection.cursor() as cursor:
                for name in POSTGRES_MIGRATIONS:
                    cursor.execute(
                        (ROOT / "migrations" / name).read_text(
                            encoding="utf-8"
                        )
                    )
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))

            store = growth.ProductGrowthStore(connection)
            tenant = "tenant-real"
            other_tenant = "tenant-other"
            inviter = "inviter-real"
            invitee = "invitee-real"
            invite_code = "consumer-invite-real-001"

            issued = store.issue_consumer_invite_code(
                tenant_id=tenant,
                owner_user_id=inviter,
                idempotency_key="issue-invite-real",
                plaintext_code=invite_code,
            )
            assert issued.created is True
            assert issued.plaintext_code == invite_code
            assert "plaintext_code" not in issued.record
            replayed_issue = store.issue_consumer_invite_code(
                tenant_id=tenant,
                owner_user_id=inviter,
                idempotency_key="issue-invite-real",
                plaintext_code=invite_code,
            )
            assert replayed_issue.created is False
            assert replayed_issue.plaintext_code is None

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT code_sha256
                    FROM product_growth_invite_codes
                    WHERE tenant_id = %s AND owner_user_id = %s
                    """,
                    (tenant, inviter),
                )
                assert cursor.fetchone()[0] == hashlib.sha256(
                    invite_code.encode("utf-8")
                ).hexdigest()
                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = 'product_growth_invite_codes'
                    """,
                    (schema,),
                )
                columns = {row[0] for row in cursor.fetchall()}
                assert "plaintext_code" not in columns

            assert (
                store.resolve_consumer_invite_code(
                    tenant_id=other_tenant,
                    invite_code=invite_code,
                )
                is None
            )
            other_issued = store.issue_consumer_invite_code(
                tenant_id=other_tenant,
                owner_user_id="other-inviter",
                idempotency_key="issue-other-invite",
                plaintext_code=invite_code,
            )
            assert other_issued.created is True
            other_resolved = store.resolve_consumer_invite_code(
                tenant_id=other_tenant,
                invite_code=invite_code,
            )
            assert other_resolved is not None
            assert other_resolved["owner_user_id"] == "other-inviter"

            with pytest.raises(growth.ProductGrowthConflict):
                store.issue_consumer_invite_code(
                    tenant_id=tenant,
                    owner_user_id="duplicate-inviter",
                    idempotency_key="duplicate-code",
                    plaintext_code=invite_code,
                )

            with pytest.raises(growth.ProductGrowthConflict):
                store.accept_consumer_invite(
                    tenant_id=tenant,
                    owner_user_id=invitee,
                    idempotency_key="accept-invite-real",
                    invite_code=invite_code,
                    inviter_user_id="spoofed-inviter",
                )

            relation_result = store.accept_consumer_invite(
                tenant_id=tenant,
                owner_user_id=invitee,
                idempotency_key="accept-invite-real",
                invite_code=invite_code,
                risk_snapshot={
                    "phoneVerified": True,
                    "humanVerified": True,
                },
            )
            assert relation_result.created is True
            relation = relation_result.record
            assert relation["inviter_user_id"] == inviter
            assert relation["relation_depth"] == 1
            replayed_relation = store.accept_consumer_invite(
                tenant_id=tenant,
                owner_user_id=invitee,
                idempotency_key="accept-invite-real",
                invite_code=invite_code,
                risk_snapshot={
                    "phoneVerified": True,
                    "humanVerified": True,
                },
            )
            assert replayed_relation.created is False
            assert (
                store.get_invite_relation(
                    tenant_id=other_tenant,
                    owner_user_id=invitee,
                )
                is None
            )

            agent_one = store.create_or_get_agent(
                tenant_id=tenant,
                owner_user_id="agent-owner-one",
                idempotency_key="create-agent-one",
                agent_code="AGENT001",
            ).record
            agent_two = store.create_or_get_agent(
                tenant_id=tenant,
                owner_user_id="agent-owner-two",
                idempotency_key="create-agent-two",
                agent_code="AGENT002",
            ).record
            store.bind_agent_customer(
                tenant_id=tenant,
                owner_user_id=invitee,
                agent_id=agent_one["id"],
                idempotency_key="bind-main-invitee",
            )

            barrier = threading.Barrier(2)

            def bind_race(agent_id: str, suffix: str) -> tuple[str, Any]:
                race_connection = connect()
                try:
                    barrier.wait(timeout=10)
                    result = growth.ProductGrowthStore(
                        race_connection
                    ).bind_agent_customer(
                        tenant_id=tenant,
                        owner_user_id="binding-race-customer",
                        agent_id=agent_id,
                        idempotency_key=f"binding-race-{suffix}",
                    )
                    return ("created", result.record["agent_id"])
                except growth.ProductGrowthConflict as exc:
                    return ("conflict", exc)
                finally:
                    race_connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                binding_results = list(
                    executor.map(
                        lambda args: bind_race(*args),
                        (
                            (agent_one["id"], "one"),
                            (agent_two["id"], "two"),
                        ),
                    )
                )
            assert sorted(item[0] for item in binding_results) == [
                "conflict",
                "created",
            ]
            winner = store.get_agent_binding(
                tenant_id=tenant,
                owner_user_id="binding-race-customer",
            )
            assert winner is not None
            assert winner["agent_id"] in {agent_one["id"], agent_two["id"]}

            outbox_payload = {"inviteRelationId": relation["id"]}
            outbox_payload_json = json.dumps(
                outbox_payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            outbox_sha256 = hashlib.sha256(
                outbox_payload_json.encode("utf-8")
            ).hexdigest()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO product_growth_outbox (
                        id, event_type, dedupe_key, payload, payload_sha256,
                        status, max_attempts, attempt_count,
                        claimed_by, claim_token, claimed_until, fence
                    ) VALUES (
                        'growth_atomic_registration_real',
                        'growth.invite_reward.requested',
                        'growth-atomic-registration-real',
                        %s::jsonb, %s,
                        'claimed', 8, 1,
                        'growth-worker-real', 'claim-real',
                        CURRENT_TIMESTAMP + INTERVAL '5 minutes', 1
                    )
                    """,
                    (outbox_payload_json, outbox_sha256),
                )
            connection.commit()

            with connection.cursor() as cursor:
                registration = growth.process_registration_reward(
                    cursor,
                    tenant_id=tenant,
                    owner_user_id=invitee,
                    invite_relation_id=relation["id"],
                    source_event_id="registration-event-real",
                    idempotency_key="registration-idem-real",
                    source_payload={"outboxId": "growth_atomic_registration_real"},
                )
                observer = connect()
                try:
                    with observer.cursor() as observer_cursor:
                        observer_cursor.execute(
                            """
                            SELECT COUNT(*)
                            FROM product_growth_business_events
                            WHERE id = %s
                            """,
                            (registration.event["id"],),
                        )
                        assert observer_cursor.fetchone()[0] == 0
                finally:
                    observer.close()
                cursor.execute(
                    """
                    UPDATE product_growth_outbox
                    SET status = 'succeeded',
                        result_payload = %s::jsonb,
                        last_error = '',
                        succeeded_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 'growth_atomic_registration_real'
                      AND status = 'claimed'
                    """,
                    (
                        json.dumps(
                            {"businessEventId": registration.event["id"]},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                assert cursor.rowcount == 1
            connection.commit()
            assert registration.applied is True
            assert {grant["points"] for grant in registration.grants} == {
                20,
                100,
            }
            registration_replay = store.process_registration_reward(
                tenant_id=tenant,
                owner_user_id=invitee,
                invite_relation_id=relation["id"],
                source_event_id="registration-event-real",
                idempotency_key="registration-idem-real",
                source_payload={"outboxId": "growth_atomic_registration_real"},
            )
            assert registration_replay.idempotent is True

            early = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
            later = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)
            _create_paid_order(
                connection,
                owner_user_id=invitee,
                order_id="pay-growth-first",
                provider_order_id="provider-growth-first",
                provider_event_id="provider-event-growth-first",
                created_at=early,
                paid_at=early,
            )
            _create_paid_order(
                connection,
                owner_user_id=invitee,
                order_id="pay-growth-second",
                provider_order_id="provider-growth-second",
                provider_event_id="provider-event-growth-second",
                created_at=later,
                paid_at=later,
            )

            processed_later_first = store.process_first_recharge_reward(
                tenant_id=tenant,
                owner_user_id=invitee,
                invite_relation_id=relation["id"],
                source_event_id="growth-payment-event-second",
                source_order_id="pay-growth-second",
                paid_cents=4900,
                idempotency_key="growth-payment-idem-second",
            )
            assert processed_later_first.applied is False
            assert (
                processed_later_first.event["outcome_reason"]
                == "not_first_recharge"
            )

            payment_store = payments.ProductPaymentStore(connection)
            payment_store.apply_verified_event(
                owner_user_id=invitee,
                provider="alipay",
                provider_order_id="provider-growth-first",
                provider_event_id="provider-refund-growth-partial",
                event_kind=payments.EVENT_REFUND_SUCCEEDED,
                event_type="REFUND_SUCCESS",
                payload={"refundNo": "growth-partial"},
                amount_cents=2450,
            )
            with pytest.raises(growth.ProductGrowthEventDeferred):
                store.process_first_recharge_refund(
                    tenant_id=tenant,
                    owner_user_id=invitee,
                    invite_relation_id=relation["id"],
                    source_event_id="growth-refund-partial",
                    source_order_id="pay-growth-first",
                    refund_cents=2450,
                    cumulative_refunded_cents=2450,
                    cumulative_refunded_points=250,
                    idempotency_key="growth-refund-partial-idem",
                )

            processed_first = store.process_first_recharge_reward(
                tenant_id=tenant,
                owner_user_id=invitee,
                invite_relation_id=relation["id"],
                source_event_id="growth-payment-event-first",
                source_order_id="pay-growth-first",
                paid_cents=4900,
                idempotency_key="growth-payment-idem-first",
            )
            assert processed_first.applied is True
            assert processed_first.grants[0]["points"] == 49

            first_classification = store.classify_agent_payment(
                tenant_id=tenant,
                owner_user_id=invitee,
                source_order_id="pay-growth-first",
                paid_cents=4900,
            )
            repeat_classification = store.classify_agent_payment(
                tenant_id=tenant,
                owner_user_id=invitee,
                source_order_id="pay-growth-second",
                paid_cents=4900,
            )
            assert first_classification is not None
            assert first_classification.is_first_order is True
            assert first_classification.commission_rate_bps == 2000
            assert first_classification.commission_amount_cents == 980
            assert repeat_classification is not None
            assert repeat_classification.is_first_order is False
            assert repeat_classification.commission_rate_bps == 1000
            assert repeat_classification.commission_amount_cents == 490
            assert (
                repeat_classification.canonical_first_order_id
                == "pay-growth-first"
            )

            partial_refund = store.process_first_recharge_refund(
                tenant_id=tenant,
                owner_user_id=invitee,
                invite_relation_id=relation["id"],
                source_event_id="growth-refund-partial",
                source_order_id="pay-growth-first",
                refund_cents=2450,
                cumulative_refunded_cents=2450,
                cumulative_refunded_points=250,
                idempotency_key="growth-refund-partial-idem",
            )
            assert partial_refund.applied is True
            assert partial_refund.points_assessed == 25
            assert partial_refund.points_recovered == 25
            assert partial_refund.outstanding_points_added == 0
            assert partial_refund.outstanding_points_after == 0

            spent_points = _spend_all_points(
                connection,
                owner_user_id=inviter,
            )
            assert spent_points == 124
            payment_store.apply_verified_event(
                owner_user_id=invitee,
                provider="alipay",
                provider_order_id="provider-growth-first",
                provider_event_id="provider-refund-growth-final",
                event_kind=payments.EVENT_REFUND_SUCCEEDED,
                event_type="REFUND_SUCCESS",
                payload={"refundNo": "growth-final"},
                amount_cents=2450,
            )

            refund_barrier = threading.Barrier(2)

            def refund_race() -> growth.GrowthRefundApplyResult:
                race_connection = connect()
                try:
                    refund_barrier.wait(timeout=10)
                    return growth.ProductGrowthStore(
                        race_connection
                    ).process_first_recharge_refund(
                        tenant_id=tenant,
                        owner_user_id=invitee,
                        invite_relation_id=relation["id"],
                        source_event_id="growth-refund-final",
                        source_order_id="pay-growth-first",
                        refund_cents=2450,
                        cumulative_refunded_cents=4900,
                        cumulative_refunded_points=500,
                        idempotency_key="growth-refund-final-idem",
                        source_payload={"providerEventId": "refund-final"},
                    )
                finally:
                    race_connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                final_results = list(
                    executor.map(lambda _: refund_race(), range(2))
                )
            assert sorted(result.idempotent for result in final_results) == [
                False,
                True,
            ]
            assert all(result.applied for result in final_results)
            assert all(result.points_assessed == 24 for result in final_results)
            assert all(result.points_recovered == 0 for result in final_results)
            assert all(
                result.outstanding_points_added == 24
                for result in final_results
            )
            assert all(
                result.outstanding_points_after == 24
                for result in final_results
            )

            stale = store.process_first_recharge_refund(
                tenant_id=tenant,
                owner_user_id=invitee,
                invite_relation_id=relation["id"],
                source_event_id="growth-refund-stale",
                source_order_id="pay-growth-first",
                refund_cents=2450,
                cumulative_refunded_cents=2450,
                cumulative_refunded_points=250,
                idempotency_key="growth-refund-stale-idem",
            )
            assert stale.applied is False
            assert stale.event["outcome_reason"] == "stale_cumulative_refund"
            assert stale.points_assessed == 0
            assert stale.points_recovered == 0
            assert stale.outstanding_points_after == 24

            debt = store.get_reward_debt(
                tenant_id=tenant,
                owner_user_id=inviter,
            )
            assert debt is not None
            assert debt["outstanding_points"] == 24
            assert debt["lifetime_assessed_points"] == 49
            assert debt["lifetime_recovered_points"] == 25
            assert (
                store.get_reward_debt(
                    tenant_id=other_tenant,
                    owner_user_id=inviter,
                )
                is None
            )
            reversals = store.list_reward_reversals(
                tenant_id=tenant,
                owner_user_id=inviter,
            )
            assert len(reversals) == 2
            assert sum(row["assessed_points"] for row in reversals) == 49
            debt_reversal = next(
                row
                for row in reversals
                if row["outstanding_points_added"] == 24
            )
            assert debt_reversal["recovered_points"] == 0
            assert debt_reversal["wallet_order_id"] is None

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT balance_points
                    FROM product_point_accounts
                    WHERE owner_user_id = %s
                    """,
                    (inviter,),
                )
                assert cursor.fetchone()[0] == 0
                cursor.execute(
                    """
                    SELECT reversed_points, recovered_points,
                           outstanding_points
                    FROM product_growth_reward_reversal_states
                    WHERE tenant_id = %s AND source_order_id = %s
                    """,
                    (tenant, "pay-growth-first"),
                )
                assert cursor.fetchone() == (49, 25, 24)
                cursor.execute(
                    """
                    SELECT metadata
                    FROM product_growth_audit_events
                    WHERE tenant_id = %s
                      AND target_id = %s
                    """,
                    (tenant, debt_reversal["business_event_id"]),
                )
                audit_metadata = cursor.fetchone()[0]
                assert audit_metadata["pointsRecovered"] == 0
                assert audit_metadata["outstandingPointsAdded"] == 24
                assert (
                    audit_metadata["ownerDebtOutstandingPointsAfter"] == 24
                )
                cursor.execute(
                    """
                    SELECT status, result_payload
                    FROM product_growth_outbox
                    WHERE id = 'growth_atomic_registration_real'
                    """
                )
                outbox_status, outbox_result = cursor.fetchone()
                assert outbox_status == "succeeded"
                assert (
                    outbox_result["businessEventId"]
                    == registration.event["id"]
                )

            partial_invitee = "invitee-debt-partial"
            partial_relation = store.accept_consumer_invite(
                tenant_id=tenant,
                owner_user_id=partial_invitee,
                idempotency_key="accept-debt-partial",
                invite_code=invite_code,
            ).record
            with connection.cursor() as cursor:
                partial_offset = growth.process_registration_reward(
                    cursor,
                    tenant_id=tenant,
                    owner_user_id=partial_invitee,
                    invite_relation_id=partial_relation["id"],
                    source_event_id="registration-debt-partial",
                    idempotency_key="registration-debt-partial-idem",
                )
                observer = connect()
                try:
                    with observer.cursor() as observer_cursor:
                        observer_cursor.execute(
                            """
                            SELECT balance_points
                            FROM product_point_accounts
                            WHERE owner_user_id = %s
                            """,
                            (inviter,),
                        )
                        assert observer_cursor.fetchone()[0] == 0
                        observer_cursor.execute(
                            """
                            SELECT outstanding_points
                            FROM product_growth_reward_debts
                            WHERE tenant_id = %s AND owner_user_id = %s
                            """,
                            (tenant, inviter),
                        )
                        assert observer_cursor.fetchone()[0] == 24
                        observer_cursor.execute(
                            """
                            SELECT COUNT(*)
                            FROM product_growth_business_events
                            WHERE id = %s
                            """,
                            (partial_offset.event["id"],),
                        )
                        assert observer_cursor.fetchone()[0] == 0
                finally:
                    observer.close()
            connection.commit()
            assert partial_offset.gross_reward_points == 120
            assert partial_offset.debt_offset_points == 24
            assert partial_offset.net_wallet_points == 96
            assert len(partial_offset.debt_recoveries) == 1
            partial_inviter_grant = next(
                row
                for row in partial_offset.grants
                if row["owner_user_id"] == inviter
            )
            assert partial_inviter_grant["points"] == 100
            assert partial_inviter_grant["debt_offset_points"] == 24
            assert partial_inviter_grant["net_wallet_points"] == 76
            assert partial_inviter_grant["wallet_order_id"] is not None
            partial_replay = store.process_registration_reward(
                tenant_id=tenant,
                owner_user_id=partial_invitee,
                invite_relation_id=partial_relation["id"],
                source_event_id="registration-debt-partial",
                idempotency_key="registration-debt-partial-idem",
            )
            assert partial_replay.idempotent is True
            assert partial_replay.gross_reward_points == 120
            assert partial_replay.debt_offset_points == 24
            assert partial_replay.net_wallet_points == 96
            assert len(partial_replay.debt_recoveries) == 1

            _create_paid_order(
                connection,
                owner_user_id=partial_invitee,
                order_id="pay-growth-debt-source",
                provider_order_id="provider-growth-debt-source",
                provider_event_id="provider-event-growth-debt-source",
                created_at=datetime(
                    2026,
                    7,
                    30,
                    3,
                    0,
                    tzinfo=timezone.utc,
                ),
                paid_at=datetime(
                    2026,
                    7,
                    30,
                    3,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            debt_source_reward = store.process_first_recharge_reward(
                tenant_id=tenant,
                owner_user_id=partial_invitee,
                invite_relation_id=partial_relation["id"],
                source_event_id="growth-payment-debt-source",
                source_order_id="pay-growth-debt-source",
                paid_cents=4900,
                idempotency_key="growth-payment-debt-source-idem",
            )
            assert debt_source_reward.debt_offset_points == 0
            assert debt_source_reward.net_wallet_points == 49
            assert _spend_all_points(
                connection,
                owner_user_id=inviter,
            ) == 125
            payment_store.apply_verified_event(
                owner_user_id=partial_invitee,
                provider="alipay",
                provider_order_id="provider-growth-debt-source",
                provider_event_id="provider-refund-growth-debt-source",
                event_kind=payments.EVENT_REFUND_SUCCEEDED,
                event_type="REFUND_SUCCESS",
                payload={"refundNo": "growth-debt-source"},
                amount_cents=4900,
            )
            debt_source_refund = store.process_first_recharge_refund(
                tenant_id=tenant,
                owner_user_id=partial_invitee,
                invite_relation_id=partial_relation["id"],
                source_event_id="growth-refund-debt-source",
                source_order_id="pay-growth-debt-source",
                refund_cents=4900,
                cumulative_refunded_cents=4900,
                cumulative_refunded_points=500,
                idempotency_key="growth-refund-debt-source-idem",
            )
            assert debt_source_refund.points_assessed == 49
            assert debt_source_refund.points_recovered == 0
            assert debt_source_refund.outstanding_points_after == 49

            _create_paid_order(
                connection,
                owner_user_id=inviter,
                order_id="pay-cash-does-not-offset-growth-debt",
                provider_order_id="provider-cash-no-growth-offset",
                provider_event_id="provider-event-cash-no-growth-offset",
                created_at=datetime(
                    2026,
                    7,
                    30,
                    4,
                    0,
                    tzinfo=timezone.utc,
                ),
                paid_at=datetime(
                    2026,
                    7,
                    30,
                    4,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            debt_after_cash = store.get_reward_debt(
                tenant_id=tenant,
                owner_user_id=inviter,
            )
            assert debt_after_cash is not None
            assert debt_after_cash["outstanding_points"] == 49
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT balance_points
                    FROM product_point_accounts
                    WHERE owner_user_id = %s
                    """,
                    (inviter,),
                )
                assert cursor.fetchone()[0] == 500

            full_invitee = "invitee-debt-full"
            full_relation = store.accept_consumer_invite(
                tenant_id=tenant,
                owner_user_id=full_invitee,
                idempotency_key="accept-debt-full",
                invite_code=invite_code,
            ).record
            _create_paid_order(
                connection,
                owner_user_id=full_invitee,
                order_id="pay-growth-debt-full-offset",
                provider_order_id="provider-growth-debt-full-offset",
                provider_event_id="provider-event-growth-debt-full-offset",
                created_at=datetime(
                    2026,
                    7,
                    30,
                    5,
                    0,
                    tzinfo=timezone.utc,
                ),
                paid_at=datetime(
                    2026,
                    7,
                    30,
                    5,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            reward_barrier = threading.Barrier(2)

            def reward_replay_race() -> growth.GrowthEventApplyResult:
                race_connection = connect()
                try:
                    reward_barrier.wait(timeout=10)
                    return growth.ProductGrowthStore(
                        race_connection
                    ).process_first_recharge_reward(
                        tenant_id=tenant,
                        owner_user_id=full_invitee,
                        invite_relation_id=full_relation["id"],
                        source_event_id="growth-payment-debt-full-offset",
                        source_order_id="pay-growth-debt-full-offset",
                        paid_cents=4900,
                        idempotency_key=(
                            "growth-payment-debt-full-offset-idem"
                        ),
                        source_payload={"case": "full-offset-replay"},
                    )
                finally:
                    race_connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                full_offset_results = list(
                    executor.map(
                        lambda _: reward_replay_race(),
                        range(2),
                    )
                )
            assert sorted(
                result.idempotent for result in full_offset_results
            ) == [False, True]
            assert all(
                result.gross_reward_points == 49
                for result in full_offset_results
            )
            assert all(
                result.debt_offset_points == 49
                for result in full_offset_results
            )
            assert all(result.net_wallet_points == 0 for result in full_offset_results)
            assert all(
                len(result.debt_recoveries) == 1
                for result in full_offset_results
            )
            full_offset_grant = full_offset_results[0].grants[0]
            assert full_offset_grant["points"] == 49
            assert full_offset_grant["debt_offset_points"] == 49
            assert full_offset_grant["net_wallet_points"] == 0
            assert full_offset_grant["wallet_order_id"] is None
            final_debt = store.get_reward_debt(
                tenant_id=tenant,
                owner_user_id=inviter,
            )
            assert final_debt is not None
            assert final_debt["outstanding_points"] == 0
            assert final_debt["lifetime_assessed_points"] == 98
            assert final_debt["lifetime_recovered_points"] == 98
            debt_recoveries = store.list_reward_debt_recoveries(
                tenant_id=tenant,
                owner_user_id=inviter,
            )
            assert len(debt_recoveries) == 2
            assert sorted(
                row["recovered_points"] for row in debt_recoveries
            ) == [24, 49]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT balance_points
                    FROM product_point_accounts
                    WHERE owner_user_id = %s
                    """,
                    (inviter,),
                )
                assert cursor.fetchone()[0] == 500
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_growth_reward_debt_recoveries
                    WHERE reward_grant_id = %s
                    """,
                    (full_offset_grant["id"],),
                )
                assert cursor.fetchone()[0] == 1

            with connection.cursor() as cursor:
                cursor.execute(MIGRATION.read_text(encoding="utf-8"))

            with pytest.raises(psycopg.Error):
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE product_growth_business_events
                        SET outcome_reason = 'mutated'
                        WHERE id = %s
                        """,
                        (registration.event["id"],),
                    )
            connection.rollback()
            with pytest.raises(psycopg.Error):
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE product_growth_reward_debt_recoveries
                        SET recovered_points = recovered_points + 1
                        WHERE id = %s
                        """,
                        (debt_recoveries[0]["id"],),
                    )
            connection.rollback()
            with pytest.raises(psycopg.Error):
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE product_growth_invite_codes
                        SET metadata = '{"changed": true}'::jsonb
                        WHERE id = %s
                        """,
                        (issued.record["id"],),
                    )
            connection.rollback()
            with pytest.raises(psycopg.Error):
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE product_growth_invite_relations
                        SET inviter_user_id = 'mutated-inviter'
                        WHERE id = %s
                        """,
                        (relation["id"],),
                    )
            connection.rollback()
        finally:
            connection.close()
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql_module.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql_module.Identifier(schema)
                )
            )
        admin.close()
