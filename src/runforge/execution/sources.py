"""Per-mode preparation of the directory an experiment's command runs in.

Each source mode answers the same question differently: given a recorded
source, produce a verified working directory, and clean up afterwards. Git
builds a detached worktree and directory-snapshot materializes its captured
copy, both in the experiment's own ``.workspace`` directory; verified-directory
re-checks an external path in place.

Expressing that as one strategy per mode keeps each mode's setup, verification,
and teardown in one place, and lets the executor prepare a source without
knowing which kind it holds. Adding a mode means adding a class and registering
it, rather than extending a type switch that the executor would otherwise have
to carry.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Generic, Literal, TypeVar

from runforge.execution.errors import WorkerError
from runforge.infrastructure.directory_scan import DirectoryScanError, ScannedFile, scan_directory
from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.infrastructure.paths import is_absent, is_safe_directory
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.directory_source import (
    DirectorySnapshotSource,
    DirectorySourceEntry,
    DirectorySourceManifest,
    VerifiedDirectorySource,
)
from runforge.schemas.source import GitSource


SourceT = TypeVar("SourceT")


def _remove_leftover_workspace(experiment: ExperimentDirectory, repository: GitRepository | None = None) -> None:
    """Delete a workdir an interrupted execution left behind before building a fresh one.

    Callers hold the experiment's claim, so no other execution owns this path.
    A leftover is never reused: it can hold a killed command's partial output
    and, for Git, an already-applied patch. A locked Git worktree is a deliberate
    refusal, so it is left untouched and the execution fails instead.
    """
    workspace = experiment.workspace
    if repository is not None:
        _deregister_leftover_worktree(repository, workspace)
    if is_absent(workspace):
        return
    try:
        # A link is removed itself; never follow it to delete what it points at.
        if is_safe_directory(workspace):
            shutil.rmtree(workspace)
        else:
            workspace.unlink()
    except OSError as error:
        raise WorkerError(f"Could not remove leftover workdir {workspace}: {error}") from error


def _deregister_leftover_worktree(repository: GitRepository, workspace: Path) -> None:
    """Drop the source repository's record of a leftover worktree at *workspace*.

    A registration whose directory was deleted by hand is cleared too, so the
    fresh worktree can be added at the same path.
    """
    try:
        registration = repository.worktree_registration(workspace)
    except GitOperationError as error:
        raise WorkerError(f"Could not inspect leftover workdir {workspace}: {error}") from error
    if registration is None:
        return
    if registration.locked:
        unlock = shlex.join(["git", "-C", str(repository.root), "worktree", "unlock", str(workspace)])
        raise WorkerError(
            f"Leftover workdir {workspace} is a locked Git worktree; inspect it, then run "
            f"{unlock} before executing this experiment again"
        )
    if workspace.is_symlink():
        # Removing through Git would follow the link; the forced add replaces
        # this unlocked registration once the link itself is gone.
        return
    try:
        repository.remove_worktree(workspace)
    except GitOperationError:
        # An unlocked registration that Git cannot remove (for example, its
        # `.git` file is gone) is overridden by the forced add; the directory
        # itself is deleted by the caller.
        pass


class SourcePreparation(ABC, Generic[SourceT]):
    """Produce a verified working directory for one recorded source kind."""

    @abstractmethod
    @contextmanager
    def working_directory(self, experiment: ExperimentDirectory, source: SourceT) -> Iterator[Path]:
        """Verify the source and yield the directory the command should run in."""
        raise NotImplementedError


class GitWorktreePreparation(SourcePreparation[GitSource]):
    """Build a detached worktree at the recorded commit and apply any patch."""

    @contextmanager
    def working_directory(self, experiment: ExperimentDirectory, source: GitSource) -> Iterator[Path]:
        try:
            repository = GitRepository.locate(source.repository)
        except GitOperationError as error:
            raise WorkerError(str(error)) from error

        # The caller holds the experiment's claim, which makes the fixed workdir path ours.
        _remove_leftover_workspace(experiment, repository)
        worktree = experiment.workspace
        try:
            repository.create_detached_worktree(worktree, source.commit, force=True)
            _apply_recorded_patch(repository, worktree, experiment, source.patch_file, source.patch_sha256)
            yield worktree
        except GitOperationError as error:
            raise WorkerError(str(error)) from error
        finally:
            # Leftover removal emptied this path, so anything here now came from
            # this execution, including a worktree Git registered before failing.
            _remove_created_worktree(repository, worktree)


def _remove_created_worktree(repository: GitRepository, worktree: Path) -> None:
    """Remove and deregister whatever this execution's worktree add left at *worktree*.

    Covers a completed add, an add that registered the worktree before failing
    (for example, a failing post-checkout hook), and a command that deleted or
    replaced its own working directory.
    """
    try:
        if worktree.is_symlink():
            # The command replaced its workdir with a link; never follow it.
            worktree.unlink()
        if repository.worktree_registration(worktree) is not None:
            repository.remove_worktree(worktree)
        if not is_absent(worktree):
            shutil.rmtree(worktree)
    except (GitOperationError, OSError) as error:
        raise WorkerError(f"Could not clean up worktree: {error}") from error


class VerifiedDirectoryPreparation(SourcePreparation[VerifiedDirectorySource]):
    """Re-verify an external directory and execute it where it stands."""

    @contextmanager
    def working_directory(self, experiment: ExperimentDirectory, source: VerifiedDirectorySource) -> Iterator[Path]:
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
    """Materialize the captured copy in the experiment's workdir and verify it again."""

    @contextmanager
    def working_directory(self, experiment: ExperimentDirectory, source: DirectorySnapshotSource) -> Iterator[Path]:
        snapshot_dir = experiment.snapshot_source_directory
        if not is_safe_directory(snapshot_dir):
            raise WorkerError(f"Captured directory-snapshot source is missing: {snapshot_dir}")
        _verify_directory_matches_manifest(
            experiment,
            snapshot_dir,
            source.tree_digest,
            changed_message="Captured directory-snapshot source has changed since planning",
        )
        _remove_leftover_workspace(experiment)
        workspace = experiment.workspace
        try:
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
            if not is_absent(workspace):
                shutil.rmtree(workspace)


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
