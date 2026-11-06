"""Tests for removing a workdir an interrupted execution left inside an experiment."""

from __future__ import annotations

import shlex

import pytest

import runforge.execution.sources as sources_module
from runforge.execution.errors import WorkerError
from runforge.infrastructure.git import GitOperationError, WorktreeRegistration
from runforge.infrastructure.storage import ExperimentDirectory


class _Repository:
    """Records removal requests against one configurable worktree registration."""

    root = "/repositories/repo A;$(touch pwned)"

    def __init__(self, *, registered: bool = True, locked: bool = False, removal_fails: bool = False) -> None:
        self.registered = registered
        self.locked = locked
        self.removal_fails = removal_fails
        self.removed = []

    def worktree_registration(self, worktree):
        return WorktreeRegistration(path=worktree, locked=self.locked) if self.registered else None

    def remove_worktree(self, worktree):
        self.removed.append(worktree)
        if self.removal_fails:
            raise GitOperationError("validation failed, cannot remove working tree")


def test_workdir_is_a_hidden_directory_inside_the_experiment(tmp_path):
    experiment = ExperimentDirectory(tmp_path / "exp")
    assert experiment.workspace == tmp_path / "exp" / ".workspace"


def test_removing_an_absent_workdir_is_a_no_op(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    sources_module._remove_leftover_workspace(experiment)
    assert not experiment.workspace.exists()


def test_a_leftover_directory_is_deleted(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    (experiment.workspace / "nested").mkdir(parents=True)
    (experiment.workspace / "nested" / "partial.txt").write_text("partial", encoding="utf-8")

    sources_module._remove_leftover_workspace(experiment)

    assert not experiment.workspace.exists()


def test_a_leftover_file_is_deleted(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    experiment.workspace.write_text("not a directory", encoding="utf-8")

    sources_module._remove_leftover_workspace(experiment)

    assert not experiment.workspace.exists()


@pytest.mark.parametrize("registered", [False, True])
def test_a_leftover_link_is_unlinked_without_touching_its_target(tmp_path, registered):
    target = tmp_path / "target"
    target.mkdir()
    (target / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    experiment = ExperimentDirectory(tmp_path / "exp")
    experiment.root.mkdir()
    experiment.workspace.symlink_to(target, target_is_directory=True)
    repository = _Repository(registered=registered)

    sources_module._remove_leftover_workspace(experiment, repository)

    assert repository.removed == []
    assert not experiment.workspace.is_symlink()
    assert (target / ".git").is_file()


def test_a_registered_leftover_worktree_is_deregistered_through_git(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    experiment.workspace.mkdir()
    repository = _Repository()

    sources_module._remove_leftover_workspace(experiment, repository)

    assert repository.removed == [experiment.workspace]


def test_a_registration_whose_directory_was_deleted_is_still_deregistered(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    repository = _Repository()

    sources_module._remove_leftover_workspace(experiment, repository)

    assert repository.removed == [experiment.workspace]


def test_an_unremovable_unlocked_worktree_falls_back_to_deleting_the_directory(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    experiment.workspace.mkdir()
    (experiment.workspace / ".git").write_text("gitdir: pruned", encoding="utf-8")
    repository = _Repository(removal_fails=True)

    sources_module._remove_leftover_workspace(experiment, repository)

    assert repository.removed == [experiment.workspace]
    assert not experiment.workspace.exists()


@pytest.mark.parametrize("present", [True, False])
def test_a_locked_leftover_worktree_fails_without_being_touched(tmp_path, present):
    experiment = ExperimentDirectory(tmp_path)
    if present:
        experiment.workspace.mkdir()
        (experiment.workspace / "keep.txt").write_text("locked by an operator", encoding="utf-8")
    repository = _Repository(locked=True)

    with pytest.raises(WorkerError, match=r"is a locked Git worktree; .*worktree unlock"):
        sources_module._remove_leftover_workspace(experiment, repository)

    assert repository.removed == []
    if present:
        assert (experiment.workspace / "keep.txt").read_text(encoding="utf-8") == "locked by an operator"


def test_an_unreadable_registration_list_fails_without_deleting(tmp_path):
    experiment = ExperimentDirectory(tmp_path)
    experiment.workspace.mkdir()

    class _Unreadable(_Repository):
        def worktree_registration(self, worktree):
            raise GitOperationError("not a git repository")

    with pytest.raises(WorkerError, match="Could not inspect leftover workdir"):
        sources_module._remove_leftover_workspace(experiment, _Unreadable())

    assert experiment.workspace.is_dir()


def test_a_removal_failure_names_the_workdir(tmp_path, monkeypatch):
    experiment = ExperimentDirectory(tmp_path)
    experiment.workspace.mkdir()

    def refuse(path):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(sources_module.shutil, "rmtree", refuse)
    with pytest.raises(WorkerError, match=r"Could not remove leftover workdir .*\.workspace: read-only filesystem"):
        sources_module._remove_leftover_workspace(experiment)


def test_the_unlock_hint_is_a_shell_safe_command(tmp_path):
    experiment = ExperimentDirectory(tmp_path / "report root with spaces" / "exp $HOME")
    repository = _Repository(locked=True)

    with pytest.raises(WorkerError) as raised:
        sources_module._remove_leftover_workspace(experiment, repository)

    hint = str(raised.value).split(" then run ", 1)[1].removesuffix(" before executing this experiment again")
    assert shlex.split(hint) == ["git", "-C", repository.root, "worktree", "unlock", str(experiment.workspace)]
