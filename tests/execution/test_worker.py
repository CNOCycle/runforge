"""Tests for the RunForge experiment execution worker."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import runforge.execution.sources as sources_module
import runforge.execution.worker as worker_module
from runforge.execution.worker import BatchRequest, WorkerError, WorkerProgressEvent, execute_paths, run_experiment
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )
    train.write_text(train.read_text(encoding="utf-8").replace("'patched'", "'later'"), encoding="utf-8")

    assert run_experiment(experiment) == 0

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "patched"
    worker_path = Path((experiment / "artifacts" / "worktree.txt").read_text(encoding="utf-8"))
    assert worker_path == ExperimentDirectory.resolve(experiment).workspace.resolve()
    assert not worker_path.exists()
    assert status.state == "completed"
    assert status.attempt == 1
    assert status.exit_code == 0
    assert (experiment / "stdout.log").is_file()
    assert (experiment / "stderr.log").is_file()
    assert str(worker_path) not in git(repository, "worktree", "list", "--porcelain")


CWD_SCRIPT = (
    "from pathlib import Path\n"
    "import os\n"
    "artifact_dir = Path(os.environ['RUNFORGE_ARTIFACT_DIR'])\n"
    "artifact_dir.joinpath('cwd.txt').write_text(str(Path.cwd()))\n"
    "artifact_dir.joinpath('listing.txt').write_text('\\n'.join(sorted(p.name for p in Path.cwd().iterdir())))\n"
)


def _git_experiment(tmp_path: Path, name: str, script: str = CWD_SCRIPT) -> Path:
    repository = _repository(tmp_path, script)
    return plan_experiment(
        PlanRequest(
            name=name,
            command=ExperimentCommand.argv((sys.executable, "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )


def _listing(experiment: Path) -> list[str]:
    return (experiment / "artifacts" / "listing.txt").read_text(encoding="utf-8").splitlines()


def test_worker_removes_the_git_workdir_after_a_failing_command(tmp_path):
    experiment = _git_experiment(tmp_path, "failing", f"raise SystemExit({FAILURE_EXIT_CODE})\n")

    assert run_experiment(experiment) == FAILURE_EXIT_CODE

    workspace = ExperimentDirectory.resolve(experiment).workspace
    assert not workspace.exists()
    assert str(workspace) not in git(tmp_path / "repository", "worktree", "list", "--porcelain")


def test_worker_replaces_a_leftover_git_worktree_instead_of_reusing_it(tmp_path):
    experiment = _git_experiment(tmp_path, "leftover-worktree")
    workspace = ExperimentDirectory.resolve(experiment).workspace
    git(tmp_path / "repository", "worktree", "add", "--detach", str(workspace), "HEAD")
    (workspace / "left-by-killed-run.txt").write_text("partial", encoding="utf-8")

    assert run_experiment(experiment) == 0

    assert "left-by-killed-run.txt" not in _listing(experiment)
    assert "train.py" in _listing(experiment)
    assert not workspace.exists()
    assert str(workspace) not in git(tmp_path / "repository", "worktree", "list", "--porcelain")


def test_worker_recovers_from_a_hand_deleted_git_worktree(tmp_path):
    experiment = _git_experiment(tmp_path, "stale-registration")
    workspace = ExperimentDirectory.resolve(experiment).workspace
    git(tmp_path / "repository", "worktree", "add", "--detach", str(workspace), "HEAD")
    shutil.rmtree(workspace)

    assert run_experiment(experiment) == 0

    assert "train.py" in _listing(experiment)
    assert not workspace.exists()


def test_worker_deregisters_a_workdir_the_command_deleted(tmp_path):
    script = "import shutil\nfrom pathlib import Path\nshutil.rmtree(Path.cwd())\n"
    experiment = _git_experiment(tmp_path, "self-deleting", script)
    workspace = ExperimentDirectory.resolve(experiment).workspace

    assert run_experiment(experiment) == 0

    assert not workspace.exists()
    assert str(workspace) not in git(tmp_path / "repository", "worktree", "list", "--porcelain")


@pytest.mark.parametrize("deleted_by_hand", [False, True])
def test_worker_refuses_a_locked_leftover_worktree_without_touching_it(tmp_path, deleted_by_hand):
    experiment = _git_experiment(tmp_path, "locked-leftover")
    workspace = ExperimentDirectory.resolve(experiment).workspace
    repository = tmp_path / "repository"
    git(repository, "worktree", "add", "--detach", str(workspace), "HEAD")
    (workspace / "keep.txt").write_text("under investigation", encoding="utf-8")
    git(repository, "worktree", "lock", "--reason", "investigating", str(workspace))
    if deleted_by_hand:
        shutil.rmtree(workspace)

    with pytest.raises(WorkerError, match="locked Git worktree.*worktree unlock"):
        run_experiment(experiment)

    assert not (experiment / "artifacts" / "listing.txt").exists()
    assert ExperimentDirectory.resolve(experiment).load_status().state == "failed"
    assert "locked investigating" in git(repository, "worktree", "list", "--porcelain")
    if not deleted_by_hand:
        assert (workspace / "keep.txt").read_text(encoding="utf-8") == "under investigation"

    # Once the operator unlocks it, an ordinary retry replaces the leftover.
    git(repository, "worktree", "unlock", str(workspace))
    result = execute_paths(BatchRequest((experiment,), retry=True))
    assert result.completed == 1
    assert "keep.txt" not in _listing(experiment)


def test_worker_removes_a_worktree_git_registered_before_failing(tmp_path):
    experiment = _git_experiment(tmp_path, "failing-hook")
    repository = tmp_path / "repository"
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    workspace = ExperimentDirectory.resolve(experiment).workspace

    with pytest.raises(WorkerError):
        run_experiment(experiment)

    assert not (experiment / "artifacts" / "listing.txt").exists()
    assert not workspace.exists()
    assert str(workspace) not in git(repository, "worktree", "list", "--porcelain")


def test_worker_keeps_a_locked_leftover_under_a_path_containing_a_newline(tmp_path):
    repository = _repository(tmp_path, CWD_SCRIPT)
    experiment = plan_experiment(
        PlanRequest(
            name="newline",
            command=ExperimentCommand.argv((sys.executable, "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports\nlocked",
        )
    )
    workspace = ExperimentDirectory.resolve(experiment).workspace
    git(repository, "worktree", "add", "--detach", str(workspace), "HEAD")
    git(repository, "worktree", "lock", str(workspace))

    with pytest.raises(WorkerError, match="locked Git worktree"):
        run_experiment(experiment)

    assert (workspace / "train.py").is_file()
    records = git(repository, "worktree", "list", "--porcelain", "-z").split("\0\0")
    assert any(record.startswith(f"worktree {workspace}\0") and "\0locked" in record for record in records)


def test_worker_replaces_a_leftover_plain_directory_for_a_git_source(tmp_path):
    experiment = _git_experiment(tmp_path, "leftover-directory")
    workspace = ExperimentDirectory.resolve(experiment).workspace
    workspace.mkdir()
    (workspace / "half-created.txt").write_text("partial", encoding="utf-8")

    assert run_experiment(experiment) == 0

    assert "half-created.txt" not in _listing(experiment)
    assert not workspace.exists()


def test_worker_unlinks_a_leftover_workdir_link_without_following_it(tmp_path):
    experiment = _git_experiment(tmp_path, "leftover-link")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("not ours", encoding="utf-8")
    workspace = ExperimentDirectory.resolve(experiment).workspace
    workspace.symlink_to(target, target_is_directory=True)

    assert run_experiment(experiment) == 0

    assert (target / "keep.txt").read_text(encoding="utf-8") == "not ours"
    assert not workspace.exists() and not workspace.is_symlink()


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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
    assert events[1].command == ExperimentCommand.argv((sys.executable, "train.py"))
    assert events[2].exit_code == 0
    assert events[2].error is None


def test_worker_reports_nonzero_exit_and_ignores_progress_callback_errors(tmp_path):
    repository = _repository(tmp_path, "raise SystemExit(7)\n")
    experiment = plan_experiment(
        PlanRequest(
            name="failed-progress",
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )
    original_copytree = sources_module.shutil.copytree

    def tamper(source_path, destination_path, *args, **kwargs):
        (Path(source_path) / "train.py").write_text('print("tampered")\n', encoding="utf-8")
        return original_copytree(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(sources_module.shutil, "copytree", tamper)
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
    assert workspace == ExperimentDirectory.resolve(experiment).workspace.resolve()
    assert not workspace.exists()
    assert status.state == "completed"
    assert status.attempt == 1
    assert status.exit_code == 0


def _snapshot_experiment(tmp_path: Path, name: str, script: str = CWD_SCRIPT) -> Path:
    source = _verified_directory_source(tmp_path, script)
    return plan_experiment(
        PlanRequest(
            name=name,
            command=ExperimentCommand.argv((sys.executable, "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )


def test_worker_removes_the_snapshot_workdir_after_a_failing_command(tmp_path):
    experiment = _snapshot_experiment(tmp_path, "snapshot-failing", f"raise SystemExit({FAILURE_EXIT_CODE})\n")

    assert run_experiment(experiment) == FAILURE_EXIT_CODE

    assert not ExperimentDirectory.resolve(experiment).workspace.exists()


def test_worker_replaces_a_leftover_snapshot_workdir_instead_of_reusing_it(tmp_path):
    experiment = _snapshot_experiment(tmp_path, "snapshot-leftover")
    workspace = ExperimentDirectory.resolve(experiment).workspace
    shutil.copytree(experiment / "source", workspace)
    (workspace / "left-by-killed-run.txt").write_text("partial", encoding="utf-8")

    # Reusing this copy would also fail the post-copy source verification.
    assert run_experiment(experiment) == 0

    assert _listing(experiment) == ["train.py"]
    assert not workspace.exists()


def test_worker_directory_snapshot_execution_does_not_modify_captured_source(tmp_path):
    source = _verified_directory_source(
        tmp_path,
        "from pathlib import Path\nPath('created-by-command.txt').write_text('side effect')\n",
    )
    experiment = plan_experiment(
        PlanRequest(
            name="snapshot",
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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
            command=ExperimentCommand.argv((sys.executable, "train.py")),
            source_path=source,
            output_root=tmp_path / "reports",
            directory_source_mode="directory-snapshot",
        )
    )

    assert run_experiment(experiment) == FAILURE_EXIT_CODE

    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert status.state == "failed"
    assert status.error == "Command exited with status 7"


def _planned_for_failure(tmp_path):
    repository = _repository(tmp_path, "print('ok')\n")
    return ExperimentDirectory.resolve(
        plan_experiment(
            PlanRequest(
                name="unexpected",
                command=ExperimentCommand.argv((sys.executable, "train.py")),
                output_root=tmp_path / "reports",
                source_path=repository,
            )
        )
    )


def test_an_unexpected_exception_after_start_records_the_experiment_as_failed(tmp_path, monkeypatch):
    experiment = _planned_for_failure(tmp_path)
    events = []

    def explode(*arguments, **keywords):
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr(worker_module, "run_command", explode)

    result = execute_paths(BatchRequest((experiment.root,)), progress=events.append)

    status = experiment.load_status()
    assert status.state == "failed"
    assert status.error == "unexpected boom"
    assert result.failed == 1
    assert [event.phase for event in events if event.phase == "failed"] == ["failed"]


def test_lost_claim_ownership_leaves_the_new_owners_status_untouched(tmp_path, monkeypatch):
    experiment = _planned_for_failure(tmp_path)

    def lose_the_claim(*arguments, **keywords):
        shutil.rmtree(experiment.claim)
        raise ClaimError("claim ownership was lost")

    monkeypatch.setattr(worker_module, "run_command", lose_the_claim)

    result = execute_paths(BatchRequest((experiment.root,)))

    assert result.failed == 1
    assert experiment.load_status().state == "inprogress"

