from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import auth_rules


DEFAULT_OTP_TTL_SECONDS = 5 * 60
DEFAULT_RATE_WINDOW_SECONDS = 60 * 60
DEFAULT_EXPIRED_RETENTION_SECONDS = 60 * 60
DEFAULT_PHONE_REQUEST_LIMIT = auth_rules.DEFAULT_PHONE_OTP_REQUEST_LIMIT_1H
DEFAULT_IP_REQUEST_LIMIT = auth_rules.DEFAULT_IP_OTP_REQUEST_LIMIT_1H
DEFAULT_SEND_COOLDOWN_SECONDS = auth_rules.DEFAULT_OTP_SEND_COOLDOWN_SECONDS
DEFAULT_ATTEMPT_LIMIT = auth_rules.DEFAULT_OTP_ATTEMPT_LIMIT

CHALLENGE_ID_RE = re.compile(r"^otp_[0-9a-f]{32}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
OTP_CODE_RE = re.compile(r"^[0-9]{6}$")

ERR_INVALID_PHONE = "invalid_phone"
ERR_INVALID_IP = "invalid_ip"
ERR_OTP_RATE_LIMITED = "otp_rate_limited"
ERR_OTP_SEND_COOLDOWN = "otp_send_cooldown"
ERR_CHALLENGE_NOT_FOUND = "challenge_not_found"
ERR_OTP_EXPIRED = "otp_expired"
ERR_OTP_USED = "otp_used"
ERR_OTP_ATTEMPT_LIMITED = "otp_attempt_limited"
ERR_OTP_MISMATCH = "otp_mismatch"


class ProductOtpStoreError(RuntimeError):
    pass


class InvalidProductOtpInput(ProductOtpStoreError):
    pass


class ProductOtpConfigurationError(ProductOtpStoreError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProductOtpUnavailable(ProductOtpStoreError):
    def __init__(self, code: str = "otp_redis_unavailable") -> None:
        super().__init__(code)
        self.code = code


class ProductOtpError(ProductOtpStoreError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int = 0,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts


@dataclass(frozen=True)
class ProductOtpConfig:
    namespace: str = "waimai:auth"
    otp_ttl_seconds: int = DEFAULT_OTP_TTL_SECONDS
    rate_window_seconds: int = DEFAULT_RATE_WINDOW_SECONDS
    expired_retention_seconds: int = DEFAULT_EXPIRED_RETENTION_SECONDS
    phone_request_limit: int = DEFAULT_PHONE_REQUEST_LIMIT
    ip_request_limit: int = DEFAULT_IP_REQUEST_LIMIT
    send_cooldown_seconds: int = DEFAULT_SEND_COOLDOWN_SECONDS
    attempt_limit: int = DEFAULT_ATTEMPT_LIMIT

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not NAMESPACE_RE.fullmatch(
            self.namespace
        ):
            raise InvalidProductOtpInput("namespace has an invalid format")
        _bounded_int(
            self.otp_ttl_seconds,
            "otp_ttl_seconds",
            minimum=30,
            maximum=30 * 60,
        )
        _bounded_int(
            self.rate_window_seconds,
            "rate_window_seconds",
            minimum=60,
            maximum=24 * 60 * 60,
        )
        _bounded_int(
            self.expired_retention_seconds,
            "expired_retention_seconds",
            minimum=60,
            maximum=24 * 60 * 60,
        )
        _bounded_int(
            self.phone_request_limit,
            "phone_request_limit",
            minimum=1,
            maximum=1000,
        )
        _bounded_int(
            self.ip_request_limit,
            "ip_request_limit",
            minimum=1,
            maximum=10000,
        )
        _bounded_int(
            self.send_cooldown_seconds,
            "send_cooldown_seconds",
            minimum=0,
            maximum=60 * 60,
        )
        _bounded_int(
            self.attempt_limit,
            "attempt_limit",
            minimum=1,
            maximum=20,
        )

    @property
    def slot_tag(self) -> str:
        return hashlib.sha256(self.namespace.encode("utf-8")).hexdigest()[:16]

    @property
    def key_prefix(self) -> str:
        return f"{self.namespace}:{{{self.slot_tag}}}"

    def challenge_key(self, challenge_id: str) -> str:
        return f"{self.key_prefix}:otp:challenge:{challenge_id}"

    def phone_rate_key(self, phone_digest: str) -> str:
        return f"{self.key_prefix}:otp:phone-rate:{phone_digest}"

    def ip_rate_key(self, ip_digest: str) -> str:
        return f"{self.key_prefix}:otp:ip-rate:{ip_digest}"


@dataclass(frozen=True)
class OtpRequestResult:
    challenge_id: str
    code: str
    phone: str
    created_at: str
    expires_at: str
    phone_requests_in_window: int
    ip_requests_in_window: int
    idempotent: bool


@dataclass(frozen=True)
class OtpVerificationResult:
    challenge_id: str
    phone: str
    used_at: str
    attempts: int


class ProductOtpStore:
    """Redis OTP state with atomic challenge, attempt, and rate-limit scripts."""

    def __init__(
        self,
        redis_client: Any,
        *,
        hash_secret: str | bytes,
        config: ProductOtpConfig | None = None,
        challenge_id_factory: Callable[[], str] | None = None,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        if redis_client is None:
            raise InvalidProductOtpInput("redis_client is required")
        self.redis = redis_client
        self._hash_secret = _secret_bytes(hash_secret, "hash_secret")
        self.config = config or ProductOtpConfig()
        self._challenge_id_factory = challenge_id_factory or (
            lambda: f"otp_{secrets.token_hex(16)}"
        )
        self._code_factory = code_factory or (
            lambda: f"{secrets.randbelow(1_000_000):06d}"
        )

    def request_otp(
        self,
        *,
        phone: str,
        ip: str,
        idempotency_key: str | None = None,
    ) -> OtpRequestResult:
        normalized_phone = _normalize_phone(phone)
        normalized_ip = _normalize_ip(ip)
        clean_idempotency_key = _optional_idempotency_key(idempotency_key)
        if clean_idempotency_key:
            challenge_id = (
                "otp_"
                + self._digest(
                    "challenge-id",
                    normalized_phone,
                    clean_idempotency_key,
                )[:32]
            )
            code_number = int(
                self._digest(
                    "challenge-code",
                    normalized_phone,
                    clean_idempotency_key,
                )[:16],
                16,
            ) % 1_000_000
            code = f"{code_number:06d}"
        else:
            challenge_id = _challenge_id(self._challenge_id_factory())
            code = _otp_code(self._code_factory())

        code_digest = self._code_digest(challenge_id, code)
        phone_digest = self._digest("phone-rate", normalized_phone)
        ip_digest = self._digest("ip-rate", normalized_ip)
        binding_digest = self._digest(
            "challenge-binding",
            challenge_id,
            normalized_phone,
            ip_digest,
            code_digest,
        )
        retention_seconds = max(
            self.config.expired_retention_seconds,
            self.config.rate_window_seconds,
        )
        try:
            result = self.redis.eval(
                _REQUEST_OTP_LUA,
                3,
                self.config.challenge_key(challenge_id),
                self.config.phone_rate_key(phone_digest),
                self.config.ip_rate_key(ip_digest),
                str(self.config.rate_window_seconds * 1000),
                str(self.config.send_cooldown_seconds * 1000),
                str(self.config.phone_request_limit),
                str(self.config.ip_request_limit),
                challenge_id,
                normalized_phone,
                code_digest,
                binding_digest,
                ip_digest,
                str(self.config.otp_ttl_seconds * 1000),
                str(retention_seconds * 1000),
            )
        except Exception as exc:
            raise ProductOtpUnavailable() from exc

        values = _redis_list(result, "OTP request")
        outcome = _redis_int(values[0], "OTP request outcome")
        if outcome in {1, 2}:
            if len(values) < 6:
                raise ProductOtpUnavailable("otp_redis_response_invalid")
            created_at_ms = _redis_int(values[1], "created_at_ms")
            expires_at_ms = _redis_int(values[2], "expires_at_ms")
            return OtpRequestResult(
                challenge_id=challenge_id,
                code=code,
                phone=normalized_phone,
                created_at=_millisecond_timestamp(created_at_ms),
                expires_at=_millisecond_timestamp(expires_at_ms),
                phone_requests_in_window=_redis_int(
                    values[3],
                    "phone_requests_in_window",
                ),
                ip_requests_in_window=_redis_int(
                    values[4],
                    "ip_requests_in_window",
                ),
                idempotent=outcome == 2,
            )
        retry_after_seconds = (
            max(1, (_redis_int(values[1], "retry_after_ms") + 999) // 1000)
            if len(values) > 1
            else 0
        )
        if outcome == -1:
            raise ProductOtpError(
                ERR_OTP_SEND_COOLDOWN,
                "OTP send cooldown is active",
                retry_after_seconds=retry_after_seconds,
            )
        if outcome in {-2, -3}:
            raise ProductOtpError(
                ERR_OTP_RATE_LIMITED,
                "too many OTP requests",
                retry_after_seconds=retry_after_seconds,
            )
        if outcome == -5:
            raise ProductOtpError(
                ERR_OTP_USED,
                "OTP challenge has already been used",
            )
        if outcome == -6:
            raise ProductOtpError(
                ERR_OTP_EXPIRED,
                "OTP challenge has expired",
            )
        if outcome == -4:
            raise ProductOtpUnavailable("otp_challenge_binding_conflict")
        raise ProductOtpUnavailable("otp_redis_response_invalid")

    def verify_otp(
        self,
        *,
        challenge_id: str,
        code: str,
    ) -> OtpVerificationResult:
        clean_challenge_id = _challenge_id(challenge_id)
        clean_code = _otp_code(code)
        provided_digest = self._code_digest(clean_challenge_id, clean_code)
        try:
            result = self.redis.eval(
                _VERIFY_OTP_LUA,
                1,
                self.config.challenge_key(clean_challenge_id),
                provided_digest,
                str(self.config.attempt_limit),
            )
        except Exception as exc:
            raise ProductOtpUnavailable() from exc

        values = _redis_list(result, "OTP verification")
        outcome = _redis_int(values[0], "OTP verification outcome")
        if outcome == 1:
            if len(values) < 4:
                raise ProductOtpUnavailable("otp_redis_response_invalid")
            return OtpVerificationResult(
                challenge_id=clean_challenge_id,
                phone=_decode(values[1]),
                attempts=_redis_int(values[2], "attempts"),
                used_at=_millisecond_timestamp(
                    _redis_int(values[3], "used_at_ms")
                ),
            )
        if outcome == 0:
            attempts = (
                _redis_int(values[1], "attempts") if len(values) > 1 else 0
            )
            raise ProductOtpError(
                ERR_OTP_MISMATCH,
                "OTP code does not match",
                attempts=attempts,
            )
        if outcome == -1:
            raise ProductOtpError(
                ERR_CHALLENGE_NOT_FOUND,
                "OTP challenge not found",
            )
        if outcome == -2:
            raise ProductOtpError(
                ERR_OTP_USED,
                "OTP challenge has already been used",
            )
        if outcome == -3:
            raise ProductOtpError(
                ERR_OTP_EXPIRED,
                "OTP challenge has expired",
            )
        if outcome == -4:
            attempts = (
                _redis_int(values[1], "attempts") if len(values) > 1 else 0
            )
            raise ProductOtpError(
                ERR_OTP_ATTEMPT_LIMITED,
                "too many OTP verification attempts",
                attempts=attempts,
            )
        raise ProductOtpUnavailable("otp_redis_response_invalid")

    def _code_digest(self, challenge_id: str, code: str) -> str:
        return self._digest("otp-code", challenge_id, code)

    def _digest(self, domain: str, *values: str) -> str:
        message = "\x00".join((domain, *values)).encode("utf-8")
        return hmac.new(self._hash_secret, message, hashlib.sha256).hexdigest()


def product_otp_store_from_env(
    env: Mapping[str, str] | None = None,
    *,
    redis_client: Any | None = None,
) -> ProductOtpStore:
    """Build the production Redis store and reject incomplete configuration."""

    values = os.environ if env is None else env
    redis_url = str(values.get("REDIS_URL") or "").strip()
    if urlsplit(redis_url).scheme.lower() not in {"redis", "rediss", "unix"}:
        raise ProductOtpConfigurationError("auth_otp_redis_url_required")
    try:
        hash_secret = _secret_bytes(
            values.get("AUTH_OTP_HASH_SECRET"),
            "AUTH_OTP_HASH_SECRET",
        )
    except InvalidProductOtpInput as exc:
        raise ProductOtpConfigurationError(
            "auth_otp_hash_secret_required"
        ) from exc
    try:
        config = ProductOtpConfig(
            namespace=str(
                values.get("AUTH_OTP_REDIS_NAMESPACE") or "waimai:auth"
            ).strip(),
            otp_ttl_seconds=_env_int(
                values,
                "AUTH_OTP_TTL_SECONDS",
                DEFAULT_OTP_TTL_SECONDS,
            ),
            rate_window_seconds=_env_int(
                values,
                "AUTH_OTP_RATE_WINDOW_SECONDS",
                DEFAULT_RATE_WINDOW_SECONDS,
            ),
            expired_retention_seconds=_env_int(
                values,
                "AUTH_OTP_EXPIRED_RETENTION_SECONDS",
                DEFAULT_EXPIRED_RETENTION_SECONDS,
            ),
            phone_request_limit=_env_int(
                values,
                "AUTH_OTP_PHONE_REQUEST_LIMIT",
                DEFAULT_PHONE_REQUEST_LIMIT,
            ),
            ip_request_limit=_env_int(
                values,
                "AUTH_OTP_IP_REQUEST_LIMIT",
                DEFAULT_IP_REQUEST_LIMIT,
            ),
            send_cooldown_seconds=_env_int(
                values,
                "AUTH_OTP_SEND_COOLDOWN_SECONDS",
                DEFAULT_SEND_COOLDOWN_SECONDS,
            ),
            attempt_limit=_env_int(
                values,
                "AUTH_OTP_ATTEMPT_LIMIT",
                DEFAULT_ATTEMPT_LIMIT,
            ),
        )
    except (InvalidProductOtpInput, ValueError, TypeError) as exc:
        raise ProductOtpConfigurationError(
            "auth_otp_configuration_invalid"
        ) from exc

    client = redis_client
    if client is None:
        try:
            import redis

            client = redis.Redis.from_url(redis_url, decode_responses=False)
        except Exception as exc:
            raise ProductOtpConfigurationError(
                "auth_otp_redis_client_unavailable"
            ) from exc
    return ProductOtpStore(
        client,
        hash_secret=hash_secret,
        config=config,
    )


def _normalize_phone(value: Any) -> str:
    try:
        return auth_rules.normalize_phone(value)
    except (TypeError, ValueError) as exc:
        raise ProductOtpError(ERR_INVALID_PHONE, "phone is invalid") from exc


def _normalize_ip(value: Any) -> str:
    if not isinstance(value, str):
        raise ProductOtpError(ERR_INVALID_IP, "IP address is required")
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError as exc:
        raise ProductOtpError(ERR_INVALID_IP, "IP address is invalid") from exc


def _optional_idempotency_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not IDEMPOTENCY_KEY_RE.fullmatch(value)
    ):
        raise InvalidProductOtpInput(
            "idempotency_key has an invalid format"
        )
    return value


def _challenge_id(value: Any) -> str:
    if not isinstance(value, str) or not CHALLENGE_ID_RE.fullmatch(value):
        raise InvalidProductOtpInput("challenge_id has an invalid format")
    return value


def _otp_code(value: Any) -> str:
    if not isinstance(value, str) or not OTP_CODE_RE.fullmatch(value):
        raise InvalidProductOtpInput("OTP code must contain six digits")
    return value


def _secret_bytes(value: Any, field: str) -> bytes:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise InvalidProductOtpInput(f"{field} must be configured")
    if len(encoded) < 32 or len(encoded) > 4096:
        raise InvalidProductOtpInput(
            f"{field} must contain between 32 and 4096 bytes"
        )
    return bytes(encoded)


def _bounded_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise InvalidProductOtpInput(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductOtpInput(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise InvalidProductOtpInput(f"{field} is out of range")
    return number


def _env_int(values: Mapping[str, str], name: str, default: int) -> int:
    return _bounded_int(
        values.get(name, default),
        name,
        minimum=0,
        maximum=24 * 60 * 60,
    )


def _redis_list(value: Any, operation: str) -> list[Any]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ProductOtpUnavailable(
            f"{operation.lower().replace(' ', '_')}_response_invalid"
        )
    return list(value)


def _redis_int(value: Any, field: str) -> int:
    try:
        return int(_decode(value))
    except (TypeError, ValueError) as exc:
        raise ProductOtpUnavailable("otp_redis_response_invalid") from exc


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _millisecond_timestamp(value: int) -> str:
    return datetime.fromtimestamp(
        value / 1000,
        tz=timezone.utc,
    ).isoformat()


_REQUEST_OTP_LUA = r"""
-- WAIMAI_AUTH_OTP_REQUEST_V1
local challenge_key = KEYS[1]
local phone_rate_key = KEYS[2]
local ip_rate_key = KEYS[3]

local window_ms = tonumber(ARGV[1])
local cooldown_ms = tonumber(ARGV[2])
local phone_limit = tonumber(ARGV[3])
local ip_limit = tonumber(ARGV[4])
local challenge_id = ARGV[5]
local phone = ARGV[6]
local code_digest = ARGV[7]
local binding_digest = ARGV[8]
local ip_digest = ARGV[9]
local ttl_ms = tonumber(ARGV[10])
local retention_ms = tonumber(ARGV[11])

local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000)
    + math.floor(tonumber(now_parts[2]) / 1000)

if redis.call('EXISTS', challenge_key) == 1 then
    local existing_binding = redis.call('HGET', challenge_key, 'binding_digest')
    if existing_binding ~= binding_digest then
        return {-4, 0}
    end
    if redis.call('HGET', challenge_key, 'used') == '1' then
        return {-5, 0}
    end
    local existing_expires = tonumber(
        redis.call('HGET', challenge_key, 'expires_at_ms') or '0'
    )
    if now_ms >= existing_expires then
        return {-6, 0}
    end
    local existing_created = tonumber(
        redis.call('HGET', challenge_key, 'created_at_ms') or '0'
    )
    redis.call('ZREMRANGEBYSCORE', phone_rate_key, '-inf', now_ms - window_ms)
    redis.call('ZREMRANGEBYSCORE', ip_rate_key, '-inf', now_ms - window_ms)
    return {
        2,
        existing_created,
        existing_expires,
        redis.call('ZCARD', phone_rate_key),
        redis.call('ZCARD', ip_rate_key),
        0
    }
end

redis.call('ZREMRANGEBYSCORE', phone_rate_key, '-inf', now_ms - window_ms)
redis.call('ZREMRANGEBYSCORE', ip_rate_key, '-inf', now_ms - window_ms)

local phone_count = redis.call('ZCARD', phone_rate_key)
local ip_count = redis.call('ZCARD', ip_rate_key)
local latest = redis.call(
    'ZREVRANGE',
    phone_rate_key,
    0,
    0,
    'WITHSCORES'
)
if cooldown_ms > 0 and #latest >= 2 then
    local elapsed_ms = now_ms - tonumber(latest[2])
    if elapsed_ms < cooldown_ms then
        return {-1, cooldown_ms - elapsed_ms}
    end
end
if phone_count >= phone_limit then
    local oldest = redis.call('ZRANGE', phone_rate_key, 0, 0, 'WITHSCORES')
    local retry_ms = window_ms
    if #oldest >= 2 then
        retry_ms = math.max(1, window_ms - (now_ms - tonumber(oldest[2])))
    end
    return {-2, retry_ms}
end
if ip_count >= ip_limit then
    local oldest = redis.call('ZRANGE', ip_rate_key, 0, 0, 'WITHSCORES')
    local retry_ms = window_ms
    if #oldest >= 2 then
        retry_ms = math.max(1, window_ms - (now_ms - tonumber(oldest[2])))
    end
    return {-3, retry_ms}
end

local expires_at_ms = now_ms + ttl_ms
redis.call(
    'HSET',
    challenge_key,
    'challenge_id', challenge_id,
    'phone', phone,
    'code_digest', code_digest,
    'binding_digest', binding_digest,
    'ip_digest', ip_digest,
    'attempts', '0',
    'used', '0',
    'created_at_ms', tostring(now_ms),
    'expires_at_ms', tostring(expires_at_ms),
    'used_at_ms', ''
)
redis.call('PEXPIRE', challenge_key, ttl_ms + retention_ms)
redis.call('ZADD', phone_rate_key, now_ms, challenge_id)
redis.call('ZADD', ip_rate_key, now_ms, challenge_id)
redis.call('PEXPIRE', phone_rate_key, window_ms + ttl_ms + retention_ms)
redis.call('PEXPIRE', ip_rate_key, window_ms + ttl_ms + retention_ms)

return {1, now_ms, expires_at_ms, phone_count + 1, ip_count + 1, 0}
"""


_VERIFY_OTP_LUA = r"""
-- WAIMAI_AUTH_OTP_VERIFY_V1
local challenge_key = KEYS[1]
local provided_digest = ARGV[1]
local attempt_limit = tonumber(ARGV[2])

if redis.call('EXISTS', challenge_key) == 0 then
    return {-1, 0}
end

local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000)
    + math.floor(tonumber(now_parts[2]) / 1000)

if redis.call('HGET', challenge_key, 'used') == '1' then
    return {-2, 0}
end
local expires_at_ms = tonumber(
    redis.call('HGET', challenge_key, 'expires_at_ms') or '0'
)
if now_ms >= expires_at_ms then
    return {-3, 0}
end
local attempts = tonumber(
    redis.call('HGET', challenge_key, 'attempts') or '0'
)
if attempts >= attempt_limit then
    return {-4, attempts}
end
local expected_digest = redis.call('HGET', challenge_key, 'code_digest')
if not expected_digest or expected_digest ~= provided_digest then
    attempts = redis.call('HINCRBY', challenge_key, 'attempts', 1)
    return {0, attempts}
end

redis.call(
    'HSET',
    challenge_key,
    'used', '1',
    'used_at_ms', tostring(now_ms)
)
redis.call('HDEL', challenge_key, 'code_digest')
return {
    1,
    redis.call('HGET', challenge_key, 'phone'),
    attempts,
    now_ms
}
"""
