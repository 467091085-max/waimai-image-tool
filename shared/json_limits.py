from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class JsonSizeLimitExceeded(ValueError):
    pass


class InvalidJsonValue(ValueError):
    pass


@dataclass
class _SizeBudget:
    limit: int
    max_depth: int
    max_nodes: int
    total: int = 0
    nodes: int = 0
    active_container_ids: set[int] = field(default_factory=set)

    def add(self, amount: int) -> None:
        self.total += amount
        if self.total > self.limit:
            raise JsonSizeLimitExceeded("JSON value exceeds size limit")

    def visit(self) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise JsonSizeLimitExceeded("JSON value exceeds node limit")


def validate_json_size(
    value: Any,
    max_bytes: int,
    *,
    max_depth: int = 32,
    max_nodes: int = 200_000,
) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes <= 0:
        raise ValueError("max_nodes must be a positive integer")

    budget = _SizeBudget(
        limit=max_bytes,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    _measure_json_value(value, budget, depth=0)
    return budget.total


def _measure_json_value(
    value: Any,
    budget: _SizeBudget,
    *,
    depth: int,
) -> None:
    budget.visit()
    if depth > budget.max_depth:
        raise JsonSizeLimitExceeded("JSON value exceeds depth limit")

    if value is None:
        budget.add(4)
        return
    if value is True:
        budget.add(4)
        return
    if value is False:
        budget.add(5)
        return
    if isinstance(value, str):
        _measure_json_string(value, budget)
        return
    if isinstance(value, int):
        budget.add(len(str(value)))
        return
    if isinstance(value, float):
        budget.add(len(json.dumps(value, allow_nan=True)))
        return
    if isinstance(value, Mapping):
        _measure_mapping(value, budget, depth=depth)
        return
    if isinstance(value, (list, tuple)):
        _measure_sequence(value, budget, depth=depth)
        return
    raise InvalidJsonValue(
        f"value of type {type(value).__name__} is not JSON serializable"
    )


def _measure_mapping(
    value: Mapping[Any, Any],
    budget: _SizeBudget,
    *,
    depth: int,
) -> None:
    container_id = id(value)
    if container_id in budget.active_container_ids:
        raise InvalidJsonValue("circular JSON value")
    budget.active_container_ids.add(container_id)
    try:
        budget.add(2)
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise InvalidJsonValue("JSON object keys must be strings")
            if index:
                budget.add(1)
            _measure_json_string(key, budget)
            budget.add(1)
            _measure_json_value(item, budget, depth=depth + 1)
    finally:
        budget.active_container_ids.remove(container_id)


def _measure_sequence(
    value: list[Any] | tuple[Any, ...],
    budget: _SizeBudget,
    *,
    depth: int,
) -> None:
    container_id = id(value)
    if container_id in budget.active_container_ids:
        raise InvalidJsonValue("circular JSON value")
    budget.active_container_ids.add(container_id)
    try:
        budget.add(2)
        for index, item in enumerate(value):
            if index:
                budget.add(1)
            _measure_json_value(item, budget, depth=depth + 1)
    finally:
        budget.active_container_ids.remove(container_id)


def _measure_json_string(value: str, budget: _SizeBudget) -> None:
    budget.add(2)
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            budget.add(2)
        elif character in {"\b", "\f", "\n", "\r", "\t"}:
            budget.add(2)
        elif codepoint <= 0x1F:
            budget.add(6)
        elif codepoint <= 0x7F:
            budget.add(1)
        elif codepoint <= 0x7FF:
            budget.add(2)
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise InvalidJsonValue("JSON string contains an invalid surrogate")
        elif codepoint <= 0xFFFF:
            budget.add(3)
        else:
            budget.add(4)


__all__ = [
    "InvalidJsonValue",
    "JsonSizeLimitExceeded",
    "validate_json_size",
]
