from __future__ import annotations

from typing import Any


MAX_PROMPT_CHARS = 8_000
MAX_PROMPT_BYTES = 32 * 1024
MAX_PROMPT_REQUEST_BODY_BYTES = 64 * 1024


class PromptValidationError(ValueError):
    pass


def normalize_prompt(
    value: Any,
    *,
    max_chars: int = MAX_PROMPT_CHARS,
    max_bytes: int = MAX_PROMPT_BYTES,
) -> str:
    if not isinstance(value, str):
        raise PromptValidationError("prompt must be a string")
    if len(value) > max_chars:
        raise PromptValidationError("prompt exceeds size limit")
    clean_prompt = value.strip()
    if not clean_prompt:
        raise PromptValidationError("prompt is required")
    try:
        encoded_size = len(clean_prompt.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PromptValidationError(
            "prompt is not valid UTF-8"
        ) from exc
    if encoded_size > max_bytes:
        raise PromptValidationError("prompt exceeds size limit")
    return clean_prompt


__all__ = [
    "MAX_PROMPT_BYTES",
    "MAX_PROMPT_CHARS",
    "MAX_PROMPT_REQUEST_BODY_BYTES",
    "PromptValidationError",
    "normalize_prompt",
]
