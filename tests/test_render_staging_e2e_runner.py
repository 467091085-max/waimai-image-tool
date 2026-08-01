from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock

import pytest
import requests
import object_storage_service


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_staging_e2e as runner


def response(status_code: int, body: bytes = b"{}") -> requests.Response:
    result = requests.Response()
    result.status_code = status_code
    result._content = body
    result._content_consumed = True
    return result


def test_job_status_get_retries_connection_reset_without_changing_request() -> None:
    client = runner.RemoteClient(
        "http://127.0.0.1:10000",
        username="staging-user",
        password="staging-password",
        timeout_seconds=30,
    )
    client.session.get = mock.Mock(
        side_effect=[requests.ConnectionError("reset"), response(200)]
    )

    with mock.patch.object(runner.time, "sleep") as sleep:
        result = client.get("/api/generation-jobs/job-1")

    assert result.status_code == 200
    assert client.session.get.call_count == 2
    assert (
        client.session.get.call_args_list[0]
        == client.session.get.call_args_list[1]
    )
    assert client.session.get.call_args.kwargs["headers"]["Connection"] == "close"
    sleep.assert_called_once_with(runner.GET_RETRY_BASE_DELAY_SECONDS)


def test_get_retries_transient_status_but_returns_terminal_status() -> None:
    client = runner.RemoteClient(
        "http://127.0.0.1:10000",
        username="staging-user",
        password="staging-password",
        timeout_seconds=30,
    )
    transient = mock.Mock(status_code=503)
    client.session.get = mock.Mock(side_effect=[transient, response(200)])

    with mock.patch.object(runner.time, "sleep"):
        result = client.get("/api/generation-jobs/job-1")

    assert result.status_code == 200
    transient.close.assert_called_once_with()
    assert client.session.get.call_count == 2


def test_generation_trigger_get_is_never_retried() -> None:
    client = runner.RemoteClient(
        "http://127.0.0.1:10000",
        username="staging-user",
        password="staging-password",
        timeout_seconds=30,
    )
    client.session.get = mock.Mock(
        side_effect=requests.ConnectionError("reset")
    )

    with pytest.raises(requests.ConnectionError, match="reset"):
        client.get("/api/style-preview-sample?style=style-3&index=2")

    assert client.session.get.call_count == 1
    assert "Connection" not in client.session.get.call_args.kwargs["headers"]


def test_post_is_not_retried_after_connection_failure() -> None:
    client = runner.RemoteClient(
        "http://127.0.0.1:10000",
        username="staging-user",
        password="staging-password",
        timeout_seconds=30,
    )
    client.session.post = mock.Mock(
        side_effect=requests.ConnectionError("reset")
    )

    with pytest.raises(requests.ConnectionError, match="reset"):
        client.post("/api/generation-jobs", json={"quality": "standard"})

    assert client.session.post.call_count == 1


def test_remote_client_runs_keepalive_guard_before_request() -> None:
    client = runner.RemoteClient(
        "http://127.0.0.1:10000",
        username="staging-user",
        password="staging-password",
        timeout_seconds=30,
    )
    guard = mock.Mock()
    client.request_guard = guard
    client.session.get = mock.Mock(return_value=response(200))

    result = client.get("/")

    assert result.status_code == 200
    guard.assert_called_once_with()


def test_keepalive_origin_rejects_unconfigured_host_before_auth_is_used() -> None:
    with mock.patch.dict(
        runner.os.environ,
        {
            "RENDER_EXTERNAL_URL": "https://waimai-image-tool-1.onrender.com",
            runner.KEEPALIVE_URL_ENV: "https://attacker.example",
        },
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="must match"):
            runner.staging_keepalive_origin()


def test_staging_loopback_origin_accepts_only_current_render_process() -> None:
    with mock.patch.dict(
        runner.os.environ,
        {"WAIMAI_STAGING_E2E_BASE_URL": "http://127.0.0.1:10000/"},
        clear=True,
    ):
        assert runner.staging_loopback_origin(10000) == "http://127.0.0.1:10000"


