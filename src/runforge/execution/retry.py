"""Preparation of one existing experiment for another execution attempt."""

from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from runforge.execution.discovery import discover_paths, is_experiment_boundary
from runforge.execution.errors import ExperimentHeldError, ExperimentNotRunnableError
from runforge.execution.scheduling import SchedulingError, is_held
from runforge.infrastructure.claims import (
    ClaimError,
    ExperimentClaim,
    clear_claim,
    describe_claim_holder,
    load_claim,
    release_claim,
    try_acquire_claim,
    verify_claim_owner,
)
from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.json_store import save_json_object
from runforge.infrastructure.paths import is_absent, is_safe_directory
from runforge.infrastructure.storage import (
    ARTIFACTS_DIRECTORY,
    STDERR_LOG_FILE,
    STDOUT_LOG_FILE,
    ExperimentDirectory,
)
from runforge.schemas.experiment import ExperimentStatus


_STATUS_SNAPSHOT_FILE = "status.snapshot.json"
_ARCHIVED_OUTPUTS = (STDOUT_LOG_FILE, STDERR_LOG_FILE, ARTIFACTS_DIRECTORY)
_ARTIFACT_SEED = ".artifacts-seed"
_ClaimIdentity = tuple[int, int, int, str | None]


class RetryError(RuntimeError):
    """Raised when an experiment cannot be prepared for retry safely."""


class RetryNotRunnableError(RetryError, ExperimentNotRunnableError):
    """An ineligible retry state, usable as a stale selection or retry error."""


class _RetryClaimError(RetryError):
    """Acquisition failed after a possibly completed ownership override."""

    def __init__(self, message: str, *, claim_cleared: bool):
        super().__init__(message)
        self.claim_cleared = claim_cleared


def is_retryable_state(state: str, *, force: bool = False) -> bool:
    """The shared selection/preparation policy for retryable lifecycle states."""
    return state == "failed" or (force and state == "inprogress")


@dataclass(frozen=True)
class RetryPreparation:
    """Result of preparing one experiment for a later worker invocation."""

    experiment: Path
    archive: Path
    previous_status: ExperimentStatus
    status: ExperimentStatus
    forced: bool


@dataclass(frozen=True)
class RetryOutcome:
    """Preparation or skip/error for one selected experiment."""

    experiment: Path
    outcome: Literal["prepared", "non-runnable", "held", "claimed", "stale", "invalid"]
    message: str | None = None
    forced: bool = False


@dataclass(frozen=True)
class RetryResult:
    """A fixed preparation batch; success does not assert task execution."""

    outcomes: tuple[RetryOutcome, ...]

    @property
    def prepared(self) -> int:
        return sum(item.outcome == "prepared" for item in self.outcomes)

    @property
    def invalid(self) -> int:
        return sum(item.outcome == "invalid" for item in self.outcomes)

    @property
    def skipped(self) -> int:
        return len(self.outcomes) - self.prepared - self.invalid

    @property
    def forced(self) -> int:
        return sum(item.forced for item in self.outcomes)

    @property
    def exit_code(self) -> int:
        return 2 if self.invalid else 0


