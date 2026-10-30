"""Tests for the worker workspace cleanup command."""

from __future__ import annotations

from pathlib import Path

from runforge.cli import main
from runforge.execution.sources import WORKSPACE_ROOT_VARIABLE, _write_worker_manifest
from runforge.infrastructure.claims import ExperimentClaim
from tests.support import create_git_repository, git


CLI_ERROR_EXIT = 2
CLAIM = ExperimentClaim(token="t", owner="node7:1", acquired_at="2026-07-30T18:15:30.000000Z")


def _snapshot_workspace(tmp_path: Path, name: str) -> Path:
    workspace = tmp_path / name
    workspace.mkdir()
    (workspace / "source").mkdir()
    _write_worker_manifest(
        workspace,
        source_kind="directory-snapshot",
        source_identity="/sources/source_A",
        source_reference="b" * 64,
        claim=CLAIM,
    )
    return workspace


def test_worker_cleanup_lists_nothing_for_an_empty_root(tmp_path, capsys):
    assert main(["workspaces", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "RunForge workspaces effective arguments:" in output
    assert f"  root: {tmp_path.resolve()}" in output
    assert "  confirm: not set (list only)" in output
    assert "Worker workspaces found (0):" in output
    assert "  none" in output


def test_worker_cleanup_lists_without_removing_by_default(tmp_path, capsys):
    workspace = _snapshot_workspace(tmp_path, "project-00000000000000000000000000000002")

    assert main(["workspaces", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "Worker workspaces found (1):" in output
    assert workspace.name in output
    assert "directory-snapshot" in output
    assert "/sources/source_A" in output
    assert workspace.exists()


def test_worker_cleanup_removes_only_the_confirmed_workspace(tmp_path, capsys):
    keep = _snapshot_workspace(tmp_path, "project-00000000000000000000000000000002")
    remove = _snapshot_workspace(tmp_path, "project-00000000000000000000000000000003")

    assert main(["workspaces", str(tmp_path), "--remove", str(remove)]) == 0

    output = capsys.readouterr().out
    assert f"Removed: {remove.resolve()}" in output
    assert keep.exists()
    assert not remove.exists()


def test_worker_cleanup_deregisters_a_confirmed_git_worktree(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('ok')\n"})
    worktree = tmp_path / "project-00000000000000000000000000000001"
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "worktree", "add", "--detach", str(worktree), commit)
    _write_worker_manifest(
        worktree, source_kind="git", source_identity=str(repository), source_reference=commit, claim=CLAIM
    )

    assert main(["workspaces", str(tmp_path), "--remove", str(worktree)]) == 0

    assert not worktree.exists()
    assert str(worktree) not in git(repository, "worktree", "list", "--porcelain")


def test_worker_cleanup_rejects_a_confirm_path_discovery_did_not_find(tmp_path, capsys):
    kept = _snapshot_workspace(tmp_path, "project-00000000000000000000000000000002")
    unrelated = tmp_path / "not-discovered"
    unrelated.mkdir()

    assert main(["workspaces", str(tmp_path), "--remove", str(unrelated)]) == CLI_ERROR_EXIT

    assert "--remove named a path that discovery did not find" in capsys.readouterr().err
    assert kept.exists()
    assert unrelated.exists()


def test_worker_cleanup_rejects_a_missing_root(tmp_path, capsys):
    assert main(["workspaces", str(tmp_path / "does-not-exist")]) == CLI_ERROR_EXIT

    assert "does not exist" in capsys.readouterr().err


def test_worker_cleanup_defaults_the_root_to_the_configured_variable(tmp_path, monkeypatch, capsys):
    workspace = _snapshot_workspace(tmp_path, "project-00000000000000000000000000000002")
    monkeypatch.setenv(WORKSPACE_ROOT_VARIABLE, str(tmp_path))

    assert main(["workspaces"]) == 0

    output = capsys.readouterr().out
    assert f"  root: {tmp_path.resolve()}" in output
    assert workspace.name in output
