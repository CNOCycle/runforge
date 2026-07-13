"""One-shot execution of one explicit planned experiment directory."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from runforge.experiment_schema import ExperimentConfiguration, ExperimentStatus
from runforge.git_ops import GitOperationError, GitRepository
from runforge.json_store import load_json_object, save_json_object


class WorkerError(RuntimeError):
    """Raised when one planned experiment cannot be prepared or executed."""


def run_experiment(experiment_path: Path) -> int:
    """Run one explicit experiment directory and return its command exit code."""
    experiment = Path(experiment_path).expanduser().resolve()
    if not experiment.is_dir():
        raise WorkerError(f"Experiment directory does not exist: {experiment}")
    try:
        configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))
        status = ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    except ValueError as error:
        raise WorkerError(str(error)) from error
    if status.state not in {"created", "init"}:
        raise WorkerError(f"Experiment is not runnable from state {status.state!r}")

    status = _save_status(experiment, replace(status, state="init", updated_at=_utc_now(), error=None))
    try:
        return _execute(experiment, configuration, status)
    except WorkerError as error:
        _save_status(
            experiment,
            replace(status, state="failed", updated_at=_utc_now(), finished_at=_utc_now(), error=str(error)),
        )
        raise
    except OSError as error:
        failure = WorkerError(str(error))
        _save_status(
            experiment,
            replace(status, state="failed", updated_at=_utc_now(), finished_at=_utc_now(), error=str(failure)),
        )
        raise failure from error


def _execute(experiment: Path, configuration: ExperimentConfiguration, status: ExperimentStatus) -> int:
    try:
        repository = GitRepository.discover(configuration.source.repository)
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
                    updated_at=_utc_now(),
                    started_at=_utc_now(),
                ),
            )
            exit_code = _run_command(experiment, worktree, configuration)
            final_state = "completed" if exit_code == 0 else "failed"
            error = None if exit_code == 0 else f"Command exited with status {exit_code}"
            _save_status(
                experiment,
                replace(
                    active_status,
                    state=final_state,
                    updated_at=_utc_now(),
                    finished_at=_utc_now(),
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


def _run_command(experiment: Path, worktree: Path, configuration: ExperimentConfiguration) -> int:
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
            result = subprocess.run(
                command,
                shell=configuration.command.mode == "shell",
                cwd=worktree,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
        except OSError as error:
            raise WorkerError(f"Could not start command: {error}") from error
        stdout.flush()
        stderr.flush()
        os.fsync(stdout.fileno())
        os.fsync(stderr.fileno())
    return result.returncode


def _save_status(experiment: Path, status: ExperimentStatus) -> ExperimentStatus:
    save_json_object(experiment / "status.json", status.to_dict())
    return status


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
