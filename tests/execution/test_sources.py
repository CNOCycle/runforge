"""Tests for worker workspace naming and reservation."""

from __future__ import annotations

import os
import re

import pytest

import runforge.execution.sources as sources_module
from runforge.execution.errors import WorkerError


def test_worker_workspace_name_matches_the_git_naming_contract():
    name = sources_module._worker_workspace_name("repo_A", "865dc691", non_git=False)
    assert re.fullmatch(
        rf"runforge-worker-repo_A-865dc691-\d{{8}}T\d{{6}}Z-p{os.getpid()}-[0-9a-f]{{4}}",
        name,
    )


def test_worker_workspace_name_matches_the_non_git_naming_contract():
    name = sources_module._worker_workspace_name("source_A", "4f82a1c0", non_git=True)
    assert re.fullmatch(
        rf"runforge-worker-source_A-nongit-4f82a1c0-\d{{8}}T\d{{6}}Z-p{os.getpid()}-[0-9a-f]{{4}}",
        name,
    )


def test_reserve_worker_workspace_creates_and_returns_the_directory(tmp_path):
    result = sources_module._reserve_worker_workspace(tmp_path, "repo_A", "865dc691", non_git=False)
    assert result.is_dir()
    assert result.parent == tmp_path
    assert re.fullmatch(r"runforge-worker-repo_A-865dc691-.+", result.name)


def test_reserve_worker_workspace_retries_past_a_name_collision(tmp_path, monkeypatch):
    (tmp_path / "runforge-worker-taken").mkdir()
    names = iter(["runforge-worker-taken", "runforge-worker-free"])
    monkeypatch.setattr(sources_module, "_worker_workspace_name", lambda *args, **kwargs: next(names))

    result = sources_module._reserve_worker_workspace(tmp_path, "repo_A", "865dc691", non_git=False)

    assert result == tmp_path / "runforge-worker-free"


def test_reserve_worker_workspace_gives_up_after_repeated_collisions(tmp_path, monkeypatch):
    (tmp_path / "runforge-worker-taken").mkdir()
    monkeypatch.setattr(sources_module, "_worker_workspace_name", lambda *args, **kwargs: "runforge-worker-taken")

    with pytest.raises(WorkerError, match="Could not create a unique worker workspace"):
        sources_module._reserve_worker_workspace(tmp_path, "repo_A", "865dc691", non_git=False)


def test_reserve_worker_workspace_reports_an_unusable_root_clearly(tmp_path):
    with pytest.raises(WorkerError, match="Could not create worker workspace under"):
        sources_module._reserve_worker_workspace(tmp_path / "does-not-exist", "repo_A", "865dc691", non_git=False)
