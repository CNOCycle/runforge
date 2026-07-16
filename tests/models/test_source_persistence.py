"""Integration coverage for GitSource metadata and generic JSON persistence."""

from __future__ import annotations

from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.models.source import GitSource


def test_git_source_metadata_round_trips_through_the_generic_json_store(tmp_path):
    source = GitSource(
        repository=tmp_path.resolve(),
        commit="a" * 40,
        branch="feature/runforge",
        patch_file="git.patch",
        patch_sha256="b" * 64,
        untracked_files=("notes.txt",),
    )
    path = tmp_path / "source.json"

    save_json_object(path, source.to_dict())

    assert GitSource.from_dict(load_json_object(path)) == source
