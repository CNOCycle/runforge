"""Preparation of one existing experiment for another execution attempt."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from runforge.infrastructure.claims import ClaimError, clear_claim, describe_claim_holder
from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.json_store import save_json_object
from runforge.infrastructure.storage import (
    ARTIFACTS_DIRECTORY,
    STDERR_LOG_FILE,
    STDOUT_LOG_FILE,
    ExperimentDirectory,
)
from runforge.schemas.experiment import ExperimentStatus


_STATUS_SNAPSHOT_FILE = "status.snapshot.json"
_ARCHIVED_OUTPUTS = (STDOUT_LOG_FILE, STDERR_LOG_FILE, ARTIFACTS_DIRECTORY)


class RetryError(RuntimeError):
    """Raised when an experiment cannot be prepared for retry safely."""


@dataclass(frozen=True)
class RetryPreparation:
    """Result of preparing one experiment for a later worker invocation."""

    experiment: Path
    archive: Path
    previous_status: ExperimentStatus
    status: ExperimentStatus
    forced: bool


@dataclass
class _ArchiveTransaction:
    experiment: Path
    staging: Path
    archive: Path
    moved_outputs: list[str] = field(default_factory=list)
    published: bool = False
    created_artifacts: bool = False


def prepare_retry(experiment_path: Path, *, force: bool = False) -> RetryPreparation:
    """Archive one prior attempt and atomically make the experiment runnable."""
    layout = ExperimentDirectory.resolve(experiment_path)
    if not layout.root.is_dir():
        raise RetryError(f"Experiment directory does not exist: {layout.root}")
    if not isinstance(force, bool):
        raise RetryError("force must be a boolean")

    try:
        _configuration, previous_status = layout.load_metadata()
    except ValueError as error:
        raise RetryError(str(error)) from error

    # Forcing is reported only when it actually overrode a sign of possible
    # liveness: an inprogress state, or a claim this retry had to clear.
    state_forced = _validate_retry_state(previous_status, force=force)
    claim_cleared = _prepare_retry_claim(layout, force=force)
    forced = state_forced or claim_cleared
    archive = layout.root / f"attempt-{previous_status.attempt:04d}"
    prepared_status = replace(
        previous_status,
        state="init",
        updated_at=utc_now(),
        started_at=None,
        finished_at=None,
        exit_code=None,
        error=None,
    )
    try:
        _archive_and_reset(layout, archive, previous_status, prepared_status)
    except RetryError:
        raise
    except OSError as error:
        raise RetryError(f"Could not prepare retry for {layout.root}: {error}") from error
    return RetryPreparation(
        experiment=layout.root,
        archive=archive,
        previous_status=previous_status,
        status=prepared_status,
        forced=forced,
    )


def _validate_retry_state(status: ExperimentStatus, *, force: bool) -> bool:
    """Return whether the recorded state itself required operator confirmation."""
    if status.state == "failed":
        return False
    if status.state == "inprogress":
        if force:
            return True
        raise RetryError(
            "Experiment is inprogress; force=True is required after confirming that the original process has stopped"
        )
    if status.state == "completed":
        raise RetryError("Completed experiments cannot be retried; create a new plan instead")
    raise RetryError(f"Experiment in state {status.state!r} should be executed with run, not retry")


def _prepare_retry_claim(experiment: ExperimentDirectory, *, force: bool) -> bool:
    """Clear an abandoned claim with confirmation, reporting whether one was cleared."""
    if not experiment.claim.exists():
        return False
    if not force:
        raise RetryError(
            f"Experiment is already claimed: {experiment.root} ({describe_claim_holder(experiment)}); "
            "use force=True only after confirming its worker stopped"
        )
    try:
        clear_claim(experiment)
    except ClaimError as error:
        raise RetryError(str(error)) from error
    return True


def _archive_and_reset(
    experiment: ExperimentDirectory,
    archive: Path,
    previous_status: ExperimentStatus,
    prepared_status: ExperimentStatus,
) -> None:
    history = archive.parent
    if history.is_symlink() or (history.exists() and not history.is_dir()):
        raise RetryError(f"Retry history path is not a directory: {history}")
    history.mkdir(parents=True, exist_ok=True)
    if archive.is_symlink() or archive.exists():
        raise RetryError(f"Retry archive already exists: {archive}")

    staging = Path(tempfile.mkdtemp(prefix=f".{archive.name}.tmp-", dir=history))
    transaction = _ArchiveTransaction(experiment=experiment.root, staging=staging, archive=archive)
    try:
        save_json_object(staging / _STATUS_SNAPSHOT_FILE, previous_status.to_dict())
        for name in _ARCHIVED_OUTPUTS:
            if _move_output(experiment.root, staging, name):
                transaction.moved_outputs.append(name)
        staging.replace(archive)
        transaction.published = True
        experiment.artifacts.mkdir()
        transaction.created_artifacts = True
        experiment.save_status(prepared_status)
    except BaseException as error:
        try:
            _rollback_archive(transaction)
        except OSError as rollback_error:
            raise RetryError(
                f"Could not prepare retry for {experiment.root}; rollback also failed: {rollback_error}"
            ) from rollback_error
        if isinstance(error, RetryError):
            raise
        if isinstance(error, Exception):
            raise RetryError(f"Could not prepare retry for {experiment.root}: {error}") from error
        raise


def _move_output(experiment: Path, staging: Path, name: str) -> bool:
    source = experiment / name
    if source.is_symlink():
        raise RetryError(f"Retry output path must not be a symbolic link: {source}")
    if not source.exists():
        return False
    if name == ARTIFACTS_DIRECTORY:
        if not source.is_dir():
            raise RetryError(f"Retry artifact path is not a directory: {source}")
    elif not source.is_file():
        raise RetryError(f"Retry log path is not a file: {source}")
    source.replace(staging / name)
    return True


def _rollback_archive(transaction: _ArchiveTransaction) -> None:
    location = transaction.archive if transaction.published else transaction.staging
    if transaction.created_artifacts:
        ExperimentDirectory(transaction.experiment).artifacts.rmdir()
    for name in reversed(transaction.moved_outputs):
        destination = transaction.experiment / name
        if destination.is_symlink() or destination.exists():
            raise OSError(f"Cannot restore retry output because the destination exists: {destination}")
        (location / name).replace(destination)
    if location.exists():
        shutil.rmtree(location)
    if transaction.staging != location and transaction.staging.exists():
        shutil.rmtree(transaction.staging)
