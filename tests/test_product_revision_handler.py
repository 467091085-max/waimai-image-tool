from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image, ImageDraw

import object_storage_service
import worker.product_revision_handler as revision_handler
from background_compositor import outside_mask_pixels_equal
from image_edit_provider import ImageEditProviderError, ImageEditResult
from refinement_pipeline import derive_locked_foreground_mask
from shared.refinement_contract import freeze_revision_batch_contract
from worker.product_revision_handler import (
    NonRetryableProductRevisionError,
    PRODUCT_REVISION_TASK_TYPE,
    ProductRevisionCancellationRequested,
    handle_product_revision,
)


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _scene() -> tuple[bytes, bytes, bytes]:
    background = Image.new("RGB", (160, 120), color=(228, 218, 196))
    source = background.copy()
    edited = background.copy()
    ImageDraw.Draw(source).ellipse(
        (42, 30, 118, 104),
        fill=(190, 45, 35),
    )
    ImageDraw.Draw(edited).ellipse(
        (42, 30, 118, 104),
        fill=(52, 145, 72),
    )
    return (
        _png_bytes(background),
        _png_bytes(source),
        _png_bytes(edited),
    )


def _contract(
    storage: object_storage_service.ObjectStorageService,
    *,
    mode: str = "refine",
) -> tuple[dict, bytes, bytes, bytes]:
    background, source, edited = _scene()
    source_key = "generated/delivery/parent-job/source.png"
    background_key = "generated/selected-backgrounds/bg-1/image.png"
    storage.put_bytes(source, object_key=source_key)
    storage.put_bytes(background, object_key=background_key)
    contract = freeze_revision_batch_contract(
        job_id="revision-job-1",
        parent_generation_job_id="parent-job-1",
        user_id="server-user",
        source_delivery_asset={
            "assetId": "source-asset-1",
            "objectKey": source_key,
            "sha256": hashlib.sha256(source).hexdigest(),
            "rowNumber": 7,
            "dishName": "番茄炒蛋",
        },
        selected_background={
            "assetId": "background-asset-1",
            "objectKey": background_key,
            "sha256": hashlib.sha256(background).hexdigest(),
        },
        quality="standard",
        mode=mode,
        refine_prompt="减少葱花并换成白色餐盘" if mode == "refine" else None,
        idempotency_key="revision-browser-key",
        created_at="2026-07-30T12:00:00Z",
    )
    return contract, background, source, edited


class FakeProvider:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.calls: list[dict] = []

    def edit(
        self,
        source_bytes: bytes,
        prompt: str,
        *,
        source_mime_type: str,
    ) -> ImageEditResult:
        self.calls.append(
            {
                "source": source_bytes,
                "prompt": prompt,
                "mimeType": source_mime_type,
            }
        )
        return ImageEditResult(
            image_bytes=self.output,
            mime_type="image/png",
            provider="fake-gemini",
            model="fake-image-edit",
            request_id="provider-request-1",
        )


def _payload(contract: dict, **extra) -> dict:
    return {
        "taskType": PRODUCT_REVISION_TASK_TYPE,
        "revisionContract": contract,
        **extra,
    }


