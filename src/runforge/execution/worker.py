"""One-shot execution of one explicit planned experiment directory."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Literal, TextIO

from runforge.infrastructure.claims import (
    ClaimError,
    ExperimentClaim,
    describe_claim_holder,
    release_claim,
    try_acquire_claim,
    verify_claim_owner,
)
from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.directory_scan import DirectoryScanError, ScannedFile, scan_directory
from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.directory_source import (
    DirectorySnapshotSource,
    DirectorySourceEntry,
    DirectorySourceManifest,
    VerifiedDirectorySource,
)
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus


class WorkerError(RuntimeError):
    """Raised when one planned experiment cannot be prepared or executed."""


class ExperimentNotRunnableError(WorkerError):
    """Raised when a claimed experiment was completed or changed by another worker."""


@dataclass(frozen=True)
class _ExecutionOptions:
    """Bundled streaming and progress-callback options for one execution."""

    stream_output: bool
    progress: Callable[[WorkerProgressEvent], None] | None
    claim: ExperimentClaim


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
                print(
                    f"warning: Could not release claim for {experiment.root}: {error}",
                    file=sys.stderr,
                    flush=True,
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
        _verify_planned_inputs(experiment)
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
    """Dispatch execution by the recorded source kind; no Git fallback for non-Git sources."""
    if isinstance(configuration.source, VerifiedDirectorySource):
        return _execute_verified_directory(experiment, configuration, status, options)
    if isinstance(configuration.source, DirectorySnapshotSource):
        return _execute_directory_snapshot(experiment, configuration, status, options)
    return _execute_git(experiment, configuration, status, options)


def _execute_git(
    experiment: ExperimentDirectory,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    options: _ExecutionOptions,
) -> int:
    try:
        repository = GitRepository.locate(configuration.source.repository)
    except GitOperationError as error:
        raise WorkerError(str(error)) from error

    with tempfile.TemporaryDirectory(prefix="runforge-worker-", dir=repository.root.parent) as temporary_root:
        worktree = Path(temporary_root)
        try:
            repository.create_detached_worktree(worktree, configuration.source.commit)
            _apply_recorded_patch(
                repository,
                worktree,
                experiment,
                configuration.source.patch_file,
                configuration.source.patch_sha256,
            )
            return _run_in_working_directory(experiment, configuration, status, worktree, options)
        except GitOperationError as error:
            raise WorkerError(str(error)) from error
        finally:
            if worktree.exists():
                try:
                    repository.remove_worktree(worktree)
                except GitOperationError as error:
                    raise WorkerError(f"Could not clean up worktree: {error}") from error


def _execute_verified_directory(
    experiment: ExperimentDirectory,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    options: _ExecutionOptions,
) -> int:
    source = configuration.source
    _verify_verified_directory_source(experiment, source)
    return _run_in_working_directory(experiment, configuration, status, source.path, options)


def _execute_directory_snapshot(
    experiment: ExperimentDirectory,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    options: _ExecutionOptions,
) -> int:
    source = configuration.source
    _verify_directory_snapshot_source(experiment, source)
    with tempfile.TemporaryDirectory(prefix="runforge-worker-") as temporary_root:
        workspace = Path(temporary_root) / "source"
        try:
            shutil.copytree(experiment.snapshot_source_directory, workspace)
        except OSError as error:
            raise WorkerError(f"Could not materialize captured directory-snapshot source: {error}") from error
        _verify_directory_matches_manifest(
            experiment,
            workspace,
            source.tree_digest,
            changed_message="Materialized directory-snapshot source does not match its manifest",
            scan_mode="complete",
        )
        return _run_in_working_directory(experiment, configuration, status, workspace, options)


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
    exit_code = _run_command(experiment, working_directory, configuration, stream_output=options.stream_output)
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


def _verify_verified_directory_source(experiment: ExperimentDirectory, source: VerifiedDirectorySource) -> None:
    """Reject a missing, moved, or changed verified-directory source before execution."""
    if source.path.is_symlink() or not source.path.is_dir():
        raise WorkerError(f"Verified-directory source is missing or not a directory: {source.path}")
    _verify_directory_matches_manifest(
        experiment,
        source.path,
        source.tree_digest,
        changed_message="Verified-directory source has changed since planning",
        scan_mode="reject-ignored",
    )


def _verify_directory_snapshot_source(experiment: ExperimentDirectory, source: DirectorySnapshotSource) -> None:
    """Reject a missing, expanded, or changed captured directory-snapshot before execution."""
    snapshot_dir = experiment.snapshot_source_directory
    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise WorkerError(f"Captured directory-snapshot source is missing: {snapshot_dir}")
    _verify_directory_matches_manifest(
        experiment,
        snapshot_dir,
        source.tree_digest,
        changed_message="Captured directory-snapshot source has changed since planning",
    )


def _verify_directory_matches_manifest(
    experiment: ExperimentDirectory,
    directory: Path,
    tree_digest: str,
    *,
    changed_message: str,
    scan_mode: Literal["ignored", "complete", "reject-ignored"] = "ignored",
) -> None:
    manifest = _load_directory_source_manifest(experiment, tree_digest)
    try:
        scan = scan_directory(
            directory,
            ignore_file=scan_mode != "complete",
            reject_ignored=scan_mode == "reject-ignored",
        )
    except DirectoryScanError as error:
        raise WorkerError(str(error)) from error
    expected = {entry.path: entry for entry in manifest.entries}
    actual = {entry.path: entry for entry in scan.files}
    _require_matching_source_paths(expected, actual)
    _require_matching_source_entries(expected, actual)
    if scan.tree_digest != tree_digest:
        raise WorkerError(changed_message)


def _load_directory_source_manifest(experiment: ExperimentDirectory, tree_digest: str) -> DirectorySourceManifest:
    try:
        manifest = experiment.load_directory_source_manifest()
    except ValueError as error:
        raise WorkerError(str(error)) from error
    if manifest.tree_digest != tree_digest:
        raise WorkerError("Recorded source manifest digest does not match configuration")
    return manifest


def _require_matching_source_paths(expected: dict[str, object], actual: dict[str, object]) -> None:
    """Require the manifest's exact file set before comparing checksums."""
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    if missing:
        raise WorkerError(f"Source file is missing: {missing[0]}")
    if unexpected:
        raise WorkerError(f"Unexpected source file: {unexpected[0]}")


