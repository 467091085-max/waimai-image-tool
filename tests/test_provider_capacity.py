from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
from unittest import mock

import pytest

from provider_capacity import (
    GenerationCapacity,
    ProviderConcurrencyGate,
    RedisProviderConcurrencyGate,
    build_mask_concurrency_gate,
    observed_batch_performance,
    provider_lease_floor_seconds,
)


class CapacityRedisDouble:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = threading.Lock()

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        px: int | None = None,
    ) -> bool:
        del px
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def eval(self, script: str, key_count: int, *values: str) -> int:
        assert key_count == 1
        key = values[0]
        token = values[1]
        with self.lock:
            if self.values.get(key) != token:
                return 0
            if "pexpire" in script:
                return 1
            if "del" in script:
                del self.values[key]
                return 1
        raise AssertionError("unexpected provider-capacity Lua script")


class ReleaseMismatchRedisDouble(CapacityRedisDouble):
    def eval(self, script: str, key_count: int, *values: str) -> int:
        if "del" in script:
            with self.lock:
                self.values[values[0]] = "foreign-owner"
            return 0
        return super().eval(script, key_count, *values)


def real_batch_evidence_env(tmp_path) -> dict[str, str]:
    payload = {
        "schemaVersion": 1,
        "mode": "real",
        "runId": "paid-hunyuan-100-20260811",
        "paidProviderVerified": True,
        "provider": "tencent-hunyuan",
        "model": "hy-image-v3",
        "requestedImages": 100,
        "succeededImages": 100,
        "elapsedSeconds": 1800,
        "peakProviderConcurrency": 10,
        "outputManifestSha256": "a" * 64,
    }
    raw = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path = tmp_path / "real-100-image-evidence.json"
    path.write_bytes(raw)
    return {
        "GENERATION_TARGET_BATCH_EVIDENCE_FILE": str(path),
        "GENERATION_TARGET_BATCH_EVIDENCE_SHA256": hashlib.sha256(
            raw
        ).hexdigest(),
    }


def test_default_capacity_requests_ten_but_activates_one_until_verified() -> None:
    capacity = GenerationCapacity.from_env({})
    contract = capacity.public_contract()

    assert capacity.batch_workers == 10
    assert capacity.provider_concurrency == 10
    assert capacity.active_provider_concurrency == 1
    assert capacity.active_concurrency == 1
    assert capacity.batch_call_limit == 120
    assert contract["structureReady"] is True
    assert contract["productionTargetVerified"] is False
    assert contract["verificationIssues"] == [
        "provider_concurrency_not_verified_at_10",
        "provider_p95_not_measured",
        "target_batch_evidence_not_configured",
    ]


def test_verified_capacity_requires_sha_bound_real_batch_evidence(
    tmp_path,
) -> None:
    capacity = GenerationCapacity.from_env(
        {
            "TENCENT_TOKENHUB_VERIFIED_CONCURRENCY": "10",
            "TENCENT_TOKENHUB_MEASURED_P95_SECONDS": "30",
            **real_batch_evidence_env(tmp_path),
        }
    )

    assert capacity.active_concurrency == 10
    assert capacity.public_contract()["productionTargetVerified"] is True
    assert capacity.max_provider_p95_seconds == 360


def test_scalar_observed_seconds_never_replace_sha_bound_evidence() -> None:
    base = {
        "TENCENT_TOKENHUB_VERIFIED_CONCURRENCY": "10",
        "TENCENT_TOKENHUB_MEASURED_P95_SECONDS": "30",
    }

    missing = GenerationCapacity.from_env(base).public_contract()
    scalar_only = GenerationCapacity.from_env(
        {**base, "GENERATION_TARGET_BATCH_OBSERVED_SECONDS": "1800"}
    ).public_contract()

    assert missing["productionTargetVerified"] is False
    assert "target_batch_evidence_not_configured" in missing["verificationIssues"]
    assert scalar_only["productionTargetVerified"] is False
    assert scalar_only["observedTargetBatchSeconds"] == 1800


def test_target_contract_ignores_environment_override_attempts(tmp_path) -> None:
    capacity = GenerationCapacity.from_env(
        {
            "GENERATION_TARGET_BATCH_IMAGES": "10",
            "GENERATION_TARGET_SECONDS": "7200",
            "TENCENT_TOKENHUB_VERIFIED_CONCURRENCY": "10",
            "TENCENT_TOKENHUB_MEASURED_P95_SECONDS": "30",
            **real_batch_evidence_env(tmp_path),
        }
    )
    contract = capacity.public_contract()

    assert capacity.target_images == 100
    assert capacity.target_seconds == 3600
    assert contract["targetImages"] == 100
    assert contract["targetSeconds"] == 3600
    assert contract["productionTargetVerified"] is True
    assert contract["structuralIssues"] == []


def test_latency_budget_uses_verified_active_concurrency_not_requested_limit() -> None:
    capacity = GenerationCapacity.from_env(
        {
            "FINAL_GENERATION_WORKERS": "32",
            "TENCENT_TOKENHUB_MAX_CONCURRENCY": "32",
            "TENCENT_TOKENHUB_VERIFIED_CONCURRENCY": "10",
            "TENCENT_TOKENHUB_MEASURED_P95_SECONDS": "361",
        }
    )
    contract = capacity.public_contract()

    assert contract["configuredConcurrency"] == 32
    assert contract["activeConcurrency"] == 10
    assert contract["configuredWaves"] == 4
    assert contract["activeWaves"] == 10
    assert contract["maxProviderP95SecondsPerWave"] == 360
    assert contract["productionTargetVerified"] is False
    assert "provider_p95_exceeds_one_hour_budget" in contract["verificationIssues"]


