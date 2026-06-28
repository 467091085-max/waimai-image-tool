from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

import growth_rules
from storage_db import json_dumps, json_loads, new_id, utc_now


class GrowthServiceError(ValueError):
    code = "growth_service_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}


class GrowthConflict(GrowthServiceError):
    code = "growth_conflict"


class GrowthNotFound(GrowthServiceError):
    code = "growth_not_found"


class InvalidGrowthInput(GrowthServiceError):
    code = "invalid_growth_input"


def create_agent_profile(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    agent_code: str = "",
    level: str = growth_rules.LEVEL_STANDARD,
    status: str = "active",
    settlement_account: Mapping[str, Any] | None = None,
    contact: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    user_id = _required_text(user_id, "user_id")
    level = _clean_level(level)
    status = _choice(status, {"active", "inactive", "suspended", "pending"}, "agent status")
    existing = _one_dict(
        conn,
        "SELECT * FROM agent_profiles WHERE user_id = ? ORDER BY created_at ASC, id ASC LIMIT 1",
        (user_id,),
    )
    if existing is not None:
        payload = _agent_from_row(existing)
        payload["idempotent"] = True
        return payload

    now = utc_now()
    record_id = agent_id or new_id("agent")
    with conn:
        conn.execute(
            """
            INSERT INTO agent_profiles (
                id, user_id, agent_code, level, status, settlement_account_json,
                contact_json, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                str(agent_code or ""),
                level,
                status,
                json_dumps(dict(settlement_account or {})),
                json_dumps(dict(contact or {})),
                json_dumps(dict(metadata or {})),
                now,
                now,
            ),
        )
    payload = _agent_from_row(_require_row(conn, "agent_profiles", record_id))
    payload["idempotent"] = False
    return payload


def bind_agent_customer(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    customer_id: str,
    source: str = "",
    metadata: Mapping[str, Any] | None = None,
    relation_id: str | None = None,
) -> dict[str, Any]:
    agent_id = _required_text(agent_id, "agent_id")
    customer_id = _required_text(customer_id, "customer_id")
    growth_rules.validate_agent_commission_depth(1)
    agent = _one_dict(conn, "SELECT * FROM agent_profiles WHERE id = ?", (agent_id,))
    if agent is None:
        raise GrowthNotFound("agent profile not found", agentId=agent_id)
    if agent["status"] != "active":
        raise GrowthConflict("agent profile is not active", agentId=agent_id, status=agent["status"])
    if str(agent["user_id"]) == customer_id:
        raise GrowthConflict("agent cannot bind self as customer", agentId=agent_id, customerId=customer_id)

    existing = _one_dict(
        conn,
        """
        SELECT * FROM agent_customer_relations
        WHERE customer_id = ? AND status IN ('pending', 'active')
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (customer_id,),
    )
    if existing is not None:
        if existing["agent_id"] != agent_id:
            raise GrowthConflict(
                "customer already belongs to another agent",
                customerId=customer_id,
                existingAgentId=existing["agent_id"],
            )
        payload = _relation_from_row(existing)
        payload["idempotent"] = True
        return payload

    now = utc_now()
    record_id = relation_id or new_id("agentrel")
    with conn:
        conn.execute(
            """
            INSERT INTO agent_customer_relations (
                id, agent_id, customer_id, source, status, bound_at,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                record_id,
                agent_id,
                customer_id,
                str(source or ""),
                now,
                json_dumps(dict(metadata or {})),
                now,
                now,
            ),
        )
    payload = _relation_from_row(_require_row(conn, "agent_customer_relations", record_id))
    payload["idempotent"] = False
    return payload


def accept_consumer_invite(
    conn: sqlite3.Connection,
    *,
    inviter_user_id: str,
    invitee_user_id: str,
    invite_code: str = "",
    agent_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    phone_verified: bool = False,
    human_verified: bool = False,
    same_device_recent_registrations: int = 0,
    same_ip_recent_registrations: int = 0,
    same_phone_registered: bool = False,
    risk_blocked: bool = False,
    relation_id: str | None = None,
) -> dict[str, Any]:
    inviter_user_id = _required_text(inviter_user_id, "inviter_user_id")
    invitee_user_id = _required_text(invitee_user_id, "invitee_user_id")
    if inviter_user_id == invitee_user_id:
        raise GrowthConflict("self invitation is not allowed", userId=invitee_user_id)
    growth_rules.validate_consumer_referral_depth(1)

    existing = _one_dict(
        conn,
        """
        SELECT * FROM invite_relations
        WHERE invitee_user_id = ? AND status NOT IN ('canceled', 'expired')
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (invitee_user_id,),
    )
    if existing is not None:
        if existing["inviter_user_id"] != inviter_user_id:
            raise GrowthConflict(
                "invitee already belongs to another inviter",
                inviteeUserId=invitee_user_id,
                existingInviterUserId=existing["inviter_user_id"],
            )
        payload = _invite_from_row(existing)
        payload["idempotent"] = True
        payload["registrationRewards"] = _pending_registration_rewards(payload)
        return payload

    reward_allowed = growth_rules.registration_reward_allowed(
        phone_verified=phone_verified,
        human_verified=human_verified,
        same_device_recent_registrations=same_device_recent_registrations,
        same_ip_recent_registrations=same_ip_recent_registrations,
        same_phone_registered=same_phone_registered,
        risk_blocked=risk_blocked,
    )
    rewards = (
        growth_rules.consumer_referral_rewards(growth_rules.EVENT_INVITEE_REGISTERED)
        if reward_allowed
        else growth_rules.consumer_referral_rewards(growth_rules.EVENT_PAYMENT)
    )
    reward_points = int(rewards["inviter_points"]) + int(rewards["invitee_points"])
    now = utc_now()
    record_id = relation_id or new_id("invite")
    metadata_payload = {
        **dict(metadata or {}),
        "registrationRewardAllowed": reward_allowed,
        "registrationRewards": rewards,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO invite_relations (
                id, inviter_user_id, invitee_user_id, agent_id, invite_code,
                status, reward_points, reward_status, accepted_at, metadata_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                inviter_user_id,
                invitee_user_id,
                str(agent_id or ""),
                str(invite_code or ""),
                reward_points,
                "pending" if reward_points > 0 else "canceled",
                now,
                json_dumps(metadata_payload),
                now,
                now,
            ),
        )
    payload = _invite_from_row(_require_row(conn, "invite_relations", record_id))
    payload["idempotent"] = False
    payload["registrationRewards"] = _pending_registration_rewards(payload)
    return payload


def mark_invite_reward_granted(conn: sqlite3.Connection, invite_id: str) -> dict[str, Any]:
    invite_id = _required_text(invite_id, "invite_id")
    row = _one_dict(conn, "SELECT * FROM invite_relations WHERE id = ?", (invite_id,))
    if row is None:
        raise GrowthNotFound("invite relation not found", inviteId=invite_id)
    if row["reward_status"] == "granted":
        payload = _invite_from_row(row)
        payload["idempotent"] = True
        return payload
    now = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE invite_relations
            SET status = 'rewarded', reward_status = 'granted',
                rewarded_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, invite_id),
        )
    payload = _invite_from_row(_require_row(conn, "invite_relations", invite_id))
    payload["idempotent"] = False
    return payload


