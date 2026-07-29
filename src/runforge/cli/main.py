"""Command dispatch and workflow composition for the RunForge CLI."""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

from runforge.cli.output import (
    path_text,
    print_discover_arguments,
    print_discovery,
    print_planning_arguments,
    print_retry_arguments,
    print_run_arguments,
    print_worker_progress,
)
from runforge.cli.parser import build_parser
from runforge.cli.requests import matrix_request, planning_request
from runforge.execution.discovery import DiscoveryError, discover_experiments
from runforge.execution.retry import RetryError, prepare_retry
from runforge.execution.worker import WorkerError, run_experiment
from runforge.planning.planner import MatrixPlanRequest, PlanningError, PlanRequest, plan_experiment, plan_matrix


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RunForge CLI and return its process exit code."""
    arguments = build_parser().parse_args(argv)
    try:
        return _dispatch(arguments)
    except (DiscoveryError, PlanningError, RetryError, WorkerError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.subcommand == "discover":
        return _run_discover_command(arguments)
    if arguments.subcommand == "matrix":
        return _run_matrix_command(arguments)
    if arguments.subcommand in {"plan", "launch"}:
        return _run_plan_command(arguments)
    if arguments.subcommand == "retry":
        return _run_retry_command(arguments)
    print_run_arguments(arguments)
    return run_experiment(
        arguments.experiment,
        stream_output=arguments.stream_output,
        progress=print_worker_progress,
    )


def _run_matrix_command(arguments: argparse.Namespace) -> int:
    request = matrix_request(arguments)
    print_planning_arguments(
        arguments,
        request.template,
        extra=(
            ("matrix file", path_text(arguments.matrix_file)),
            ("matrix combinations", str(len(request.combinations))),
        ),
    )
    experiments = _plan_matrix_with_warnings(request)
    print(f"Experiment plans created ({len(experiments)}):", flush=True)
    for experiment in experiments:
        print(f"  {experiment}", flush=True)
    return 0


def _run_plan_command(arguments: argparse.Namespace) -> int:
    request = planning_request(arguments)
    print_planning_arguments(arguments, request)
    experiment = _plan_with_warnings(request)
    print(f"Experiment plan created at: {experiment}", flush=True)
    if arguments.subcommand != "launch":
        return 0
    return run_experiment(
        experiment,
        stream_output=arguments.stream_output,
        progress=print_worker_progress,
    )


def _run_retry_command(arguments: argparse.Namespace) -> int:
    print_retry_arguments(arguments)
    preparation = prepare_retry(arguments.experiment, force=arguments.force)
    if preparation.forced:
        print(
            "warning: Forced retry cannot prove that the previous worker has stopped",
            file=sys.stderr,
            flush=True,
        )
    print(f"Previous attempt archived at: {preparation.archive}", flush=True)
    return run_experiment(
        preparation.experiment,
        stream_output=arguments.stream_output,
        progress=print_worker_progress,
    )


def _run_discover_command(arguments: argparse.Namespace) -> int:
    print_discover_arguments(arguments)
    result = discover_experiments(arguments.root)
    print_discovery(result)
    return 2 if result.diagnostics else 0


def _plan_with_warnings(request: PlanRequest) -> Path:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        experiment = plan_experiment(request)
    for captured_warning in captured:
        print(f"warning: {captured_warning.message}", file=sys.stderr)
    return experiment


def _plan_matrix_with_warnings(request: MatrixPlanRequest) -> tuple[Path, ...]:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        experiments = plan_matrix(request)
    for captured_warning in captured:
        print(f"warning: {captured_warning.message}", file=sys.stderr)
    return experiments
