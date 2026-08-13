from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any, Mapping


ATTESTATION_CONTEXT = "waimai-image-tool.contract-attestation"
MIN_ATTESTATION_SECRET_BYTES = 32
SHA256_RE = re.compile(r"[a-f0-9]{64}")


def create_contract_attestation(
    contract: Mapping[str, Any],
    secret: str | bytes,
    *,
    domain: str,
    version: str,
) -> dict[str, str]:
    signature = hmac.new(
        _secret_bytes(secret),
        _attestation_message(
            contract,
            domain=domain,
            version=version,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "version": version,
        "hmacSha256": signature,
    }


def contract_attestation_valid(
    contract: Mapping[str, Any],
    secret: str | bytes,
    *,
    domain: str,
    version: str,
) -> bool:
    raw = contract.get("contractAttestation")
    if not isinstance(raw, Mapping) or set(raw) != {"version", "hmacSha256"}:
        return False
    supplied_version = str(raw.get("version") or "").strip()
    supplied_signature = str(raw.get("hmacSha256") or "").strip().lower()
    if supplied_version != version or not SHA256_RE.fullmatch(supplied_signature):
        return False
    try:
        expected = create_contract_attestation(
            contract,
            secret,
            domain=domain,
            version=version,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(
        supplied_signature,
        expected["hmacSha256"],
    )


def contract_attestation_secret_from_env() -> str:
    for name in (
        "OBJECT_SIGNING_SECRET",
        "ASSET_SIGNING_SECRET",
        "DOWNLOAD_SIGNING_SECRET",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _attestation_message(
    contract: Mapping[str, Any],
    *,
    domain: str,
    version: str,
) -> bytes:
    clean_domain = _nonempty_text(domain, "contract attestation domain")
    clean_version = _nonempty_text(version, "contract attestation version")
    basis = {
        str(key): value
        for key, value in contract.items()
        if str(key) != "contractAttestation"
    }
    canonical = json.dumps(
        basis,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "\x00".join(
        (
            ATTESTATION_CONTEXT,
            clean_domain,
            clean_version,
            canonical,
        )
    ).encode("utf-8")


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        value = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        value = secret
    else:
        raise TypeError("contract attestation secret is invalid")
    if len(value) < MIN_ATTESTATION_SECRET_BYTES:
        raise ValueError(
            "contract attestation secret must contain at least 32 bytes"
        )
    return value


def _nonempty_text(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field} is required")
    return clean
