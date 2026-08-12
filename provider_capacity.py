from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


MIN_PRODUCT_CONCURRENCY = 10
DEFAULT_PRODUCT_BATCH_IMAGES = 100
DEFAULT_PRODUCT_BATCH_TARGET_SECONDS = 60 * 60
DEFAULT_BATCH_WORKERS = 10
DEFAULT_TOKENHUB_MAX_CONCURRENCY = 10
DEFAULT_BATCH_CALL_LIMIT = 120
MAX_CONCURRENCY = 32
MAX_BATCH_CALL_LIMIT = 10_000
MAX_EVIDENCE_BYTES = 256 * 1024
MAX_PROVIDER_LEASE_SECONDS = 7 * 24 * 60 * 60
MAX_PROVIDER_REQUEST_TIMEOUT_SECONDS = 15 * 60
MAX_PROVIDER_POLL_TIMEOUT_SECONDS = 60 * 60
SHA256_RE = re.compile(r"[a-f0-9]{64}")


class ProviderCapacityExhausted(RuntimeError):
    pass


def _bounded_env_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(str(values.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class GenerationCapacity:
    batch_workers: int
    provider_concurrency: int
    batch_call_limit: int
    target_images: int
    target_seconds: int
    verified_provider_concurrency: int
    measured_provider_p95_seconds: int
    observed_target_batch_seconds: int
    target_batch_evidence_file: str
    target_batch_evidence_sha256: str
    provider_model: str
    acquire_timeout_seconds: int

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "GenerationCapacity":
        values = os.environ if env is None else env
        return cls(
            batch_workers=_bounded_env_int(
                values,
                "FINAL_GENERATION_WORKERS",
                DEFAULT_BATCH_WORKERS,
                minimum=1,
                maximum=MAX_CONCURRENCY,
            ),
            provider_concurrency=_bounded_env_int(
                values,
                "TENCENT_TOKENHUB_MAX_CONCURRENCY",
                DEFAULT_TOKENHUB_MAX_CONCURRENCY,
                minimum=1,
                maximum=MAX_CONCURRENCY,
            ),
            batch_call_limit=_bounded_env_int(
                values,
                "TENCENT_HUNYUAN_SYNC_LIMIT",
                DEFAULT_BATCH_CALL_LIMIT,
                minimum=0,
                maximum=MAX_BATCH_CALL_LIMIT,
            ),
            target_images=DEFAULT_PRODUCT_BATCH_IMAGES,
            target_seconds=DEFAULT_PRODUCT_BATCH_TARGET_SECONDS,
            verified_provider_concurrency=_bounded_env_int(
                values,
                "TENCENT_TOKENHUB_VERIFIED_CONCURRENCY",
                0,
                minimum=0,
                maximum=MAX_CONCURRENCY,
            ),
            measured_provider_p95_seconds=_bounded_env_int(
                values,
                "TENCENT_TOKENHUB_MEASURED_P95_SECONDS",
                0,
                minimum=0,
                maximum=24 * 60 * 60,
            ),
            observed_target_batch_seconds=_bounded_env_int(
                values,
                "GENERATION_TARGET_BATCH_OBSERVED_SECONDS",
                0,
                minimum=0,
                maximum=24 * 60 * 60,
            ),
            target_batch_evidence_file=str(
                values.get("GENERATION_TARGET_BATCH_EVIDENCE_FILE") or ""
            ).strip(),
            target_batch_evidence_sha256=str(
                values.get("GENERATION_TARGET_BATCH_EVIDENCE_SHA256") or ""
            ).strip().lower(),
            provider_model=str(
                values.get("TENCENT_TOKENHUB_IMAGE_MODEL") or "hy-image-v3"
            ).strip(),
            acquire_timeout_seconds=_bounded_env_int(
                values,
                "TENCENT_TOKENHUB_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS",
                10 * 60,
                minimum=1,
                maximum=24 * 60 * 60,
            ),
        )

    @property
    def configured_concurrency(self) -> int:
        return min(self.batch_workers, self.provider_concurrency)

    @property
    def active_provider_concurrency(self) -> int:
        verified = self.verified_provider_concurrency or 1
        return min(self.provider_concurrency, verified)

    @property
    def active_concurrency(self) -> int:
        return min(self.batch_workers, self.active_provider_concurrency)

    @property
    def configured_waves(self) -> int:
        return math.ceil(self.target_images / self.configured_concurrency)

    @property
    def active_waves(self) -> int:
        return math.ceil(self.target_images / self.active_concurrency)

    @property
    def max_provider_p95_seconds(self) -> float:
        return self.target_seconds / self.active_waves

    def public_contract(self) -> dict[str, Any]:
        structural_issues = []
        if self.target_images != DEFAULT_PRODUCT_BATCH_IMAGES:
            structural_issues.append("target_images_must_equal_100")
        if self.target_seconds != DEFAULT_PRODUCT_BATCH_TARGET_SECONDS:
            structural_issues.append("target_seconds_must_equal_3600")
        if self.batch_workers < MIN_PRODUCT_CONCURRENCY:
            structural_issues.append("batch_workers_below_10")
        if self.provider_concurrency < MIN_PRODUCT_CONCURRENCY:
            structural_issues.append("provider_concurrency_below_10")
        if self.batch_call_limit < self.target_images:
            structural_issues.append("batch_call_limit_below_target_images")

        verification_issues = []
        if self.verified_provider_concurrency < MIN_PRODUCT_CONCURRENCY:
            verification_issues.append("provider_concurrency_not_verified_at_10")
        if self.measured_provider_p95_seconds <= 0:
            verification_issues.append("provider_p95_not_measured")
        elif self.measured_provider_p95_seconds > self.max_provider_p95_seconds:
            verification_issues.append("provider_p95_exceeds_one_hour_budget")
        evidence, evidence_issues = verified_target_batch_evidence(self)
        verification_issues.extend(evidence_issues)

        return {
            "targetImages": self.target_images,
            "targetSeconds": self.target_seconds,
            "minimumRequiredConcurrency": MIN_PRODUCT_CONCURRENCY,
            "batchWorkers": self.batch_workers,
            "providerConcurrency": self.provider_concurrency,
            "configuredConcurrency": self.configured_concurrency,
            "activeProviderConcurrency": self.active_provider_concurrency,
            "activeConcurrency": self.active_concurrency,
            "batchCallLimit": self.batch_call_limit,
            "configuredWaves": self.configured_waves,
            "activeWaves": self.active_waves,
            "maxProviderP95SecondsPerWave": round(
                self.max_provider_p95_seconds,
                3,
            ),
            "verifiedProviderConcurrency": self.verified_provider_concurrency,
            "measuredProviderP95Seconds": self.measured_provider_p95_seconds,
            "observedTargetBatchSeconds": (
                evidence.get("elapsedSeconds")
                if evidence is not None
                else self.observed_target_batch_seconds
            ),
            "targetBatchEvidence": evidence,
            "structureReady": not structural_issues,
            "productionTargetVerified": (
                not structural_issues and not verification_issues
            ),
            "structuralIssues": structural_issues,
            "verificationIssues": verification_issues,
        }


class ProviderConcurrencyGate:
    def __init__(self, max_concurrency: int, *, acquire_timeout_seconds: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if acquire_timeout_seconds < 1:
            raise ValueError("acquire_timeout_seconds must be positive")
        self.max_concurrency = int(max_concurrency)
        self.acquire_timeout_seconds = int(acquire_timeout_seconds)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._state_lock = threading.Lock()
        self._active = 0
        self._peak = 0
        self._completed = 0

    @contextmanager
    def slot(self) -> Iterator[dict[str, float]]:
        wait_started = time.monotonic()
        acquired = self._semaphore.acquire(timeout=self.acquire_timeout_seconds)
        wait_seconds = time.monotonic() - wait_started
        if not acquired:
            raise ProviderCapacityExhausted(
                "TokenHub provider concurrency gate timed out before submit"
            )
        call_started = time.monotonic()
        with self._state_lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
        timing = {"slotWaitSeconds": round(wait_seconds, 6)}
        try:
            yield timing
        finally:
            timing["slotHeldSeconds"] = round(
                time.monotonic() - call_started,
                6,
            )
            with self._state_lock:
                self._active -= 1
                self._completed += 1
            self._semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "limit": self.max_concurrency,
                "active": self._active,
                "peak": self._peak,
                "completed": self._completed,
                "distributed": False,
            }


_RENEW_SLOT_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SLOT_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class RedisProviderConcurrencyGate:
    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str,
        max_concurrency: int,
        acquire_timeout_seconds: int,
        lease_seconds: int = 300,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if acquire_timeout_seconds < 1 or lease_seconds < 3:
            raise ValueError("provider gate timeouts are invalid")
        self.redis = redis_client
        self.key_prefix = str(key_prefix).strip().rstrip(":")
        self.max_concurrency = int(max_concurrency)
        self.acquire_timeout_seconds = int(acquire_timeout_seconds)
        self.lease_seconds = int(lease_seconds)
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._state_lock = threading.Lock()
        self._active = 0
        self._peak = 0
        self._completed = 0
        self._retained = 0

    def _slot_key(self, slot_index: int) -> str:
        return f"{self.key_prefix}:slot:{slot_index}"

    @contextmanager
    def slot(self) -> Iterator[dict[str, Any]]:
        token = secrets.token_urlsafe(24)
        wait_started = time.monotonic()
        deadline = wait_started + self.acquire_timeout_seconds
        lease_ms = self.lease_seconds * 1000
        first_slot = int.from_bytes(
            token.encode("utf-8")[:4].ljust(4, b"0"),
            "big",
        ) % self.max_concurrency
        acquired_key = ""
        acquired_index = -1
        while time.monotonic() < deadline and not acquired_key:
            for offset in range(self.max_concurrency):
                slot_index = (first_slot + offset) % self.max_concurrency
                slot_key = self._slot_key(slot_index)
                if self.redis.set(
                    slot_key,
                    token,
                    nx=True,
                    px=lease_ms,
                ):
                    acquired_key = slot_key
                    acquired_index = slot_index
                    break
            if not acquired_key:
                time.sleep(
                    min(
                        self.poll_interval_seconds,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
        if not acquired_key:
            raise ProviderCapacityExhausted(
                "TokenHub distributed concurrency gate timed out before submit"
            )

        stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat_interval = max(1.0, min(self.lease_seconds / 3, 30.0))

        def heartbeat() -> None:
            while not stop.wait(heartbeat_interval):
                try:
                    renewed = self.redis.eval(
                        _RENEW_SLOT_LUA,
                        1,
                        acquired_key,
                        token,
                        str(lease_ms),
                    )
                except Exception:
                    lease_lost.set()
                    return
                if int(renewed or 0) != 1:
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"tokenhub-capacity-{acquired_index}",
            daemon=True,
        )
        heartbeat_thread.start()
        call_started = time.monotonic()
        with self._state_lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
        timing: dict[str, Any] = {
            "slotWaitSeconds": round(call_started - wait_started, 6),
            "distributed": True,
            "slot": acquired_index,
        }
        call_failed = False
        try:
            yield timing
        except BaseException:
            call_failed = True
            raise
        finally:
            timing["slotHeldSeconds"] = round(
                time.monotonic() - call_started,
                6,
            )
            stop.set()
            heartbeat_thread.join(timeout=1)
            retain_lease = call_failed or lease_lost.is_set()
            if not retain_lease:
                try:
                    released = self.redis.eval(
                        _RELEASE_SLOT_LUA,
                        1,
                        acquired_key,
                        token,
                    )
                    if int(released or 0) != 1:
                        lease_lost.set()
                        retain_lease = True
                        timing["releaseFailed"] = True
                except Exception:
                    timing["releaseFailed"] = True
                    retain_lease = True
            timing["leaseLost"] = lease_lost.is_set()
            timing["leaseRetained"] = retain_lease
            with self._state_lock:
                self._active -= 1
                self._completed += 1
                if retain_lease:
                    self._retained += 1

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "limit": self.max_concurrency,
                "activeInProcess": self._active,
                "peakInProcess": self._peak,
                "completedInProcess": self._completed,
                "retainedAmbiguousLeasesInProcess": self._retained,
                "distributed": True,
                "leaseSeconds": self.lease_seconds,
            }


def provider_lease_floor_seconds(values: Mapping[str, str]) -> int:
    request_timeout = _bounded_env_int(
        values,
        "TENCENT_REQUEST_TIMEOUT",
        55,
        minimum=1,
        maximum=MAX_PROVIDER_REQUEST_TIMEOUT_SECONDS,
    )
    poll_timeout = _bounded_env_int(
        values,
        "TENCENT_TOKENHUB_POLL_TIMEOUT",
        120,
        minimum=1,
        maximum=MAX_PROVIDER_POLL_TIMEOUT_SECONDS,
    )
    configured_target_seconds = _bounded_env_int(
        values,
        "GENERATION_TARGET_SECONDS",
        DEFAULT_PRODUCT_BATCH_TARGET_SECONDS,
        minimum=60,
        maximum=24 * 60 * 60,
    )
    return min(
        MAX_PROVIDER_LEASE_SECONDS,
        max(
            DEFAULT_PRODUCT_BATCH_TARGET_SECONDS,
            configured_target_seconds,
        )
        + request_timeout
        + poll_timeout
        + 60,
    )


def build_provider_concurrency_gate(
    capacity: GenerationCapacity,
    env: Mapping[str, str] | None = None,
) -> ProviderConcurrencyGate | RedisProviderConcurrencyGate:
    values = os.environ if env is None else env
    redis_url = str(values.get("REDIS_URL") or "").strip()
    if not redis_url:
        return ProviderConcurrencyGate(
            capacity.active_provider_concurrency,
            acquire_timeout_seconds=capacity.acquire_timeout_seconds,
        )
    import redis

    namespace = str(values.get("REDIS_NAMESPACE") or "waimai:saas").strip()
    lease_seconds = _bounded_env_int(
        values,
        "TENCENT_TOKENHUB_CONCURRENCY_LEASE_SECONDS",
        300,
        minimum=30,
        maximum=MAX_PROVIDER_LEASE_SECONDS,
    )
    lease_seconds = max(lease_seconds, provider_lease_floor_seconds(values))
    return RedisProviderConcurrencyGate(
        redis.Redis.from_url(redis_url, decode_responses=True),
        key_prefix=f"{namespace}:provider-capacity:tokenhub",
        max_concurrency=capacity.active_provider_concurrency,
        acquire_timeout_seconds=capacity.acquire_timeout_seconds,
        lease_seconds=lease_seconds,
    )


def build_mask_concurrency_gate(
    verified_concurrency: int,
    env: Mapping[str, str] | None = None,
) -> ProviderConcurrencyGate | RedisProviderConcurrencyGate:
    values = os.environ if env is None else env
    concurrency = max(1, min(int(verified_concurrency), MAX_CONCURRENCY))
    acquire_timeout = _bounded_env_int(
        values,
        "TENCENT_MASK_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS",
        10 * 60,
        minimum=1,
        maximum=24 * 60 * 60,
    )
    redis_url = str(values.get("REDIS_URL") or "").strip()
    if not redis_url:
        return ProviderConcurrencyGate(
            concurrency,
            acquire_timeout_seconds=acquire_timeout,
        )
    import redis

    namespace = str(values.get("REDIS_NAMESPACE") or "waimai:saas").strip()
    request_timeout = _bounded_env_int(
        values,
        "TENCENT_REQUEST_TIMEOUT",
        55,
        minimum=1,
        maximum=MAX_PROVIDER_REQUEST_TIMEOUT_SECONDS,
    )
    lease_seconds = _bounded_env_int(
        values,
        "TENCENT_MASK_CONCURRENCY_LEASE_SECONDS",
        request_timeout + 60,
        minimum=30,
        maximum=MAX_PROVIDER_LEASE_SECONDS,
    )
    lease_seconds = max(lease_seconds, request_timeout + 60)
    return RedisProviderConcurrencyGate(
        redis.Redis.from_url(redis_url, decode_responses=True),
        key_prefix=f"{namespace}:provider-capacity:tencent-mask",
        max_concurrency=concurrency,
        acquire_timeout_seconds=acquire_timeout,
        lease_seconds=lease_seconds,
    )


def verified_target_batch_evidence(
    capacity: GenerationCapacity,
) -> tuple[dict[str, Any] | None, list[str]]:
    path_text = capacity.target_batch_evidence_file
    expected_sha256 = capacity.target_batch_evidence_sha256
    if not path_text or not SHA256_RE.fullmatch(expected_sha256):
        return None, ["target_batch_evidence_not_configured"]
    path = Path(path_text).expanduser()
    try:
        if not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
            return None, ["target_batch_evidence_unavailable"]
        raw = path.read_bytes()
    except OSError:
        return None, ["target_batch_evidence_unavailable"]
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not secrets.compare_digest(actual_sha256, expected_sha256):
        return None, ["target_batch_evidence_sha256_mismatch"]
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ["target_batch_evidence_invalid"]
    if not isinstance(payload, dict):
        return None, ["target_batch_evidence_invalid"]
    required_digest = str(payload.get("outputManifestSha256") or "").lower()
    elapsed = payload.get("elapsedSeconds")
    try:
        elapsed_seconds = float(elapsed)
        requested = int(payload.get("requestedImages") or 0)
        succeeded = int(payload.get("succeededImages") or 0)
        peak_concurrency = int(payload.get("peakProviderConcurrency") or 0)
    except (TypeError, ValueError):
        return None, ["target_batch_evidence_invalid"]
    valid = bool(
        payload.get("schemaVersion") == 1
        and payload.get("mode") == "real"
        and payload.get("paidProviderVerified") is True
        and str(payload.get("provider") or "") == "tencent-hunyuan"
        and str(payload.get("model") or "") == capacity.provider_model
        and requested == DEFAULT_PRODUCT_BATCH_IMAGES
        and succeeded == DEFAULT_PRODUCT_BATCH_IMAGES
        and 0 < elapsed_seconds <= DEFAULT_PRODUCT_BATCH_TARGET_SECONDS
        and peak_concurrency >= MIN_PRODUCT_CONCURRENCY
        and SHA256_RE.fullmatch(required_digest)
        and bool(str(payload.get("runId") or "").strip())
    )
    if not valid:
        return None, ["target_batch_evidence_invalid"]
    return {
        "runId": str(payload["runId"]),
        "provider": "tencent-hunyuan",
        "model": capacity.provider_model,
        "requestedImages": requested,
        "succeededImages": succeeded,
        "elapsedSeconds": round(elapsed_seconds, 3),
        "peakProviderConcurrency": peak_concurrency,
        "outputManifestSha256": required_digest,
        "evidenceSha256": actual_sha256,
    }, []
def observed_batch_performance(
    *,
    requested: int,
    succeeded: int,
    elapsed_seconds: float,
    capacity: GenerationCapacity,
) -> dict[str, Any]:
    clean_elapsed = max(0.0, float(elapsed_seconds))
    clean_succeeded = max(0, int(succeeded))
    projected = None
    if clean_succeeded > 0 and clean_elapsed > 0:
        projected = (
            clean_elapsed
            * DEFAULT_PRODUCT_BATCH_IMAGES
            / clean_succeeded
        )
    complete_target_batch = (
        int(requested) >= DEFAULT_PRODUCT_BATCH_IMAGES
        and clean_succeeded >= DEFAULT_PRODUCT_BATCH_IMAGES
    )
    return {
        "elapsedSeconds": round(clean_elapsed, 3),
        "completedImages": clean_succeeded,
        "projectedSecondsForTargetImages": (
            round(projected, 3) if projected is not None else None
        ),
        "oneHourTargetObserved": (
            clean_elapsed <= DEFAULT_PRODUCT_BATCH_TARGET_SECONDS
            if complete_target_batch
            else None
        ),
    }
