"""Focused tests for Stage 1A.7 CLI wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from runforge.cli import main
from runforge.experiment_schema import ExperimentStatus
from runforge.json_store import load_json_object


CLI_ERROR_EXIT = 2
PLAN_CREATED_PREFIX = "Experiment plan created at: "


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "train.py").write_text("print('planned command ran')\n", encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "initial")
    return repository


def _planned_path(output: str) -> Path:
    assert output.startswith(PLAN_CREATED_PREFIX)
    return Path(output.removeprefix(PLAN_CREATED_PREFIX).strip())


def test_cli_plans_and_runs_one_explicit_experiment(tmp_path, capsys):
    repository = _repository(tmp_path)
    environment_file = tmp_path / "environment.env"
    environment_file.write_text("RUN_MODE=ablation\n", encoding="utf-8")

    assert (
        main(
            [
                "plan",
                "--name",
                "cli",
                "--out-dir",
                str(tmp_path / "reports"),
                "--source-path",
                str(repository),
                "--env-file",
                str(environment_file),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    experiment = _planned_path(capsys.readouterr().out)

    assert experiment.is_dir()
    assert main(["run", str(experiment)]) == 0


def test_cli_rejects_missing_command(capsys):
    assert main(["plan"]) == CLI_ERROR_EXIT
    assert "No command provided" in capsys.readouterr().err


def test_cli_launches_a_new_experiment_immediately(tmp_path, capsys):
    repository = _repository(tmp_path)

    assert (
        main(
            [
                "launch",
                "--name",
                "immediate",
                "--out-dir",
                str(tmp_path / "reports"),
                "--source-path",
                str(repository),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    experiment = _planned_path(capsys.readouterr().out)
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))

    assert status.state == "completed"
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "planned command ran\n"
