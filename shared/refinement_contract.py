from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Mapping

import prompt_compiler
from shared.batch_contract import BatchContractError, canonical_json
from shared.contract_attestation import (
    contract_attestation_valid,
    create_contract_attestation,
)
import background_catalog


SCHEMA_VERSION = 2
JOB_TYPE = "delivery_asset_revision_batch"
CONTRACT_ATTESTATION_VERSION = "revision-contract-attestation.v1"
CONTRACT_ATTESTATION_DOMAIN = JOB_TYPE
PRICING_VERSION = "revision-v1"
REWORK_POINTS = {"standard": 10, "premium": 20}
REFINE_POINTS = 10
MAX_DISH_NAME_LENGTH = 160
MAX_REFINE_PROMPT_LENGTH = 500
DEFAULT_PROVIDER_SNAPSHOT = {
    "provider": "google-gemini",
    "model": "gemini-3.1-flash-image",
    "apiSurface": "interactions-v1beta",
    "endpoint": "https://generativelanguage.googleapis.com/v1beta/interactions",
    "promptVersion": "food-refinement.v1",
    "output": {
        "mimeType": "image/jpeg",
        "imageSize": "1K",
        "aspectRatio": "source-nearest-supported",
        "canvasNormalization": "same-as-source.v1",
    },
}

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
OBJECT_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}")
SHA256_RE = re.compile(r"[a-f0-9]{64}")


class RefinementContractError(BatchContractError):
    pass


def freeze_revision_batch_contract(
    *,
    job_id: str,
    parent_generation_job_id: str,
    user_id: str,
    source_delivery_asset: Mapping[str, Any],
    selected_background: Mapping[str, Any],
    quality: str,
    mode: str,
    refine_prompt: str | None,
    idempotency_key: str,
    provider_snapshot: Mapping[str, Any] | None = None,
    free_rework_quota_verified: bool = False,
    debit_order_id: str | None = None,
    refund_order_id: str | None = None,
    pricing_version: str = PRICING_VERSION,
    created_at: str | None = None,
    attestation_secret: str | bytes | None = None,
) -> dict[str, Any]:
    clean_job_id = _clean_id(job_id, "jobId")
    clean_parent_job_id = _clean_id(parent_generation_job_id, "parentGenerationJobId")
    clean_user_id = _clean_id(user_id, "userId")
    clean_source = _source_delivery_asset_snapshot(source_delivery_asset)
    clean_background = _background_snapshot(selected_background)
    clean_quality = _quality(quality)
    clean_mode = _mode(mode)
    clean_prompt = _refine_prompt(refine_prompt, clean_mode)
    clean_provider_snapshot = _provider_snapshot(
        DEFAULT_PROVIDER_SNAPSHOT
        if provider_snapshot is None
        else provider_snapshot
    )
    clean_free_rework = _bool(
        free_rework_quota_verified,
        "billing.freeReworkQuotaVerified",
    )
    if clean_free_rework and clean_mode != "rework":
        raise RefinementContractError(
            "invalid_free_rework",
            "free rework quota can only be applied to rework mode",
            field="billing.freeReworkQuotaVerified",
        )

    clean_pricing_version = _clean_id(pricing_version, "billing.pricingVersion")
    base_points = (
        REWORK_POINTS[clean_quality]
        if clean_mode == "rework"
        else REFINE_POINTS
    )
    total_points = 0 if clean_free_rework else base_points
    clean_debit_order_id = _clean_id(
        f"revision:{clean_job_id}:debit" if debit_order_id is None else debit_order_id,
        "billing.debitOrderId",
    )
    clean_refund_order_id = _clean_id(
        f"revision:{clean_job_id}:refund" if refund_order_id is None else refund_order_id,
        "billing.refundOrderId",
    )
    if clean_debit_order_id == clean_refund_order_id:
        raise RefinementContractError(
            "duplicate_order_id",
            "debit and refund order IDs must be different",
            field="billing.refundOrderId",
        )

    contract = {
        "schemaVersion": SCHEMA_VERSION,
        "jobType": JOB_TYPE,
        "jobId": clean_job_id,
        "parentGenerationJobId": clean_parent_job_id,
        "userId": clean_user_id,
        "sourceDeliveryAsset": clean_source,
        "selectedBackground": clean_background,
        "quality": {
            "id": clean_quality,
        },
        "mode": clean_mode,
        "refinePrompt": clean_prompt,
        "providerSnapshot": clean_provider_snapshot,
        "billing": {
            "snapshotVersion": 1,
            "pricingVersion": clean_pricing_version,
            "basePoints": base_points,
            "totalPoints": total_points,
            "freeReworkQuotaVerified": clean_free_rework,
            "debitOrderId": clean_debit_order_id,
            "refundOrderId": clean_refund_order_id,
            "state": "pending_debit",
        },
        "idempotency": {
            "key": _clean_id(idempotency_key, "idempotency.key"),
            "requestSha256": "",
        },
        "createdAt": _created_at(created_at),
    }
    contract["idempotency"]["requestSha256"] = revision_request_sha256(contract)
    if attestation_secret is not None:
        contract["contractAttestation"] = revision_contract_attestation(
            contract,
            attestation_secret,
        )
    return contract


