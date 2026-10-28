"""Read-only discovery of planned RunForge experiment directories."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from runforge.execution.scheduling import SchedulingError, is_held
from runforge.infrastructure.json_store import load_json_object
from runforge.infrastructure.storage import COMMAND_FILE, CONFIGURATION_FILE, STATUS_FILE, ExperimentDirectory
from runforge.schemas.experiment import ExperimentConfiguration, ExperimentStatus


class DiscoveryError(RuntimeError):
    """Raised when a discovery root cannot be scanned."""


@dataclass(frozen=True)
class DiscoveredExperiment:
    """One validated experiment directory and its current metadata."""

    path: Path
    configuration: ExperimentConfiguration
    status: ExperimentStatus
    held: bool = False


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    """One candidate or descendant that could not be inspected."""

    path: Path
    message: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Validated experiments and diagnostics from one read-only scan."""

    root: Path
    experiments: tuple[DiscoveredExperiment, ...]
    diagnostics: tuple[DiscoveryDiagnostic, ...]


def discover_experiments(root: Path = Path("."), *, read_scheduling: bool = True) -> DiscoveryResult:
    """Recursively discover valid experiments below root without mutation."""
    discovery_root = _resolve_root(root)
    candidates: list[Path] = []
    diagnostics: list[DiscoveryDiagnostic] = []

    def record_walk_error(error: OSError) -> None:
        path = Path(error.filename).resolve() if error.filename else discovery_root
        diagnostics.append(DiscoveryDiagnostic(path=path, message=f"could not scan directory: {error}"))

    try:
        walker = os.walk(discovery_root, topdown=True, onerror=record_walk_error, followlinks=False)
        for current_text, directory_names, file_names in walker:
            current = Path(current_text)
            # Dot-prefixed directories are never published plans. Planning builds each
            # plan in ".NAME.tmp-UUID" inside the report root before renaming it into
            # place, and that staging tree already holds config.json and status.json.
            # Descending into it would let a worker claim and execute an unpublished
            # plan that publication may still rename or delete.
            directory_names[:] = sorted(
                name for name in directory_names if not name.startswith(".") and not (current / name).is_symlink()
            )
            names = set(file_names)
            boundary, metadata_diagnostics = _inspect_experiment_boundary(current, names)
            diagnostics.extend(metadata_diagnostics)
            if boundary:
                candidates.append(current.resolve())
                # Inputs, outputs, captured source and retry archives are payloads,
                # even when they contain complete experiment metadata themselves.
                directory_names.clear()
    except OSError as error:
        raise DiscoveryError(f"Could not scan discovery root {discovery_root}: {error}") from error

    experiments: list[DiscoveredExperiment] = []
    for candidate in sorted(candidates):
        experiment, diagnostic = _load_candidate(candidate, read_scheduling=read_scheduling)
        if experiment is not None:
            experiments.append(experiment)
        if diagnostic is not None:
            diagnostics.append(diagnostic)

    return DiscoveryResult(
        root=discovery_root,
        experiments=tuple(experiments),
        diagnostics=tuple(sorted(diagnostics, key=lambda diagnostic: (diagnostic.path, diagnostic.message))),
    )


def _resolve_root(root: Path) -> Path:
    source = Path(root).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        raise DiscoveryError(f"Discovery root does not exist or cannot be resolved: {source}") from error
    if not resolved.is_dir():
        raise DiscoveryError(f"Discovery root is not a directory: {resolved}")
    try:
        with os.scandir(resolved):
            pass
    except OSError as error:
        raise DiscoveryError(f"Discovery root cannot be scanned: {resolved}: {error}") from error
    return resolved


def _load_candidate(
    candidate: Path,
    *,
    read_scheduling: bool,
) -> tuple[DiscoveredExperiment | None, DiscoveryDiagnostic | None]:
    layout = ExperimentDirectory(candidate)
    if not layout.configuration_file.is_file():
        return None, DiscoveryDiagnostic(candidate, f"missing {CONFIGURATION_FILE}")
    if not layout.status_file.is_file():
        return None, DiscoveryDiagnostic(candidate, f"missing {STATUS_FILE}")
    try:
        configuration, status = layout.load_metadata()
        held = is_held(layout) if read_scheduling else False
    except (ValueError, SchedulingError) as error:
        return None, DiscoveryDiagnostic(candidate, str(error))
    return DiscoveredExperiment(candidate, configuration, status, held), None


def _inspect_experiment_boundary(path: Path, names: set[str]) -> tuple[bool, tuple[DiscoveryDiagnostic, ...]]:
    """Require independent RunForge evidence before pruning malformed metadata."""
    metadata = names & {CONFIGURATION_FILE, STATUS_FILE}
    if not metadata:
        return False, ()
    if COMMAND_FILE in names:
        return True, ()
    diagnostics = []
    for filename in sorted(metadata):
        try:
            kind = load_json_object(path / filename).get("kind")
        except ValueError as error:
            diagnostics.append(DiscoveryDiagnostic(path / filename, str(error)))
            continue
        if kind in {"runforge_experiment_configuration", "runforge_experiment_status"}:
            return True, ()
    return False, tuple(diagnostics)


def discover_paths(
    paths: tuple[Path, ...], *, require_each: bool = False, read_scheduling: bool = True
) -> DiscoveryResult:
    """Union explicit report roots/experiment leaves by resolved path."""
    if not paths:
        paths = (Path("."),)
    experiments: dict[Path, DiscoveredExperiment] = {}
    diagnostics: dict[tuple[Path, str], DiscoveryDiagnostic] = {}
    first_root: Path | None = None
    for path in paths:
        result = discover_experiments(path, read_scheduling=read_scheduling)
        if first_root is None:
            first_root = result.root
        if require_each and not result.experiments and not result.diagnostics:
            raise DiscoveryError(f"No experiments found under: {result.root}")
        experiments.update((item.path, item) for item in result.experiments)
        diagnostics.update(((item.path, item.message), item) for item in result.diagnostics)
    assert first_root is not None
    return DiscoveryResult(
        first_root,
        tuple(experiments[path] for path in sorted(experiments)),
        tuple(diagnostics[key] for key in sorted(diagnostics)),
    )
