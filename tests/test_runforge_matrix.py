"""Tests for the deterministic parameter matrix expansion."""

from __future__ import annotations

import pytest

from runforge.matrix import MatrixError, expand_matrix


def test_matrix_expansion_sorts_keys_and_preserves_axis_value_order():
    combinations = expand_matrix(
        {
            "SEED": [2, 1],
            "LR": [0.1, 0.01],
            "AMP": [True],
        }
    )

    assert combinations == (
        {"AMP": "true", "LR": "0.1", "SEED": "2"},
        {"AMP": "true", "LR": "0.1", "SEED": "1"},
        {"AMP": "true", "LR": "0.01", "SEED": "2"},
        {"AMP": "true", "LR": "0.01", "SEED": "1"},
    )


@pytest.mark.parametrize(
    "matrix",
    [
        {},
        {"ARTIFACT_DIR": ["reserved"]},
        {"bad-name": ["value"]},
        {"LR": []},
        {"LR": "not-an-array"},
        {"LR": [None]},
        {"LR": [float("inf")]},
    ],
)
def test_matrix_expansion_rejects_invalid_inputs(matrix):
    with pytest.raises(MatrixError):
        expand_matrix(matrix)