def revision_request_sha256(contract: Mapping[str, Any]) -> str:
    basis = {
        "schemaVersion": contract.get("schemaVersion"),
        "jobType": contract.get("jobType"),
        "parentGenerationJobId": contract.get("parentGenerationJobId"),
        "userId": contract.get("userId"),
        "sourceDeliveryAsset": contract.get("sourceDeliveryAsset"),
        "selectedBackground": contract.get("selectedBackground"),
        "quality": contract.get("quality"),
        "mode": contract.get("mode"),
        "refinePrompt": contract.get("refinePrompt"),
        "providerSnapshot": contract.get("providerSnapshot"),
        "billing": _billing_request_basis(contract.get("billing")),
    }
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()


def revision_contract_attestation(
    contract: Mapping[str, Any],
    secret: str | bytes,
) -> dict[str, str]:
    return create_contract_attestation(
        contract,
        secret,
        domain=CONTRACT_ATTESTATION_DOMAIN,
        version=CONTRACT_ATTESTATION_VERSION,
    )


def revision_contract_attestation_valid(
    contract: Mapping[str, Any],
    secret: str | bytes,
) -> bool:
    return contract_attestation_valid(
        contract,
        secret,
        domain=CONTRACT_ATTESTATION_DOMAIN,
        version=CONTRACT_ATTESTATION_VERSION,
    )


def public_revision_payload(contract: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(contract, "contract")
    source_asset = _mapping(source.get("sourceDeliveryAsset"), "sourceDeliveryAsset")
    background = _mapping(source.get("selectedBackground"), "selectedBackground")
    quality = _mapping(source.get("quality"), "quality")
    billing = _mapping(source.get("billing"), "billing")
    idempotency = _mapping(source.get("idempotency"), "idempotency")
    provider_snapshot = _mapping(
        source.get("providerSnapshot"),
        "providerSnapshot",
    )
    provider_output = _mapping(
        provider_snapshot.get("output"),
        "providerSnapshot.output",
    )
    return {
        "schemaVersion": source.get("schemaVersion"),
        "jobType": source.get("jobType"),
        "jobId": source.get("jobId"),
        "parentGenerationJobId": source.get("parentGenerationJobId"),
        "userId": source.get("userId"),
        "sourceDeliveryAsset": {
            "assetId": source_asset.get("assetId"),
            "sha256": source_asset.get("sha256"),
            "rowNumber": source_asset.get("rowNumber"),
            "dishName": source_asset.get("dishName"),
        },
        "selectedBackground": {
            "assetId": background.get("assetId"),
            "sha256": background.get("sha256"),
        },
        "quality": {
            "id": quality.get("id"),
        },
        "mode": source.get("mode"),
        "refinePrompt": source.get("refinePrompt"),
        "providerSnapshot": {
            "provider": provider_snapshot.get("provider"),
            "model": provider_snapshot.get("model"),
            "apiSurface": provider_snapshot.get("apiSurface"),
            "promptVersion": provider_snapshot.get("promptVersion"),
            "output": dict(provider_output),
        },
        "billing": {
            key: billing.get(key)
            for key in (
                "snapshotVersion",
                "pricingVersion",
                "basePoints",
                "totalPoints",
                "freeReworkQuotaVerified",
                "debitOrderId",
                "refundOrderId",
                "state",
            )
        },
        "idempotency": {
            "key": idempotency.get("key"),
            "requestSha256": idempotency.get("requestSha256"),
        },
        "createdAt": source.get("createdAt"),
    }


def _source_delivery_asset_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "sourceDeliveryAsset")
    return {
        "assetId": _clean_id(source.get("assetId"), "sourceDeliveryAsset.assetId"),
        "objectKey": _object_key(
            source.get("objectKey"),
            "sourceDeliveryAsset.objectKey",
        ),
        "sha256": _sha256(source.get("sha256"), "sourceDeliveryAsset.sha256"),
        "rowNumber": _positive_int(
            source.get("rowNumber"),
            "sourceDeliveryAsset.rowNumber",
        ),
        "dishName": _text(
            source.get("dishName"),
            "sourceDeliveryAsset.dishName",
            max_length=MAX_DISH_NAME_LENGTH,
        ),
    }


