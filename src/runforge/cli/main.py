"""Parse the CLI and invoke its command handler."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from runforge.cli.commands import _COMMANDS
from runforge.cli.parser import build_parser
from runforge.execution.discovery import DiscoveryError
from runforge.execution.retry import RetryError
from runforge.execution.worker import WorkerError
from runforge.execution.workspace_cleanup import WorkspaceCleanupError
from runforge.planning.planner import PlanningError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RunForge CLI and return its process exit code."""
    arguments = build_parser().parse_args(argv)
    try:
        return _COMMANDS[arguments.subcommand](arguments)
    except (DiscoveryError, PlanningError, RetryError, WorkerError, WorkspaceCleanupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
