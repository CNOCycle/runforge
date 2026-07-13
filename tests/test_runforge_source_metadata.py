"""Focused tests for Stage 1A.3 current-HEAD Git source metadata."""

from __future__ import annotations

import pytest

from runforge.source_metadata import GitSource, SourceMetadataError


def test_git_source_round_trip_preserves_commit_branch_patch_and_untracked_files(tmp_path):
    source = GitSource(
        repository=tmp_path.resolve(),
        commit="a" * 40,
        branch="feature/runforge",
        patch_file="git.patch",
        patch_sha256="b" * 64,
        untracked_files=("notes.txt", "scratch/data.csv"),
    )

    payload = source.to_dict()

    assert GitSource.from_dict(payload) == source
    assert payload["schema_version"] == 1
    assert payload["patch"] == {"file": "git.patch", "sha256": "b" * 64}


@pytest.mark.parametrize(
    "factory, match",
    [
        (lambda path: GitSource(repository=path, commit="short", branch="main"), "full Git object ID"),
        (
            lambda path: GitSource(repository=path, commit="a" * 40, branch="main", patch_file="git.patch"),
            "supplied together",
        ),
        (
            lambda path: GitSource(
                repository=path,
                commit="a" * 40,
                branch="main",
                patch_file="nested/git.patch",
                patch_sha256="b" * 64,
            ),
            "filename",
        ),
    ],
)
def test_git_source_rejects_invalid_metadata(tmp_path, factory, match):
    with pytest.raises(SourceMetadataError, match=match):
        factory(tmp_path.resolve())


def test_git_source_rejects_unknown_or_unsupported_serialized_metadata(tmp_path):
    source = GitSource(repository=tmp_path.resolve(), commit="a" * 40, branch="main")
    payload = source.to_dict()
    payload["future_field"] = True
    with pytest.raises(SourceMetadataError, match="Unknown Git source field"):
        GitSource.from_dict(payload)

    payload = source.to_dict()
    payload["schema_version"] = 2
    with pytest.raises(SourceMetadataError, match="Unsupported Git source schema version"):
        GitSource.from_dict(payload)
