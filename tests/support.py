"""Shared Git and CLI helpers for RunForge tests."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path


PLAN_CREATED_PREFIX = "Experiment plan created at: "


def git(repository: Path, *arguments: str) -> str:
    """Run one required Git command in a test repository."""
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def create_git_repository(repository: Path, files: Mapping[str, str]) -> Path:
    """Create one committed Git repository with deterministic test identity."""
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.email", "test@example.com")
    git(repository, "config", "user.name", "Test User")
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "initial")
    return repository


def planned_path(output: str) -> Path:
    """Extract the experiment directory from CLI plan or launch output."""
    for line in output.splitlines():
        if line.startswith(PLAN_CREATED_PREFIX):
            return Path(line.removeprefix(PLAN_CREATED_PREFIX).strip())
    raise AssertionError(f"{PLAN_CREATED_PREFIX!r} not found in output:\n{output}")
