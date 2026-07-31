"""Tests for strict matrix placeholder discovery."""

import pytest

from runforge.planning.inputs import InputTemplate
from runforge.planning.placeholders import command_placeholders, input_placeholders, validate_declared_placeholders
from runforge.schemas.experiment import ExperimentCommand


def test_command_placeholders_extracts_argv_identifiers():
    command = ExperimentCommand.argv(("python", "train.py", "--lr={LR}", "--seed={SEED}", "--again={LR}"))

    assert command_placeholders(command) == {"LR", "SEED"}


def test_shell_command_placeholders_are_not_interpreted():
    command = ExperimentCommand.shell("awk '{print $1}' input.txt && echo {LR}")

    assert command_placeholders(command) == set()


def test_input_placeholders_skip_copy_entries():
    inputs = (
        InputTemplate(path="copied.txt", kind="copy", content="{IGNORED}"),
        InputTemplate(path="config.yaml", kind="text-template", content="lr: {LR}\n"),
        InputTemplate(path="config.json", kind="json-template", content='{"seed": "{SEED}"}'),
    )

    assert input_placeholders(inputs) == {"LR", "SEED"}


def test_declared_placeholder_validation_allows_matrix_and_runforge_names():
    validate_declared_placeholders(
        ExperimentCommand.argv(("python", "train.py", "--lr={LR}", "--out={ARTIFACT_DIR}")),
        (InputTemplate(path="config.yaml", kind="text-template", content="seed: {SEED}"),),
        {"LR", "SEED"},
    )


def test_declared_placeholder_validation_rejects_unknown_names():
    with pytest.raises(ValueError, match=r"\{MISSING\}"):
        validate_declared_placeholders(
            ExperimentCommand.argv(("python", "train.py", "--value={MISSING}")),
            (),
            {"LR"},
        )
