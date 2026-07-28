"""Command dispatch and workflow composition for the RunForge CLI."""

from __future__ import annotations

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
from runforge.execution.discovery import DiscoveryError, DiscoveryResult, discover_experiments
from runforge.execution.retry import RetryError, prepare_retry
from runforge.execution.worker import WorkerError, run_experiment
from runforge.planning.planner import MatrixPlanRequest, PlanningError, PlanRequest, plan_experiment, plan_matrix


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RunForge CLI and return its process exit code."""
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.subcommand == "discover":
            print_discover_arguments(arguments)
            return _discover(
                arguments.root,
                execute=arguments.execute,
                stream_output=arguments.stream_output,
                max_tasks=arguments.max_tasks,
            )
        if arguments.subcommand == "matrix":
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
        if arguments.subcommand in {"plan", "launch"}:
            request = planning_request(arguments)
            print_planning_arguments(arguments, request)
            experiment = _plan_with_warnings(request)
            print(f"Experiment plan created at: {experiment}", flush=True)
            exit_code = 0
            if arguments.subcommand == "launch":
                exit_code = run_experiment(
                    experiment,
                    stream_output=arguments.stream_output,
                    progress=print_worker_progress,
                )
            return exit_code
        if arguments.subcommand == "retry":
            print_retry_arguments(arguments)
            preparation = prepare_retry(arguments.experiment, force=arguments.force)
            if preparation.forced:
                print(
                    "warning: Forced retry cannot prove that the previous inprogress worker has stopped",
                    file=sys.stderr,
                    flush=True,
                )
            print(f"Previous attempt archived at: {preparation.archive}", flush=True)
            return run_experiment(
                preparation.experiment,
                stream_output=arguments.stream_output,
                progress=print_worker_progress,
            )
        print_run_arguments(arguments)
        return run_experiment(
            arguments.experiment,
            stream_output=arguments.stream_output,
            progress=print_worker_progress,
        )
    except (DiscoveryError, PlanningError, RetryError, WorkerError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _discover(root: Path, *, execute: bool, stream_output: bool, max_tasks: int | None) -> int:
    if stream_output and not execute:
        raise ValueError("--stream-output requires --execute")
    if max_tasks is not None and not execute:
        raise ValueError("--max-tasks requires --execute")
    result = discover_experiments(root)
    print_discovery(result)
    if not execute:
        return 2 if result.diagnostics else 0
    return _execute_discovered(result, stream_output=stream_output, max_tasks=max_tasks)


def _execute_discovered(
    result: DiscoveryResult,
    *,
    stream_output: bool,
    max_tasks: int | None,
) -> int:
    eligible = tuple(experiment for experiment in result.experiments if experiment.status.state == "created")
    selected = eligible if max_tasks is None else eligible[:max_tasks]
    deferred = len(eligible) - len(selected)
    completed = 0
    failed = 0
    for index, experiment in enumerate(selected, start=1):
        print(f"Selected experiment ({index}/{len(selected)}): {experiment.path}", flush=True)
        try:
            exit_code = run_experiment(
                experiment.path,
                stream_output=stream_output,
                progress=print_worker_progress,
            )
        except WorkerError:
            failed += 1
            continue
        if exit_code == 0:
            completed += 1
        else:
            failed += 1

    print("Execution summary:")
    print(f"  selected: {len(selected)}")
    print(f"  completed: {completed}")
    print(f"  failed: {failed}")
    print(f"  skipped: {len(result.experiments) - len(eligible)}")
    print(f"  deferred: {deferred}")
    print(f"  invalid: {len(result.diagnostics)}")
    if result.diagnostics:
        return 2
    return 1 if failed else 0


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
