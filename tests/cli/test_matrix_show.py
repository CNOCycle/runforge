"""Tests for the read-only matrix mapping inspection command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runforge.cli import main
from runforge.infrastructure.json_store import save_json_object
from runforge.planning.matrix_mapping import save_matrix_mapping
from runforge.planning.planner import MatrixPlanRequest, PlanRequest, plan_matrix
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


CLI_ERROR_EXIT = 2
EXPECTED_ROWS = 2


def _artifact(tmp_path: Path) -> Path:
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('ok')\n"})
    experiments = plan_matrix(
        MatrixPlanRequest(
            template=PlanRequest(
                name="sweep",
                command=ExperimentCommand.argv(("python", "train.py", "--lr={LR}", "--seed={SEED}")),
                source_path=repository,
                output_root=tmp_path / "reports",
            ),
            parameters={"SEED": [1, 2], "LR": [0.001]},
        )
    )
    return save_matrix_mapping(experiments)


def test_matrix_show_renders_a_persisted_mapping(tmp_path, capsys):
    artifact = _artifact(tmp_path)

    assert main(["matrix-show", str(artifact)]) == 0

    output = capsys.readouterr().out
    assert "RunForge matrix-show effective arguments:" in output
    assert f"  artifact: {artifact}" in output
    assert "Matrix identity: " in output
    assert "index | dir_name" in output
    assert "| LR    | SEED" in output
    assert output.count("_sweep_00") >= EXPECTED_ROWS


def test_matrix_show_leaves_every_experiment_untouched(tmp_path):
    artifact = _artifact(tmp_path)
    before = {path: path.read_bytes() for path in sorted(artifact.parent.rglob("*.json"))}

    assert main(["matrix-show", str(artifact)]) == 0

    assert {path: path.read_bytes() for path in sorted(artifact.parent.rglob("*.json"))} == before


def test_matrix_show_reports_an_empty_row_set_without_a_traceback(tmp_path, capsys):
    artifact = tmp_path / "empty_matrix.json"
    save_json_object(
        artifact,
        {
            "kind": "runforge_matrix_mapping",
            "schema_version": 1,
            "matrix_id": "main_01234567_exp",
            "parameters": [],
            "rows": [],
        },
    )

    assert main(["matrix-show", str(artifact)]) == 0

    assert "no matrix rows recorded" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "Could not read JSON object"),
        ("{ broken\n", "Could not read JSON object"),
        (json.dumps({"kind": "other", "schema_version": 1}), "unsupported matrix mapping kind"),
        (json.dumps([1, 2]), "Expected a JSON object"),
        (
            json.dumps(
                {
                    "kind": "runforge_matrix_mapping",
                    "schema_version": 1,
                    "matrix_id": "m",
                    "parameters": ["LR", "SEED"],
                    "rows": [{"index": 0, "dir_name": "d0", "parameters": {"WRONG": 1}}],
                }
            ),
            "does not match the declared parameters",
        ),
        (
            json.dumps(
                {
                    "kind": "runforge_matrix_mapping",
                    "schema_version": 1,
                    "matrix_id": "m",
                    "parameters": ["LR"],
                    "rows": [{"index": 0, "dir_name": "d0", "parameters": {"LR": [1, 2]}}],
                }
            ),
            "non-scalar value",
        ),
        (
            json.dumps(
                {
                    "kind": "runforge_matrix_mapping",
                    "schema_version": 1,
                    "matrix_id": "m",
                    "parameters": ["LR"],
                    "rows": [{"index": 7, "dir_name": "d0", "parameters": {"LR": 1}}],
                }
            ),
            "numbered from zero",
        ),
    ],
)
def test_matrix_show_rejects_unreadable_artifacts_cleanly(tmp_path, capsys, content, match):
    artifact = tmp_path / "artifact.json"
    if content is not None:
        artifact.write_text(content, encoding="utf-8")

    assert main(["matrix-show", str(artifact)]) == CLI_ERROR_EXIT

    assert match in capsys.readouterr().err
