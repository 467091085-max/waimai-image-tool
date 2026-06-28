from __future__ import annotations

from dataclasses import dataclass
from typing import Final


LEVEL_AGENT: Final = "agent"
LEVEL_STANDARD: Final = LEVEL_AGENT

EVENT_INVITEE_REGISTERED: Final = "invitee_registered"
EVENT_FIRST_IMAGE_COMPLETED: Final = "first_image_completed"
EVENT_FIRST_PAYMENT: Final = "first_payment"
EVENT_PAYMENT: Final = "payment"

POINTS_PER_YUAN: Final = 10
CENTS_PER_YUAN: Final = 100
MAX_AGENT_COMMISSION_DEPTH: Final = 1
MAX_CONSUMER_REFERRAL_DEPTH: Final = 1

_AGENT_RULE: Final = {
    "first_order_bps": 2000,
    "repeat_order_bps": 2000,
    "instant_points": 200,
}

_EVENT_ALIASES: Final = {
    EVENT_INVITEE_REGISTERED: EVENT_INVITEE_REGISTERED,
    "registration": EVENT_INVITEE_REGISTERED,
    "registered": EVENT_INVITEE_REGISTERED,
    EVENT_FIRST_IMAGE_COMPLETED: EVENT_FIRST_IMAGE_COMPLETED,
    "first_generation_completed": EVENT_FIRST_IMAGE_COMPLETED,
    "first_output_completed": EVENT_FIRST_IMAGE_COMPLETED,
    EVENT_FIRST_PAYMENT: EVENT_FIRST_PAYMENT,
    EVENT_PAYMENT: EVENT_PAYMENT,
    "first_paid_order": EVENT_FIRST_PAYMENT,
    "first_paid": EVENT_FIRST_PAYMENT,
    "paid_order": EVENT_PAYMENT,
    "recharge": EVENT_PAYMENT,
}

_PAYMENT_REWARD_MIN_CENTS: Final = 1
CONSUMER_REFERRER_REGISTRATION_POINTS: Final = 50
CONSUMER_INVITEE_REGISTRATION_POINTS: Final = 50
CONSUMER_REFERRER_FIRST_IMAGE_POINTS: Final = 30
CONSUMER_FIRST_RECHARGE_REBATE_PERCENT: Final = 10
CONSUMER_REGISTRATION_DEVICE_LIMIT_24H: Final = 2
CONSUMER_REGISTRATION_IP_LIMIT_24H: Final = 5

_REFERRER_REGISTRATION_POINTS: Final = CONSUMER_REFERRER_REGISTRATION_POINTS
_INVITEE_REGISTRATION_POINTS: Final = CONSUMER_INVITEE_REGISTRATION_POINTS
_REFERRER_FIRST_IMAGE_POINTS: Final = CONSUMER_REFERRER_FIRST_IMAGE_POINTS
_REFERRER_PAYMENT_REBATE_RATE: Final = CONSUMER_FIRST_RECHARGE_REBATE_PERCENT


@dataclass(frozen=True)
class InviteRewardDecision:
    allowed: bool
    reasons: tuple[str, ...]
    inviter_points: int = 0
    invitee_points: int = 0


def agent_commission(level: str, paid_cents: int, is_first_order: bool = True) -> int:
    """Return the agent cash commission in cents.

    Only cash paid_cents participates in commission calculation. Gifted points
    should stay outside this function and never be converted into commission.
    Fractional cents are rounded down.
    """
    rules = _agent_rules(level)
    cents = _non_negative_int(paid_cents, "paid_cents")
    if not isinstance(is_first_order, bool):
        raise TypeError("is_first_order must be a bool")

    rate_key = "first_order_bps" if is_first_order else "repeat_order_bps"
    return cents * rules[rate_key] // 10000


def agent_instant_points(level: str) -> int:
    """Return points granted immediately for a first payment by agent level."""
    return _agent_rules(level)["instant_points"]


def consumer_referral_rewards(event: str, paid_cents: int = 0) -> dict[str, int | str]:
    """Return inviter and invitee point rewards for a consumer referral event."""
    event_name = _normalize_event(event)
    cents = _non_negative_int(paid_cents, "paid_cents")

    if event_name == EVENT_INVITEE_REGISTERED:
        return _reward(
            event_name,
            inviter_points=_REFERRER_REGISTRATION_POINTS,
            invitee_points=_INVITEE_REGISTRATION_POINTS,
        )

    if event_name == EVENT_FIRST_IMAGE_COMPLETED:
        return _reward(event_name, inviter_points=_REFERRER_FIRST_IMAGE_POINTS)

    if event_name == EVENT_PAYMENT:
        return _reward(event_name)

    if event_name == EVENT_FIRST_PAYMENT:
        if cents < _PAYMENT_REWARD_MIN_CENTS:
            return _reward(event_name)

        paid_points = _cash_points(cents)
        inviter_payment_points = paid_points * _REFERRER_PAYMENT_REBATE_RATE // 100

        return _reward(
            event_name,
            inviter_points=inviter_payment_points,
            inviter_payment_points=inviter_payment_points,
        )

    raise ValueError(f"unsupported referral event: {event!r}")


def consumer_first_recharge_rewards(
    paid_cents: int,
    *,
    is_first_recharge: bool = True,
    depth: int = 1,
) -> dict[str, int | str]:
    """Return direct consumer invite rewards for first recharge only."""
    validate_consumer_referral_depth(depth)
    if not isinstance(is_first_recharge, bool):
        raise TypeError("is_first_recharge must be a bool")
    if not is_first_recharge:
        return consumer_referral_rewards(EVENT_PAYMENT, paid_cents=paid_cents)
    return consumer_referral_rewards(EVENT_FIRST_PAYMENT, paid_cents=paid_cents)