def test_e2e_run_claim_blocks_restart_before_paid_work(tmp_path: Path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    with mock.patch.object(
        object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        object_key, record = runner.claim_e2e_run(
            "final-run-20260801",
            instance_nonce="a" * 64,
            menu_sha256="b" * 64,
            expected_category="mixed_rice",
        )
        with pytest.raises(RuntimeError, match="already claimed"):
            runner.claim_e2e_run(
                "final-run-20260801",
                instance_nonce="c" * 64,
                menu_sha256="b" * 64,
                expected_category="mixed_rice",
            )
        runner.finalize_e2e_run_claim(
            object_key,
            record,
            status=runner.PASS,
            report_artifact={"sha256": "d" * 64},
        )

    final_record = json.loads(storage.read_bytes(object_key))
    assert final_record["status"] == runner.PASS
    assert final_record["reportArtifact"]["sha256"] == "d" * 64


@pytest.mark.parametrize(
    "value",
    [
        "https://waimai-image-tool-1.onrender.com",
        "http://attacker.example:10000",
        "http://user:secret@127.0.0.1:10000",
        "http://127.0.0.1:10001",
        "http://127.0.0.1:10000/api",
    ],
)
def test_staging_loopback_origin_rejects_remote_or_ambiguous_urls(
    value: str,
) -> None:
    with mock.patch.dict(
        runner.os.environ,
        {"WAIMAI_STAGING_E2E_BASE_URL": value},
        clear=True,
    ):
        with pytest.raises(RuntimeError, match="current loopback"):
            runner.staging_loopback_origin(10000)


@pytest.mark.parametrize(
    "value",
    [
        "http://waimai-image-tool-1.onrender.com",
        "https://user:secret@waimai-image-tool-1.onrender.com",
        "https://waimai-image-tool-1.onrender.com/health",
        "https://127.0.0.1",
    ],
)
def test_keepalive_origin_rejects_unsafe_urls(value: str) -> None:
    with pytest.raises(RuntimeError):
        runner.https_origin(value)


def test_public_keepalive_probes_without_following_redirects() -> None:
    instance_nonce = "a" * 64
    keepalive = runner.PublicKeepAlive(
        "https://waimai-image-tool-1.onrender.com/",
        hostname="waimai-image-tool-1.onrender.com",
        username="staging-user",
        password="staging-password",
        instance_nonce=instance_nonce,
        interval_seconds=60,
        startup_timeout_seconds=0,
    )
    keepalive.session.get = mock.Mock(
        return_value=response(200, b'{"instanceMatched":true}')
    )

    keepalive.start()
    try:
        keepalive.assert_healthy()
        report = keepalive.report()
    finally:
        keepalive.stop()

    assert report == {
        "hostname": "waimai-image-tool-1.onrender.com",
        "instanceMatched": True,
        "probeCount": 1,
        "consecutiveFailures": 0,
    }
    assert keepalive.session.auth == ("staging-user", "staging-password")
    assert keepalive.session.get.call_args.kwargs["allow_redirects"] is False
    assert (
        keepalive.session.get.call_args.kwargs["headers"][
            "X-Waimai-Staging-Instance"
        ]
        == instance_nonce
    )


def test_public_keepalive_fails_closed_on_first_non_200_response() -> None:
    keepalive = runner.PublicKeepAlive(
        "https://waimai-image-tool-1.onrender.com/",
        hostname="waimai-image-tool-1.onrender.com",
        username="staging-user",
        password="staging-password",
        instance_nonce="a" * 64,
        interval_seconds=60,
        startup_timeout_seconds=0,
    )
    keepalive.session.get = mock.Mock(return_value=response(302))

    with pytest.raises(RuntimeError, match="HTTP 302"):
        keepalive.start()

    assert keepalive.report()["probeCount"] == 0
    keepalive.stop()


def test_public_keepalive_rejects_200_from_a_different_instance() -> None:
    keepalive = runner.PublicKeepAlive(
        "https://waimai-image-tool-1.onrender.com/",
        hostname="waimai-image-tool-1.onrender.com",
        username="staging-user",
        password="staging-password",
        instance_nonce="a" * 64,
        interval_seconds=60,
        startup_timeout_seconds=0,
    )
    keepalive.session.get = mock.Mock(
        return_value=response(200, b'{"instanceMatched":false}')
    )

    with pytest.raises(RuntimeError, match="different Render instance"):
        keepalive.start()

    assert keepalive.report()["probeCount"] == 0
    keepalive.stop()


def test_runtime_generation_capacity_accepts_sufficient_limit() -> None:
    client = mock.Mock()
    client.get.return_value = runner.RemoteResponse(
        response(
            200,
            json.dumps(
                {
                    "configured": True,
                    "provider": "tencent-hunyuan",
                    "syncLimit": 60,
                }
            ).encode("utf-8"),
        )
    )

    payload = runner.verify_runtime_generation_capacity(client, 60)

    assert payload["syncLimit"] == 60
    client.get.assert_called_once_with("/api/tencent-status")


def test_runtime_generation_capacity_rejects_low_limit_before_paid_flow() -> None:
    client = mock.Mock()
    client.get.return_value = runner.RemoteResponse(
        response(
            200,
            json.dumps(
                {
                    "configured": True,
                    "provider": "tencent-hunyuan",
                    "syncLimit": 1,
                }
            ).encode("utf-8"),
        )
    )

    with pytest.raises(
        runner.AcceptanceError,
        match="required 60, got 1",
    ) as exc_info:
        runner.verify_runtime_generation_capacity(client, 60)

    assert exc_info.value.stage == "preflight"
    client.get.assert_called_once_with("/api/tencent-status")


def test_runtime_generation_capacity_rejects_unconfigured_provider() -> None:
    client = mock.Mock()
    client.get.return_value = runner.RemoteResponse(
        response(200, b'{"configured":false,"syncLimit":60}')
    )

    with pytest.raises(
        runner.AcceptanceError,
        match="not configured",
    ) as exc_info:
        runner.verify_runtime_generation_capacity(client, 60)

    assert exc_info.value.stage == "preflight"
