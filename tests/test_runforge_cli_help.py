"""Tests for semantic RunForge CLI help."""

from __future__ import annotations

import pytest

from runforge.cli import main


def _help(capsys, *arguments: str) -> str:
    with pytest.raises(SystemExit) as raised:
        main([*arguments, "--help"])
    assert raised.value.code == 0
    return capsys.readouterr().out


def _single_line(value: str) -> str:
    return " ".join(value.split())


def test_top_level_help_lists_every_command_and_subcommand_guidance(capsys):
    output = _help(capsys)

    assert "Plan, inspect, and run reproducible Git-backed experiments." in output
    for subcommand in ("plan", "launch", "matrix", "run"):
        assert subcommand in output
    assert "runforge SUBCOMMAND --help" in output


def test_plan_help_states_every_semantic_default(capsys):
    output = _single_line(_help(capsys, "plan"))

    assert "--name NAME" in output
    assert "(default: exp)" in output
    assert output.count("(default: exp)") == 1
    assert "(default: SOURCE_REPOSITORY/reports)" in output
    assert "(default: current directory)" in output
    assert "(default: current-head)" in output
    assert output.count("(default: current-head)") == 1
    assert "commit or ref for a pinned Git source (default: not set)" in output
    assert "optional patch for a pinned Git source (default: not set)" in output
    assert "environment override file (default: not set)" in output
    assert "shell pipeline (default: disabled)" in output


def test_matrix_help_describes_implicit_pinned_mode_and_required_inputs(capsys):
    output = _single_line(_help(capsys, "matrix"))

    assert "required pinned Git source" in output
    assert "JSON object defining matrix parameters (required)" in output
    assert "commit or ref for a pinned Git source (required)" in output
    assert "--source-mode" not in output


def test_run_help_describes_safe_execution_defaults(capsys):
    run_output = _single_line(_help(capsys, "run"))

    assert "stream stdout and stderr while preserving log files (default: disabled)" in run_output


@pytest.mark.parametrize("subcommand", ["plan", "launch", "matrix", "run"])
def test_subcommand_help_never_exposes_raw_python_defaults(capsys, subcommand):
    output = _help(capsys, subcommand)

    assert "(default: None)" not in output
    assert "(default: False)" not in output
