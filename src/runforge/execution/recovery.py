"""Read-only recovery guidance from an existing experiment discovery snapshot."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from runforge.execution.discovery import DiscoveredExperiment
from runforge.execution.sources import workspace_root_for
from runforge.execution.staleness import assess_claim_liveness
from runforge.execution.workspace_cleanup import WorkspaceCleanupError, discover_workspaces
from runforge.infrastructure.claims import ClaimError, load_claim
from runforge.infrastructure.paths import is_absent
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.directory_source import DirectorySnapshotSource
from runforge.schemas.source import GitSource


def recovery_messages(experiments: Sequence[DiscoveredExperiment]) -> tuple[tuple[Path, str], ...]:
    """Identify existing executions; never clear ownership or modify history.

    Only the current configured/default workspace parents are searched, once
    each, and only for experiments with an inprogress status or remaining claim.
    Records are observations: execution still makes its own ownership checks.
    """
    messages = []
    workspaces: dict[Path, tuple[dict[str, tuple[Path, ...]], str | None]] = {}
    for item in experiments:
        try:
            layout = ExperimentDirectory(item.path)
            if item.status.state != "inprogress" and is_absent(layout.claim):
                continue
            message = _describe_existing_execution(item, workspaces)
            if message is not None:
                messages.append((item.path, message))
        except (OSError, ValueError) as error:
            messages.append(
                (item.path, f"Could not inspect existing execution at {item.path}: {error}; inspect its claim manually")
            )
    return tuple(messages)


def _describe_existing_execution(item, workspace_cache) -> str | None:
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
    lines.extend(_workspace_lines(item, claim, workspace_cache))
    lines.extend(_action_lines(item, claim, liveness))
    return "\n".join(lines)


def _workspace_lines(item, claim, workspace_cache) -> list[str]:
    root = _workspace_root(item)
    if root is None:
        return []
    if root not in workspace_cache:
        workspace_cache[root] = _workspace_index(root)
    index, error = workspace_cache[root]
    matches = index.get(claim.token, ()) if claim is not None else ()
    lines = [f"  matching workspace: {workspace}" for workspace in matches]
    if not matches:
        lines.append(f"  matching workspace unknown: no readable token match under {root}")
    if error:
        lines.append(f"  workspace inspection incomplete: {error}")
    lines.append(f"  inspect workspaces: runforge workspaces {shlex.quote(str(root))}")
    return lines


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
    lines.append("  Retry starts a new attempt; it does not resume the old process or workspace.")
    return lines


def _workspace_root(item: DiscoveredExperiment) -> Path | None:
    source = item.configuration.source
    if isinstance(source, GitSource):
        return workspace_root_for(source.repository)
    if isinstance(source, DirectorySnapshotSource):
        return workspace_root_for(source.original_path)
    return None


def _workspace_index(root: Path) -> tuple[dict[str, tuple[Path, ...]], str | None]:
    try:
        discovered = discover_workspaces(root)
    except (WorkspaceCleanupError, OSError) as error:
        return {}, str(error)
    index: dict[str, tuple[Path, ...]] = {}
    unreadable = 0
    for workspace in discovered:
        if workspace.manifest is None:
            unreadable += 1
        else:
            token = workspace.manifest.worker_id
            index[token] = (*index.get(token, ()), workspace.path)
    error = f"{unreadable} workspace manifest(s) unreadable; associations unknown" if unreadable else None
    return index, error
