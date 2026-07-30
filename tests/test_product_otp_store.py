from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from shared.product_otp_store import (
    ERR_CHALLENGE_NOT_FOUND,
    ERR_INVALID_IP,
    ERR_INVALID_PHONE,
    ERR_OTP_ATTEMPT_LIMITED,
    ERR_OTP_EXPIRED,
    ERR_OTP_MISMATCH,
    ERR_OTP_RATE_LIMITED,
    ERR_OTP_USED,
    ProductOtpConfig,
    ProductOtpConfigurationError,
    ProductOtpError,
    ProductOtpStore,
    ProductOtpUnavailable,
    product_otp_store_from_env,
)


SECRET = "otp-test-secret-" + ("o" * 32)


class OtpRedisDouble:
    def __init__(self, *, now_ms: int = 1_785_427_200_000) -> None:
        self.now_ms = now_ms
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, int]] = {}
        self.expirations: dict[str, int] = {}
        self.calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        self.lock = threading.RLock()

    def advance(self, milliseconds: int) -> None:
        with self.lock:
            self.now_ms += milliseconds
            self._purge_expired()

    def eval(self, script: str, key_count: int, *values: str) -> list[Any]:
        keys = tuple(values[:key_count])
        args = tuple(values[key_count:])
        with self.lock:
            self._purge_expired()
            if "WAIMAI_AUTH_OTP_REQUEST_V1" in script:
                self.calls.append(("request", keys, args))
                return self._request(keys, args)
            if "WAIMAI_AUTH_OTP_VERIFY_V1" in script:
                self.calls.append(("verify", keys, args))
                return self._verify(keys, args)
        raise AssertionError("unexpected Lua script")

    def _request(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> list[Any]:
        challenge_key, phone_rate_key, ip_rate_key = keys
        (
            window_ms_raw,
            cooldown_ms_raw,
            phone_limit_raw,
            ip_limit_raw,
            challenge_id,
            phone,
            code_digest,
            binding_digest,
            ip_digest,
            ttl_ms_raw,
            retention_ms_raw,
        ) = args
        window_ms = int(window_ms_raw)
        cooldown_ms = int(cooldown_ms_raw)
        phone_limit = int(phone_limit_raw)
        ip_limit = int(ip_limit_raw)
        ttl_ms = int(ttl_ms_raw)
        retention_ms = int(retention_ms_raw)

        existing = self.hashes.get(challenge_key)
        if existing is not None:
            if existing["binding_digest"] != binding_digest:
                return [-4, 0]
            if existing["used"] == "1":
                return [-5, 0]
            if self.now_ms >= int(existing["expires_at_ms"]):
                return [-6, 0]
            self._trim(phone_rate_key, window_ms)
            self._trim(ip_rate_key, window_ms)
            return [
                2,
                int(existing["created_at_ms"]),
                int(existing["expires_at_ms"]),
                len(self.sorted_sets.get(phone_rate_key, {})),
                len(self.sorted_sets.get(ip_rate_key, {})),
                0,
            ]

        self._trim(phone_rate_key, window_ms)
        self._trim(ip_rate_key, window_ms)
        phone_rates = self.sorted_sets.setdefault(phone_rate_key, {})
        ip_rates = self.sorted_sets.setdefault(ip_rate_key, {})
        if cooldown_ms > 0 and phone_rates:
            latest = max(phone_rates.values())
            if self.now_ms - latest < cooldown_ms:
                return [-1, cooldown_ms - (self.now_ms - latest)]
        if len(phone_rates) >= phone_limit:
            oldest = min(phone_rates.values())
            return [-2, max(1, window_ms - (self.now_ms - oldest))]
        if len(ip_rates) >= ip_limit:
            oldest = min(ip_rates.values())
            return [-3, max(1, window_ms - (self.now_ms - oldest))]

        expires_at_ms = self.now_ms + ttl_ms
        self.hashes[challenge_key] = {
            "challenge_id": challenge_id,
            "phone": phone,
            "code_digest": code_digest,
            "binding_digest": binding_digest,
            "ip_digest": ip_digest,
            "attempts": "0",
            "used": "0",
            "created_at_ms": str(self.now_ms),
            "expires_at_ms": str(expires_at_ms),
            "used_at_ms": "",
        }
        self.expirations[challenge_key] = self.now_ms + ttl_ms + retention_ms
        phone_rates[challenge_id] = self.now_ms
        ip_rates[challenge_id] = self.now_ms
        self.expirations[phone_rate_key] = (
            self.now_ms + window_ms + ttl_ms + retention_ms
        )
        self.expirations[ip_rate_key] = (
            self.now_ms + window_ms + ttl_ms + retention_ms
        )
        return [
            1,
            self.now_ms,
            expires_at_ms,
            len(phone_rates),
            len(ip_rates),
            0,
        ]

    def _verify(
        self,
        keys: tuple[str, ...],
        args: tuple[str, ...],
    ) -> list[Any]:
        challenge = self.hashes.get(keys[0])
        if challenge is None:
            return [-1, 0]
        if challenge["used"] == "1":
            return [-2, 0]
        if self.now_ms >= int(challenge["expires_at_ms"]):
            return [-3, 0]
        attempts = int(challenge["attempts"])
        attempt_limit = int(args[1])
        if attempts >= attempt_limit:
            return [-4, attempts]
        if challenge.get("code_digest") != args[0]:
            attempts += 1
            challenge["attempts"] = str(attempts)
            return [0, attempts]
        challenge["used"] = "1"
        challenge["used_at_ms"] = str(self.now_ms)
        challenge.pop("code_digest", None)
        return [1, challenge["phone"].encode("utf-8"), attempts, self.now_ms]

    def _trim(self, key: str, window_ms: int) -> None:
        values = self.sorted_sets.setdefault(key, {})
        threshold = self.now_ms - window_ms
        for member in [
            member for member, score in values.items() if score <= threshold
        ]:
            values.pop(member, None)

    def _purge_expired(self) -> None:
        for key in [
            key
            for key, expires_at in self.expirations.items()
            if expires_at <= self.now_ms
        ]:
            self.expirations.pop(key, None)
            self.hashes.pop(key, None)
            self.sorted_sets.pop(key, None)


class FailingRedis:
    def eval(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("redis unavailable")


def otp_store(
    redis_client: Any,
    *,
    secret: str = SECRET,
    phone_limit: int = 5,
    ip_limit: int = 20,
    cooldown: int = 0,
    ttl: int = 300,
    attempt_limit: int = 5,
) -> ProductOtpStore:
    return ProductOtpStore(
        redis_client,
        hash_secret=secret,
        config=ProductOtpConfig(
            namespace="waimai:test-auth",
            otp_ttl_seconds=ttl,
            rate_window_seconds=3600,
            expired_retention_seconds=3600,
            phone_request_limit=phone_limit,
            ip_request_limit=ip_limit,
            send_cooldown_seconds=cooldown,
            attempt_limit=attempt_limit,
        ),
    )


def test_request_stores_only_digest_and_obfuscated_rate_keys() -> None:
    redis_client = OtpRedisDouble()
    instance = otp_store(redis_client)

    result = instance.request_otp(
        phone="138 0013 8000",
        ip="203.0.113.10",
        idempotency_key="request-1",
    )

    assert result.phone == "+8613800138000"
    assert len(result.code) == 6
    assert result.idempotent is False
    challenge = next(iter(redis_client.hashes.values()))
    assert challenge["code_digest"] != result.code
    assert len(challenge["code_digest"]) == 64
    assert "203.0.113.10" not in repr(redis_client.hashes)
    assert "203.0.113.10" not in repr(redis_client.sorted_sets)
    request_call = redis_client.calls[0]
    assert result.code not in request_call[1]
    assert result.code not in request_call[2]


def test_idempotent_request_reuses_challenge_code_and_rate_slot() -> None:
    redis_client = OtpRedisDouble()
    instance_a = otp_store(redis_client)
    instance_b = otp_store(redis_client)

    first = instance_a.request_otp(
        phone="13800138000",
        ip="203.0.113.10",
        idempotency_key="same-browser-request",
    )
    replay = instance_b.request_otp(
        phone="+8613800138000",
        ip="203.0.113.10",
        idempotency_key="same-browser-request",
    )

    assert replay.challenge_id == first.challenge_id
    assert replay.code == first.code
    assert replay.idempotent is True
    assert replay.phone_requests_in_window == 1
    assert replay.ip_requests_in_window == 1


def test_verify_works_across_instances_and_is_single_use() -> None:
    redis_client = OtpRedisDouble()
    request_store = otp_store(redis_client)
    verify_store = otp_store(redis_client)
    request = request_store.request_otp(
        phone="13800138000",
        ip="203.0.113.10",
    )

    verified = verify_store.verify_otp(
        challenge_id=request.challenge_id,
        code=request.code,
    )

    assert verified.phone == "+8613800138000"
    assert verified.attempts == 0
    challenge = next(iter(redis_client.hashes.values()))
    assert "code_digest" not in challenge
    with pytest.raises(ProductOtpError) as replay:
        request_store.verify_otp(
            challenge_id=request.challenge_id,
            code=request.code,
        )
    assert replay.value.code == ERR_OTP_USED


def test_concurrent_correct_verification_has_exactly_one_winner() -> None:
    redis_client = OtpRedisDouble()
    instance = otp_store(redis_client)
    request = instance.request_otp(
        phone="13800138000",
        ip="203.0.113.10",
    )

    def verify() -> str:
        try:
            instance.verify_otp(
                challenge_id=request.challenge_id,
                code=request.code,
            )
            return "success"
        except ProductOtpError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(lambda _index: verify(), range(10)))

    assert outcomes.count("success") == 1
    assert outcomes.count(ERR_OTP_USED) == 9


def test_attempt_limit_is_atomic_and_fail_closed() -> None:
    redis_client = OtpRedisDouble()
    instance = otp_store(redis_client, attempt_limit=3)
    request = instance.request_otp(
        phone="13800138000",
        ip="203.0.113.10",
    )

    for expected_attempts in (1, 2, 3):
        with pytest.raises(ProductOtpError) as mismatch:
            instance.verify_otp(
                challenge_id=request.challenge_id,
                code="000000" if request.code != "000000" else "999999",
            )
        assert mismatch.value.code == ERR_OTP_MISMATCH
        assert mismatch.value.attempts == expected_attempts

    with pytest.raises(ProductOtpError) as limited:
        instance.verify_otp(
            challenge_id=request.challenge_id,
            code=request.code,
        )
    assert limited.value.code == ERR_OTP_ATTEMPT_LIMITED
    assert limited.value.attempts == 3


def test_phone_rate_limit_is_atomic_under_concurrency() -> None:
    redis_client = OtpRedisDouble()
    instance = otp_store(
        redis_client,
        phone_limit=5,
        ip_limit=100,
        cooldown=0,
    )

    def request(index: int) -> str:
        try:
            instance.request_otp(
                phone="13800138000",
                ip=f"203.0.113.{index + 1}",
                idempotency_key=f"request-{index}",
            )
            return "success"
        except ProductOtpError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=20) as executor:
        outcomes = list(executor.map(request, range(20)))

    assert outcomes.count("success") == 5
    assert outcomes.count(ERR_OTP_RATE_LIMITED) == 15


