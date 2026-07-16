"""Caller-facing and normalized Git source metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runforge.models.validation import require_exact_fields, require_object, require_text


SOURCE_SCHEMA_VERSION = 1
GIT_PATCH_FILE = "git.patch"
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SourceMetadataError(ValueError):
    """Raised when Git source metadata is invalid or cannot be decoded."""


@dataclass(frozen=True)
class PinnedGitSource:
    """An explicit repository, commit/ref, and optional patch supplied by a caller."""

    repository: Path
    commit: str
    patch: Path | None = None

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser().resolve()
        object.__setattr__(self, "repository", repository)
        require_text(self.commit, "commit", SourceMetadataError)
        if self.patch is not None:
            object.__setattr__(self, "patch", Path(self.patch).expanduser().resolve())

    def to_dict(self) -> dict[str, Any]:
        """Return the documented caller-facing pinned-source descriptor."""
        return {
            "mode": "pinned-git",
            "repository": str(self.repository),
            "commit": self.commit,
            "patch": str(self.patch) if self.patch is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Any) -> PinnedGitSource:
        """Decode one exact pinned-source descriptor."""
        data = require_object(value, "pinned Git source", SourceMetadataError)
        require_exact_fields(
            data,
            {"mode", "repository", "commit", "patch"},
            "pinned Git source",
            SourceMetadataError,
        )
        if data["mode"] != "pinned-git":
            raise SourceMetadataError("Pinned Git source mode must be pinned-git")
        patch = data["patch"]
        if patch is not None:
            patch = Path(require_text(patch, "patch", SourceMetadataError))
        return cls(
            repository=Path(require_text(data["repository"], "repository", SourceMetadataError)),
            commit=require_text(data["commit"], "commit", SourceMetadataError),
            patch=patch,
        )


@dataclass(frozen=True)
class GitSource:
    """Normalized Git identity persisted independently from experiment details."""

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
        require_text(self.branch, "branch", SourceMetadataError)
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
        data = require_object(value, "Git source metadata", SourceMetadataError)
        require_exact_fields(
            data,
            {"kind", "schema_version", "repository", "commit", "branch", "patch", "untracked_files"},
            "Git source",
            SourceMetadataError,
        )
        if data["kind"] != "runforge_git_source":
            raise SourceMetadataError("Unsupported Git source kind")
        if data["schema_version"] != SOURCE_SCHEMA_VERSION:
            raise SourceMetadataError(f"Unsupported Git source schema version: {data['schema_version']!r}")
        patch_file: str | None = None
        patch_sha256: str | None = None
        patch = data["patch"]
        if patch is not None:
            patch_data = require_object(patch, "patch", SourceMetadataError)
            require_exact_fields(patch_data, {"file", "sha256"}, "patch", SourceMetadataError)
            patch_file = require_text(patch_data["file"], "patch.file", SourceMetadataError)
            patch_sha256 = require_text(patch_data["sha256"], "patch.sha256", SourceMetadataError)
        untracked = data["untracked_files"]
        if not isinstance(untracked, list):
            raise SourceMetadataError("untracked_files must be an array")
        return cls(
            repository=Path(require_text(data["repository"], "repository", SourceMetadataError)),
            commit=require_text(data["commit"], "commit", SourceMetadataError),
            branch=require_text(data["branch"], "branch", SourceMetadataError),
            patch_file=patch_file,
            patch_sha256=patch_sha256,
            untracked_files=tuple(untracked),
        )
