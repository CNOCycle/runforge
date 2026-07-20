"""Versioned immutable planned-input metadata."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from runforge.schemas.validation import require_exact_fields, require_object, require_text


INPUT_MANIFEST_SCHEMA_VERSION = 1
INPUT_MANIFEST_FILE = "input-manifest.json"
INPUTS_DIRECTORY = "inputs"
_INPUT_KINDS = frozenset({"copy", "text-template"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PlannedInputError(ValueError):
    """Raised when planned input metadata is invalid or cannot be decoded."""


def require_safe_relative_input_path(value: Any, context: str = "input path") -> str:
    """Return one normalized portable relative path below an input directory."""
    path = require_text(value, context, PlannedInputError)
    if "\\x00" in path or "\\" in path:
        raise PlannedInputError(f"{context} must use a safe relative POSIX path")
    posix_path = PurePosixPath(path)
    if (
        posix_path.is_absolute()
        or PureWindowsPath(path).is_absolute()
        or PureWindowsPath(path).drive
        or path != posix_path.as_posix()
        or any(part in {".", ".."} for part in posix_path.parts)
    ):
        raise PlannedInputError(f"{context} must use a safe relative POSIX path")
    return path


@dataclass(frozen=True)
class PlannedInput:
    """One immutable file published below an experiment's input directory."""

    path: str
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", require_safe_relative_input_path(self.path, "input.path"))
        if self.kind not in _INPUT_KINDS:
            choices = ", ".join(sorted(_INPUT_KINDS))
            raise PlannedInputError(f"input.kind must be one of: {choices}")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise PlannedInputError("input.sha256 must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, str]:
        """Return the persisted representation of one input file."""
        return {"path": self.path, "kind": self.kind, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: Any) -> PlannedInput:
        """Decode one exact planned-input entry."""
        data = require_object(value, "input", PlannedInputError)
        require_exact_fields(data, {"path", "kind", "sha256"}, "input", PlannedInputError)
        return cls(
            path=require_text(data["path"], "input.path", PlannedInputError),
            kind=require_text(data["kind"], "input.kind", PlannedInputError),
            sha256=require_text(data["sha256"], "input.sha256", PlannedInputError),
        )


@dataclass(frozen=True)
class PlannedInputManifest:
    """Ordered digests and rendering kinds for one immutable input tree."""

    entries: tuple[PlannedInput, ...]

    def __post_init__(self) -> None:
        if isinstance(self.entries, (str, bytes)) or not isinstance(self.entries, Sequence):
            raise PlannedInputError("input manifest entries must be an array")
        entries = tuple(self.entries)
        if not entries:
            raise PlannedInputError("input manifest entries must not be empty")
        if not all(isinstance(entry, PlannedInput) for entry in entries):
            raise PlannedInputError("input manifest entries must be PlannedInput metadata")
        paths = tuple(entry.path for entry in entries)
        if len(set(paths)) != len(paths):
            raise PlannedInputError("input manifest paths must not contain duplicates")
        if paths != tuple(sorted(paths)):
            raise PlannedInputError("input manifest paths must be sorted")
        object.__setattr__(self, "entries", entries)

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned immutable input-manifest object."""
        return {
            "kind": "runforge_planned_input_manifest",
            "schema_version": INPUT_MANIFEST_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, value: Any) -> PlannedInputManifest:
        """Decode one exact supported input manifest."""
        data = require_object(value, "input manifest", PlannedInputError)
        require_exact_fields(
            data,
            {"kind", "schema_version", "entries"},
            "input manifest",
            PlannedInputError,
        )
        if data["kind"] != "runforge_planned_input_manifest":
            raise PlannedInputError("Unsupported input manifest kind")
        if data["schema_version"] != INPUT_MANIFEST_SCHEMA_VERSION:
            raise PlannedInputError(f"Unsupported input manifest schema version: {data['schema_version']!r}")
        entries = data["entries"]
        if not isinstance(entries, list):
            raise PlannedInputError("input manifest entries must be an array")
        return cls(entries=tuple(PlannedInput.from_dict(entry) for entry in entries))
