"""Tests for the mapping between matrix combinations and plan directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from runforge.planning.matrix_mapping import build_matrix_mapping, matrix_directory_prefix
from runforge.planning.planner import MatrixPlanRequest, PlanRequest, plan_matrix
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


EXPECTED_COMBINATIONS = 4


def _matrix(tmp_path: Path, parameters: dict[str, list], name: str = "exp") -> tuple[Path, ...]:
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('ok')\n"})
    return plan_matrix(
        MatrixPlanRequest(
            template=PlanRequest(
                name=name,
                command=ExperimentCommand.argv(("python", "train.py")),
                source_path=repository,
                output_root=tmp_path / "reports",
            ),
            parameters=parameters,
        )
    )


def test_mapping_rows_follow_planning_order_and_sorted_axes(tmp_path):
    # Declared SEED first, but expansion sorts axes, so columns must match that.
    experiments = _matrix(tmp_path, {"SEED": [1, 2], "LR": [0.001, 0.01]})

    mapping = build_matrix_mapping(experiments)

    assert mapping.parameters == ("LR", "SEED")
    assert len(mapping.rows) == EXPECTED_COMBINATIONS
    assert [row.index for row in mapping.rows] == [0, 1, 2, 3]
    assert [row.dir_name for row in mapping.rows] == [path.name for path in experiments]
    assert [row.parameters for row in mapping.rows] == [
        {"LR": 0.001, "SEED": 1},
        {"LR": 0.001, "SEED": 2},
        {"LR": 0.01, "SEED": 1},
        {"LR": 0.01, "SEED": 2},
    ]


def test_mapping_preserves_scalar_parameter_types(tmp_path):
    experiments = _matrix(tmp_path, {"FLAG": [True], "N": [8], "LR": [0.5], "TAG": ["a-b"]})

    values = build_matrix_mapping(experiments).rows[0].parameters

    assert values == {"FLAG": True, "N": 8, "LR": 0.5, "TAG": "a-b"}
    assert [type(value).__name__ for value in (values["FLAG"], values["N"], values["LR"], values["TAG"])] == [
        "bool",
        "int",
        "float",
        "str",
    ]


def test_mapping_identity_names_the_band_and_shared_prefix(tmp_path):
    experiments = _matrix(tmp_path, {"SEED": [1, 2]}, name="sweep")

    mapping = build_matrix_mapping(experiments)

    band = experiments[0].parent.name
    assert mapping.matrix_id == f"{band}_{matrix_directory_prefix(experiments[0])}"
    assert mapping.matrix_id.endswith("_sweep")


def test_mapping_rejects_an_empty_expansion():
    with pytest.raises(ValueError, match="no experiment directories"):
        build_matrix_mapping(())


def test_mapping_rejects_directories_from_different_expansions(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = _matrix(tmp_path / "a", {"SEED": [1]}, name="first")
    second = _matrix(tmp_path / "b", {"SEED": [1]}, name="second")

    with pytest.raises(ValueError, match="one matrix expansion"):
        build_matrix_mapping((*first, *second))
