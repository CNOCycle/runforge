"""Tests for the experiment command and configuration schemas."""

from __future__ import annotations

from dataclasses import replace

import pytest

from runforge.schemas.experiment import (
    ExperimentCommand,
    ExperimentConfiguration,
    ExperimentSchemaError,
    ExperimentStatus,
)
from runforge.schemas.source import GitSource


CONFIGURATION_SCHEMA_VERSION = 3


def _source(tmp_path):
    return GitSource(repository=tmp_path.resolve(), commit="a" * 40, branch="feature/runforge")


def _configuration(tmp_path, command: ExperimentCommand) -> ExperimentConfiguration:
    return ExperimentConfiguration(
        experiment_id="baseline-001",
        name="baseline",
        command=command,
        environment={"RUN_MODE": "ablation"},
        source=_source(tmp_path),
        created_at="2026-07-11T00:00:00Z",
    )


def test_argument_command_and_configuration_round_trip(tmp_path):
    configuration = _configuration(tmp_path, ExperimentCommand.argv(("python", "train.py", "--out", "{ARTIFACT_DIR}")))

    decoded = ExperimentConfiguration.from_dict(configuration.to_dict())

    assert decoded == configuration
    assert decoded.command.to_dict() == {"mode": "argv", "arguments": ["python", "train.py", "--out", "{ARTIFACT_DIR}"]}


def test_configuration_records_matrix_parameters_and_decodes_schema_version_one(tmp_path):
    configuration = replace(
        _configuration(tmp_path, ExperimentCommand.argv(("python", "train.py"))),
        parameters={"LR": 0.01, "SEED": 2, "AMP": False},
    )
    payload = configuration.to_dict()

    assert payload["schema_version"] == CONFIGURATION_SCHEMA_VERSION
    assert payload["parameters"] == {"LR": 0.01, "SEED": 2, "AMP": False}
    assert ExperimentConfiguration.from_dict(payload) == configuration

    legacy = dict(payload)
    legacy["schema_version"] = 2
    legacy["parameters"] = {"LR": "0.01"}
    assert ExperimentConfiguration.from_dict(legacy).parameters == {"LR": "0.01"}

    legacy["schema_version"] = 1
    legacy.pop("parameters")
    assert ExperimentConfiguration.from_dict(legacy).parameters == {}


def test_shell_pipeline_template_renders_artifact_dir_without_mutating_template(tmp_path):
    template = ExperimentCommand.shell(
        "python train.py --out '{ARTIFACT_DIR}' && python evaluate.py --weights '{ARTIFACT_DIR}/weights.pt'"
    )

    rendered = template.render_placeholders({"ARTIFACT_DIR": "/reports/baseline/artifacts"})

    assert template.script is not None and "{ARTIFACT_DIR}" in template.script
    assert rendered == ExperimentCommand.shell(
        "python train.py --out '/reports/baseline/artifacts' "
        "&& python evaluate.py --weights '/reports/baseline/artifacts/weights.pt'"
    )
    assert _configuration(tmp_path, rendered).command == rendered


def test_placeholder_rendering_is_one_pass_and_does_not_rewrite_inserted_values():
    template = ExperimentCommand.argv(("{FIRST}", "{SECOND}"))

    rendered = template.render_placeholders({"FIRST": "{SECOND}", "SECOND": "done"})

    assert rendered.arguments == ("{SECOND}", "done")


def test_status_round_trip_is_separate_from_configuration(tmp_path):
    configuration = _configuration(tmp_path, ExperimentCommand.argv(("python", "train.py")))
    status = ExperimentStatus(
        state="inprogress",
        attempt=1,
        updated_at="2026-07-11T00:01:00Z",
        started_at="2026-07-11T00:01:00Z",
    )

    assert ExperimentConfiguration.from_dict(configuration.to_dict()) == configuration
    assert ExperimentStatus.from_dict(status.to_dict()) == status


def test_schema_rejects_invalid_command_status_and_unknown_configuration_field(tmp_path):
    with pytest.raises(ExperimentSchemaError, match="command.arguments"):
        ExperimentCommand(mode="argv", arguments="python train.py")  # type: ignore[arg-type]
    with pytest.raises(ExperimentSchemaError, match="shell command"):
        ExperimentCommand(mode="shell", arguments=("python",), script="python train.py")
    with pytest.raises(ExperimentSchemaError, match="status.attempt"):
        ExperimentStatus(state="created", attempt=True, updated_at="2026-07-11T00:00:00Z")

    payload = _configuration(tmp_path, ExperimentCommand.argv(("python", "train.py"))).to_dict()
    payload["future_field"] = True
    with pytest.raises(ExperimentSchemaError, match="Unknown configuration field"):
        ExperimentConfiguration.from_dict(payload)