def prepare_retries(
    paths: tuple[Path, ...],
    *,
    force: bool = False,
    reuse_artifacts: bool = False,
    progress: Callable[[RetryOutcome], None] | None = None,
) -> RetryResult:
    """Prepare a discovered selection once without executing experiment commands.

    Successful records are immediately visible to workers after claim release;
    this is not an atomic publication of the entire batch.
    """
    if not paths:
        raise RetryError("Retry requires explicit experiment or report-root paths")
    if not isinstance(force, bool) or not isinstance(reuse_artifacts, bool):
        raise RetryError("force and reuse_artifacts must be booleans")
    discovery = discover_paths(paths, require_each=True)
    if force:
        leaves = {item.path for item in discovery.experiments}
        # Damaged leaves are target-specific errors; a report root is never a
        # valid forced selection, even when it contains damaged experiments.
        leaves.update(item.path for item in discovery.diagnostics if is_experiment_boundary(item.path))
        if any(Path(path).expanduser().resolve() not in leaves for path in paths):
            raise RetryError("Forced retry requires explicit experiment-directory paths, not report roots")
    outcomes = [RetryOutcome(item.path, "invalid", item.message) for item in discovery.diagnostics]
    for item in outcomes:
        _report_retry_outcome(progress, item)
    for item in discovery.experiments:
        if item.held:
            outcome = RetryOutcome(item.path, "held", "scheduling hold prevents retry")
        elif not is_retryable_state(item.status.state, force=force):
            outcome = RetryOutcome(item.path, "non-runnable", f"state={item.status.state}; not eligible for retry")
        else:
            outcome = _prepare_retry_target(
                ExperimentDirectory(item.path), force=force, reuse_artifacts=reuse_artifacts
            )
        outcomes.append(outcome)
        _report_retry_outcome(progress, outcome)
    return RetryResult(tuple(outcomes))


def _prepare_retry_target(experiment: ExperimentDirectory, *, force: bool, reuse_artifacts: bool) -> RetryOutcome:
    claim = None
    overridden = False
    outcome = RetryOutcome(experiment.root, "invalid", "Preparation interrupted")
    try:
        claim, overridden = acquire_retry_claim(experiment, force=force)
        if claim is None:
            outcome = RetryOutcome(
                experiment.root,
                "claimed",
                f"Experiment is already claimed ({describe_claim_holder(experiment)}); "
                "use retry --force only after confirming its worker AND child stopped",
                forced=overridden,
            )
        else:
            if is_held(experiment):
                raise ExperimentHeldError("scheduling hold prevents retry")
            preparation = prepare_retry(experiment.root, force=force, claim=claim, reuse_artifacts=reuse_artifacts)
            outcome = RetryOutcome(experiment.root, "prepared", forced=preparation.forced or overridden)
    except ExperimentHeldError as error:
        outcome = RetryOutcome(experiment.root, "held", str(error), forced=overridden)
    except RetryNotRunnableError as error:
        outcome = RetryOutcome(experiment.root, "stale", str(error), forced=overridden)
    except _RetryClaimError as error:
        overridden = error.claim_cleared
        outcome = RetryOutcome(experiment.root, "invalid", str(error), forced=overridden)
    except (ClaimError, RetryError, SchedulingError, ValueError, OSError) as error:
        outcome = RetryOutcome(experiment.root, "invalid", str(error), forced=overridden)
    finally:
        if claim is not None:
            try:
                release_claim(experiment, claim)
            except ClaimError as error:
                outcome = RetryOutcome(
                    experiment.root,
                    "invalid",
                    f"{outcome.message or outcome.outcome}; could not release preparation claim: {error}",
                    forced=outcome.forced,
                )
    if overridden and outcome.outcome != "prepared":
        outcome = replace(outcome, message=f"{outcome.message}; forced retry already cleared the previous claim")
    return outcome


def _report_retry_outcome(progress: Callable[[RetryOutcome], None] | None, outcome: RetryOutcome) -> None:
    if progress is not None:
        try:
            progress(outcome)
        except Exception:
            # Progress reporting cannot change preparation outcomes.
            pass


@dataclass
class _ArchiveTransaction:
    experiment: Path
    staging: Path
    archive: Path
    moved_outputs: list[str] = field(default_factory=list)
    published: bool = False
    created_artifacts: bool = False
    seeded_artifacts: bool = False


