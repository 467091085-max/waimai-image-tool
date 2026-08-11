from __future__ import annotations

import hashlib
import hmac
import io
import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageChops, ImageOps

import object_storage_service
from background_compositor import outside_mask_pixels_equal
from image_edit_provider import GeminiImageEditProvider, ImageEditProviderError
from refinement_pipeline import (
    compose_locked_refinement,
    derive_locked_foreground_mask,
)
from shared.batch_contract import canonical_json
from shared.json_limits import (
    InvalidJsonValue,
    JsonSizeLimitExceeded,
    validate_json_size,
)
from shared.refinement_contract import JOB_TYPE, revision_request_sha256
from worker.product_batch_handler import (
    NonRetryableProductBatchError,
    ProductBatchCancellationRequested,
)


PRODUCT_REVISION_TASK_TYPE = "product_revision"
REVISION_MANIFEST_SCHEMA_VERSION = 1
MAX_REVISION_IMAGE_BYTES = 30 * 1024 * 1024
MAX_REVISION_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_REVISION_IMAGE_PIXELS = 24_000_000
MAX_REVISION_IMAGE_SIDE = 12_000


class NonRetryableProductRevisionError(NonRetryableProductBatchError):
    pass


class ProductRevisionCancellationRequested(ProductBatchCancellationRequested):
    pass


def handle_product_revision(
    payload: Mapping[str, Any],
    *,
    provider: Any | None = None,
    storage: Any | None = None,
) -> Mapping[str, Any]:
    contract = _validated_contract(payload)
    execution_guard = payload.get("_executionGuard")
    if execution_guard is not None and not callable(execution_guard):
        raise NonRetryableProductRevisionError(
            "product revision execution guard is invalid"
        )

    object_store = storage or object_storage_service.get_object_storage_service()
    asset_key, manifest_key = _deterministic_keys(contract)
    completed = _completed_manifest_result(
        object_store,
        contract,
        asset_key=asset_key,
        manifest_key=manifest_key,
    )
    if completed is not None:
        return completed

    source_snapshot = _required_mapping(
        contract.get("sourceDeliveryAsset"),
        "sourceDeliveryAsset",
    )
    background_snapshot = _required_mapping(
        contract.get("selectedBackground"),
        "selectedBackground",
    )
    source_bytes = _read_verified_object(
        object_store,
        source_snapshot,
        "sourceDeliveryAsset",
    )
    background_bytes = _read_verified_object(
        object_store,
        background_snapshot,
        "selectedBackground",
    )
    source_image, source_mime_type = _decode_image(
        source_bytes,
        "sourceDeliveryAsset",
    )
    background_image, _ = _decode_image(
        background_bytes,
        "selectedBackground",
    )

    _run_execution_guard(execution_guard)
    editor = provider or GeminiImageEditProvider.from_env()
    prompt = _provider_prompt(contract)
    try:
        provider_result = editor.edit(
            source_bytes,
            prompt,
            source_mime_type=source_mime_type,
        )
    except ImageEditProviderError as exc:
        if not exc.retryable:
            raise NonRetryableProductRevisionError(str(exc)) from exc
        raise
    _run_execution_guard(execution_guard)

    edited_bytes = getattr(provider_result, "image_bytes", None)
    if not isinstance(edited_bytes, bytes):
        raise RuntimeError("image edit provider returned no image bytes")
    edited_image, _ = _decode_image(edited_bytes, "providerOutput")
    if edited_image.size == source_image.size and _pixels_equal(
        source_image,
        edited_image,
    ):
        raise RuntimeError("image edit provider returned the unchanged source image")

    composition = compose_locked_refinement(
        source_image,
        background_image,
        edited_image,
    )
    final_bytes = composition.png_bytes()
    final_image, _ = _decode_image(
        final_bytes,
        "revisionOutput",
        require_png=True,
    )
    if _pixels_equal(source_image, final_image):
        raise RuntimeError("product revision produced no visible image change")

    request_sha256 = str(contract["idempotency"]["requestSha256"])
    expected_asset = {
        "sha256": hashlib.sha256(final_bytes).hexdigest(),
        "size": len(final_bytes),
    }
    manifest_document = {
        "schemaVersion": REVISION_MANIFEST_SCHEMA_VERSION,
        "taskType": PRODUCT_REVISION_TASK_TYPE,
        "jobType": JOB_TYPE,
        "jobId": str(contract["jobId"]),
        "parentGenerationJobId": str(contract["parentGenerationJobId"]),
        "requestSha256": request_sha256,
        "mode": str(contract["mode"]),
        "sourceDeliveryAsset": {
            "assetId": str(source_snapshot["assetId"]),
            "objectKey": str(source_snapshot["objectKey"]),
            "sha256": str(source_snapshot["sha256"]),
        },
        "selectedBackground": {
            "assetId": str(background_snapshot["assetId"]),
            "objectKey": str(background_snapshot["objectKey"]),
            "sha256": str(background_snapshot["sha256"]),
        },
        "imageAsset": {
            "assetId": f"revision_{request_sha256[:32]}",
            "objectKey": asset_key,
            "sha256": expected_asset["sha256"],
            "size": expected_asset["size"],
            "mimeType": "image/png",
        },
        "provider": {
            "name": str(getattr(provider_result, "provider", "") or ""),
            "model": str(getattr(provider_result, "model", "") or ""),
            "requestId": str(getattr(provider_result, "request_id", "") or ""),
        },
        "composition": dict(composition.metadata),
    }
    try:
        validate_json_size(
            manifest_document,
            MAX_REVISION_MANIFEST_BYTES,
        )
        candidate_manifest = canonical_json(
            manifest_document
        ).encode("utf-8")
    except (
        InvalidJsonValue,
        JsonSizeLimitExceeded,
        TypeError,
        UnicodeEncodeError,
    ) as exc:
        raise NonRetryableProductRevisionError(
            "product revision manifest exceeds size limit"
        ) from exc
    if len(candidate_manifest) > MAX_REVISION_MANIFEST_BYTES:
        raise NonRetryableProductRevisionError(
            "product revision manifest exceeds size limit"
        )

    _run_execution_guard(execution_guard)
    _put_if_absent(object_store, asset_key, final_bytes)
    persisted_asset = _read_and_validate_locked_asset(
        object_store,
        asset_key,
        source_image=source_image,
        background_image=background_image,
    )
    if (
        persisted_asset["size"] != expected_asset["size"]
        or not hmac.compare_digest(
            str(persisted_asset["sha256"]),
            str(expected_asset["sha256"]),
        )
    ):
        raise NonRetryableProductRevisionError(
            "persisted product revision integrity mismatch"
        )
    _put_if_absent(object_store, manifest_key, candidate_manifest)

    completed = _completed_manifest_result(
        object_store,
        contract,
        asset_key=asset_key,
        manifest_key=manifest_key,
    )
    if completed is None:
        raise RuntimeError("product revision manifest was not persisted")
    return completed


