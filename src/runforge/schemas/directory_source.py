"""Versioned non-Git directory source identity and manifest schemas."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from runforge.schemas.validation import require_exact_fields, require_object, require_text


DIRECTORY_SOURCE_MANIFEST_SCHEMA_VERSION = 1
DIRECTORY_SOURCE_MANIFEST_FILE = "source-manifest.json"
DIRECTORY_SNAPSHOT_DIRECTORY = "source"
VERIFIED_DIRECTORY_SOURCE_KIND = "runforge_verified_directory_source"
VERIFIED_DIRECTORY_SOURCE_SCHEMA_VERSION = 1
DIRECTORY_SNAPSHOT_SOURCE_KIND = "runforge_directory_snapshot_source"
DIRECTORY_SNAPSHOT_SOURCE_SCHEMA_VERSION = 1

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DirectorySourceError(ValueError):
    """Raised when non-Git directory source metadata is invalid or cannot be decoded."""


def require_safe_relative_source_path(value: Any, context: str = "source path") -> str:
    """Return one normalized portable relative POSIX path below a source root."""
    path = require_text(value, context, DirectorySourceError)
    if "\\" in path:
        raise DirectorySourceError(f"{context} must use a safe relative POSIX path")
    posix_path = PurePosixPath(path)
    if (
        posix_path.is_absolute()
        or PureWindowsPath(path).is_absolute()
        or PureWindowsPath(path).drive
        or path != posix_path.as_posix()
        or any(part in {".", ".."} for part in posix_path.parts)
    ):
        raise DirectorySourceError(f"{context} must use a safe relative POSIX path")
    return path


@dataclass(frozen=True)
class DirectorySourceEntry:
    """One regular file recorded in an ordered non-Git source manifest."""

    path: str
    executable: bool
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", require_safe_relative_source_path(self.path, "manifest entry path"))
        if not isinstance(self.executable, bool):
            raise DirectorySourceError("manifest entry executable must be a boolean")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise DirectorySourceError("manifest entry sha256 must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted representation of one source manifest entry."""
        return {"path": self.path, "executable": self.executable, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: Any) -> DirectorySourceEntry:
        """Decode one exact source manifest entry."""
        data = require_object(value, "manifest entry", DirectorySourceError)
        require_exact_fields(data, {"path", "executable", "sha256"}, "manifest entry", DirectorySourceError)
        executable = data["executable"]
        if not isinstance(executable, bool):
            raise DirectorySourceError("manifest entry executable must be a boolean")
        return cls(
            path=require_text(data["path"], "manifest entry path", DirectorySourceError),
            executable=executable,
            sha256=require_text(data["sha256"], "manifest entry sha256", DirectorySourceError),
        )