def test_local_gate_reaches_ten_and_releases_slots_after_errors() -> None:
    gate = ProviderConcurrencyGate(10, acquire_timeout_seconds=2)
    state_lock = threading.Lock()
    ready = threading.Event()
    active = 0
    peak = 0

    def run(index: int) -> None:
        nonlocal active, peak
        with gate.slot():
            with state_lock:
                active += 1
                peak = max(peak, active)
                if active == 10:
                    ready.set()
            ready.wait(timeout=2)
            time.sleep(0.005)
            with state_lock:
                active -= 1
            if index == 3:
                raise RuntimeError("simulated paid call failure")

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(run, index) for index in range(20)]
        for index, future in enumerate(futures):
            if index == 3:
                with pytest.raises(RuntimeError):
                    future.result()
            else:
                future.result()

    assert peak == 10
    assert gate.snapshot() == {
        "limit": 10,
        "active": 0,
        "peak": 10,
        "completed": 20,
        "distributed": False,
    }


def test_two_process_gates_share_the_same_ten_redis_slots() -> None:
    redis = CapacityRedisDouble()
    gates = [
        RedisProviderConcurrencyGate(
            redis,
            key_prefix="test:provider",
            max_concurrency=10,
            acquire_timeout_seconds=2,
            lease_seconds=30,
            poll_interval_seconds=0.001,
        )
        for _ in range(2)
    ]
    state_lock = threading.Lock()
    ready = threading.Event()
    active = 0
    peak = 0

    def run(index: int) -> None:
        nonlocal active, peak
        with gates[index % 2].slot():
            with state_lock:
                active += 1
                peak = max(peak, active)
                if active == 10:
                    ready.set()
            ready.wait(timeout=2)
            time.sleep(0.005)
            with state_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(run, range(20)))

    assert peak == 10
    assert redis.values == {}
    assert sum(gate.snapshot()["completedInProcess"] for gate in gates) == 20


def test_distributed_gate_retains_slot_lease_after_ambiguous_failure() -> None:
    redis = CapacityRedisDouble()
    gate = RedisProviderConcurrencyGate(
        redis,
        key_prefix="test:provider",
        max_concurrency=1,
        acquire_timeout_seconds=1,
        lease_seconds=30,
        poll_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeError, match="ambiguous provider timeout"):
        with gate.slot():
            raise RuntimeError("ambiguous provider timeout")

    assert len(redis.values) == 1
    assert gate.snapshot()["retainedAmbiguousLeasesInProcess"] == 1


def test_distributed_gate_retains_ambiguous_release_mismatch() -> None:
    redis = ReleaseMismatchRedisDouble()
    gate = RedisProviderConcurrencyGate(
        redis,
        key_prefix="test:provider",
        max_concurrency=1,
        acquire_timeout_seconds=1,
        lease_seconds=30,
        poll_interval_seconds=0.001,
    )

    with gate.slot() as timing:
        pass

    assert timing["releaseFailed"] is True
    assert timing["leaseLost"] is True
    assert timing["leaseRetained"] is True
    assert gate.snapshot()["retainedAmbiguousLeasesInProcess"] == 1


def test_mask_gate_builder_uses_shared_redis_namespace() -> None:
    redis = CapacityRedisDouble()
    env = {
        "REDIS_URL": "redis://example.test/0",
        "REDIS_NAMESPACE": "test:waimai",
        "TENCENT_REQUEST_TIMEOUT": "90",
    }

    with mock.patch("redis.Redis.from_url", return_value=redis):
        first = build_mask_concurrency_gate(3, env)
        second = build_mask_concurrency_gate(3, env)

    assert isinstance(first, RedisProviderConcurrencyGate)
    assert isinstance(second, RedisProviderConcurrencyGate)
    assert first.key_prefix == second.key_prefix
    assert first.key_prefix == "test:waimai:provider-capacity:tencent-mask"
    assert first.snapshot()["distributed"] is True
    assert first.lease_seconds >= 150


def test_observed_performance_only_claims_target_for_full_hundred() -> None:
    capacity = GenerationCapacity.from_env({})

    partial = observed_batch_performance(
        requested=100,
        succeeded=99,
        elapsed_seconds=120,
        capacity=capacity,
    )
    complete = observed_batch_performance(
        requested=100,
        succeeded=100,
        elapsed_seconds=1800,
        capacity=capacity,
    )

    assert partial["oneHourTargetObserved"] is None
    assert complete["oneHourTargetObserved"] is True
    assert complete["projectedSecondsForTargetImages"] == 1800

    weakened = GenerationCapacity.from_env(
        {
            "GENERATION_TARGET_BATCH_IMAGES": "10",
            "GENERATION_TARGET_SECONDS": "7200",
        }
    )
    insufficient = observed_batch_performance(
        requested=10,
        succeeded=10,
        elapsed_seconds=7000,
        capacity=weakened,
    )
    assert insufficient["oneHourTargetObserved"] is None
    assert insufficient["projectedSecondsForTargetImages"] == 70000


def test_provider_lease_floor_outlives_submit_and_poll_window() -> None:
    assert provider_lease_floor_seconds({}) == 3835
    assert provider_lease_floor_seconds(
        {
            "TENCENT_REQUEST_TIMEOUT": "180",
            "TENCENT_TOKENHUB_POLL_TIMEOUT": "300",
        }
    ) == 4140
