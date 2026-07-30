"""Tests for rendering the matrix configuration mapping."""

from __future__ import annotations

from runforge.cli.output import matrix_value_text, print_matrix_mapping
from runforge.planning.matrix_mapping import MatrixMapping, MatrixMappingRow


def _mapping(*rows: MatrixMappingRow, parameters: tuple[str, ...] = ("LR", "SEED")) -> MatrixMapping:
    return MatrixMapping(matrix_id="main_01234567_exp", parameters=parameters, rows=rows)


def test_mapping_table_aligns_columns_and_zero_pads_the_index(capsys):
    mapping = _mapping(
        MatrixMappingRow(index=0, dir_name="01234567_exp_0000", parameters={"LR": 0.001, "SEED": 1}),
        MatrixMappingRow(index=1, dir_name="01234567_exp_0001", parameters={"LR": 0.01, "SEED": 20}),
    )

    print_matrix_mapping(mapping)

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Matrix configuration mapping:"
    assert lines[1] == "index | dir_name          | LR    | SEED"
    assert lines[2] == "------+-------------------+-------+-----"
    assert lines[3] == "0000  | 01234567_exp_0000 | 0.001 | 1   "
    assert lines[4] == "0001  | 01234567_exp_0001 | 0.01  | 20  "


def test_mapping_table_reports_an_empty_row_set_without_failing(capsys):
    # A hand-edited or truncated artifact must not crash the renderer.
    print_matrix_mapping(_mapping())

    assert capsys.readouterr().out.splitlines() == [
        "Matrix configuration mapping:",
        "  no matrix rows recorded",
    ]


def test_mapping_table_renders_a_mapping_without_parameters(capsys):
    mapping = _mapping(
        MatrixMappingRow(index=0, dir_name="01234567_exp_0000", parameters={}),
        parameters=(),
    )

    print_matrix_mapping(mapping)

    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == "index | dir_name         "
    assert lines[3] == "0000  | 01234567_exp_0000"


def test_matrix_values_keep_strings_distinguishable_from_numbers():
    assert matrix_value_text(1) == "1"
    assert matrix_value_text("1") == '"1"'
    assert matrix_value_text(True) == "true"
    assert matrix_value_text(0.001) == "0.001"
    assert matrix_value_text(None) == "null"