def test_ip_rate_limit_applies_across_different_phones() -> None:
    redis_client = OtpRedisDouble()
    instance = otp_store(
        redis_client,
        phone_limit=10,
        ip_limit=2,
        cooldown=0,
    )

    for phone in ("13800138000", "13900139000"):
        instance.request_otp(
            phone=phone,
            ip="203.0.113.10",
        )
    with pytest.raises(ProductOtpError) as limited:
        instance.request_otp(
            phone="13700137000",
            ip="203.0.113.10",
        )
    assert limited.value.code == ERR_OTP_RATE_LIMITED
    assert limited.value.retry_after_seconds > 0


def test_expired_challenge_is_retained_as_expired_not_recreated() -> None:
    redis_client = OtpRedisDouble()
    instance = otp_store(redis_client, ttl=30)
    request = instance.request_otp(
        phone="13800138000",
        ip="203.0.113.10",
        idempotency_key="expiring-request",
    )
    redis_client.advance(30_000)

    with pytest.raises(ProductOtpError) as verification:
        instance.verify_otp(
            challenge_id=request.challenge_id,
            code=request.code,
        )
    assert verification.value.code == ERR_OTP_EXPIRED

    with pytest.raises(ProductOtpError) as replay:
        instance.request_otp(
            phone="13800138000",
            ip="203.0.113.10",
            idempotency_key="expiring-request",
        )
    assert replay.value.code == ERR_OTP_EXPIRED