def record_payment_growth(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    customer_id: str,
    paid_cents: int,
    source: str = "payment",
    request_id: str = "",
) -> dict[str, Any]:
    order_id = _required_text(order_id, "order_id")
    customer_id = _required_text(customer_id, "customer_id")
    paid_cents = _non_negative_int(paid_cents, "paid_cents")
    response: dict[str, Any] = {
        "ok": True,
        "orderId": order_id,
        "customerId": customer_id,
        "agentCommission": None,
        "consumerReferralReward": None,
    }

    response["agentCommission"] = _create_agent_commission(
        conn,
        order_id=order_id,
        customer_id=customer_id,
        paid_cents=paid_cents,
        source=source,
    )
    response["consumerReferralReward"] = _create_first_payment_reward(
        conn,
        order_id=order_id,
        customer_id=customer_id,
        paid_cents=paid_cents,
        request_id=request_id or order_id,
    )
    return response


def record_payment_refund(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    customer_id: str,
    paid_cents: int,
    refund_cents: int,
    source: str = "payment_refund",
    request_id: str = "",
) -> dict[str, Any]:
    order_id = _required_text(order_id, "order_id")
    customer_id = _required_text(customer_id, "customer_id")
    paid_cents = _non_negative_int(paid_cents, "paid_cents")
    refund_cents = min(_non_negative_int(refund_cents, "refund_cents"), paid_cents)
    request_id = str(request_id or order_id)
    return {
        "ok": True,
        "orderId": order_id,
        "customerId": customer_id,
        "refundCents": refund_cents,
        "agentCommissionRefund": _apply_agent_commission_refund(
            conn,
            order_id=order_id,
            paid_cents=paid_cents,
            refund_cents=refund_cents,
            source=source,
            request_id=request_id,
        ),
        "consumerReferralRefund": _apply_first_payment_referral_refund(
            conn,
            order_id=order_id,
            customer_id=customer_id,
            paid_cents=paid_cents,
            refund_cents=refund_cents,
            source=source,
            request_id=request_id,
        ),
    }