def _validated_contract(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise NonRetryableProductRevisionError(
            "product revision payload must be an object"
        )
    contract = payload.get("revisionContract")
    if not isinstance(contract, Mapping):
        raise NonRetryableProductRevisionError(
            "product revision contract is required"
        )
    idempotency = contract.get("idempotency")
    expected_sha256 = str(
        idempotency.get("requestSha256") or ""
        if isinstance(idempotency, Mapping)
        else ""
    )
    try:
        calculated_sha256 = revision_request_sha256(contract)
    except Exception as exc:
        raise NonRetryableProductRevisionError(
            "product revision contract cannot be digested"
        ) from exc
    if (
        len(expected_sha256) != 64
        or not hmac.compare_digest(calculated_sha256, expected_sha256)
    ):
        raise NonRetryableProductRevisionError(
            "product revision request digest mismatch"
        )
    if str(payload.get("taskType") or "") != PRODUCT_REVISION_TASK_TYPE:
        raise NonRetryableProductRevisionError(
            "product revision task type mismatch"
        )
    if str(contract.get("jobType") or "") != JOB_TYPE:
        raise NonRetryableProductRevisionError(
            "product revision contract job type mismatch"
        )
    if not str(contract.get("jobId") or "").strip():
        raise NonRetryableProductRevisionError(
            "product revision contract job ID is required"
        )
    if str(contract.get("mode") or "") not in {"refine", "rework"}:
        raise NonRetryableProductRevisionError(
            "product revision contract mode is invalid"
        )
    _validated_contract_asset(
        contract.get("sourceDeliveryAsset"),
        "sourceDeliveryAsset",
    )
    _validated_contract_asset(
        contract.get("selectedBackground"),
        "selectedBackground",
    )
    return contract


def _validated_contract_asset(value: Any, label: str) -> None:
    snapshot = _required_mapping(value, label)
    try:
        object_storage_service.validate_object_key(
            str(snapshot.get("objectKey") or "")
        )
    except (TypeError, ValueError) as exc:
        raise NonRetryableProductRevisionError(
            f"{label} object key is invalid"
        ) from exc
    digest = str(snapshot.get("sha256") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise NonRetryableProductRevisionError(
            f"{label} SHA-256 is invalid"
        )


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NonRetryableProductRevisionError(f"{label} must be an object")
    return value


def _deterministic_keys(
    contract: Mapping[str, Any],
) -> tuple[str, str]:
    job_id = str(contract["jobId"])
    request_sha256 = str(contract["idempotency"]["requestSha256"])
    prefix = f"{object_storage_service.GENERATED_PREFIX}revisions/{job_id}/{request_sha256}"
    return (
        object_storage_service.validate_object_key(f"{prefix}/revision.png"),
        object_storage_service.validate_object_key(f"{prefix}/manifest.json"),
    )


def _read_verified_object(
    storage: Any,
    snapshot: Mapping[str, Any],
    label: str,
) -> bytes:
    object_key = object_storage_service.validate_object_key(
        str(snapshot["objectKey"])
    )
    expected_sha256 = str(snapshot["sha256"])
    try:
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            MAX_REVISION_IMAGE_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise NonRetryableProductRevisionError(
            f"{label} object exceeds size limit"
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        raise NonRetryableProductRevisionError(
            f"{label} object is unavailable"
        ) from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise NonRetryableProductRevisionError(
            f"{label} object digest mismatch"
        )
    return raw


def _provider_prompt(contract: Mapping[str, Any]) -> str:
    source = _required_mapping(
        contract.get("sourceDeliveryAsset"),
        "sourceDeliveryAsset",
    )
    dish_name = " ".join(str(source.get("dishName") or "").split())
    if str(contract["mode"]) == "rework":
        return (
            f'Recompose and re-plate the dish named "{dish_name}" with a '
            "clearly different, appetizing commercial arrangement. Keep the "
            "dish name, primary ingredients, portion identity, and food type "
            "accurate. Change the food or plating rather than the background."
        )
    refine_prompt = " ".join(str(contract.get("refinePrompt") or "").split())
    return (
        f'For the dish named "{dish_name}", apply this requested food or '
        f"plating edit exactly: {refine_prompt}. Keep the dish identity and "
        "primary ingredients accurate, and do not change the background."
    )


def _run_execution_guard(guard: Any) -> None:
    if guard is not None:
        guard()


def _decode_image(
    raw: bytes,
    label: str,
    *,
    require_png: bool = False,
) -> tuple[Image.Image, str]:
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > MAX_REVISION_IMAGE_BYTES
    ):
        raise NonRetryableProductRevisionError(f"{label} image is invalid")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_REVISION_IMAGE_SIDE
                or height > MAX_REVISION_IMAGE_SIDE
                or width * height > MAX_REVISION_IMAGE_PIXELS
            ):
                raise ValueError("image dimensions exceed limit")
            image.load()
            decoded = ImageOps.exif_transpose(image).convert("RGBA")
    except Exception as exc:
        raise NonRetryableProductRevisionError(
            f"{label} image is invalid"
        ) from exc
    if require_png and image_format != "PNG":
        raise NonRetryableProductRevisionError(
            f"{label} must be a canonical PNG"
        )
    mime_types = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }
    mime_type = mime_types.get(image_format)
    if mime_type is None:
        raise NonRetryableProductRevisionError(
            f"{label} image format is unsupported"
        )
    return decoded, mime_type


