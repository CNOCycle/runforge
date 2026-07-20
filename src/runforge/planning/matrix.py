"""Deterministic validation and Cartesian expansion for parameter matrices."""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Mapping, Sequence


JsonScalar = str | int | float | bool


_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MatrixError(ValueError):
    """Raised when a parameter matrix cannot be expanded deterministically."""


def expand_matrix(matrix: Mapping[str, Sequence[object]]) -> tuple[dict[str, JsonScalar], ...]:
    """Return combinations ordered by sorted keys and each axis's input order."""
    if not isinstance(matrix, Mapping) or not matrix:
        raise MatrixError("parameter matrix must be a non-empty mapping")
    if not all(isinstance(key, str) for key in matrix):
        raise MatrixError("parameter matrix keys must be strings")
    normalized: list[tuple[str, tuple[JsonScalar, ...]]] = []
    for key in sorted(matrix):
        if not _PARAMETER_PATTERN.fullmatch(key):
            raise MatrixError(f"Invalid matrix parameter name: {key!r}")
        if key in {"ARTIFACT_DIR", "INPUT_DIR"}:
            raise MatrixError(f"{key} is reserved by RunForge")
        raw_values = matrix[key]
        if isinstance(raw_values, (str, bytes)) or not isinstance(raw_values, Sequence):
            raise MatrixError(f"Matrix parameter {key!r} must contain an array of values")
        values = tuple(_parameter_value(value, key) for value in raw_values)
        if not values:
            raise MatrixError(f"Matrix parameter {key!r} must contain at least one value")
        normalized.append((key, values))
    keys = tuple(key for key, _values in normalized)
    products = itertools.product(*(values for _key, values in normalized))
    return tuple(dict(zip(keys, values, strict=True)) for values in products)


def parameter_text(value: JsonScalar) -> str:
    """Return the deterministic command-placeholder representation of one scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parameter_value(value: object, key: str) -> JsonScalar:
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MatrixError(f"Matrix parameter {key!r} contains a non-finite float")
        return value
    raise MatrixError(f"Matrix parameter {key!r} values must be strings, numbers, or booleans")
