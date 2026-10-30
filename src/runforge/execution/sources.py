"""Per-mode preparation of the directory an experiment's command runs in.

Each source mode answers the same question differently: given a recorded
source, produce a verified working directory, and clean up afterwards. Git
builds a detached worktree, verified-directory re-checks an external path in
place, and directory-snapshot materializes its captured copy in isolation.

Expressing that as one strategy per mode keeps each mode's setup, verification,
and teardown in one place, and lets the executor prepare a source without
knowing which kind it holds. Adding a mode means adding a class and registering
it, rather than extending a type switch that the executor would otherwise have
to carry.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Generic, Literal, TypeVar

from runforge.execution.errors import WorkerError
from runforge.infrastructure.claims import ExperimentClaim
from runforge.infrastructure.directory_scan import DirectoryScanError, ScannedFile, scan_directory
from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.infrastructure.json_store import JsonStoreError, load_json_object, save_json_object
from runforge.infrastructure.paths import is_safe_directory
from runforge.infrastructure.slug import slugify
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.directory_source import (
    DIRECTORY_SNAPSHOT_DIRECTORY,
    DirectorySnapshotSource,
    DirectorySourceEntry,
    DirectorySourceManifest,
    VerifiedDirectorySource,
)
from runforge.schemas.source import GitSource
from runforge.schemas.worker_manifest import WORKER_MANIFEST_FILE, WorkerManifest


SourceT = TypeVar("SourceT")

WORKSPACE_ROOT_VARIABLE = "RUNFORGE_WORKSPACE_ROOT"


def _configured_workspace_root() -> Path | None:
    """Resolve the operator-configured worker workspace root, if any.

    A blank value falls back to the root beside the recorded source.
    """
    configured = os.environ.get(WORKSPACE_ROOT_VARIABLE, "").strip()
    return Path(configured).expanduser().resolve() if configured else None


def workspace_root_for(source_path: Path) -> Path:
    """Return the configured root or the root beside the recorded source."""
    return _configured_workspace_root() or source_path.parent / "runforge-workspaces"


_WORKSPACE_NAME_ATTEMPTS = 5


def _worker_workspace_name(project: str) -> str:
    """Identify the project and a unique execution in the common root."""
    project_name = slugify(project, "source").rstrip("-._")
    return f"{project_name}-{uuid.uuid4().hex}"


def _prepare_workspace_root(source_path: Path) -> Path:
    """Create the default root; explicitly configured roots must already exist."""
    parent = workspace_root_for(source_path)
    if _configured_workspace_root() is None:
        try:
            parent.mkdir(exist_ok=True)
        except OSError as error:
            raise WorkerError(f"Could not create worker workspace root {parent}: {error}") from error
    return parent


def _reserve_worker_workspace(parent: Path, project: str) -> Path:
    """Create and return one empty, descriptively-named worker workspace directory."""
    last_error: OSError | None = None
    for _ in range(_WORKSPACE_NAME_ATTEMPTS):
        candidate = parent / _worker_workspace_name(project)
        try:
            candidate.mkdir(parents=False)
            return candidate
        except FileExistsError as error:
            last_error = error
            continue
        except OSError as error:
            raise WorkerError(f"Could not create worker workspace under {parent}: {error}") from error
    raise WorkerError(f"Could not create a unique worker workspace under {parent}: {last_error}")


def _write_worker_manifest(
    workspace: Path,
    *,
    source_kind: Literal["git", "directory-snapshot"],
    source_identity: str,
    source_reference: str,
    claim: ExperimentClaim,
) -> None:
    """Persist the versioned manifest identifying who built one worker workspace and from what."""
    manifest = WorkerManifest(
        source_kind=source_kind,
        source_identity=source_identity,
        source_reference=source_reference,
        host=socket.gethostname(),
        pid=os.getpid(),
        owner=claim.owner,
        worker_id=claim.token,
        started_at=claim.acquired_at,
    )
    try:
        save_json_object(workspace / WORKER_MANIFEST_FILE, manifest.to_dict())
    except (OSError, JsonStoreError) as error:
        raise WorkerError(f"Could not write worker workspace manifest: {error}") from error


def load_worker_manifest(workspace: Path) -> WorkerManifest:
    """Load the versioned manifest inside one worker workspace directory, for read-only inspection."""
    return WorkerManifest.from_dict(load_json_object(workspace / WORKER_MANIFEST_FILE))


class SourcePreparation(ABC, Generic[SourceT]):
    """Produce a verified working directory for one recorded source kind."""

    @abstractmethod
    @contextmanager
    def working_directory(
        self, experiment: ExperimentDirectory, source: SourceT, claim: ExperimentClaim
    ) -> Iterator[Path]:
        """Verify the source and yield the directory the command should run in."""
        raise NotImplementedError


class GitWorktreePreparation(SourcePreparation[GitSource]):
    """Build a detached worktree at the recorded commit and apply any patch."""

    @contextmanager
    def working_directory(
        self, experiment: ExperimentDirectory, source: GitSource, claim: ExperimentClaim
    ) -> Iterator[Path]:
        try:
            repository = GitRepository.locate(source.repository)
        except GitOperationError as error:
            raise WorkerError(str(error)) from error

        worktree = _reserve_worker_workspace(_prepare_workspace_root(repository.root), repository.root.name)
        try:
            repository.create_detached_worktree(worktree, source.commit)
            _write_worker_manifest(
                worktree,
                source_kind="git",
                source_identity=str(repository.root),
                source_reference=source.commit,
                claim=claim,
            )
            _apply_recorded_patch(repository, worktree, experiment, source.patch_file, source.patch_sha256)
            yield worktree
        except GitOperationError as error:
            raise WorkerError(str(error)) from error
        finally:
            if worktree.exists():
                try:
                    repository.remove_worktree(worktree)
                except GitOperationError as error:
                    raise WorkerError(f"Could not clean up worktree: {error}") from error


class VerifiedDirectoryPreparation(SourcePreparation[VerifiedDirectorySource]):
    """Re-verify an external directory and execute it where it stands."""

    @contextmanager
    def working_directory(
        self, experiment: ExperimentDirectory, source: VerifiedDirectorySource, claim: ExperimentClaim
    ) -> Iterator[Path]:
        # No ephemeral workspace is built here, so there is nothing to manifest;
        # `claim` is accepted only to satisfy the shared SourcePreparation signature.
        if not is_safe_directory(source.path):
            raise WorkerError(f"Verified-directory source is missing or not a directory: {source.path}")
        _verify_directory_matches_manifest(
            experiment,
            source.path,
            source.tree_digest,
            changed_message="Verified-directory source has changed since planning",
            scan_mode="reject-ignored",
        )
        yield source.path


class DirectorySnapshotPreparation(SourcePreparation[DirectorySnapshotSource]):
    """Materialize the captured copy in an isolated workspace and verify it again."""

    @contextmanager
    def working_directory(
        self, experiment: ExperimentDirectory, source: DirectorySnapshotSource, claim: ExperimentClaim
    ) -> Iterator[Path]:
        snapshot_dir = experiment.snapshot_source_directory
        if not is_safe_directory(snapshot_dir):
            raise WorkerError(f"Captured directory-snapshot source is missing: {snapshot_dir}")
        _verify_directory_matches_manifest(
            experiment,
            snapshot_dir,
            source.tree_digest,
            changed_message="Captured directory-snapshot source has changed since planning",
        )
        container = _reserve_worker_workspace(_prepare_workspace_root(source.original_path), source.original_path.name)
        try:
            _write_worker_manifest(
                container,
                source_kind="directory-snapshot",
                source_identity=str(source.original_path),
                source_reference=source.tree_digest,
                claim=claim,
            )
            workspace = container / DIRECTORY_SNAPSHOT_DIRECTORY
            try:
                shutil.copytree(snapshot_dir, workspace)
            except OSError as error:
                raise WorkerError(f"Could not materialize captured directory-snapshot source: {error}") from error
            # Verify again after materialization: the copy is what actually runs.
            _verify_directory_matches_manifest(
                experiment,
                workspace,
                source.tree_digest,
                changed_message="Materialized directory-snapshot source does not match its manifest",
                scan_mode="complete",
            )
            yield workspace
        finally:
            shutil.rmtree(container)


_PREPARATIONS: tuple[tuple[type, SourcePreparation], ...] = (
    (VerifiedDirectorySource, VerifiedDirectoryPreparation()),
    (DirectorySnapshotSource, DirectorySnapshotPreparation()),
)
_GIT_PREPARATION = GitWorktreePreparation()


def preparation_for(source: object) -> SourcePreparation:
    """Select the preparation for one recorded source kind.

    Non-Git sources are matched explicitly so they can never fall through to
    Git repository operations.
    """
    for source_type, preparation in _PREPARATIONS:
        if isinstance(source, source_type):
            return preparation
    return _GIT_PREPARATION


def _verify_directory_matches_manifest(
    experiment: ExperimentDirectory,
    directory: Path,
    tree_digest: str,
    *,
    changed_message: str,
    scan_mode: Literal["ignored", "complete", "reject-ignored"] = "ignored",
) -> None:
    manifest = _load_directory_source_manifest(experiment, tree_digest)
    try:
        scan = scan_directory(
            directory,
            ignore_file=scan_mode != "complete",
            reject_ignored=scan_mode == "reject-ignored",
        )
    except DirectoryScanError as error:
        raise WorkerError(str(error)) from error
    expected = {entry.path: entry for entry in manifest.entries}
    actual = {entry.path: entry for entry in scan.files}
    _require_matching_source_paths(expected, actual)
    _require_matching_source_entries(expected, actual)
    if scan.tree_digest != tree_digest:
        raise WorkerError(changed_message)


def _load_directory_source_manifest(experiment: ExperimentDirectory, tree_digest: str) -> DirectorySourceManifest:
    try:
        manifest = experiment.load_directory_source_manifest()
    except ValueError as error:
        raise WorkerError(str(error)) from error
    if manifest.tree_digest != tree_digest:
        raise WorkerError("Recorded source manifest digest does not match configuration")
    return manifest


def _require_matching_source_paths(expected: dict[str, object], actual: dict[str, object]) -> None:
    """Require the manifest's exact file set before comparing checksums."""
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    if missing:
        raise WorkerError(f"Source file is missing: {missing[0]}")
    if unexpected:
        raise WorkerError(f"Unexpected source file: {unexpected[0]}")


def _require_matching_source_entries(expected: dict[str, DirectorySourceEntry], actual: dict[str, ScannedFile]) -> None:
    for path, expected_entry in expected.items():
        actual_entry = actual[path]
        if actual_entry.sha256 != expected_entry.sha256:
            raise WorkerError(f"Source file checksum does not match manifest: {path}")
        if actual_entry.executable != expected_entry.executable:
            raise WorkerError(f"Source file executable bit does not match manifest: {path}")


def _apply_recorded_patch(
    repository: GitRepository,
    worktree: Path,
    experiment: ExperimentDirectory,
    patch_file: str | None,
    patch_sha256: str | None,
) -> None:
    if patch_file is None:
        return
    patch_path = experiment.source_file(patch_file)
    if not patch_path.is_file():
        raise WorkerError(f"Recorded Git patch is missing: {patch_path}")
    actual_sha256 = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    if actual_sha256 != patch_sha256:
        raise WorkerError("Recorded Git patch checksum does not match configuration")
    try:
        repository.check_patch(worktree, patch_path)
        repository.apply_patch(worktree, patch_path)
    except GitOperationError as error:
        raise WorkerError(str(error)) from error
