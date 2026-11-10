"""Command dispatch and workflow composition for the RunForge CLI."""

from __future__ import annotations

import argparse
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from runforge.cli.output import (
    path_text,
    print_discovery,
    print_effective_arguments,
    print_mapping_arguments,
    print_matrix_mapping,
    print_matrix_mapping_file,
    print_onstart_left_unchanged,
    print_onstart_plan,
    print_onstart_warning,
    print_planning_arguments,
    print_worker_progress,
    print_worker_summary,
)
from runforge.cli.requests import matrix_request, planning_request
from runforge.execution.discovery import DiscoveryError, discover_paths
from runforge.execution.errors import WorkerError
from runforge.execution.onstart import ReportRootLock, find_interrupted
from runforge.execution.path_patterns import expand_path_arguments
from runforge.execution.retry import RetryError
from runforge.execution.scheduling import SchedulingError, set_hold
from runforge.execution.worker import BatchRequest, execute_paths
from runforge.infrastructure.claims import ClaimError
from runforge.planning.matrix_mapping import build_matrix_mapping, save_matrix_mapping
from runforge.planning.planner import MatrixPlanRequest, PlanRequest, plan_experiment, plan_matrix


def run(arguments: argparse.Namespace) -> int:
    rows = [
        ("paths", ", ".join(path_text(path) for path in arguments.paths)),
        ("stream output", "enabled" if arguments.stream_output else "disabled"),
    ]
    if arguments.retry:
        rows.append(("force", "enabled" if arguments.force else "disabled"))
        rows.append(("reuse artifacts", "enabled" if arguments.reuse_artifacts else "disabled"))
    else:
        rows.append(("max tasks", str(arguments.max_tasks) if arguments.max_tasks else "unlimited"))
    print_effective_arguments(arguments.command_name, rows)
    result = execute_paths(
        BatchRequest(
            expand_path_arguments(tuple(arguments.paths)),
            arguments.max_tasks,
            arguments.stream_output,
            retry=arguments.retry,
            force=arguments.force,
            reuse_artifacts=arguments.reuse_artifacts,
        ),
        progress=print_worker_progress,
    )
    print_worker_summary(result, label="Execution")
    return result.exit_code


def onstart(arguments: argparse.Namespace) -> int:
    """Restart interrupted experiments, then run the rest, on a single machine that just started."""
    root = arguments.path.expanduser()
    if not root.is_dir():
        raise DiscoveryError(f"Report root is not a directory: {root}")
    print_onstart_warning()
    if not arguments.single_node:
        print("error: refusing to continue without --single-node; nothing was changed", file=sys.stderr)
        return 2
    root = root.resolve()
    print_effective_arguments(
        "onstart",
        [
            ("report root", path_text(root)),
            ("single node", "confirmed"),
            ("stream output", "enabled" if arguments.stream_output else "disabled"),
        ],
    )
    with ReportRootLock(root):
        found = find_interrupted(root)
        print_onstart_plan(root, found)
        exit_code = 0
        for experiment in found.restart:
            exit_code = max(exit_code, _restart_interrupted(experiment, arguments.stream_output))
        result = execute_paths(BatchRequest((root,), None, arguments.stream_output), progress=print_worker_progress)
        print_worker_summary(result, label="Execution")
        if found.held or found.unstarted_claimed:
            print("Left unchanged:")
            print_onstart_left_unchanged(found)
        return max(exit_code, result.exit_code)


def _restart_interrupted(experiment: Path, stream_output: bool) -> int:
    """Force-retry one experiment with its artifacts restored; a failure is reported, never repeated."""
    try:
        result = execute_paths(
            BatchRequest((experiment,), None, stream_output, retry=True, force=True, reuse_artifacts=True),
            progress=print_worker_progress,
        )
    except (ClaimError, RetryError, WorkerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print_worker_summary(result, label="Recovery")
    return result.exit_code


def matrix(arguments: argparse.Namespace) -> int:
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
    # The plans are already published and runnable, so a mapping failure must
    # not hide them or make a successful expansion look like a failed one.
    try:
        print_matrix_mapping(build_matrix_mapping(experiments))
        print(f"Matrix mapping saved at: {save_matrix_mapping(experiments)}", flush=True)
    except ValueError as error:
        print(f"warning: Could not record the matrix mapping: {error}", file=sys.stderr, flush=True)
    print(f"Experiment plans created ({len(experiments)}):", flush=True)
    for experiment in experiments:
        print(f"  {experiment}", flush=True)
    return 0


def mapping(arguments: argparse.Namespace) -> int:
    print_mapping_arguments(arguments)
    print_matrix_mapping_file(arguments.artifact)
    return 0


def plan(arguments: argparse.Namespace) -> int:
    if arguments.matrix_file is not None:
        return matrix(arguments)
    request = planning_request(arguments)
    print_planning_arguments(arguments, request)
    experiment = _plan_with_warnings(request)
    print(f"Experiment plan created at: {experiment}", flush=True)
    if not arguments.execute_after_plan:
        return 0
    result = execute_paths(
        BatchRequest((experiment,), stream_output=arguments.stream_output), progress=print_worker_progress
    )
    print_worker_summary(result, label="Execution")
    return result.exit_code


def discover(arguments: argparse.Namespace) -> int:
    cutoff = _resolve_stale_cutoff(arguments)
    print_effective_arguments(
        "discover",
        [
            ("paths", ", ".join(path_text(path) for path in arguments.paths)),
            ("stale cutoff", cutoff.isoformat().replace("+00:00", "Z") if cutoff else "not set"),
        ],
    )
    result = discover_paths(expand_path_arguments(tuple(arguments.paths)))
    print_discovery(result, stale_cutoff=cutoff)
    return 2 if result.diagnostics else 0


def _resolve_stale_cutoff(arguments: argparse.Namespace) -> datetime | None:
    if arguments.older_than is not None:
        return datetime.now(timezone.utc) - arguments.older_than
    if arguments.started_before is not None:
        return arguments.started_before
    return None


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


def schedule(arguments: argparse.Namespace) -> int:
    # Directories without experiments, such as an empty one matched by a wildcard, are skipped.
    discovery = discover_paths(expand_path_arguments(tuple(arguments.paths)), read_scheduling=False)
    if not discovery.experiments and not discovery.diagnostics:
        raise DiscoveryError("No experiments found under the given paths")
    errors = [f"{item.path}: {item.message}" for item in discovery.diagnostics]
    for item in discovery.experiments:
        try:
            set_hold(item.path, held=arguments.held)
            print(f"{'Held' if arguments.held else 'Released'}: {item.path}")
        except SchedulingError as error:
            errors.append(str(error))
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 2 if errors else 0
