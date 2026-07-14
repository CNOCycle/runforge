"""Git-backed single-experiment planning without command execution."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.json_store import save_json_object
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


def plan_experiment(request: PlanRequest) -> Path:
    """Create one self-contained current-HEAD or pinned experiment plan."""
    try:
        if request.source is None:
            resolved = resolve_current_git_source(request.source_path)
        else:
            resolved = resolve_pinned_git_source(request.source)
    except SourceResolutionError as error:
        raise PlanningError(str(error)) from error
    return _plan_resolved_experiment(request, resolved)


def _plan_resolved_experiment(request: PlanRequest, resolved: ResolvedGitSource) -> Path:
    """Publish one plan from source metadata that has already been resolved."""
    source = resolved.source
    output_root = request.output_root or resolved.repository.root / _DEFAULT_OUTPUT_ROOT
    destination = _destination(output_root, source.branch, source.commit, request.name)
    rendered_command = request.command.render_placeholders({"ARTIFACT_DIR": str(destination / "artifacts")})
    timestamp = utc_now()
    configuration = ExperimentConfiguration(
        experiment_id=destination.name,
        name=request.name,
        command=rendered_command,
        environment=request.environment,
        source=source,
        created_at=timestamp,
    )
    status = ExperimentStatus(state="created", attempt=0, updated_at=timestamp)
    _publish(destination, configuration, status, resolved.patch)
    if source.untracked_files:
        warnings.warn(
            "Planned Git source has untracked files that are not included in git.patch:\n"
            + "\n".join(f"  {path}" for path in source.untracked_files),
            stacklevel=2,
        )
    return destination


def _destination(output_root: Path, branch: str, commit: str, name: str) -> Path:
    parent = output_root.expanduser().resolve() / _slug(branch, "detached")
    parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{commit[:_COMMIT_PREFIX_LENGTH]}_{_slug(name, 'experiment')}"
    index = 0
    while True:
        destination = parent / f"{prefix}_{index}"
        if not destination.exists():
            return destination
        index += 1


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
