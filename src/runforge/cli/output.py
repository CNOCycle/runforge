"""Human-readable CLI summaries, discovery listings, and progress messages."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from runforge.execution.discovery import DiscoveryResult
from runforge.execution.sources import load_worker_manifest
from runforge.execution.staleness import describe_staleness
from runforge.execution.worker import WorkerProgressEvent, WorkerResult
from runforge.execution.workspace_cleanup import DiscoveredWorkspace
from runforge.infrastructure.duration import format_duration
from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.matrix_mapping import MatrixMapping, load_matrix_mapping
from runforge.planning.planner import PlanRequest
from runforge.schemas.directory_source import DirectorySnapshotSource, VerifiedDirectorySource
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.source import GitSource


_DISCOVERY_STATES = ("created", "init", "inprogress", "completed", "failed")


def print_matrix_mapping(mapping: MatrixMapping) -> None:
    """Print one row per matrix directory with one column per parameter."""
    headers = ("index", "dir_name", *mapping.parameters)
    rows = tuple(
        (
            f"{row.index:04d}",
            row.dir_name,
            *(matrix_value_text(row.parameters.get(name)) for name in mapping.parameters),
        )
        for row in mapping.rows
    )
    print("Matrix configuration mapping:")
    if not rows:
        print("  no matrix rows recorded")
        return
    # max() over a single list keeps an empty column set from degenerating.
    widths = [max([len(header), *(len(row[column]) for row in rows)]) for column, header in enumerate(headers)]
    print(" | ".join(header.ljust(widths[column]) for column, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(widths[column]) for column, value in enumerate(row)))


def print_discovery(result: DiscoveryResult, *, stale_cutoff: datetime | None = None) -> None:
    """Print discovered experiments, diagnostics, and lifecycle totals.

    ``stale_cutoff`` is opt-in: when ``None`` (the default), output is
    byte-for-byte identical to a plain scan -- no claim file is even read.
    When given, an ``inprogress`` experiment whose ``started_at`` predates it
    gets one additional indented hint line, and the summary gains a trailing
    stale count.
    """
    print(f"Experiments discovered under: {result.root}")
    stale_count = 0
    if result.experiments:
        for experiment in result.experiments:
            print(
                f"{experiment.status.state} | attempt={experiment.status.attempt} "
                f"| name={experiment.configuration.name} | source={_source_identity_text(experiment.configuration)} "
                f"| path={experiment.path}"
            )
            if stale_cutoff is None:
                continue
            hint = describe_staleness(experiment, stale_cutoff)
            if hint is not None:
                stale_count += 1
                print(
                    f"  stale: inprogress for {format_duration(hint.elapsed_seconds)}, "
                    f"started {hint.started_at} — {hint.verdict}"
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
    if stale_cutoff is not None:
        print(f"  stale: {stale_count}")


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


def print_matrix_show_arguments(arguments: argparse.Namespace) -> None:
    """Print the effective artifact path for read-only mapping inspection."""
    _print_effective_arguments("matrix-show", [("artifact", path_text(arguments.artifact))])


def print_matrix_mapping_file(path: Path) -> None:
    """Load and render one persisted matrix mapping artifact."""
    mapping = load_matrix_mapping(path)
    print(f"Matrix identity: {mapping.matrix_id}")
    print_matrix_mapping(mapping)


def print_worker_inspect_arguments(arguments: argparse.Namespace) -> None:
    """Print the effective workspace path for read-only manifest inspection."""
    _print_effective_arguments("worker-inspect", [("workspace", path_text(arguments.workspace))])


def print_worker_manifest_file(workspace: Path) -> None:
    """Load and render the manifest inside one worker workspace directory."""
    manifest = load_worker_manifest(workspace)
    print(f"Worker workspace manifest ({workspace.name}):")
    print(f"  source kind: {manifest.source_kind}")
    print(f"  source identity: {manifest.source_identity}")
    print(f"  source reference: {manifest.source_reference}")
    print(f"  host: {manifest.host}")
    print(f"  pid: {manifest.pid}")
    print(f"  owner: {manifest.owner}")
    print(f"  worker id: {manifest.worker_id}")
    print(f"  started at: {manifest.started_at}")


def print_worker_cleanup_arguments(arguments: argparse.Namespace, root: Path) -> None:
    """Print the effective scan root and confirmation targets for workspace cleanup."""
    confirm = "not set (list only)" if arguments.confirm is None else ", ".join(path_text(p) for p in arguments.confirm)
    _print_effective_arguments("worker-cleanup", [("root", path_text(root)), ("confirm", confirm)])


def print_discovered_workspaces(discovered: Sequence[DiscoveredWorkspace]) -> None:
    """List every discovered worker workspace and, when readable, its manifest summary."""
    print(f"Worker workspaces found ({len(discovered)}):")
    if not discovered:
        print("  none")
        return
    for workspace in discovered:
        if workspace.manifest is not None:
            manifest = workspace.manifest
            print(
                f"  {workspace.path.name}\n"
                f"    source: {manifest.source_kind} {manifest.source_identity} @ {manifest.source_reference}\n"
                f"    owner: {manifest.owner}, started: {manifest.started_at}"
            )
        else:
            print(f"  {workspace.path.name}\n    manifest unavailable: {workspace.manifest_error}")


def print_removed_workspace(path: Path) -> None:
    """Report one worker workspace removed after explicit confirmation."""
    print(f"Removed: {path}", flush=True)


def print_discover_arguments(arguments: argparse.Namespace, *, stale_cutoff: datetime | None = None) -> None:
    """Print the effective root and staleness cutoff for read-only discovery."""
    cutoff_text = "not set" if stale_cutoff is None else stale_cutoff.isoformat().replace("+00:00", "Z")
    _print_effective_arguments(
        "discover",
        [("root", path_text(arguments.root)), ("stale cutoff", cutoff_text)],
    )


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
        timestamp=True,
    )


def print_worker_summary(result: WorkerResult) -> None:
    """Print the counts from one finite shared-worker invocation."""
    _worker_print("Worker summary:")
    _worker_print(f"  candidates: {result.candidates}")
    _worker_print(f"  selected: {result.selected}")
    _worker_print(f"  completed: {result.completed}")
    _worker_print(f"  failed: {result.failed}")
    _worker_print(f"  skipped: {result.skipped}")
    _worker_print(f"    non-runnable: {result.not_runnable}")
    _worker_print(f"    claim contention: {result.claim_contended}")
    _worker_print(f"    stale after claim: {result.stale_skipped}")
    _worker_print(f"  deferred: {result.deferred}")
    _worker_print(f"  invalid: {result.invalid}")


def print_worker_progress(event: WorkerProgressEvent) -> None:
    """Print one worker lifecycle event without changing worker behavior."""
    task = f" [{event.task_index}/{event.task_total}]" if event.task_index is not None else ""
    if event.phase == "preparing":
        _worker_print(f"Preparing experiment{task}: {event.experiment}")
        return
    if event.phase == "executing":
        command = command_text(event.command) if event.command is not None else "not available"
        _worker_print(f"Executing command{task}: {command}")
        if event.stream_output:
            _worker_print("  output mode: streaming and logging")
        else:
            _worker_print(f"  stdout log: {event.stdout_log}")
            _worker_print(f"  stderr log: {event.stderr_log}")
        return
    if event.phase == "completed":
        _worker_print(f"Experiment completed with exit code {event.exit_code}{task}: {event.experiment}")
        return
    if event.phase == "warning":
        _worker_print(f"warning: {event.error}: {event.experiment}", file=sys.stderr)
        return
    if event.exit_code is not None:
        message = f"Experiment failed with exit code {event.exit_code}{task}: {event.experiment}"
    else:
        message = f"Experiment failed{task}: {event.experiment}: {event.error}"
    _worker_print(message, file=sys.stderr)


def matrix_value_text(value: object) -> str:
    """Render one matrix value so a string stays distinguishable from a number."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


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


def _print_effective_arguments(
    subcommand: str,
    rows: Sequence[tuple[str, str]],
    *,
    timestamp: bool = False,
) -> None:
    printer = _worker_print if timestamp else print
    printer(f"RunForge {subcommand} effective arguments:")
    for label, value in rows:
        printer(f"  {label}: {value}")


def _worker_print(message: str, *, file: object | None = None) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"[{timestamp}] {message}", file=sys.stdout if file is None else file, flush=True)


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
