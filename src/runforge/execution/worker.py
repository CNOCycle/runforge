"""Claim-protected experiment execution and finite lazy queue processing."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from runforge.execution.discovery import DiscoveredExperiment, DiscoveryError, DiscoveryResult, discover_paths
from runforge.execution.errors import ExperimentHeldError, ExperimentNotRunnableError, WorkerError
from runforge.execution.process import CommandExecutionError, run_command
from runforge.execution.recovery import recovery_messages
from runforge.execution.retry import RetryError, acquire_retry_claim, is_retryable_state, prepare_retry
from runforge.execution.scheduling import SchedulingError, is_held
from runforge.execution.sources import preparation_for
from runforge.execution.verification import verify_planned_inputs
from runforge.infrastructure.claims import (
    ClaimError,
    ExperimentClaim,
    describe_claim_holder,
    release_claim,
    try_acquire_claim,
    verify_claim_owner,
)
from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus


@dataclass(frozen=True)
class WorkerResult:
    """Summary of one finite shared-worker invocation."""

    root: Path
    candidates: int
    selected: int
    completed: int
    failed: int
    not_runnable: int
    claim_contended: int
    stale_skipped: int
    deferred: int
    invalid: int
    held: int = 0

    @property
    def skipped(self) -> int:
        """Total work skipped, derived so it cannot drift from its reasons."""
        return self.not_runnable + self.claim_contended + self.stale_skipped + self.held

    @property
    def exit_code(self) -> int:
        """Aggregate outcome; successful skips do not assert target completion."""
        if self.invalid:
            return 2
        return 1 if self.failed else 0


@dataclass(frozen=True)
class BatchRequest:
    """Selection policy for one finite, lazily refilled execution invocation."""

    paths: tuple[Path, ...]
    max_tasks: int | None = None
    stream_output: bool = False
    retry: bool = False
    force: bool = False
    require_each: bool = True
    reuse_artifacts: bool = False


@dataclass(frozen=True)
class _ExecutionOptions:
    """Bundled streaming and progress-callback options for one execution."""

    stream_output: bool
    progress: Callable[[WorkerProgressEvent], None] | None
    claim: ExperimentClaim


@dataclass(frozen=True)
class WorkerProgressEvent:
    """One observable worker lifecycle transition."""

    phase: Literal["preparing", "executing", "completed", "failed", "warning", "recovery"]
    experiment: Path
    stdout_log: Path
    stderr_log: Path
    stream_output: bool
    command: ExperimentCommand | None = None
    exit_code: int | None = None
    error: str | None = None
    task_index: int | None = None
    task_total: int | None = None
    environment_keys: tuple[str, ...] | None = None
    artifact_directory: Path | None = None
    attempt: int | None = None


@dataclass
class _ExecutionProgress:
    progress: Callable[[WorkerProgressEvent], None] | None
    failed: bool = False

    def __call__(self, event: WorkerProgressEvent) -> None:
        self.failed = self.failed or event.phase == "failed"
        _notify_progress(self.progress, event)


def run_experiment(
    experiment_path: Path,
    *,
    stream_output: bool = False,
    progress: Callable[[WorkerProgressEvent], None] | None = None,
    claim: ExperimentClaim | None = None,
) -> int:
    """Run one explicit experiment through the shared ownership boundary.

    A caller-provided claim is transferred to this function and released before
    it returns or raises; callers must not reuse it afterwards.
    """
    experiment = ExperimentDirectory.resolve(experiment_path)
    if not experiment.root.is_dir():
        _notify_progress(progress, _progress_event("preparing", experiment, stream_output))
        error = WorkerError(f"Experiment directory does not exist: {experiment.root}")
        _notify_progress(
            progress,
            replace(_progress_event("failed", experiment, stream_output), error=str(error)),
        )
        raise error
    owned_claim = claim
    try:
        if owned_claim is None:
            owned_claim = try_acquire_claim(experiment)
            if owned_claim is None:
                raise WorkerError(
                    f"Experiment is already claimed: {experiment.root} ({describe_claim_holder(experiment)})"
                )
        verify_claim_owner(experiment, owned_claim)
        return _run_claimed_experiment(
            experiment,
            owned_claim,
            stream_output=stream_output,
            progress=progress,
        )
    except ClaimError as error:
        raise WorkerError(str(error)) from error
    finally:
        if owned_claim is not None:
            _release_execution_claim(experiment, owned_claim, stream_output=stream_output, progress=progress)


def _release_execution_claim(experiment, claim, *, stream_output, progress) -> None:
    """Report a leaked claim without masking its outcome or stopping the queue."""
    try:
        release_claim(experiment, claim)
    except ClaimError as error:
        _notify_progress(
            progress,
            replace(
                _progress_event("warning", experiment, stream_output),
                error=f"Could not release claim: {error}",
            ),
        )


def run_worker(
    root: Path,
    *,
    max_tasks: int | None = None,
    stream_output: bool = False,
    progress: Callable[[WorkerProgressEvent], None] | None = None,
) -> WorkerResult:
    """Consume path-ordered queues, discovering again only when each is exhausted."""
    return execute_paths(BatchRequest((root,), max_tasks, stream_output, require_each=False), progress=progress)


def execute_paths(
    request: BatchRequest,
    *,
    progress: Callable[[WorkerProgressEvent], None] | None = None,
) -> WorkerResult:
    """Run a finite union of experiment leaves with a single completion budget."""
    limit = request.max_tasks
    if isinstance(limit, bool) or (limit is not None and (not isinstance(limit, int) or limit <= 0)):
        raise WorkerError("max_tasks must be a positive integer or None")
    if request.reuse_artifacts and not request.retry:
        raise WorkerError("Artifact reuse requires retry")
    initial = discover_paths(request.paths, require_each=request.require_each)
    if request.force:
        experiments = {item.path for item in initial.experiments}
        if not request.retry or any(Path(path).expanduser().resolve() not in experiments for path in request.paths):
            raise WorkerError("Forced retry requires explicit experiment-directory paths, not report roots")
    if progress is not None:
        for path, message in recovery_messages(initial.experiments):
            _notify_progress(
                progress,
                replace(_progress_event("recovery", ExperimentDirectory(path), request.stream_output), error=message),
            )
    return _BatchExecution(request, initial, progress).run()


class _BatchExecution:
    """Keep invocation-wide counts while allowing new work on queue refill."""

    def __init__(self, request: BatchRequest, initial: DiscoveryResult, progress):
        self.request = request
        self.initial = initial
        self.progress = progress
        leaves = {item.path for item in initial.experiments}
        paths = (Path(path).expanduser().resolve() for path in request.paths or (Path("."),))
        self.refill_paths = tuple(path for path in paths if path not in leaves)
        self.observed: dict[Path, DiscoveredExperiment] = {}
        self.visited: set[Path] = set()
        self.candidates: set[Path] = set()
        self.non_runnable: set[Path] = set()
        self.held: set[Path] = set()
        self.invalid: set[tuple[Path, str]] = set()
        self.invalid_paths: set[Path] = set()
        self.counts: Counter[str] = Counter()

    def _eligible(self, item: DiscoveredExperiment) -> bool:
        if self.request.retry:
            return is_retryable_state(item.status.state, force=self.request.force)
        return item.status.state in {"created", "init"}

    def _queue(self, discovery: DiscoveryResult) -> tuple[DiscoveredExperiment, ...]:
        for diagnostic in discovery.diagnostics:
            key = (diagnostic.path, diagnostic.message)
            if key not in self.invalid:
                layout = ExperimentDirectory.resolve(diagnostic.path)
                _notify_progress(
                    self.progress,
                    replace(
                        _progress_event("warning", layout, self.request.stream_output),
                        error=diagnostic.message,
                    ),
                )
                self.invalid.add(key)
            self.invalid_paths.add(diagnostic.path)
        queue = []
        for item in discovery.experiments:
            if item.path not in self.observed and not self._eligible(item):
                self.non_runnable.add(item.path)
            self.observed[item.path] = item
            if not self._eligible(item):
                continue
            self.candidates.add(item.path)
            if item.held:
                self.held.add(item.path)
            else:
                self.held.discard(item.path)
                if item.path not in self.visited:
                    queue.append(item)
        return tuple(queue)

    def _quota_reached(self) -> bool:
        limit = self.request.max_tasks
        return limit is not None and self.counts["completed"] >= limit

    def run(self) -> WorkerResult:
        queue = self._queue(self.initial)
        while queue and not self._quota_reached():
            for item in queue:
                if self._quota_reached():
                    break
                self._process(item)
            if self._quota_reached() or not self.refill_paths:
                break
            try:
                queue = self._queue(discover_paths(self.refill_paths))
            except DiscoveryError as error:
                self.counts["discovery_errors"] += 1
                _notify_progress(
                    self.progress,
                    replace(
                        _progress_event("warning", ExperimentDirectory(self.initial.root), self.request.stream_output),
                        error=f"Could not refill queue: {error}",
                    ),
                )
                break
        deferred = sum(
            self._eligible(item) and not item.held and item.path not in self.visited for item in self.observed.values()
        )
        return WorkerResult(
            root=self.initial.root,
            candidates=len(self.candidates),
            selected=self.counts["completed"] + self.counts["failed"],
            completed=self.counts["completed"],
            failed=self.counts["failed"],
            not_runnable=len(self.non_runnable),
            claim_contended=self.counts["claim_contended"],
            stale_skipped=self.counts["stale"],
            deferred=deferred,
            invalid=len(self.invalid_paths) + self.counts["discovery_errors"],
            held=len(self.held),
        )

    def _process(self, item: DiscoveredExperiment) -> None:
        self.visited.add(item.path)

        def task_progress(event: WorkerProgressEvent) -> None:
            _notify_progress(
                self.progress, replace(event, task_index=len(self.visited), task_total=len(self.candidates))
            )

        outcome = _process_worker_candidate(
            ExperimentDirectory.resolve(item.path),
            self.request,
            progress=task_progress,
        )
        if outcome == "held":
            self.visited.discard(item.path)
            self.held.add(item.path)
        elif outcome == "invalid":
            self.invalid_paths.add(item.path)
        else:
            self.counts[outcome] += 1


def _process_worker_candidate(
    experiment: ExperimentDirectory,
    request: BatchRequest,
    *,
    progress: Callable[[WorkerProgressEvent], None] | None,
) -> Literal["completed", "failed", "claim_contended", "stale", "held", "invalid"]:
    """Acquire ownership before retry preparation and execution-boundary rereads."""
    try:
        if request.retry:
            claim, overridden = acquire_retry_claim(experiment, force=request.force)
        else:
            claim, overridden = try_acquire_claim(experiment), False
    except (ClaimError, RetryError, ValueError) as error:
        return _claim_failure_outcome(experiment, request, progress, error)
    if claim is None:
        _report_retry_contention(experiment, request, progress)
        return "claim_contended"
    execution_progress = _ExecutionProgress(progress)
    try:
        if request.retry:
            if is_held(experiment):
                raise ExperimentHeldError(f"Experiment is held: {experiment.root}")
            _prepare_candidate_retry(experiment, claim, request, progress, overridden)
        transferred_claim, claim = claim, None
        exit_code = run_experiment(
            experiment.root,
            stream_output=request.stream_output,
            progress=execution_progress,
            claim=transferred_claim,
        )
    except ExperimentHeldError:
        return "held"
    except ExperimentNotRunnableError:
        return "stale"
    except (WorkerError, RetryError, ValueError, OSError) as error:
        return _execution_error_outcome(experiment, request, progress, error, execution_progress.failed)
    finally:
        if claim is not None:
            _release_execution_claim(experiment, claim, stream_output=request.stream_output, progress=progress)
    return "completed" if exit_code == 0 else "failed"


def _execution_error_outcome(experiment, request, progress, error, reported_failure):
    if not reported_failure:
        _notify_progress(
            progress,
            replace(
                _progress_event("warning", experiment, request.stream_output),
                error=str(error),
            ),
        )
    # Retry archival/preparation failures never started an execution attempt.
    return "invalid" if isinstance(error, (SchedulingError, RetryError)) else "failed"


def _report_retry_contention(experiment, request, progress) -> None:
    """Guide an explicitly named retry without changing batch skip semantics."""
    if request.retry and experiment.root in {Path(path).expanduser().resolve() for path in request.paths}:
        _notify_progress(
            progress,
            replace(
                _progress_event("warning", experiment, request.stream_output),
                error=(
                    f"Experiment is already claimed ({describe_claim_holder(experiment)}); "
                    "use retry --force only after independently confirming its worker and child processes stopped"
                ),
            ),
        )


def _claim_failure_outcome(experiment, request, progress, error):
    if isinstance(error, ExperimentNotRunnableError):
        return "stale"
    _notify_progress(
        progress,
        replace(
            _progress_event("warning", experiment, request.stream_output),
            error=f"Could not acquire claim: {error}",
        ),
    )
    # Ownership was never acquired, so no execution attempt failed.
    return "invalid"


def _prepare_candidate_retry(experiment, claim, request, progress, overridden) -> None:
    preparation = prepare_retry(
        experiment.root, force=request.force, claim=claim, reuse_artifacts=request.reuse_artifacts
    )
    if preparation.forced or overridden:
        _notify_progress(
            progress,
            replace(
                _progress_event("warning", experiment, request.stream_output),
                error="Forced retry cannot prove that the previous worker has stopped",
            ),
        )


def _run_claimed_experiment(
    experiment: ExperimentDirectory,
    claim: ExperimentClaim,
    *,
    stream_output: bool,
    progress: Callable[[WorkerProgressEvent], None] | None,
) -> int:
    _notify_progress(progress, _progress_event("preparing", experiment, stream_output))
    try:
        configuration, status = experiment.load_metadata()
    except ValueError as error:
        failure = WorkerError(str(error))
        _notify_progress(
            progress,
            replace(_progress_event("failed", experiment, stream_output), error=str(failure)),
        )
        raise failure from error
    verify_claim_owner(experiment, claim)
    if is_held(experiment):
        raise ExperimentHeldError(f"Experiment is held; release its scheduling hold first: {experiment.root}")
    if status.state not in {"created", "init"}:
        error = ExperimentNotRunnableError(f"Experiment is not runnable from state {status.state!r}")
        _notify_progress(
            progress,
            replace(
                _progress_event("failed", experiment, stream_output, command=configuration.command),
                error=str(error),
            ),
        )
        raise error

    try:
        verify_planned_inputs(experiment)
        status = _save_claimed_status(
            experiment,
            claim,
            replace(status, state="init", updated_at=utc_now(), error=None),
        )
        exit_code = _execute(
            experiment,
            configuration,
            status,
            _ExecutionOptions(stream_output, progress, claim),
        )
    except WorkerError as error:
        _save_claimed_status(experiment, claim, _failed_status(experiment, status, str(error)))
        _notify_progress(
            progress,
            replace(
                _progress_event("failed", experiment, stream_output, command=configuration.command),
                error=str(error),
            ),
        )
        raise
    except OSError as error:
        failure = WorkerError(str(error))
        _save_claimed_status(experiment, claim, _failed_status(experiment, status, str(failure)))
        _notify_progress(
            progress,
            replace(
                _progress_event("failed", experiment, stream_output, command=configuration.command),
                error=str(failure),
            ),
        )
        raise failure from error
    phase = "completed" if exit_code == 0 else "failed"
    event_error = None if exit_code == 0 else f"Command exited with status {exit_code}"
    _notify_progress(
        progress,
        replace(
            _progress_event(phase, experiment, stream_output, command=configuration.command),
            exit_code=exit_code,
            error=event_error,
        ),
    )
    return exit_code


def _save_claimed_status(
    experiment: ExperimentDirectory,
    claim: ExperimentClaim,
    status: ExperimentStatus,
) -> ExperimentStatus:
    """Fence every lifecycle status write with the current claim token."""
    verify_claim_owner(experiment, claim)
    return experiment.save_status(status)


def _failed_status(experiment: ExperimentDirectory, fallback: ExperimentStatus, error: str) -> ExperimentStatus:
    """Preserve an already-persisted attempt when preparation or startup fails."""
    try:
        current = experiment.load_status()
    except ValueError:
        current = fallback
    now = utc_now()
    return replace(current, state="failed", updated_at=now, finished_at=now, error=error)


def _execute(
    experiment: ExperimentDirectory,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    options: _ExecutionOptions,
) -> int:
    """Prepare the recorded source, then run the command in the prepared directory."""
    preparation = preparation_for(configuration.source)
    with preparation.working_directory(experiment, configuration.source, options.claim) as working_directory:
        return _run_in_working_directory(experiment, configuration, status, working_directory, options)


def _run_in_working_directory(
    experiment: ExperimentDirectory,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    working_directory: Path,
    options: _ExecutionOptions,
) -> int:
    """Transition to inprogress, run the recorded command, and record the final status."""
    active_status = _save_claimed_status(
        experiment,
        options.claim,
        replace(status, state="inprogress", attempt=status.attempt + 1, updated_at=utc_now(), started_at=utc_now()),
    )
    _notify_progress(
        options.progress,
        replace(
            _progress_event("executing", experiment, options.stream_output, command=configuration.command),
            environment_keys=tuple(sorted(configuration.environment)),
            artifact_directory=experiment.artifacts,
            attempt=active_status.attempt,
        ),
    )
    try:
        exit_code = run_command(experiment, working_directory, configuration, stream_output=options.stream_output)
    except CommandExecutionError as error:
        # process.py reports its own failures; the worker owns the lifecycle error type.
        raise WorkerError(str(error)) from error
    final_state = "completed" if exit_code == 0 else "failed"
    error = None if exit_code == 0 else f"Command exited with status {exit_code}"
    _save_claimed_status(
        experiment,
        options.claim,
        replace(
            active_status,
            state=final_state,
            updated_at=utc_now(),
            finished_at=utc_now(),
            exit_code=exit_code,
            error=error,
        ),
    )
    return exit_code


def _progress_event(
    phase: Literal["preparing", "executing", "completed", "failed", "warning", "recovery"],
    experiment: ExperimentDirectory,
    stream_output: bool,
    *,
    command: ExperimentCommand | None = None,
) -> WorkerProgressEvent:
    return WorkerProgressEvent(
        phase=phase,
        experiment=experiment.root,
        stdout_log=experiment.stdout_log,
        stderr_log=experiment.stderr_log,
        stream_output=stream_output,
        command=command,
    )


def _notify_progress(
    progress: Callable[[WorkerProgressEvent], None] | None,
    event: WorkerProgressEvent,
) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:
        # Progress reporting is observational and must not change experiment outcomes.
        return
