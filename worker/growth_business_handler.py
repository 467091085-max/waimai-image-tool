from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import growth_rules
from shared import (
    product_finance_store,
    product_growth_outbox,
    product_growth_store,
)
from worker.growth_event_processor import PermanentGrowthEventError


DEFAULT_GROWTH_TENANT_ID = "waimai"
DEFAULT_GROWTH_ACTOR = "service:growth-event-worker"


class GrowthBusinessDependencyPending(RuntimeError):
    """A valid event arrived before its prerequisite business effect."""


class GrowthBusinessHandler:
    def __init__(
        self,
        *,
        tenant_id: str = DEFAULT_GROWTH_TENANT_ID,
        actor_user_id: str = DEFAULT_GROWTH_ACTOR,
    ) -> None:
        self.tenant_id = _required_text(tenant_id, "tenant_id")
        self.actor_user_id = _required_text(
            actor_user_id,
            "actor_user_id",
        )

    def __call__(
        self,
        cursor: Any,
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        event_type = _required_text(event.get("event_type"), "event_type")
        payload = _payload(event)
        _validate_contract(payload)
        try:
            if event_type == product_growth_outbox.EVENT_FIRST_PAYMENT_REWARD:
                return self._payment_succeeded(
                    cursor,
                    event,
                    payload,
                    include_consumer_reward=True,
                )
            if event_type == product_growth_outbox.EVENT_AGENT_COMMISSION:
                return self._payment_succeeded(
                    cursor,
                    event,
                    payload,
                    include_consumer_reward=False,
                )
            if event_type == product_growth_outbox.EVENT_PAYMENT_REFUND:
                return self._payment_refunded(cursor, event, payload)
            if event_type == product_growth_outbox.EVENT_INVITE_REWARD:
                return self._registration_reward(cursor, event, payload)
        except product_growth_store.ProductGrowthEventDeferred as exc:
            raise GrowthBusinessDependencyPending(str(exc)) from exc
        except product_finance_store.ProductFinanceNotFound as exc:
            raise GrowthBusinessDependencyPending(str(exc)) from exc
        except (
            product_growth_store.InvalidProductGrowthInput,
            product_growth_store.ProductGrowthNotFound,
            product_growth_store.ProductGrowthConflict,
            product_growth_store.ProductGrowthIntegrityError,
            product_finance_store.InvalidProductFinanceInput,
            product_finance_store.ProductFinanceConflict,
        ) as exc:
            raise PermanentGrowthEventError(str(exc)) from exc
        raise PermanentGrowthEventError(
            f"unsupported growth event type: {event_type}"
        )

    def _payment_succeeded(
        self,
        cursor: Any,
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        include_consumer_reward: bool,
    ) -> dict[str, Any]:
        customer_id = _required_text(payload.get("customerId"), "customerId")
        order_id = _required_text(payload.get("orderId"), "orderId")
        paid_cents = _positive_int(payload.get("paidCents"), "paidCents")
        source_event_id = _required_text(event.get("id"), "event.id")
        idempotency_key = _required_text(
            event.get("dedupe_key"),
            "event.dedupe_key",
        )

        consumer_result: dict[str, Any] | None = None
        if include_consumer_reward:
            invite = product_growth_store.get_invite_for_invitee(
                cursor,
                tenant_id=self.tenant_id,
                owner_user_id=customer_id,
            )
            if invite is not None:
                reward = product_growth_store.process_first_recharge_reward(
                    cursor,
                    tenant_id=self.tenant_id,
                    owner_user_id=customer_id,
                    invite_relation_id=str(invite["id"]),
                    source_event_id=source_event_id,
                    source_order_id=order_id,
                    paid_cents=paid_cents,
                    idempotency_key=idempotency_key,
                    source_payload=payload,
                    actor_user_id=self.actor_user_id,
                )
                consumer_result = _growth_reward_result(reward)

        classification = (
            product_growth_store.resolve_agent_payment_classification(
                cursor,
                tenant_id=self.tenant_id,
                owner_user_id=customer_id,
                source_order_id=order_id,
                paid_cents=paid_cents,
            )
        )
        commission_result = self._record_commission(
            cursor,
            classification=classification,
            payload=payload,
        )
        return {
            "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
            "consumerReward": consumer_result,
            "agentCommission": commission_result,
        }

    def _record_commission(
        self,
        cursor: Any,
        *,
        classification: (
            product_growth_store.AgentPaymentClassification | None
        ),
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if classification is None:
            return None
        _require_rule_version(classification.rule_version)
        commission_id = _commission_order_id(
            self.tenant_id,
            classification.source_order_id,
            classification.agent_id,
        )
        if classification.commission_amount_cents <= 0:
            return {
                "commissionOrderId": commission_id,
                "created": False,
                "skipped": True,
                "reason": "commission_rounds_to_zero",
            }

        product_finance_store.ensure_agent_finance_account(
            cursor,
            agent_id=classification.agent_id,
            metadata={
                "growthAgentOwnerUserId": (
                    classification.agent_owner_user_id
                ),
                "growthTenantId": self.tenant_id,
            },
        )
        created = product_finance_store.record_commission_order(
            cursor,
            commission_order_id=commission_id,
            source_order_id=classification.source_order_id,
            agent_id=classification.agent_id,
            customer_user_id=classification.customer_user_id,
            order_amount_cents=classification.paid_cents,
            commission_amount_cents=(
                classification.commission_amount_cents
            ),
            commission_rate_bps=classification.commission_rate_bps,
            status="pending",
            metadata={
                "bindingId": classification.binding_id,
                "canonicalFirstOrderId": (
                    classification.canonical_first_order_id
                ),
                "isFirstOrder": classification.is_first_order,
                "ruleVersion": classification.rule_version,
                "provider": str(payload.get("provider") or ""),
                "providerOrderId": str(
                    payload.get("providerOrderId") or ""
                ),
            },
        )
        return {
            "commissionOrderId": str(created.record["id"]),
            "created": created.created,
            "skipped": False,
            "commissionAmountCents": int(
                created.record["commission_amount_cents"]
            ),
            "commissionRateBps": int(
                created.record["commission_rate_bps"]
            ),
            "status": str(created.record["status"]),
        }

    def _payment_refunded(
        self,
        cursor: Any,
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        customer_id = _required_text(payload.get("customerId"), "customerId")
        order_id = _required_text(payload.get("orderId"), "orderId")
        paid_cents = _positive_int(payload.get("paidCents"), "paidCents")
        refund_cents = _positive_int(
            payload.get("refundCents"),
            "refundCents",
        )
        cumulative_refunded_cents = _nonnegative_int(
            payload.get("cumulativeRefundedCents"),
            "cumulativeRefundedCents",
        )
        cumulative_refunded_points = _nonnegative_int(
            payload.get("cumulativeRefundedPoints"),
            "cumulativeRefundedPoints",
        )
        source_event_id = _required_text(event.get("id"), "event.id")
        idempotency_key = _required_text(
            event.get("dedupe_key"),
            "event.dedupe_key",
        )

        consumer_result: dict[str, Any] | None = None
        invite = product_growth_store.get_invite_for_invitee(
            cursor,
            tenant_id=self.tenant_id,
            owner_user_id=customer_id,
        )
        if invite is not None:
            reversal = product_growth_store.process_first_recharge_refund(
                cursor,
                tenant_id=self.tenant_id,
                owner_user_id=customer_id,
                invite_relation_id=str(invite["id"]),
                source_event_id=source_event_id,
                source_order_id=order_id,
                refund_cents=refund_cents,
                cumulative_refunded_cents=cumulative_refunded_cents,
                cumulative_refunded_points=cumulative_refunded_points,
                idempotency_key=idempotency_key,
                source_payload=payload,
                actor_user_id=self.actor_user_id,
            )
            consumer_result = _growth_refund_result(reversal)

        classification = (
            product_growth_store.resolve_agent_payment_classification(
                cursor,
                tenant_id=self.tenant_id,
                owner_user_id=customer_id,
                source_order_id=order_id,
                paid_cents=paid_cents,
            )
        )
        commission_result: dict[str, Any] | None = None
        if (
            classification is not None
            and classification.commission_amount_cents > 0
        ):
            _require_rule_version(classification.rule_version)
            commission_order_id = _commission_order_id(
                self.tenant_id,
                order_id,
                classification.agent_id,
            )
            refund_id = product_growth_store.stable_growth_id(
                "commission_refund",
                self.tenant_id,
                source_event_id,
            )
            action_id = product_growth_store.stable_growth_id(
                "finance_action",
                self.tenant_id,
                source_event_id,
            )
            mutation = product_finance_store.apply_commission_refund(
                cursor,
                refund_id=refund_id,
                commission_order_id=commission_order_id,
                refund_amount_cents=refund_cents,
                cumulative_refunded_amount_cents=(
                    cumulative_refunded_cents
                ),
                action_id=action_id,
                actor_user_id=self.actor_user_id,
                idempotency_key=idempotency_key,
                metadata={
                    "providerEventId": str(
                        payload.get("providerEventId") or ""
                    ),
                    "ruleVersion": classification.rule_version,
                },
            )
            order = mutation.record.get("commission_order") or {}
            commission_result = {
                "commissionOrderId": commission_order_id,
                "commissionRefundId": str(mutation.record["id"]),
                "idempotent": mutation.idempotent,
                "cumulativeRefundedCents": int(
                    mutation.record["cumulative_refunded_amount_cents"]
                ),
                "cumulativeReversedCommissionCents": int(
                    mutation.record[
                        "cumulative_reversed_commission_cents"
                    ]
                ),
                "status": str(order.get("status") or ""),
            }
        return {
            "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
            "consumerRewardRefund": consumer_result,
            "agentCommissionRefund": commission_result,
        }

    def _registration_reward(
        self,
        cursor: Any,
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        invitee_id = _required_text(
            payload.get("inviteeUserId") or payload.get("customerId"),
            "inviteeUserId",
        )
        relation_id = _required_text(
            payload.get("inviteRelationId"),
            "inviteRelationId",
        )
        reward = product_growth_store.process_registration_reward(
            cursor,
            tenant_id=self.tenant_id,
            owner_user_id=invitee_id,
            invite_relation_id=relation_id,
            source_event_id=_required_text(event.get("id"), "event.id"),
            idempotency_key=_required_text(
                event.get("dedupe_key"),
                "event.dedupe_key",
            ),
            source_payload=payload,
            actor_user_id=self.actor_user_id,
        )
        return {
            "ruleVersion": growth_rules.GROWTH_RULE_VERSION,
            "registrationReward": _growth_reward_result(reward),
        }


def build_growth_business_handler(
    env: Mapping[str, str] | None = None,
) -> GrowthBusinessHandler:
    values = os.environ if env is None else env
    return GrowthBusinessHandler(
        tenant_id=(
            str(values.get("PRODUCT_GROWTH_TENANT_ID") or "").strip()
            or DEFAULT_GROWTH_TENANT_ID
        ),
        actor_user_id=(
            str(values.get("GROWTH_WORKER_ACTOR_ID") or "").strip()
            or DEFAULT_GROWTH_ACTOR
        ),
    )


def _commission_order_id(
    tenant_id: str,
    source_order_id: str,
    agent_id: str,
) -> str:
    return product_growth_store.stable_growth_id(
        "commission_order",
        tenant_id,
        source_order_id,
        agent_id,
    )


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    value = event.get("payload")
    if not isinstance(value, Mapping):
        raise PermanentGrowthEventError(
            "growth event payload must be an object"
        )
    return dict(value)


def _validate_contract(payload: Mapping[str, Any]) -> None:
    if payload.get("schemaVersion") != 1:
        raise PermanentGrowthEventError(
            "unsupported growth event schemaVersion"
        )
    _require_rule_version(payload.get("ruleVersion"))


def _require_rule_version(value: Any) -> None:
    if str(value or "") != growth_rules.GROWTH_RULE_VERSION:
        raise PermanentGrowthEventError(
            "growth event ruleVersion does not match the active rule"
        )


def _growth_reward_result(
    result: product_growth_store.GrowthEventApplyResult,
) -> dict[str, Any]:
    return {
        "eventId": str(result.event["id"]),
        "applied": result.applied,
        "idempotent": result.idempotent,
        "outcome": str(result.event["outcome"]),
        "grantIds": [str(row["id"]) for row in result.grants],
    }


def _growth_refund_result(
    result: product_growth_store.GrowthRefundApplyResult,
) -> dict[str, Any]:
    return {
        "eventId": str(result.event["id"]),
        "applied": result.applied,
        "idempotent": result.idempotent,
        "reversalIds": [str(row["id"]) for row in result.reversals],
        "pointsAssessed": result.points_assessed,
        "pointsRecovered": result.points_recovered,
        "outstandingPointsAdded": result.outstanding_points_added,
        "outstandingPointsAfter": result.outstanding_points_after,
    }


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PermanentGrowthEventError(f"{field} is required")
    return text


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PermanentGrowthEventError(
            f"{field} must be a positive integer"
        ) from exc
    if isinstance(value, bool) or number <= 0:
        raise PermanentGrowthEventError(
            f"{field} must be a positive integer"
        )
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PermanentGrowthEventError(
            f"{field} must be a nonnegative integer"
        ) from exc
    if isinstance(value, bool) or number < 0:
        raise PermanentGrowthEventError(
            f"{field} must be a nonnegative integer"
        )
    return number
