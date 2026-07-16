"""Translate parsed CLI arguments into planner requests."""

from __future__ import annotations

import argparse
from pathlib import Path

from runforge.infrastructure.json_store import load_json_object
from runforge.models.experiment import ExperimentCommand
from runforge.models.source import PinnedGitSource
from runforge.planning.planner import MatrixPlanRequest, PlanningError, PlanRequest


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
        environment=_environment(arguments.env_file),
    )


def _pinned_source(arguments: argparse.Namespace) -> PinnedGitSource | None:
    if arguments.source_mode == "current-head":
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
