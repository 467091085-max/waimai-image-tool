from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol

from shared.redis_queue import RedisTaskQueue, queue_from_env
from worker.prompt_provider import (
    PromptProviderError,
    TokenHubPromptConfig,
    TokenHubPromptProvider,
)
from worker.worker import GenerationWorker


LOGGER = logging.getLogger("waimai.prompt-worker")
PROMPT_GENERATION_TASK_TYPE = "prompt_generation"


class PromptProvider(Protocol):
    def generate(self, prompt: str) -> Mapping[str, Any]:
        ...


class PromptTaskHandler:
    def __init__(self, provider: PromptProvider) -> None:
        self.provider = provider

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        task_type = str(payload.get("taskType") or "").strip()
        if task_type != PROMPT_GENERATION_TASK_TYPE:
            raise PromptProviderError(
                f"unsupported prompt task type: {task_type or 'missing'}"
            )
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise PromptProviderError("prompt is required")
        return self.provider.generate(prompt)


def build_prompt_worker(
    *,
    env: Mapping[str, str] | None = None,
    queue: RedisTaskQueue | None = None,
    provider: PromptProvider | None = None,
) -> GenerationWorker:
    values = os.environ if env is None else env
    resolved_provider = provider or TokenHubPromptProvider(
        TokenHubPromptConfig.from_env(values)
    )
    return GenerationWorker(
        queue or queue_from_env(values),
        handler=PromptTaskHandler(resolved_provider),
        max_retries=_env_int(values, "WORKER_MAX_RETRIES", 2, minimum=0),
        task_timeout_seconds=_env_float(
            values,
            "WORKER_TASK_TIMEOUT",
            180,
            minimum=0.001,
        ),
        recovery_stale_seconds=_env_float(
            values,
            "WORKER_RECOVERY_STALE_SECONDS",
            180,
            minimum=0.001,
        ),
        worker_id=str(values.get("WORKER_ID") or "").strip() or None,
        lease_seconds=(
            _env_float(
                values,
                "WORKER_LEASE_SECONDS",
                0,
                minimum=0,
            )
            or None
        ),
        service_id=(
            str(values.get("PROMPT_WORKER_SERVICE_ID") or "prompt-worker").strip()
            or "prompt-worker"
        ),
        service_heartbeat_ttl_seconds=_env_int(
            values,
            "WORKER_SERVICE_HEARTBEAT_TTL_SECONDS",
            30,
            minimum=3,
        ),
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    worker = build_prompt_worker()
    LOGGER.info(
        "prompt worker started",
        extra={
            "queue_name": worker.queue.config.queue_name,
            "service_id": worker.service_id,
        },
    )
    worker.run_forever(
        timeout_seconds=_env_int(
            os.environ,
            "WORKER_BRPOP_TIMEOUT",
            5,
            minimum=0,
        )
    )


def _env_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = values.get(name)
    try:
        number = int(default if raw in (None, "") else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


def _env_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    raw = values.get(name)
    try:
        number = float(default if raw in (None, "") else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


if __name__ == "__main__":
    main()