def qualifies_for_commission(
    paid_cents: int,
    *,
    gifted_points: int = 0,
    is_refunded: bool = False,
) -> bool:
    """Return whether an order can produce agent commission.

    Gifted points are accepted for caller clarity, but are ignored by design:
    an order funded only by gifted points does not qualify for cash commission.
    """
    cents = _non_negative_int(paid_cents, "paid_cents")
    _non_negative_int(gifted_points, "gifted_points")
    if not isinstance(is_refunded, bool):
        raise TypeError("is_refunded must be a bool")

    return cents > 0 and not is_refunded


def validate_agent_commission_depth(depth: int) -> int:
    """Return depth when it is inside the one-level compliance boundary."""
    value = _positive_int(depth, "depth")
    if value > MAX_AGENT_COMMISSION_DEPTH:
        raise ValueError("multi-level agent commission is disabled for compliance")
    return value


def validate_consumer_referral_depth(depth: int) -> int:
    """Return depth when it is inside the one-level referral boundary."""
    value = _positive_int(depth, "depth")
    if value > MAX_CONSUMER_REFERRAL_DEPTH:
        raise ValueError("multi-level consumer referral rewards are disabled for compliance")
    return value


def registration_reward_allowed(
    *,
    phone_verified: bool,
    human_verified: bool,
    same_device_recent_registrations: int = 0,
    same_ip_recent_registrations: int = 0,
    same_phone_registered: bool = False,
    risk_blocked: bool = False,
) -> bool:
    """Return whether registration rewards can be granted.

    A real phone number and human verification are required before any
    invitation registration points are issued.
    """
    return invite_registration_reward_decision(
        phone_verified=phone_verified,
        human_verified=human_verified,
        same_phone_registered=same_phone_registered,
        same_device_recent_registrations=same_device_recent_registrations,
        same_ip_recent_registrations=same_ip_recent_registrations,
        risk_blocked=risk_blocked,
    ).allowed


def invite_registration_reward_decision(
    *,
    phone_verified: bool,
    human_verified: bool,
    same_phone_registered: bool = False,
    same_device_recent_registrations: int = 0,
    same_ip_recent_registrations: int = 0,
    self_invite: bool = False,
    reward_already_claimed: bool = False,
    risk_blocked: bool = False,
    device_limit_24h: int = CONSUMER_REGISTRATION_DEVICE_LIMIT_24H,
    ip_limit_24h: int = CONSUMER_REGISTRATION_IP_LIMIT_24H,
) -> InviteRewardDecision:
    """Return whether a direct invite registration reward can be granted."""
    _require_bool(phone_verified, "phone_verified")
    _require_bool(human_verified, "human_verified")
    _require_bool(same_phone_registered, "same_phone_registered")
    _require_bool(self_invite, "self_invite")
    _require_bool(reward_already_claimed, "reward_already_claimed")
    _require_bool(risk_blocked, "risk_blocked")
    device_count = _non_negative_int(
        same_device_recent_registrations,
        "same_device_recent_registrations",
    )
    ip_count = _non_negative_int(
        same_ip_recent_registrations,
        "same_ip_recent_registrations",
    )
    device_limit = _non_negative_int(device_limit_24h, "device_limit_24h")
    ip_limit = _non_negative_int(ip_limit_24h, "ip_limit_24h")

    reasons: list[str] = []
    if not phone_verified:
        reasons.append("phone_not_verified")
    if not human_verified:
        reasons.append("human_not_verified")
    if self_invite:
        reasons.append("self_invite")
    if reward_already_claimed:
        reasons.append("reward_already_claimed")
    if same_phone_registered:
        reasons.append("phone_already_registered")
    if risk_blocked:
        reasons.append("risk_blocked")
    if device_count >= device_limit:
        reasons.append("device_registration_limit_reached")
    if ip_count >= ip_limit:
        reasons.append("ip_registration_limit_reached")

    if reasons:
        return InviteRewardDecision(False, tuple(reasons))

    return InviteRewardDecision(
        True,
        (),
        inviter_points=CONSUMER_REFERRER_REGISTRATION_POINTS,
        invitee_points=CONSUMER_INVITEE_REGISTRATION_POINTS,
    )


def _agent_rules(level: str) -> dict[str, int]:
    if not isinstance(level, str):
        raise TypeError("level must be a string")
    normalized = level.strip().lower()
    if normalized in {LEVEL_AGENT, "standard", "default"}:
        return dict(_AGENT_RULE)
    raise ValueError(f"unsupported agent level: {level!r}")


def _normalize_event(event: str) -> str:
    if not isinstance(event, str):
        raise TypeError("event must be a string")
    normalized = event.strip().lower()
    try:
        return _EVENT_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported referral event: {event!r}") from exc


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _require_bool(value: bool, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")


def _cash_points(paid_cents: int) -> int:
    return paid_cents * POINTS_PER_YUAN // CENTS_PER_YUAN


def _reward(
    event: str,
    *,
    inviter_points: int = 0,
    invitee_points: int = 0,
    inviter_payment_points: int = 0,
    inviter_bonus_points: int = 0,
    invitee_rebate_points: int = 0,
) -> dict[str, int | str]:
    return {
        "event": event,
        "inviter_points": inviter_points,
        "invitee_points": invitee_points,
        "inviter_payment_points": inviter_payment_points,
        "inviter_bonus_points": inviter_bonus_points,
        "invitee_rebate_points": invitee_rebate_points,
    }
