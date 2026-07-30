from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import time
from typing import Any

import asset_security
import download_guard


SECRET = "download-secret"
NOW = 1_800_000_000


def _claims(
    *,
    nonce: str = "shared-nonce",
    purpose: str = asset_security.EXPORT,
    variant: str = asset_security.EXPORT,
    user_id: str = "user-1",
    asset_id: str = "asset-1",
    expires_at: int = NOW + 60,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "user_id": user_id,
        "order_id": "order-1",
        "job_id": "job-1",
        "variant": variant,
        "purpose": purpose,
        "expires_at": expires_at,
        "nonce": nonce,
    }


def _authorize(
    claims: dict[str, Any],
    consumer: download_guard.NonceConsumer | None,
) -> dict[str, Any]:
    token = asset_security.sign_asset_url(claims, SECRET, now=NOW)
    return download_guard.authorize_download(
        asset_record={
            "asset_id": claims["asset_id"],
            "user_id": claims["user_id"],
            "order_id": claims["order_id"],
            "job_id": claims["job_id"],
            "allowed_purposes": asset_security.ASSET_PURPOSES,
            "available_variants": asset_security.ASSET_VARIANTS,
        },
        user_context={"user_id": claims["user_id"]},
        order_context={"order_id": claims["order_id"]},
        job_context={"job_id": claims["job_id"]},
        purpose=claims["purpose"],
        variant=claims["variant"],
        token=token,
        secret=SECRET,
        now=NOW,
        nonce_consumer=consumer,
    )


def test_one_time_token_fails_closed_without_consumer() -> None:
    decision = _authorize(_claims(), None)

    assert decision["allowed"] is False
    assert decision["reason"] == download_guard.REASON_NONCE_CONSUMER_REQUIRED
    assert decision["audit"]["token_consumption_status"] == "consumer_required"


def test_consumer_error_fails_closed() -> None:
    class BrokenConsumer:
        def consume_once(
            self,
            request: download_guard.NonceConsumptionRequest,
            *,
            now: int | float | None = None,
        ) -> download_guard.NonceConsumptionResult:
            del request, now
            raise ConnectionError("nonce backend unavailable")

    decision = _authorize(_claims(), BrokenConsumer())

    assert decision["allowed"] is False
    assert decision["reason"] == download_guard.REASON_NONCE_CONSUMER_ERROR
    assert decision["audit"]["token_consumption_status"] == "consumer_error"


def test_invalid_consumer_result_fails_closed() -> None:
    class InvalidConsumer:
        def consume_once(
            self,
            request: download_guard.NonceConsumptionRequest,
            *,
            now: int | float | None = None,
        ) -> download_guard.NonceConsumptionResult:
            del request, now
            return download_guard.NonceConsumptionResult("unexpected")  # type: ignore[arg-type]

    decision = _authorize(_claims(), InvalidConsumer())

    assert decision["allowed"] is False
    assert decision["reason"] == download_guard.REASON_NONCE_CONSUMER_ERROR
    assert decision["audit"]["token_consumption_status"] == "consumer_error"


def test_same_token_can_be_consumed_only_once() -> None:
    consumer = download_guard.InMemoryNonceConsumer()
    claims = _claims()

    first = _authorize(claims, consumer)
    replay = _authorize(claims, consumer)

    assert first["allowed"] is True
    assert first["audit"]["token_consumption_status"] == "consumed"
    assert replay["allowed"] is False
    assert replay["reason"] == download_guard.REASON_TOKEN_REPLAYED
    assert replay["audit"]["token_consumption_status"] == "replayed"


def test_expired_nonce_is_rejected_without_consuming_it() -> None:
    consumer = download_guard.InMemoryNonceConsumer()
    expired = download_guard.nonce_consumption_request_from_claims(
        _claims(expires_at=NOW - 1)
    )

    result = consumer.consume_once(expired, now=NOW)

    assert result.status is download_guard.NonceConsumptionStatus.EXPIRED
    assert result.consumed is False


def test_same_nonce_cannot_cross_purpose_user_or_asset_scope() -> None:
    consumer = download_guard.InMemoryNonceConsumer()
    first = _authorize(_claims(), consumer)
    assert first["allowed"] is True

    changed_scopes = [
        _claims(
            purpose=asset_security.ORIGINAL,
            variant=asset_security.ORIGINAL,
        ),
        _claims(user_id="user-2"),
        _claims(asset_id="asset-2"),
    ]

    for claims in changed_scopes:
        decision = _authorize(claims, consumer)
        assert decision["allowed"] is False
        assert decision["reason"] == download_guard.REASON_TOKEN_REPLAYED
        assert decision["audit"]["token_consumption_status"] == "scope_mismatch"


def test_concurrent_consumers_allow_at_most_one_success() -> None:
    attempts = 32
    barrier = Barrier(attempts)
    consumer = download_guard.InMemoryNonceConsumer()
    claims = _claims()

    def authorize_at_once(_: int) -> dict[str, Any]:
        barrier.wait()
        return _authorize(claims, consumer)

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        decisions = list(pool.map(authorize_at_once, range(attempts)))

    allowed = [decision for decision in decisions if decision["allowed"]]
    denied = [decision for decision in decisions if not decision["allowed"]]
    assert len(allowed) == 1
    assert len(denied) == attempts - 1
    assert {decision["reason"] for decision in denied} == {
        download_guard.REASON_TOKEN_REPLAYED
    }


def test_redis_consumer_uses_one_atomic_server_time_operation() -> None:
    class AtomicRedis:
        def __init__(self) -> None:
            self.lock = Lock()
            self.values: dict[str, str] = {}
            self.scripts: list[str] = []

        def eval(
            self,
            script: str,
            key_count: int,
            key: str,
            scope: str,
            expires_at: str,
        ) -> int:
            assert key_count == 1
            self.scripts.append(script)
            with self.lock:
                existing = self.values.get(key)
                if existing is not None:
                    return 0 if existing == scope else -1
                if float(expires_at) <= time.time():
                    return -2
                self.values[key] = scope
                return 1

    redis = AtomicRedis()
    consumer = download_guard.RedisNonceConsumer(redis)
    claims = _claims(
        nonce="redis-shared-nonce",
        expires_at=int(time.time()) + 60,
    )
    request = download_guard.nonce_consumption_request_from_claims(claims)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(lambda _index: consumer.consume_once(request), range(16))
        )

    assert sum(result.consumed for result in results) == 1
    assert {
        result.status
        for result in results
        if not result.consumed
    } == {download_guard.NonceConsumptionStatus.REPLAYED}
    assert "-- WAIMAI_ASSET_NONCE_CONSUME" in redis.scripts[0]
    assert "redis.call('TIME')" in redis.scripts[0]
    assert "'NX'" in redis.scripts[0]
