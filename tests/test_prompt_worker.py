from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest
import yaml

import worker.prompt_provider as prompt_provider_module
from shared.redis_queue import RedisQueueConfig, RedisTaskQueue
from tests.redis_test_double import RedisTestDouble
from worker.prompt_provider import (
    PromptProviderConfigurationError,
    PromptProviderError,
    TOKENHUB_HY_V3_URL,
    TOKENHUB_IMAGE_QUERY_URL,
    TOKENHUB_IMAGE_SUBMIT_URL,
    TokenHubPromptConfig,
    TokenHubPromptProvider,
)
from worker.prompt_worker import build_prompt_worker


ROOT = Path(__file__).resolve().parents[1]


class RecordingProvider:
    def __init__(self, result: Mapping[str, Any] | None = None) -> None:
        self.calls: list[str] = []
        self.result = dict(
            result
            or {
                "image_url": "https://cdn.example/generated.jpg",
                "provider": "test",
            }
        )

    def generate(self, prompt: str) -> Mapping[str, Any]:
        self.calls.append(prompt)
        return dict(self.result)


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _prompt: str) -> Mapping[str, Any]:
        self.calls += 1
        raise PromptProviderError("provider unavailable", retryable=True)


def _queue() -> RedisTaskQueue:
    return RedisTaskQueue(
        RedisTestDouble(),
        RedisQueueConfig(namespace="prompt-test", queue_name="generate"),
    )


