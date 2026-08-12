"""Tests for worker workspace discovery and explicitly-confirmed removal."""

from __future__ import annotations

from pathlib import Path

import pytest

import runforge.execution.sources as sources_module
from runforge.execution.errors import WorkerError
from runforge.execution.sources import _write_worker_manifest
from runforge.execution.workspace_cleanup import (
    WorkspaceCleanupError,
    default_workspace_root,
    discover_workspaces,
    remove_workspace,
)
from runforge.infrastructure.claims import ExperimentClaim
from tests.support import create_git_repository, git


CLAIM = ExperimentClaim(token="t", owner="node7:1", acquired_at="2026-07-30T18:15:30.000000Z")


def test_discover_workspaces_finds_only_matching_directories(tmp_path):
    (tmp_path / "runforge-worker-repo_A-865dc691-20260730T181530Z-p1-aaaa").mkdir()
    (tmp_path / "runforge-worker-repo_B-97488696-20260730T182011Z-p2-bbbb").mkdir()
    (tmp_path / "unrelated-directory").mkdir()
    (tmp_path / "runforge-worker-not-a-directory.txt").write_text("x", encoding="utf-8")

    discovered = discover_workspaces(tmp_path)

    assert [workspace.path.name for workspace in discovered] == [
        "runforge-worker-repo_A-865dc691-20260730T181530Z-p1-aaaa",
        "runforge-worker-repo_B-97488696-20260730T182011Z-p2-bbbb",
    ]


def test_discover_workspaces_excludes_a_symlinked_candidate(tmp_path):
    real_target = tmp_path / "elsewhere" / "runforge-worker-repo_A-865dc691-20260730T181530Z-p1-aaaa"
    real_target.mkdir(parents=True)
    linked = tmp_path / "runforge-worker-repo_B-97488696-20260730T182011Z-p2-bbbb"
    linked.symlink_to(real_target, target_is_directory=True)

    discovered = discover_workspaces(tmp_path)

    assert discovered == ()


def test_discover_workspaces_reports_a_readable_manifest(tmp_path):
    workspace = tmp_path / "runforge-worker-repo_A-865dc691-20260730T181530Z-p1-aaaa"
    workspace.mkdir()
    _write_worker_manifest(
        workspace,
        source_kind="git",
        source_identity="/repositories/repo_A",
        source_reference="a" * 40,
        claim=CLAIM,
    )

    (discovered,) = discover_workspaces(tmp_path)

    assert discovered.manifest is not None
    assert discovered.manifest.source_identity == "/repositories/repo_A"
    assert discovered.manifest_error is None


def test_discover_workspaces_reports_a_missing_manifest_without_failing(tmp_path):
    workspace = tmp_path / "runforge-worker-legacy-865dc691-20260730T181530Z-p1-aaaa"
    workspace.mkdir()

    (discovered,) = discover_workspaces(tmp_path)

    assert discovered.manifest is None
    assert "Could not read JSON object" in discovered.manifest_error


def test_discover_workspaces_rejects_a_missing_root(tmp_path):
    with pytest.raises(WorkspaceCleanupError, match="does not exist"):
        discover_workspaces(tmp_path / "does-not-exist")


def test_discover_workspaces_rejects_a_non_directory_root(tmp_path):
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(WorkspaceCleanupError, match="is not a directory"):
        discover_workspaces(not_a_directory)


def test_remove_workspace_deletes_a_plain_directory(tmp_path):
    workspace = tmp_path / "runforge-worker-source_A-nongit-4f82a1c0-20260730T182011Z-p1-aaaa"
    workspace.mkdir()
    (workspace / "source").mkdir()

    remove_workspace(workspace)

    assert not workspace.exists()


def test_remove_workspace_reports_a_clear_error_for_a_missing_path(tmp_path):
    with pytest.raises(WorkerError, match="Could not remove worker workspace"):
        remove_workspace(tmp_path / "does-not-exist")


def test_remove_workspace_deregisters_a_git_worktree(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('ok')\n"})
    worktree = tmp_path / "runforge-worker-repository-865dc691-20260730T181530Z-p1-aaaa"
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "worktree", "add", "--detach", str(worktree), commit)
    assert worktree.is_dir()

    remove_workspace(worktree)

    assert not worktree.exists()
    assert "runforge-worker-" not in git(repository, "worktree", "list", "--porcelain")


def test_default_workspace_root_uses_the_configured_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(sources_module.WORKER_ROOT_VARIABLE, str(tmp_path))

    assert default_workspace_root() == tmp_path.resolve()


def test_default_workspace_root_falls_back_to_the_current_directory(monkeypatch):
    monkeypatch.delenv(sources_module.WORKER_ROOT_VARIABLE, raising=False)

    assert default_workspace_root() == Path(".")
