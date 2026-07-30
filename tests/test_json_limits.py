from __future__ import annotations

import json

import pytest

from shared.json_limits import (
    InvalidJsonValue,
    JsonSizeLimitExceeded,
    validate_json_size,
)


def test_json_size_matches_compact_utf8_encoding() -> None:
    value = {
        "name": "番茄\n炒蛋",
        "enabled": True,
        "items": [None, 12, '"quoted"', "\u0001"],
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert validate_json_size(value, len(encoded)) == len(encoded)


def test_json_size_rejects_before_a_large_string_can_be_serialized() -> None:
    with pytest.raises(JsonSizeLimitExceeded):
        validate_json_size({"metadata": "x" * 129}, 128)


def test_json_size_rejects_cycles_and_excessive_depth() -> None:
    circular: list[object] = []
    circular.append(circular)

    with pytest.raises(InvalidJsonValue, match="circular"):
        validate_json_size(circular, 1024)
    with pytest.raises(JsonSizeLimitExceeded, match="depth"):
        validate_json_size([[[["deep"]]]], 1024, max_depth=2)
