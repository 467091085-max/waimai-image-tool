from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

import pytest
import requests


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
