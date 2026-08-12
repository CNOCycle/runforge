"""Tests for the worker workspace manifest schema."""

from __future__ import annotations

import pytest

from runforge.schemas.worker_manifest import WorkerManifest, WorkerManifestError


def _manifest(**overrides) -> WorkerManifest:
    fields = {
        "source_kind": "git",
        "source_identity": "/repositories/repo_A",
        "source_reference": "a" * 40,
        "host": "node7",
        "pid": 4127,
        "owner": "node7:4127",
        "worker_id": "81cf070577f54cfb991791368f7fc2c3",
        "started_at": "2026-07-30T18:15:30.000000Z",
    }
    fields.update(overrides)
    return WorkerManifest(**fields)


def test_worker_manifest_round_trip_preserves_every_field():
    manifest = _manifest()

    payload = manifest.to_dict()

    assert WorkerManifest.from_dict(payload) == manifest
    assert payload["kind"] == "runforge_worker_manifest"
    assert payload["schema_version"] == 1


def test_worker_manifest_accepts_directory_snapshot_source_kind():
    manifest = _manifest(source_kind="directory-snapshot", source_reference="b" * 64)

    assert WorkerManifest.from_dict(manifest.to_dict()) == manifest


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"source_kind": "svn"}, "source_kind must be one of"),
        ({"source_identity": ""}, "source_identity must be a non-empty string"),
        ({"host": ""}, "host must be a non-empty string"),
        ({"pid": 0}, "pid must be a positive integer"),
        ({"pid": -1}, "pid must be a positive integer"),
        ({"pid": True}, "pid must be a positive integer"),
        ({"pid": "4127"}, "pid must be a positive integer"),
        ({"owner": ""}, "owner must be a non-empty string"),
        ({"worker_id": ""}, "worker_id must be a non-empty string"),
        ({"started_at": ""}, "started_at must be a non-empty string"),
    ],
)
def test_worker_manifest_rejects_invalid_fields(overrides, match):
    with pytest.raises(WorkerManifestError, match=match):
        _manifest(**overrides)


def test_worker_manifest_from_dict_rejects_unknown_fields():
    payload = _manifest().to_dict()
    payload["extra"] = "unexpected"

    with pytest.raises(WorkerManifestError, match="Unknown"):
        WorkerManifest.from_dict(payload)


def test_worker_manifest_from_dict_rejects_missing_fields():
    payload = _manifest().to_dict()
    del payload["worker_id"]

    with pytest.raises(WorkerManifestError, match="Missing"):
        WorkerManifest.from_dict(payload)


def test_worker_manifest_from_dict_rejects_unsupported_kind():
    payload = _manifest().to_dict()
    payload["kind"] = "something_else"

    with pytest.raises(WorkerManifestError, match="Unsupported worker workspace manifest"):
        WorkerManifest.from_dict(payload)


def test_worker_manifest_from_dict_rejects_unsupported_schema_version():
    payload = _manifest().to_dict()
    payload["schema_version"] = 2

    with pytest.raises(WorkerManifestError, match="Unsupported worker workspace manifest"):
        WorkerManifest.from_dict(payload)


def test_worker_manifest_from_dict_rejects_a_non_object_root():
    with pytest.raises(WorkerManifestError, match="must be a JSON object"):
        WorkerManifest.from_dict([1, 2, 3])
