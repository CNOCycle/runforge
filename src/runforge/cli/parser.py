"""Static argparse command tree for RunForge."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from runforge.infrastructure.clock import parse_utc
from runforge.infrastructure.duration import DurationError, parse_duration
from runforge.version import display_version


def _positive_integer(value: str) -> int:
    """Parse a strictly positive integer CLI value."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _duration_argument(value: str) -> timedelta:
    """Parse a --older-than CLI value into a timedelta."""
    try:
        return parse_duration(value)
    except DurationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _utc_timestamp_argument(value: str) -> datetime:
    """Parse a --updated-before CLI value into an aware UTC datetime."""
    try:
        return parse_utc(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a UTC ISO 8601 timestamp (e.g. 2026-08-01T00:00:00Z)") from error


class SemanticDefaultsHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show literal defaults automatically while preserving semantic descriptions."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_text = action.help or ""
        if action.required or "(default:" in help_text:
            return help_text
        return super()._get_help_string(action)


def build_parser() -> argparse.ArgumentParser:
    """Build the complete RunForge argument parser."""
    parser = argparse.ArgumentParser(
        prog="runforge",
        description="Plan, inspect, and run reproducible Git-backed and non-Git experiments.",
        epilog="Use 'runforge SUBCOMMAND --help' for detailed subcommand options.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {display_version()}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="create one experiment directory without execution",
        description="Create one Git-backed or non-Git experiment directory without executing its command.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_planning_arguments(plan)

    launch = subparsers.add_parser(
        "launch",
        help="create and immediately run one experiment",
        description="Create one Git-backed or non-Git experiment directory and execute it immediately.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_planning_arguments(launch)
    _add_stream_output_argument(launch)

    matrix = subparsers.add_parser(
        "matrix",
        help="create a Cartesian experiment matrix from one shared source",
        description="Create a Cartesian matrix of plans that all share one resolved Git-backed or non-Git source.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    matrix.add_argument(
        "--matrix-file",
        type=Path,
        required=True,
        help="JSON object defining matrix parameters (required)",
    )
    _add_planning_arguments(matrix)

    matrix_show = subparsers.add_parser(
        "matrix-show",
        help="render a persisted matrix configuration mapping",
        description=(
            "Render a persisted matrix configuration mapping. This command only inspects; "
            "it never plans, executes, or changes experiment state."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    matrix_show.add_argument(
        "artifact",
        type=Path,
        help="matrix mapping JSON artifact written beside the generated experiment directories",
    )

    worker_inspect = subparsers.add_parser(
        "worker-inspect",
        help="render a worker workspace manifest",
        description=(
            "Render the runforge-worker.json manifest inside one worker workspace directory "
            "(a runforge-worker-* directory). This command only inspects; it never executes "
            "commands or removes anything."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    worker_inspect.add_argument(
        "workspace",
        type=Path,
        help="worker workspace directory (a runforge-worker-* directory) to inspect",
    )

    worker_cleanup = subparsers.add_parser(
        "worker-cleanup",
        help="list, and with explicit confirmation remove, leftover worker workspaces",
        description=(
            "List every runforge-worker-* directory directly under one root -- workspaces a killed "
            "worker left behind. Never removes anything by itself, and never selects a workspace by "
            "age: pass --confirm with the exact discovered paths to remove, one invocation at a time. "
            "A Git-sourced workspace is deregistered as a worktree, not just deleted."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    worker_cleanup.add_argument(
        "root",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "directory to scan for runforge-worker-* directories "
            "(default: RUNFORGE_WORKER_ROOT if set, else current directory)"
        ),
    )
    worker_cleanup.add_argument(
        "--confirm",
        type=Path,
        nargs="+",
        metavar="WORKSPACE",
        help="remove exactly these discovered workspace directories (default: list only, remove nothing)",
    )

    run = subparsers.add_parser(
        "run",
        help="execute one explicit planned experiment directory",
        description="Execute one explicit planned experiment directory.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_stream_output_argument(run)
    run.add_argument("experiment", type=Path, help="planned experiment directory to execute")

    retry = subparsers.add_parser(
        "retry",
        help="archive and rerun one failed or interrupted experiment",
        description=(
            "Archive the previous outputs and immediately rerun one failed experiment. "
            "An inprogress or claimed failed experiment additionally requires --force."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_stream_output_argument(retry)
    retry.add_argument(
        "-f",
        "--force",
        action="store_true",
        help=(
            "retry an inprogress or claimed failed experiment after independently confirming its worker stopped "
            "(default: disabled)"
        ),
    )
    retry.add_argument("experiment", type=Path, help="failed or interrupted experiment directory to retry")

    discover = subparsers.add_parser(
        "discover",
        help="list planned experiments recursively",
        description="Recursively inspect planned experiments without changing status or executing commands.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    discover.add_argument(
        "root",
        type=Path,
        nargs="?",
        default=Path("."),
        help="directory to scan recursively (default: current directory)",
    )
    staleness_group = discover.add_mutually_exclusive_group()
    staleness_group.add_argument(
        "--older-than",
        type=_duration_argument,
        metavar="DURATION",
        help=(
            "flag inprogress experiments started more than DURATION ago, e.g. 6h, 30m, 2d "
            "(default: not set; a hint only, never automatic recovery)"
        ),
    )
    staleness_group.add_argument(
        "--updated-before",
        type=_utc_timestamp_argument,
        metavar="TIMESTAMP",
        help=(
            "flag inprogress experiments started before this UTC ISO 8601 timestamp "
            "(default: not set; a hint only, never automatic recovery)"
        ),
    )

    worker = subparsers.add_parser(
        "worker",
        help="execute available experiments from one report root",
        description=(
            "Execute created and initialized experiments from one discovery snapshot, "
            "using an atomic claim before each existing executor run."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    worker.add_argument(
        "root",
        type=Path,
        nargs="?",
        default=Path("."),
        help="report root to scan once (default: current directory)",
    )
    worker.add_argument(
        "-n",
        "--max-tasks",
        type=_positive_integer,
        metavar="N",
        help="maximum experiments to execute (default: unlimited; must be positive when set)",
    )
    _add_stream_output_argument(worker)
    return parser


def _add_planning_arguments(parser: argparse.ArgumentParser, *, default_source_mode: str = "current-head") -> None:
    parser.add_argument("--name", default="exp", help="short experiment name")
    parser.add_argument(
        "--out-dir",
        type=Path,
        help=(
            "root directory for experiment outputs (default: SOURCE_REPOSITORY/reports for current-head and "
            "pinned-git; required and must resolve outside --source-path for verified-directory and "
            "directory-snapshot)"
        ),
    )
    parser.add_argument(
        "--source-path",
        type=Path,
        default=Path("."),
        help=(
            "path within the source Git repository for current-head/pinned-git, or the directory to verify/capture "
            "for verified-directory/directory-snapshot (default: current directory)"
        ),
    )
    parser.add_argument(
        "--source-mode",
        choices=("current-head", "pinned-git", "verified-directory", "directory-snapshot"),
        default=default_source_mode,
        help=(
            "source selection mode: Git current-HEAD or pinned-commit, or non-Git verified-directory "
            "(live external path, re-verified before every run) or directory-snapshot (self-contained captured copy)"
        ),
    )
    parser.add_argument(
        "--commit",
        help="commit or ref for a pinned Git source (default: not set); required when --source-mode is pinned-git",
    )
    parser.add_argument(
        "--patch",
        type=Path,
        help="optional patch for a pinned Git source (default: not set)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="KEY=VALUE environment override file (default: not set)",
    )
    parser.add_argument(
        "--input-tree",
        type=Path,
        help=(
            "UTF-8 configuration tree captured as immutable inputs; JSON renders structurally and YAML/YML/INI "
            "render with syntax validation (default: not set)"
        ),
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="interpret one command string as an explicit shell pipeline (default: disabled)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="experiment command placed after --")


def _add_stream_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-s",
        "--stream-output",
        action="store_true",
        help="stream stdout and stderr while preserving log files (default: disabled)",
    )
