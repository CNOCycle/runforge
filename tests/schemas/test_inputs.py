"""Tests for immutable planned-input metadata."""

from __future__ import annotations

import pytest

from runforge.schemas.inputs import PlannedInput, PlannedInputError, PlannedInputManifest


def test_input_manifest_round_trip_preserves_ordered_entries():
    manifest = PlannedInputManifest(
        entries=(
            PlannedInput(path="configs/eval.ini", kind="text-template", sha256="a" * 64),
            PlannedInput(path="configs/train.yaml", kind="copy", sha256="b" * 64),
        )
    )

    payload = manifest.to_dict()

    assert payload["kind"] == "runforge_planned_input_manifest"
    assert payload["schema_version"] == 1
    assert PlannedInputManifest.from_dict(payload) == manifest


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.json",
        "../escape.json",
        "nested/../escape.json",
        "./relative.json",
        "nested\\windows.json",
        "C:drive-relative.json",
        "nested//duplicate.json",
    ],
)
def test_input_rejects_unsafe_relative_paths(path):
    with pytest.raises(PlannedInputError, match="safe relative POSIX path"):
        PlannedInput(path=path, kind="copy", sha256="a" * 64)


@pytest.mark.parametrize(
    "kind, digest, match",
    [
        ("template", "a" * 64, "input.kind"),
        ("copy", "A" * 64, "SHA-256"),
    ],
)
def test_input_rejects_unknown_kind_or_invalid_digest(kind, digest, match):
    with pytest.raises(PlannedInputError, match=match):
        PlannedInput(path="config.json", kind=kind, sha256=digest)


def test_manifest_rejects_empty_duplicate_or_unsorted_entries():
    entry_a = PlannedInput(path="a.json", kind="copy", sha256="a" * 64)
    entry_b = PlannedInput(path="b.json", kind="copy", sha256="b" * 64)

    with pytest.raises(PlannedInputError, match="must not be empty"):
        PlannedInputManifest(entries=())
    with pytest.raises(PlannedInputError, match="duplicates"):
        PlannedInputManifest(entries=(entry_a, entry_a))
    with pytest.raises(PlannedInputError, match="must be sorted"):
        PlannedInputManifest(entries=(entry_b, entry_a))


def test_manifest_rejects_unknown_or_unsupported_serialized_fields():
    manifest = PlannedInputManifest(entries=(PlannedInput(path="config.json", kind="copy", sha256="a" * 64),))
    payload = manifest.to_dict()
    payload["future_field"] = True

    with pytest.raises(PlannedInputError, match="Unknown input manifest field"):
        PlannedInputManifest.from_dict(payload)

    payload = manifest.to_dict()
    payload["schema_version"] = 2
    with pytest.raises(PlannedInputError, match="Unsupported input manifest schema version"):
        PlannedInputManifest.from_dict(payload)
