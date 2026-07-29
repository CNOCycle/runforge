"""Human-readable CLI summaries, discovery listings, and progress messages."""

from __future__ import annotations

import argparse
import shlex
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from runforge.execution.discovery import DiscoveryResult
from runforge.execution.worker import WorkerProgressEvent, WorkerResult
from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest
from runforge.schemas.directory_source import DirectorySnapshotSource, VerifiedDirectorySource
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.source import GitSource


_DISCOVERY_STATES = ("created", "init", "inprogress", "completed", "failed")


def print_discovery(result: DiscoveryResult) -> None:
    """Print discovered experiments, diagnostics, and lifecycle totals."""
    print(f"Experiments discovered under: {result.root}")
    if result.experiments:
        for experiment in result.experiments:
            print(
                f"{experiment.status.state} | attempt={experiment.status.attempt} "
                f"| name={experiment.configuration.name} | source={_source_identity_text(experiment.configuration)} "
                f"| path={experiment.path}"
            )
    else:
        print("No experiments found.")

    for diagnostic in result.diagnostics:
        print(f"invalid | path={diagnostic.path} | error={diagnostic.message}", file=sys.stderr)

    counts = Counter(experiment.status.state for experiment in result.experiments)
    print("Summary:")
    for state in _DISCOVERY_STATES:
        print(f"  {state}: {counts[state]}")
    print(f"  invalid: {len(result.diagnostics)}")


def _source_identity_text(configuration: ExperimentConfiguration) -> str:
    """Render one persisted source's identity for discovery listings."""
    source = configuration.source
    if isinstance(source, GitSource):
        return f"{source.branch}@{source.commit[:8]}"
    if isinstance(source, VerifiedDirectorySource):
        return f"verified@{source.tree_digest[:8]}"
    if isinstance(source, DirectorySnapshotSource):
        return f"snapshot@{source.tree_digest[:8]}"
    return "unknown"


