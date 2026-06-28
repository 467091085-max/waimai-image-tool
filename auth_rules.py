from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final


DEFAULT_DEVICE_REGISTRATION_REWARD_LIMIT_24H: Final = 2
DEFAULT_IP_REGISTRATION_REWARD_LIMIT_24H: Final = 5
DEFAULT_PHONE_OTP_REQUEST_LIMIT_1H: Final = 5
DEFAULT_IP_OTP_REQUEST_LIMIT_1H: Final = 20
DEFAULT_SAME_PHONE_REGISTRATION_LIMIT_24H: Final = 1
DEFAULT_OTP_SEND_COOLDOWN_SECONDS: Final = 60
DEFAULT_OTP_ATTEMPT_LIMIT: Final = 5
DEFAULT_OTP_ATTEMPT_COOLDOWN_SECONDS: Final = 5 * 60
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS: Final = 60 * 60
DEFAULT_REGISTRATION_CLUSTER_COOLDOWN_SECONDS: Final = 24 * 60 * 60

_MAINLAND_MOBILE_RE: Final = re.compile(r"^1\d{10}$")
_PHONE_SEPARATOR_RE: Final = re.compile(r"[\s-]+")


@dataclass(frozen=True)
class AuthRuleResult:
    allowed: bool
    code: str
    reasons: tuple[str, ...] = ()
    cooldown_seconds: int = 0
    normalized_phone: str = ""


def normalize_phone(phone: str) -> str:
    """Normalize a mainland China mobile number to +86xxxxxxxxxxx."""
    if not isinstance(phone, str):
        raise TypeError("phone must be a string")

    compact = _PHONE_SEPARATOR_RE.sub("", phone.strip())
    if compact.startswith("+86"):
        compact = compact[3:]

    if not _MAINLAND_MOBILE_RE.fullmatch(compact):
        raise ValueError("phone must be a mainland China mobile number")

    return f"+86{compact}"


def evaluate_phone_registration_otp(
    phone: str,
    *,
    phone_requests_1h: int = 0,
    ip_requests_1h: int = 0,
    same_phone_registrations_24h: int = 0,
    device_registrations_24h: int = 0,
    ip_registrations_24h: int = 0,
    otp_attempts: int = 0,
    seconds_since_last_otp: int | None = None,
    phone_limit_1h: int = DEFAULT_PHONE_OTP_REQUEST_LIMIT_1H,
    ip_limit_1h: int = DEFAULT_IP_OTP_REQUEST_LIMIT_1H,
    same_phone_limit_24h: int = DEFAULT_SAME_PHONE_REGISTRATION_LIMIT_24H,
    device_limit_24h: int = DEFAULT_DEVICE_REGISTRATION_REWARD_LIMIT_24H,
    ip_limit_24h: int = DEFAULT_IP_REGISTRATION_REWARD_LIMIT_24H,
    otp_attempt_limit: int = DEFAULT_OTP_ATTEMPT_LIMIT,
    send_cooldown_seconds: int = DEFAULT_OTP_SEND_COOLDOWN_SECONDS,
    attempt_cooldown_seconds: int = DEFAULT_OTP_ATTEMPT_COOLDOWN_SECONDS,
    rate_limit_cooldown_seconds: int = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    registration_cluster_cooldown_seconds: int = DEFAULT_REGISTRATION_CLUSTER_COOLDOWN_SECONDS,
) -> AuthRuleResult:
    """Evaluate local registration and SMS OTP risk rules.

    Counts are observations before the current request. When a count reaches
    its configured limit, the next request is blocked deterministically.
    """
    try:
        normalized_phone = normalize_phone(phone)
    except (TypeError, ValueError):
        return AuthRuleResult(
            allowed=False,
            code="invalid_phone",
            reasons=("invalid_phone",),
            normalized_phone="",
        )

    phone_count = _non_negative_int(phone_requests_1h, "phone_requests_1h")
    ip_count = _non_negative_int(ip_requests_1h, "ip_requests_1h")
    same_phone_count = _non_negative_int(
        same_phone_registrations_24h,
        "same_phone_registrations_24h",
    )
    device_count = _non_negative_int(device_registrations_24h, "device_registrations_24h")
    registration_ip_count = _non_negative_int(ip_registrations_24h, "ip_registrations_24h")
    attempts = _non_negative_int(otp_attempts, "otp_attempts")

    phone_limit = _non_negative_int(phone_limit_1h, "phone_limit_1h")
    ip_limit = _non_negative_int(ip_limit_1h, "ip_limit_1h")
    same_phone_limit = _non_negative_int(same_phone_limit_24h, "same_phone_limit_24h")
    device_limit = _non_negative_int(device_limit_24h, "device_limit_24h")
    registration_ip_limit = _non_negative_int(ip_limit_24h, "ip_limit_24h")
    attempt_limit = _non_negative_int(otp_attempt_limit, "otp_attempt_limit")
    send_cooldown = _non_negative_int(send_cooldown_seconds, "send_cooldown_seconds")
    attempt_cooldown = _non_negative_int(
        attempt_cooldown_seconds,
        "attempt_cooldown_seconds",
    )
    rate_cooldown = _non_negative_int(
        rate_limit_cooldown_seconds,
        "rate_limit_cooldown_seconds",
    )
    cluster_cooldown = _non_negative_int(
        registration_cluster_cooldown_seconds,
        "registration_cluster_cooldown_seconds",
    )

    reasons: list[str] = []
    cooldowns: list[int] = []
    elapsed = _optional_non_negative_int(seconds_since_last_otp, "seconds_since_last_otp")
    if elapsed is not None and elapsed < send_cooldown:
        reasons.append("otp_send_cooldown")
        cooldowns.append(send_cooldown - elapsed)
    if phone_count >= phone_limit:
        reasons.append("phone_otp_rate_limited")
        cooldowns.append(rate_cooldown)
    if ip_count >= ip_limit:
        reasons.append("ip_otp_rate_limited")
        cooldowns.append(rate_cooldown)
    if attempts >= attempt_limit:
        reasons.append("otp_attempt_limit_reached")
        cooldowns.append(attempt_cooldown)
    if same_phone_count >= same_phone_limit:
        reasons.append("phone_registration_limit_reached")
        cooldowns.append(cluster_cooldown)
    if device_count >= device_limit:
        reasons.append("device_registration_limit_reached")
        cooldowns.append(cluster_cooldown)
    if registration_ip_count >= registration_ip_limit:
        reasons.append("ip_registration_limit_reached")
        cooldowns.append(cluster_cooldown)

    return _auth_result(reasons, cooldowns, normalized_phone)


