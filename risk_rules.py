from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Final, Mapping


LEVEL_INFO: Final = "info"
LEVEL_LOW: Final = "low"
LEVEL_MEDIUM: Final = "medium"
LEVEL_HIGH: Final = "high"
LEVEL_CRITICAL: Final = "critical"

DECISION_ALLOW: Final = "allow"
DECISION_REVIEW: Final = "review"
DECISION_FREEZE: Final = "freeze"
DECISION_DENY: Final = "deny"

RISK_REGISTRATION_REWARD: Final = "registration_reward"
RISK_SMS_OTP: Final = "sms_otp"
RISK_INVITE_REWARD: Final = "invite_reward"
RISK_REFERRAL_REBATE: Final = "referral_rebate"
RISK_AGENT_COMMISSION: Final = "agent_commission"
RISK_DOWNLOAD: Final = "download"

REGISTRATION_PHONE_LIMIT_24H: Final = 1
REGISTRATION_DEVICE_LIMIT_24H: Final = 2
REGISTRATION_IP_LIMIT_24H: Final = 5
OTP_PHONE_LIMIT_1H: Final = 5
OTP_IP_LIMIT_1H: Final = 20
OTP_ATTEMPT_LIMIT: Final = 5
PAYMENT_ACCOUNT_REVIEW_USERS_30D: Final = 2
PAYMENT_ACCOUNT_FREEZE_USERS_30D: Final = 4
RELATED_ACCOUNT_FREEZE_COUNT: Final = 3
REFUND_REVIEW_RATE_30D: Final = 0.20
REFUND_FREEZE_RATE_30D: Final = 0.35
REFUND_DENY_RATE_30D: Final = 0.60
REFUND_MIN_ORDERS_30D: Final = 3
DOWNLOAD_REVIEW_LIMIT_1H: Final = 20
DOWNLOAD_FREEZE_LIMIT_1H: Final = 40
DOWNLOAD_DENY_LIMIT_1H: Final = 80
DOWNLOAD_REVIEW_LIMIT_24H: Final = 100
DOWNLOAD_FREEZE_LIMIT_24H: Final = 200
DOWNLOAD_DENY_LIMIT_24H: Final = 500

SHORT_COOLDOWN_SECONDS: Final = 300
RATE_LIMIT_COOLDOWN_SECONDS: Final = 3600
FREEZE_COOLDOWN_SECONDS: Final = 24 * 60 * 60

_LEVEL_RANK: Final = {
    LEVEL_INFO: 0,
    LEVEL_LOW: 1,
    LEVEL_MEDIUM: 2,
    LEVEL_HIGH: 3,
    LEVEL_CRITICAL: 4,
}

_DECISION_RANK: Final = {
    DECISION_ALLOW: 0,
    DECISION_REVIEW: 1,
    DECISION_FREEZE: 2,
    DECISION_DENY: 3,
}

_RISK_ALIASES: Final = {
    RISK_REGISTRATION_REWARD: RISK_REGISTRATION_REWARD,
    "register_reward": RISK_REGISTRATION_REWARD,
    "registration_bonus": RISK_REGISTRATION_REWARD,
    "signup_reward": RISK_REGISTRATION_REWARD,
    RISK_SMS_OTP: RISK_SMS_OTP,
    "otp": RISK_SMS_OTP,
    "otp_request": RISK_SMS_OTP,
    "sms_verification": RISK_SMS_OTP,
    RISK_INVITE_REWARD: RISK_INVITE_REWARD,
    "invite_reward_claim": RISK_INVITE_REWARD,
    "invitation_reward": RISK_INVITE_REWARD,
    "registration_invite_reward": RISK_INVITE_REWARD,
    RISK_REFERRAL_REBATE: RISK_REFERRAL_REBATE,
    "invite_rebate": RISK_REFERRAL_REBATE,
    "invitation_rebate": RISK_REFERRAL_REBATE,
    "consumer_referral": RISK_REFERRAL_REBATE,
    RISK_AGENT_COMMISSION: RISK_AGENT_COMMISSION,
    "commission": RISK_AGENT_COMMISSION,
    "agent_rebate": RISK_AGENT_COMMISSION,
    RISK_DOWNLOAD: RISK_DOWNLOAD,
    "download_frequency": RISK_DOWNLOAD,
    "asset_download": RISK_DOWNLOAD,
}


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    level: str
    decision: str
    reasons: tuple[str, ...]
    cooldown_seconds: int = 0


