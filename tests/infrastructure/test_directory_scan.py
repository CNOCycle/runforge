"""Tests for deterministic non-Git directory scanning."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from runforge.infrastructure.directory_scan import DirectoryScanError, capture_directory, scan_directory


_SHA256_HEX_LENGTH = 64


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def test_scan_directory_returns_sorted_files_with_digests(tmp_path):
    root = tmp_path / "source"
    _write(root / "b.txt", "b\n")
    _write(root / "a" / "nested.txt", "nested\n")

    result = scan_directory(root)

    assert [entry.path for entry in result.files] == ["a/nested.txt", "b.txt"]
    assert all(len(entry.sha256) == _SHA256_HEX_LENGTH for entry in result.files)
    assert len(result.tree_digest) == _SHA256_HEX_LENGTH


def test_scan_directory_captures_executable_bit(tmp_path):
    root = tmp_path / "source"
    _write(root / "run.sh", "#!/bin/sh\necho hi\n", executable=True)
    _write(root / "data.txt", "value\n")

    result = scan_directory(root)
    by_path = {entry.path: entry for entry in result.files}

    assert by_path["run.sh"].executable is True
    assert by_path["data.txt"].executable is False


def test_scan_directory_always_ignores_git_directories_at_any_depth(tmp_path):
    root = tmp_path / "source"
    _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(root / "vendor" / ".git" / "config", "[core]\n")
    _write(root / "keep.txt", "keep\n")

    result = scan_directory(root)

    assert [entry.path for entry in result.files] == ["keep.txt"]


def test_scan_directory_applies_gitignore_basename_and_path_patterns(tmp_path):
    root = tmp_path / "source"
    _write(root / ".gitignore", "*.log\nbuild/\n# comment\n\n")
    _write(root / "keep.txt", "keep\n")
    _write(root / "debug.log", "noisy\n")
    _write(root / "build" / "output.bin", "binary\n")
    _write(root / "nested" / "more.log", "noisy\n")

    result = scan_directory(root)

    assert [entry.path for entry in result.files] == [".gitignore", "keep.txt"]


def test_scan_directory_can_reject_gitignore_excluded_files(tmp_path):
    root = tmp_path / "source"
    _write(root / ".gitignore", "scratch/\n")
    _write(root / "scratch" / "generated.txt", "generated\n")

    with pytest.raises(DirectoryScanError, match="excluded by [.]gitignore"):
        scan_directory(root, reject_ignored=True)


def test_scan_directory_does_not_give_runforgeignore_special_meaning(tmp_path):
    root = tmp_path / "source"
    _write(root / ".runforgeignore", "*.log\n")
    _write(root / "debug.log", "keep this file\n")

    result = scan_directory(root)

    assert [entry.path for entry in result.files] == [".runforgeignore", "debug.log"]


def test_scan_directory_rejects_missing_or_non_directory_root(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(DirectoryScanError, match="does not exist"):
        scan_directory(missing)

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(DirectoryScanError, match="not a directory"):
        scan_directory(file_path)


def test_scan_directory_rejects_symlink_files_and_directories(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside\n", encoding="utf-8")
    (root / "link.txt").symlink_to(target)

    with pytest.raises(DirectoryScanError, match="symbolic link"):
        scan_directory(root)

    (root / "link.txt").unlink()
    (root / "link-dir").symlink_to(tmp_path)
    with pytest.raises(DirectoryScanError, match="symbolic link"):
        scan_directory(root)


def test_scan_directory_rejects_special_files(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    fifo_path = root / "pipe"
    os.mkfifo(fifo_path)
    with pytest.raises(DirectoryScanError, match="unsupported file type"):
        scan_directory(root)
    fifo_path.unlink()

    socket_path = root / "socket"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        unix_socket.bind(str(socket_path))
        with pytest.raises(DirectoryScanError, match="unsupported file type"):
            scan_directory(root)
    finally:
        unix_socket.close()


def test_scan_directory_root_is_excluded_from_its_own_relative_paths(tmp_path):
    root = tmp_path / "source"
    _write(root / "file.txt", "value\n")

    result = scan_directory(root)

    assert result.root == root.resolve()
    assert result.files[0].path == "file.txt"


def test_scan_directory_digest_is_independent_of_traversal_order_and_timestamps(tmp_path):
    first = tmp_path / "first"
    _write(first / "a.txt", "a\n")
    _write(first / "z" / "b.txt", "b\n")
    os.utime(first / "a.txt", (1_000_000, 1_000_000))
    os.utime(first / "z" / "b.txt", (1_000_000, 1_000_000))

    second = tmp_path / "second"
    _write(second / "z" / "b.txt", "b\n")
    _write(second / "a.txt", "a\n")
    os.utime(second / "a.txt", (2_000_000, 2_000_000))
    os.utime(second / "z" / "b.txt", (2_000_000, 2_000_000))

    assert scan_directory(first).tree_digest == scan_directory(second).tree_digest


def test_scan_directory_digest_changes_when_content_changes(tmp_path):
    root = tmp_path / "source"
    _write(root / "a.txt", "a\n")
    before = scan_directory(root).tree_digest

    _write(root / "a.txt", "changed\n")
    after = scan_directory(root).tree_digest

    assert before != after


def test_capture_directory_copies_content_and_preserves_executable_bit(tmp_path):
    root = tmp_path / "source"
    _write(root / "nested" / "data.txt", "value\n")
    _write(root / "run.sh", "#!/bin/sh\necho hi\n", executable=True)
    destination = tmp_path / "capture"

    result = capture_directory(root, destination)

    assert (destination / "nested" / "data.txt").read_text(encoding="utf-8") == "value\n"
    assert (destination / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"
    assert (destination / "run.sh").stat().st_mode & 0o111
    assert not (destination / "nested" / "data.txt").stat().st_mode & 0o111
    assert [entry.path for entry in result.files] == ["nested/data.txt", "run.sh"]
    assert result.root == root.resolve()


def test_capture_directory_matches_scan_directory_identity(tmp_path):
    root = tmp_path / "source"
    _write(root / "a.txt", "a\n")
    _write(root / "b" / "c.txt", "c\n")
    destination = tmp_path / "capture"

    scanned = scan_directory(root)
    captured = capture_directory(root, destination)

    assert captured.tree_digest == scanned.tree_digest
    assert captured.files == scanned.files


def test_capture_directory_rejects_existing_destination(tmp_path):
    root = tmp_path / "source"
    _write(root / "a.txt", "a\n")
    destination = tmp_path / "capture"
    destination.mkdir()

    with pytest.raises(DirectoryScanError, match="already exists"):
        capture_directory(root, destination)


def test_capture_directory_applies_ignore_rules_and_rejects_special_files(tmp_path):
    root = tmp_path / "source"
    _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(root / "keep.txt", "keep\n")
    destination = tmp_path / "capture"

    result = capture_directory(root, destination)

    assert [entry.path for entry in result.files] == ["keep.txt"]
    assert not (destination / ".git").exists()

    other_root = tmp_path / "special-source"
    other_root.mkdir()
    (other_root / "link.txt").symlink_to(tmp_path / "missing")
    with pytest.raises(DirectoryScanError, match="symbolic link"):
        capture_directory(other_root, tmp_path / "other-capture")
