"""Tests for the RunForge command-line interface."""

from __future__ import annotations

import subprocess
from pathlib import Path

from runforge.cli import main
from runforge.experiment_schema import ExperimentConfiguration, ExperimentStatus
from runforge.json_store import load_json_object, save_json_object


CLI_ERROR_EXIT = 2
MATRIX_PLAN_COUNT = 4
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
    assert main(["run", "--stream-output", str(experiment)]) == 0
    assert capsys.readouterr().out == "planned command ran\n"


def test_cli_rejects_missing_command(capsys):
    assert main(["plan"]) == CLI_ERROR_EXIT
    assert "No command provided" in capsys.readouterr().err


def test_cli_formats_untracked_warnings_without_python_source_lines(tmp_path, capsys):
    repository = _repository(tmp_path)
    (repository / "b").write_text("untracked\n", encoding="utf-8")

    assert (
        main(
            [
                "plan",
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
    captured = capsys.readouterr()

    assert captured.err.splitlines() == [
        "warning: Planned Git source has untracked files that are not included in git.patch:",
        "  b",
    ]
    assert "return plan_experiment(" not in captured.err


def test_cli_plans_an_explicit_pinned_git_source(tmp_path, capsys):
    repository = _repository(tmp_path)
    pinned_commit = _git(repository, "rev-parse", "HEAD")
    train = repository / "train.py"
    train.write_text("print('advanced checkout')\n", encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "advance checkout")

    assert (
        main(
            [
                "plan",
                "--source-mode",
                "pinned-git",
                "--commit",
                pinned_commit,
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    experiment = _planned_path(capsys.readouterr().out)
    configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))

    assert configuration.source.commit == pinned_commit
    assert configuration.source.branch == "pinned"


def test_cli_creates_a_pinned_cartesian_matrix(tmp_path, capsys):
    repository = _repository(tmp_path)
    matrix_file = tmp_path / "matrix.json"
    save_json_object(
        matrix_file,
        {"SEED": [1, 2], "LR": [0.1, 0.01]},
    )

    assert (
        main(
            [
                "matrix",
                "--matrix-file",
                str(matrix_file),
                "--commit",
                "HEAD",
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
                "--lr={LR}",
                "--seed={SEED}",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out.splitlines()

    assert output[0] == f"Experiment plans created ({MATRIX_PLAN_COUNT}):"
    experiments = tuple(Path(line.strip()) for line in output[1:])
    assert len(experiments) == MATRIX_PLAN_COUNT
    configurations = [
        ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json")) for experiment in experiments
    ]
    assert [configuration.parameters for configuration in configurations] == [
        {"LR": "0.1", "SEED": "1"},
        {"LR": "0.1", "SEED": "2"},
        {"LR": "0.01", "SEED": "1"},
        {"LR": "0.01", "SEED": "2"},
    ]
    assert all(experiment.is_dir() for experiment in experiments)


def test_cli_launches_a_new_experiment_immediately(tmp_path, capsys):
    repository = _repository(tmp_path)

    assert (
        main(
            [
                "launch",
                "--stream-output",
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
    output = capsys.readouterr().out.splitlines()
    experiment = _planned_path(output[0])
    assert output[1:] == ["planned command ran"]
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))

    assert status.state == "completed"
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "planned command ran\n"
