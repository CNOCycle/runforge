"""Tests for the RunForge experiment execution worker."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import runforge.execution.worker as worker_module
from runforge.execution.worker import WorkerError, WorkerProgressEvent, run_experiment
from runforge.infrastructure.claims import ClaimError, release_claim, try_acquire_claim
from runforge.infrastructure.json_store import load_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.inputs import InputTemplate
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.source import PinnedGitSource
from tests.support import create_git_repository, git


FAILURE_EXIT_CODE = 7


def _repository(tmp_path: Path, script: str) -> Path:
    return create_git_repository(tmp_path / "repository", {"train.py": script})


def _verified_directory_source(tmp_path: Path, script: str) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "train.py").write_text(script, encoding="utf-8")
    return source


def test_executor_rejects_an_already_claimed_experiment(tmp_path):
    repository = _repository(tmp_path, "print('must not run')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="claimed",
            command=ExperimentCommand.argv(("python", "train.py")),
            output_root=tmp_path / "reports",
            source_path=repository,
        )
    )
    claim = try_acquire_claim(ExperimentDirectory.resolve(experiment), owner="other-worker")
    assert claim is not None

    # The operator must be told which process holds the claim.
    with pytest.raises(WorkerError, match="already claimed.*held by other-worker since "):
        run_experiment(experiment)

    release_claim(ExperimentDirectory.resolve(experiment), claim)


def test_executor_reports_claim_release_warning_without_console_output(tmp_path, monkeypatch, capsys):
    repository = _repository(tmp_path, "print('done')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="release-failure",
            command=ExperimentCommand.argv(("python", "train.py")),
            output_root=tmp_path / "reports",
            source_path=repository,
        )
    )

    def fail_release(*args, **kwargs):
        raise ClaimError("metadata unavailable")

    monkeypatch.setattr(worker_module, "release_claim", fail_release)
    events: list[WorkerProgressEvent] = []

    assert run_experiment(experiment, progress=events.append) == 0

    assert capsys.readouterr().err == ""
    assert events[-1].phase == "warning"
    assert events[-1].error == "Could not release claim: metadata unavailable"
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "completed"
    assert status.exit_code == 0


def test_executor_preserves_original_failure_when_a_claim_cannot_be_released(tmp_path, monkeypatch, capsys):
    repository = _repository(tmp_path, "print('done')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="masked-failure",
            command=ExperimentCommand.argv(("python", "train.py")),
            output_root=tmp_path / "reports",
            source_path=repository,
        )
    )
    (experiment / "inputs").mkdir()

    def fail_release(*args, **kwargs):
        raise ClaimError("metadata unavailable")

    monkeypatch.setattr(worker_module, "release_claim", fail_release)
    events: list[WorkerProgressEvent] = []

    # The preparation failure must survive; the release problem is only a warning.
    with pytest.raises(WorkerError, match="Planned input manifest is missing"):
        run_experiment(experiment, progress=events.append)

    assert capsys.readouterr().err == ""
    assert events[-1].phase == "warning"
    assert events[-1].error == "Could not release claim: metadata unavailable"


def test_executor_releases_its_claim_after_execution(tmp_path):
    repository = _repository(tmp_path, "print('done')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="released",
            command=ExperimentCommand.argv(("python", "train.py")),
            output_root=tmp_path / "reports",
            source_path=repository,
        )
    )

    assert run_experiment(experiment) == 0
    assert not ExperimentDirectory.resolve(experiment).claim.exists()


def test_worker_executes_recorded_commit_and_patch_then_cleans_worktree(tmp_path):
    repository = _repository(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "artifact_dir = Path(os.environ['RUNFORGE_ARTIFACT_DIR'])\n"
            "artifact_dir.joinpath('result.txt').write_text('base')\n"
            "artifact_dir.joinpath('worktree.txt').write_text(str(Path.cwd()))\n"
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
    worker_path = Path((experiment / "artifacts" / "worktree.txt").read_text(encoding="utf-8"))
    assert worker_path.name.startswith("runforge-worker-")
    assert worker_path.parent == repository.parent
    assert not worker_path.exists()
    assert status.state == "completed"
    assert status.attempt == 1
    assert status.exit_code == 0
    assert (experiment / "stdout.log").is_file()
    assert (experiment / "stderr.log").is_file()
    assert "runforge-worker-" not in git(repository, "worktree", "list", "--porcelain")


def test_worker_executes_pinned_commit_after_current_checkout_advances(tmp_path):
    repository = _repository(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('result.txt').write_text('first')\n"
        ),
    )
    first_commit = git(repository, "rev-parse", "HEAD")
    train = repository / "train.py"
    train.write_text(train.read_text(encoding="utf-8").replace("'first'", "'second'"), encoding="utf-8")
    git(repository, "add", "train.py")
    git(repository, "commit", "-m", "second")
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


def test_worker_verifies_inputs_and_exposes_the_input_directory(tmp_path):
    repository = _repository(
        tmp_path,
        (
            "import os\n"
            "from pathlib import Path\n"
            "input_path = Path(os.environ['RUNFORGE_INPUT_DIR']) / 'config.json'\n"
            "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('result.txt').write_text(input_path.read_text())\n"
        ),
    )
    experiment = plan_experiment(
        PlanRequest(
            name="inputs",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
            inputs=(InputTemplate(path="config.json", kind="copy", content="configured\n"),),
        )
    )

    assert run_experiment(experiment) == 0

    assert (experiment / "artifacts/result.txt").read_text(encoding="utf-8") == "configured\n"


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda inputs: (inputs / "config.json").write_text("changed\\n", encoding="utf-8"), "checksum"),
        (lambda inputs: (inputs / "extra.txt").write_text("unexpected\\n", encoding="utf-8"), "Unexpected"),
        (lambda inputs: (inputs / "config.json").unlink(), "missing"),
    ],
)
def test_worker_rejects_mutated_planned_inputs_before_execution(tmp_path, mutation, match):
    repository = _repository(tmp_path, "raise SystemExit('must not execute')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="invalid inputs",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
            inputs=(InputTemplate(path="config.json", kind="copy", content="configured\n"),),
        )
    )
    mutation(experiment / "inputs")

    with pytest.raises(WorkerError, match=match):
        run_experiment(experiment)

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.attempt == 0


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


def test_worker_executes_verified_directory_source_in_place_without_git(tmp_path):
    source = _verified_directory_source(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "artifact_dir = Path(os.environ['RUNFORGE_ARTIFACT_DIR'])\n"
            "artifact_dir.joinpath('result.txt').write_text('base')\n"
            "artifact_dir.joinpath('cwd.txt').write_text(str(Path.cwd()))\n"
        ),
    )
    experiment = plan_experiment(
        PlanRequest(
            name="verified",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="verified-directory",
        )
    )

    assert run_experiment(experiment) == 0

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "base"
    assert Path((experiment / "artifacts" / "cwd.txt").read_text(encoding="utf-8")) == source.resolve()
    assert status.state == "completed"
    assert status.attempt == 1
    assert status.exit_code == 0
    assert not (experiment / "git.patch").exists()


def test_worker_rejects_missing_verified_directory_source(tmp_path):
    source = _verified_directory_source(tmp_path, "print('should not run')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="verified",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="verified-directory",
        )
    )
    shutil.rmtree(source)

    with pytest.raises(WorkerError, match="missing or not a directory"):
        run_experiment(experiment)

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.attempt == 0


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda source: (source / "train.py").write_text("changed", encoding="utf-8"), "checksum"),
        (lambda source: (source / "extra.txt").write_text("unexpected", encoding="utf-8"), "Unexpected"),
        (lambda source: (source / "train.py").unlink(), "missing"),
    ],
)
def test_worker_rejects_changed_verified_directory_source_before_execution(tmp_path, mutation, match):
    source = _verified_directory_source(tmp_path, "raise SystemExit('must not execute')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="verified",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="verified-directory",
        )
    )
    mutation(source)

    with pytest.raises(WorkerError, match=match):
        run_experiment(experiment)

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.attempt == 0


def test_worker_records_attempt_when_command_cannot_start(tmp_path):
    source = _verified_directory_source(tmp_path, 'print("must not execute")\n')
    experiment = plan_experiment(
        PlanRequest(
            name="startup-failure",
            command=ExperimentCommand.argv(("runforge-command-does-not-exist",)),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="verified-directory",
        )
    )

    with pytest.raises(WorkerError, match="Could not start command"):
        run_experiment(experiment)

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.attempt == 1
    assert status.started_at is not None


def test_worker_records_verified_directory_command_failure(tmp_path):
    source = _verified_directory_source(tmp_path, "raise SystemExit(7)\n")
    experiment = plan_experiment(
        PlanRequest(
            name="verified",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="verified-directory",
        )
    )

    assert run_experiment(experiment) == FAILURE_EXIT_CODE

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.error == "Command exited with status 7"


def test_worker_rechecks_materialized_snapshot_before_execution(tmp_path, monkeypatch):
    source = _verified_directory_source(tmp_path, 'print("original")\n')
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot-race",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )
    original_copytree = worker_module.shutil.copytree

    def tamper(source_path, destination_path, *args, **kwargs):
        (Path(source_path) / "train.py").write_text('print("tampered")\n', encoding="utf-8")
        return original_copytree(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(worker_module.shutil, "copytree", tamper)
    with pytest.raises(WorkerError, match="checksum"):
        run_experiment(experiment)

    assert not (experiment / "artifacts" / "result.txt").exists()
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"


def test_worker_executes_directory_snapshot_source_from_isolated_workspace(tmp_path):
    source = _verified_directory_source(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "artifact_dir = Path(os.environ['RUNFORGE_ARTIFACT_DIR'])\n"
            "artifact_dir.joinpath('result.txt').write_text('base')\n"
            "artifact_dir.joinpath('cwd.txt').write_text(str(Path.cwd()))\n"
        ),
    )
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )
    shutil.rmtree(source)

    assert run_experiment(experiment) == 0

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    workspace = Path((experiment / "artifacts" / "cwd.txt").read_text(encoding="utf-8"))
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "base"
    assert workspace.name == "source"
    assert workspace.parent.name.startswith("runforge-worker-")
    assert not workspace.exists()
    assert status.state == "completed"
    assert status.attempt == 1
    assert status.exit_code == 0


def test_worker_directory_snapshot_execution_does_not_modify_captured_source(tmp_path):
    source = _verified_directory_source(
        tmp_path,
        "from pathlib import Path\nPath('created-by-command.txt').write_text('side effect')\n",
    )
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )

    assert run_experiment(experiment) == 0

    assert sorted(path.name for path in (experiment / "source").iterdir()) == ["train.py"]


def test_worker_rejects_missing_captured_directory_snapshot_source(tmp_path):
    source = _verified_directory_source(tmp_path, "print('should not run')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )
    shutil.rmtree(experiment / "source")

    with pytest.raises(WorkerError, match="Captured directory-snapshot source is missing"):
        run_experiment(experiment)

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.attempt == 0


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda captured: (captured / "train.py").write_text("changed", encoding="utf-8"), "checksum"),
        (lambda captured: (captured / "extra.txt").write_text("unexpected", encoding="utf-8"), "Unexpected"),
        (lambda captured: (captured / "train.py").unlink(), "missing"),
    ],
)
def test_worker_rejects_tampered_captured_directory_snapshot_source(tmp_path, mutation, match):
    source = _verified_directory_source(tmp_path, "raise SystemExit('must not execute')\n")
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )
    mutation(experiment / "source")

    with pytest.raises(WorkerError, match=match):
        run_experiment(experiment)

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.attempt == 0


def test_worker_records_directory_snapshot_command_failure(tmp_path):
    source = _verified_directory_source(tmp_path, "raise SystemExit(7)\n")
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )

    assert run_experiment(experiment) == FAILURE_EXIT_CODE

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.error == "Command exited with status 7"
