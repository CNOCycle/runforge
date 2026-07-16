"""One-shot execution of one explicit planned experiment directory."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Literal, TextIO

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.git_ops import GitOperationError, GitRepository
from runforge.json_store import load_json_object, save_json_object
from runforge.time_utils import utc_now


class WorkerError(RuntimeError):
    """Raised when one planned experiment cannot be prepared or executed."""


@dataclass(frozen=True)
class WorkerProgressEvent:
    """One observable worker lifecycle transition."""

    phase: Literal["preparing", "executing", "completed", "failed"]
    experiment: Path
    stdout_log: Path
    stderr_log: Path
    stream_output: bool
    command: ExperimentCommand | None = None
    exit_code: int | None = None
    error: str | None = None


def run_experiment(
    experiment_path: Path,
    *,
    stream_output: bool = False,
    progress: Callable[[WorkerProgressEvent], None] | None = None,
) -> int:
    """Run one explicit experiment directory and return its command exit code."""
    experiment = Path(experiment_path).expanduser().resolve()
    _notify_progress(progress, _progress_event("preparing", experiment, stream_output))
    if not experiment.is_dir():
        error = WorkerError(f"Experiment directory does not exist: {experiment}")
        _notify_progress(
            progress,
            replace(_progress_event("failed", experiment, stream_output), error=str(error)),
        )
        raise error
    try:
        configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))
        status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    except ValueError as error:
        failure = WorkerError(str(error))
        _notify_progress(
            progress,
            replace(_progress_event("failed", experiment, stream_output), error=str(failure)),
        )
        raise failure from error
    if status.state not in {"created", "init"}:
        error = WorkerError(f"Experiment is not runnable from state {status.state!r}")
        _notify_progress(
            progress,
            replace(
                _progress_event("failed", experiment, stream_output, command=configuration.command),
                error=str(error),
            ),
        )
        raise error

    try:
        status = _save_status(experiment, replace(status, state="init", updated_at=utc_now(), error=None))
        exit_code = _execute(
            experiment,
            configuration,
            status,
            stream_output=stream_output,
            progress=progress,
        )
    except WorkerError as error:
        _save_status(
            experiment,
            replace(status, state="failed", updated_at=utc_now(), finished_at=utc_now(), error=str(error)),
        )
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
        _save_status(
            experiment,
            replace(status, state="failed", updated_at=utc_now(), finished_at=utc_now(), error=str(failure)),
        )
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


def _execute(
    experiment: Path,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    *,
    stream_output: bool,
    progress: Callable[[WorkerProgressEvent], None] | None,
) -> int:
    try:
        repository = GitRepository.locate(configuration.source.repository)
    except GitOperationError as error:
        raise WorkerError(str(error)) from error

    with tempfile.TemporaryDirectory(prefix="runforge-worker-", dir=repository.root.parent) as temporary_root:
        worktree = Path(temporary_root) / "worktree"
        try:
            repository.create_detached_worktree(worktree, configuration.source.commit)
            _apply_recorded_patch(
                repository, worktree, experiment, configuration.source.patch_file, configuration.source.patch_sha256
            )
            active_status = _save_status(
                experiment,
                replace(
                    status,
                    state="inprogress",
                    attempt=status.attempt + 1,
                    updated_at=utc_now(),
                    started_at=utc_now(),
                ),
            )
            _notify_progress(
                progress,
                _progress_event(
                    "executing",
                    experiment,
                    stream_output,
                    command=configuration.command,
                ),
            )
            exit_code = _run_command(experiment, worktree, configuration, stream_output=stream_output)
            final_state = "completed" if exit_code == 0 else "failed"
            error = None if exit_code == 0 else f"Command exited with status {exit_code}"
            _save_status(
                experiment,
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
        except GitOperationError as error:
            raise WorkerError(str(error)) from error
        finally:
            if worktree.exists():
                try:
                    repository.remove_worktree(worktree)
                except GitOperationError as error:
                    raise WorkerError(f"Could not clean up worktree: {error}") from error


def _apply_recorded_patch(
    repository: GitRepository,
    worktree: Path,
    experiment: Path,
    patch_file: str | None,
    patch_sha256: str | None,
) -> None:
    if patch_file is None:
        return
    patch_path = experiment / patch_file
    if not patch_path.is_file():
        raise WorkerError(f"Recorded Git patch is missing: {patch_path}")
    actual_sha256 = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    if actual_sha256 != patch_sha256:
        raise WorkerError("Recorded Git patch checksum does not match configuration")
    try:
        repository.check_patch(worktree, patch_path)
        repository.apply_patch(worktree, patch_path)
    except GitOperationError as error:
        raise WorkerError(str(error)) from error


def _run_command(
    experiment: Path,
    worktree: Path,
    configuration: ExperimentConfiguration,
    *,
    stream_output: bool,
) -> int:
    environment = os.environ.copy()
    environment.update(configuration.environment)
    environment["RUNFORGE_ARTIFACT_DIR"] = str(experiment / "artifacts")
    paths = [str(worktree / "src"), str(worktree)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    command = configuration.command.script if configuration.command.mode == "shell" else configuration.command.arguments
    with (experiment / "stdout.log").open("wb") as stdout, (experiment / "stderr.log").open("wb") as stderr:
        try:
            if stream_output:
                process = subprocess.Popen(
                    command,
                    shell=configuration.command.mode == "shell",
                    cwd=worktree,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                exit_code = _stream_process(process, stdout, stderr)
            else:
                exit_code = subprocess.run(
                    command,
                    shell=configuration.command.mode == "shell",
                    cwd=worktree,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                ).returncode
        except OSError as error:
            raise WorkerError(f"Could not start command: {error}") from error
        stdout.flush()
        stderr.flush()
        os.fsync(stdout.fileno())
        os.fsync(stderr.fileno())
    return exit_code


def _stream_process(
    process: subprocess.Popen[bytes],
    stdout_log: BinaryIO,
    stderr_log: BinaryIO,
) -> int:
    assert process.stdout is not None
    assert process.stderr is not None
    errors: list[OSError | ValueError] = []
    # Drain both pipes concurrently so neither child stream can block the other.
    threads = [
        threading.Thread(target=_pump_output, args=(process.stdout, stdout_log, sys.stdout, errors)),
        threading.Thread(target=_pump_output, args=(process.stderr, stderr_log, sys.stderr, errors)),
    ]
    for thread in threads:
        thread.start()
    exit_code = process.wait()
    for thread in threads:
        thread.join()
    if errors:
        raise WorkerError(f"Could not write command output: {errors[0]}")
    return exit_code


def _pump_output(
    source: BinaryIO,
    log: BinaryIO,
    console: TextIO,
    errors: list[OSError | ValueError],
) -> None:
    log_available = True
    console_available = True
    try:
        while chunk := source.read1(8192):
            if log_available:
                try:
                    log.write(chunk)
                    log.flush()
                except (OSError, ValueError) as error:
                    errors.append(error)
                    log_available = False
            if console_available:
                try:
                    _write_console(console, chunk)
                except (OSError, ValueError):
                    console_available = False
    except (OSError, ValueError) as error:
        errors.append(error)
    finally:
        source.close()


def _write_console(console: TextIO, chunk: bytes) -> None:
    buffer = getattr(console, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    console.write(chunk.decode("utf-8", errors="replace"))
    console.flush()


def _save_status(experiment: Path, status: ExperimentStatus) -> ExperimentStatus:
    save_json_object(experiment / "status.json", status.to_dict())
    return status


def _progress_event(
    phase: Literal["preparing", "executing", "completed", "failed"],
    experiment: Path,
    stream_output: bool,
    *,
    command: ExperimentCommand | None = None,
) -> WorkerProgressEvent:
    return WorkerProgressEvent(
        phase=phase,
        experiment=experiment,
        stdout_log=experiment / "stdout.log",
        stderr_log=experiment / "stderr.log",
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
