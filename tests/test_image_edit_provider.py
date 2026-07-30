from __future__ import annotations

import base64
import io
import json
import urllib.error
from unittest import mock

import pytest
from PIL import Image

import image_edit_provider as provider_module
from image_edit_provider import (
    GeminiImageEditProvider,
    ImageEditProviderError,
    gemini_image_edit_readiness,
)


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 18), color=(220, 48, 38)).save(output, "PNG")
    return output.getvalue()


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


def test_readiness_fails_closed_without_key() -> None:
    status = gemini_image_edit_readiness({})

    assert status["ready"] is False
    assert status["missingConfig"] == ["GEMINI_API_KEY"]
    assert "gemini_image_edit_api_key_required" in status["blockingIssues"]


def test_provider_sends_source_and_returns_last_inline_image() -> None:
    source = image_bytes()
    output = image_bytes()
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "id": "interaction-1",
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {"type": "text", "text": "done"},
                            {
                                "type": "image",
                                "mime_type": "image/png",
                                "data": base64.b64encode(output).decode("ascii"),
                            },
                        ],
                    }
                ],
            }
        )

    result = GeminiImageEditProvider(
        api_key="test-secret",
        opener=opener,
    ).edit(source, "把香菜去掉")

    assert result.image_bytes == output
    assert result.request_id == "interaction-1"
    assert result.model == "gemini-3.1-flash-image"
    assert captured["url"].endswith("/v1beta/interactions")
    assert "test-secret" not in captured["url"]
    assert "test-secret" not in json.dumps(captured["payload"])
    assert captured["payload"]["input"][1]["data"] == base64.b64encode(source).decode("ascii")
    assert "把香菜去掉" in captured["payload"]["input"][0]["text"]
    assert captured["payload"]["response_format"]["type"] == "image"


def test_provider_parses_generate_content_inline_data_compatibility() -> None:
    output = image_bytes()

    provider = GeminiImageEditProvider(
        api_key="test-secret",
        opener=lambda *_args, **_kwargs: FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(output).decode("ascii"),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ),
    )

    assert provider.edit(image_bytes(), "换成白色餐盘").image_bytes == output


def test_provider_rejects_missing_image_and_invalid_source() -> None:
    provider = GeminiImageEditProvider(
        api_key="test-secret",
        opener=lambda *_args, **_kwargs: FakeResponse(
            {"id": "interaction-1", "status": "completed", "steps": []}
        ),
    )

    with pytest.raises(ImageEditProviderError) as missing:
        provider.edit(image_bytes(), "减少辣椒")
    assert missing.value.code == "gemini_image_edit_no_image"

    with pytest.raises(ImageEditProviderError) as invalid:
        provider.edit(b"not-an-image", "减少辣椒")
    assert invalid.value.code == "invalid_refinement_source"


def test_provider_marks_rate_limit_retryable() -> None:
    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://example.invalid",
            429,
            "rate limited",
            {},
            io.BytesIO(
                json.dumps(
                    {"error": {"message": "quota exhausted"}}
                ).encode("utf-8")
            ),
        )

    provider = GeminiImageEditProvider(
        api_key="test-secret",
        opener=opener,
    )
    with pytest.raises(ImageEditProviderError) as raised:
        provider.edit(image_bytes(), "换一个摆盘")

    assert raised.value.code == "gemini_image_edit_http_error"
    assert raised.value.retryable is True


def test_provider_caps_http_response_before_json_or_base64_decode(
    monkeypatch,
) -> None:
    class OversizedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size: int = -1) -> bytes:
            assert size == 9
            return b"x" * 9

    monkeypatch.setattr(provider_module, "MAX_PROVIDER_RESPONSE_BYTES", 8)
    provider = GeminiImageEditProvider(
        api_key="test-secret",
        opener=lambda *_args, **_kwargs: OversizedResponse(),
    )

    with pytest.raises(ImageEditProviderError) as raised:
        provider.edit(image_bytes(), "换一个摆盘")

    assert raised.value.code == "gemini_image_edit_response_too_large"
    assert raised.value.retryable is True


def test_image_validation_rejects_pixel_bomb_before_verify() -> None:
    image = mock.MagicMock()
    image.__enter__.return_value = image
    image.size = (100_000, 100_000)
    image.format = "PNG"

    with mock.patch.object(
        provider_module.Image,
        "open",
        return_value=image,
    ):
        with pytest.raises(ImageEditProviderError) as raised:
            provider_module._validated_image_bytes(
                b"small-image-header",
                max_bytes=1024,
                code="invalid_image",
            )

    assert raised.value.code == "invalid_image"
    image.verify.assert_not_called()
