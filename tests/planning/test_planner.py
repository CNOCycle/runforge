"""Tests for the RunForge experiment planner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import runforge.planning.planner as planner_module
from runforge.infrastructure.json_store import load_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.inputs import InputTemplate
from runforge.planning.planner import MatrixPlanRequest, PlanningError, PlanRequest, plan_experiment, plan_matrix
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.source import PinnedGitSource
from tests.support import create_git_repository, git


def _repository(tmp_path: Path) -> Path:
    return create_git_repository(tmp_path / "repository", {"train.py": "VALUE = 1\n"})


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
    assert configuration.source.commit == git(repository, "rev-parse", "HEAD")
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


def test_planner_publishes_rendered_linked_input_tree_and_manifest(tmp_path):
    repository = _repository(tmp_path)
    request = PlanRequest(
        name="linked inputs",
        command=ExperimentCommand.argv(
            ("python", "train.py", "{INPUT_DIR}/configs/train.json", "--artifact-dir={ARTIFACT_DIR}")
        ),
        source_path=repository,
        output_root=tmp_path / "reports",
        inputs=(
            InputTemplate(
                path="configs/eval.json",
                kind="json-template",
                content='{"checkpoint": "{ARTIFACT_DIR}/training/checkpoint.pt"}',
            ),
            InputTemplate(
                path="configs/train.json",
                kind="json-template",
                content='{"output": "{ARTIFACT_DIR}/training"}',
            ),
            InputTemplate(path="components/data.json", kind="copy", content='{"version": 1}\\n'),
        ),
    )

    experiment = plan_experiment(request)
    layout = ExperimentDirectory(experiment)
    manifest = layout.load_input_manifest()

    assert manifest.entries[0].path == "components/data.json"
    assert json.loads(layout.input_file("configs/train.json").read_text(encoding="utf-8")) == {
        "output": f"{experiment}/artifacts/training",
    }
    assert json.loads(layout.input_file("configs/eval.json").read_text(encoding="utf-8")) == {
        "checkpoint": f"{experiment}/artifacts/training/checkpoint.pt"
    }
    configuration = ExperimentConfiguration.from_dict(load_json_object(layout.configuration_file))
    assert configuration.command.arguments[2] == f"{experiment}/inputs/configs/train.json"
    assert configuration.command.arguments[3] == f"--artifact-dir={experiment}/artifacts"


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
    commit8 = git(repository, "rev-parse", "HEAD")[:8]
    assert first != second
    assert first.name == f"{commit8}_pipeline_0000"
    assert second.name == f"{commit8}_pipeline_0001"
    assert first_configuration.command.script is not None
    assert "{ARTIFACT_DIR}" not in first_configuration.command.script
    assert str(first / "artifacts") in first_configuration.command.script


def test_planner_resolves_copies_and_hashes_patch_for_pinned_commit(tmp_path):
    repository = _repository(tmp_path)
    pinned_commit = git(repository, "rev-parse", "HEAD")
    train = repository / "train.py"
    train.write_text("VALUE = 2\n", encoding="utf-8")
    patch_path = tmp_path / "change.patch"
    patch_path.write_text(git(repository, "diff", "--binary", "HEAD", "--") + "\n", encoding="utf-8")
    git(repository, "add", "train.py")
    git(repository, "commit", "-m", "advance checkout")
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
    pinned_commit = git(repository, "rev-parse", "HEAD")
    template = PlanRequest(
        name="sweep",
        command=ExperimentCommand.argv(("python", "train.py", "--lr={LR}", "--seed={SEED}", "--out={ARTIFACT_DIR}")),
        output_root=tmp_path / "reports",
        source=PinnedGitSource(repository=repository, commit=pinned_commit),
        inputs=(
            InputTemplate(
                path="configs/train.json",
                kind="json-template",
                content='{"lr": "{LR}", "seed": "{SEED}", "amp": "{AMP}"}',
            ),
        ),
    )
    request = MatrixPlanRequest(
        template=template,
        parameters={"SEED": [2, 1], "LR": [0.1, 0.01], "AMP": [True]},
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
        {"AMP": True, "LR": 0.1, "SEED": 2},
        {"AMP": True, "LR": 0.1, "SEED": 1},
        {"AMP": True, "LR": 0.01, "SEED": 2},
        {"AMP": True, "LR": 0.01, "SEED": 1},
    )

    assert calls == 1
    assert tuple(configuration.parameters for configuration in configurations) == expected
    assert [experiment.name.rsplit("_", 1)[-1] for experiment in experiments] == ["0000", "0001", "0002", "0003"]
    assert {configuration.source.commit for configuration in configurations} == {pinned_commit}
    for experiment, configuration, parameters in zip(experiments, configurations, expected, strict=True):
        assert configuration.command.arguments[-3] == f"--lr={parameters['LR']}"
        assert configuration.command.arguments[-2] == f"--seed={parameters['SEED']}"
        assert configuration.command.arguments[-1] == f"--out={experiment / 'artifacts'}"
        assert json.loads((experiment / "inputs/configs/train.json").read_text(encoding="utf-8")) == {
            "amp": True,
            "lr": parameters["LR"],
            "seed": parameters["SEED"],
        }


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


def test_matrix_planner_supports_current_head_source_resolved_once_with_shared_warning(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    (repository / "untracked.txt").write_text("not captured\n", encoding="utf-8")
    template = PlanRequest(
        name="current sweep",
        command=ExperimentCommand.argv(("python", "train.py", "--lr={LR}", "--out={ARTIFACT_DIR}")),
        output_root=tmp_path / "reports",
        source_path=repository,
    )
    request = MatrixPlanRequest(template=template, parameters={"LR": [0.1, 0.01]})
    calls = 0
    original_resolver = planner_module.resolve_current_git_source

    def count_resolution(source_path):
        nonlocal calls
        calls += 1
        return original_resolver(source_path)

    monkeypatch.setattr(planner_module, "resolve_current_git_source", count_resolution)

    with pytest.warns(UserWarning, match="untracked files") as warning:
        experiments = plan_matrix(request)

    expected_commit = git(repository, "rev-parse", "HEAD")
    configurations = tuple(
        ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json")) for experiment in experiments
    )
    assert calls == 1
    assert len(warning) == 1
    assert {configuration.source.commit for configuration in configurations} == {expected_commit}
    assert {configuration.source.branch for configuration in configurations} == {
        git(repository, "branch", "--show-current")
    }
    assert all(configuration.source.untracked_files == ("untracked.txt",) for configuration in configurations)


def test_planner_reports_invalid_non_git_source_path(tmp_path):
    request = PlanRequest(
        name="invalid",
        command=ExperimentCommand.argv(("python", "train.py")),
        source_path=tmp_path,
        output_root=tmp_path / "reports",
    )

    with pytest.raises(PlanningError, match="find Git repository"):
        plan_experiment(request)
