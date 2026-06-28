from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final


DEFAULT_EXTRA_PLATFORM_POINTS: Final = 100

_PLATFORM_RULES: Final[dict[str, dict[str, Any]]] = {
    "meituan": {
        "platform_id": "meituan",
        "name": "美团外卖",
        "width": 800,
        "height": 600,
        "max_mb": 5,
        "max_kb": 5 * 1024,
    },
    "taobao": {
        "platform_id": "taobao",
        "name": "淘宝外卖/饿了么",
        "width": 800,
        "height": 800,
        "max_mb": 20,
        "max_kb": 20 * 1024,
    },
    "jd": {
        "platform_id": "jd",
        "name": "京东外卖/京东秒送",
        "width": 800,
        "height": 800,
        "max_mb": 5,
        "max_kb": 5 * 1024,
    },
}


def get_platform_rule(platform_id: str) -> dict[str, Any]:
    """Return a copy of the export rule for a platform."""
    normalized = _normalize_platform_id(platform_id)
    try:
        return dict(_PLATFORM_RULES[normalized])
    except KeyError as exc:
        raise ValueError(f"unknown platform: {platform_id!r}") from exc


def list_platform_rules() -> list[dict[str, Any]]:
    """Return all platform export rules in display order."""
    return [dict(rule) for rule in _PLATFORM_RULES.values()]


def validate_platform_selection(platform_ids: Iterable[str] | str | None) -> list[str]:
    """Validate selected platforms, deduplicating them while preserving order."""
    selected: list[str] = []
    seen: set[str] = set()

    for raw_platform_id in _iter_platform_ids(platform_ids):
        platform_id = _normalize_platform_id(raw_platform_id)
        if not platform_id:
            continue
        if platform_id not in _PLATFORM_RULES:
            raise ValueError(f"unknown platform: {raw_platform_id!r}")
        if platform_id not in seen:
            selected.append(platform_id)
            seen.add(platform_id)

    if not selected:
        raise ValueError("at least one platform must be selected")
    return selected


def platform_charge_points(
    platform_ids: Iterable[str] | str | None,
    extra_platform_points: int = DEFAULT_EXTRA_PLATFORM_POINTS,
) -> int:
    """Return points charged for additional selected platforms.

    The first selected platform is free. Each additional unique platform costs
    extra_platform_points.
    """
    extra_points = _non_negative_int(extra_platform_points, "extra_platform_points")
    selected = validate_platform_selection(platform_ids)
    return max(len(selected) - 1, 0) * extra_points


def _iter_platform_ids(platform_ids: Iterable[str] | str | None) -> list[Any]:
    if platform_ids is None:
        return []
    if isinstance(platform_ids, str):
        return platform_ids.split(",")
    try:
        return list(platform_ids)
    except TypeError as exc:
        raise TypeError("platform_ids must be an iterable of platform ids") from exc


def _normalize_platform_id(platform_id: Any) -> str:
    return str(platform_id).strip().lower()


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


__all__ = [
    "DEFAULT_EXTRA_PLATFORM_POINTS",
    "get_platform_rule",
    "list_platform_rules",
    "platform_charge_points",
    "validate_platform_selection",
]
