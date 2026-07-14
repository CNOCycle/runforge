"""Git-backed single and matrix experiment planning without execution."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import uuid
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.json_store import save_json_object
from runforge.matrix import MatrixError, expand_matrix
from runforge.source_metadata import PinnedGitSource
from runforge.source_resolver import (
    ResolvedGitSource,
    SourceResolutionError,
    resolve_current_git_source,
    resolve_pinned_git_source,
)
from runforge.time_utils import utc_now


_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_COMMIT_PREFIX_LENGTH = 8
_DEFAULT_OUTPUT_ROOT = Path("reports")


class PlanningError(RuntimeError):
    """Raised when one experiment directory cannot be planned safely."""


@dataclass(frozen=True)
class PlanRequest:
    """Input for one current-HEAD or explicitly pinned experiment plan."""

    name: str
    command: ExperimentCommand
    output_root: Path | None = None
    source_path: Path = Path(".")
    source: PinnedGitSource | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise PlanningError("name must be a non-empty string")
        if not isinstance(self.command, ExperimentCommand):
            raise PlanningError("command must be ExperimentCommand metadata")
        if self.output_root is not None and not isinstance(self.output_root, Path):
            object.__setattr__(self, "output_root", Path(self.output_root))
        if not isinstance(self.source_path, Path):
            object.__setattr__(self, "source_path", Path(self.source_path))
        if self.source is not None and not isinstance(self.source, PinnedGitSource):
            raise PlanningError("source must be PinnedGitSource metadata")
        environment = dict(self.environment)
        if not all(isinstance(key, str) and key and isinstance(value, str) for key, value in environment.items()):
            raise PlanningError("environment must map non-empty strings to strings")
        object.__setattr__(self, "environment", environment)


@dataclass(frozen=True)
class MatrixPlanRequest:
    """A pinned plan template plus one validated Cartesian parameter matrix."""

    template: PlanRequest
    parameters: Mapping[str, Sequence[object]]
    combinations: tuple[dict[str, str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.template, PlanRequest):
            raise PlanningError("template must be PlanRequest metadata")
        if self.template.source is None:
            raise PlanningError("matrix planning requires a pinned Git source")
        try:
            combinations = expand_matrix(self.parameters)
        except MatrixError as error:
            raise PlanningError(str(error)) from error
        object.__setattr__(self, "parameters", {key: tuple(self.parameters[key]) for key in sorted(self.parameters)})
        object.__setattr__(self, "combinations", combinations)


@dataclass(frozen=True)
class _PreparedExperiment:
    destination: Path
    configuration: ExperimentConfiguration
    status: ExperimentStatus


def plan_experiment(request: PlanRequest) -> Path:
    """Create one self-contained current-HEAD or pinned experiment plan."""
    return _plan_resolved_experiment(request, _resolve_source(request))


def plan_matrix(request: MatrixPlanRequest) -> tuple[Path, ...]:
    """Create one experiment plan per deterministic pinned-source combination."""
    return _plan_resolved_matrix(request, _resolve_source(request.template))


def _resolve_source(request: PlanRequest) -> ResolvedGitSource:
    try:
        if request.source is None:
            resolved = resolve_current_git_source(request.source_path)
        else:
            resolved = resolve_pinned_git_source(request.source)
    except SourceResolutionError as error:
        raise PlanningError(str(error)) from error
    return resolved


def _plan_resolved_experiment(request: PlanRequest, resolved: ResolvedGitSource) -> Path:
    """Publish one plan from source metadata that has already been resolved."""
    source = resolved.source
    output_root = request.output_root or resolved.repository.root / _DEFAULT_OUTPUT_ROOT
    destination = _destination(output_root, source.branch, source.commit, request.name)
    prepared = _prepare_experiment(request, resolved, destination, {}, utc_now())
    _publish(prepared.destination, prepared.configuration, prepared.status, resolved.patch)
    _warn_untracked(source.untracked_files)
    return destination


def _plan_resolved_matrix(request: MatrixPlanRequest, resolved: ResolvedGitSource) -> tuple[Path, ...]:
    """Prepare every combination before publishing any experiment directory."""
    template = request.template
    source = resolved.source
    output_root = template.output_root or resolved.repository.root / _DEFAULT_OUTPUT_ROOT
    reserved: set[Path] = set()
    timestamp = utc_now()
    prepared: list[_PreparedExperiment] = []
    for parameters in request.combinations:
        destination = _destination(output_root, source.branch, source.commit, template.name, reserved=reserved)
        reserved.add(destination)
        prepared.append(_prepare_experiment(template, resolved, destination, parameters, timestamp))
    created: list[Path] = []
    try:
        for experiment in prepared:
            _publish(experiment.destination, experiment.configuration, experiment.status, resolved.patch)
            created.append(experiment.destination)
    except Exception:
        for destination in created:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return tuple(experiment.destination for experiment in prepared)


def _prepare_experiment(
    request: PlanRequest,
    resolved: ResolvedGitSource,
    destination: Path,
    parameters: Mapping[str, str],
    timestamp: str,
) -> _PreparedExperiment:
    """Build and validate immutable plan data without publishing files."""
    placeholders = dict(parameters)
    placeholders["ARTIFACT_DIR"] = str(destination / "artifacts")
    rendered_command = request.command.render_placeholders(placeholders)
    configuration = ExperimentConfiguration(
        experiment_id=destination.name,
        name=request.name,
        command=rendered_command,
        environment=request.environment,
        source=resolved.source,
        created_at=timestamp,
        parameters=parameters,
    )
    status = ExperimentStatus(state="created", attempt=0, updated_at=timestamp)
    return _PreparedExperiment(destination, configuration, status)


def _destination(
    output_root: Path,
    branch: str,
    commit: str,
    name: str,
    reserved: set[Path] | None = None,
) -> Path:
    parent = output_root.expanduser().resolve() / _slug(branch, "detached")
    parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{commit[:_COMMIT_PREFIX_LENGTH]}_{_slug(name, 'experiment')}"
    index = 0
    while True:
        destination = parent / f"{prefix}_{index}"
        if not destination.exists() and (reserved is None or destination not in reserved):
            return destination
        index += 1


def _warn_untracked(untracked_files: Sequence[str]) -> None:
    if untracked_files:
        warnings.warn(
            "Planned Git source has untracked files that are not included in git.patch:\n"
            + "\n".join(f"  {path}" for path in untracked_files),
            stacklevel=3,
        )


def _publish(
    destination: Path,
    configuration: ExperimentConfiguration,
    status: ExperimentStatus,
    patch: bytes,
) -> None:
    temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        save_json_object(temporary / "config.json", configuration.to_dict())
        save_json_object(temporary / "status.json", status.to_dict())
        (temporary / "artifacts").mkdir()
        _write_command_file(temporary / "cmd.sh", configuration.command)
        if patch:
            (temporary / "git.patch").write_bytes(patch)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _write_command_file(path: Path, command: ExperimentCommand) -> None:
    command_text = command.script if command.mode == "shell" else shlex.join(command.arguments)
    path.write_text(f"#!/bin/sh\n{command_text}\n", encoding="utf-8")
    path.chmod(0o755)


def _slug(value: str, fallback: str) -> str:
    slug = _SLUG_PATTERN.sub("-", value.strip()).strip("-._")
    return (slug or fallback)[:64]