def _create_agent_commission(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    customer_id: str,
    paid_cents: int,
    source: str,
) -> dict[str, Any] | None:
    relation = _active_agent_relation(conn, customer_id)
    if relation is None:
        return None
    if not growth_rules.qualifies_for_commission(paid_cents):
        return None

    existing = _one_dict(
        conn,
        "SELECT * FROM commission_orders WHERE order_id = ? AND relation_id = ? LIMIT 1",
        (order_id, relation["id"]),
    )
    if existing is not None:
        payload = _commission_from_row(existing)
        payload["idempotent"] = True
        return payload

    agent = _one_dict(conn, "SELECT * FROM agent_profiles WHERE id = ?", (relation["agent_id"],))
    if agent is None:
        return None
    commission_cents = growth_rules.agent_commission(str(agent["level"] or growth_rules.LEVEL_STANDARD), paid_cents)
    if commission_cents <= 0:
        return None

    now = utc_now()
    commission_id = _deterministic_id("commission", order_id, relation["id"])
    with conn:
        conn.execute(
            """
            INSERT INTO commission_orders (
                id, order_id, agent_id, customer_id, relation_id, order_amount,
                commission_amount, commission_rate_bps, currency, status, source,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 2000, 'CNY', 'pending', ?, ?, ?, ?)
            """,
            (
                commission_id,
                order_id,
                relation["agent_id"],
                customer_id,
                relation["id"],
                paid_cents,
                commission_cents,
                source,
                json_dumps({"rule": "direct_agent_20_percent", "depth": 1}),
                now,
                now,
            ),
        )
    payload = _commission_from_row(_require_row(conn, "commission_orders", commission_id))
    payload["idempotent"] = False
    return payload