def _load_api_server_module():
    module_path = ROOT / "api-server" / "app.py"
    spec = importlib.util.spec_from_file_location(
        "prompt_worker_api_server",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tokenhub_prompt_config_fails_closed_without_real_provider() -> None:
    with pytest.raises(PromptProviderConfigurationError):
        TokenHubPromptConfig.from_env({})

    with pytest.raises(PromptProviderConfigurationError):
        TokenHubPromptConfig.from_env(
            {
                "AI_IMAGE_PROVIDER": "mock",
                "TENCENT_TOKENHUB_API_KEY": "must-not-enable-mock",
            }
        )

    with pytest.raises(PromptProviderConfigurationError):
        TokenHubPromptConfig.from_env(
            {
                "AI_IMAGE_PROVIDER": "tencent-tokenhub",
                "TENCENT_TOKENHUB_ENABLED": "false",
                "TENCENT_TOKENHUB_API_KEY": "disabled-key",
            }
        )


def test_tokenhub_prompt_http_response_is_bounded_before_json_decode() -> None:
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"x" * 9
    provider = TokenHubPromptProvider(
        TokenHubPromptConfig(api_key="test-only-key")
    )

    with (
        mock.patch.object(
            prompt_provider_module,
            "MAX_PROVIDER_RESPONSE_BYTES",
            8,
        ),
        mock.patch.object(
            prompt_provider_module.urllib.request,
            "urlopen",
            return_value=response,
        ),
        mock.patch.object(
            prompt_provider_module.json,
            "loads",
        ) as json_loads,
    ):
        with pytest.raises(
            PromptProviderError,
            match="response exceeds size limit",
        ):
            provider._post_json(
                TOKENHUB_IMAGE_SUBMIT_URL,
                {"model": "hy-image-v3.0", "prompt": "dish"},
                30,
            )

    response.read.assert_called_once_with(9)
    json_loads.assert_not_called()


def test_tokenhub_prompt_input_is_bounded_before_http_request() -> None:
    http_post = mock.Mock(
        side_effect=AssertionError(
            "oversized prompt must not reach the provider"
        )
    )
    provider = TokenHubPromptProvider(
        TokenHubPromptConfig(api_key="test-only-key"),
        http_post=http_post,
    )

    with mock.patch.object(
        prompt_provider_module,
        "MAX_PROMPT_CHARS",
        8,
    ):
        with pytest.raises(
            PromptProviderError,
            match="prompt exceeds size limit",
        ):
            provider.generate("x" * 9)

    http_post.assert_not_called()


def test_tokenhub_request_payload_is_bounded_before_serialization() -> None:
    provider = TokenHubPromptProvider(
        TokenHubPromptConfig(api_key="test-only-key")
    )

    with (
        mock.patch.object(
            prompt_provider_module,
            "MAX_PROVIDER_REQUEST_BYTES",
            32,
        ),
        mock.patch.object(
            prompt_provider_module.json,
            "dumps",
            side_effect=AssertionError(
                "oversized request must not be serialized"
            ),
        ) as json_dumps,
    ):
        with pytest.raises(
            PromptProviderError,
            match="request exceeds size limit",
        ):
            provider._post_json(
                TOKENHUB_IMAGE_SUBMIT_URL,
                {"prompt": "x" * 64},
                30,
            )

    json_dumps.assert_not_called()


def test_tokenhub_v3_adapter_uses_submit_then_query_without_network() -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []
    responses = [
        {
            "id": "job-123",
            "request_id": "submit-request",
            "status": "queued",
        },
        {
            "id": "job-123",
            "request_id": "query-request",
            "status": "completed",
            "data": [
                {"url": "https://cdn.example/tokenhub-result.jpg"}
            ],
        },
    ]
    clock = {"now": 0.0}

    def http_post(
        url: str,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> Mapping[str, Any]:
        calls.append((url, dict(payload), timeout))
        return responses.pop(0)

    provider = TokenHubPromptProvider(
        TokenHubPromptConfig(
            api_key="test-only-key",
            poll_timeout_seconds=10,
            poll_interval_seconds=1,
        ),
        http_post=http_post,
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__(
            "now",
            clock["now"] + seconds,
        ),
    )

    result = provider.generate("一份完整铺满画面的牛肉饭商品图")

    assert result == {
        "image_url": "https://cdn.example/tokenhub-result.jpg",
        "model": "hy-image-v3.0",
        "provider": "tencent-tokenhub",
        "provider_job_id": "job-123",
        "request_id": "query-request",
    }
    assert calls == [
        (
            TOKENHUB_IMAGE_SUBMIT_URL,
            {
                "model": "hy-image-v3.0",
                "prompt": "一份完整铺满画面的牛肉饭商品图",
            },
            30,
        ),
        (
            TOKENHUB_IMAGE_QUERY_URL,
            {"model": "hy-image-v3.0", "id": "job-123"},
            9,
        ),
    ]


def test_tokenhub_current_hy_image_v3_uses_official_sync_protocol() -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []

    def http_post(
        url: str,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> Mapping[str, Any]:
        calls.append((url, dict(payload), timeout))
        return {
            "request_id": "wand-prompt-1",
            "data": [{"url": "https://cdn.example/hy-image-v3.jpg"}],
        }

    provider = TokenHubPromptProvider(
        TokenHubPromptConfig(
            api_key="test-only-key",
            model="hy-image-v3",
            protocol="wand-sync-v1",
            request_timeout_seconds=55,
            poll_timeout_seconds=120,
        ),
        http_post=http_post,
    )

    result = provider.generate("适合盖饭的干净外卖摄影背景")

    assert result == {
        "image_url": "https://cdn.example/hy-image-v3.jpg",
        "model": "hy-image-v3",
        "provider": "tencent-tokenhub",
        "provider_job_id": "",
        "request_id": "wand-prompt-1",
    }
    assert calls == [
        (
            TOKENHUB_HY_V3_URL,
            {
                "model": "hy-image-v3",
                "prompt": "适合盖饭的干净外卖摄影背景",
                "revise": False,
            },
            120,
        )
    ]


def test_generate_queue_flows_from_api_to_prompt_worker_done(
    monkeypatch,
) -> None:
    queue = _queue()
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    monkeypatch.setenv("PROMPT_API_TOKEN", "prompt-api-test-token")
    api_module.app.config.update(TESTING=True)
    provider = RecordingProvider()
    headers = {"Authorization": "Bearer prompt-api-test-token"}

    response = api_module.app.test_client().post(
        "/generate",
        json={"prompt": "招牌牛肉饭商品图"},
        headers=headers,
    )
    assert response.status_code == 202
    task_id = response.get_json()["task_id"]

    worker = build_prompt_worker(queue=queue, provider=provider, env={})
    assert worker.process_one(timeout_seconds=0) is True

    status_response = api_module.app.test_client().get(
        f"/status/{task_id}",
        headers=headers,
    )
    assert status_response.status_code == 200
    assert status_response.get_json() == {
        "status": "done",
        "image_url": "https://cdn.example/generated.jpg",
    }
    assert provider.calls == ["招牌牛肉饭商品图"]
    assert queue.get(task_id)["attempts"] == 1


def test_prompt_worker_retries_with_bound_then_writes_failed(
    monkeypatch,
) -> None:
    queue = _queue()
    task = queue.enqueue(
        {
            "taskType": "prompt_generation",
            "prompt": "强制失败",
        },
        task_id="prompt-failed",
    )
    provider = FailingProvider()
    import worker.worker as worker_module

    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
    worker = build_prompt_worker(
        queue=queue,
        provider=provider,
        env={"WORKER_MAX_RETRIES": "1"},
    )

    assert task["status"] == "pending"
    assert worker.process_one(timeout_seconds=0) is True

    result = queue.get("prompt-failed")
    assert provider.calls == 2
    assert result["status"] == "failed"
    assert result["image_url"] == ""
    assert result["attempts"] == 2
    assert result["error"] == "provider unavailable"


def test_prompt_worker_does_not_retry_ambiguous_paid_submission() -> None:
    queue = _queue()
    queue.enqueue(
        {
            "taskType": "prompt_generation",
            "prompt": "不要重复付费提交",
        },
        task_id="prompt-nonretryable",
    )
    provider = FailingProvider()

    def fail_once(_prompt: str) -> Mapping[str, Any]:
        provider.calls += 1
        raise PromptProviderError("paid submit outcome unknown")

    provider.generate = fail_once  # type: ignore[method-assign]
    worker = build_prompt_worker(
        queue=queue,
        provider=provider,
        env={"WORKER_MAX_RETRIES": "2"},
    )

    assert worker.process_one(timeout_seconds=0) is True
    result = queue.get("prompt-nonretryable")
    assert provider.calls == 1
    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["error"] == "paid submit outcome unknown"


def test_prompt_worker_publishes_queue_scoped_service_heartbeat() -> None:
    queue = _queue()
    worker = build_prompt_worker(
        queue=queue,
        provider=RecordingProvider(),
        env={
            "PROMPT_WORKER_SERVICE_ID": "prompt-worker",
            "WORKER_SERVICE_HEARTBEAT_TTL_SECONDS": "12",
        },
    )

    heartbeat = worker.publish_service_heartbeat()
    liveness = queue.service_liveness("prompt-worker")

    assert heartbeat["queueName"] == "generate"
    assert liveness is not None
    assert liveness["serviceId"] == "prompt-worker"
    assert liveness["instanceId"] == worker.worker_id
    assert liveness["ttlSeconds"] == 12


def test_api_health_requires_live_prompt_worker_heartbeat(
    monkeypatch,
) -> None:
    queue = _queue()
    api_module = _load_api_server_module()
    monkeypatch.setattr(api_module, "task_queue", lambda: queue)
    api_module.app.config.update(TESTING=True)
    client = api_module.app.test_client()

    unavailable = client.get("/healthz")
    assert unavailable.status_code == 503
    assert unavailable.get_json() == {
        "ok": False,
        "service": "api-server",
        "code": "prompt_worker_unavailable",
    }

    worker = build_prompt_worker(
        queue=queue,
        provider=RecordingProvider(),
        env={"PROMPT_WORKER_SERVICE_ID": "prompt-worker"},
    )
    worker.publish_service_heartbeat()

    ready = client.get("/healthz")
    assert ready.status_code == 200
    assert ready.get_json() == {
        "ok": True,
        "service": "api-server",
        "promptWorker": "ready",
    }


def test_api_server_does_not_import_prompt_provider_or_worker() -> None:
    source = (ROOT / "api-server" / "app.py").read_text(encoding="utf-8")

    assert "prompt_provider" not in source
    assert "from worker.prompt_worker" not in source
    assert "import worker.prompt_worker" not in source
    assert "shared.generator" not in source
    assert "generate_image" not in source


def test_render_blueprint_declares_real_prompt_consumer_and_keeps_web() -> None:
    blueprint = yaml.safe_load(
        (ROOT / "render.yaml").read_text(encoding="utf-8")
    )
    services = {
        service["name"]: service
        for service in blueprint["services"]
    }
    customer = services["waimai-image-tool"]
    api = services["waimai-image-tool-api"]
    prompt_worker = services["waimai-image-tool-prompt-worker"]

    assert customer["startCommand"].startswith("gunicorn app:app ")
    assert api["startCommand"].startswith(
        "gunicorn --chdir api-server app:app "
    )
    assert prompt_worker["type"] == "worker"
    assert prompt_worker["plan"] != "free"
    assert (
        prompt_worker["startCommand"]
        == "python -m worker.prompt_worker"
    )
    env = {
        entry["key"]: entry
        for entry in prompt_worker["envVars"]
        if "key" in entry
    }
    assert env["REDIS_URL"]["fromService"] == {
        "type": "keyvalue",
        "name": "waimai-image-tool-redis",
        "property": "connectionString",
    }
    assert env["AI_IMAGE_PROVIDER"]["value"] == "tencent-tokenhub"
    assert env["TENCENT_TOKENHUB_API_KEY"]["fromService"] == {
        "type": "web",
        "name": "waimai-image-tool",
        "envVarKey": "TENCENT_TOKENHUB_API_KEY",
    }
    assert env["PROMPT_WORKER_SERVICE_ID"]["value"] == "prompt-worker"
    assert int(env["WORKER_MAX_RETRIES"]["value"]) >= 0
    assert "DATABASE_URL" not in env
    assert "ALLOW_MOCK_GENERATION" not in env
    api_env = {
        entry["key"]: entry
        for entry in api["envVars"]
        if "key" in entry
    }
    assert api_env["PROMPT_WORKER_SERVICE_ID"]["value"] == "prompt-worker"
    assert api_env["PROMPT_API_TOKEN"]["generateValue"] is True