@dataclass(frozen=True)
class RiskStats:
    phone_number_valid: bool = True
    phone_verified: bool = True
    human_verified: bool = True
    same_phone_registered: bool = False
    risk_blocked: bool = False
    registration_reward_frozen: bool = False
    reward_already_claimed: bool = False
    same_phone_registrations_24h: int = 0
    same_device_registrations_24h: int = 0
    same_ip_registrations_24h: int = 0
    phone_otp_requests_1h: int = 0
    ip_otp_requests_1h: int = 0
    otp_attempts: int = 0
    payment_account_users_30d: int = 0
    refund_rate_30d: float = 0.0
    refund_orders_30d: int = 0
    self_invite: bool = False
    related_account_count: int = 0
    downloads_1h: int = 0
    downloads_24h: int = 0

    def __post_init__(self) -> None:
        for name in (
            "phone_number_valid",
            "phone_verified",
            "human_verified",
            "same_phone_registered",
            "risk_blocked",
            "registration_reward_frozen",
            "reward_already_claimed",
            "self_invite",
        ):
            _require_bool(getattr(self, name), name)

        for name in (
            "same_phone_registrations_24h",
            "same_device_registrations_24h",
            "same_ip_registrations_24h",
            "phone_otp_requests_1h",
            "ip_otp_requests_1h",
            "otp_attempts",
            "payment_account_users_30d",
            "refund_orders_30d",
            "related_account_count",
            "downloads_1h",
            "downloads_24h",
        ):
            _non_negative_int(getattr(self, name), name)

        _rate(self.refund_rate_30d, "refund_rate_30d")


@dataclass(frozen=True)
class _RiskHit:
    level: str
    decision: str
    reason: str
    cooldown_seconds: int = 0


