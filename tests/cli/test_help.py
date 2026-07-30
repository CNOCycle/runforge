"""Tests for semantic RunForge CLI help."""

from __future__ import annotations

import re

import pytest

from runforge.cli import main
from runforge.cli.parser import build_parser


def _help(capsys, *arguments: str) -> str:
    with pytest.raises(SystemExit) as raised:
        main([*arguments, "--help"])
    assert raised.value.code == 0
    return capsys.readouterr().out


def _single_line(value: str) -> str:
    # argparse wraps long help text at column width, including inside existing
    # hyphenated words; undo that line-wrap artifact before collapsing whitespace.
    unwrapped = re.sub(r"-\n\s*", "-", value)
    return " ".join(unwrapped.split())


def test_top_level_help_lists_every_command_and_subcommand_guidance(capsys):
    output = _help(capsys)

    assert "Plan, inspect, and run reproducible Git-backed and non-Git experiments." in output
    for subcommand in ("plan", "launch", "matrix", "run", "retry", "discover", "worker"):
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
        (["retry", "-f", "experiment"], "force"),
        (["worker", "-s"], "stream_output"),
    ],
)
def test_short_flags_match_their_long_option_destinations(arguments, attribute):
    assert getattr(build_parser().parse_args(arguments), attribute) is True


def test_plan_help_states_every_semantic_default(capsys):
    output = _single_line(_help(capsys, "plan"))

    assert "--name NAME" in output
    assert "(default: exp)" in output
    assert output.count("(default: exp)") == 1
    assert "(default: SOURCE_REPOSITORY/reports for current-head and pinned-git" in output
    assert "required and must resolve outside --source-path for verified-directory and directory-snapshot)" in output
    assert "(default: current directory)" in output
    assert "verified-directory (live external path, re-verified before every run)" in output
    assert "directory-snapshot (self-contained captured copy)" in output
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
    assert "verified-directory" in output
    assert "directory-snapshot" in output


@pytest.mark.parametrize("subcommand", ["plan", "launch", "matrix"])
def test_planning_subcommands_advertise_non_git_source_modes(capsys, subcommand):
    output = _single_line(_help(capsys, subcommand))

    assert "current-head,pinned-git,verified-directory,directory-snapshot" in output
    assert "verified-directory (live external path, re-verified before every run)" in output
    assert "directory-snapshot (self-contained captured copy)" in output
    assert "must resolve outside --source-path for verified-directory and directory-snapshot" in output


def test_run_help_describes_safe_execution_defaults(capsys):
    run_output = _single_line(_help(capsys, "run"))

    assert "stream stdout and stderr while preserving log files (default: disabled)" in run_output


def test_retry_help_describes_state_policy_and_safe_defaults(capsys):
    output = _single_line(_help(capsys, "retry"))

    assert "failed experiment" in output
    assert "inprogress or claimed failed experiment additionally requires --force" in output
    assert "independently confirming its worker stopped (default: disabled)" in output
    assert "stream stdout and stderr while preserving log files (default: disabled)" in output


def test_discover_help_describes_read_only_listing(capsys):
    output = _single_line(_help(capsys, "discover"))

    assert "without changing status or executing commands" in output
    assert "directory to scan recursively (default: current directory)" in output


@pytest.mark.parametrize("subcommand", ["plan", "launch", "matrix", "run", "retry", "discover", "worker"])
def test_subcommand_help_never_exposes_raw_python_defaults(capsys, subcommand):
    output = _help(capsys, subcommand)

    assert "(default: None)" not in output
    assert "(default: False)" not in output


def test_worker_max_tasks_short_flag_sets_value():
    max_tasks = 2
    arguments = build_parser().parse_args(["worker", "-n", str(max_tasks)])
    assert arguments.max_tasks == max_tasks


def test_worker_help_describes_single_snapshot_and_budget(capsys):
    output = _single_line(_help(capsys, "worker"))

    assert "one discovery snapshot" in output
    assert "-n N, --max-tasks N" in output
    assert "must be positive when set" in output
    assert "stream stdout and stderr while preserving log files (default: disabled)" in output