def print_planning_arguments(
    arguments: argparse.Namespace,
    request: PlanRequest,
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> None:
    """Print effective arguments for plan, launch, or matrix."""
    rows = [
        ("name", request.name),
        ("output root", _effective_output_root(request)),
        ("source path", path_text(request.source_path)),
        ("source mode", arguments.source_mode),
        ("commit/ref", request.source.commit if request.source is not None else "not set"),
        ("patch", _optional_path_text(request.source.patch if request.source is not None else None)),
        ("environment file", _optional_path_text(arguments.env_file)),
        ("environment keys", _keys_text(request.environment)),
        ("input tree", _optional_path_text(arguments.input_tree)),
        ("planned inputs", str(len(request.inputs))),
        ("command mode", request.command.mode),
        ("shell mode", boolean_text(request.command.mode == "shell")),
        ("command", command_text(request.command)),
    ]
    if hasattr(arguments, "stream_output"):
        rows.append(("stream output", boolean_text(arguments.stream_output)))
    rows.extend(extra)
    _print_effective_arguments(arguments.subcommand, rows)


def print_run_arguments(arguments: argparse.Namespace) -> None:
    """Print effective arguments for one explicit run."""
    layout = ExperimentDirectory.resolve(arguments.experiment)
    _print_effective_arguments(
        "run",
        [
            ("experiment", str(layout.root)),
            ("stream output", boolean_text(arguments.stream_output)),
            *_execution_summary_rows(layout),
        ],
    )


def print_retry_arguments(arguments: argparse.Namespace) -> None:
    """Print effective arguments and attempt context for retry."""
    layout = ExperimentDirectory.resolve(arguments.experiment)
    status = _status_for_summary(layout)
    current_state = status.state if status is not None else "not available"
    current_attempt = str(status.attempt) if status is not None else "not available"
    next_attempt = str(status.attempt + 1) if status is not None else "not available"
    _print_effective_arguments(
        "retry",
        [
            ("experiment", str(layout.root)),
            ("force", boolean_text(arguments.force)),
            ("stream output", boolean_text(arguments.stream_output)),
            ("current state", current_state),
            ("current attempt", current_attempt),
            ("next attempt", next_attempt),
            *_execution_summary_rows(layout),
        ],
    )


def print_discover_arguments(arguments: argparse.Namespace) -> None:
    """Print the effective root for read-only discovery."""
    _print_effective_arguments("discover", [("root", path_text(arguments.root))])


def print_worker_arguments(arguments: argparse.Namespace) -> None:
    """Print effective arguments for one finite shared-worker invocation."""
    max_tasks = arguments.max_tasks
    effective_limit = "unlimited" if max_tasks is None else str(max_tasks)
    _print_effective_arguments(
        "worker",
        [
            ("root", path_text(arguments.root)),
            ("max tasks", effective_limit),
            ("stream output", boolean_text(arguments.stream_output)),
        ],
    )


def print_worker_summary(result: WorkerResult) -> None:
    """Print the counts from one finite shared-worker invocation."""
    # Flushed like progress output: claim warnings go to stderr, so an unflushed
    # summary on a redirected stdout could otherwise appear before them.
    print("Worker summary:", flush=True)
    print(f"  candidates: {result.candidates}", flush=True)
    print(f"  selected: {result.selected}", flush=True)
    print(f"  completed: {result.completed}", flush=True)
    print(f"  failed: {result.failed}", flush=True)
    print(f"  skipped: {result.skipped}", flush=True)
    print(f"    non-runnable: {result.not_runnable}", flush=True)
    print(f"    claim contention: {result.claim_contended}", flush=True)
    print(f"    stale after claim: {result.stale_skipped}", flush=True)
    print(f"  deferred: {result.deferred}", flush=True)
    print(f"  invalid: {result.invalid}", flush=True)


def print_worker_progress(event: WorkerProgressEvent) -> None:
    """Print one worker lifecycle event without changing worker behavior."""
    if event.phase == "preparing":
        print(f"Preparing experiment: {event.experiment}", flush=True)
        return
    if event.phase == "executing":
        command = command_text(event.command) if event.command is not None else "not available"
        print(f"Executing command: {command}", flush=True)
        if event.stream_output:
            print("  output mode: streaming and logging", flush=True)
        else:
            print(f"  stdout log: {event.stdout_log}", flush=True)
            print(f"  stderr log: {event.stderr_log}", flush=True)
        return
    if event.phase == "completed":
        print(f"Experiment completed with exit code {event.exit_code}: {event.experiment}", flush=True)
        return
    if event.phase == "warning":
        print(f"warning: {event.error}: {event.experiment}", file=sys.stderr, flush=True)
        return
    if event.exit_code is not None:
        message = f"Experiment failed with exit code {event.exit_code}: {event.experiment}"
    else:
        message = f"Experiment failed: {event.experiment}: {event.error}"
    print(message, file=sys.stderr, flush=True)


def command_text(command: ExperimentCommand) -> str:
    """Render a recorded command for human-readable console output."""
    if command.mode == "shell":
        return command.script or ""
    return shlex.join(command.arguments)


def path_text(path: Path) -> str:
    """Return an expanded absolute path for console summaries."""
    return str(Path(path).expanduser().resolve())


def boolean_text(value: bool) -> str:
    """Render a boolean option consistently in effective summaries."""
    return "enabled" if value else "disabled"


def _print_effective_arguments(subcommand: str, rows: Sequence[tuple[str, str]]) -> None:
    print(f"RunForge {subcommand} effective arguments:", flush=True)
    for label, value in rows:
        print(f"  {label}: {value}", flush=True)


def _configuration_for_summary(layout: ExperimentDirectory) -> ExperimentConfiguration | None:
    try:
        return layout.load_configuration()
    except ValueError:
        return None


def _execution_summary_rows(layout: ExperimentDirectory) -> list[tuple[str, str]]:
    configuration = _configuration_for_summary(layout)
    command = command_text(configuration.command) if configuration is not None else "not available"
    environment_keys = _keys_text(configuration.environment) if configuration is not None else "not available"
    return [
        ("recorded command", command),
        ("environment keys", environment_keys),
        ("artifact directory", str(layout.artifacts)),
        ("stdout log", str(layout.stdout_log)),
        ("stderr log", str(layout.stderr_log)),
    ]


def _status_for_summary(layout: ExperimentDirectory) -> ExperimentStatus | None:
    try:
        return layout.load_status()
    except ValueError:
        return None


def _effective_output_root(request: PlanRequest) -> str:
    if request.output_root is not None:
        return path_text(request.output_root)
    source_path = request.source.repository if request.source is not None else request.source_path
    try:
        return str(GitRepository.locate(source_path).root / "reports")
    except GitOperationError:
        return "not resolved (SOURCE_REPOSITORY/reports)"


def _optional_path_text(path: Path | None) -> str:
    return path_text(path) if path is not None else "not set"


def _keys_text(values: Mapping[str, object]) -> str:
    keys = sorted(values)
    return ", ".join(keys) if keys else "none"