def _apply_agent_commission_refund(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    paid_cents: int,
    refund_cents: int,
    source: str,
    request_id: str,
) -> dict[str, Any] | None:
    row = _one_dict(
        conn,
        """
        SELECT * FROM commission_orders
        WHERE order_id = ?
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (order_id,),
    )
    if row is None:
        return None
    current = _commission_from_row(row)
    metadata = dict(current.get("metadata") or {})
    refunds = metadata.get("refundAdjustments")
    if not isinstance(refunds, list):
        refunds = []
    if any(str(item.get("requestId")) == request_id for item in refunds if isinstance(item, Mapping)):
        current["idempotent"] = True
        return current

    net_paid_cents = max(paid_cents - refund_cents, 0)
    agent = _one_dict(conn, "SELECT * FROM agent_profiles WHERE id = ?", (current["agentId"],))
    level = str(agent.get("level") if agent else growth_rules.LEVEL_STANDARD)
    adjusted_commission = growth_rules.agent_commission(level, net_paid_cents) if net_paid_cents > 0 else 0
    current_status = str(row["status"])
    settlement_id = str(row["settlement_id"] or "")
    next_status = current_status
    clawback_needed = False
    if current_status in {"canceled", "refunded"}:
        next_status = current_status
    elif current_status == "settled":
        next_status = "refunded"
        clawback_needed = True
    elif adjusted_commission <= 0:
        next_status = "refunded"

    refunds.append(
        {
            "requestId": request_id,
            "source": source,
            "paidCents": paid_cents,
            "refundCents": refund_cents,
            "netPaidCents": net_paid_cents,
            "previousCommissionAmount": current["commissionAmount"],
            "adjustedCommissionAmount": adjusted_commission,
            "clawbackNeeded": clawback_needed,
            "createdAt": utc_now(),
        }
    )
    metadata["refundAdjustments"] = refunds
    if clawback_needed:
        metadata["clawbackNeeded"] = True

    effective_settlement_id = settlement_id
    if next_status == "refunded" and current_status != "settled":
        effective_settlement_id = ""
    now = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE commission_orders
            SET order_amount = ?, commission_amount = ?, status = ?,
                settlement_id = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                net_paid_cents,
                adjusted_commission,
                next_status,
                effective_settlement_id,
                json_dumps(metadata),
                now,
                current["id"],
            ),
        )
    if settlement_id and settlement_id != effective_settlement_id:
        _recalculate_settlement_totals(conn, settlement_id)
    updated = _commission_from_row(_require_row(conn, "commission_orders", current["id"]))
    updated["idempotent"] = False
    updated["clawbackNeeded"] = clawback_needed
    return updated


def _create_first_payment_reward(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    customer_id: str,
    paid_cents: int,
    request_id: str,
) -> dict[str, Any] | None:
    invite = _active_invite_for_customer(conn, customer_id)
    if invite is None:
        return None
    if not _is_first_paid_order(conn, customer_id, order_id):
        return None

    event_type = "consumer_first_payment_reward"
    existing = _one_dict(
        conn,
        """
        SELECT * FROM promotion_event_logs
        WHERE user_id = ? AND event_type = ? AND request_id = ?
        LIMIT 1
        """,
        (customer_id, event_type, order_id),
    )
    rewards = growth_rules.consumer_referral_rewards(growth_rules.EVENT_FIRST_PAYMENT, paid_cents=paid_cents)
    inviter_points = int(rewards["inviter_points"])
    if existing is not None:
        return {
            "id": existing["id"],
            "idempotent": True,
            "inviteId": invite["id"],
            "inviterUserId": invite["inviter_user_id"],
            "inviteeUserId": customer_id,
            "inviterPoints": 0,
            "eventType": event_type,
        }
    if inviter_points <= 0:
        return None

    now = utc_now()
    event_id = _deterministic_id("promo", order_id, invite["id"], event_type)
    with conn:
        conn.execute(
            """
            INSERT INTO promotion_event_logs (
                id, agent_id, user_id, customer_id, event_type, channel,
                campaign_id, request_id, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'referral', ?, ?, ?, ?)
            """,
            (
                event_id,
                str(invite["agent_id"] or ""),
                customer_id,
                customer_id,
                event_type,
                str(invite["invite_code"] or ""),
                order_id,
                json_dumps({"inviteId": invite["id"], "rewards": rewards, "depth": 1}),
                now,
            ),
        )
    return {
        "id": event_id,
        "idempotent": False,
        "inviteId": invite["id"],
        "inviterUserId": invite["inviter_user_id"],
        "inviteeUserId": customer_id,
        "inviterPoints": inviter_points,
        "eventType": event_type,
    }


def _apply_first_payment_referral_refund(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    customer_id: str,
    paid_cents: int,
    refund_cents: int,
    source: str,
    request_id: str,
) -> dict[str, Any] | None:
    event = _one_dict(
        conn,
        """
        SELECT * FROM promotion_event_logs
        WHERE user_id = ? AND event_type = 'consumer_first_payment_reward' AND request_id = ?
        LIMIT 1
        """,
        (customer_id, order_id),
    )
    if event is None:
        return None
    existing_refund = _one_dict(
        conn,
        """
        SELECT * FROM promotion_event_logs
        WHERE user_id = ? AND event_type = 'consumer_first_payment_refund' AND request_id = ?
        LIMIT 1
        """,
        (customer_id, order_id),
    )
    event_metadata = json_loads(event.get("metadata_json"), {})
    if not isinstance(event_metadata, dict):
        event_metadata = {}
    rewards = event_metadata.get("rewards") if isinstance(event_metadata.get("rewards"), Mapping) else {}
    original_points = int(rewards.get("inviter_points") or 0)
    invite_id = str(event_metadata.get("inviteId") or "")
    invite = _one_dict(conn, "SELECT * FROM invite_relations WHERE id = ?", (invite_id,)) if invite_id else None
    if invite is None:
        return None
    if existing_refund is not None:
        return {
            "id": existing_refund["id"],
            "idempotent": True,
            "inviteId": invite_id,
            "inviterUserId": invite["inviter_user_id"],
            "inviteeUserId": customer_id,
            "inviterPointsToDebit": 0,
            "eventType": "consumer_first_payment_refund",
        }

    net_paid_cents = max(paid_cents - refund_cents, 0)
    adjusted_rewards = growth_rules.consumer_referral_rewards(
        growth_rules.EVENT_FIRST_PAYMENT,
        paid_cents=net_paid_cents,
    )
    adjusted_points = int(adjusted_rewards["inviter_points"])
    points_to_debit = max(original_points - adjusted_points, 0)
    refund_event_id = _deterministic_id("promo", order_id, invite_id, "consumer_first_payment_refund")
    with conn:
        conn.execute(
            """
            INSERT INTO promotion_event_logs (
                id, agent_id, user_id, customer_id, event_type, channel,
                campaign_id, request_id, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, 'consumer_first_payment_refund', 'referral', ?, ?, ?, ?)
            """,
            (
                refund_event_id,
                str(invite["agent_id"] or ""),
                customer_id,
                customer_id,
                str(event["campaign_id"] or ""),
                order_id,
                json_dumps(
                    {
                        "source": source,
                        "originalEventId": event["id"],
                        "inviteId": invite_id,
                        "paidCents": paid_cents,
                        "refundCents": refund_cents,
                        "netPaidCents": net_paid_cents,
                        "originalInviterPoints": original_points,
                        "adjustedInviterPoints": adjusted_points,
                        "inviterPointsToDebit": points_to_debit,
                    }
                ),
                utc_now(),
            ),
        )
    return {
        "id": refund_event_id,
        "idempotent": False,
        "inviteId": invite_id,
        "inviterUserId": invite["inviter_user_id"],
        "inviteeUserId": customer_id,
        "inviterPointsToDebit": points_to_debit,
        "eventType": "consumer_first_payment_refund",
    }


def _recalculate_settlement_totals(conn: sqlite3.Connection, settlement_id: str) -> None:
    totals = _one_dict(
        conn,
        """
        SELECT
            COALESCE(SUM(order_amount), 0) AS total_order_amount,
            COALESCE(SUM(commission_amount), 0) AS total_commission_amount,
            COUNT(*) AS order_count
        FROM commission_orders
        WHERE settlement_id = ? AND status = 'eligible'
        """,
        (settlement_id,),
    ) or {"total_order_amount": 0, "total_commission_amount": 0, "order_count": 0}
    with conn:
        conn.execute(
            """
            UPDATE commission_settlements
            SET total_order_amount = ?, total_commission_amount = ?,
                order_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(totals["total_order_amount"] or 0),
                int(totals["total_commission_amount"] or 0),
                int(totals["order_count"] or 0),
                utc_now(),
                settlement_id,
            ),
        )


