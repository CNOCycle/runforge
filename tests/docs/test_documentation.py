"""Checks that documentation stays consistent with the shipped CLI."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

from runforge.cli.parser import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTATION_ROOT = PROJECT_ROOT / "docs"
README = PROJECT_ROOT / "README.md"

_FENCED_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_LINE_CONTINUATION = re.compile(r"\\\s*\n\s*")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# Planning notes are internal working documents, not published guidance.
_EXCLUDED = ("docs/planning",)


def _documentation_files() -> list[Path]:
    files = [README, *sorted(DOCUMENTATION_ROOT.rglob("*.md"))]
    return [path for path in files if not any(part in path.as_posix() for part in _EXCLUDED)]


def _invocations(text: str) -> list[str]:
    """Return every `runforge ...` command written in a fenced block."""
    found: list[str] = []
    for block in _FENCED_BLOCK.findall(text):
        joined = _LINE_CONTINUATION.sub(" ", block)
        for line in joined.splitlines():
            stripped = line.strip().removeprefix("$ ").strip()
            if stripped.startswith("runforge "):
                found.append(stripped)
    return found


def _parses(parser: argparse.ArgumentParser, invocation: str) -> bool:
    arguments = shlex.split(invocation)[1:]
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(arguments)
        except SystemExit:
            return False
    return True


@pytest.mark.parametrize("path", _documentation_files(), ids=lambda path: path.name)
def test_documented_commands_are_accepted_by_the_current_parser(path):
    # Documentation drifts silently when a flag is renamed or removed; parsing
    # every published example turns that into a test failure.
    parser = build_parser()
    documented = _invocations(path.read_text(encoding="utf-8"))
    rejected = [invocation for invocation in documented if not _parses(parser, invocation)]

    assert rejected == [], f"{path.relative_to(PROJECT_ROOT)} documents commands the CLI rejects: {rejected}"


@pytest.mark.parametrize("path", _documentation_files(), ids=lambda path: path.name)
def test_documentation_links_resolve(path):
    broken = []
    for target in _MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            broken.append(target)

    assert broken == [], f"{path.relative_to(PROJECT_ROOT)} has unresolved links: {broken}"


def test_every_public_subcommand_is_documented():
    """Each subcommand needs a published home, so new commands cannot ship unwritten."""
    subcommands = {
        name
        for action in build_parser()._subparsers._group_actions  # noqa: SLF001
        for name in action.choices
    }
    documented = " ".join(path.read_text(encoding="utf-8") for path in _documentation_files())

    missing = sorted(name for name in subcommands if f"runforge {name}" not in documented)

    assert missing == [], f"subcommands with no documented example: {missing}"
