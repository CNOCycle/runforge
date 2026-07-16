"""Normalize supported Git source requests before experiment publication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from runforge.infrastructure.git import GitOperationError, GitRepository
from runforge.models.source import GIT_PATCH_FILE, GitSource, PinnedGitSource


class SourceResolutionError(RuntimeError):
    """Raised when a requested source cannot be normalized reproducibly."""


@dataclass(frozen=True)
class ResolvedGitSource:
    """A normalized source plus the captured patch bytes to publish."""

    repository: GitRepository
    source: GitSource
    patch: bytes


def resolve_current_git_source(source_path: Path) -> ResolvedGitSource:
    """Capture the repository's current HEAD, tracked patch, and untracked names."""
    try:
        repository = GitRepository.discover(source_path)
        head = repository.head()
        patch = repository.tracked_patch()
        untracked = repository.untracked_files()
    except GitOperationError as error:
        raise SourceResolutionError(str(error)) from error
    source = GitSource(
        repository=repository.root,
        commit=head.commit,
        branch=head.branch,
        patch_file=GIT_PATCH_FILE if patch else None,
        patch_sha256=hashlib.sha256(patch).hexdigest() if patch else None,
        untracked_files=tuple(untracked),
    )
    return ResolvedGitSource(repository=repository, source=source, patch=patch)


def resolve_pinned_git_source(descriptor: PinnedGitSource) -> ResolvedGitSource:
    """Resolve and validate an explicit source without consulting current HEAD."""
    try:
        repository = GitRepository.locate(descriptor.repository)
        commit = repository.resolve_commit(descriptor.commit)
    except GitOperationError as error:
        raise SourceResolutionError(str(error)) from error
    patch = b""
    if descriptor.patch is not None:
        patch_path = descriptor.patch
        try:
            patch = patch_path.read_bytes()
        except OSError as error:
            raise SourceResolutionError(f"Could not read Git patch {patch_path}: {error}") from error
        try:
            repository.check_patch_at_commit(commit, patch)
        except GitOperationError as error:
            raise SourceResolutionError(str(error)) from error
    source = GitSource(
        repository=repository.root,
        commit=commit,
        branch="pinned",
        patch_file=GIT_PATCH_FILE if patch else None,
        patch_sha256=hashlib.sha256(patch).hexdigest() if patch else None,
    )
    return ResolvedGitSource(repository=repository, source=source, patch=patch)
