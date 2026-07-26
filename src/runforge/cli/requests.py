"""Translate parsed CLI arguments into planner requests."""

from __future__ import annotations

import argparse
from pathlib import Path

from runforge.infrastructure.json_store import load_json_object
from runforge.planning.inputs import InputTemplate
from runforge.planning.planner import MatrixPlanRequest, PlanningError, PlanRequest
from runforge.schemas.experiment import ExperimentCommand
from runforge.schemas.source import PinnedGitSource


def matrix_request(arguments: argparse.Namespace) -> MatrixPlanRequest:
    """Build one validated matrix-planning request."""
    try:
        parameters = load_json_object(arguments.matrix_file)
    except ValueError as error:
        raise PlanningError(str(error)) from error
    return MatrixPlanRequest(template=planning_request(arguments), parameters=parameters)


def planning_request(arguments: argparse.Namespace) -> PlanRequest:
    """Build one validated plan or launch request."""
    parts = list(arguments.command)
    if parts and parts[0] == "--":
        parts.pop(0)
    if not parts:
        raise PlanningError("No command provided; place it after --")
    if arguments.shell:
        if len(parts) != 1:
            raise PlanningError("--shell requires one quoted command string after --")
        command = ExperimentCommand.shell(parts[0])
    else:
        command = ExperimentCommand.argv(parts)
    return PlanRequest(
        name=arguments.name,
        command=command,
        output_root=arguments.out_dir,
        source_path=arguments.source_path,
        source=_pinned_source(arguments),
        directory_source_mode=_directory_source_mode(arguments),
        environment=_environment(arguments.env_file),
        inputs=_input_templates(arguments.input_tree),
    )


def _pinned_source(arguments: argparse.Namespace) -> PinnedGitSource | None:
    if arguments.source_mode != "pinned-git":
        if arguments.commit is not None or arguments.patch is not None:
            raise PlanningError("--commit and --patch require --source-mode pinned-git")
        return None
    if arguments.commit is None:
        raise PlanningError("--source-mode pinned-git requires --commit")
    return PinnedGitSource(
        repository=arguments.source_path,
        commit=arguments.commit,
        patch=arguments.patch,
    )


def _directory_source_mode(arguments: argparse.Namespace) -> str | None:
    if arguments.source_mode in {"verified-directory", "directory-snapshot"}:
        return arguments.source_mode
    return None


def _environment(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PlanningError(f"Could not read environment file {path}: {error}") from error
    environment: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PlanningError(f"Invalid environment entry at {path}:{number}; expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise PlanningError(f"Empty environment key at {path}:{number}")
        environment[key] = value.strip().strip("'").strip('"')
    return environment


def _input_templates(root: Path | None) -> tuple[InputTemplate, ...]:
    """Capture one safe UTF-8 configuration tree for immutable plan publication."""
    if root is None:
        return ()
    root = root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise PlanningError(f"Input tree must be a non-symlink directory: {root}")
    templates: list[InputTemplate] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PlanningError(f"Input tree must not contain symbolic links: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PlanningError(f"Input tree contains a non-regular file: {relative}")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PlanningError(f"Could not read UTF-8 input file {path}: {error}") from error
        suffix = path.suffix.lower()
        kind = (
            "json-template" if suffix == ".json" else "text-template" if suffix in {".yaml", ".yml", ".ini"} else "copy"
        )
        templates.append(InputTemplate(path=relative, kind=kind, content=content))
    if not templates:
        raise PlanningError(f"Input tree must contain at least one regular file: {root}")
    return tuple(templates)
