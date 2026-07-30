from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


CATALOG_VERSION = "recharge-cny-v1"
CURRENCY = "CNY"


class PaymentCatalogError(ValueError):
    pass


class UnknownPaymentPackage(PaymentCatalogError):
    pass


class InvalidPaymentCatalogRequest(PaymentCatalogError):
    pass


@dataclass(frozen=True)
class PaymentPackage:
    package_id: str
    name: str
    amount_cents: int
    points: int

    def snapshot(self) -> dict[str, Any]:
        snapshot = {
            "packageId": self.package_id,
            "name": self.name,
            "amountCents": self.amount_cents,
            "points": self.points,
            "currency": CURRENCY,
            "catalogVersion": CATALOG_VERSION,
        }
        return {
            **snapshot,
            "snapshotDigest": stable_snapshot_digest(snapshot),
        }


PACKAGES = (
    PaymentPackage("starter-500", "Starter", 4_900, 500),
    PaymentPackage("store-1040", "Store", 9_900, 1_040),
    PaymentPackage("team-3190", "Team", 29_900, 3_190),
)
_PACKAGES_BY_ID = {item.package_id: item for item in PACKAGES}


def stable_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(snapshot),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def package_snapshot(package_id: str) -> dict[str, Any]:
    clean_package_id = str(package_id or "").strip()
    package = _PACKAGES_BY_ID.get(clean_package_id)
    if package is None:
        raise UnknownPaymentPackage(f"Unknown payment package: {clean_package_id or '<empty>'}")
    return package.snapshot()


def resolve_client_package(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise InvalidPaymentCatalogRequest("Payment request must be an object")
    unexpected = sorted(str(key) for key in request if str(key) != "packageId")
    if unexpected:
        raise InvalidPaymentCatalogRequest(
            "Only packageId is accepted from the payment client: " + ", ".join(unexpected)
        )
    return package_snapshot(str(request.get("packageId") or ""))


def catalog_snapshot() -> dict[str, Any]:
    snapshot = {
        "catalogVersion": CATALOG_VERSION,
        "currency": CURRENCY,
        "packages": [item.snapshot() for item in PACKAGES],
    }
    return {
        **snapshot,
        "catalogDigest": stable_snapshot_digest(snapshot),
    }


def legacy_cash_packages() -> dict[int, int]:
    return {
        item.amount_cents // 100: item.points
        for item in PACKAGES
        if item.amount_cents % 100 == 0
    }
