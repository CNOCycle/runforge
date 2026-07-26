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
from runforge.planning.directory_source import (
    DirectorySourceResolutionError,
    resolve_verified_directory_source,
)
from runforge.planning.inputs import InputRenderingError, InputTemplate, RenderedInput, render_input_templates
from runforge.planning.matrix import JsonScalar, MatrixError, expand_matrix, parameter_text
from runforge.planning.source import (
    ResolvedGitSource,
    SourceResolutionError,
    resolve_current_git_source,
    resolve_pinned_git_source,
)
from runforge.schemas.directory_source import DirectorySourceManifest, VerifiedDirectorySource
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.inputs import PlannedInput, PlannedInputManifest
from runforge.schemas.source import GitSource, PinnedGitSource


_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_COMMIT_PREFIX_LENGTH = 8
_DEFAULT_OUTPUT_ROOT = Path("reports")
_DIRECTORY_SOURCE_MODES = frozenset({"verified-directory"})
_DIRECTORY_SOURCE_BANDS = {"verified-directory": "verified"}


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
    directory_source_mode: str | None = None
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
        self._validate_directory_source_mode()
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

    def _validate_directory_source_mode(self) -> None:
        if self.directory_source_mode is None:
            return
        if self.directory_source_mode not in _DIRECTORY_SOURCE_MODES:
            choices = ", ".join(sorted(_DIRECTORY_SOURCE_MODES))
            raise PlanningError(f"directory_source_mode must be one of: {choices}")
        if self.source is not None:
            raise PlanningError("directory_source_mode and a pinned Git source are mutually exclusive")
        if self.output_root is None:
            raise PlanningError(f"output_root is required when directory_source_mode is {self.directory_source_mode!r}")


@dataclass(frozen=True)
class MatrixPlanRequest:
    """A plan template plus one validated Cartesian parameter matrix, sharing one resolved source."""

    template: PlanRequest
    parameters: Mapping[str, Sequence[object]]
    combinations: tuple[dict[str, JsonScalar], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.template, PlanRequest):
            raise PlanningError("template must be PlanRequest metadata")
        if self.template.directory_source_mode is not None:
            raise PlanningError("Matrix planning does not yet support directory_source_mode")
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
    """Create one self-contained Git-backed or non-Git experiment plan."""
    if request.directory_source_mode == "verified-directory":
        return _plan_verified_directory_experiment(request)
    return _plan_resolved_experiment(request, _resolve_source(request))


def plan_matrix(request: MatrixPlanRequest) -> tuple[Path, ...]:
    """Create one experiment plan per parameter combination, sharing one resolved source."""
    resolved = _resolve_source(request.template)
    created = _plan_resolved_matrix(request, resolved)
    _warn_untracked(resolved.source.untracked_files)
    return created


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
    prepared = _prepare_experiment(request, source, destination, {}, utc_now())
    _publish(prepared, patch=resolved.patch)
    _warn_untracked(source.untracked_files)
    return destination


def _plan_verified_directory_experiment(request: PlanRequest) -> Path:
    """Publish one self-contained verified-directory plan without copying source bytes."""
    try:
        resolved = resolve_verified_directory_source(request.source_path)
    except DirectorySourceResolutionError as error:
        raise PlanningError(str(error)) from error
    source = resolved.source
    if request.output_root is None:
        raise PlanningError("output_root is required for verified-directory plans")
    output_root = request.output_root.expanduser().resolve()
    _require_output_root_outside_source(output_root, source.path)
    band = _DIRECTORY_SOURCE_BANDS[request.directory_source_mode]
    destination = _destination(output_root, band, source.tree_digest, request.name)
    prepared = _prepare_experiment(request, source, destination, {}, utc_now())
    _publish(prepared, directory_manifest=resolved.manifest)
    return destination


def _require_output_root_outside_source(output_root: Path, source_path: Path) -> None:
    if output_root == source_path or source_path in output_root.parents:
        raise PlanningError("output_root must resolve outside the source directory")


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
        prepared.append(_prepare_experiment(template, source, destination, parameters, timestamp))
    created: list[Path] = []
    try:
        for experiment in prepared:
            _publish(experiment, patch=resolved.patch)
            created.append(experiment.destination)
    except Exception:
        for destination in created:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return tuple(experiment.destination for experiment in prepared)


def _prepare_experiment(
    request: PlanRequest,
    source: GitSource | VerifiedDirectorySource,
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
        source=source,
        created_at=timestamp,
        parameters=parameters,
    )
    status = ExperimentStatus(state="created", attempt=0, updated_at=timestamp)
    return _PreparedExperiment(destination, configuration, status, rendered_inputs)


def _destination(
    output_root: Path,
    band: str,
    identity: str,
    name: str,
    reserved: set[Path] | None = None,
) -> Path:
    parent = output_root.expanduser().resolve() / _slug(band, "band")
    parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{identity[:_COMMIT_PREFIX_LENGTH]}_{_slug(name, 'experiment')}"
    index = 0
    while True:
        destination = parent / f"{prefix}_{index:04d}"
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
    prepared: _PreparedExperiment,
    *,
    patch: bytes | None = None,
    directory_manifest: DirectorySourceManifest | None = None,
) -> None:
    destination = prepared.destination
    temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    layout = ExperimentDirectory(temporary)
    try:
        layout.save_configuration(prepared.configuration)
        layout.save_status(prepared.status)
        layout.artifacts.mkdir()
        if prepared.inputs:
            _publish_inputs(layout, prepared.inputs)
        _write_command_file(layout.command_file, prepared.configuration.command)
        if patch:
            layout.git_patch_file.write_bytes(patch)
        if directory_manifest is not None:
            layout.save_directory_source_manifest(directory_manifest)
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
