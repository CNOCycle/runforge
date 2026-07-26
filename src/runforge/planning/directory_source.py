"""Normalize non-Git directory source requests before experiment publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runforge.infrastructure.directory_scan import DirectoryScanError, ScannedFile, scan_directory
from runforge.schemas.directory_source import DirectorySourceEntry, DirectorySourceManifest, VerifiedDirectorySource


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