def test_wrong_secret_cannot_verify_existing_challenge() -> None:
    redis_client = OtpRedisDouble()
    request_store = otp_store(redis_client)
    request = request_store.request_otp(
        phone="13800138000",
        ip="203.0.113.10",
    )
    other_store = otp_store(
        redis_client,
        secret="different-otp-secret-" + ("x" * 32),
    )

    with pytest.raises(ProductOtpError) as mismatch:
        other_store.verify_otp(
            challenge_id=request.challenge_id,
            code=request.code,
        )
    assert mismatch.value.code == ERR_OTP_MISMATCH


@pytest.mark.parametrize(
    ("phone", "ip", "error_code"),
    [
        ("123", "203.0.113.10", ERR_INVALID_PHONE),
        ("13800138000", "", ERR_INVALID_IP),
        ("13800138000", "not-an-ip", ERR_INVALID_IP),
    ],
)
def test_invalid_identity_input_fails_before_redis(
    phone: str,
    ip: str,
    error_code: str,
) -> None:
    redis_client = OtpRedisDouble()
    with pytest.raises(ProductOtpError) as error:
        otp_store(redis_client).request_otp(phone=phone, ip=ip)
    assert error.value.code == error_code
    assert redis_client.calls == []


def test_redis_failure_never_falls_back_to_process_memory() -> None:
    instance = otp_store(FailingRedis())

    with pytest.raises(ProductOtpUnavailable) as error:
        instance.request_otp(
            phone="13800138000",
            ip="203.0.113.10",
        )

    assert error.value.code == "otp_redis_unavailable"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"REDIS_URL": "redis://localhost:6379/0"},
        {"AUTH_OTP_HASH_SECRET": SECRET},
        {"REDIS_URL": "http://localhost", "AUTH_OTP_HASH_SECRET": SECRET},
        {
            "REDIS_URL": "redis://localhost:6379/0",
            "AUTH_OTP_HASH_SECRET": "short",
        },
    ],
)
def test_production_factory_fails_closed_on_missing_configuration(
    env: dict[str, str],
) -> None:
    with pytest.raises(ProductOtpConfigurationError):
        product_otp_store_from_env(env, redis_client=OtpRedisDouble())