def _pending_registration_rewards(invite: Mapping[str, Any]) -> dict[str, int]:
    if invite.get("reward_status") != "pending":
        return {"inviterPoints": 0, "inviteePoints": 0}
    metadata = invite.get("metadata") if isinstance(invite.get("metadata"), Mapping) else {}
    rewards = metadata.get("registrationRewards") if isinstance(metadata.get("registrationRewards"), Mapping) else {}
    return {
        "inviterPoints": int(rewards.get("inviter_points") or 0),
        "inviteePoints": int(rewards.get("invitee_points") or 0),
    }


def _active_agent_relation(conn: sqlite3.Connection, customer_id: str) -> dict[str, Any] | None:
    return _one_dict(
        conn,
        """
        SELECT * FROM agent_customer_relations
        WHERE customer_id = ? AND status = 'active'
        ORDER BY bound_at ASC, id ASC
        LIMIT 1
        """,
        (customer_id,),
    )


def _active_invite_for_customer(conn: sqlite3.Connection, customer_id: str) -> dict[str, Any] | None:
    return _one_dict(
        conn,
        """
        SELECT * FROM invite_relations
        WHERE invitee_user_id = ? AND status IN ('accepted', 'rewarded')
        ORDER BY accepted_at ASC, id ASC
        LIMIT 1
        """,
        (customer_id,),
    )