def prepare_retry(
    experiment_path: Path,
    *,
    force: bool = False,
    claim: ExperimentClaim | None = None,
    reuse_artifacts: bool = False,
) -> RetryPreparation:
    """Archive one prior attempt, optionally copying its artifacts into the retry."""
    layout = ExperimentDirectory.resolve(experiment_path)
    if not layout.root.is_dir():
        raise RetryError(f"Experiment directory does not exist: {layout.root}")
    if not isinstance(force, bool):
        raise RetryError("force must be a boolean")
    if not isinstance(reuse_artifacts, bool):
        raise RetryError("reuse_artifacts must be a boolean")

    try:
        _configuration, previous_status = layout.load_metadata()
    except ValueError as error:
        raise RetryError(str(error)) from error

    # Forcing is reported only when it actually overrode a sign of possible
    # liveness: an inprogress state, or a claim this retry had to clear.
    state_forced = _validate_retry_state(previous_status, force=force)
    if claim is None:
        claim_cleared = _prepare_retry_claim(layout, force=force)
    else:
        verify_claim_owner(layout, claim)
        claim_cleared = False
    forced = state_forced or claim_cleared
    archive = _archive_path(layout.root, previous_status)
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
        _archive_and_reset(layout, archive, previous_status, prepared_status, reuse_artifacts=reuse_artifacts)
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
    if is_retryable_state(status.state, force=force):
        return status.state == "inprogress"
    if status.state == "inprogress":
        raise RetryNotRunnableError(
            "Experiment is inprogress; force=True is required after confirming that the original process has stopped"
        )
    if status.state == "completed":
        raise RetryNotRunnableError("Completed experiments cannot be retried; create a new plan instead")
    raise RetryNotRunnableError(f"Experiment in state {status.state!r} should be executed with run, not retry")


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
        return _clear_existing_claim(experiment, expected_identity=_claim_identity(experiment))
    except ClaimError as error:
        raise RetryError(str(error)) from error


def _archive_and_reset(
    experiment: ExperimentDirectory,
    archive: Path,
    previous_status: ExperimentStatus,
    prepared_status: ExperimentStatus,
    *,
    reuse_artifacts: bool = False,
) -> None:
    _validate_retry_archive(archive)
    if reuse_artifacts:
        _validate_artifact_tree(experiment.artifacts)
    history = archive.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{archive.name}.tmp-", dir=history))
    transaction = _ArchiveTransaction(experiment=experiment.root, staging=staging, archive=archive)
    try:
        save_json_object(staging / _STATUS_SNAPSHOT_FILE, previous_status.to_dict())
        if reuse_artifacts:
            # Keep old outputs in place until an independent, complete seed exists.
            # Never follow links if the tree changes after preflight.
            shutil.copytree(experiment.artifacts, staging / _ARTIFACT_SEED, symlinks=True)
            _validate_artifact_tree(staging / _ARTIFACT_SEED)
        for name in _ARCHIVED_OUTPUTS:
            if _move_output(experiment.root, staging, name):
                transaction.moved_outputs.append(name)
        staging.replace(archive)
        transaction.published = True
        if reuse_artifacts:
            (archive / _ARTIFACT_SEED).replace(experiment.artifacts)
            transaction.seeded_artifacts = True
        else:
            experiment.artifacts.mkdir()
        transaction.created_artifacts = True
        experiment.save_status(prepared_status)
    except BaseException as error:
        try:
            _rollback_archive(transaction)
        except OSError as rollback_error:
            raise RetryError(
                f"Could not prepare retry for {experiment.root}; rollback also failed: {rollback_error}; "
                f"preserve and inspect {staging} and {archive} before retrying"
            ) from rollback_error
        if isinstance(error, RetryError):
            raise
        if isinstance(error, Exception):
            raise RetryError(f"Could not prepare retry for {experiment.root}: {error}") from error
        raise


def _archive_path(root: Path, previous: ExperimentStatus) -> Path:
    """Name the archive after the attempt number.

    A failure before the command starts never advances the number, so its archives take a suffix. An
    existing archive of a started attempt is still refused by ``_validate_retry_archive``.
    """
    archive = root / f"attempt-{previous.attempt:04d}"
    suffix = 0
    while previous.started_at is None and not is_absent(archive):
        suffix += 1
        archive = root / f"attempt-{previous.attempt:04d}-{suffix}"
    return archive


