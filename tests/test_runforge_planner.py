"""Focused tests for Stage 1A.5 current-HEAD experiment planning."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.json_store import load_json_object
from runforge.planner import PlanningError, PlanRequest, plan_experiment


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
    (repository / "train.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_planner_captures_current_head_metadata_and_never_executes_command(tmp_path):
    repository = _repository(tmp_path)
    marker = tmp_path / "executed.txt"
    (repository / "train.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("not captured\n", encoding="utf-8")
    (repository / "notes").mkdir()
    (repository / "notes" / "second.txt").write_text("also not captured\n", encoding="utf-8")
    request = PlanRequest(
        name="minor revision",
        command=ExperimentCommand.argv(
            ("python", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()", "--out={ARTIFACT_DIR}")
        ),
        source_path=repository,
        output_root=tmp_path / "reports",
        environment={"RUN_MODE": "ablation"},
    )

    with pytest.warns(UserWarning, match="untracked files") as warning:
        experiment = plan_experiment(request)

    configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))
    status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    assert str(warning[0].message).splitlines() == [
        "Planned Git source has untracked files that are not included in git.patch:",
        "  notes/second.txt",
        "  untracked.txt",
    ]
    assert not marker.exists()
    assert configuration.source.commit == _git(repository, "rev-parse", "HEAD")
    assert configuration.source.untracked_files == ("notes/second.txt", "untracked.txt")
    assert configuration.command.arguments[-1] == f"--out={experiment / 'artifacts'}"
    assert configuration.environment == {"RUN_MODE": "ablation"}
    assert status.state == "created"
    assert (experiment / "artifacts").is_dir()
    assert (experiment / "git.patch").is_file()
    assert "+VALUE = 2" in (experiment / "git.patch").read_text(encoding="utf-8")
    assert (experiment / "cmd.sh").stat().st_mode & 0o111


def test_planner_defaults_output_root_to_source_repository_reports(tmp_path):
    repository = _repository(tmp_path)

    experiment = plan_experiment(
        PlanRequest(
            name="default root",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
        )
    )

    assert experiment.parent.parent == repository.resolve() / "reports"


def test_planner_allocates_distinct_experiment_directories_and_persists_shell_pipeline(tmp_path):
    repository = _repository(tmp_path)
    request = PlanRequest(
        name="pipeline",
        command=ExperimentCommand.shell(
            "python train.py --out '{ARTIFACT_DIR}' && python evaluate.py --out '{ARTIFACT_DIR}'"
        ),
        source_path=repository,
        output_root=tmp_path / "reports",
    )

    first = plan_experiment(request)
    second = plan_experiment(request)

    first_configuration = ExperimentConfiguration.from_dict(load_json_object(first / "config.json"))
    commit8 = _git(repository, "rev-parse", "HEAD")[:8]
    assert first != second
    assert first.name == f"{commit8}_pipeline_0"
    assert second.name == f"{commit8}_pipeline_1"
    assert first_configuration.command.script is not None
    assert "{ARTIFACT_DIR}" not in first_configuration.command.script
    assert str(first / "artifacts") in first_configuration.command.script


def test_planner_reports_invalid_non_git_source_path(tmp_path):
    request = PlanRequest(
        name="invalid",
        command=ExperimentCommand.argv(("python", "train.py")),
        source_path=tmp_path,
        output_root=tmp_path / "reports",
    )

    with pytest.raises(PlanningError, match="find Git repository"):
        plan_experiment(request)
