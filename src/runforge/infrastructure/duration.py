"""Minimal duration parsing and formatting shared by the CLI and its diagnostics."""

from __future__ import annotations

import re
from datetime import timedelta


_DURATION_PATTERN = re.compile(r"(\d+)([smhd])")
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class DurationError(ValueError):
    """Raised when a duration string cannot be parsed."""


def parse_duration(value: str) -> timedelta:
    """Parse a single integer-plus-unit duration, e.g. ``6h``, ``30m``, ``2d``.

    Deliberately minimal: one integer, one unit letter, no compound form like
    ``1h30m``. This satisfies the current need (a single --older-than cutoff)
    without speculative parsing generality.
    """
    match = _DURATION_PATTERN.fullmatch(value.strip())
    if not match:
        raise DurationError("must be a positive integer followed by s, m, h, or d (e.g. 6h)")
    amount = int(match.group(1))
    if amount <= 0:
        raise DurationError("must be a positive integer followed by s, m, h, or d (e.g. 6h)")
    return timedelta(seconds=amount * _SECONDS_PER_UNIT[match.group(2)])


def format_duration(seconds: float) -> str:
    """Format elapsed seconds using the same s/m/h/d vocabulary parse_duration accepts.

    Shows the two coarsest non-zero units (e.g. ``8h12m``, not
    ``8h12m34s``), which is precise enough for a staleness hint without
    implying second-level accuracy a heartbeat-free diagnostic can't offer.
    """
    total = int(seconds)
    days, remainder = divmod(total, _SECONDS_PER_UNIT["d"])
    hours, remainder = divmod(remainder, _SECONDS_PER_UNIT["h"])
    minutes, secs = divmod(remainder, _SECONDS_PER_UNIT["m"])
    parts = [f"{value}{unit}" for value, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (secs, "s")) if value]
    return "".join(parts[:2]) if parts else "0s"
