"""Deterministic safe-directory scanning for non-Git source modes.

``.git`` directories are always excluded, at any depth. An optional
``.gitignore`` file directly below the scanned root adds additional
exclusions: each non-blank, non-comment line is a glob pattern matched with
``fnmatch``. A pattern containing ``/`` is matched against the file's full
POSIX path relative to the root; a pattern without ``/`` is matched against
the file's basename at any depth. A trailing ``/`` is stripped before
matching. There is no negation syntax, and ``.git`` cannot be re-included by
any pattern.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from runforge.infrastructure.paths import is_absent, is_safe_directory, is_safe_file


IGNORE_FILE = ".gitignore"
_ALWAYS_IGNORED_NAME = ".git"
_HASH_CHUNK_SIZE = 1024 * 1024


class DirectoryScanError(RuntimeError):
    """Raised when a directory cannot be scanned deterministically and safely."""


@dataclass(frozen=True)
class ScannedFile:
    """One regular file discovered below a scanned source directory."""

    path: str
    executable: bool
    sha256: str


@dataclass(frozen=True)
class DirectoryScanResult:
    """An ordered file manifest plus its deterministic full-tree digest."""

    root: Path
    files: tuple[ScannedFile, ...]
    tree_digest: str


def scan_directory(root: Path, *, ignore_file: bool = True, reject_ignored: bool = False) -> DirectoryScanResult:
    """Recursively scan *root* and hash its regular files deterministically.

    Every symlink, socket, device, FIFO, and other special file is rejected.
    The returned files are sorted by relative path, and the full-tree digest
    is a function of each file's path, executable bit, and content digest, so
    it does not depend on traversal order, timestamps, or user/group IDs.
    """
    resolved_root = Path(root).expanduser()
    if not is_safe_directory(resolved_root):
        raise DirectoryScanError(f"Source directory does not exist or is not a directory: {resolved_root}")
    resolved_root = resolved_root.resolve()
    patterns = _load_ignore_patterns(resolved_root) if ignore_file else ()
    paths: list[Path] = []
    ignored: list[str] = []
    _walk_directory(resolved_root, resolved_root, patterns, paths, ignored)
    if reject_ignored and ignored:
        raise DirectoryScanError(f"Source directory contains files excluded by {IGNORE_FILE}: {ignored[0]}")
    files = tuple(sorted((_scan_file(resolved_root, path) for path in paths), key=lambda entry: entry.path))
    return DirectoryScanResult(root=resolved_root, files=files, tree_digest=_tree_digest(files))


def capture_directory(source: Path, destination: Path) -> DirectoryScanResult:
    """Copy *source* into a fresh, non-existent *destination* while scanning it.

    The same ignore and safety rules as :func:`scan_directory` apply. Every
    regular file is copied byte-for-byte into *destination*, preserving its
    executable bit, while its content digest is computed in the same pass.
    """
    resolved_root = Path(source).expanduser()
    if not is_safe_directory(resolved_root):
        raise DirectoryScanError(f"Source directory does not exist or is not a directory: {resolved_root}")
    resolved_root = resolved_root.resolve()
    resolved_destination = Path(destination).expanduser()
    if not is_absent(resolved_destination):
        raise DirectoryScanError(f"Capture destination already exists: {resolved_destination}")
    patterns = _load_ignore_patterns(resolved_root)
    paths: list[Path] = []
    _walk_directory(resolved_root, resolved_root, patterns, paths, [])
    resolved_destination.mkdir(parents=True)
    files = tuple(
        sorted(
            (_capture_file(resolved_root, resolved_destination, path) for path in paths),
            key=lambda entry: entry.path,
        )
    )
    return DirectoryScanResult(root=resolved_root, files=files, tree_digest=_tree_digest(files))


def _capture_file(root: Path, destination_root: Path, path: Path) -> ScannedFile:
    relative = path.relative_to(root).as_posix()
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise DirectoryScanError(f"Could not stat source file {relative}: {error}") from error
    destination = destination_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source_stream, destination.open("wb") as destination_stream:
            for chunk in iter(lambda: source_stream.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
                destination_stream.write(chunk)
    except OSError as error:
        raise DirectoryScanError(f"Could not copy source file {relative}: {error}") from error
    executable = bool(stat.S_IMODE(mode) & 0o111)
    if executable:
        destination.chmod(destination.stat().st_mode | 0o111)
    return ScannedFile(path=relative, executable=executable, sha256=digest.hexdigest())


def _walk_directory(
    root: Path, current: Path, patterns: tuple[str, ...], files: list[Path], ignored: list[str]
) -> None:
    try:
        entries = sorted(os.scandir(current), key=lambda entry: entry.name)
    except OSError as error:
        raise DirectoryScanError(f"Could not scan source directory: {error}") from error
    for entry in entries:
        candidate = Path(entry.path)
        relative = candidate.relative_to(root).as_posix()
        if _is_ignored(relative, entry.name, patterns):
            if entry.name != _ALWAYS_IGNORED_NAME:
                ignored.append(relative)
            continue
        if entry.is_symlink():
            raise DirectoryScanError(f"Source directory contains a symbolic link: {relative}")
        if entry.is_dir(follow_symlinks=False):
            _walk_directory(root, candidate, patterns, files, ignored)
        elif entry.is_file(follow_symlinks=False):
            files.append(candidate)
        else:
            raise DirectoryScanError(f"Source directory contains an unsupported file type: {relative}")


def _scan_file(root: Path, path: Path) -> ScannedFile:
    relative = path.relative_to(root).as_posix()
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise DirectoryScanError(f"Could not stat source file {relative}: {error}") from error
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as error:
        raise DirectoryScanError(f"Could not read source file {relative}: {error}") from error
    return ScannedFile(path=relative, executable=bool(stat.S_IMODE(mode) & 0o111), sha256=digest.hexdigest())


def _tree_digest(files: Sequence[ScannedFile]) -> str:
    digest = hashlib.sha256()
    for entry in files:
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(b"1" if entry.executable else b"0")
        digest.update(b"\x00")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load_ignore_patterns(root: Path) -> tuple[str, ...]:
    ignore_file = root / IGNORE_FILE
    if not is_safe_file(ignore_file):
        return ()
    try:
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise DirectoryScanError(f"Could not read {IGNORE_FILE}: {error}") from error
    patterns = []
    for line in lines:
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        patterns.append(pattern.rstrip("/"))
    return tuple(patterns)


def _is_ignored(relative: str, name: str, patterns: Sequence[str]) -> bool:
    if name == _ALWAYS_IGNORED_NAME:
        return True
    for pattern in patterns:
        if "/" in pattern:
            if fnmatch(relative, pattern):
                return True
        elif fnmatch(name, pattern):
            return True
    return False
