"""One-shot execution of one explicit planned experiment directory."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from runforge.execution.discovery import discover_experiments
from runforge.execution.errors import ExperimentNotRunnableError, WorkerError
from runforge.execution.process import CommandExecutionError, run_command
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

    @property
    def skipped(self) -> int:
        """Total work skipped, derived so it cannot drift from its reasons."""
        return self.not_runnable + self.claim_contended + self.stale_skipped


@dataclass(frozen=True)
class _ExecutionOptions:
    """Bundled streaming and progress-callback options for one execution."""

    stream_output: bool
    progress: Callable[[WorkerProgressEvent], None] | None
    claim: ExperimentClaim


@dataclass(frozen=True)
class WorkerProgressEvent:
    """One observable worker lifecycle transition."""

    phase: Literal["preparing", "executing", "completed", "failed", "warning"]
    experiment: Path
    stdout_log: Path
    stderr_log: Path
    stream_output: bool
    command: ExperimentCommand | None = None
    exit_code: int | None = None
    error: str | None = None
    task_index: int | None = None
    task_total: int | None = None


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
            try:
                release_claim(experiment, owned_claim)
            except ClaimError as error:
                # A leaked claim needs an operator, but it must not rewrite the
                # recorded result or mask the failure that is already in flight.
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
    """Execute one finite discovery snapshot through the existing executor."""
    if isinstance(max_tasks, bool) or (max_tasks is not None and (not isinstance(max_tasks, int) or max_tasks <= 0)):
        raise WorkerError("max_tasks must be a positive integer or None")
    discovery = discover_experiments(root)
    candidates = tuple(
        experiment for experiment in discovery.experiments if experiment.status.state in {"created", "init"}
    )
    selected = candidates if max_tasks is None else candidates[:max_tasks]
    completed = 0
    failed = 0
    not_runnable = len(discovery.experiments) - len(candidates)
    claim_contended = 0
    stale_skipped = 0
    task_total = len(candidates)
    for task_index, discovered in enumerate(selected, start=1):
        experiment = ExperimentDirectory.resolve(discovered.path)

        def task_progress(event: WorkerProgressEvent) -> None:
            _notify_progress(
                progress,
                replace(event, task_index=task_index, task_total=task_total),
            )

        try:
            claim = try_acquire_claim(experiment)
        except ClaimError as error:
            _notify_progress(
                progress,
                replace(
                    _progress_event("warning", experiment, stream_output),
                    error=f"Could not acquire claim: {error}",
                ),
            )
            failed += 1
            continue
        if claim is None:
            claim_contended += 1
            continue
        try:
            exit_code = run_experiment(
                experiment.root,
                stream_output=stream_output,
                progress=task_progress,
                claim=claim,
            )
        except ExperimentNotRunnableError:
            stale_skipped += 1
        except WorkerError:
            failed += 1
        else:
            if exit_code == 0:
                completed += 1
            else:
                failed += 1
    return WorkerResult(
        root=discovery.root,
        candidates=len(candidates),
        selected=len(selected),
        completed=completed,
        failed=failed,
        not_runnable=not_runnable,
        claim_contended=claim_contended,
        stale_skipped=stale_skipped,
        deferred=len(candidates) - len(selected),
        invalid=len(discovery.diagnostics),
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
    with preparation.working_directory(experiment, configuration.source) as working_directory:
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
        _progress_event("executing", experiment, options.stream_output, command=configuration.command),
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
    phase: Literal["preparing", "executing", "completed", "failed", "warning"],
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
