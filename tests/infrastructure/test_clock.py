"""Tests for the shared UTC time helpers."""

from __future__ import annotations

import re
from datetime import timezone

import pytest

from runforge.infrastructure.clock import parse_utc, utc_compact_timestamp, utc_now


def test_utc_now_is_iso8601_with_a_literal_z_offset():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", utc_now())


def test_utc_compact_timestamp_is_colon_free_and_filesystem_safe():
    timestamp = utc_compact_timestamp()
    assert re.fullmatch(r"\d{8}T\d{6}Z", timestamp)
    assert ":" not in timestamp


def test_parse_utc_round_trips_utc_now():
    text = utc_now()
    parsed = parse_utc(text)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_parse_utc_accepts_an_explicit_cutoff_timestamp():
    parsed = parse_utc("2026-08-01T00:00:00Z")
    assert parsed == parsed.replace(year=2026, month=8, day=1, hour=0, minute=0, second=0, microsecond=0)
    assert parsed.tzinfo == timezone.utc


def test_parse_utc_rejects_an_unparseable_value():
    with pytest.raises(ValueError):
        parse_utc("not-a-timestamp")
