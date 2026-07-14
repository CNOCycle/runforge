"""Tests for the RunForge experiment planner."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

import runforge.planner as planner_module
from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.json_store import load_json_object
from runforge.planner import MatrixPlanRequest, PlanningError, PlanRequest, plan_experiment, plan_matrix
from runforge.source_metadata import PinnedGitSource


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


def test_planner_resolves_copies_and_hashes_patch_for_pinned_commit(tmp_path):
    repository = _repository(tmp_path)
    pinned_commit = _git(repository, "rev-parse", "HEAD")
    train = repository / "train.py"
    train.write_text("VALUE = 2\n", encoding="utf-8")
    patch_path = tmp_path / "change.patch"
    patch_path.write_text(_git(repository, "diff", "--binary", "HEAD", "--") + "\n", encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "advance checkout")
    experiment = plan_experiment(
        PlanRequest(
            name="pinned patch",
            command=ExperimentCommand.argv(("python", "train.py")),
            output_root=tmp_path / "reports",
            source=PinnedGitSource(
                repository=repository,
                commit=pinned_commit,
                patch=patch_path,
            ),
        )
    )

    configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))
    captured_patch = (experiment / "git.patch").read_bytes()

    assert configuration.source.commit == pinned_commit
    assert configuration.source.branch == "pinned"
    assert configuration.source.untracked_files == ()
    assert configuration.source.patch_file == "git.patch"
    assert configuration.source.patch_sha256 == hashlib.sha256(captured_patch).hexdigest()
    assert captured_patch == patch_path.read_bytes()


def test_matrix_planner_resolves_source_once_and_publishes_deterministic_combinations(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    pinned_commit = _git(repository, "rev-parse", "HEAD")
    template = PlanRequest(
        name="sweep",
        command=ExperimentCommand.argv(("python", "train.py", "--lr={LR}", "--seed={SEED}", "--out={ARTIFACT_DIR}")),
        output_root=tmp_path / "reports",
        source=PinnedGitSource(repository=repository, commit=pinned_commit),
    )
    request = MatrixPlanRequest(
        template=template,
        parameters={"SEED": [2, 1], "LR": [0.1, 0.01]},
    )
    calls = 0
    original_resolver = planner_module.resolve_pinned_git_source

    def count_resolution(descriptor):
        nonlocal calls
        calls += 1
        return original_resolver(descriptor)

    monkeypatch.setattr(planner_module, "resolve_pinned_git_source", count_resolution)
    experiments = plan_matrix(request)
    configurations = tuple(
        ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json")) for experiment in experiments
    )
    expected = (
        {"LR": "0.1", "SEED": "2"},
        {"LR": "0.1", "SEED": "1"},
        {"LR": "0.01", "SEED": "2"},
        {"LR": "0.01", "SEED": "1"},
    )

    assert calls == 1
    assert tuple(configuration.parameters for configuration in configurations) == expected
    assert [experiment.name.rsplit("_", 1)[-1] for experiment in experiments] == ["0", "1", "2", "3"]
    assert {configuration.source.commit for configuration in configurations} == {pinned_commit}
    for experiment, configuration, parameters in zip(experiments, configurations, expected, strict=True):
        assert configuration.command.arguments[-3] == f"--lr={parameters['LR']}"
        assert configuration.command.arguments[-2] == f"--seed={parameters['SEED']}"
        assert configuration.command.arguments[-1] == f"--out={experiment / 'artifacts'}"


def test_matrix_planner_rejects_invalid_axis_before_creating_output(tmp_path):
    repository = _repository(tmp_path)
    output_root = tmp_path / "reports"
    template = PlanRequest(
        name="invalid matrix",
        command=ExperimentCommand.argv(("python", "train.py", "--lr={LR}")),
        output_root=output_root,
        source=PinnedGitSource(repository=repository, commit="HEAD"),
    )

    with pytest.raises(PlanningError, match="strings, numbers, or booleans"):
        MatrixPlanRequest(
            template=template,
            parameters={"LR": [0.1, None]},
        )

    assert not output_root.exists()

    current_template = PlanRequest(
        name="current matrix",
        command=ExperimentCommand.argv(("python", "train.py")),
        source_path=repository,
    )
    with pytest.raises(PlanningError, match="requires a pinned Git source"):
        MatrixPlanRequest(template=current_template, parameters={"LR": [0.1]})


def test_planner_reports_invalid_non_git_source_path(tmp_path):
    request = PlanRequest(
        name="invalid",
        command=ExperimentCommand.argv(("python", "train.py")),
        source_path=tmp_path,
        output_root=tmp_path / "reports",
    )

    with pytest.raises(PlanningError, match="find Git repository"):
        plan_experiment(request)
