"""Behavioral checks for claim-coordinated scheduling holds."""

from __future__ import annotations

import pytest

from runforge.execution.errors import ExperimentHeldError
from runforge.execution.scheduling import SchedulingError, set_hold
from runforge.execution.worker import run_experiment
from runforge.infrastructure.claims import release_claim, try_acquire_claim
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


def _plan(root, repository, name, *, command=None):
    return plan_experiment(
        PlanRequest(
            name=name,
            source_path=repository,
            output_root=root / "reports",
            command=command or ExperimentCommand.argv(("python", "train.py")),
        )
    )


def test_hold_preserves_config_status_and_prevents_exact_execution(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    experiment = _plan(tmp_path, repository, "held")
    before = {name: (experiment / name).read_bytes() for name in ("config.json", "status.json")}

    set_hold(experiment, held=True)
    with pytest.raises(ExperimentHeldError, match="held"):
        run_experiment(experiment)

    assert {name: (experiment / name).read_bytes() for name in before} == before
    assert not (experiment / "claim").exists()
    set_hold(experiment, held=False)
    assert run_experiment(experiment) == 0


def test_scheduling_cannot_edit_or_release_an_active_execution_claim(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    experiment = _plan(tmp_path, repository, "busy")
    layout = ExperimentDirectory.resolve(experiment)
    claim = try_acquire_claim(layout)
    assert claim is not None

    try:
        for held in (True, False):
            with pytest.raises(SchedulingError, match="already claimed"):
                set_hold(experiment, held=held)
        assert layout.load_status().state == "created"
        assert not (experiment / "schedule.json").exists()
        assert layout.claim.exists()
    finally:
        release_claim(layout, claim)
