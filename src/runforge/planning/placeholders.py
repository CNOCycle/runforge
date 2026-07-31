"""Strict placeholder discovery for matrix planning."""

from __future__ import annotations

import re
from collections.abc import Iterable

from runforge.planning.inputs import InputTemplate
from runforge.schemas.experiment import ExperimentCommand


STRICT_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RUNFORGE_PLACEHOLDERS = frozenset({"ARTIFACT_DIR", "INPUT_DIR"})


def extract_strict_placeholders(text: str) -> frozenset[str]:
    """Return brace placeholders that use RunForge's identifier grammar."""
    return frozenset(match.group(1) for match in STRICT_PLACEHOLDER_PATTERN.finditer(text))


def command_placeholders(command: ExperimentCommand) -> frozenset[str]:
    """Find strict placeholders in argv commands, leaving shell syntax opaque."""
    if command.mode != "argv":
        return frozenset()
    return frozenset().union(*(extract_strict_placeholders(argument) for argument in command.arguments))


def input_placeholders(inputs: Iterable[InputTemplate]) -> frozenset[str]:
    """Find placeholders in rendered input templates, excluding byte-for-byte copies."""
    return frozenset().union(*(extract_strict_placeholders(entry.content) for entry in inputs if entry.kind != "copy"))


def validate_declared_placeholders(
    command: ExperimentCommand,
    inputs: Iterable[InputTemplate],
    matrix_parameters: Iterable[str],
) -> None:
    """Reject strict placeholders that are neither matrix nor RunForge-owned."""
    declared = set(matrix_parameters) | RUNFORGE_PLACEHOLDERS
    used = command_placeholders(command) | input_placeholders(inputs)
    undeclared = sorted(used - declared)
    if undeclared:
        names = ", ".join(f"{{{name}}}" for name in undeclared)
        raise ValueError(f"Undeclared matrix placeholder(s): {names}")