def evaluate_risk(
    risk_type: str,
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    risk_name = _normalize_risk_type(risk_type)
    risk_stats = _coerce_stats(stats, **overrides)

    if risk_name == RISK_REGISTRATION_REWARD:
        return _decision(_registration_reward_hits(risk_stats))
    if risk_name == RISK_SMS_OTP:
        return _decision(_sms_otp_hits(risk_stats))
    if risk_name == RISK_INVITE_REWARD:
        return _decision(_invite_reward_hits(risk_stats))
    if risk_name == RISK_REFERRAL_REBATE:
        return _decision(_referral_rebate_hits(risk_stats))
    if risk_name == RISK_AGENT_COMMISSION:
        return _decision(_agent_commission_hits(risk_stats))
    if risk_name == RISK_DOWNLOAD:
        return _decision(_download_hits(risk_stats))

    raise ValueError(f"unsupported risk type: {risk_type!r}")


def evaluate_all_risks(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> dict[str, RiskDecision]:
    risk_stats = _coerce_stats(stats, **overrides)
    return {
        RISK_REGISTRATION_REWARD: evaluate_registration_reward_risk(risk_stats),
        RISK_SMS_OTP: evaluate_sms_otp_risk(risk_stats),
        RISK_INVITE_REWARD: evaluate_invite_reward_risk(risk_stats),
        RISK_REFERRAL_REBATE: evaluate_referral_rebate_risk(risk_stats),
        RISK_AGENT_COMMISSION: evaluate_agent_commission_risk(risk_stats),
        RISK_DOWNLOAD: evaluate_download_risk(risk_stats),
    }


def evaluate_registration_reward_risk(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    return _decision(_registration_reward_hits(_coerce_stats(stats, **overrides)))


def evaluate_sms_otp_risk(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    return _decision(_sms_otp_hits(_coerce_stats(stats, **overrides)))


def evaluate_invite_reward_risk(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    return _decision(_invite_reward_hits(_coerce_stats(stats, **overrides)))


def evaluate_referral_rebate_risk(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    return _decision(_referral_rebate_hits(_coerce_stats(stats, **overrides)))


def evaluate_agent_commission_risk(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    return _decision(_agent_commission_hits(_coerce_stats(stats, **overrides)))


def evaluate_download_risk(
    stats: RiskStats | Mapping[str, object] | None = None,
    **overrides: object,
) -> RiskDecision:
    return _decision(_download_hits(_coerce_stats(stats, **overrides)))


def _registration_reward_hits(stats: RiskStats) -> list[_RiskHit]:
    hits = _phone_identity_hits(stats)

    if stats.same_phone_registered:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "phone_already_registered"))
    if stats.risk_blocked:
        hits.append(_hit(LEVEL_HIGH, DECISION_FREEZE, "risk_blocked", FREEZE_COOLDOWN_SECONDS))
    if stats.registration_reward_frozen:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "registration_reward_frozen",
                FREEZE_COOLDOWN_SECONDS,
            )
        )

    hits.extend(_registration_cluster_hits(stats))
    hits.extend(_relationship_hits(stats))
    return hits


def _sms_otp_hits(stats: RiskStats) -> list[_RiskHit]:
    hits: list[_RiskHit] = []

    if not stats.phone_number_valid:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "invalid_phone_number"))
    if not stats.human_verified:
        hits.append(
            _hit(
                LEVEL_MEDIUM,
                DECISION_REVIEW,
                "human_verification_required",
                SHORT_COOLDOWN_SECONDS,
            )
        )
    if stats.phone_otp_requests_1h >= OTP_PHONE_LIMIT_1H:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_DENY,
                "phone_otp_rate_limited",
                RATE_LIMIT_COOLDOWN_SECONDS,
            )
        )
    if stats.ip_otp_requests_1h >= OTP_IP_LIMIT_1H:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_DENY,
                "ip_otp_rate_limited",
                RATE_LIMIT_COOLDOWN_SECONDS,
            )
        )
    if stats.otp_attempts >= OTP_ATTEMPT_LIMIT:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_DENY,
                "otp_attempt_limit_reached",
                SHORT_COOLDOWN_SECONDS,
            )
        )

    return hits


def _invite_reward_hits(stats: RiskStats) -> list[_RiskHit]:
    hits: list[_RiskHit] = []

    if not stats.phone_number_valid:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "invalid_phone_number"))
    if not stats.phone_verified:
        hits.append(_hit(LEVEL_HIGH, DECISION_DENY, "phone_not_verified"))
    if not stats.human_verified:
        hits.append(
            _hit(
                LEVEL_MEDIUM,
                DECISION_REVIEW,
                "human_not_verified",
                SHORT_COOLDOWN_SECONDS,
            )
        )
    if stats.same_phone_registered:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "phone_already_registered"))
    if stats.self_invite:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "self_invite"))
    if stats.reward_already_claimed:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "reward_already_claimed"))
    if stats.risk_blocked:
        hits.append(_hit(LEVEL_HIGH, DECISION_FREEZE, "risk_blocked", FREEZE_COOLDOWN_SECONDS))
    if stats.registration_reward_frozen:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "registration_reward_frozen",
                FREEZE_COOLDOWN_SECONDS,
            )
        )

    hits.extend(_registration_cluster_hits(stats))
    hits.extend(_related_account_hits(stats))
    return hits


