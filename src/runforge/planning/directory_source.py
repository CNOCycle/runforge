"""Normalize non-Git directory source requests before experiment publication."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from runforge.infrastructure.directory_scan import DirectoryScanError, ScannedFile, capture_directory, scan_directory
from runforge.schemas.directory_source import (
    DIRECTORY_SNAPSHOT_DIRECTORY,
    DirectorySnapshotSource,
    DirectorySourceEntry,
    DirectorySourceManifest,
    VerifiedDirectorySource,
)


class DirectorySourceResolutionError(RuntimeError):
    """Raised when a requested non-Git directory source cannot be normalized."""


@dataclass(frozen=True)
class ResolvedVerifiedDirectorySource:
    """A normalized verified-directory source plus its capture-time manifest."""

    source: VerifiedDirectorySource
    manifest: DirectorySourceManifest


def resolve_verified_directory_source(source_path: Path) -> ResolvedVerifiedDirectorySource:
    """Scan and validate a live, path-backed non-Git source directory."""
    resolved_path = Path(source_path).expanduser()
    if resolved_path.is_symlink() or not resolved_path.is_dir():
        raise DirectorySourceResolutionError(
            f"Verified-directory source must be a non-symlink directory: {resolved_path}"
        )
    try:
        scan = scan_directory(resolved_path)
    except DirectoryScanError as error:
        raise DirectorySourceResolutionError(str(error)) from error
    manifest = _manifest_from_scan(scan.files, scan.tree_digest)
    source = VerifiedDirectorySource(path=scan.root, tree_digest=scan.tree_digest)
    return ResolvedVerifiedDirectorySource(source=source, manifest=manifest)


def _manifest_from_scan(files: tuple[ScannedFile, ...], tree_digest: str) -> DirectorySourceManifest:
    entries = tuple(
        DirectorySourceEntry(path=entry.path, executable=entry.executable, sha256=entry.sha256) for entry in files
    )
    return DirectorySourceManifest(entries=entries, tree_digest=tree_digest)


@dataclass(frozen=True)
class ResolvedDirectorySnapshotSource:
    """A captured directory-snapshot source plus its manifest and staged bytes.

    ``captured_source`` is a temporary directory tree, and ``staging_root`` is
    its owning temporary parent. Publication must move ``captured_source`` into
    the experiment directory; the caller must remove ``staging_root`` in either
    case so no temporary capture is left behind.
    """

    source: DirectorySnapshotSource
    manifest: DirectorySourceManifest
    captured_source: Path
    staging_root: Path


def resolve_directory_snapshot_source(source_path: Path) -> ResolvedDirectorySnapshotSource:
    """Atomically capture a non-Git source directory into a temporary staging tree."""
    resolved_path = Path(source_path).expanduser()
    if resolved_path.is_symlink() or not resolved_path.is_dir():
        raise DirectorySourceResolutionError(
            f"Directory-snapshot source must be a non-symlink directory: {resolved_path}"
        )
    staging_root = Path(tempfile.mkdtemp(prefix="runforge-snapshot-"))
    captured_source = staging_root / DIRECTORY_SNAPSHOT_DIRECTORY
    try:
        scan = capture_directory(resolved_path, captured_source)
    except DirectoryScanError as error:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise DirectorySourceResolutionError(str(error)) from error
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    manifest = _manifest_from_scan(scan.files, scan.tree_digest)
    source = DirectorySnapshotSource(original_path=scan.root, tree_digest=scan.tree_digest)
    return ResolvedDirectorySnapshotSource(
        source=source,
        manifest=manifest,
        captured_source=captured_source,
        staging_root=staging_root,
    )
