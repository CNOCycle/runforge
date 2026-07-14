"""Tests for the standalone Git operations."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from runforge.git_ops import GitOperationError, GitRepository


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "staged.txt").write_text("base\n", encoding="utf-8")
    (repository / "unstaged.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "staged.txt", "unstaged.txt")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_discover_resolves_repository_head_and_branch_from_a_nested_path(tmp_path):
    repository_path = _repository(tmp_path)
    nested_path = repository_path / "nested" / "path"
    nested_path.mkdir(parents=True)

    repository = GitRepository.discover(nested_path)
    head = repository.head()

    assert repository.root == repository_path.resolve()
    assert head.commit == _git(repository_path, "rev-parse", "HEAD")
    assert head.branch == _git(repository_path, "branch", "--show-current")


def test_discover_rejects_non_git_path_and_repository_without_head(tmp_path):
    non_git_path = tmp_path / "not-a-repository"
    non_git_path.mkdir()
    with pytest.raises(GitOperationError, match="find Git repository"):
        GitRepository.discover(non_git_path)

    empty_repository = tmp_path / "empty-repository"
    empty_repository.mkdir()
    _git(empty_repository, "init")
    assert GitRepository.locate(empty_repository).root == empty_repository.resolve()
    with pytest.raises(GitOperationError, match="resolve Git commit"):
        GitRepository.discover(empty_repository)


def test_tracked_patch_captures_staged_and_unstaged_changes_and_lists_untracked(tmp_path):
    repository_path = _repository(tmp_path)
    (repository_path / "staged.txt").write_text("staged change\n", encoding="utf-8")
    _git(repository_path, "add", "staged.txt")
    (repository_path / "unstaged.txt").write_text("unstaged change\n", encoding="utf-8")
    (repository_path / "untracked.txt").write_text("do not read me\n", encoding="utf-8")
    (repository_path / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (repository_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")

    repository = GitRepository.discover(repository_path)
    patch = repository.tracked_patch().decode("utf-8")

    assert "+staged change" in patch
    assert "+unstaged change" in patch
    assert repository.untracked_files() == [".gitignore", "untracked.txt"]


def test_worktree_patch_operations_and_forced_cleanup(tmp_path):
    repository_path = _repository(tmp_path)
    repository = GitRepository.discover(repository_path)
    head = repository.head()
    (repository_path / "staged.txt").write_text("patched\n", encoding="utf-8")
    patch_path = tmp_path / "change.patch"
    patch_path.write_bytes(repository.tracked_patch())
    _git(repository_path, "checkout", "--", "staged.txt")
    repository.check_patch_at_commit(head.commit, patch_path.read_bytes())
    worktree_path = tmp_path / "worktree"

    worktree = repository.create_detached_worktree(worktree_path, head.commit)
    repository.check_patch(worktree, patch_path)
    repository.apply_patch(worktree, patch_path)

    assert (worktree / "staged.txt").read_text(encoding="utf-8") == "patched\n"
    assert GitRepository.discover(worktree).head().branch == "detached"

    repository.remove_worktree(worktree)
    assert not worktree.exists()
    assert str(worktree) not in _git(repository_path, "worktree", "list", "--porcelain")


def test_patch_operations_report_invalid_patch_and_cleanup_still_works(tmp_path):
    repository_path = _repository(tmp_path)
    repository = GitRepository.discover(repository_path)
    worktree = repository.create_detached_worktree(tmp_path / "worktree", repository.head().commit)
    invalid_patch = tmp_path / "invalid.patch"
    invalid_patch.write_text("not a patch\n", encoding="utf-8")

    with pytest.raises(GitOperationError, match="check Git patch"):
        repository.check_patch(worktree, invalid_patch)

    repository.remove_worktree(worktree)
    assert not worktree.exists()