def _is_first_paid_order(conn: sqlite3.Connection, customer_id: str, order_id: str) -> bool:
    count = conn.execute(
        "SELECT COUNT(*) FROM payment_orders WHERE user_id = ? AND status = 'paid'",
        (customer_id,),
    ).fetchone()[0]
    if int(count) <= 1:
        return True
    first_order = conn.execute(
        """
        SELECT order_id FROM payment_orders
        WHERE user_id = ? AND status = 'paid'
        ORDER BY paid_at ASC, created_at ASC, order_id ASC
        LIMIT 1
        """,
        (customer_id,),
    ).fetchone()
    return first_order is not None and first_order[0] == order_id


def _agent_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "userId": data["user_id"],
        "agentCode": data["agent_code"],
        "level": data["level"],
        "status": data["status"],
        "settlementAccount": json_loads(data.get("settlement_account_json"), {}),
        "contact": json_loads(data.get("contact_json"), {}),
        "metadata": json_loads(data.get("metadata_json"), {}),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _relation_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "agentId": data["agent_id"],
        "customerId": data["customer_id"],
        "source": data["source"],
        "status": data["status"],
        "boundAt": data["bound_at"],
        "releasedAt": data.get("released_at"),
        "metadata": json_loads(data.get("metadata_json"), {}),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _invite_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "inviterUserId": data["inviter_user_id"],
        "inviteeUserId": data["invitee_user_id"],
        "agentId": data["agent_id"],
        "inviteCode": data["invite_code"],
        "status": data["status"],
        "rewardPoints": int(data["reward_points"]),
        "reward_status": data["reward_status"],
        "rewardStatus": data["reward_status"],
        "acceptedAt": data.get("accepted_at"),
        "rewardedAt": data.get("rewarded_at"),
        "metadata": json_loads(data.get("metadata_json"), {}),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _commission_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "orderId": data["order_id"],
        "agentId": data["agent_id"],
        "customerId": data["customer_id"],
        "relationId": data["relation_id"],
        "orderAmount": int(data["order_amount"]),
        "commissionAmount": int(data["commission_amount"]),
        "commissionRateBps": int(data["commission_rate_bps"]),
        "currency": data["currency"],
        "status": data["status"],
        "source": data["source"],
        "metadata": json_loads(data.get("metadata_json"), {}),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
        "settledAt": data.get("settled_at"),
    }


def _require_row(conn: sqlite3.Connection, table: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise GrowthNotFound(f"{table} record not found", table=table, id=record_id)
    return row


def _one_dict(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [description[0] for description in cursor.description]
    return dict(zip(columns, row))


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidGrowthInput(f"{field_name} is required")
    return text


def _choice(value: Any, allowed: set[str], label: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise InvalidGrowthInput(f"invalid {label}: {value}")
    return text


def _clean_level(value: Any) -> str:
    text = str(value or growth_rules.LEVEL_STANDARD).strip().lower()
    growth_rules.agent_commission(text, 0)
    return "standard" if text in {"agent", "default"} else text


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidGrowthInput(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidGrowthInput(f"{name} must be an integer") from exc
    if number < 0:
        raise InvalidGrowthInput(f"{name} must be non-negative")
    return number


def _deterministic_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1(":".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"
