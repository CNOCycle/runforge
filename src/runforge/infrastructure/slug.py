"""Filesystem-safe slug derivation shared by planning and execution."""

from __future__ import annotations

import re


_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(value: str, fallback: str) -> str:
    """Return a filesystem-safe slug for value, or fallback if nothing usable remains."""
    slug = _SLUG_PATTERN.sub("-", value.strip()).strip("-._")
    return (slug or fallback)[:64]
