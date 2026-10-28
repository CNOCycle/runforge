"""Command dispatch and workflow composition for the RunForge CLI."""

from __future__ import annotations

import argparse
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from runforge.cli.output import (
    path_text,
    print_discovered_workspaces,
    print_discovery,
    print_effective_arguments,
    print_matrix_mapping,
    print_matrix_mapping_file,
    print_matrix_show_arguments,
    print_planning_arguments,
    print_removed_workspace,
    print_worker_cleanup_arguments,
    print_worker_progress,
    print_worker_summary,
)
from runforge.cli.requests import matrix_request, planning_request
from runforge.execution.discovery import discover_paths
from runforge.execution.scheduling import SchedulingError, set_hold
from runforge.execution.worker import BatchRequest, WorkerError, execute_paths
from runforge.execution.workspace_cleanup import (
    default_workspace_root,
    discover_workspaces,
    remove_workspace,
)
from runforge.planning.matrix_mapping import build_matrix_mapping, save_matrix_mapping
from runforge.planning.planner import MatrixPlanRequest, PlanRequest, plan_experiment, plan_matrix


def run(arguments: argparse.Namespace) -> int:
    rows = [
        ("paths", ", ".join(path_text(path) for path in arguments.paths)),
        ("stream output", "enabled" if arguments.stream_output else "disabled"),
    ]
    if arguments.retry:
        rows.append(("force", "enabled" if arguments.force else "disabled"))
    else:
        rows.append(("max tasks", str(arguments.max_tasks) if arguments.max_tasks else "unlimited"))
    print_effective_arguments(arguments.command_name, rows)
    result = execute_paths(
        BatchRequest(
            tuple(arguments.paths),
            arguments.max_tasks,
            arguments.stream_output,
            retry=arguments.retry,
            force=arguments.force,
        ),
        progress=print_worker_progress,
    )
    print_worker_summary(result, label="Execution")
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


def matrix_show(arguments: argparse.Namespace) -> int:
    print_matrix_show_arguments(arguments)
    print_matrix_mapping_file(arguments.artifact)
    return 0


def workspace_cleanup(arguments: argparse.Namespace) -> int:
    root = arguments.root if arguments.root is not None else default_workspace_root()
    print_worker_cleanup_arguments(arguments, root)
    discovered = discover_workspaces(root)
    print_discovered_workspaces(discovered)
    if arguments.confirm is None:
        return 0
    discovered_paths = {workspace.path for workspace in discovered}
    requested = tuple(Path(path).expanduser().resolve() for path in arguments.confirm)
    unknown = tuple(path for path in requested if path not in discovered_paths)
    if unknown:
        raise WorkerError(
            "--remove named a path that discovery did not find: " + ", ".join(str(path) for path in unknown)
        )
    for path in requested:
        remove_workspace(path)
        print_removed_workspace(path)
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
    result = discover_paths(tuple(arguments.paths))
    print_discovery(result, stale_cutoff=cutoff)
    return 2 if result.diagnostics else 0


def _resolve_stale_cutoff(arguments: argparse.Namespace) -> datetime | None:
    if arguments.older_than is not None:
        return datetime.now(timezone.utc) - arguments.older_than
    if arguments.updated_before is not None:
        return arguments.updated_before
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
    discovery = discover_paths(tuple(arguments.paths), require_each=True, read_scheduling=False)
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
