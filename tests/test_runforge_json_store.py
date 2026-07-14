"""Tests for the generic atomic JSON-object persistence."""

from __future__ import annotations

import json

import pytest

from runforge.json_store import JsonStoreError, load_json_object, save_json_object


def test_json_object_round_trip_creates_parent_directories_and_sorts_keys(tmp_path):
    path = tmp_path / "nested" / "metadata.json"

    save_json_object(path, {"z": [1, 2], "a": {"enabled": True}})

    assert load_json_object(path) == {"a": {"enabled": True}, "z": [1, 2]}
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": {"enabled": True}, "z": [1, 2]}


def test_atomic_save_preserves_previous_json_when_replacement_fails(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    save_json_object(path, {"state": "created"})

    def fail_replace(*_args: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("runforge.json_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_json_object(path, {"state": "failed"})

    assert load_json_object(path) == {"state": "created"}
    assert not list(tmp_path.glob(".metadata.json.*.tmp"))


def test_json_store_rejects_non_object_values_and_malformed_json(tmp_path):
    with pytest.raises(JsonStoreError, match="must map string keys"):
        save_json_object(tmp_path / "array.json", ["not", "an", "object"])  # type: ignore[arg-type]

    malformed = tmp_path / "malformed.json"
    malformed.write_text("[not valid JSON", encoding="utf-8")
    with pytest.raises(JsonStoreError, match="Could not read JSON object"):
        load_json_object(malformed)