@dataclass(frozen=True)
class DirectorySourceManifest:
    """Ordered file entries and full-tree digest for one non-Git source capture."""

    entries: tuple[DirectorySourceEntry, ...]
    tree_digest: str

    def __post_init__(self) -> None:
        if isinstance(self.entries, (str, bytes)) or not isinstance(self.entries, Sequence):
            raise DirectorySourceError("source manifest entries must be an array")
        entries = tuple(self.entries)
        if not all(isinstance(entry, DirectorySourceEntry) for entry in entries):
            raise DirectorySourceError("source manifest entries must be DirectorySourceEntry metadata")
        paths = tuple(entry.path for entry in entries)
        if len(set(paths)) != len(paths):
            raise DirectorySourceError("source manifest paths must not contain duplicates")
        if paths != tuple(sorted(paths)):
            raise DirectorySourceError("source manifest paths must be sorted")
        object.__setattr__(self, "entries", entries)
        if not _SHA256_PATTERN.fullmatch(self.tree_digest):
            raise DirectorySourceError("source manifest tree_digest must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned immutable source manifest object."""
        return {
            "kind": "runforge_directory_source_manifest",
            "schema_version": DIRECTORY_SOURCE_MANIFEST_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
            "tree_digest": self.tree_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> DirectorySourceManifest:
        """Decode one exact supported source manifest."""
        data = require_object(value, "source manifest", DirectorySourceError)
        require_exact_fields(
            data,
            {"kind", "schema_version", "entries", "tree_digest"},
            "source manifest",
            DirectorySourceError,
        )
        if data["kind"] != "runforge_directory_source_manifest":
            raise DirectorySourceError("Unsupported source manifest kind")
        if data["schema_version"] != DIRECTORY_SOURCE_MANIFEST_SCHEMA_VERSION:
            raise DirectorySourceError(f"Unsupported source manifest schema version: {data['schema_version']!r}")
        entries = data["entries"]
        if not isinstance(entries, list):
            raise DirectorySourceError("source manifest entries must be an array")
        return cls(
            entries=tuple(DirectorySourceEntry.from_dict(entry) for entry in entries),
            tree_digest=require_text(data["tree_digest"], "source manifest tree_digest", DirectorySourceError),
        )


@dataclass(frozen=True)
class VerifiedDirectorySource:
    """Normalized identity for a live, path-backed non-Git source directory.

    No source bytes are stored in the experiment directory; the worker
    re-verifies the recorded absolute path before every execution.
    """

    path: Path
    tree_digest: str

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise DirectorySourceError("path must be an absolute path")
        object.__setattr__(self, "path", path)
        if not _SHA256_PATTERN.fullmatch(self.tree_digest):
            raise DirectorySourceError("tree_digest must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned JSON object representing this source state."""
        return {
            "kind": VERIFIED_DIRECTORY_SOURCE_KIND,
            "schema_version": VERIFIED_DIRECTORY_SOURCE_SCHEMA_VERSION,
            "path": str(self.path),
            "tree_digest": self.tree_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> VerifiedDirectorySource:
        """Decode one exact supported version of verified-directory source metadata."""
        data = require_object(value, "verified-directory source", DirectorySourceError)
        require_exact_fields(
            data,
            {"kind", "schema_version", "path", "tree_digest"},
            "verified-directory source",
            DirectorySourceError,
        )
        if data["kind"] != VERIFIED_DIRECTORY_SOURCE_KIND:
            raise DirectorySourceError("Unsupported verified-directory source kind")
        if data["schema_version"] != VERIFIED_DIRECTORY_SOURCE_SCHEMA_VERSION:
            raise DirectorySourceError(
                f"Unsupported verified-directory source schema version: {data['schema_version']!r}"
            )
        return cls(
            path=Path(require_text(data["path"], "path", DirectorySourceError)),
            tree_digest=require_text(data["tree_digest"], "tree_digest", DirectorySourceError),
        )


@dataclass(frozen=True)
class DirectorySnapshotSource:
    """Normalized identity for a self-contained captured directory snapshot.

    ``original_path`` is diagnostic provenance only; the worker never requires
    it to exist, since the snapshot itself is the source of truth.
    """

    original_path: Path
    tree_digest: str

    def __post_init__(self) -> None:
        path = Path(self.original_path).expanduser()
        if not path.is_absolute():
            raise DirectorySourceError("original_path must be an absolute path")
        object.__setattr__(self, "original_path", path)
        if not _SHA256_PATTERN.fullmatch(self.tree_digest):
            raise DirectorySourceError("tree_digest must be a lowercase SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned JSON object representing this source state."""
        return {
            "kind": DIRECTORY_SNAPSHOT_SOURCE_KIND,
            "schema_version": DIRECTORY_SNAPSHOT_SOURCE_SCHEMA_VERSION,
            "original_path": str(self.original_path),
            "tree_digest": self.tree_digest,
        }

    @classmethod
    def from_dict(cls, value: Any) -> DirectorySnapshotSource:
        """Decode one exact supported version of directory-snapshot source metadata."""
        data = require_object(value, "directory-snapshot source", DirectorySourceError)
        require_exact_fields(
            data,
            {"kind", "schema_version", "original_path", "tree_digest"},
            "directory-snapshot source",
            DirectorySourceError,
        )
        if data["kind"] != DIRECTORY_SNAPSHOT_SOURCE_KIND:
            raise DirectorySourceError("Unsupported directory-snapshot source kind")
        if data["schema_version"] != DIRECTORY_SNAPSHOT_SOURCE_SCHEMA_VERSION:
            raise DirectorySourceError(
                f"Unsupported directory-snapshot source schema version: {data['schema_version']!r}"
            )
        return cls(
            original_path=Path(require_text(data["original_path"], "original_path", DirectorySourceError)),
            tree_digest=require_text(data["tree_digest"], "tree_digest", DirectorySourceError),
        )