def test_refinement_persists_locked_png_and_private_manifest_pointer(
    tmp_path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, background_raw, source_raw, edited_raw = _contract(storage)
    provider = FakeProvider(edited_raw)
    guard_calls = []

    result = handle_product_revision(
        _payload(
            contract,
            _executionGuard=lambda: guard_calls.append("checked"),
        ),
        provider=provider,
        storage=storage,
    )

    assert guard_calls == ["checked", "checked", "checked"]
    assert len(provider.calls) == 1
    assert provider.calls[0]["source"] == source_raw
    assert provider.calls[0]["mimeType"] == "image/png"
    assert "减少葱花并换成白色餐盘" in provider.calls[0]["prompt"]
    assert result["task_type"] == PRODUCT_REVISION_TASK_TYPE
    assert result["job_id"] == contract["jobId"]
    assert result["request_sha256"] == contract["idempotency"]["requestSha256"]
    assert result["image_url"] == "/api/image-refinements/revision-job-1/asset"
    assert result["manifest_object_key"].endswith("/manifest.json")
    assert set(result) == {
        "image_url",
        "manifest_object_key",
        "manifest_sha256",
        "manifest_size",
        "request_sha256",
        "job_id",
        "task_type",
    }
    assert "source.png" not in str(result)
    assert "selected-backgrounds" not in str(result)
    assert str(result).count("object_key") == 1

    asset_key = result["manifest_object_key"].removesuffix(
        "manifest.json"
    ) + "revision.png"
    final_raw = storage.read_bytes(asset_key)
    assert final_raw.startswith(b"\x89PNG\r\n\x1a\n")
    with (
        Image.open(io.BytesIO(source_raw)) as source,
        Image.open(io.BytesIO(background_raw)) as background,
        Image.open(io.BytesIO(final_raw)) as final,
    ):
        mask, normalized_background, _ = derive_locked_foreground_mask(
            source,
            background,
        )
        assert outside_mask_pixels_equal(
            normalized_background,
            final.convert("RGBA"),
            mask,
        )
        assert final.getpixel((80, 65))[:3] == (52, 145, 72)


def test_revision_manifest_rejects_oversized_provider_metadata_before_encoding(
    tmp_path,
    monkeypatch,
) -> None:
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    contract, _background, _source, edited = _contract(storage)

    class OversizedMetadataProvider(FakeProvider):
        def edit(
            self,
            source_bytes: bytes,
            prompt: str,
            *,
            source_mime_type: str,
        ) -> ImageEditResult:
            result = super().edit(
                source_bytes,
                prompt,
                source_mime_type=source_mime_type,
            )
            return ImageEditResult(
                image_bytes=result.image_bytes,
                mime_type=result.mime_type,
                provider=result.provider,
                model=result.model,
                request_id="x" * 2048,
            )

    monkeypatch.setattr(
        revision_handler,
        "MAX_REVISION_MANIFEST_BYTES",
        512,
    )
    manifest_key = revision_handler._deterministic_keys(contract)[1]
    with mock.patch.object(
        revision_handler,
        "canonical_json",
        side_effect=AssertionError(
            "oversized manifest must not be encoded"
        ),
    ) as canonical:
        with pytest.raises(
            NonRetryableProductRevisionError,
            match="manifest exceeds size limit",
        ):
            handle_product_revision(
                _payload(contract),
                provider=OversizedMetadataProvider(edited),
                storage=storage,
            )

    canonical.assert_not_called()
    assert not storage.exists(manifest_key)
    revision_prefix = (
        "generated/revisions/revision-job-1/"
        f"{contract['idempotency']['requestSha256']}"
    )
    assert storage.list_prefix(revision_prefix) == []


def test_rejects_tampered_contract_before_provider_call(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, edited = _contract(storage)
    contract["refinePrompt"] = "攻击者篡改的提示词"
    provider = FakeProvider(edited)

    with pytest.raises(
        NonRetryableProductRevisionError,
        match="request digest mismatch",
    ):
        handle_product_revision(
            _payload(contract),
            provider=provider,
            storage=storage,
        )

    assert provider.calls == []


def test_revision_rejects_oversized_source_before_body_read(
    tmp_path,
    monkeypatch,
) -> None:
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    contract, _background, _source, edited = _contract(storage)
    read_bytes = mock.Mock(
        side_effect=AssertionError("oversized object must not be read")
    )
    monkeypatch.setattr(storage, "read_bytes", read_bytes)
    monkeypatch.setattr(revision_handler, "MAX_REVISION_IMAGE_BYTES", 8)
    provider = FakeProvider(edited)

    with pytest.raises(
        NonRetryableProductRevisionError,
        match="object exceeds size limit",
    ):
        handle_product_revision(
            _payload(contract),
            provider=provider,
            storage=storage,
        )

    read_bytes.assert_not_called()
    assert provider.calls == []


def test_revision_decode_rejects_pixel_bomb_before_load() -> None:
    image = mock.MagicMock()
    image.__enter__.return_value = image
    image.size = (100_000, 100_000)
    image.format = "PNG"

    with mock.patch.object(
        revision_handler.Image,
        "open",
        return_value=image,
    ):
        with pytest.raises(
            NonRetryableProductRevisionError,
            match="image is invalid",
        ):
            revision_handler._decode_image(
                b"small-image-header",
                "providerOutput",
            )

    image.load.assert_not_called()


def test_rejects_wrong_task_type_after_digest_validation(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, edited = _contract(storage)

    with pytest.raises(
        NonRetryableProductRevisionError,
        match="task type mismatch",
    ):
        handle_product_revision(
            {
                "taskType": "prompt_generation",
                "revisionContract": contract,
            },
            provider=FakeProvider(edited),
            storage=storage,
        )


@pytest.mark.parametrize(
    "snapshot_name",
    ["sourceDeliveryAsset", "selectedBackground"],
)
def test_rejects_tampered_private_input_object(
    tmp_path,
    snapshot_name: str,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, edited = _contract(storage)
    object_key = contract[snapshot_name]["objectKey"]
    storage.put_bytes(b"tampered-object", object_key=object_key)
    provider = FakeProvider(edited)

    with pytest.raises(
        NonRetryableProductRevisionError,
        match="object digest mismatch",
    ):
        handle_product_revision(
            _payload(contract),
            provider=provider,
            storage=storage,
        )

    assert provider.calls == []


def test_completed_manifest_retry_does_not_call_paid_provider_again(
    tmp_path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, edited = _contract(storage)
    first_provider = FakeProvider(edited)

    first = handle_product_revision(
        _payload(contract),
        provider=first_provider,
        storage=storage,
    )
    retry_provider = SimpleNamespace(
        edit=lambda *_args, **_kwargs: pytest.fail(
            "paid provider must not run for a completed retry"
        )
    )
    second = handle_product_revision(
        _payload(contract),
        provider=retry_provider,
        storage=storage,
    )

    assert first == second
    assert len(first_provider.calls) == 1


def test_existing_manifest_rejects_tampered_revision_asset(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, edited = _contract(storage)
    first = handle_product_revision(
        _payload(contract),
        provider=FakeProvider(edited),
        storage=storage,
    )
    asset_key = first["manifest_object_key"].removesuffix(
        "manifest.json"
    ) + "revision.png"
    storage.put_bytes(b"tampered", object_key=asset_key)

    with pytest.raises(
        NonRetryableProductRevisionError,
        match="asset digest mismatch",
    ):
        handle_product_revision(
            _payload(contract),
            provider=FakeProvider(edited),
            storage=storage,
        )


def test_provider_failure_is_not_converted_to_fake_success(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, _edited = _contract(storage)

    class FailingProvider:
        def edit(self, *_args, **_kwargs):
            raise ImageEditProviderError(
                "provider_failed",
                "provider failed",
                retryable=True,
            )

    with pytest.raises(ImageEditProviderError, match="provider failed"):
        handle_product_revision(
            _payload(contract),
            provider=FailingProvider(),
            storage=storage,
        )

    revision_prefix = (
        "generated/revisions/revision-job-1/"
        f"{contract['idempotency']['requestSha256']}"
    )
    assert storage.list_prefix(revision_prefix) == []


def test_permanent_provider_failure_is_not_retried(tmp_path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, _edited = _contract(storage)

    class RejectedProvider:
        def edit(self, *_args, **_kwargs):
            raise ImageEditProviderError(
                "provider_rejected",
                "provider rejected the request",
                retryable=False,
            )

    with pytest.raises(
        NonRetryableProductRevisionError,
        match="provider rejected the request",
    ):
        handle_product_revision(
            _payload(contract),
            provider=RejectedProvider(),
            storage=storage,
        )


@pytest.mark.parametrize("cancel_on_call", [1, 2, 3])
def test_execution_guard_can_cancel_at_every_required_boundary(
    tmp_path,
    cancel_on_call: int,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, _source, edited = _contract(storage)
    provider = FakeProvider(edited)
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == cancel_on_call:
            raise ProductRevisionCancellationRequested("cancel requested")

    with pytest.raises(ProductRevisionCancellationRequested):
        handle_product_revision(
            _payload(contract, _executionGuard=guard),
            provider=provider,
            storage=storage,
        )

    assert calls == cancel_on_call
    assert len(provider.calls) == (0 if cancel_on_call == 1 else 1)
    revision_prefix = (
        "generated/revisions/revision-job-1/"
        f"{contract['idempotency']['requestSha256']}"
    )
    assert storage.list_prefix(revision_prefix) == []


def test_rework_uses_server_prompt_and_rejects_unchanged_provider_output(
    tmp_path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    contract, _background, source, edited = _contract(storage, mode="rework")
    provider = FakeProvider(edited)

    handle_product_revision(
        _payload(contract),
        provider=provider,
        storage=storage,
    )

    prompt = provider.calls[0]["prompt"]
    assert "番茄炒蛋" in prompt
    assert "Recompose and re-plate" in prompt
    assert "dish name" in prompt

    other_storage = object_storage_service.ObjectStorageService(
        tmp_path / "other-objects"
    )
    other_contract, _background, other_source, _edited = _contract(
        other_storage,
        mode="rework",
    )
    with pytest.raises(RuntimeError, match="unchanged source image"):
        handle_product_revision(
            _payload(other_contract),
            provider=FakeProvider(other_source),
            storage=other_storage,
        )