def otp_verification_allowed(
    attempts: int,
    *,
    attempt_limit: int = DEFAULT_OTP_ATTEMPT_LIMIT,
) -> bool:
    """Return whether another OTP verification attempt can be accepted."""
    return _non_negative_int(attempts, "attempts") < _non_negative_int(
        attempt_limit,
        "attempt_limit",
    )


def can_issue_registration_reward(
    phone_verified: bool,
    human_verified: bool,
    same_phone_registered: bool,
    device_registrations_24h: int,
    ip_registrations_24h: int,
    risk_blocked: bool,
    *,
    device_limit_24h: int = DEFAULT_DEVICE_REGISTRATION_REWARD_LIMIT_24H,
    ip_limit_24h: int = DEFAULT_IP_REGISTRATION_REWARD_LIMIT_24H,
) -> bool:
    """Return whether a registration reward can be issued.

    The registration counts are rewards already triggered in the rolling
    24-hour window before this registration. Once a count reaches its limit,
    the next reward is denied.
    """
    _require_bool(phone_verified, "phone_verified")
    _require_bool(human_verified, "human_verified")
    _require_bool(same_phone_registered, "same_phone_registered")
    _require_bool(risk_blocked, "risk_blocked")

    device_count = _non_negative_int(device_registrations_24h, "device_registrations_24h")
    ip_count = _non_negative_int(ip_registrations_24h, "ip_registrations_24h")
    device_limit = _non_negative_int(device_limit_24h, "device_limit_24h")
    ip_limit = _non_negative_int(ip_limit_24h, "ip_limit_24h")

    return bool(
        phone_verified
        and human_verified
        and not same_phone_registered
        and not risk_blocked
        and device_count < device_limit
        and ip_count < ip_limit
    )


def otp_request_allowed(
    phone_requests_1h: int,
    ip_requests_1h: int,
    *,
    phone_limit_1h: int = DEFAULT_PHONE_OTP_REQUEST_LIMIT_1H,
    ip_limit_1h: int = DEFAULT_IP_OTP_REQUEST_LIMIT_1H,
) -> bool:
    """Return whether another OTP request can be sent in the current hour."""
    phone_count = _non_negative_int(phone_requests_1h, "phone_requests_1h")
    ip_count = _non_negative_int(ip_requests_1h, "ip_requests_1h")
    phone_limit = _non_negative_int(phone_limit_1h, "phone_limit_1h")
    ip_limit = _non_negative_int(ip_limit_1h, "ip_limit_1h")

    return phone_count < phone_limit and ip_count < ip_limit


def _auth_result(
    reasons: list[str],
    cooldowns: list[int],
    normalized_phone: str,
) -> AuthRuleResult:
    if not reasons:
        return AuthRuleResult(
            allowed=True,
            code="allow",
            reasons=(),
            cooldown_seconds=0,
            normalized_phone=normalized_phone,
        )

    return AuthRuleResult(
        allowed=False,
        code=_auth_code(reasons),
        reasons=tuple(reasons),
        cooldown_seconds=max(cooldowns) if cooldowns else 0,
        normalized_phone=normalized_phone,
    )


def _auth_code(reasons: list[str]) -> str:
    if "invalid_phone" in reasons:
        return "invalid_phone"
    if "otp_attempt_limit_reached" in reasons:
        return "otp_attempt_limited"
    if "phone_otp_rate_limited" in reasons or "ip_otp_rate_limited" in reasons:
        return "otp_rate_limited"
    if any(reason.endswith("_registration_limit_reached") for reason in reasons):
        return "registration_rate_limited"
    if "otp_send_cooldown" in reasons:
        return "otp_send_cooldown"
    return "blocked"


def _require_bool(value: bool, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")


def _optional_non_negative_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, name)


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value
