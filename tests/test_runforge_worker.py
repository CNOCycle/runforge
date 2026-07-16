"""Tests for the RunForge experiment execution worker."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.json_store import load_json_object
from runforge.planner import PlanRequest, plan_experiment
from runforge.source_metadata import PinnedGitSource
from runforge.worker import WorkerError, WorkerProgressEvent, run_experiment


FAILURE_EXIT_CODE = 7


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, script: str) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "train.py").write_text(script, encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_worker_executes_recorded_commit_and_patch_then_cleans_worktree(tmp_path):
    repository = _repository(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('result.txt').write_text('base')\n"
        ),
    )
    train = repository / "train.py"
    train.write_text(train.read_text(encoding="utf-8").replace("'base'", "'patched'"), encoding="utf-8")
    experiment = plan_experiment(
        PlanRequest(
            name="patched",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )
    train.write_text(train.read_text(encoding="utf-8").replace("'patched'", "'later'"), encoding="utf-8")

    assert run_experiment(experiment) == 0

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "patched"
    assert status.state == "completed"
    assert status.attempt == 1
    assert status.exit_code == 0
    assert (experiment / "stdout.log").is_file()
    assert (experiment / "stderr.log").is_file()
    assert "runforge-worker-" not in _git(repository, "worktree", "list", "--porcelain")


def test_worker_executes_pinned_commit_after_current_checkout_advances(tmp_path):
    repository = _repository(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('result.txt').write_text('first')\n"
        ),
    )
    first_commit = _git(repository, "rev-parse", "HEAD")
    train = repository / "train.py"
    train.write_text(train.read_text(encoding="utf-8").replace("'first'", "'second'"), encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "second")
    experiment = plan_experiment(
        PlanRequest(
            name="pinned",
            command=ExperimentCommand.argv(("python", "train.py")),
            output_root=tmp_path / "reports",
            source=PinnedGitSource(repository=repository, commit=first_commit),
        )
    )

    configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))

    assert experiment.parent.name == "pinned"
    assert configuration.source.commit == first_commit
    assert configuration.source.branch == "pinned"
    assert run_experiment(experiment) == 0
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "first"


def test_worker_records_nonzero_command_exit(tmp_path):
    repository = _repository(
        tmp_path,
        "import sys\nprint('out')\nprint('err', file=sys.stderr)\nraise SystemExit(7)\n",
    )
    experiment = plan_experiment(
        PlanRequest(
            name="failure",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )

    assert run_experiment(experiment) == FAILURE_EXIT_CODE

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.error == "Command exited with status 7"
    assert "out" in (experiment / "stdout.log").read_text(encoding="utf-8")
    assert "err" in (experiment / "stderr.log").read_text(encoding="utf-8")


def test_worker_streams_output_to_console_and_preserves_logs(tmp_path, capsys):
    repository = _repository(
        tmp_path,
        "import sys\nprint('live out', flush=True)\nprint('live err', file=sys.stderr, flush=True)\n",
    )
    experiment = plan_experiment(
        PlanRequest(
            name="streamed",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )

    assert run_experiment(experiment, stream_output=True) == 0
    captured = capsys.readouterr()

    assert captured.out == "live out\n"
    assert captured.err == "live err\n"
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "live out\n"
    assert (experiment / "stderr.log").read_text(encoding="utf-8") == "live err\n"


def test_worker_reports_successful_lifecycle_without_changing_its_result(tmp_path):
    repository = _repository(tmp_path, "print('done')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="progress",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )
    events: list[WorkerProgressEvent] = []

    assert run_experiment(experiment, progress=events.append) == 0

    assert [event.phase for event in events] == ["preparing", "executing", "completed"]
    assert all(event.experiment == experiment for event in events)
    assert all(event.stdout_log == experiment / "stdout.log" for event in events)
    assert all(event.stderr_log == experiment / "stderr.log" for event in events)
    assert events[0].command is None
    assert events[1].command == ExperimentCommand.argv(("python", "train.py"))
    assert events[2].exit_code == 0
    assert events[2].error is None


def test_worker_reports_nonzero_exit_and_ignores_progress_callback_errors(tmp_path):
    repository = _repository(tmp_path, "raise SystemExit(7)\n")
    experiment = plan_experiment(
        PlanRequest(
            name="failed-progress",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )
    events: list[WorkerProgressEvent] = []

    def progress(event: WorkerProgressEvent) -> None:
        events.append(event)
        if event.phase == "executing":
            raise RuntimeError("observer failed")

    assert run_experiment(experiment, progress=progress) == FAILURE_EXIT_CODE

    assert [event.phase for event in events] == ["preparing", "executing", "failed"]
    assert events[-1].exit_code == FAILURE_EXIT_CODE
    assert events[-1].error == "Command exited with status 7"
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.exit_code == FAILURE_EXIT_CODE


def test_worker_reports_preparation_failure(tmp_path):
    experiment = tmp_path / "missing"
    events: list[WorkerProgressEvent] = []

    with pytest.raises(WorkerError, match="Experiment directory does not exist"):
        run_experiment(experiment, progress=events.append)

    assert [event.phase for event in events] == ["preparing", "failed"]
    assert events[-1].error == f"Experiment directory does not exist: {experiment}"
