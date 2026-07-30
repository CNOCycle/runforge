"""Tests for the finite single-snapshot worker API."""

from __future__ import annotations

import multiprocessing
from dataclasses import replace
from pathlib import Path

import pytest

import runforge.execution.worker as worker_module
from runforge.execution.worker import WorkerResult, run_worker
from runforge.infrastructure.claims import try_acquire_claim
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


EXPECTED_TASKS = 2


def _plan(tmp_path: Path, name: str, script: str):
    repository = create_git_repository(tmp_path / f"repository-{name}", {"train.py": script})
    return plan_experiment(
        PlanRequest(
            name=name,
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )


def test_worker_result_skipped_total_is_derived_from_its_reasons():
    result = WorkerResult(
        root=Path("reports"),
        candidates=6,
        selected=6,
        completed=1,
        failed=0,
        not_runnable=2,
        claim_contended=3,
        stale_skipped=4,
        deferred=0,
        invalid=0,
    )

    assert result.skipped == 2 + 3 + 4


def test_worker_consumes_one_snapshot_and_applies_task_budget(tmp_path):
    _plan(tmp_path, "first", "print('first')\n")
    _plan(tmp_path, "second", "print('second')\n")

    result = run_worker(tmp_path / "reports", max_tasks=1)

    assert result.candidates == EXPECTED_TASKS
    assert result.selected == 1
    assert result.completed == 1
    assert result.failed == 0
    assert result.skipped == 0
    assert result.not_runnable == 0
    assert result.claim_contended == 0
    assert result.stale_skipped == 0
    assert result.deferred == 1
    assert result.invalid == 0


@pytest.mark.parametrize("max_tasks", [0, -1])
def test_worker_rejects_nonpositive_task_budget(tmp_path, max_tasks):
    with pytest.raises(worker_module.WorkerError, match="positive integer"):
        run_worker(tmp_path / "reports", max_tasks=max_tasks)


def test_worker_skips_an_experiment_claimed_by_another_worker(tmp_path):
    experiment = _plan(tmp_path, "claimed", "print('must not run')\n")
    claim = try_acquire_claim(ExperimentDirectory.resolve(experiment), owner="other-worker")
    assert claim is not None

    result = run_worker(tmp_path / "reports")

    assert result.candidates == 1
    assert result.selected == 1
    assert result.completed == 0
    assert result.failed == 0
    assert result.skipped == 1
    assert result.not_runnable == 0
    assert result.claim_contended == 1
    assert result.stale_skipped == 0


def _worker_process(root: Path, barrier, results) -> None:
    barrier.wait()
    result = run_worker(root)
    results.put((result.completed, result.failed, result.skipped))


def test_two_process_workers_execute_one_experiment_at_most_once(tmp_path):
    repository = create_git_repository(
        tmp_path / "repository-race",
        {
            "train.py": (
                "import os\n"
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(0.2)\n"
                "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('ran.txt').write_text('ran')\n"
            )
        },
    )
    experiment = plan_experiment(
        PlanRequest(
            name="race",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )

    context = multiprocessing.get_context()
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_worker_process, args=(tmp_path / "reports", barrier, results)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)

    assert all(process.exitcode == 0 for process in processes)
    observed = sorted(results.get(timeout=5) for _ in processes)
    assert observed == [(0, 0, 1), (1, 0, 0)]
    assert (experiment / "artifacts" / "ran.txt").read_text(encoding="utf-8") == "ran"


def test_worker_skips_experiment_that_becomes_non_runnable_after_claim(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    first = plan_experiment(
        PlanRequest(
            name="first",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )
    second = plan_experiment(
        PlanRequest(
            name="second",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )

    def complete_second_before_it_runs(event):
        if event.phase == "preparing" and event.experiment == first:
            layout = ExperimentDirectory.resolve(second)
            layout.save_status(replace(layout.load_status(), state="completed"))

    result = run_worker(tmp_path / "reports", progress=complete_second_before_it_runs)

    assert result.completed == 1
    assert result.failed == 0
    assert result.skipped == 1
    assert result.not_runnable == 0
    assert result.claim_contended == 0
    assert result.stale_skipped == 1
    assert not (second / "claim").exists()


def test_worker_reports_claim_error_through_progress_and_continues(tmp_path, monkeypatch, capsys):
    _plan(tmp_path, "claim-error", "print('first')\n")
    _plan(tmp_path, "runs-after-claim-error", "print('second')\n")
    original = worker_module.try_acquire_claim
    calls = 0

    def fail_first(layout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise worker_module.ClaimError("permission denied")
        return original(layout)

    monkeypatch.setattr(worker_module, "try_acquire_claim", fail_first)
    events = []
    result = run_worker(tmp_path / "reports", progress=events.append)

    assert result.completed == 1
    assert result.failed == 1
    assert result.skipped == 0
    assert capsys.readouterr().err == ""
    warning = next(event for event in events if event.phase == "warning")
    assert warning.error == "Could not acquire claim: permission denied"