def _referral_rebate_hits(stats: RiskStats) -> list[_RiskHit]:
    hits = _phone_identity_hits(stats)

    if stats.risk_blocked:
        hits.append(_hit(LEVEL_HIGH, DECISION_FREEZE, "risk_blocked", FREEZE_COOLDOWN_SECONDS))
    if stats.reward_already_claimed:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "reward_already_claimed"))

    hits.extend(_registration_cluster_hits(stats))
    hits.extend(_relationship_hits(stats))
    hits.extend(_payment_account_hits(stats))
    hits.extend(_refund_hits(stats))
    return hits


def _agent_commission_hits(stats: RiskStats) -> list[_RiskHit]:
    hits = _phone_identity_hits(stats)

    if stats.risk_blocked:
        hits.append(_hit(LEVEL_HIGH, DECISION_FREEZE, "risk_blocked", FREEZE_COOLDOWN_SECONDS))
    if stats.self_invite:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "self_commission_attempt"))

    hits.extend(_related_account_hits(stats))
    hits.extend(_payment_account_hits(stats))
    hits.extend(_refund_hits(stats))
    return hits


def _download_hits(stats: RiskStats) -> list[_RiskHit]:
    hits: list[_RiskHit] = []

    if stats.risk_blocked:
        hits.append(_hit(LEVEL_HIGH, DECISION_FREEZE, "risk_blocked", FREEZE_COOLDOWN_SECONDS))

    if stats.downloads_1h >= DOWNLOAD_DENY_LIMIT_1H or stats.downloads_24h >= DOWNLOAD_DENY_LIMIT_24H:
        hits.append(
            _hit(
                LEVEL_CRITICAL,
                DECISION_DENY,
                "download_rate_critical",
                RATE_LIMIT_COOLDOWN_SECONDS,
            )
        )
    elif stats.downloads_1h >= DOWNLOAD_FREEZE_LIMIT_1H or stats.downloads_24h >= DOWNLOAD_FREEZE_LIMIT_24H:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "download_rate_high",
                RATE_LIMIT_COOLDOWN_SECONDS,
            )
        )
    elif stats.downloads_1h >= DOWNLOAD_REVIEW_LIMIT_1H or stats.downloads_24h >= DOWNLOAD_REVIEW_LIMIT_24H:
        hits.append(
            _hit(
                LEVEL_MEDIUM,
                DECISION_REVIEW,
                "download_rate_elevated",
                SHORT_COOLDOWN_SECONDS,
            )
        )

    return hits


def _phone_identity_hits(stats: RiskStats) -> list[_RiskHit]:
    hits: list[_RiskHit] = []

    if not stats.phone_number_valid:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "invalid_phone_number"))
    if not stats.phone_verified:
        hits.append(_hit(LEVEL_MEDIUM, DECISION_REVIEW, "phone_not_verified"))
    if not stats.human_verified:
        hits.append(_hit(LEVEL_MEDIUM, DECISION_REVIEW, "human_not_verified"))

    return hits


def _registration_cluster_hits(stats: RiskStats) -> list[_RiskHit]:
    hits: list[_RiskHit] = []

    if stats.same_phone_registrations_24h >= REGISTRATION_PHONE_LIMIT_24H:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "phone_registration_limit_reached"))
    if stats.same_device_registrations_24h >= REGISTRATION_DEVICE_LIMIT_24H:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "device_registration_limit_reached",
                FREEZE_COOLDOWN_SECONDS,
            )
        )
    if stats.same_ip_registrations_24h >= REGISTRATION_IP_LIMIT_24H:
        hits.append(
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "ip_registration_limit_reached",
                FREEZE_COOLDOWN_SECONDS,
            )
        )

    return hits


def _relationship_hits(stats: RiskStats) -> list[_RiskHit]:
    hits: list[_RiskHit] = []

    if stats.self_invite:
        hits.append(_hit(LEVEL_CRITICAL, DECISION_DENY, "self_invite"))
    hits.extend(_related_account_hits(stats))
    return hits


