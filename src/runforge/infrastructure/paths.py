"""Predicates for the filesystem entries RunForge is willing to act on.

RunForge rejects symbolic links wherever it stores, verifies, or executes
content, because a link makes the recorded identity of a path depend on
something outside the experiment. These predicates state that rule once so
every caller enforces it identically; each caller keeps its own message,
because what a rejected path means differs by context.
"""

from __future__ import annotations

from pathlib import Path


def is_safe_directory(path: Path) -> bool:
    """Report whether path is a real directory rather than a link or other entry."""
    return path.is_dir() and not path.is_symlink()


def is_safe_file(path: Path) -> bool:
    """Report whether path is a real regular file rather than a link or other entry."""
    return path.is_file() and not path.is_symlink()


def is_absent(path: Path) -> bool:
    """Report whether nothing occupies path, counting a dangling link as present."""
    return not path.exists() and not path.is_symlink()
