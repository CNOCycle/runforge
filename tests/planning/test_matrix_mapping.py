"""Tests for the mapping between matrix combinations and plan directories."""

from __future__ import annotations

from pathlib import Path

import pytest

from runforge.infrastructure.json_store import load_json_object
from runforge.planning.matrix_mapping import (
    MatrixMapping,
    _reserve_mapping_file,
    build_matrix_mapping,
    matrix_directory_prefix,
    save_matrix_mapping,
)
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


def test_mapping_artifact_round_trips_through_json(tmp_path):
    experiments = _matrix(tmp_path, {"FLAG": [True], "N": [8], "LR": [0.5], "TAG": ["a-b"]})
    original = build_matrix_mapping(experiments)

    restored = MatrixMapping.from_dict(original.to_dict())

    assert restored == original
    assert original.to_dict()["kind"] == "runforge_matrix_mapping"
    assert original.to_dict()["schema_version"] == 1


def test_saved_mapping_lands_beside_the_experiment_directories(tmp_path):
    experiments = _matrix(tmp_path, {"SEED": [1, 2]})

    destination = save_matrix_mapping(experiments)

    assert destination.parent == experiments[0].parent
    assert destination.name.endswith("_matrix.json")
    assert load_json_object(destination) == build_matrix_mapping(experiments).to_dict()


def test_repeated_expansions_never_overwrite_an_existing_mapping(tmp_path):
    experiments = _matrix(tmp_path, {"SEED": [1, 2]})

    first = save_matrix_mapping(experiments)
    second = save_matrix_mapping(experiments)

    assert first != second
    assert first.name.endswith("_matrix.json")
    assert second.name.endswith("_matrix_0001.json")
    assert first.is_file() and second.is_file()


def test_concurrent_reservations_each_receive_a_distinct_file(tmp_path):
    # Exclusive creation must hand every caller its own name, even interleaved.
    band = tmp_path / "band"
    band.mkdir()

    reserved = [_reserve_mapping_file(band, "01234567_exp") for _ in range(25)]

    assert len(set(reserved)) == len(reserved)
    assert all(path.is_file() for path in reserved)


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ({"kind": "other", "schema_version": 1}, "unsupported matrix mapping kind"),
        ({"kind": "runforge_matrix_mapping", "schema_version": 2}, "schema version"),
        ({"kind": "runforge_matrix_mapping", "schema_version": 1, "matrix_id": ""}, "matrix_id"),
        (
            {"kind": "runforge_matrix_mapping", "schema_version": 1, "matrix_id": "m", "parameters": "LR"},
            "parameters must be an array",
        ),
        (
            {"kind": "runforge_matrix_mapping", "schema_version": 1, "matrix_id": "m", "parameters": [], "rows": {}},
            "rows must be an array",
        ),
        (
            {
                "kind": "runforge_matrix_mapping",
                "schema_version": 1,
                "matrix_id": "m",
                "parameters": [],
                "rows": [{"index": True, "dir_name": "d", "parameters": {}}],
            },
            "rows have invalid fields",
        ),
    ],
)
def test_mapping_artifact_rejects_unsupported_documents(document, match):
    with pytest.raises(ValueError, match=match):
        MatrixMapping.from_dict(document)


def _document(rows: list[dict], parameters: list[str] | None = None) -> dict:
    return {
        "kind": "runforge_matrix_mapping",
        "schema_version": 1,
        "matrix_id": "main_01234567_exp",
        "parameters": ["LR", "SEED"] if parameters is None else parameters,
        "rows": rows,
    }


@pytest.mark.parametrize(
    ("rows", "match"),
    [
        ([{"index": 0, "dir_name": "d0", "parameters": {"WRONG": 1}}], "does not match the declared parameters"),
        ([{"index": 0, "dir_name": "d0", "parameters": {"LR": 0.1}}], "does not match the declared parameters"),
        (
            [{"index": 0, "dir_name": "d0", "parameters": {"LR": 0.1, "SEED": 1, "EXTRA": 2}}],
            "does not match the declared parameters",
        ),
        (
            [{"index": 0, "dir_name": "d0", "parameters": {"LR": {"nested": 1}, "SEED": 1}}],
            "non-scalar value for 'LR'",
        ),
        (
            [{"index": 0, "dir_name": "d0", "parameters": {"LR": 0.1, "SEED": [1, 2]}}],
            "non-scalar value for 'SEED'",
        ),
    ],
)
def test_mapping_artifact_rejects_rows_that_disagree_with_their_columns(rows, match):
    with pytest.raises(ValueError, match=match):
        MatrixMapping.from_dict(_document(rows))


@pytest.mark.parametrize(
    "indexes",
    [[7, 7], [0, 0], [1, 2], [0, 2], [1, 0]],
)
def test_mapping_artifact_requires_rows_numbered_from_zero(indexes):
    rows = [
        {"index": index, "dir_name": f"d{position}", "parameters": {"LR": 0.1, "SEED": 1}}
        for position, index in enumerate(indexes)
    ]

    with pytest.raises(ValueError, match="numbered from zero without gaps"):
        MatrixMapping.from_dict(_document(rows))


def test_mapping_artifact_accepts_every_scalar_the_planner_produces():
    rows = [{"index": 0, "dir_name": "d0", "parameters": {"LR": 0.1, "SEED": True}}]

    mapping = MatrixMapping.from_dict(_document(rows))

    assert mapping.rows[0].parameters == {"LR": 0.1, "SEED": True}


def test_failed_mapping_write_releases_its_reserved_filename(tmp_path, monkeypatch):
    experiments = _matrix(tmp_path, {"SEED": [1, 2]})

    def fail_write(path, value):
        raise OSError("No space left on device")

    monkeypatch.setattr("runforge.planning.matrix_mapping.save_json_object", fail_write)

    # A failed write must raise ValueError, not OSError, and leave nothing behind.
    with pytest.raises(ValueError, match="could not write matrix mapping"):
        save_matrix_mapping(experiments)

    assert list(experiments[0].parent.glob("*_matrix*.json")) == []


def test_mapping_artifact_rejects_a_repeated_parameter_name():
    rows = [{"index": 0, "dir_name": "d0", "parameters": {"LR": 0.1}}]

    with pytest.raises(ValueError, match="must not repeat a name"):
        MatrixMapping.from_dict(_document(rows, parameters=["LR", "LR"]))


def test_mapping_artifact_rejects_a_null_parameter_value():
    # The planner accepts strings, numbers, and booleans, so null cannot be one.
    rows = [{"index": 0, "dir_name": "d0", "parameters": {"LR": 0.1, "SEED": None}}]

    with pytest.raises(ValueError, match="non-scalar value for 'SEED'"):
        MatrixMapping.from_dict(_document(rows))


def test_mapping_rejects_directories_with_different_parameter_sets(tmp_path):
    # Two expansions sharing a repository and name land in one band with one
    # prefix, so the identity check alone cannot tell them apart.
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('ok')\n"})

    def expand(parameters: dict[str, list]) -> tuple[Path, ...]:
        return plan_matrix(
            MatrixPlanRequest(
                template=PlanRequest(
                    name="same",
                    command=ExperimentCommand.argv(("python", "train.py")),
                    source_path=repository,
                    output_root=tmp_path / "reports",
                ),
                parameters=parameters,
            )
        )

    first = expand({"LR": [0.1]})
    second = expand({"SEED": [1]})
    assert first[0].parent == second[0].parent
    assert matrix_directory_prefix(first[0]) == matrix_directory_prefix(second[0])

    with pytest.raises(ValueError, match="one matrix parameter set"):
        build_matrix_mapping((*first, *second))
