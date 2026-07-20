"""Package and source-checkout version reporting."""

from __future__ import annotations

import re
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path


_PACKAGE_NAME = "runforge"
_GIT_REVISION_PATTERN = re.compile(r"[0-9a-fA-F]{4,}")
_TABLE_PATTERN = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_VERSION_PATTERN = re.compile(r'^\s*version\s*=\s*(?:"([^"]+)"|\'([^\']+)\')\s*(?:#.*)?$')


def display_version() -> str:
    """Return the project version with best-effort source revision provenance."""
    source_root = _source_root()
    base_version = _project_version(source_root) or _installed_version()
    if base_version is None:
        return "unknown"
    revision = _git_revision(source_root)
    return base_version if revision is None else f"{base_version}+{revision}"


def _source_root() -> Path | None:
    """Find the project root when this package is imported from a source tree."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def _project_version(source_root: Path | None) -> str | None:
    """Read a quoted `[project].version` without adding a general TOML parser."""
    if source_root is None:
        return None
    try:
        lines = (source_root / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_project = False
    for line in lines:
        table = _TABLE_PATTERN.fullmatch(line)
        if table is not None:
            if in_project:
                return None
            in_project = table.group(1) == "project"
            continue
        if in_project:
            version = _VERSION_PATTERN.fullmatch(line)
            if version is not None:
                return version.group(1) or version.group(2)
    return None


def _installed_version() -> str | None:
    """Return installed distribution metadata when no source TOML is available."""
    try:
        return distribution_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return None


def _git_revision(source_root: Path | None) -> str | None:
    """Return the first four characters of this package checkout's Git revision."""
    if source_root is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    revision = completed.stdout.strip()
    return revision[:4].lower() if completed.returncode == 0 and _GIT_REVISION_PATTERN.fullmatch(revision) else None
