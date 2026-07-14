"""CLI wrappers for planning, running, or launching one experiment."""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

from runforge.experiment_schema import ExperimentCommand
from runforge.planner import PlanningError, PlanRequest, plan_experiment
from runforge.source_metadata import PinnedGitSource
from runforge.worker import WorkerError, run_experiment


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RunForge CLI and return its process exit code."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.subcommand in {"plan", "launch"}:
            experiment = _plan_with_warnings(arguments)
            print(f"Experiment plan created at: {experiment}", flush=True)
            if arguments.subcommand == "launch":
                return run_experiment(experiment, stream_output=arguments.stream_output)
            return 0
        return run_experiment(arguments.experiment, stream_output=arguments.stream_output)
    except (PlanningError, WorkerError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runforge", description="Plan, launch, or run one Git-backed experiment.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    plan = subparsers.add_parser("plan", help="create one Git-backed experiment directory without execution")
    _add_planning_arguments(plan)

    launch = subparsers.add_parser("launch", help="create and immediately run one Git-backed experiment")
    _add_planning_arguments(launch)
    _add_stream_output_argument(launch)

    run = subparsers.add_parser("run", help="execute one explicit planned experiment directory")
    _add_stream_output_argument(run)
    run.add_argument("experiment", type=Path)
    return parser


def _add_planning_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="exp")
    parser.add_argument("--out-dir", type=Path, help="default: SOURCE_REPOSITORY/reports")
    parser.add_argument("--source-path", type=Path, default=Path("."))
    parser.add_argument("--source-mode", choices=("current-head", "pinned-git"), default="current-head")
    parser.add_argument("--commit", help="commit or ref for a pinned Git source")
    parser.add_argument("--patch", type=Path, help="optional patch for a pinned Git source")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--shell",
        action="store_true",
        help="interpret one command string as an explicit shell pipeline",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")


def _add_stream_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stream-output",
        action="store_true",
        help="stream stdout and stderr to the console while preserving log files",
    )


def _plan_with_warnings(arguments: argparse.Namespace) -> Path:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        experiment = _plan(arguments)
    for captured_warning in captured:
        print(f"warning: {captured_warning.message}", file=sys.stderr)
    return experiment


def _plan(arguments: argparse.Namespace) -> Path:
    parts = list(arguments.command)
    if parts and parts[0] == "--":
        parts.pop(0)
    if not parts:
        raise PlanningError("No command provided; place it after --")
    if arguments.shell:
        if len(parts) != 1:
            raise PlanningError("--shell requires one quoted command string after --")
        command = ExperimentCommand.shell(parts[0])
    else:
        command = ExperimentCommand.argv(parts)
    return plan_experiment(
        PlanRequest(
            name=arguments.name,
            command=command,
            output_root=arguments.out_dir,
            source_path=arguments.source_path,
            source=_pinned_source(arguments),
            environment=_environment(arguments.env_file),
        )
    )


def _pinned_source(arguments: argparse.Namespace) -> PinnedGitSource | None:
    if arguments.source_mode == "current-head":
        if arguments.commit is not None or arguments.patch is not None:
            raise PlanningError("--commit and --patch require --source-mode pinned-git")
        return None
    if arguments.commit is None:
        raise PlanningError("--source-mode pinned-git requires --commit")
    return PinnedGitSource(
        repository=arguments.source_path,
        commit=arguments.commit,
        patch=arguments.patch,
    )


def _environment(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PlanningError(f"Could not read environment file {path}: {error}") from error
    environment: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PlanningError(f"Invalid environment entry at {path}:{number}; expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise PlanningError(f"Empty environment key at {path}:{number}")
        environment[key] = value.strip().strip("'").strip('"')
    return environment
