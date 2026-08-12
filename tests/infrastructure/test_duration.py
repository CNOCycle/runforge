"""Tests for the minimal duration parser and formatter."""

from __future__ import annotations

from datetime import timedelta

import pytest

from runforge.infrastructure.duration import DurationError, format_duration, parse_duration


@pytest.mark.parametrize(
    "value, expected",
    [
        ("6h", timedelta(hours=6)),
        ("30m", timedelta(minutes=30)),
        ("2d", timedelta(days=2)),
        ("45s", timedelta(seconds=45)),
        ("1s", timedelta(seconds=1)),
        ("  6h  ", timedelta(hours=6)),
    ],
)
def test_parse_duration_accepts_a_single_integer_and_unit(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "6",
        "h",
        "6hh",
        "1h30m",
        "-6h",
        "0h",
        "6.5h",
        "6H",
        "6 h",
        "six hours",
    ],
)
def test_parse_duration_rejects_invalid_input(value):
    with pytest.raises(DurationError):
        parse_duration(value)


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (45, "45s"),
        (90, "1m30s"),
        (3600, "1h"),
        (3600 + 12 * 60, "1h12m"),
        (8 * 3600 + 12 * 60 + 34, "8h12m"),
        (2 * 86400 + 3 * 3600, "2d3h"),
        (2 * 86400, "2d"),
    ],
)
def test_format_duration_shows_the_two_coarsest_units(seconds, expected):
    assert format_duration(seconds) == expected
