"""Atomic ownership claims for shared experiment execution."""

from __future__ import annotations

import os
import shutil
import socket
import uuid
from dataclasses import dataclass

from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.json_store import JsonStoreError, load_json_object, save_json_object
from runforge.infrastructure.paths import is_safe_directory
from runforge.infrastructure.storage import ExperimentDirectory


CLAIM_OWNER_VARIABLE = "RUNFORGE_CLAIM_OWNER"


class ClaimError(RuntimeError):
    """Raised when an experiment claim cannot be read, acquired, or released."""


class ClaimOwnershipError(ClaimError):
    """Raised when a process tries to release another owner's claim."""


@dataclass(frozen=True)
class ExperimentClaim:
    """Identity metadata for one exclusive experiment claim."""

    token: str
    owner: str
    acquired_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "runforge_experiment_claim",
            "schema_version": 1,
            "token": self.token,
            "owner": self.owner,
            "acquired_at": self.acquired_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> ExperimentClaim:
        if not isinstance(value, dict):
            raise ClaimError("Claim metadata must be a JSON object")
        expected = {"kind", "schema_version", "token", "owner", "acquired_at"}
        if set(value) != expected:
            raise ClaimError("Claim metadata has unexpected fields")
        if value["kind"] != "runforge_experiment_claim" or value["schema_version"] != 1:
            raise ClaimError("Unsupported experiment claim metadata")
        fields = ("token", "owner", "acquired_at")
        if not all(isinstance(value[field], str) and value[field] for field in fields):
            raise ClaimError("Claim metadata fields must be non-empty strings")
        return cls(token=value["token"], owner=value["owner"], acquired_at=value["acquired_at"])


def _default_owner() -> str:
    """Resolve the recorded claim owner from the environment or the local process."""
    # One explicitly named variable; RunForge never snapshots the ambient environment.
    configured = " ".join(os.environ.get(CLAIM_OWNER_VARIABLE, "").split())
    return configured or f"{socket.gethostname()}:{os.getpid()}"


def try_acquire_claim(
    experiment: ExperimentDirectory,
    *,
    owner: str | None = None,
) -> ExperimentClaim | None:
    """Atomically acquire an experiment claim, returning None on contention.

    Ownership is decided only by the random claim token. ``owner`` never affects
    that decision; it records which process an operator must check before
    forcing recovery, and it is reported by ``describe_claim_holder``. Its value
    is resolved from the explicit argument, then ``RUNFORGE_CLAIM_OWNER``, then
    ``HOSTNAME:PID``.

    The default suits a native process, which an operator can check with
    ``ps -p PID`` on that host. Set the variable where the pair is not
    addressable from outside -- a container whose hostname is an image-local id
    and whose pids are namespaced, or a batch job that should be identified by
    its scheduler id.
    """
    claim = ExperimentClaim(
        token=uuid.uuid4().hex,
        owner=owner or _default_owner(),
        acquired_at=utc_now(),
    )
    try:
        experiment.claim.mkdir()
    except FileExistsError:
        return None
    except OSError as error:
        raise ClaimError(f"Could not acquire claim for {experiment.root}: {error}") from error
    try:
        save_json_object(experiment.claim_file, claim.to_dict())
    except (OSError, JsonStoreError) as error:
        try:
            experiment.claim.rmdir()
        except OSError as cleanup_error:
            raise ClaimError(
                f"Could not write claim for {experiment.root}: {error}; could not remove partial claim: {cleanup_error}"
            ) from error
        raise ClaimError(f"Could not write claim for {experiment.root}: {error}") from error
    return claim


def load_claim(experiment: ExperimentDirectory) -> ExperimentClaim:
    """Load the claim currently occupying an experiment."""
    try:
        return ExperimentClaim.from_dict(load_json_object(experiment.claim_file))
    except (OSError, JsonStoreError, ClaimError) as error:
        raise ClaimError(f"Could not load claim for {experiment.root}: {error}") from error


def describe_claim_holder(experiment: ExperimentDirectory) -> str:
    """Describe the current claim holder for an operator-facing message."""
    try:
        claim = load_claim(experiment)
    except ClaimError:
        return "holder unknown: claim metadata is missing or unreadable"
    return f"held by {claim.owner} since {claim.acquired_at}"


def verify_claim_owner(experiment: ExperimentDirectory, claim: ExperimentClaim) -> None:
    """Verify that the current claim token still owns the experiment."""
    current = load_claim(experiment)
    if current.token != claim.token:
        raise ClaimOwnershipError(
            f"Claim for {experiment.root} belongs to another owner: held by {current.owner} since {current.acquired_at}"
        )


def release_claim(experiment: ExperimentDirectory, claim: ExperimentClaim) -> None:
    """Release a claim only when its token still owns the experiment."""
    verify_claim_owner(experiment, claim)
    try:
        experiment.claim_file.unlink()
        experiment.claim.rmdir()
    except OSError as error:
        raise ClaimError(f"Could not release claim for {experiment.root}: {error}") from error


def clear_claim(experiment: ExperimentDirectory) -> None:
    """Remove an abandoned claim after the operator has confirmed its owner stopped."""
    if not experiment.claim.exists():
        return
    if not is_safe_directory(experiment.claim):
        raise ClaimError(f"Claim path is not a directory: {experiment.claim}")
    try:
        shutil.rmtree(experiment.claim)
    except OSError as error:
        raise ClaimError(f"Could not clear claim for {experiment.root}: {error}") from error
