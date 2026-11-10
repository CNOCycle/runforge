"""Static argparse command tree for RunForge."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from runforge.cli import commands
from runforge.infrastructure.clock import parse_utc
from runforge.infrastructure.duration import DurationError, parse_duration
from runforge.version import display_version


_WORKDIR_HELP = (
    "Workdir: Git and directory-snapshot plans run in a temporary .workspace "
    "directory inside each experiment, removed after the command exits; one left "
    "by an interrupted execution is deleted before the next execution of that "
    "experiment. Verified-directory plans run in their original source directory."
)


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
    """Parse a --started-before CLI value into an aware UTC datetime."""
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


class SemanticRawDescriptionHelpFormatter(SemanticDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Preserve example layout while showing semantic defaults."""


def build_parser() -> argparse.ArgumentParser:
    """Build the eight explicit, flat operation selectors."""
    parser = argparse.ArgumentParser(
        prog="runforge",
        description="Plan, inspect, and run reproducible Git-backed and non-Git experiments.",
        epilog="Use 'runforge SUBCOMMAND --help' for detailed subcommand options.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {display_version()}")
    leaves = parser.add_subparsers(dest="subcommand", required=True)
    for name in ("plan", "launch"):
        leaf = leaves.add_parser(
            name,
            help="publish one plan" if name == "plan" else "publish and execute one plan",
            epilog=_WORKDIR_HELP if name == "launch" else None,
            formatter_class=SemanticDefaultsHelpFormatter,
        )
        _add_planning_arguments(leaf)
        leaf.set_defaults(
            handler=commands.plan, command_name=name, execute_after_plan=name == "launch", matrix_file=None
        )
        if name == "plan":
            leaf.add_argument(
                "--matrix",
                dest="matrix_file",
                type=Path,
                help="JSON object defining matrix parameters (default: not set)",
            )
        else:
            _add_stream_output_argument(leaf)
    for name in ("run", "retry"):
        leaf = leaves.add_parser(
            name,
            help="execute eligible experiments" if name == "run" else "archive and retry failed experiments",
            epilog=_WORKDIR_HELP,
            formatter_class=SemanticDefaultsHelpFormatter,
        )
        leaf.add_argument(
            "paths",
            type=Path,
            nargs="+",
            metavar="PATH",
            help="experiment or report-root paths, or quoted wildcard patterns",
        )
        _add_stream_output_argument(leaf)
        leaf.set_defaults(
            handler=commands.run,
            command_name=name,
            retry=name == "retry",
            force=False,
            max_tasks=None,
            reuse_artifacts=False,
        )
        if name == "run":
            leaf.add_argument(
                "-n",
                "--max-tasks",
                type=_positive_integer,
                metavar="N",
                help="successful-completion quota across queue refills (default: unlimited)",
            )
        else:
            leaf.add_argument(
                "--reuse-artifacts",
                action="store_true",
                help="copy previous artifacts for application checkpoint recovery (default: disabled)",
            )
            leaf.add_argument(
                "-f",
                "--force",
                action="store_true",
                help="retry explicit experiment paths after confirming the old process stopped (default: disabled)",
            )
    leaf = leaves.add_parser(
        "onstart",
        help="entry point for an interruptible machine: restart interrupted experiments, then run the rest",
        description=(
            "Entry point for an interruptible machine; run it each time the machine starts. Restarts every "
            "experiment under PATH that an interruption left inprogress, then processes the remaining plans like "
            "'runforge run PATH'. WARNING: your program must be resumable. RunForge restarts the recorded command "
            "and restores the previous artifacts to RUNFORGE_ARTIFACT_DIR but does not resume the process. "
            "WARNING: the restarts are forced, which is only safe on one machine that has just started while no "
            "other machine works on this report root; a forced retry of an experiment that is still running starts "
            "it a second time. Pass --single-node to confirm."
        ),
        epilog=_WORKDIR_HELP,
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    leaf.add_argument("path", type=Path, metavar="PATH", help="report root holding the published plans")
    leaf.add_argument(
        "--single-node",
        action="store_true",
        help="confirm that this machine has just started and no other machine works on PATH (default: disabled)",
    )
    _add_stream_output_argument(leaf)
    leaf.set_defaults(handler=commands.onstart, command_name="onstart")
    leaf = leaves.add_parser(
        "discover", help="inspect experiment paths without execution", formatter_class=SemanticDefaultsHelpFormatter
    )
    leaf.add_argument(
        "paths",
        type=Path,
        nargs="*",
        default=[Path(".")],
        metavar="PATH",
        help="paths or quoted wildcard patterns to inspect (default: current directory)",
    )
    hints = leaf.add_mutually_exclusive_group()
    hints.add_argument(
        "--older-than",
        type=_duration_argument,
        metavar="DURATION",
        help="read-only age hint (default: not set)",
    )
    hints.add_argument(
        "--started-before",
        dest="started_before",
        type=_utc_timestamp_argument,
        metavar="TIMESTAMP",
        help="read-only start-time cutoff (default: not set)",
    )
    leaf.set_defaults(handler=commands.discover, command_name="discover")
    leaf = leaves.add_parser(
        "mapping",
        help="render a saved matrix mapping without changing experiments",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    leaf.add_argument("artifact", type=Path)
    leaf.set_defaults(handler=commands.mapping, command_name="mapping")
    leaf = leaves.add_parser(
        "schedule",
        help="hold or release pending work without changing execution history",
        epilog=(
            "Examples:\n"
            '  runforge schedule --hold "$REPORT_ROOT/**/*baseline*"\n'
            '  runforge schedule --hold "$EXPERIMENT1" "$EXPERIMENT2"\n'
            '  runforge schedule --release "$REPORT_ROOT/**/*baseline*"\n\n'
            "PATH accepts filesystem wildcards; ** matches any number of directory levels (one per pattern).\n"
            "Quote a pattern so RunForge expands it and skips matrix mapping files.\n"
            "Unmatched patterns are errors."
        ),
        formatter_class=SemanticRawDescriptionHelpFormatter,
    )
    action = leaf.add_mutually_exclusive_group(required=True)
    action.add_argument("--hold", dest="held", action="store_true", help="prevent future execution of selected plans")
    action.add_argument(
        "--release", dest="held", action="store_false", help="remove a scheduling hold, never an execution claim"
    )
    leaf.add_argument("paths", type=Path, nargs="+", metavar="PATH", help="experiment paths, report roots, or patterns")
    leaf.set_defaults(handler=commands.schedule, command_name="schedule")
    return parser


def _add_planning_arguments(parser: argparse.ArgumentParser) -> None:
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
        default="current-head",
        help=(
            "source selection mode: Git current-HEAD or pinned-commit, or non-Git verified-directory "
            "(live external path, re-verified before every run) or directory-snapshot (captured copy of the source)"
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
