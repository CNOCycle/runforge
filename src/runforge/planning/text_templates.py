"""One-pass rendering for format-preserving UTF-8 text templates."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping


_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")


class TextTemplateError(ValueError):
    """Raised when a text template cannot be rendered safely."""


def render_text_template(content: str, replacements: Mapping[str, object]) -> str:
    """Render known JSON scalar placeholders without changing surrounding text."""
    if not isinstance(content, str):
        raise TextTemplateError("text template content must be UTF-8 text")
    values = _normalize_replacements(replacements)
    return _PLACEHOLDER_PATTERN.sub(lambda match: _replacement_text(match.group(1), values), content)


def _normalize_replacements(values: Mapping[str, object]) -> dict[str, str | int | float | bool]:
    if not isinstance(values, Mapping):
        raise TextTemplateError("text placeholder values must be a mapping")
    normalized: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or not _PLACEHOLDER_PATTERN.fullmatch(f"{{{key}}}"):
            raise TextTemplateError("text placeholder names must be non-empty brace-safe strings")
        if isinstance(value, bool) or isinstance(value, (str, int)):
            normalized[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            normalized[key] = value
        else:
            raise TextTemplateError("text placeholder values must be strings, numbers, or booleans")
    return normalized


def _replacement_text(key: str, replacements: Mapping[str, str | int | float | bool]) -> str:
    try:
        value = replacements[key]
    except KeyError as error:
        raise TextTemplateError(f"unknown text placeholder: {key}") from error
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
