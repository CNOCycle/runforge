"""Tests for the shared UTC time helpers."""

from __future__ import annotations

import re

from runforge.infrastructure.clock import utc_compact_timestamp, utc_now


def test_utc_now_is_iso8601_with_a_literal_z_offset():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", utc_now())


def test_utc_compact_timestamp_is_colon_free_and_filesystem_safe():
    timestamp = utc_compact_timestamp()
    assert re.fullmatch(r"\d{8}T\d{6}Z", timestamp)
    assert ":" not in timestamp
