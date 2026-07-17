"""Small, testable Git operations used by later RunForge stages."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from runforge.schemas.source import GIT_PATCH_FILE


class GitOperationError(RuntimeError):
    """Raised when a required Git operation cannot be completed."""


@dataclass(frozen=True)
class GitHead:
    """The resolved commit and human-readable branch label for a repository."""

    commit: str
    branch: str


@dataclass(frozen=True)
class GitRepository:
    """A validated Git repository with standalone source/worktree operations."""

    root: Path

    @classmethod
    def locate(cls, path: Path) -> GitRepository:
        """Find the Git repository containing *path* without consulting HEAD."""
        requested_path = Path(path).expanduser().resolve()
        if not requested_path.exists():
            raise GitOperationError(f"Git source path does not exist: {requested_path}")
        root = cls._text_at(requested_path, ["rev-parse", "--show-toplevel"], "find Git repository")
        return cls(Path(root).resolve())

    @classmethod
    def discover(cls, path: Path) -> GitRepository:
        """Find the Git repository containing *path* and require a valid HEAD."""
        repository = cls.locate(path)
        repository.head()
        return repository

    def resolve_commit(self, reference: str = "HEAD") -> str:
        """Resolve a commit reference to its full immutable object ID."""
        if not isinstance(reference, str) or not reference.strip():
            raise GitOperationError("Git commit reference must be a non-empty string")
        return self._text(["rev-parse", "--verify", f"{reference}^{{commit}}"], "resolve Git commit")

    def head(self) -> GitHead:
        """Return the full HEAD commit and its branch name, or ``detached``."""
        commit = self.resolve_commit()
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], required=False)
        branch = result.stdout.decode("utf-8", errors="replace").strip() or "detached"
        return GitHead(commit=commit, branch=branch)

    def tracked_patch(self) -> bytes:
        """Return staged and unstaged tracked changes relative to HEAD."""
        return self._run(["diff", "--binary", "HEAD", "--"]).stdout

    def untracked_files(self) -> list[str]:
        """List untracked, non-ignored paths without reading their contents."""
        output = self._run(["ls-files", "--others", "--exclude-standard", "-z"]).stdout
        return [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]

    def create_detached_worktree(self, destination: Path, commit: str) -> Path:
        """Create and return a detached worktree at a resolved commit."""
        worktree = Path(destination).expanduser().resolve()
        resolved_commit = self.resolve_commit(commit)
        self._run(["worktree", "add", "--detach", str(worktree), resolved_commit])
        return worktree

    def remove_worktree(self, worktree: Path, *, force: bool = True) -> None:
        """Remove a worktree, forcing cleanup by default for patched worktrees."""
        arguments = ["worktree", "remove"]
        if force:
            arguments.append("--force")
        arguments.append(str(Path(worktree).expanduser().resolve()))
        self._run(arguments)

    def check_patch(self, worktree: Path, patch: Path) -> None:
        """Check that a patch can apply in *worktree* without modifying it."""
        patch_path = self._patch_path(patch)
        self._run_at(
            Path(worktree).expanduser().resolve(),
            ["apply", "--check", "--whitespace=nowarn", str(patch_path)],
            "check Git patch",
        )

    def apply_patch(self, worktree: Path, patch: Path) -> None:
        """Apply a checked patch in *worktree*. The caller controls cleanup."""
        patch_path = self._patch_path(patch)
        self._run_at(
            Path(worktree).expanduser().resolve(),
            ["apply", "--whitespace=nowarn", str(patch_path)],
            "apply Git patch",
        )

    def check_patch_at_commit(self, commit: str, patch: bytes) -> None:
        """Check captured patch bytes in a temporary worktree at *commit*."""
        with tempfile.TemporaryDirectory(prefix="runforge-patch-check-") as temporary_root:
            patch_path = Path(temporary_root) / GIT_PATCH_FILE
            try:
                patch_path.write_bytes(patch)
            except OSError as error:
                raise GitOperationError(f"Could not stage Git patch for validation: {error}") from error
            worktree = Path(temporary_root) / "worktree"
            try:
                self.create_detached_worktree(worktree, commit)
                self.check_patch(worktree, patch_path)
            finally:
                if worktree.exists():
                    self.remove_worktree(worktree)

    def _text(self, arguments: list[str], operation: str) -> str:
        return self._run(arguments, operation=operation).stdout.decode("utf-8", errors="replace").strip()

    def _run(
        self,
        arguments: list[str],
        operation: str = "run Git command",
        *,
        required: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run_at(self.root, arguments, operation, required=required)

    @staticmethod
    def _patch_path(patch: Path) -> Path:
        patch_path = Path(patch).expanduser().resolve()
        if not patch_path.is_file():
            raise GitOperationError(f"Git patch does not exist or is not a file: {patch_path}")
        return patch_path

    @staticmethod
    def _text_at(path: Path, arguments: list[str], operation: str) -> str:
        return GitRepository._run_at(path, arguments, operation).stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _run_at(
        path: Path,
        arguments: list[str],
        operation: str,
        *,
        required: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, check=False)
        except FileNotFoundError as error:
            if required:
                raise GitOperationError("Git executable was not found") from error
            return subprocess.CompletedProcess(arguments, 127, b"", b"Git executable was not found")
        if required and result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitOperationError(f"Could not {operation}: {message or 'unknown Git error'}")
        return result
