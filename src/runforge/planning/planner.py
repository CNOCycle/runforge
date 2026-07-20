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

from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.inputs import InputRenderingError, InputTemplate, RenderedInput, render_input_templates
from runforge.planning.matrix import JsonScalar, MatrixError, expand_matrix, parameter_text
from runforge.planning.source import (
    ResolvedGitSource,
    SourceResolutionError,
    resolve_current_git_source,
    resolve_pinned_git_source,
)
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.inputs import PlannedInput, PlannedInputManifest
from runforge.schemas.source import PinnedGitSource


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
    inputs: Sequence[InputTemplate] = ()

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
        if isinstance(self.inputs, (str, bytes)) or not isinstance(self.inputs, Sequence):
            raise PlanningError("inputs must be an array of InputTemplate metadata")
        inputs = tuple(self.inputs)
        if not all(isinstance(entry, InputTemplate) for entry in inputs):
            raise PlanningError("inputs must be an array of InputTemplate metadata")
        object.__setattr__(self, "inputs", inputs)


@dataclass(frozen=True)
class MatrixPlanRequest:
    """A pinned plan template plus one validated Cartesian parameter matrix."""

    template: PlanRequest
    parameters: Mapping[str, Sequence[object]]
    combinations: tuple[dict[str, JsonScalar], ...] = field(init=False, repr=False)

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
    inputs: tuple[RenderedInput, ...]


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
    _publish(prepared.destination, prepared.configuration, prepared.status, resolved.patch, prepared.inputs)
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
            _publish(
                experiment.destination, experiment.configuration, experiment.status, resolved.patch, experiment.inputs
            )
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
    parameters: Mapping[str, JsonScalar],
    timestamp: str,
) -> _PreparedExperiment:
    """Build and validate immutable plan data without publishing files."""
    layout = ExperimentDirectory(destination)
    input_placeholders: dict[str, object] = dict(parameters)
    input_placeholders["ARTIFACT_DIR"] = str(layout.artifacts)
    input_placeholders["INPUT_DIR"] = str(layout.inputs)
    command_placeholders = {key: parameter_text(value) for key, value in parameters.items()}
    command_placeholders["ARTIFACT_DIR"] = str(layout.artifacts)
    command_placeholders["INPUT_DIR"] = str(layout.inputs)
    try:
        rendered_command = request.command.render_placeholders(command_placeholders)
        rendered_inputs = render_input_templates(request.inputs, input_placeholders) if request.inputs else ()
    except InputRenderingError as error:
        raise PlanningError(str(error)) from error
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
    return _PreparedExperiment(destination, configuration, status, rendered_inputs)


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
    inputs: Sequence[RenderedInput],
) -> None:
    temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    layout = ExperimentDirectory(temporary)
    try:
        layout.save_configuration(configuration)
        layout.save_status(status)
        layout.artifacts.mkdir()
        if inputs:
            _publish_inputs(layout, inputs)
        _write_command_file(layout.command_file, configuration.command)
        if patch:
            layout.git_patch_file.write_bytes(patch)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _publish_inputs(layout: ExperimentDirectory, inputs: Sequence[RenderedInput]) -> None:
    """Write an already-rendered immutable input tree into an unpublished plan."""
    layout.inputs.mkdir()
    entries: list[PlannedInput] = []
    for rendered in inputs:
        destination = layout.input_file(rendered.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(rendered.content)
        entries.append(PlannedInput(path=rendered.path, kind=rendered.kind, sha256=rendered.sha256))
    layout.save_input_manifest(PlannedInputManifest(entries=tuple(entries)))


def _write_command_file(path: Path, command: ExperimentCommand) -> None:
    command_text = command.script if command.mode == "shell" else shlex.join(command.arguments)
    path.write_text(f"#!/bin/sh\n{command_text}\n", encoding="utf-8")
    path.chmod(0o755)


def _slug(value: str, fallback: str) -> str:
    slug = _SLUG_PATTERN.sub("-", value.strip()).strip("-._")
    return (slug or fallback)[:64]
