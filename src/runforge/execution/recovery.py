"""Read-only recovery guidance from an existing experiment discovery snapshot."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from runforge.execution.discovery import DiscoveredExperiment
from runforge.execution.staleness import assess_claim_liveness
from runforge.infrastructure.claims import ClaimError, load_claim
from runforge.infrastructure.paths import is_absent
from runforge.infrastructure.storage import ExperimentDirectory


def recovery_messages(experiments: Sequence[DiscoveredExperiment]) -> tuple[tuple[Path, str], ...]:
    """Identify existing executions; never clear ownership or modify history.

    Only experiments with an inprogress status or remaining claim are described.
    Records are observations: execution still makes its own ownership checks.
    """
    messages = []
    for item in experiments:
        try:
            layout = ExperimentDirectory(item.path)
            if item.status.state != "inprogress" and is_absent(layout.claim):
                continue
            message = _describe_existing_execution(item)
            if message is not None:
                messages.append((item.path, message))
        except (OSError, ValueError) as error:
            messages.append(
                (item.path, f"Could not inspect existing execution at {item.path}: {error}; inspect its claim manually")
            )
    return tuple(messages)


def _describe_existing_execution(item) -> str | None:
    """Describe a stale or unverifiable record; verifiably active ones need no action."""
    lines = [
        f"Existing execution: state={item.status.state}, attempt={item.status.attempt} | path={item.path}",
        f"  started: {item.status.started_at or 'unknown'}",
    ]
    claim = None
    liveness = None
    try:
        claim = load_claim(ExperimentDirectory(item.path))
    except ClaimError as error:
        lines.append(f"  owner/activity unknown: {error}")
    if claim is not None:
        liveness = assess_claim_liveness(claim)
        if liveness.verdict == "active":
            return None
        lines.append(f"  owner: {claim.owner}; claimed: {claim.acquired_at}")
        lines.append(f"  {liveness.description}")
    if liveness is not None and liveness.verdict == "stale":
        lines.append("  assessment: stale -- the recorded worker process is no longer running")
    else:
        lines.append("  assessment: unknown -- worker activity cannot be verified from this host")
    lines.extend(_workspace_lines(item))
    lines.extend(_action_lines(item, claim, liveness))
    return "\n".join(lines)


def _workspace_lines(item: DiscoveredExperiment) -> list[str]:
    workspace = ExperimentDirectory(item.path).workspace
    if is_absent(workspace):
        return []
    return [f"  workdir present: {workspace} (a later execution deletes it and builds a fresh one)"]


def _action_lines(item, claim, liveness) -> list[str]:
    lines = ["  Confirm the old worker AND child stopped using host/container/job evidence; age is not proof."]
    if item.status.state not in {"failed", "inprogress"}:
        lines.append("  This state cannot be force-retried; preserve the record and inspect its claim manually.")
        return lines
    if item.held:
        lines.append("  A scheduling hold also prevents retry; resolve ownership before releasing it.")
    if liveness is not None and liveness.verdict == "unknown" and liveness.pid_running:
        pid = claim.owner.partition(":")[2]
        lines.append(f"  Its recorded PID is running: check it with 'ps -o lstart= -p {pid}' before any forced retry.")
    else:
        lines.append(f"  after confirmation: runforge retry --force {shlex.quote(str(item.path))}")
    lines.append("  Retry starts a new attempt; it does not resume the old process or workdir.")
    return lines
