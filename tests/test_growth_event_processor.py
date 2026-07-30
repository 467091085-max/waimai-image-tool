from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from shared import product_growth_outbox
from worker import growth_event_processor as processor


class FakeCursor:
    def __init__(self) -> None:
        self.closed = False
        self.effects: list[str] = []

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    autocommit = False

    def __init__(self) -> None:
        self.cursors: list[FakeCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeOutbox:
    def __init__(self, claims: list[dict[str, Any]]) -> None:
        self.claims = claims
        self.claim_calls: list[dict[str, Any]] = []
        self.retries: list[dict[str, Any]] = []
        self.dead_letters: list[dict[str, Any]] = []
        self.retry_result_status = "pending"

    def claim(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.claim_calls.append(kwargs)
        return list(self.claims)

    def mark_retry(self, **kwargs: Any) -> Any:
        self.retries.append(kwargs)
        return SimpleNamespace(event={"status": self.retry_result_status})

    def mark_dead_letter(self, **kwargs: Any) -> Any:
        self.dead_letters.append(kwargs)
        return SimpleNamespace(event={"status": "dead_letter"})


def claimed_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "id": "growth-event-1",
        "event_type": product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
        "status": "claimed",
        "claim_token": "claim-token-1",
        "fence": 1,
        "payload": {
            "orderId": "payment-order-1",
            "customerId": "customer-1",
        },
    }
    event.update(overrides)
    return event


def test_business_effect_and_success_share_caller_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    calls: list[dict[str, Any]] = []

    def handler(cursor: FakeCursor, event: dict[str, Any]) -> dict[str, Any]:
        cursor.effects.append(event["payload"]["orderId"])
        return {"rewardId": "reward-1"}

    def mark(cursor: FakeCursor, **kwargs: Any) -> Any:
        calls.append({"cursor": cursor, **kwargs})
        assert cursor.effects == ["payment-order-1"]
        return SimpleNamespace(idempotent=False)

    monkeypatch.setattr(
        product_growth_outbox,
        "mark_growth_event_succeeded",
        mark,
    )

    result = processor.process_claimed_event(
        connection,
        claimed_event(),
        handler=handler,
    )

    assert result["status"] == "succeeded"
    assert result["result"] == {"rewardId": "reward-1"}
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.cursors[0].closed is True
    assert calls[0]["event_id"] == "growth-event-1"
    assert calls[0]["fence"] == 1


def test_success_fence_loss_rolls_back_business_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()

    def handler(cursor: FakeCursor, _event: dict[str, Any]) -> dict[str, Any]:
        cursor.effects.append("credited")
        return {"rewardId": "reward-1"}

    def lose_claim(*_args: Any, **_kwargs: Any) -> Any:
        raise product_growth_outbox.GrowthOutboxClaimLost(
            event_id="growth-event-1",
            expected_fence=1,
            current_status="claimed",
            current_fence=2,
        )

    monkeypatch.setattr(
        product_growth_outbox,
        "mark_growth_event_succeeded",
        lose_claim,
    )

    with pytest.raises(product_growth_outbox.GrowthOutboxClaimLost):
        processor.process_claimed_event(
            connection,
            claimed_event(),
            handler=handler,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.cursors[0].effects == ["credited"]
    assert connection.cursors[0].closed is True


def test_once_routes_transient_and_permanent_failures_without_batch_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = [
        claimed_event(id="transient"),
        claimed_event(id="permanent"),
        claimed_event(id="success"),
    ]
    outbox = FakeOutbox(claims)
    connection = FakeConnection()
    monkeypatch.setattr(
        product_growth_outbox,
        "mark_growth_event_succeeded",
        lambda *_args, **_kwargs: SimpleNamespace(idempotent=False),
    )

    def handler(_cursor: FakeCursor, event: dict[str, Any]) -> dict[str, Any]:
        if event["id"] == "transient":
            raise RuntimeError("database temporarily unavailable")
        if event["id"] == "permanent":
            raise processor.PermanentGrowthEventError("invalid relation")
        return {"rewardId": "reward-success"}

    report = processor.process_growth_events_once(
        outbox,
        connection,
        handler=handler,
        worker_id="growth-worker-1",
        retry_in_seconds=7,
    )

    assert report["claimed"] == 3
    assert report["succeeded"] == 1
    assert report["retried"] == 1
    assert report["deadLettered"] == 1
    assert report["failed"] == 0
    assert outbox.retries == [
        {
            "event_id": "transient",
            "claim_token": "claim-token-1",
            "fence": 1,
            "error": "database temporarily unavailable",
            "retry_in_seconds": 7,
        }
    ]
    assert outbox.dead_letters == [
        {
            "event_id": "permanent",
            "claim_token": "claim-token-1",
            "fence": 1,
            "error": "invalid relation",
        }
    ]


def test_claim_loss_does_not_retry_or_dead_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = FakeOutbox([claimed_event()])
    connection = FakeConnection()

    def lose_claim(*_args: Any, **_kwargs: Any) -> Any:
        raise product_growth_outbox.GrowthOutboxClaimLost(
            event_id="growth-event-1",
            expected_fence=1,
            current_status="claimed",
            current_fence=2,
        )

    monkeypatch.setattr(
        product_growth_outbox,
        "mark_growth_event_succeeded",
        lose_claim,
    )
    report = processor.process_growth_events_once(
        outbox,
        connection,
        handler=lambda _cursor, _event: {"rewardId": "reward-1"},
        worker_id="growth-worker-1",
    )

    assert report["claimLost"] == 1
    assert report["retried"] == 0
    assert report["deadLettered"] == 0
    assert outbox.retries == []
    assert outbox.dead_letters == []


@pytest.mark.parametrize(
    "claim",
    [
        {},
        claimed_event(status="pending"),
        claimed_event(payload=None),
        claimed_event(fence=0),
        claimed_event(event_type="growth.unsupported"),
    ],
)
def test_invalid_claim_is_permanent(claim: dict[str, Any]) -> None:
    with pytest.raises(processor.InvalidGrowthEventClaim):
        processor.process_claimed_event(
            FakeConnection(),
            claim,
            handler=lambda _cursor, _event: {},
        )
