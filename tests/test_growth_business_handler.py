from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import growth_rules
from shared import (
    product_finance_store,
    product_growth_outbox,
    product_growth_store,
)
from worker import growth_business_handler as handler_module
from worker.growth_event_processor import PermanentGrowthEventError


def event(
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "growth_evt_123",
        "event_type": event_type,
        "dedupe_key": "growth:first-payment:123",
        "payload": (
            {
                "schemaVersion": 1,
                "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
                "orderId": "payment-order-1",
                "customerId": "customer-1",
                "provider": "alipay",
                "providerOrderId": "provider-order-1",
                "paidCents": 10_000,
                "points": 1000,
                "requestId": "payment-order-1",
            }
            if payload is None
            else payload
        ),
    }


def classification(
    *,
    commission_amount_cents: int = 2_000,
) -> product_growth_store.AgentPaymentClassification:
    return product_growth_store.AgentPaymentClassification(
        tenant_id="waimai",
        customer_user_id="customer-1",
        agent_id="growth_agent_" + ("a" * 40),
        agent_owner_user_id="agent-owner-1",
        binding_id="growth_binding_" + ("b" * 40),
        source_order_id="payment-order-1",
        canonical_first_order_id="payment-order-1",
        paid_cents=10_000,
        is_first_order=True,
        commission_rate_bps=2000,
        commission_amount_cents=commission_amount_cents,
        rule_version=growth_rules.GROWTH_RULE_VERSION,
    )


def reward_result() -> product_growth_store.GrowthEventApplyResult:
    return product_growth_store.GrowthEventApplyResult(
        event={
            "id": "growth_event_" + ("c" * 40),
            "outcome": "applied",
        },
        grants=({"id": "growth_grant_" + ("d" * 40)},),
        wallet_accounts=(),
        applied=True,
        idempotent=False,
    )


def refund_result() -> product_growth_store.GrowthRefundApplyResult:
    return product_growth_store.GrowthRefundApplyResult(
        event={
            "id": "growth_event_" + ("e" * 40),
            "outcome": "applied",
        },
        reversals=({"id": "growth_reversal_" + ("f" * 40)},),
        wallet_account=None,
        cumulative_refunded_cents=5_000,
        cumulative_reversed_points=50,
        points_assessed=50,
        points_recovered=20,
        outstanding_points_added=30,
        outstanding_points_after=30,
        applied=True,
        idempotent=False,
    )


def test_payment_applies_consumer_reward_and_pending_commission_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    invite = {"id": "growth_invite_" + ("1" * 40)}
    classified = classification()

    monkeypatch.setattr(
        product_growth_store,
        "get_invite_for_invitee",
        lambda cursor, **kwargs: invite,
    )

    def process_reward(cursor: Any, **kwargs: Any):
        calls.append(("reward", kwargs))
        return reward_result()

    def classify(cursor: Any, **kwargs: Any):
        calls.append(("classify", kwargs))
        return classified

    def ensure_account(cursor: Any, **kwargs: Any):
        calls.append(("account", kwargs))
        return product_finance_store.FinanceCreateResult(
            record={"agent_id": kwargs["agent_id"]},
            created=True,
        )

    def record_commission(cursor: Any, **kwargs: Any):
        calls.append(("commission", kwargs))
        return product_finance_store.FinanceCreateResult(
            record={
                "id": kwargs["commission_order_id"],
                "commission_amount_cents": kwargs[
                    "commission_amount_cents"
                ],
                "commission_rate_bps": kwargs["commission_rate_bps"],
                "status": kwargs["status"],
            },
            created=True,
        )

    monkeypatch.setattr(
        product_growth_store,
        "process_first_recharge_reward",
        process_reward,
    )
    monkeypatch.setattr(
        product_growth_store,
        "resolve_agent_payment_classification",
        classify,
    )
    monkeypatch.setattr(
        product_finance_store,
        "ensure_agent_finance_account",
        ensure_account,
    )
    monkeypatch.setattr(
        product_finance_store,
        "record_commission_order",
        record_commission,
    )

    result = handler_module.GrowthBusinessHandler()(object(), event(
        product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD
    ))

    assert [name for name, _ in calls] == [
        "reward",
        "classify",
        "account",
        "commission",
    ]
    assert calls[-1][1]["status"] == "pending"
    assert calls[-1][1]["commission_rate_bps"] == 2000
    assert result["consumerReward"]["applied"] is True
    assert result["agentCommission"]["commissionAmountCents"] == 2_000


def test_payment_without_relations_is_a_successful_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        product_growth_store,
        "get_invite_for_invitee",
        lambda cursor, **kwargs: None,
    )
    monkeypatch.setattr(
        product_growth_store,
        "resolve_agent_payment_classification",
        lambda cursor, **kwargs: None,
    )
    result = handler_module.GrowthBusinessHandler()(object(), event(
        product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD
    ))
    assert result["consumerReward"] is None
    assert result["agentCommission"] is None


