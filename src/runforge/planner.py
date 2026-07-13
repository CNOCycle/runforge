"""Current-HEAD single-experiment planning without command execution."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.git_ops import GitOperationError, GitRepository
from runforge.json_store import save_json_object
from runforge.source_metadata import GitSource


_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_COMMIT_PREFIX_LENGTH = 8
_DEFAULT_OUTPUT_ROOT = Path("reports")


class PlanningError(RuntimeError):
    """Raised when one experiment directory cannot be planned safely."""


@dataclass(frozen=True)
class PlanRequest:
    """Input for one current-HEAD experiment plan."""

    name: str
    command: ExperimentCommand
    output_root: Path | None = None
    source_path: Path = Path(".")
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
        environment = dict(self.environment)
        if not all(isinstance(key, str) and key and isinstance(value, str) for key, value in environment.items()):
            raise PlanningError("environment must map non-empty strings to strings")
        object.__setattr__(self, "environment", environment)


def plan_experiment(request: PlanRequest) -> Path:
    """Create one self-contained current-HEAD experiment directory without execution."""
    try:
        repository = GitRepository.discover(request.source_path)
        head = repository.head()
        patch = repository.tracked_patch()
        untracked = repository.untracked_files()
    except GitOperationError as error:
        raise PlanningError(str(error)) from error

    output_root = request.output_root or repository.root / _DEFAULT_OUTPUT_ROOT
    destination = _destination(output_root, head.branch, head.commit, request.name)
    rendered_command = request.command.render_placeholders({"ARTIFACT_DIR": str(destination / "artifacts")})
    patch_sha256 = hashlib.sha256(patch).hexdigest() if patch else None
    source = GitSource(
        repository=repository.root,
        commit=head.commit,
        branch=head.branch,
        patch_file="git.patch" if patch else None,
        patch_sha256=patch_sha256,
        untracked_files=tuple(untracked),
    )
    timestamp = _utc_now()
    configuration = ExperimentConfiguration(
        experiment_id=destination.name,
        name=request.name,
        command=rendered_command,
        environment=request.environment,
        source=source,
        created_at=timestamp,
    )
    status = ExperimentStatus(state="created", attempt=0, updated_at=timestamp)
    _publish(destination, configuration, status, patch)
    if untracked:
        warnings.warn(
            "Planned Git source has untracked files that are not included in git.patch: " + ", ".join(untracked),
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