def _background_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "selectedBackground")
    prompt_version = str(
        source.get("backgroundPromptVersion")
        or prompt_compiler.LEGACY_BACKGROUND_PROMPT_VERSION
    ).strip()
    asset_sha256 = _sha256(
        source.get("sha256"),
        "selectedBackground.sha256",
    )
    snapshot = {
        "assetId": _clean_id(source.get("assetId"), "selectedBackground.assetId"),
        "objectKey": _object_key(
            source.get("objectKey"),
            "selectedBackground.objectKey",
        ),
        "sha256": asset_sha256,
    }
    if prompt_version == background_catalog.EMPTY_SET_PROMPT_VERSION:
        snapshot["backgroundPromptVersion"] = prompt_version
        raw_evidence = source.get("generationEvidence")
        if not isinstance(raw_evidence, Mapping):
            raise RefinementContractError(
                "invalid_background_generation_evidence",
                "selectedBackground.generationEvidence is required",
                field="selectedBackground.generationEvidence",
            )
        frozen_evidence = background_catalog.frozen_generation_evidence(
            dict(raw_evidence),
            prompt_version=prompt_version,
            asset_sha256=asset_sha256,
        )
        calculated_sha256 = background_catalog.generation_evidence_sha256(
            frozen_evidence
        )
        supplied_sha256 = _sha256(
            source.get("generationEvidenceSha256"),
            "selectedBackground.generationEvidenceSha256",
        )
        if (
            canonical_json(dict(raw_evidence))
            != canonical_json(frozen_evidence)
            or not background_catalog.generation_evidence_valid(
                frozen_evidence,
                prompt_version=prompt_version,
            )
            or not hmac.compare_digest(calculated_sha256, supplied_sha256)
        ):
            raise RefinementContractError(
                "invalid_background_generation_evidence",
                "selectedBackground generation evidence is invalid",
                field="selectedBackground.generationEvidence",
            )
        snapshot["generationEvidence"] = frozen_evidence
        snapshot["generationEvidenceSha256"] = calculated_sha256
    return snapshot


