from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from shared import product_growth_outbox as growth_outbox


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "006_product_growth_outbox_postgres.sql"
DEFAULT_PAYLOAD = {
    "customerId": "customer-1",
    "orderId": "payment-order-1",
    "paidCents": 9800,
}


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
        parameters: tuple[Any, ...] = (),
    ) -> None:
        match = re.search(
            r"/\* product_growth_outbox:([a-z_]+) \*/",
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
        self.rows.clear()
        return rows

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    autocommit = False

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


def event_row(
    *,
    event_type: str = growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    effective_payload = dict(payload or DEFAULT_PAYLOAD)
    effective_key = dedupe_key or growth_outbox.stable_growth_dedupe_key(
        event_type,
        effective_payload["orderId"],
    )
    payload_json = json.dumps(
        effective_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    row = {
        "id": (
            "growth_evt_"
            + hashlib.sha256(effective_key.encode("utf-8")).hexdigest()[:48]
        ),
        "event_type": event_type,
        "dedupe_key": effective_key,
        "payload": effective_payload,
        "payload_sha256": hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest(),
        "status": "pending",
        "max_attempts": 8,
        "attempt_count": 0,
        "available_at": "2026-07-30T00:00:00Z",
        "claimed_by": None,
        "claim_token": None,
        "claimed_until": None,
        "fence": 0,
        "result_payload": {},
        "last_error": "",
        "succeeded_at": None,
        "dead_lettered_at": None,
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
    }
    row.update(overrides)
    return row


def enqueue_args(
    *,
    event_type: str = growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 8,
) -> dict[str, Any]:
    effective_payload = dict(payload or DEFAULT_PAYLOAD)
    return {
        "event_type": event_type,
        "dedupe_key": dedupe_key
        or growth_outbox.stable_growth_dedupe_key(
            event_type,
            effective_payload["orderId"],
        ),
        "payload": effective_payload,
        "max_attempts": max_attempts,
    }


def claimed_row(**overrides: Any) -> dict[str, Any]:
    row = event_row(
        status="claimed",
        attempt_count=1,
        claimed_by="growth-worker-1",
        claim_token="claim-token-1",
        claimed_until="2026-07-30T00:01:00Z",
        fence=1,
    )
    row.update(overrides)
    return row


def test_migration_defines_durable_growth_business_event_states() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS product_growth_outbox" in sql
    for event_type in growth_outbox.SUPPORTED_EVENT_TYPES:
        assert f"'{event_type}'" in sql
    assert "UNIQUE (dedupe_key)" in sql
    assert "payload_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "'pending', 'claimed', 'succeeded', 'dead_letter'" in sql
    assert "attempt_count BETWEEN 0 AND max_attempts" in sql
    assert "claimed_until TIMESTAMPTZ" in sql
    assert "fence BIGINT NOT NULL DEFAULT 0" in sql
    assert "succeeded_at TIMESTAMPTZ" in sql
    assert "dead_lettered_at TIMESTAMPTZ" in sql


def test_stable_dedupe_key_is_canonical_and_business_scoped() -> None:
    first = growth_outbox.stable_growth_dedupe_key(
        growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
        "payment-order-1",
        "customer-1",
    )
    replay = growth_outbox.stable_growth_dedupe_key(
        growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
        "payment-order-1",
        "customer-1",
    )
    other_business_event = growth_outbox.stable_growth_dedupe_key(
        growth_outbox.EVENT_AGENT_COMMISSION,
        "payment-order-1",
        "customer-1",
    )
    refund_event = growth_outbox.stable_growth_dedupe_key(
        growth_outbox.EVENT_PAYMENT_REFUND,
        "payment-order-1",
        "provider-refund-event-1",
    )

    assert first == replay
    assert first.startswith("growth:first-payment:")
    assert first != other_business_event
    assert refund_event.startswith("growth:payment-refund:")
    assert refund_event not in {first, other_business_event}
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}", first)

    with pytest.raises(
        growth_outbox.InvalidGrowthOutboxInput,
        match="identity part",
    ):
        growth_outbox.stable_growth_dedupe_key(
            growth_outbox.EVENT_INVITE_REWARD
        )


def test_external_cursor_insert_does_not_commit_or_touch_wallet() -> None:
    row = event_row()
    connection = ScriptedConnection(insert_event=[[row]])
    cursor = connection.cursor()

    result = growth_outbox.enqueue_growth_event(cursor, **enqueue_args())

    assert result.created is True
    assert result.event == row
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert [call[0] for call in connection.calls] == ["insert_event"]
    inserted_payload = connection.calls[0][2][3]
    assert inserted_payload == json.dumps(
        DEFAULT_PAYLOAD,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    source = (
        ROOT / "shared" / "product_growth_outbox.py"
    ).read_text(encoding="utf-8")
    assert "product_point_accounts" not in source
    assert "product_point_orders" not in source
    assert "billing" not in source
    assert "growth_service" not in source


def test_same_dedupe_and_canonical_payload_is_idempotent() -> None:
    row = event_row(payload={"paidCents": 9800, "orderId": "payment-order-1", "customerId": "customer-1"})
    connection = ScriptedConnection(
        insert_event=[[]],
        select_by_dedupe_for_update=[[row]],
    )

    result = growth_outbox.enqueue_growth_event(
        connection.cursor(),
        **enqueue_args(
            payload={
                "customerId": "customer-1",
                "orderId": "payment-order-1",
                "paidCents": 9800,
            }
        ),
    )

    assert result.created is False
    assert result.event["id"] == row["id"]
    assert [call[0] for call in connection.calls] == [
        "insert_event",
        "select_by_dedupe_for_update",
    ]
    assert "FOR UPDATE" in connection.calls[1][1]


def test_same_dedupe_with_payload_or_delivery_policy_drift_conflicts() -> None:
    original = event_row()
    payload_drift = ScriptedConnection(
        insert_event=[[]],
        select_by_dedupe_for_update=[[original]],
    )

    with pytest.raises(growth_outbox.GrowthEventConflict):
        growth_outbox.enqueue_growth_event(
            payload_drift.cursor(),
            **enqueue_args(
                payload={**DEFAULT_PAYLOAD, "paidCents": 9900},
            ),
        )

    attempts_drift = ScriptedConnection(
        insert_event=[[]],
        select_by_dedupe_for_update=[[original]],
    )
    with pytest.raises(growth_outbox.GrowthEventConflict):
        growth_outbox.enqueue_growth_event(
            attempts_drift.cursor(),
            **enqueue_args(max_attempts=9),
        )


def test_connection_owned_enqueue_commits_and_failure_rolls_back() -> None:
    created = ScriptedConnection(insert_event=[[event_row()]])

    result = growth_outbox.ProductGrowthOutbox(created).enqueue(
        **enqueue_args()
    )

    assert result.created is True
    assert created.commits == 1
    assert created.rollbacks == 0
    assert created.cursors[0].closed is True

    failed = ScriptedConnection(
        insert_event=[RuntimeError("database unavailable")]
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        growth_outbox.ProductGrowthOutbox(failed).enqueue(**enqueue_args())
    assert failed.commits == 0
    assert failed.rollbacks == 1
    assert failed.cursors[0].closed is True

    commit_failed = ScriptedConnection(
        insert_event=[[event_row()]],
        commit_error=RuntimeError("commit failed"),
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        growth_outbox.ProductGrowthOutbox(commit_failed).enqueue(
            **enqueue_args()
        )
    assert commit_failed.commits == 1
    assert commit_failed.rollbacks == 1


def test_autocommit_and_invalid_business_inputs_fail_closed() -> None:
    with pytest.raises(
        growth_outbox.InvalidGrowthOutboxInput,
        match="autocommit-disabled",
    ):
        growth_outbox.ProductGrowthOutbox(AutocommitConnection())

    connection = ScriptedConnection()
    cursor = connection.cursor()
    with pytest.raises(
        growth_outbox.InvalidGrowthOutboxInput,
        match="unsupported",
    ):
        growth_outbox.enqueue_growth_event(
            cursor,
            event_type="growth.unknown.requested",
            dedupe_key="growth:unknown:1",
            payload={},
        )
    with pytest.raises(
        growth_outbox.InvalidGrowthOutboxInput,
        match="JSON-compatible",
    ):
        growth_outbox.enqueue_growth_event(
            cursor,
            event_type=growth_outbox.EVENT_INVITE_REWARD,
            dedupe_key="growth:invite:1",
            payload={"invalid": float("nan")},
        )
    with pytest.raises(
        growth_outbox.InvalidGrowthOutboxInput,
        match="max_attempts",
    ):
        growth_outbox.enqueue_growth_event(
            cursor,
            event_type=growth_outbox.EVENT_INVITE_REWARD,
            dedupe_key="growth:invite:1",
            payload={},
            max_attempts=0,
        )
    assert connection.calls == []


def test_claim_uses_skip_locked_lease_and_monotonic_fence() -> None:
    row = claimed_row()
    connection = ScriptedConnection(
        expire_exhausted_claims=[[]],
        claim=[[row]],
    )

    claims = growth_outbox.ProductGrowthOutbox(connection).claim(
        worker_id="growth-worker-1",
        limit=3,
        lease_seconds=90,
        claim_token="claim-token-1",
    )

    assert claims == [row]
    assert connection.commits == 1
    assert [call[0] for call in connection.calls] == [
        "expire_exhausted_claims",
        "claim",
    ]
    expire_sql = connection.calls[0][1]
    claim_sql = connection.calls[1][1]
    assert "FOR UPDATE SKIP LOCKED" in expire_sql
    assert "attempt_count >= max_attempts" in expire_sql
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "claimed_until < CURRENT_TIMESTAMP" in claim_sql
    assert "fence = o.fence + 1" in claim_sql
    assert "attempt_count = o.attempt_count + 1" in claim_sql
    assert connection.calls[1][2] == (
        3,
        "growth-worker-1",
        "claim-token-1",
        90,
    )


def test_claim_renewal_and_old_fence_are_enforced() -> None:
    renewed = claimed_row(claimed_until="2026-07-30T00:02:00Z")
    success_connection = ScriptedConnection(renew_claim=[[renewed]])

    result = growth_outbox.ProductGrowthOutbox(
        success_connection
    ).renew_claim(
        event_id=renewed["id"],
        claim_token="claim-token-1",
        fence=1,
        lease_seconds=120,
    )

    assert result == renewed
    renew_sql = success_connection.calls[0][1]
    assert "claimed_until >= CURRENT_TIMESTAMP" in renew_sql
    assert "claim_token = %s" in renew_sql
    assert "fence = %s" in renew_sql

    newer_claim = claimed_row(
        claim_token="claim-token-2",
        fence=2,
        attempt_count=2,
    )
    lost_connection = ScriptedConnection(
        renew_claim=[[]],
        select_by_id=[[newer_claim]],
    )
    with pytest.raises(growth_outbox.GrowthOutboxClaimLost) as captured:
        growth_outbox.ProductGrowthOutbox(lost_connection).renew_claim(
            event_id=newer_claim["id"],
            claim_token="claim-token-1",
            fence=1,
        )
    assert captured.value.expected_fence == 1
    assert captured.value.current_fence == 2
    assert lost_connection.rollbacks == 1


def test_success_is_persistent_idempotent_and_content_checked() -> None:
    result_payload = {"commissionId": "commission-1"}
    succeeded = claimed_row(
        status="succeeded",
        result_payload=result_payload,
        succeeded_at="2026-07-30T00:00:30Z",
    )
    connection = ScriptedConnection(mark_succeeded=[[succeeded]])

    result = growth_outbox.ProductGrowthOutbox(
        connection
    ).mark_succeeded(
        event_id=succeeded["id"],
        claim_token="claim-token-1",
        fence=1,
        result_payload=result_payload,
    )

    assert result.idempotent is False
    assert result.event["status"] == "succeeded"
    sql = connection.calls[0][1]
    assert "status = 'succeeded'" in sql
    assert "succeeded_at = CURRENT_TIMESTAMP" in sql
    assert "claimed_until >= CURRENT_TIMESTAMP" in sql

    replay_connection = ScriptedConnection(
        mark_succeeded=[[]],
        select_by_id=[[succeeded]],
    )
    replay = growth_outbox.ProductGrowthOutbox(
        replay_connection
    ).mark_succeeded(
        event_id=succeeded["id"],
        claim_token="claim-token-1",
        fence=1,
        result_payload=result_payload,
    )
    assert replay.idempotent is True

    drift_connection = ScriptedConnection(
        mark_succeeded=[[]],
        select_by_id=[[succeeded]],
    )
    with pytest.raises(growth_outbox.GrowthOutboxClaimLost):
        growth_outbox.ProductGrowthOutbox(
            drift_connection
        ).mark_succeeded(
            event_id=succeeded["id"],
            claim_token="claim-token-1",
            fence=1,
            result_payload={"commissionId": "different"},
        )


def test_cursor_owned_success_does_not_commit_and_preserves_fence_check() -> None:
    result_payload = {"commissionId": "commission-1"}
    succeeded = claimed_row(
        status="succeeded",
        result_payload=result_payload,
        succeeded_at="2026-07-30T00:00:30Z",
    )
    connection = ScriptedConnection(mark_succeeded=[[succeeded]])
    cursor = connection.cursor()

    result = growth_outbox.mark_growth_event_succeeded(
        cursor,
        event_id=succeeded["id"],
        claim_token="claim-token-1",
        fence=1,
        result_payload=result_payload,
    )

    assert result.idempotent is False
    assert result.event["status"] == "succeeded"
    assert connection.commits == 0
    assert connection.rollbacks == 0

    newer_claim = claimed_row(
        claim_token="claim-token-2",
        fence=2,
        attempt_count=2,
    )
    lost_connection = ScriptedConnection(
        mark_succeeded=[[]],
        select_by_id=[[newer_claim]],
    )
    with pytest.raises(growth_outbox.GrowthOutboxClaimLost):
        growth_outbox.mark_growth_event_succeeded(
            lost_connection.cursor(),
            event_id=newer_claim["id"],
            claim_token="claim-token-1",
            fence=1,
            result_payload=result_payload,
        )
    assert lost_connection.commits == 0
    assert lost_connection.rollbacks == 0


def test_retry_persists_schedule_and_exhaustion_becomes_dead_letter() -> None:
    pending = claimed_row(
        status="pending",
        claimed_by=None,
        claim_token=None,
        claimed_until=None,
        last_error="temporary failure",
    )
    retry_connection = ScriptedConnection(mark_retry=[[pending]])

    retried = growth_outbox.ProductGrowthOutbox(
        retry_connection
    ).mark_retry(
        event_id=pending["id"],
        claim_token="claim-token-1",
        fence=1,
        error="temporary failure",
        retry_in_seconds=45,
    )

    assert retried.event["status"] == "pending"
    retry_sql = retry_connection.calls[0][1]
    assert "CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')" in retry_sql
    assert "attempt_count >= max_attempts" in retry_sql
    assert "ELSE NULL" in retry_sql
    assert retry_connection.calls[0][2][0] == 45

    dead = claimed_row(
        status="dead_letter",
        max_attempts=1,
        attempt_count=1,
        last_error="permanent failure",
        dead_lettered_at="2026-07-30T00:00:30Z",
    )
    dead_connection = ScriptedConnection(mark_retry=[[dead]])
    exhausted = growth_outbox.ProductGrowthOutbox(
        dead_connection
    ).mark_retry(
        event_id=dead["id"],
        claim_token="claim-token-1",
        fence=1,
        error="permanent failure",
    )

    assert exhausted.event["status"] == "dead_letter"
    assert exhausted.event["attempt_count"] == 1
    assert exhausted.event["claim_token"] == "claim-token-1"


def test_explicit_dead_letter_is_persistent_and_idempotent() -> None:
    dead = claimed_row(
        status="dead_letter",
        last_error="invalid growth relation",
        dead_lettered_at="2026-07-30T00:00:30Z",
    )
    connection = ScriptedConnection(mark_dead_letter=[[dead]])

    result = growth_outbox.ProductGrowthOutbox(
        connection
    ).mark_dead_letter(
        event_id=dead["id"],
        claim_token="claim-token-1",
        fence=1,
        error="invalid growth relation",
    )

    assert result.idempotent is False
    assert result.event["status"] == "dead_letter"

    replay_connection = ScriptedConnection(
        mark_dead_letter=[[]],
        select_by_id=[[dead]],
    )
    replay = growth_outbox.ProductGrowthOutbox(
        replay_connection
    ).mark_dead_letter(
        event_id=dead["id"],
        claim_token="claim-token-1",
        fence=1,
        error="invalid growth relation",
    )
    assert replay.idempotent is True


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_growth_outbox_protocol() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ["TEST_POSTGRES_DSN"]
    schema = f"test_growth_outbox_{uuid4().hex}"

    admin_connection = psycopg.connect(dsn, autocommit=True)
    try:
        with admin_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin_connection.close()

    connection = psycopg.connect(dsn, autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()

        migration_sql = MIGRATION.read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute(migration_sql)

        rolled_back_key = growth_outbox.stable_growth_dedupe_key(
            growth_outbox.EVENT_INVITE_REWARD,
            "invite-rollback",
        )
        with connection.cursor() as cursor:
            growth_outbox.enqueue_growth_event(
                cursor,
                event_type=growth_outbox.EVENT_INVITE_REWARD,
                dedupe_key=rolled_back_key,
                payload={"inviteId": "invite-rollback"},
            )
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM product_growth_outbox "
                "WHERE dedupe_key = %s",
                (rolled_back_key,),
            )
            assert cursor.fetchone()[0] == 0
        connection.commit()

        store = growth_outbox.ProductGrowthOutbox(connection)
        first_key = growth_outbox.stable_growth_dedupe_key(
            growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
            "payment-real-1",
        )
        invite_key = growth_outbox.stable_growth_dedupe_key(
            growth_outbox.EVENT_INVITE_REWARD,
            "invite-real-1",
        )
        commission_key = growth_outbox.stable_growth_dedupe_key(
            growth_outbox.EVENT_AGENT_COMMISSION,
            "payment-real-1",
            "agent-real-1",
        )
        store.enqueue(
            event_type=growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
            dedupe_key=first_key,
            payload={
                "customerId": "customer-real-1",
                "orderId": "payment-real-1",
                "paidCents": 9800,
            },
        )
        replay = store.enqueue(
            event_type=growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
            dedupe_key=first_key,
            payload={
                "paidCents": 9800,
                "orderId": "payment-real-1",
                "customerId": "customer-real-1",
            },
        )
        assert replay.created is False
        with pytest.raises(growth_outbox.GrowthEventConflict):
            store.enqueue(
                event_type=growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
                dedupe_key=first_key,
                payload={
                    "customerId": "customer-real-1",
                    "orderId": "payment-real-1",
                    "paidCents": 9900,
                },
            )

        store.enqueue(
            event_type=growth_outbox.EVENT_INVITE_REWARD,
            dedupe_key=invite_key,
            payload={
                "inviteId": "invite-real-1",
                "inviteeUserId": "customer-real-1",
                "inviterUserId": "inviter-real-1",
            },
        )
        store.enqueue(
            event_type=growth_outbox.EVENT_AGENT_COMMISSION,
            dedupe_key=commission_key,
            payload={
                "agentId": "agent-real-1",
                "customerId": "customer-real-1",
                "orderId": "payment-real-1",
                "paidCents": 9800,
            },
            max_attempts=1,
        )

        claims = store.claim(
            worker_id="growth-worker-real-1",
            limit=10,
            lease_seconds=60,
            claim_token="claim-real-1",
        )
        assert len(claims) == 3
        claims_by_key = {row["dedupe_key"]: row for row in claims}
        assert all(row["fence"] == 1 for row in claims)

        first_claim = claims_by_key[first_key]
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE growth_effect_probe "
                "(event_id TEXT PRIMARY KEY)"
            )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO growth_effect_probe (event_id) VALUES (%s)",
                (first_claim["id"],),
            )
            with pytest.raises(growth_outbox.GrowthOutboxClaimLost):
                growth_outbox.mark_growth_event_succeeded(
                    cursor,
                    event_id=first_claim["id"],
                    claim_token="claim-real-1",
                    fence=2,
                    result_payload={
                        "promotionEventId": "must-roll-back",
                    },
                )
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM growth_effect_probe")
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT status, fence FROM product_growth_outbox "
                "WHERE id = %s",
                (first_claim["id"],),
            )
            assert cursor.fetchone() == ("claimed", 1)
        connection.commit()

        store.mark_succeeded(
            event_id=first_claim["id"],
            claim_token="claim-real-1",
            fence=1,
            result_payload={"promotionEventId": "promo-real-1"},
        )

        invite_claim = claims_by_key[invite_key]
        store.mark_retry(
            event_id=invite_claim["id"],
            claim_token="claim-real-1",
            fence=1,
            error="temporary relation lock",
            retry_in_seconds=0,
        )
        with pytest.raises(growth_outbox.GrowthOutboxClaimLost):
            store.mark_succeeded(
                event_id=invite_claim["id"],
                claim_token="claim-real-1",
                fence=1,
            )

        commission_claim = claims_by_key[commission_key]
        exhausted = store.mark_retry(
            event_id=commission_claim["id"],
            claim_token="claim-real-1",
            fence=1,
            error="invalid agent relation",
            retry_in_seconds=0,
        )
        assert exhausted.event["status"] == "dead_letter"

        reclaimed = store.claim(
            worker_id="growth-worker-real-2",
            limit=10,
            lease_seconds=60,
            claim_token="claim-real-2",
        )
        assert len(reclaimed) == 1
        assert reclaimed[0]["dedupe_key"] == invite_key
        assert reclaimed[0]["fence"] == 2
        store.mark_succeeded(
            event_id=reclaimed[0]["id"],
            claim_token="claim-real-2",
            fence=2,
            result_payload={"inviteRewardId": "invite-reward-real-1"},
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT dedupe_key, status, attempt_count, fence "
                "FROM product_growth_outbox ORDER BY dedupe_key"
            )
            persisted = {
                row[0]: {
                    "status": row[1],
                    "attempt_count": row[2],
                    "fence": row[3],
                }
                for row in cursor.fetchall()
            }
        connection.commit()
        assert persisted[first_key]["status"] == "succeeded"
        assert persisted[invite_key] == {
            "status": "succeeded",
            "attempt_count": 2,
            "fence": 2,
        }
        assert persisted[commission_key]["status"] == "dead_letter"
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("SET search_path TO public")
        connection.commit()
        connection.close()

        cleanup_connection = psycopg.connect(dsn, autocommit=True)
        try:
            with cleanup_connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cleanup_connection.close()