def _validate_retry_archive(archive: Path) -> None:
    """Do not overwrite history or hide interrupted preparation for this attempt."""
    history = archive.parent
    # A dangling symlink reports exists() as false while still occupying the
    # path, so absence and a usable directory must both be checked.
    if not is_absent(history) and not is_safe_directory(history):
        raise RetryError(f"Retry history path is not a directory: {history}")
    history.mkdir(parents=True, exist_ok=True)
    if not is_absent(archive):
        raise RetryError(f"Retry archive already exists: {archive}")
    leftover = next(history.glob(f".{archive.name}.tmp-*"), None)
    if leftover is not None:
        raise RetryError(
            f"Interrupted retry preparation at {leftover}; inspect and recover its outputs before retrying"
        )


def _validate_artifact_tree(root: Path) -> None:
    """Reject missing trees, links and special files before publishing a seed."""
    if not is_safe_directory(root):
        raise RetryError(f"Artifact reuse requires a real directory: {root}")
    for path in root.iterdir():
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            _validate_artifact_tree(path)
        elif not stat.S_ISREG(mode):
            raise RetryError(f"Artifact reuse supports only regular files and directories: {path}")


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
        artifacts = ExperimentDirectory(transaction.experiment).artifacts
        if transaction.seeded_artifacts:
            shutil.rmtree(artifacts)
        else:
            artifacts.rmdir()
    for name in reversed(transaction.moved_outputs):
        destination = transaction.experiment / name
        if not is_absent(destination):
            raise OSError(f"Cannot restore retry output because the destination exists: {destination}")
        (location / name).replace(destination)
    if location.exists():
        shutil.rmtree(location)
    if transaction.staging != location and transaction.staging.exists():
        shutil.rmtree(transaction.staging)


def acquire_retry_claim(experiment: ExperimentDirectory, *, force: bool = False) -> tuple[ExperimentClaim | None, bool]:
    """Validate before an explicit force override, then acquire preparation ownership."""
    observed_claim = _claim_identity(experiment) if force else None
    _configuration, status = experiment.load_metadata()
    _validate_retry_state(status, force=force)
    if is_held(experiment):
        # A hold written after discovery must not cost the previous owner's claim evidence.
        raise ExperimentHeldError(f"Experiment is held: {experiment.root}")
    overridden = _clear_existing_claim(experiment, expected_identity=observed_claim) if force else False
    try:
        return try_acquire_claim(experiment), overridden
    except (ClaimError, OSError, ValueError) as error:
        raise _RetryClaimError(str(error), claim_cleared=overridden) from error


def _claim_identity(experiment: ExperimentDirectory) -> _ClaimIdentity | None:
    """Observe directory identity even when an abandoned claim has unreadable metadata."""
    try:
        metadata = experiment.claim.lstat()
    except FileNotFoundError:
        return None
    try:
        token = load_claim(experiment).token
    except ClaimError:
        token = None
    return metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns, token


def _clear_existing_claim(experiment: ExperimentDirectory, *, expected_identity: _ClaimIdentity | None) -> bool:
    """Refuse a changed owner/state instead of clearing a peer's new claim."""
    if _claim_identity(experiment) != expected_identity:
        raise RetryNotRunnableError("Claim changed during forced retry; preserve the new owner and inspect it")
    if expected_identity is None:
        return False
    _configuration, status = experiment.load_metadata()
    _validate_retry_state(status, force=True)
    if is_held(experiment):
        raise ExperimentHeldError(f"Experiment is held: {experiment.root}")
    if _claim_identity(experiment) != expected_identity:
        raise RetryNotRunnableError("Claim changed during forced retry; preserve the new owner and inspect it")
    clear_claim(experiment)
    return True
