"""Tests for the RunForge command-line interface."""

from __future__ import annotations

from pathlib import Path

from runforge.cli import main
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from tests.support import create_git_repository, git, planned_path


CLI_ERROR_EXIT = 2
MATRIX_PLAN_COUNT = 4
CURRENT_HEAD_MATRIX_PLAN_COUNT = 2


def _repository(tmp_path: Path) -> Path:
    return create_git_repository(tmp_path / "repository", {"train.py": "print('planned command ran')\n"})


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
    experiment = planned_path(capsys.readouterr().out)

    assert experiment.is_dir()
    assert main(["run", "--stream-output", str(experiment)]) == 0
    output = capsys.readouterr().out
    assert f"Preparing experiment: {experiment}" in output
    assert "Executing command: python train.py" in output
    assert "  output mode: streaming and logging" in output
    assert "planned command ran" in output
    assert f"Experiment completed with exit code 0: {experiment}" in output


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
    pinned_commit = git(repository, "rev-parse", "HEAD")
    train = repository / "train.py"
    train.write_text("print('advanced checkout')\n", encoding="utf-8")
    git(repository, "add", "train.py")
    git(repository, "commit", "-m", "advance checkout")

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
    experiment = planned_path(capsys.readouterr().out)
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
                "--source-mode",
                "pinned-git",
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
    full_output = capsys.readouterr().out
    assert "RunForge matrix effective arguments:" in full_output
    assert f"  matrix file: {matrix_file}" in full_output
    assert "  matrix combinations: 4" in full_output
    assert "  source mode: pinned-git" in full_output
    assert "  commit/ref: HEAD" in full_output
    output = full_output.splitlines()
    summary_index = output.index(f"Experiment plans created ({MATRIX_PLAN_COUNT}):")
    experiments = tuple(Path(line.strip()) for line in output[summary_index + 1 :])
    assert len(experiments) == MATRIX_PLAN_COUNT
    configurations = [
        ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json")) for experiment in experiments
    ]
    assert [configuration.parameters for configuration in configurations] == [
        {"LR": 0.1, "SEED": 1},
        {"LR": 0.1, "SEED": 2},
        {"LR": 0.01, "SEED": 1},
        {"LR": 0.01, "SEED": 2},
    ]
    assert all(experiment.is_dir() for experiment in experiments)


def test_cli_creates_a_current_head_cartesian_matrix(tmp_path, capsys):
    repository = _repository(tmp_path)
    matrix_file = tmp_path / "matrix.json"
    save_json_object(matrix_file, {"SEED": [1, 2]})
    pinned_commit = git(repository, "rev-parse", "HEAD")

    assert (
        main(
            [
                "matrix",
                "--matrix-file",
                str(matrix_file),
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
                "--seed={SEED}",
            ]
        )
        == 0
    )
    full_output = capsys.readouterr().out
    assert "  source mode: current-head" in full_output
    assert "  commit/ref: not set" in full_output
    output = full_output.splitlines()
    summary_index = output.index(f"Experiment plans created ({CURRENT_HEAD_MATRIX_PLAN_COUNT}):")
    experiments = tuple(Path(line.strip()) for line in output[summary_index + 1 :])
    assert len(experiments) == CURRENT_HEAD_MATRIX_PLAN_COUNT
    configurations = [
        ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json")) for experiment in experiments
    ]
    assert {configuration.source.commit for configuration in configurations} == {pinned_commit}


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
    output = capsys.readouterr().out
    experiment = planned_path(output)
    assert "RunForge launch effective arguments:" in output
    assert "  stream output: enabled" in output
    assert f"Preparing experiment: {experiment}" in output
    assert "Executing command: python train.py" in output
    assert "planned command ran" in output
    assert f"Experiment completed with exit code 0: {experiment}" in output
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))

    assert status.state == "completed"
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "planned command ran\n"


def test_worker_cli_prints_effective_arguments_progress_and_summary(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('worker output', flush=True)\n"})
    reports = tmp_path / "reports"
    plan_experiment(
        PlanRequest(
            name="worker-cli",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=reports,
        )
    )

    assert main(["worker", str(reports), "--max-tasks", "1"]) == 0

    output = capsys.readouterr().out
    assert "RunForge worker effective arguments:" in output
    assert f"  root: {reports.resolve()}" in output
    assert "  max tasks: 1" in output
    assert "  stream output: disabled" in output
    assert "Preparing experiment:" in output
    assert "Executing command: python train.py" in output
    assert "Experiment completed with exit code 0:" in output
    assert "Worker summary:" in output
    assert "  candidates: 1" in output
    assert "  selected: 1" in output
    assert "  completed: 1" in output
    assert "  failed: 0" in output
    assert "  skipped: 0" in output
    assert "    non-runnable: 0" in output
    assert "    claim contention: 0" in output
    assert "    stale after claim: 0" in output
    assert "  deferred: 0" in output
    assert "  invalid: 0" in output


def _plan_worker_experiment(tmp_path: Path, reports: Path, name: str, script: str) -> Path:
    repository = create_git_repository(tmp_path / f"repository-{name}", {"train.py": script})
    return plan_experiment(
        PlanRequest(
            name=name,
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=reports,
        )
    )


def test_worker_cli_returns_one_when_a_selected_command_fails(tmp_path, capsys):
    reports = tmp_path / "reports"
    _plan_worker_experiment(tmp_path, reports, "failing", "raise SystemExit(7)\n")

    assert main(["worker", str(reports)]) == 1

    output = capsys.readouterr().out
    assert "  completed: 0" in output
    assert "  failed: 1" in output
    assert "  invalid: 0" in output


def test_worker_cli_reports_invalid_metadata_ahead_of_a_failed_command(tmp_path, capsys):
    reports = tmp_path / "reports"
    _plan_worker_experiment(tmp_path, reports, "failing", "raise SystemExit(7)\n")
    broken = _plan_worker_experiment(tmp_path, reports, "broken", "print('never runs')\n")
    (broken / "config.json").write_text("{ not json\n", encoding="utf-8")

    # Invalid metadata outranks a failed command: 2 means the scan itself is untrustworthy,
    # so a caller testing for 1 must not conclude that every candidate was inspected.
    assert main(["worker", str(reports)]) == CLI_ERROR_EXIT

    output = capsys.readouterr().out
    assert "  failed: 1" in output
    assert "  invalid: 1" in output
