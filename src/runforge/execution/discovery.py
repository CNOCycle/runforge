"""Read-only discovery of planned RunForge experiment directories."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from runforge.infrastructure.storage import CONFIGURATION_FILE, STATUS_FILE, ExperimentDirectory
from runforge.schemas.experiment import ExperimentConfiguration, ExperimentStatus


class DiscoveryError(RuntimeError):
    """Raised when a discovery root cannot be scanned."""


@dataclass(frozen=True)
class DiscoveredExperiment:
    """One validated experiment directory and its current metadata."""

    path: Path
    configuration: ExperimentConfiguration
    status: ExperimentStatus


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


def discover_experiments(root: Path = Path(".")) -> DiscoveryResult:
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
            if CONFIGURATION_FILE in names or STATUS_FILE in names:
                candidates.append(current.resolve())
    except OSError as error:
        raise DiscoveryError(f"Could not scan discovery root {discovery_root}: {error}") from error

    experiments: list[DiscoveredExperiment] = []
    for candidate in sorted(candidates):
        experiment, diagnostic = _load_candidate(candidate)
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
) -> tuple[DiscoveredExperiment | None, DiscoveryDiagnostic | None]:
    layout = ExperimentDirectory(candidate)
    if not layout.configuration_file.is_file():
        return None, DiscoveryDiagnostic(candidate, f"missing {CONFIGURATION_FILE}")
    if not layout.status_file.is_file():
        return None, DiscoveryDiagnostic(candidate, f"missing {STATUS_FILE}")
    try:
        configuration, status = layout.load_metadata()
    except ValueError as error:
        return None, DiscoveryDiagnostic(candidate, str(error))
    return DiscoveredExperiment(candidate, configuration, status), None
