"""Integrity checks for the immutable input tree published with a plan.

A worker must reject a plan whose rendered inputs no longer match the manifest
recorded at planning time, because the digests are what make the rendered bytes
part of the experiment's identity.
"""

from __future__ import annotations

import hashlib

from runforge.execution.errors import WorkerError
from runforge.infrastructure.storage import ExperimentDirectory


def verify_planned_inputs(experiment: ExperimentDirectory) -> None:
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
