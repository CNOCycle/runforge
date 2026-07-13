"""Versioned current-HEAD Git source metadata for planned experiments."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SOURCE_SCHEMA_VERSION = 1
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SourceMetadataError(ValueError):
    """Raised when Git source metadata is invalid or cannot be decoded."""


def _non_empty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceMetadataError(f"{context} must be a non-empty string")
    return value


def _exact_fields(data: Mapping[str, Any], expected: set[str]) -> None:
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown:
        raise SourceMetadataError(f"Unknown Git source field(s): {', '.join(unknown)}")
    if missing:
        raise SourceMetadataError(f"Missing Git source field(s): {', '.join(missing)}")


@dataclass(frozen=True)
class GitSource:
    """Current-HEAD Git identity persisted independently from experiment details."""

    repository: Path
    commit: str
    branch: str
    patch_file: str | None = None
    patch_sha256: str | None = None
    untracked_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser()
        if not repository.is_absolute():
            raise SourceMetadataError("repository must be an absolute path")
        object.__setattr__(self, "repository", repository)
        if not _COMMIT_PATTERN.fullmatch(self.commit):
            raise SourceMetadataError("commit must be a full Git object ID")
        _non_empty_string(self.branch, "branch")
        if (self.patch_file is None) != (self.patch_sha256 is None):
            raise SourceMetadataError("patch_file and patch_sha256 must be supplied together")
        if self.patch_file is not None:
            # The worker joins this value to the experiment directory. Requiring a
            # basename keeps the captured patch inside that self-contained directory.
            if Path(self.patch_file).name != self.patch_file:
                raise SourceMetadataError("patch_file must be a filename without path components")
            if not _SHA256_PATTERN.fullmatch(self.patch_sha256 or ""):
                raise SourceMetadataError("patch_sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.untracked_files, str):
            raise SourceMetadataError("untracked_files must be an array")
        untracked = tuple(self.untracked_files)
        if not all(isinstance(path, str) and path for path in untracked):
            raise SourceMetadataError("untracked_files must contain non-empty strings")
        if len(set(untracked)) != len(untracked):
            raise SourceMetadataError("untracked_files must not contain duplicates")
        object.__setattr__(self, "untracked_files", untracked)

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned JSON object representing this source state."""
        patch = None
        if self.patch_file is not None:
            patch = {"file": self.patch_file, "sha256": self.patch_sha256}
        return {
            # This is single-valued today, but distinguishes Git metadata from
            # future source representations independently of their schema versions.
            "kind": "runforge_git_source",
            "schema_version": SOURCE_SCHEMA_VERSION,
            "repository": str(self.repository),
            "commit": self.commit,
            "branch": self.branch,
            "patch": patch,
            "untracked_files": list(self.untracked_files),
        }

    @classmethod
    def from_dict(cls, value: Any) -> GitSource:
        """Decode one exact supported version of source metadata."""
        if not isinstance(value, Mapping):
            raise SourceMetadataError("Git source metadata must be a JSON object")
        data = value
        _exact_fields(
            data,
            {"kind", "schema_version", "repository", "commit", "branch", "patch", "untracked_files"},
        )
        if data["kind"] != "runforge_git_source":
            raise SourceMetadataError("Unsupported Git source kind")
        if data["schema_version"] != SOURCE_SCHEMA_VERSION:
            raise SourceMetadataError(f"Unsupported Git source schema version: {data['schema_version']!r}")
        patch_file: str | None = None
        patch_sha256: str | None = None
        patch = data["patch"]
        if patch is not None:
            if not isinstance(patch, Mapping):
                raise SourceMetadataError("patch must be a JSON object or null")
            unknown = sorted(set(patch) - {"file", "sha256"})
            missing = sorted({"file", "sha256"} - set(patch))
            if unknown:
                raise SourceMetadataError(f"Unknown patch field(s): {', '.join(unknown)}")
            if missing:
                raise SourceMetadataError(f"Missing patch field(s): {', '.join(missing)}")
            patch_file = _non_empty_string(patch["file"], "patch.file")
            patch_sha256 = _non_empty_string(patch["sha256"], "patch.sha256")
        untracked = data["untracked_files"]
        if not isinstance(untracked, list):
            raise SourceMetadataError("untracked_files must be an array")
        return cls(
            repository=Path(_non_empty_string(data["repository"], "repository")),
            commit=_non_empty_string(data["commit"], "commit"),
            branch=_non_empty_string(data["branch"], "branch"),
            patch_file=patch_file,
            patch_sha256=patch_sha256,
            untracked_files=tuple(untracked),
        )