def _pixels_equal(left: Image.Image, right: Image.Image) -> bool:
    clean_left = ImageOps.exif_transpose(left).convert("RGBA")
    clean_right = ImageOps.exif_transpose(right).convert("RGBA")
    if clean_left.size != clean_right.size:
        return False
    extrema = ImageChops.difference(clean_left, clean_right).getextrema()
    return all(low == 0 and high == 0 for low, high in extrema)


def _read_and_validate_locked_asset(
    storage: Any,
    object_key: str,
    *,
    source_image: Image.Image,
    background_image: Image.Image,
) -> dict[str, Any]:
    try:
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            MAX_REVISION_IMAGE_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise NonRetryableProductRevisionError(
            "product revision asset exceeds size limit"
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("product revision asset was not persisted") from exc
    output_image, _ = _decode_image(
        raw,
        "persistedRevision",
        require_png=True,
    )
    if output_image.size != source_image.size:
        raise NonRetryableProductRevisionError(
            "persisted product revision size mismatch"
        )
    mask, normalized_background, _ = derive_locked_foreground_mask(
        source_image,
        background_image,
    )
    if not outside_mask_pixels_equal(
        normalized_background,
        output_image,
        mask,
    ):
        raise NonRetryableProductRevisionError(
            "persisted product revision changed the locked background"
        )
    if _pixels_equal(source_image, output_image):
        raise NonRetryableProductRevisionError(
            "persisted product revision contains no visible change"
        )
    return {
        "raw": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }


def _put_if_absent(storage: Any, object_key: str, raw: bytes) -> bool:
    object_key = object_storage_service.validate_object_key(object_key)
    if storage.exists(object_key):
        return False

    custom_put = getattr(storage, "put_bytes_if_absent", None)
    if callable(custom_put):
        return bool(custom_put(object_key, raw))

    try:
        target = storage.path_for_key(object_key)
    except (AttributeError, NotImplementedError):
        target = None
    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output:
                output.write(raw)
        except FileExistsError:
            return False
        return True

    client = getattr(storage, "client", None)
    bucket = getattr(storage, "bucket", None)
    remote_key = getattr(storage, "remote_key", None)
    if client is None or not bucket or not callable(remote_key):
        raise RuntimeError(
            "object storage backend does not support atomic first-writer writes"
        )
    try:
        client.put_object(
            Bucket=bucket,
            Body=raw,
            Key=remote_key(object_key),
            ContentType=object_storage_service.content_type_for_key(object_key),
            IfNoneMatch="*",
        )
    except Exception:
        if storage.exists(object_key):
            return False
        raise
    return True


def _completed_manifest_result(
    storage: Any,
    contract: Mapping[str, Any],
    *,
    asset_key: str,
    manifest_key: str,
) -> dict[str, Any] | None:
    if not storage.exists(manifest_key):
        return None
    try:
        raw_manifest = object_storage_service.read_object_bytes_limited(
            storage,
            manifest_key,
            MAX_REVISION_MANIFEST_BYTES,
        )
        document = json.loads(raw_manifest.decode("utf-8"))
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        object_storage_service.ObjectStorageReadLimitExceeded,
    ) as exc:
        raise NonRetryableProductRevisionError(
            "product revision manifest is unreadable"
        ) from exc
    if not isinstance(document, dict):
        raise NonRetryableProductRevisionError(
            "product revision manifest must be an object"
        )
    if canonical_json(document).encode("utf-8") != raw_manifest:
        raise NonRetryableProductRevisionError(
            "product revision manifest is not canonical"
        )

    expected_request_sha256 = str(contract["idempotency"]["requestSha256"])
    actual_request_sha256 = str(document.get("requestSha256") or "")
    if not hmac.compare_digest(actual_request_sha256, expected_request_sha256):
        raise NonRetryableProductRevisionError(
            "product revision manifest request digest mismatch"
        )
    expected_scalars = {
        "schemaVersion": REVISION_MANIFEST_SCHEMA_VERSION,
        "taskType": PRODUCT_REVISION_TASK_TYPE,
        "jobType": JOB_TYPE,
        "jobId": str(contract["jobId"]),
        "parentGenerationJobId": str(contract["parentGenerationJobId"]),
        "mode": str(contract["mode"]),
    }
    if any(document.get(key) != value for key, value in expected_scalars.items()):
        raise NonRetryableProductRevisionError(
            "product revision manifest contract mismatch"
        )
    _validate_manifest_snapshot(
        document.get("sourceDeliveryAsset"),
        contract.get("sourceDeliveryAsset"),
        "sourceDeliveryAsset",
    )
    _validate_manifest_snapshot(
        document.get("selectedBackground"),
        contract.get("selectedBackground"),
        "selectedBackground",
    )

    asset = _required_mapping(document.get("imageAsset"), "imageAsset")
    if str(asset.get("objectKey") or "") != asset_key:
        raise NonRetryableProductRevisionError(
            "product revision manifest asset key mismatch"
        )
    expected_asset_sha256 = str(asset.get("sha256") or "")
    expected_asset_size = asset.get("size")
    if (
        len(expected_asset_sha256) != 64
        or isinstance(expected_asset_size, bool)
        or not isinstance(expected_asset_size, int)
        or expected_asset_size <= 0
        or expected_asset_size > MAX_REVISION_IMAGE_BYTES
        or asset.get("mimeType") != "image/png"
    ):
        raise NonRetryableProductRevisionError(
            "product revision manifest asset metadata is invalid"
        )
    try:
        raw_asset = object_storage_service.read_object_bytes_limited(
            storage,
            asset_key,
            MAX_REVISION_IMAGE_BYTES,
        )
    except object_storage_service.ObjectStorageReadLimitExceeded as exc:
        raise NonRetryableProductRevisionError(
            "product revision manifest asset exceeds size limit"
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        raise NonRetryableProductRevisionError(
            "product revision manifest asset is unavailable"
        ) from exc
    actual_asset_sha256 = hashlib.sha256(raw_asset).hexdigest()
    if (
        len(raw_asset) != expected_asset_size
        or not hmac.compare_digest(actual_asset_sha256, expected_asset_sha256)
    ):
        raise NonRetryableProductRevisionError(
            "product revision manifest asset digest mismatch"
        )
    source_snapshot = _required_mapping(
        contract.get("sourceDeliveryAsset"),
        "sourceDeliveryAsset",
    )
    background_snapshot = _required_mapping(
        contract.get("selectedBackground"),
        "selectedBackground",
    )
    source_bytes = _read_verified_object(
        storage,
        source_snapshot,
        "sourceDeliveryAsset",
    )
    background_bytes = _read_verified_object(
        storage,
        background_snapshot,
        "selectedBackground",
    )
    source_image, _ = _decode_image(source_bytes, "sourceDeliveryAsset")
    background_image, _ = _decode_image(
        background_bytes,
        "selectedBackground",
    )
    verified_asset = _read_and_validate_locked_asset(
        storage,
        asset_key,
        source_image=source_image,
        background_image=background_image,
    )
    if (
        verified_asset["size"] != expected_asset_size
        or not hmac.compare_digest(
            str(verified_asset["sha256"]),
            expected_asset_sha256,
        )
    ):
        raise NonRetryableProductRevisionError(
            "product revision manifest asset verification mismatch"
        )

    return {
        "image_url": f"/api/image-refinements/{contract['jobId']}/asset",
        "manifest_object_key": manifest_key,
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "manifest_size": len(raw_manifest),
        "request_sha256": expected_request_sha256,
        "job_id": str(contract["jobId"]),
        "task_type": PRODUCT_REVISION_TASK_TYPE,
    }


def _validate_manifest_snapshot(
    actual_value: Any,
    expected_value: Any,
    label: str,
) -> None:
    actual = _required_mapping(actual_value, f"manifest.{label}")
    expected = _required_mapping(expected_value, label)
    for key in ("assetId", "objectKey", "sha256"):
        if str(actual.get(key) or "") != str(expected.get(key) or ""):
            raise NonRetryableProductRevisionError(
                f"product revision manifest {label} mismatch"
            )


__all__ = [
    "NonRetryableProductRevisionError",
    "PRODUCT_REVISION_TASK_TYPE",
    "ProductRevisionCancellationRequested",
    "handle_product_revision",
]
