"""Versioned command, experiment configuration, and status schemas."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from runforge.schemas.source import GitSource
from runforge.schemas.validation import require_exact_fields, require_object, require_string_mapping, require_text


_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")
EXPERIMENT_SCHEMA_VERSION = 1
_CONFIGURATION_SCHEMA_VERSION = 2
_STATUS_STATES = frozenset({"created", "init", "inprogress", "completed", "failed"})


class ExperimentSchemaError(ValueError):
    """Raised when experiment command, configuration, or status data is invalid."""


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
            require_text(self.script, "command.script", ExperimentSchemaError)
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
        data = require_object(value, "command", ExperimentSchemaError)
        mode = data.get("mode")
        if mode == "argv":
            require_exact_fields(data, {"mode", "arguments"}, "command", ExperimentSchemaError)
            arguments = data["arguments"]
            if not isinstance(arguments, list):
                raise ExperimentSchemaError("command.arguments must be an array")
            return cls.argv(arguments)
        if mode == "shell":
            require_exact_fields(data, {"mode", "script"}, "command", ExperimentSchemaError)
            return cls.shell(require_text(data["script"], "command.script", ExperimentSchemaError))
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
    parameters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.experiment_id, "experiment_id", ExperimentSchemaError)
        require_text(self.name, "name", ExperimentSchemaError)
        if not isinstance(self.command, ExperimentCommand):
            raise ExperimentSchemaError("command must be ExperimentCommand metadata")
        environment = require_string_mapping(self.environment, "environment", ExperimentSchemaError)
        object.__setattr__(self, "environment", environment)
        if not isinstance(self.source, GitSource):
            raise ExperimentSchemaError("source must be GitSource metadata")
        require_text(self.created_at, "created_at", ExperimentSchemaError)
        parameters = require_string_mapping(self.parameters, "parameters", ExperimentSchemaError)
        object.__setattr__(self, "parameters", parameters)

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned immutable configuration object."""
        return {
            "kind": "runforge_experiment_configuration",
            "schema_version": _CONFIGURATION_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "name": self.name,
            "command": self.command.to_dict(),
            "environment": dict(self.environment),
            "source": self.source.to_dict(),
            "created_at": self.created_at,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, value: Any) -> ExperimentConfiguration:
        """Decode one exact supported configuration object."""
        data = require_object(value, "configuration", ExperimentSchemaError)
        schema_version = data.get("schema_version")
        if schema_version not in {EXPERIMENT_SCHEMA_VERSION, _CONFIGURATION_SCHEMA_VERSION}:
            raise ExperimentSchemaError(f"Unsupported configuration schema version: {schema_version!r}")
        fields = {"kind", "schema_version", "experiment_id", "name", "command", "environment", "source", "created_at"}
        if schema_version == _CONFIGURATION_SCHEMA_VERSION:
            fields.add("parameters")
        require_exact_fields(
            data,
            fields,
            "configuration",
            ExperimentSchemaError,
        )
        if data["kind"] != "runforge_experiment_configuration":
            raise ExperimentSchemaError("Unsupported configuration kind")
        return cls(
            experiment_id=require_text(data["experiment_id"], "experiment_id", ExperimentSchemaError),
            name=require_text(data["name"], "name", ExperimentSchemaError),
            command=ExperimentCommand.from_dict(data["command"]),
            environment=require_string_mapping(data["environment"], "environment", ExperimentSchemaError),
            source=GitSource.from_dict(data["source"]),
            created_at=require_text(data["created_at"], "created_at", ExperimentSchemaError),
            parameters=(
                require_string_mapping(data["parameters"], "parameters", ExperimentSchemaError)
                if schema_version == _CONFIGURATION_SCHEMA_VERSION
                else {}
            ),
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
        require_text(self.updated_at, "status.updated_at", ExperimentSchemaError)
        for context, value in (("status.started_at", self.started_at), ("status.finished_at", self.finished_at)):
            if value is not None:
                require_text(value, context, ExperimentSchemaError)
        if self.exit_code is not None and (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)):
            raise ExperimentSchemaError("status.exit_code must be an integer or null")
        if self.error is not None:
            require_text(self.error, "status.error", ExperimentSchemaError)

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
        data = require_object(value, "status", ExperimentSchemaError)
        require_exact_fields(
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
            ExperimentSchemaError,
        )
        if data["kind"] != "runforge_experiment_status":
            raise ExperimentSchemaError("Unsupported status kind")
        if data["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
            raise ExperimentSchemaError(f"Unsupported status schema version: {data['schema_version']!r}")
        return cls(
            state=require_text(data["state"], "status.state", ExperimentSchemaError),
            attempt=data["attempt"],
            updated_at=require_text(data["updated_at"], "status.updated_at", ExperimentSchemaError),
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            exit_code=data["exit_code"],
            error=data["error"],
        )
