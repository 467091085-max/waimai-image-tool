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


def sized_image_bytes(size: tuple[int, int], *, image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=(220, 48, 38)).save(output, image_format)
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
    assert captured["payload"]["store"] is False
    assert captured["payload"]["input"][1]["data"] == base64.b64encode(source).decode("ascii")
    assert "把香菜去掉" in captured["payload"]["input"][0]["text"]
    assert captured["payload"]["response_format"]["type"] == "image"
    assert captured["payload"]["response_format"]["aspect_ratio"] == "4:3"
    assert captured["payload"]["response_format"]["image_size"] == "1K"


def test_provider_normalizes_same_ratio_output_to_exact_source_canvas() -> None:
    source = sized_image_bytes((800, 600))
    provider_output = sized_image_bytes((1024, 768))
    provider = GeminiImageEditProvider(
        api_key="test-secret",
        opener=lambda *_args, **_kwargs: FakeResponse(
            {
                "id": "interaction-normalized",
                "steps": [
                    {
                        "content": [
                            {
                                "type": "image",
                                "mime_type": "image/png",
                                "data": base64.b64encode(provider_output).decode("ascii"),
                            }
                        ]
                    }
                ],
            }
        ),
    )

    result = provider.edit(source, "增强菜品光泽")

    with Image.open(io.BytesIO(result.image_bytes)) as image:
        assert image.size == (800, 600)
    assert result.source_size == (800, 600)
    assert result.provider_size == (1024, 768)
    assert result.normalized_to_source is True
    assert result.mime_type == "image/jpeg"


def test_provider_rejects_output_with_different_aspect_ratio() -> None:
    source = sized_image_bytes((800, 600))
    square_output = sized_image_bytes((1024, 1024))
    provider = GeminiImageEditProvider(
        api_key="test-secret",
        opener=lambda *_args, **_kwargs: FakeResponse(
            {
                "steps": [
                    {
                        "content": [
                            {
                                "type": "image",
                                "mime_type": "image/png",
                                "data": base64.b64encode(square_output).decode("ascii"),
                            }
                        ]
                    }
                ]
            }
        ),
    )

    with pytest.raises(ImageEditProviderError) as raised:
        provider.edit(source, "增强菜品光泽")

    assert raised.value.code == "gemini_image_edit_output_aspect_ratio_mismatch"
    assert raised.value.retryable is False


@pytest.mark.parametrize("timeout", [0, 0.5, 901, float("nan"), float("inf")])
def test_provider_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ImageEditProviderError) as raised:
        GeminiImageEditProvider(
            api_key="test-secret",
            timeout_seconds=timeout,
        )

    assert raised.value.code == "gemini_image_edit_timeout_invalid"


def test_provider_rejects_non_google_interactions_endpoint() -> None:
    status = gemini_image_edit_readiness(
        {
            "GEMINI_API_KEY": "test-secret",
            "GEMINI_INTERACTIONS_URL": "https://gemini-proxy.example/v1beta/interactions",
        }
    )

    assert status["ready"] is False
    assert "gemini_image_edit_endpoint_invalid" in status["blockingIssues"]
    with pytest.raises(ImageEditProviderError) as raised:
        GeminiImageEditProvider(
            api_key="test-secret",
            endpoint="https://gemini-proxy.example/v1beta/interactions",
        )
    assert raised.value.code == "gemini_image_edit_endpoint_invalid"


def test_provider_rejects_malformed_endpoint_port_without_raising() -> None:
    status = gemini_image_edit_readiness(
        {
            "GEMINI_API_KEY": "test-secret",
            "GEMINI_INTERACTIONS_URL": (
                "https://generativelanguage.googleapis.com:not-a-port/"
                "v1beta/interactions"
            ),
        }
    )

    assert status["ready"] is False
    assert "gemini_image_edit_endpoint_invalid" in status["blockingIssues"]


def test_default_transport_refuses_redirect_before_forwarding_headers() -> None:
    request = provider_module.urllib.request.Request(
        provider_module.DEFAULT_GEMINI_INTERACTIONS_URL,
        headers={"x-goog-api-key": "test-secret"},
    )
    handler = provider_module._RejectGeminiRedirects()

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.example/collect",
    )

    assert redirected is None


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
