"""Claim-coordinated holds, separate from execution status and immutable inputs."""

from __future__ import annotations

from pathlib import Path

from runforge.execution.errors import WorkerError
from runforge.infrastructure.claims import ClaimError, describe_claim_holder, release_claim, try_acquire_claim
from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.infrastructure.storage import ExperimentDirectory


class SchedulingError(WorkerError):
    """Raised when scheduling metadata is invalid or an experiment is busy."""


def is_held(experiment: ExperimentDirectory) -> bool:
    """Missing scheduling metadata means eligible; malformed metadata never does."""
    path = experiment.schedule_file
    if not path.exists():
        if path.is_symlink():
            raise SchedulingError(f"Invalid scheduling metadata symlink at {path}")
        return False
    try:
        data = load_json_object(path)
        if (
            set(data) != {"kind", "schema_version", "held", "updated_at"}
            or data["kind"] != "runforge_schedule"
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or not isinstance(data["held"], bool)
            or not isinstance(data["updated_at"], str)
            or not data["updated_at"].strip()
        ):
            raise ValueError("Unsupported or invalid scheduling metadata")
    except ValueError as error:
        raise SchedulingError(f"Invalid scheduling metadata at {path}: {error}") from error
    return data["held"]


def set_hold(path: Path, *, held: bool) -> None:
    """Edit a hold only while owning the same claim execution must acquire."""
    if not isinstance(held, bool):
        raise SchedulingError("held must be a boolean")
    experiment = ExperimentDirectory.resolve(path)
    try:
        claim = try_acquire_claim(experiment)
        if claim is None:
            raise SchedulingError(
                f"Experiment is already claimed: {experiment.root} ({describe_claim_holder(experiment)})"
            )
        try:
            _configuration, status = experiment.load_metadata()
            if status.state == "inprogress":
                raise SchedulingError(f"Cannot change scheduling for an inprogress experiment: {experiment.root}")
            save_json_object(
                experiment.schedule_file,
                {
                    "kind": "runforge_schedule",
                    "schema_version": 1,
                    "held": held,
                    "updated_at": utc_now(),
                },
            )
        finally:
            release_claim(experiment, claim)
    except (ClaimError, OSError, ValueError) as error:
        raise SchedulingError(str(error)) from error