def _provider_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(value, "providerSnapshot")
    output = _mapping(source.get("output"), "providerSnapshot.output")
    endpoint = _text(
        source.get("endpoint"),
        "providerSnapshot.endpoint",
        max_length=300,
    )
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError as exc:
        raise RefinementContractError(
            "invalid_provider_endpoint",
            "providerSnapshot.endpoint is invalid",
            field="providerSnapshot.endpoint",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "generativelanguage.googleapis.com"
        or parsed.port not in {None, 443}
        or parsed.path.rstrip("/") != "/v1beta/interactions"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RefinementContractError(
            "invalid_provider_endpoint",
            "providerSnapshot.endpoint is invalid",
            field="providerSnapshot.endpoint",
        )
    clean_output = {
        "mimeType": _text(
            output.get("mimeType"),
            "providerSnapshot.output.mimeType",
            max_length=40,
        ),
        "imageSize": _clean_id(
            output.get("imageSize"),
            "providerSnapshot.output.imageSize",
        ),
        "aspectRatio": _clean_id(
            output.get("aspectRatio"),
            "providerSnapshot.output.aspectRatio",
        ),
        "canvasNormalization": _clean_id(
            output.get("canvasNormalization"),
            "providerSnapshot.output.canvasNormalization",
        ),
    }
    if clean_output != DEFAULT_PROVIDER_SNAPSHOT["output"]:
        raise RefinementContractError(
            "invalid_provider_output_contract",
            "providerSnapshot.output is unsupported",
            field="providerSnapshot.output",
        )
    clean = {
        "provider": _clean_id(
            source.get("provider"),
            "providerSnapshot.provider",
        ),
        "model": _clean_id(
            source.get("model"),
            "providerSnapshot.model",
        ),
        "apiSurface": _clean_id(
            source.get("apiSurface"),
            "providerSnapshot.apiSurface",
        ),
        "endpoint": endpoint,
        "promptVersion": _clean_id(
            source.get("promptVersion"),
            "providerSnapshot.promptVersion",
        ),
        "output": clean_output,
    }
    if clean["provider"] != "google-gemini":
        raise RefinementContractError(
            "invalid_provider",
            "providerSnapshot.provider is unsupported",
            field="providerSnapshot.provider",
        )
    return clean


def _billing_request_basis(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source.get(key)
        for key in (
            "snapshotVersion",
            "pricingVersion",
            "basePoints",
            "totalPoints",
            "freeReworkQuotaVerified",
        )
    }


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RefinementContractError(
            "invalid_object",
            f"{field} must be an object",
            field=field,
        )
    return value


def _clean_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RefinementContractError(
            "invalid_identifier",
            f"{field} has an invalid format",
            field=field,
        )
    clean = value.strip()
    if not ID_RE.fullmatch(clean):
        raise RefinementContractError(
            "invalid_identifier",
            f"{field} has an invalid format",
            field=field,
        )
    return clean


def _object_key(value: Any, field: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if (
        not OBJECT_KEY_RE.fullmatch(clean)
        or clean.startswith(("/", "\\"))
        or "\\" in clean
        or any(part in {"", ".", ".."} for part in clean.split("/"))
    ):
        raise RefinementContractError(
            "invalid_object_key",
            f"{field} must be a private relative object key",
            field=field,
        )
    return clean


def _sha256(value: Any, field: str) -> str:
    clean = value.strip().lower() if isinstance(value, str) else ""
    if not SHA256_RE.fullmatch(clean):
        raise RefinementContractError(
            "invalid_sha256",
            f"{field} must be a 64-character SHA-256 digest",
            field=field,
        )
    return clean


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RefinementContractError(
            "invalid_integer",
            f"{field} must be a positive integer",
            field=field,
        )
    return value


def _quality(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in REWORK_POINTS:
        raise RefinementContractError(
            "invalid_quality",
            "quality must be standard or premium",
            field="quality",
        )
    return value.strip().lower()


def _mode(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in {"rework", "refine"}:
        raise RefinementContractError(
            "invalid_mode",
            "mode must be rework or refine",
            field="mode",
        )
    return value.strip().lower()


def _refine_prompt(value: Any, mode: str) -> str:
    if value is not None and not isinstance(value, str):
        raise RefinementContractError(
            "invalid_refine_prompt",
            "refinePrompt must be a string",
            field="refinePrompt",
        )
    prompt = _normalize_text(value or "", "refinePrompt")
    if mode == "rework" and prompt:
        raise RefinementContractError(
            "unexpected_refine_prompt",
            "rework mode must not include a refine prompt",
            field="refinePrompt",
        )
    if mode == "refine" and not prompt:
        raise RefinementContractError(
            "missing_refine_prompt",
            "refine mode requires a refine prompt",
            field="refinePrompt",
        )
    if len(prompt) > MAX_REFINE_PROMPT_LENGTH:
        raise RefinementContractError(
            "refine_prompt_too_long",
            f"refinePrompt must not exceed {MAX_REFINE_PROMPT_LENGTH} characters",
            field="refinePrompt",
        )
    return prompt


def _text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise RefinementContractError(
            "invalid_text",
            f"{field} must be a string",
            field=field,
        )
    clean = _normalize_text(value, field)
    if not clean or len(clean) > max_length:
        raise RefinementContractError(
            "invalid_text",
            f"{field} must contain 1 to {max_length} characters",
            field=field,
        )
    return clean


def _normalize_text(value: str, field: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(char) in {"Cc", "Cf"} and not char.isspace()
        for char in normalized
    ):
        raise RefinementContractError(
            "invalid_text_control",
            f"{field} contains unsupported control characters",
            field=field,
        )
    return re.sub(r"\s+", " ", normalized).strip()


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise RefinementContractError(
            "invalid_boolean",
            f"{field} must be a boolean",
            field=field,
        )
    return value


def _created_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        raise RefinementContractError(
            "invalid_timestamp",
            "createdAt has an invalid format",
            field="createdAt",
        )
    clean = value.strip()
    if not clean or len(clean) > 40:
        raise RefinementContractError(
            "invalid_timestamp",
            "createdAt has an invalid format",
            field="createdAt",
        )
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RefinementContractError(
            "invalid_timestamp",
            "createdAt has an invalid format",
            field="createdAt",
        ) from exc
    if parsed.tzinfo is None:
        raise RefinementContractError(
            "invalid_timestamp",
            "createdAt must include a timezone",
            field="createdAt",
        )
    return clean
