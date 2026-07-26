"""Tests for resolving non-Git verified-directory source requests."""

from __future__ import annotations

from pathlib import Path

import pytest

from runforge.planning.directory_source import DirectorySourceResolutionError, resolve_verified_directory_source


def test_resolve_verified_directory_source_scans_and_normalizes_identity(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "train.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "nested" / "config.json").write_text("{}", encoding="utf-8")

    resolved = resolve_verified_directory_source(source)

    assert resolved.source.path == source.resolve()
    assert resolved.source.tree_digest == resolved.manifest.tree_digest
    assert [entry.path for entry in resolved.manifest.entries] == ["nested/config.json", "train.py"]


def test_resolve_verified_directory_source_rejects_missing_or_non_directory_path(tmp_path):
    with pytest.raises(DirectorySourceResolutionError, match="non-symlink directory"):
        resolve_verified_directory_source(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(DirectorySourceResolutionError, match="non-symlink directory"):
        resolve_verified_directory_source(file_path)


def test_resolve_verified_directory_source_rejects_symlinked_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(DirectorySourceResolutionError, match="non-symlink directory"):
        resolve_verified_directory_source(link)


def test_resolve_verified_directory_source_propagates_scan_errors(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "link.txt").symlink_to(tmp_path / "does-not-exist")

    with pytest.raises(DirectorySourceResolutionError):
        resolve_verified_directory_source(source)


def test_resolve_verified_directory_source_expands_relative_and_user_paths(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resolved = resolve_verified_directory_source(Path("source"))

    assert resolved.source.path == source.resolve()