def test_production_factory_accepts_explicit_redis_and_secret() -> None:
    instance = product_otp_store_from_env(
        {
            "REDIS_URL": "redis://localhost:6379/0",
            "AUTH_OTP_HASH_SECRET": SECRET,
            "AUTH_OTP_REDIS_NAMESPACE": "waimai:test-auth",
            "AUTH_OTP_SEND_COOLDOWN_SECONDS": "0",
        },
        redis_client=OtpRedisDouble(),
    )

    assert isinstance(instance, ProductOtpStore)
    assert instance.config.send_cooldown_seconds == 0
    assert SECRET not in repr(instance)


def test_missing_challenge_has_stable_error_code() -> None:
    with pytest.raises(ProductOtpError) as error:
        otp_store(OtpRedisDouble()).verify_otp(
            challenge_id="otp_" + ("a" * 32),
            code="123456",
        )
    assert error.value.code == ERR_CHALLENGE_NOT_FOUND


def test_real_redis_lua_cross_instance_and_atomic_single_use() -> None:
    redis_url = str(os.environ.get("TEST_REDIS_URL") or "").strip()
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is not configured")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(redis_url, decode_responses=False)
    namespace = f"waimai:test-auth:{uuid4().hex}"
    config = ProductOtpConfig(
        namespace=namespace,
        otp_ttl_seconds=60,
        rate_window_seconds=60,
        expired_retention_seconds=60,
        phone_request_limit=2,
        ip_request_limit=20,
        send_cooldown_seconds=0,
        attempt_limit=5,
    )
    first = ProductOtpStore(client, hash_secret=SECRET, config=config)
    second = ProductOtpStore(client, hash_secret=SECRET, config=config)
    try:
        request = first.request_otp(
            phone="13800138000",
            ip="203.0.113.10",
            idempotency_key="real-redis-request",
        )
        replay = second.request_otp(
            phone="+8613800138000",
            ip="203.0.113.10",
            idempotency_key="real-redis-request",
        )
        assert replay.challenge_id == request.challenge_id
        assert replay.code == request.code
        assert replay.idempotent is True

        second.request_otp(
            phone="13800138000",
            ip="203.0.113.11",
            idempotency_key="real-redis-request-2",
        )
        with pytest.raises(ProductOtpError) as limited:
            first.request_otp(
                phone="13800138000",
                ip="203.0.113.12",
                idempotency_key="real-redis-request-3",
            )
        assert limited.value.code == ERR_OTP_RATE_LIMITED

        outcomes: list[str] = []
        lock = threading.Lock()

        def verify() -> None:
            try:
                second.verify_otp(
                    challenge_id=request.challenge_id,
                    code=request.code,
                )
                outcome = "success"
            except ProductOtpError as exc:
                outcome = exc.code
            with lock:
                outcomes.append(outcome)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _index: verify(), range(8)))
        assert outcomes.count("success") == 1
        assert outcomes.count(ERR_OTP_USED) == 7
    finally:
        keys = list(client.scan_iter(match=f"{namespace}:*"))
        if keys:
            client.delete(*keys)
