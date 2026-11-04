"""Wildcard expansion for directory path arguments.

One rule serves every command that accepts patterns: matching walks directories
the way discovery does. It never enters hidden directories or symbolic links and
never lists anything below an experiment, damaged or not. A directory named
literally before the first wildcard is an explicit root and is used as written.

Supported syntax is ``*``, ``?``, ``[...]`` within one path component, and at most
one ``**`` component, which matches any number of directory levels including none.
Only directories match.
"""

from __future__ import annotations

import fnmatch
import os
import re
from functools import cache
from pathlib import Path

from runforge.execution.discovery import DiscoveryError, is_experiment_boundary


WILDCARD_CHARACTERS = "*?["


def has_wildcard(text: str) -> bool:
    """Whether ``text`` contains pattern syntax."""
    return any(character in text for character in WILDCARD_CHARACTERS)


def expand_path_pattern(pattern: Path) -> list[Path]:
    """Directories matching ``pattern``, sorted; empty when nothing matches.

    Raises ``DiscoveryError`` when a directory cannot be scanned, so a partial
    selection is never mistaken for a complete one.
    """
    parts = pattern.parts
    if parts.count("**") > 1:
        raise ValueError(f"At most one ** is supported in a path pattern: {pattern}")
    base, rest = _split_literal_prefix(parts)
    return sorted(_match(base, rest))


def _split_literal_prefix(parts: tuple[str, ...]) -> tuple[Path, tuple[str, ...]]:
    """Leading components used as written: plain names and existing directories.

    A directory such as ``reports [2026]`` is matched literally when it exists.
    Wildcard characters in components after the first real wildcard are pattern syntax.
    """
    for index, part in enumerate(parts):
        if part == "**" or (has_wildcard(part) and not Path(*parts[: index + 1]).is_dir()):
            return (Path(*parts[:index]) if index else Path(".")), parts[index:]
    return Path(*parts), ()


@cache
def _component(part: str) -> re.Pattern[str]:
    return re.compile(fnmatch.translate(part))


def _children(directory: Path) -> list[Path]:
    """Subdirectories discovery would descend into."""
    if is_experiment_boundary(directory):
        return []
    try:
        with os.scandir(directory) as entries:
            return sorted(
                directory / entry.name
                for entry in entries
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
            )
    except OSError as error:
        raise DiscoveryError(f"Could not scan directory {directory}: {error}") from error


def _match(directory: Path, parts: tuple[str, ...]) -> set[Path]:
    children = _children(directory)
    if not parts:
        return {directory}
    part, rest = parts[0], parts[1:]
    if part == "**":
        found = _match(directory, rest)
        for child in children:
            found |= _match(child, parts)
        return found
    matches = _component(part).match
    found = set()
    for child in children:
        if matches(child.name):
            found |= _match(child, rest)
    return found
