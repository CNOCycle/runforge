"""Tests for semantic RunForge CLI help."""

from __future__ import annotations

import pytest

from runforge.cli import main
from runforge.cli.parser import build_parser


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
    for subcommand in ("plan", "launch", "matrix", "run", "retry", "discover"):
        assert subcommand in output
    assert "runforge SUBCOMMAND --help" in output
    assert "-v, --version" in output


def test_version_flags_print_one_line_without_a_subcommand(capsys):
    for flag in ("--version", "-v"):
        with pytest.raises(SystemExit) as raised:
            main([flag])
        assert raised.value.code == 0
        assert capsys.readouterr().out.startswith("runforge 0.1.0+")


@pytest.mark.parametrize(
    "arguments, attribute",
    [
        (["launch", "-s", "--", "python", "train.py"], "stream_output"),
        (["run", "-s", "experiment"], "stream_output"),
        (["retry", "-s", "experiment"], "stream_output"),
        (["discover", "-s", "--execute"], "stream_output"),
        (["retry", "-f", "experiment"], "force"),
    ],
)
def test_short_flags_match_their_long_option_destinations(arguments, attribute):
    assert getattr(build_parser().parse_args(arguments), attribute) is True


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
    assert "configuration tree captured as immutable inputs" in output
    assert "shell pipeline (default: disabled)" in output


def test_matrix_help_describes_current_head_default_and_pinned_requirements(capsys):
    output = _single_line(_help(capsys, "matrix"))

    assert "JSON object defining matrix parameters (required)" in output
    assert "commit or ref for a pinned Git source (default: not set)" in output
    assert "required when --source-mode is pinned-git" in output
    assert "--source-mode" in output
    assert "(default: current-head)" in output


def test_run_help_describes_safe_execution_defaults(capsys):
    run_output = _single_line(_help(capsys, "run"))

    assert "stream stdout and stderr while preserving log files (default: disabled)" in run_output


def test_retry_help_describes_state_policy_and_safe_defaults(capsys):
    output = _single_line(_help(capsys, "retry"))

    assert "failed experiment" in output
    assert "inprogress experiment additionally requires --force" in output
    assert "independently confirming its worker stopped (default: disabled)" in output
    assert "stream stdout and stderr while preserving log files (default: disabled)" in output


def test_discover_help_describes_list_and_sequential_execution_defaults(capsys):
    output = _single_line(_help(capsys, "discover"))

    assert "default mode only lists status" in output
    assert "directory to scan recursively (default: current directory)" in output
    assert "run created experiments sequentially after discovery (default: disabled; list only)" in output
    assert "maximum number of experiments to launch with --execute (default: unlimited; requires --execute)" in output
    assert "stream stdout and stderr while preserving log files (default: disabled)" in output


@pytest.mark.parametrize("subcommand", ["plan", "launch", "matrix", "run", "retry", "discover"])
def test_subcommand_help_never_exposes_raw_python_defaults(capsys, subcommand):
    output = _help(capsys, subcommand)

    assert "(default: None)" not in output
    assert "(default: False)" not in output
