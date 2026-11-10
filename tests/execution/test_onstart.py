"""Lock and interrupted-experiment discovery for runforge onstart."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from runforge.execution import onstart
from runforge.execution.onstart import Interrupted, ReportRootLock, find_interrupted
from runforge.infrastructure.claims import ClaimError


def _experiment(path: Path, state: str, *, claimed: bool = False, held: bool = False):
    path.mkdir(parents=True)
    if claimed:
        (path / "claim").mkdir()
    return SimpleNamespace(path=path, status=SimpleNamespace(state=state), held=held)


def _discovery(monkeypatch, experiments):
    monkeypatch.setattr(onstart, "discover_experiments", lambda root: SimpleNamespace(experiments=tuple(experiments)))


def test_a_second_holder_is_refused_until_the_first_releases(tmp_path):
    first = ReportRootLock(tmp_path)
    first.acquire()

    with pytest.raises(ClaimError, match="already holds"):
        ReportRootLock(tmp_path).acquire()

    first.release()
    with ReportRootLock(tmp_path):
        assert (tmp_path / ".onstart.lock").exists()


def test_the_lock_is_released_when_the_block_raises(tmp_path):
    with pytest.raises(RuntimeError), ReportRootLock(tmp_path):
        raise RuntimeError("boom")

    with ReportRootLock(tmp_path):
        pass


def test_only_unheld_inprogress_is_restarted_and_the_rest_is_reported(tmp_path, monkeypatch):
    _discovery(
        monkeypatch,
        [
            _experiment(tmp_path / "a", "inprogress", claimed=True),
            _experiment(tmp_path / "b", "created", claimed=True),
            _experiment(tmp_path / "c", "init"),
            _experiment(tmp_path / "d", "failed"),
            _experiment(tmp_path / "e", "completed"),
            _experiment(tmp_path / "f", "inprogress", claimed=True, held=True),
        ],
    )

    assert find_interrupted(tmp_path) == Interrupted(
        restart=(tmp_path / "a",), held=(tmp_path / "f",), unstarted_claimed=(tmp_path / "b",)
    )
