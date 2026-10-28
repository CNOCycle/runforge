"""Parse the CLI and invoke an explicitly selected leaf handler."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from runforge.cli.parser import build_parser
from runforge.execution.discovery import DiscoveryError
from runforge.execution.errors import WorkerError
from runforge.execution.retry import RetryError
from runforge.execution.workspace_cleanup import WorkspaceCleanupError
from runforge.infrastructure.claims import ClaimError
from runforge.planning.planner import PlanningError


def main(argv: Sequence[str] | None = None) -> int:
    """Usage/domain failures return 2; leaves define their execution contract."""
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except (
        ClaimError,
        DiscoveryError,
        PlanningError,
        RetryError,
        WorkerError,
        WorkspaceCleanupError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
