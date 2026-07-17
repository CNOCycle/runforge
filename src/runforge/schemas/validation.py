"""Shared validation helpers for strict RunForge JSON schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def require_text(value: Any, context: str, error_type: type[ValueError]) -> str:
    """Return a non-empty string or raise the caller's schema error."""
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{context} must be a non-empty string")
    return value


def require_object(value: Any, context: str, error_type: type[ValueError]) -> Mapping[str, Any]:
    """Return a JSON-object-like mapping or raise the caller's schema error."""
    if not isinstance(value, Mapping):
        raise error_type(f"{context} must be a JSON object")
    return value


def require_exact_fields(
    data: Mapping[str, Any],
    expected: set[str],
    context: str,
    error_type: type[ValueError],
) -> None:
    """Reject missing or unknown fields in a strict JSON object."""
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown:
        raise error_type(f"Unknown {context} field(s): {', '.join(unknown)}")
    if missing:
        raise error_type(f"Missing {context} field(s): {', '.join(missing)}")


def require_string_mapping(value: Any, context: str, error_type: type[ValueError]) -> dict[str, str]:
    """Return a copied non-empty-string-to-string mapping."""
    if not isinstance(value, Mapping):
        raise error_type(f"{context} must map non-empty strings to strings")
    result = dict(value)
    if not all(isinstance(key, str) and key and isinstance(item, str) for key, item in result.items()):
        raise error_type(f"{context} must map non-empty strings to strings")
    return result
