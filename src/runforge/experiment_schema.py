"""Versioned command, experiment configuration, and status schemas."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from runforge.source_metadata import GitSource


_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")
EXPERIMENT_SCHEMA_VERSION = 1
_STATUS_STATES = frozenset({"created", "init", "inprogress", "completed", "failed"})


class ExperimentSchemaError(ValueError):
    """Raised when experiment command, configuration, or status data is invalid."""


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentSchemaError(f"{context} must be a non-empty string")
    return value


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentSchemaError(f"{context} must be a JSON object")
    return value


def _exact_fields(data: Mapping[str, Any], expected: set[str], context: str) -> None:
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown:
        raise ExperimentSchemaError(f"Unknown {context} field(s): {', '.join(unknown)}")
    if missing:
        raise ExperimentSchemaError(f"Missing {context} field(s): {', '.join(missing)}")


def _arguments(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ExperimentSchemaError("command.arguments must be an array of non-empty strings")
    try:
        arguments = tuple(value)
    except TypeError as error:
        raise ExperimentSchemaError("command.arguments must be an array of non-empty strings") from error
    if not arguments or not all(isinstance(argument, str) and argument for argument in arguments):
        raise ExperimentSchemaError("command.arguments must be an array of non-empty strings")
    return arguments


@dataclass(frozen=True)
class ExperimentCommand:
    """An argument command or explicit shell pipeline template."""

    mode: str
    arguments: tuple[str, ...] = ()
    script: str | None = None

    def __post_init__(self) -> None:
        if self.mode == "argv":
            if self.script is not None:
                raise ExperimentSchemaError("argv command must not define script")
            object.__setattr__(self, "arguments", _arguments(self.arguments))
            return
        if self.mode == "shell":
            if self.arguments:
                raise ExperimentSchemaError("shell command must not define arguments")
            _text(self.script, "command.script")
            return
        raise ExperimentSchemaError("command.mode must be either argv or shell")

    @classmethod
    def argv(cls, arguments: Sequence[str]) -> ExperimentCommand:
        """Create a command that will execute without a shell."""
        return cls(mode="argv", arguments=tuple(arguments))

    @classmethod
    def shell(cls, script: str) -> ExperimentCommand:
        """Create an explicit shell command, which may be a pipeline."""
        return cls(mode="shell", script=script)

    def render_placeholders(self, values: Mapping[str, str]) -> ExperimentCommand:
        """Return a copy with exact ``{KEY}`` placeholders replaced.

        The future planner owns value selection and calls this after allocating
        the artifact directory, for example with ``{"ARTIFACT_DIR": path}``.
        """
        replacements: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise ExperimentSchemaError("placeholder values must map non-empty strings to strings")
            replacements[key] = value

        def render(text: str) -> str:
            return _PLACEHOLDER_PATTERN.sub(lambda match: replacements.get(match.group(1), match.group(0)), text)

        if self.mode == "argv":
            return ExperimentCommand.argv(tuple(render(argument) for argument in self.arguments))
        return ExperimentCommand.shell(render(self.script or ""))

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible command data."""
        if self.mode == "argv":
            return {"mode": "argv", "arguments": list(self.arguments)}
        return {"mode": "shell", "script": self.script}

    @classmethod
    def from_dict(cls, value: Any) -> ExperimentCommand:
        """Decode one supported command representation."""
        data = _object(value, "command")
        mode = data.get("mode")
        if mode == "argv":
            _exact_fields(data, {"mode", "arguments"}, "command")
            arguments = data["arguments"]
            if not isinstance(arguments, list):
                raise ExperimentSchemaError("command.arguments must be an array")
            return cls.argv(arguments)
        if mode == "shell":
            _exact_fields(data, {"mode", "script"}, "command")
            return cls.shell(_text(data["script"], "command.script"))
        raise ExperimentSchemaError("command.mode must be either argv or shell")


@dataclass(frozen=True)
class ExperimentConfiguration:
    """Immutable planned experiment input, separate from mutable status."""

    experiment_id: str
    name: str
    command: ExperimentCommand
    environment: Mapping[str, str]
    source: GitSource
    created_at: str

    def __post_init__(self) -> None:
        _text(self.experiment_id, "experiment_id")
        _text(self.name, "name")
        if not isinstance(self.command, ExperimentCommand):
            raise ExperimentSchemaError("command must be ExperimentCommand metadata")
        environment = dict(self.environment)
        if not all(isinstance(key, str) and key and isinstance(value, str) for key, value in environment.items()):
            raise ExperimentSchemaError("environment must map non-empty strings to strings")
        object.__setattr__(self, "environment", environment)
        if not isinstance(self.source, GitSource):
            raise ExperimentSchemaError("source must be GitSource metadata")
        _text(self.created_at, "created_at")

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned immutable configuration object."""
        return {
            "kind": "runforge_experiment_configuration",
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "name": self.name,
            "command": self.command.to_dict(),
            "environment": dict(self.environment),
            "source": self.source.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ExperimentConfiguration:
        """Decode one exact supported configuration object."""
        data = _object(value, "configuration")
        _exact_fields(
            data,
            {"kind", "schema_version", "experiment_id", "name", "command", "environment", "source", "created_at"},
            "configuration",
        )
        if data["kind"] != "runforge_experiment_configuration":
            raise ExperimentSchemaError("Unsupported configuration kind")
        if data["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
            raise ExperimentSchemaError(f"Unsupported configuration schema version: {data['schema_version']!r}")
        return cls(
            experiment_id=_text(data["experiment_id"], "experiment_id"),
            name=_text(data["name"], "name"),
            command=ExperimentCommand.from_dict(data["command"]),
            environment=dict(_object(data["environment"], "environment")),
            source=GitSource.from_dict(data["source"]),
            created_at=_text(data["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class ExperimentStatus:
    """Mutable lifecycle/result data, stored separately from configuration."""

    state: str
    attempt: int
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _STATUS_STATES:
            raise ExperimentSchemaError(f"Unsupported experiment status state: {self.state!r}")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ExperimentSchemaError("status.attempt must be a non-negative integer")
        _text(self.updated_at, "status.updated_at")
        for context, value in (("status.started_at", self.started_at), ("status.finished_at", self.finished_at)):
            if value is not None:
                _text(value, context)
        if self.exit_code is not None and (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)):
            raise ExperimentSchemaError("status.exit_code must be an integer or null")
        if self.error is not None:
            _text(self.error, "status.error")

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned mutable status object."""
        return {
            "kind": "runforge_experiment_status",
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "state": self.state,
            "attempt": self.attempt,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ExperimentStatus:
        """Decode one exact supported status object."""
        data = _object(value, "status")
        _exact_fields(
            data,
            {
                "kind",
                "schema_version",
                "state",
                "attempt",
                "updated_at",
                "started_at",
                "finished_at",
                "exit_code",
                "error",
            },
            "status",
        )
        if data["kind"] != "runforge_experiment_status":
            raise ExperimentSchemaError("Unsupported status kind")
        if data["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
            raise ExperimentSchemaError(f"Unsupported status schema version: {data['schema_version']!r}")
        return cls(
            state=_text(data["state"], "status.state"),
            attempt=data["attempt"],
            updated_at=_text(data["updated_at"], "status.updated_at"),
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            exit_code=data["exit_code"],
            error=data["error"],
        )