def _require_matching_source_entries(expected: dict[str, DirectorySourceEntry], actual: dict[str, ScannedFile]) -> None:
    for path, expected_entry in expected.items():
        actual_entry = actual[path]
        if actual_entry.sha256 != expected_entry.sha256:
            raise WorkerError(f"Source file checksum does not match manifest: {path}")
        if actual_entry.executable != expected_entry.executable:
            raise WorkerError(f"Source file executable bit does not match manifest: {path}")


def _apply_recorded_patch(
    repository: GitRepository,
    worktree: Path,
    experiment: ExperimentDirectory,
    patch_file: str | None,
    patch_sha256: str | None,
) -> None:
    if patch_file is None:
        return
    patch_path = experiment.source_file(patch_file)
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


def _verify_planned_inputs(experiment: ExperimentDirectory) -> None:
    """Reject a missing, changed, or expanded immutable planned input tree."""
    manifest = _load_planned_input_manifest(experiment)
    if manifest is None:
        return
    expected = {entry.path: entry for entry in manifest.entries}
    actual = _planned_input_paths(experiment)
    _require_matching_input_paths(expected, actual)
    for path, entry in expected.items():
        actual_sha256 = hashlib.sha256(experiment.input_file(path).read_bytes()).hexdigest()
        if actual_sha256 != entry.sha256:
            raise WorkerError(f"Planned input checksum does not match manifest: {path}")


def _load_planned_input_manifest(experiment: ExperimentDirectory):
    """Load one planned-input manifest or recognize a legacy plan without inputs."""
    manifest_exists = experiment.input_manifest_file.exists()
    inputs_exists = experiment.inputs.exists()
    if not manifest_exists and not inputs_exists:
        return None
    if not manifest_exists:
        raise WorkerError("Planned input manifest is missing")
    if not experiment.input_manifest_file.is_file():
        raise WorkerError("Planned input manifest is not a regular file")
    if not inputs_exists or not experiment.inputs.is_dir():
        raise WorkerError("Planned input directory is missing")
    try:
        return experiment.load_input_manifest()
    except ValueError as error:
        raise WorkerError(str(error)) from error


def _planned_input_paths(experiment: ExperimentDirectory) -> set[str]:
    """Return regular input files while rejecting links and special filesystem entries."""
    actual: set[str] = set()
    for candidate in experiment.inputs.rglob("*"):
        relative = candidate.relative_to(experiment.inputs).as_posix()
        if candidate.is_symlink():
            raise WorkerError(f"Planned input is a symbolic link: {relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise WorkerError(f"Planned input is not a regular file: {relative}")
        actual.add(relative)
    return actual


def _require_matching_input_paths(expected: dict[str, object], actual: set[str]) -> None:
    """Require the manifest's exact file set before checksum validation."""
    missing = sorted(set(expected) - actual)
    unexpected = sorted(actual - set(expected))
    if missing:
        raise WorkerError(f"Planned input is missing: {missing[0]}")
    if unexpected:
        raise WorkerError(f"Unexpected planned input: {unexpected[0]}")


def _run_command(
    experiment: ExperimentDirectory,
    working_directory: Path,
    configuration: ExperimentConfiguration,
    *,
    stream_output: bool,
) -> int:
    environment = os.environ.copy()
    environment.update(configuration.environment)
    environment["RUNFORGE_ARTIFACT_DIR"] = str(experiment.artifacts)
    environment["RUNFORGE_INPUT_DIR"] = str(experiment.inputs)
    paths = [str(working_directory / "src"), str(working_directory)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    command = configuration.command.script if configuration.command.mode == "shell" else configuration.command.arguments
    with experiment.stdout_log.open("wb") as stdout, experiment.stderr_log.open("wb") as stderr:
        try:
            if stream_output:
                process = subprocess.Popen(
                    command,
                    shell=configuration.command.mode == "shell",
                    cwd=working_directory,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                exit_code = _stream_process(process, stdout, stderr)
            else:
                exit_code = subprocess.run(
                    command,
                    shell=configuration.command.mode == "shell",
                    cwd=working_directory,
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


def _progress_event(
    phase: Literal["preparing", "executing", "completed", "failed"],
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
