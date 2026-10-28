"""Behavioral checks for holds, lazy refills, and experiment-leaf selection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import runforge.execution.retry as retry_module
import runforge.execution.worker as worker_module
from runforge.execution.discovery import discover_paths
from runforge.execution.errors import ExperimentHeldError, WorkerError
from runforge.execution.scheduling import SchedulingError, set_hold
from runforge.execution.worker import BatchRequest, execute_paths, run_experiment, run_worker
from runforge.infrastructure.claims import release_claim, try_acquire_claim
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


THREE_TASKS = 3
COMPLETION_QUOTA = 2
SECOND_ATTEMPT = 2
CLI_ERROR_EXIT = 2


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
    assert run_worker(tmp_path / "reports").held == 1
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


def test_worker_refills_and_reconsiders_released_work_without_interrupting_active_task(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    first = _plan(tmp_path, repository, "a-first")
    second = _plan(tmp_path, repository, "b-held")
    order = []
    third = None

    def progress(event):
        nonlocal third
        if event.phase == "executing":
            order.append(event.experiment)
        if event.phase == "executing" and event.experiment == first:
            set_hold(second, held=True)
            third = _plan(tmp_path, repository, "c-published")
        if event.phase == "completed" and event.experiment == third:
            set_hold(second, held=False)

    result = run_worker(tmp_path / "reports", progress=progress)

    assert order == [first, third, second]
    assert result.completed == THREE_TASKS
    assert result.failed == 0
    assert result.held == 0
    assert all(ExperimentDirectory.resolve(path).load_status().attempt == 1 for path in order)


def test_successful_completion_quota_spans_queue_refills(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    first = _plan(tmp_path, repository, "first")
    published = []

    def progress(event):
        if event.phase == "completed" and event.experiment == first:
            published.extend(_plan(tmp_path, repository, name) for name in ("new-one", "new-two"))

    result = run_worker(tmp_path / "reports", max_tasks=2, progress=progress)

    assert result.completed == COMPLETION_QUOTA
    assert result.deferred == 1
    assert sum(ExperimentDirectory.resolve(path).load_status().state == "created" for path in published) == 1


def test_overlapping_paths_execute_one_attempt_and_completed_work_is_skipped(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    experiment = _plan(tmp_path, repository, "one")
    paths = (tmp_path / "reports", experiment, experiment)

    discovery = discover_paths(paths)
    assert [item.path for item in discovery.experiments] == [experiment]
    first = execute_paths(BatchRequest(paths))
    second = execute_paths(BatchRequest(paths))

    assert first.completed == 1
    assert second.completed == 0
    assert second.not_runnable == 1
    assert second.exit_code == 0
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 1


def test_failed_work_does_not_reexecute_on_refill(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "raise SystemExit(7)\n"})
    experiment = _plan(tmp_path, repository, "fails")

    result = run_worker(tmp_path / "reports")

    assert result.selected == result.failed == 1
    assert result.exit_code == 1
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 1


def test_invalid_hold_metadata_is_reported_without_running(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('must not run')\n"})
    experiment = _plan(tmp_path, repository, "invalid")
    (experiment / "schedule.json").write_text('{"held": "false"}\n')

    result = run_worker(tmp_path / "reports")

    assert result.invalid == 1
    assert result.exit_code == CLI_ERROR_EXIT
    assert result.selected == 0
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 0


def test_forced_root_retry_is_rejected_before_archiving_or_clearing_claims(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "raise SystemExit(7)\n"})
    experiment = _plan(tmp_path, repository, "failed")
    run_experiment(experiment)
    before = (experiment / "status.json").read_bytes()

    with pytest.raises(WorkerError, match="explicit experiment-directory"):
        execute_paths(BatchRequest((tmp_path / "reports",), retry=True, force=True))

    assert (experiment / "status.json").read_bytes() == before
    assert not list(experiment.glob("attempt-*"))


def test_retry_prepares_and_executes_under_one_claim_and_preserves_history(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "raise SystemExit(7)\n"})
    experiment = _plan(tmp_path, repository, "failed")
    run_experiment(experiment)

    result = execute_paths(BatchRequest((experiment,), retry=True))

    assert result.failed == 1
    assert (experiment / "attempt-0001" / "status.snapshot.json").exists()
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == SECOND_ATTEMPT
    assert not (experiment / "claim").exists()


def test_hold_during_selection_is_a_skip_without_failure_or_attempt_increment(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    first = _plan(tmp_path, repository, "a")
    second = _plan(tmp_path, repository, "b")

    def progress(event):
        if event.phase == "executing" and event.experiment == first:
            set_hold(second, held=True)

    result = run_worker(tmp_path / "reports", progress=progress)

    assert result.completed == 1
    assert result.held == 1
    assert result.failed == 0
    assert ExperimentDirectory.resolve(second).load_status().attempt == 0


def test_completed_retry_is_skipped_without_archiving(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    experiment = _plan(tmp_path, repository, "done")
    layout = ExperimentDirectory.resolve(experiment)
    layout.save_status(replace(layout.load_status(), state="completed"))

    result = execute_paths(BatchRequest((experiment,), retry=True))

    assert result.selected == 0
    assert result.not_runnable == 1
    assert not list(experiment.glob("attempt-*"))


def test_retry_skips_a_peer_result_changed_after_discovery_before_claim(tmp_path, monkeypatch):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "raise SystemExit(7)\n"})
    experiment = _plan(tmp_path, repository, "failed")
    run_experiment(experiment)
    layout = ExperimentDirectory.resolve(experiment)
    original = retry_module.try_acquire_claim

    def peer_completes_before_claim(current):
        current.save_status(replace(current.load_status(), state="completed"))
        return original(current)

    monkeypatch.setattr(retry_module, "try_acquire_claim", peer_completes_before_claim)

    result = execute_paths(BatchRequest((experiment,), retry=True))

    assert result.stale_skipped == 1
    assert result.failed == 0
    assert layout.load_status().attempt == 1
    assert not list(experiment.glob("attempt-*"))
    assert not layout.claim.exists()


def test_invalid_scheduling_edit_is_not_reported_as_an_executed_task_failure(tmp_path):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    first = _plan(tmp_path, repository, "a-first")
    second = _plan(tmp_path, repository, "b-invalidated")

    def progress(event):
        if event.phase == "executing" and event.experiment == first:
            (second / "schedule.json").write_text('{"held": "invalid"}\n')

    result = run_worker(tmp_path / "reports", progress=progress)

    assert result.invalid == 1
    assert result.failed == 0
    assert result.selected == result.completed == 1
    assert result.exit_code == CLI_ERROR_EXIT
    assert ExperimentDirectory.resolve(second).load_status().attempt == 0


def test_retry_claim_error_with_unreadable_status_does_not_abort_other_work(tmp_path, monkeypatch):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "raise SystemExit(7)\n"})
    broken = _plan(tmp_path, repository, "a-broken")
    following = _plan(tmp_path, repository, "b-following")
    run_experiment(broken)
    run_experiment(following)
    acquire = worker_module.acquire_retry_claim

    def corrupt_status_before_error(layout, *, force=False):
        if layout.root == broken:
            layout.status_file.write_text("{unreadable status\n")
            raise retry_module.RetryError("could not prepare retry claim")
        return acquire(layout, force=force)

    monkeypatch.setattr(worker_module, "acquire_retry_claim", corrupt_status_before_error)

    result = execute_paths(BatchRequest((tmp_path / "reports",), retry=True))

    assert result.invalid == 1
    assert result.failed == result.selected == 1
    assert ExperimentDirectory.resolve(following).load_status().attempt == SECOND_ATTEMPT
    assert not list(broken.glob("attempt-*"))


@pytest.mark.parametrize(
    ("root_selection", "expected_scans", "max_control_reads"),
    [(False, 1, 2), (True, 2, 3)],
)
def test_explicit_leaves_avoid_refill_and_hold_reads_are_bounded(
    tmp_path, monkeypatch, root_selection, expected_scans, max_control_reads
):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    experiment = _plan(tmp_path, repository, "one")
    set_hold(experiment, held=False)
    scans = []
    control_reads = 0
    discover = worker_module.discover_paths
    read_text = Path.read_text

    def record_discovery(*args, **kwargs):
        scans.append(args[0])
        return discover(*args, **kwargs)

    def record_read(path, *args, **kwargs):
        nonlocal control_reads
        if path == experiment / "schedule.json":
            control_reads += 1
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(worker_module, "discover_paths", record_discovery)
    monkeypatch.setattr(Path, "read_text", record_read)
    target = tmp_path / "reports" if root_selection else experiment

    result = execute_paths(BatchRequest((target,)))

    assert result.completed == 1
    assert len(scans) == expected_scans
    assert control_reads <= max_control_reads
