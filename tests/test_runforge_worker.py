"""Focused tests for Stage 1A.6 explicit experiment execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from runforge.experiment_schema import ExperimentCommand, ExperimentStatus
from runforge.json_store import load_json_object
from runforge.planner import PlanRequest, plan_experiment
from runforge.worker import run_experiment


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
