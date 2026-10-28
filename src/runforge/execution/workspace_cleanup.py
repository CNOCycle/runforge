"""Read-only discovery and explicitly-confirmed removal of leftover worker workspaces.

A killed worker leaves its workspace behind; nothing recovers it
automatically. This module only ever acts on a workspace an operator names
explicitly after reviewing what discovery found -- it never selects a
workspace for removal by itself, and in particular never by age.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from runforge.execution.errors import WorkerError
from runforge.execution.sources import WORKSPACE_NAME_PREFIX, _configured_workspace_root, load_worker_manifest
from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.infrastructure.json_store import JsonStoreError
from runforge.infrastructure.paths import is_safe_directory
from runforge.schemas.worker_manifest import WorkerManifest, WorkerManifestError


def default_workspace_root() -> Path:
    """Resolve the workspace scan root when the operator gives none explicitly.

    Reuses the same RUNFORGE_WORKSPACE_ROOT resolution the worker itself uses,
    so cleanup scans the one place workspaces actually land when it is
    configured, and falls back to the current directory otherwise -- an
    explicit root argument always overrides this.
    """
    return _configured_workspace_root() or Path(".")


class WorkspaceCleanupError(RuntimeError):
    """Raised when a workspace cleanup root cannot be scanned."""


@dataclass(frozen=True)
class DiscoveredWorkspace:
    """One candidate leftover worker workspace directory.

    ``manifest`` is ``None`` when the workspace predates Stage 5.2, or its
    manifest could not be read; ``manifest_error`` then explains why. Absence
    of a manifest never excludes a workspace from discovery or removal --
    both are still name-driven, not manifest-driven.
    """

    path: Path
    manifest: WorkerManifest | None
    manifest_error: str | None


def discover_workspaces(root: Path) -> tuple[DiscoveredWorkspace, ...]:
    """List every ``runforge-worker-*`` directory directly under root, without changing anything.

    Rejects a symlink candidate the same way every other RunForge path check
    does (``is_safe_directory``): a link makes what actually gets removed
    depend on something outside the workspace itself.
    """
    resolved = _resolve_root(root)
    discovered: list[DiscoveredWorkspace] = []
    for candidate in sorted(resolved.iterdir()):
        if not candidate.name.startswith(WORKSPACE_NAME_PREFIX) or not is_safe_directory(candidate):
            continue
        manifest: WorkerManifest | None = None
        manifest_error: str | None = None
        try:
            manifest = load_worker_manifest(candidate)
        except (JsonStoreError, WorkerManifestError) as error:
            manifest_error = str(error)
        discovered.append(DiscoveredWorkspace(path=candidate, manifest=manifest, manifest_error=manifest_error))
    return tuple(discovered)


def remove_workspace(workspace: Path) -> None:
    """Remove one confirmed worker workspace, deregistering it as a Git worktree if it is one.

    Detects a Git worktree by the presence of ``.git`` inside the workspace
    itself -- ground truth independent of whether its manifest exists or is
    readable -- rather than trusting the manifest's ``source_kind`` alone.
    """
    if (workspace / ".git").exists():
        try:
            repository = GitRepository.locate(workspace)
            repository.remove_worktree(workspace)
            return
        except GitOperationError as error:
            raise WorkerError(f"Could not deregister Git worktree at {workspace}: {error}") from error
    try:
        shutil.rmtree(workspace)
    except OSError as error:
        raise WorkerError(f"Could not remove worker workspace {workspace}: {error}") from error


def _resolve_root(root: Path) -> Path:
    source = Path(root).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError as error:
        raise WorkspaceCleanupError(f"Workspace cleanup root does not exist or cannot be resolved: {source}") from error
    if not resolved.is_dir():
        raise WorkspaceCleanupError(f"Workspace cleanup root is not a directory: {resolved}")
    return resolved