def test_zero_commission_does_not_create_finance_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        product_growth_store,
        "get_invite_for_invitee",
        lambda cursor, **kwargs: None,
    )
    monkeypatch.setattr(
        product_growth_store,
        "resolve_agent_payment_classification",
        lambda cursor, **kwargs: classification(
            commission_amount_cents=0,
        ),
    )
    monkeypatch.setattr(
        product_finance_store,
        "ensure_agent_finance_account",
        lambda *args, **kwargs: pytest.fail("must not create an account"),
    )
    result = handler_module.GrowthBusinessHandler()(object(), event(
        product_growth_outbox.EVENT_AGENT_COMMISSION
    ))
    assert result["agentCommission"]["skipped"] is True


def test_refund_applies_consumer_reversal_and_commission_clawback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite = {"id": "growth_invite_" + ("1" * 40)}
    classified = classification()
    finance_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        product_growth_store,
        "get_invite_for_invitee",
        lambda cursor, **kwargs: invite,
    )
    monkeypatch.setattr(
        product_growth_store,
        "process_first_recharge_refund",
        lambda cursor, **kwargs: refund_result(),
    )
    monkeypatch.setattr(
        product_growth_store,
        "resolve_agent_payment_classification",
        lambda cursor, **kwargs: classified,
    )

    def apply_refund(cursor: Any, **kwargs: Any):
        finance_calls.append(kwargs)
        return product_finance_store.FinanceMutationResult(
            record={
                "id": kwargs["refund_id"],
                "cumulative_refunded_amount_cents": 5_000,
                "cumulative_reversed_commission_cents": 1_000,
                "commission_order": {"status": "pending"},
            },
            idempotent=False,
        )

    monkeypatch.setattr(
        product_finance_store,
        "apply_commission_refund",
        apply_refund,
    )
    payload = {
        **event(
            product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD
        )["payload"],
        "providerEventId": "provider-refund-1",
        "requestId": "provider-refund-1",
        "refundCents": 5_000,
        "cumulativeRefundedCents": 5_000,
        "cumulativeRefundedPoints": 500,
    }
    result = handler_module.GrowthBusinessHandler()(object(), event(
        product_growth_outbox.EVENT_PAYMENT_REFUND,
        payload=payload,
    ))
    assert finance_calls[0]["cumulative_refunded_amount_cents"] == 5_000
    assert result["consumerRewardRefund"]["pointsAssessed"] == 50
    assert (
        result["agentCommissionRefund"][
            "cumulativeReversedCommissionCents"
        ]
        == 1_000
    )


def test_registration_event_uses_relation_and_invitee_from_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def process(cursor: Any, **kwargs: Any):
        captured.append(kwargs)
        return reward_result()

    monkeypatch.setattr(
        product_growth_store,
        "process_registration_reward",
        process,
    )
    payload = {
        "schemaVersion": 1,
        "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
        "inviteRelationId": "growth_invite_" + ("1" * 40),
        "inviteeUserId": "customer-1",
    }
    result = handler_module.GrowthBusinessHandler()(object(), event(
        product_growth_outbox.EVENT_INVITE_REWARD,
        payload=payload,
    ))
    assert captured[0]["owner_user_id"] == "customer-1"
    assert captured[0]["invite_relation_id"] == payload["inviteRelationId"]
    assert result["registrationReward"]["applied"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schemaVersion": 2, "ruleVersion": growth_rules.GROWTH_RULE_VERSION},
        {"schemaVersion": 1, "ruleVersion": "stale-rule"},
    ],
)
def test_invalid_contract_is_permanent(payload: dict[str, Any]) -> None:
    with pytest.raises(PermanentGrowthEventError):
        handler_module.GrowthBusinessHandler()(object(), event(
            product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD,
            payload=payload,
        ))


def test_deferred_growth_dependency_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        product_growth_store,
        "get_invite_for_invitee",
        lambda cursor, **kwargs: {"id": "growth_invite_" + ("1" * 40)},
    )
    monkeypatch.setattr(
        product_growth_store,
        "process_first_recharge_reward",
        lambda cursor, **kwargs: (_ for _ in ()).throw(
            product_growth_store.ProductGrowthEventDeferred("wait")
        ),
    )
    with pytest.raises(
        handler_module.GrowthBusinessDependencyPending
    ) as captured:
        handler_module.GrowthBusinessHandler()(object(), event(
            product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD
        ))
    assert not isinstance(captured.value, PermanentGrowthEventError)


def test_rule_drift_in_agent_binding_is_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        product_growth_store,
        "get_invite_for_invitee",
        lambda cursor, **kwargs: None,
    )
    monkeypatch.setattr(
        product_growth_store,
        "resolve_agent_payment_classification",
        lambda cursor, **kwargs: replace(
            classification(),
            rule_version="stale-rule",
        ),
    )
    with pytest.raises(PermanentGrowthEventError):
        handler_module.GrowthBusinessHandler()(object(), event(
            product_growth_outbox.EVENT_AGENT_COMMISSION
        ))


def test_handler_configuration_is_server_owned() -> None:
    handler = handler_module.build_growth_business_handler(
        {
            "PRODUCT_GROWTH_TENANT_ID": "tenant-a",
            "GROWTH_WORKER_ACTOR_ID": "service:growth-a",
        }
    )
    assert handler.tenant_id == "tenant-a"
    assert handler.actor_user_id == "service:growth-a"
