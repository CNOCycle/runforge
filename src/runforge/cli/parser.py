"""Static argparse command tree for RunForge."""

from __future__ import annotations

import argparse
from pathlib import Path

from runforge.version import display_version


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
        description="Plan, inspect, and run reproducible Git-backed experiments.",
        epilog="Use 'runforge SUBCOMMAND --help' for detailed subcommand options.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {display_version()}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="create one Git-backed experiment directory without execution",
        description="Create one Git-backed experiment directory without executing its command.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_planning_arguments(plan)

    launch = subparsers.add_parser(
        "launch",
        help="create and immediately run one Git-backed experiment",
        description="Create one Git-backed experiment directory and execute it immediately.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_planning_arguments(launch)
    _add_stream_output_argument(launch)

    matrix = subparsers.add_parser(
        "matrix",
        help="create a pinned-source Cartesian experiment matrix",
        description="Create a Cartesian matrix of plans using one required pinned Git source.",
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    matrix.add_argument(
        "--matrix-file",
        type=Path,
        required=True,
        help="JSON object defining matrix parameters (required)",
    )
    _add_planning_arguments(matrix, pinned_only=True)

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
            "An inprogress experiment additionally requires --force."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    _add_stream_output_argument(retry)
    retry.add_argument(
        "--force",
        action="store_true",
        help="retry an inprogress experiment after independently confirming its worker stopped (default: disabled)",
    )
    retry.add_argument("experiment", type=Path, help="failed or interrupted experiment directory to retry")

    discover = subparsers.add_parser(
        "discover",
        help="list or sequentially execute planned experiments recursively",
        description=(
            "Recursively inspect planned experiments. The default mode only lists status; execution requires --execute."
        ),
        formatter_class=SemanticDefaultsHelpFormatter,
    )
    discover.add_argument(
        "root",
        type=Path,
        nargs="?",
        default=Path("."),
        help="directory to scan recursively (default: current directory)",
    )
    discover.add_argument(
        "--execute",
        action="store_true",
        help="run created experiments sequentially after discovery (default: disabled; list only)",
    )
    _add_stream_output_argument(discover)
    return parser


def _add_planning_arguments(parser: argparse.ArgumentParser, *, pinned_only: bool = False) -> None:
    parser.add_argument("--name", default="exp", help="short experiment name")
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="root directory for experiment outputs (default: SOURCE_REPOSITORY/reports)",
    )
    parser.add_argument(
        "--source-path",
        type=Path,
        default=Path("."),
        help="path within the source Git repository (default: current directory)",
    )
    if pinned_only:
        parser.set_defaults(source_mode="pinned-git")
    else:
        parser.add_argument(
            "--source-mode",
            choices=("current-head", "pinned-git"),
            default="current-head",
            help="source selection mode",
        )
    commit_requirement = "required" if pinned_only else "default: not set"
    parser.add_argument(
        "--commit",
        required=pinned_only,
        help=f"commit or ref for a pinned Git source ({commit_requirement})",
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
        "--stream-output",
        action="store_true",
        help="stream stdout and stderr while preserving log files (default: disabled)",
    )