def _related_account_hits(stats: RiskStats) -> list[_RiskHit]:
    if stats.related_account_count >= RELATED_ACCOUNT_FREEZE_COUNT:
        return [
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "related_account_cluster",
                FREEZE_COOLDOWN_SECONDS,
            )
        ]
    if stats.related_account_count > 0:
        return [_hit(LEVEL_MEDIUM, DECISION_REVIEW, "related_account")]
    return []


def _payment_account_hits(stats: RiskStats) -> list[_RiskHit]:
    if stats.payment_account_users_30d >= PAYMENT_ACCOUNT_FREEZE_USERS_30D:
        return [
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "shared_payment_account_cluster",
                FREEZE_COOLDOWN_SECONDS,
            )
        ]
    if stats.payment_account_users_30d >= PAYMENT_ACCOUNT_REVIEW_USERS_30D:
        return [_hit(LEVEL_MEDIUM, DECISION_REVIEW, "shared_payment_account")]
    return []


def _refund_hits(stats: RiskStats) -> list[_RiskHit]:
    if stats.refund_orders_30d < REFUND_MIN_ORDERS_30D:
        return []
    if stats.refund_rate_30d >= REFUND_DENY_RATE_30D:
        return [_hit(LEVEL_CRITICAL, DECISION_DENY, "refund_rate_critical")]
    if stats.refund_rate_30d >= REFUND_FREEZE_RATE_30D:
        return [
            _hit(
                LEVEL_HIGH,
                DECISION_FREEZE,
                "refund_rate_high",
                FREEZE_COOLDOWN_SECONDS,
            )
        ]
    if stats.refund_rate_30d >= REFUND_REVIEW_RATE_30D:
        return [_hit(LEVEL_MEDIUM, DECISION_REVIEW, "refund_rate_elevated")]
    return []


def _decision(hits: list[_RiskHit]) -> RiskDecision:
    if not hits:
        return RiskDecision(
            allowed=True,
            level=LEVEL_INFO,
            decision=DECISION_ALLOW,
            reasons=(),
            cooldown_seconds=0,
        )

    reasons: list[str] = []
    for hit in hits:
        if hit.reason not in reasons:
            reasons.append(hit.reason)

    level = max((hit.level for hit in hits), key=_LEVEL_RANK.__getitem__)
    decision = max((hit.decision for hit in hits), key=_DECISION_RANK.__getitem__)
    cooldown_seconds = max(hit.cooldown_seconds for hit in hits)

    return RiskDecision(
        allowed=decision == DECISION_ALLOW,
        level=level,
        decision=decision,
        reasons=tuple(reasons),
        cooldown_seconds=cooldown_seconds,
    )


def _hit(level: str, decision: str, reason: str, cooldown_seconds: int = 0) -> _RiskHit:
    return _RiskHit(
        level=level,
        decision=decision,
        reason=reason,
        cooldown_seconds=_non_negative_int(cooldown_seconds, "cooldown_seconds"),
    )


def _coerce_stats(
    stats: RiskStats | Mapping[str, object] | None,
    **overrides: object,
) -> RiskStats:
    if stats is None:
        data: dict[str, object] = {}
    elif isinstance(stats, RiskStats):
        data = {field.name: getattr(stats, field.name) for field in fields(RiskStats)}
    elif isinstance(stats, Mapping):
        data = dict(stats)
    else:
        raise TypeError("stats must be RiskStats, mapping, or None")

    data.update(overrides)
    try:
        return RiskStats(**data)
    except TypeError as exc:
        raise TypeError(f"invalid risk stats: {exc}") from exc


def _normalize_risk_type(risk_type: str) -> str:
    if not isinstance(risk_type, str):
        raise TypeError("risk_type must be a string")
    normalized = risk_type.strip().lower()
    try:
        return _RISK_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported risk type: {risk_type!r}") from exc


def _require_bool(value: bool, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _rate(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if value < 0 or value > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)
