"""Tests for the versioned non-Git directory source schemas."""

from __future__ import annotations

import pytest

from runforge.schemas.directory_source import (
    DirectorySnapshotSource,
    DirectorySourceEntry,
    DirectorySourceError,
    DirectorySourceManifest,
    VerifiedDirectorySource,
)


def test_directory_source_entry_round_trip():
    entry = DirectorySourceEntry(path="nested/file.txt", executable=True, sha256="a" * 64)

    payload = entry.to_dict()

    assert DirectorySourceEntry.from_dict(payload) == entry
    assert payload == {"path": "nested/file.txt", "executable": True, "sha256": "a" * 64}


@pytest.mark.parametrize(
    "factory, match",
    [
        (lambda: DirectorySourceEntry(path="/abs.txt", executable=False, sha256="a" * 64), "safe relative"),
        (lambda: DirectorySourceEntry(path="../escape.txt", executable=False, sha256="a" * 64), "safe relative"),
        (lambda: DirectorySourceEntry(path="file.txt", executable=False, sha256="not-hex"), "SHA-256"),
        (lambda: DirectorySourceEntry(path="file.txt", executable="yes", sha256="a" * 64), "boolean"),
    ],
)
def test_directory_source_entry_rejects_invalid_metadata(factory, match):
    with pytest.raises(DirectorySourceError, match=match):
        factory()


def test_directory_source_manifest_round_trip():
    manifest = DirectorySourceManifest(
        entries=(
            DirectorySourceEntry(path="a.txt", executable=False, sha256="a" * 64),
            DirectorySourceEntry(path="b/c.txt", executable=True, sha256="b" * 64),
        ),
        tree_digest="c" * 64,
    )

    payload = manifest.to_dict()

    assert DirectorySourceManifest.from_dict(payload) == manifest
    assert payload["schema_version"] == 1
    assert payload["tree_digest"] == "c" * 64


def test_directory_source_manifest_allows_empty_entries():
    manifest = DirectorySourceManifest(entries=(), tree_digest="a" * 64)

    assert DirectorySourceManifest.from_dict(manifest.to_dict()) == manifest


def test_directory_source_manifest_rejects_duplicate_or_unsorted_paths():
    entry = DirectorySourceEntry(path="a.txt", executable=False, sha256="a" * 64)
    with pytest.raises(DirectorySourceError, match="duplicates"):
        DirectorySourceManifest(entries=(entry, entry), tree_digest="a" * 64)

    unsorted = (
        DirectorySourceEntry(path="b.txt", executable=False, sha256="a" * 64),
        DirectorySourceEntry(path="a.txt", executable=False, sha256="b" * 64),
    )
    with pytest.raises(DirectorySourceError, match="sorted"):
        DirectorySourceManifest(entries=unsorted, tree_digest="a" * 64)


def test_directory_source_manifest_rejects_unknown_or_unsupported_serialized_metadata():
    manifest = DirectorySourceManifest(entries=(), tree_digest="a" * 64)
    payload = manifest.to_dict()
    payload["future_field"] = True
    with pytest.raises(DirectorySourceError, match="Unknown source manifest field"):
        DirectorySourceManifest.from_dict(payload)

    payload = manifest.to_dict()
    payload["schema_version"] = 2
    with pytest.raises(DirectorySourceError, match="Unsupported source manifest schema version"):
        DirectorySourceManifest.from_dict(payload)


def test_verified_directory_source_round_trip(tmp_path):
    source = VerifiedDirectorySource(path=tmp_path.resolve(), tree_digest="a" * 64)

    payload = source.to_dict()

    assert VerifiedDirectorySource.from_dict(payload) == source
    assert payload["kind"] == "runforge_verified_directory_source"
    assert payload["schema_version"] == 1


def test_verified_directory_source_rejects_relative_path_and_bad_digest(tmp_path):
    with pytest.raises(DirectorySourceError, match="absolute"):
        VerifiedDirectorySource(path="relative/path", tree_digest="a" * 64)
    with pytest.raises(DirectorySourceError, match="SHA-256"):
        VerifiedDirectorySource(path=tmp_path.resolve(), tree_digest="not-hex")


def test_verified_directory_source_rejects_unknown_or_unsupported_serialized_metadata(tmp_path):
    source = VerifiedDirectorySource(path=tmp_path.resolve(), tree_digest="a" * 64)
    payload = source.to_dict()
    payload["kind"] = "runforge_directory_snapshot_source"
    with pytest.raises(DirectorySourceError, match="Unsupported verified-directory source kind"):
        VerifiedDirectorySource.from_dict(payload)

    payload = source.to_dict()
    payload["schema_version"] = 2
    with pytest.raises(DirectorySourceError, match="Unsupported verified-directory source schema version"):
        VerifiedDirectorySource.from_dict(payload)


def test_directory_snapshot_source_round_trip(tmp_path):
    source = DirectorySnapshotSource(original_path=tmp_path.resolve(), tree_digest="a" * 64)

    payload = source.to_dict()

    assert DirectorySnapshotSource.from_dict(payload) == source
    assert payload["kind"] == "runforge_directory_snapshot_source"
    assert payload["schema_version"] == 1


def test_directory_snapshot_source_rejects_relative_path_and_bad_digest(tmp_path):
    with pytest.raises(DirectorySourceError, match="absolute"):
        DirectorySnapshotSource(original_path="relative/path", tree_digest="a" * 64)
    with pytest.raises(DirectorySourceError, match="SHA-256"):
        DirectorySnapshotSource(original_path=tmp_path.resolve(), tree_digest="not-hex")


def test_directory_snapshot_source_rejects_unknown_or_unsupported_serialized_metadata(tmp_path):
    source = DirectorySnapshotSource(original_path=tmp_path.resolve(), tree_digest="a" * 64)
    payload = source.to_dict()
    payload["kind"] = "runforge_verified_directory_source"
    with pytest.raises(DirectorySourceError, match="Unsupported directory-snapshot source kind"):
        DirectorySnapshotSource.from_dict(payload)

    payload = source.to_dict()
    payload["schema_version"] = 2
    with pytest.raises(DirectorySourceError, match="Unsupported directory-snapshot source schema version"):
        DirectorySnapshotSource.from_dict(payload)
