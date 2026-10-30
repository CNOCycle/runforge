"""Help, option scope, and child-command boundaries for this candidate."""

from __future__ import annotations

import re

import pytest

from runforge.cli import main
from runforge.cli.parser import build_parser


CLI_ERROR_EXIT = 2
QUOTA = 2


def _help(capsys, *arguments):
    with pytest.raises(SystemExit) as raised:
        main([*arguments, "--help"])
    assert raised.value.code == 0
    return capsys.readouterr().out


def _single_line(value):
    return " ".join(re.sub(r"-\n\s*", "-", value).split())


def test_version_flags_need_no_subcommand(capsys):
    for flag in ("--version", "-v"):
        with pytest.raises(SystemExit) as raised:
            main([flag])
        assert raised.value.code == 0
        assert capsys.readouterr().out.startswith("runforge 0.3.0+")


def test_plan_help_preserves_source_input_and_command_contract(capsys):
    output = _single_line(_help(capsys, "plan"))
    for fragment in (
        "--name NAME",
        "(default: exp)",
        "--source-path",
        "--source-mode",
        "current-head,pinned-git,verified-directory,directory-snapshot",
        "required and must resolve outside --source-path",
        "--env-file",
        "--input-tree",
        "--shell",
        "command placed after --",
    ):
        assert fragment in output
    assert output.count("(default: exp)") == 1
    assert "(default: None)" not in output
    assert "(default: False)" not in output


@pytest.mark.parametrize("leaf", ["launch", "run", "retry"])
def test_streaming_alias_and_default(leaf, capsys):
    output = _single_line(_help(capsys, leaf))
    assert "stream stdout and stderr while preserving log files (default: disabled)" in output
    args = [leaf, "-s", "--", "echo"] if leaf == "launch" else [leaf, "-s", "experiment"]
    assert build_parser().parse_args(args).stream_output


def test_child_command_tokens_after_separator_stay_opaque():
    child = ["matrix", "show", "--force", "two words", "schedule"]
    arguments = build_parser().parse_args(["plan", "--", *child])
    assert arguments.command == ["--", *child]


def test_staleness_options_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["discover", "--older-than", "6h", "--started-before", "2026-08-01T00:00:00Z"])
    assert raised.value.code == CLI_ERROR_EXIT
    assert "not allowed with argument" in capsys.readouterr().err


def test_bare_cli_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == CLI_ERROR_EXIT
    assert "required" in capsys.readouterr().err


def test_top_help_has_eight_canonical_operations(capsys):
    output = _help(capsys)
    for name in ("plan", "launch", "run", "retry", "discover", "mapping", "schedule", "workspaces"):
        assert name in output
    for old in ("matrix-show", "worker-cleanup", "worker-inspect"):
        assert old not in output
    assert "runforge SUBCOMMAND --help" in output


def test_matrix_is_a_plan_option_and_cannot_be_added_to_launch(capsys):
    assert "--matrix" in _help(capsys, "plan")
    with pytest.raises(SystemExit) as raised:
        main(["launch", "--matrix", "axes.json", "--", "echo", "hello"])
    assert raised.value.code == CLI_ERROR_EXIT


def test_run_quota_alias_and_required_paths(capsys):
    assert build_parser().parse_args(["run", "-n", "2", "reports"]).max_tasks == QUOTA
    with pytest.raises(SystemExit) as raised:
        main(["run"])
    assert raised.value.code == CLI_ERROR_EXIT


def test_priority_is_deferred_and_hold_release_are_exclusive(capsys):
    for arguments in (
        ["schedule", "--priority", "10", "experiment"],
        ["schedule", "--hold", "--release", "experiment"],
    ):
        with pytest.raises(SystemExit) as raised:
            main(arguments)
        assert raised.value.code == CLI_ERROR_EXIT
    assert "--priority" not in _help(capsys, "schedule")


@pytest.mark.parametrize("leaf", ["discover", "mapping", "schedule", "workspaces"])
def test_readonly_and_control_commands_reject_streaming(leaf, capsys):
    with pytest.raises(SystemExit) as raised:
        main([leaf, "--stream-output"])
    assert raised.value.code == CLI_ERROR_EXIT
