"""Read-only staleness diagnosis for inprogress experiments.

This never mutates state, releases a claim, or retries anything -- RunForge
cannot distinguish a dead worker from one that is merely slow, so this only
ever reports what it can verify, for a human to act on.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from runforge.execution.discovery import DiscoveredExperiment
from runforge.infrastructure.claims import ClaimError, load_claim
from runforge.infrastructure.clock import parse_utc
from runforge.infrastructure.storage import ExperimentDirectory


@dataclass(frozen=True)
class StaleHint:
    """One inprogress experiment whose started_at predates a configured cutoff."""

    started_at: str
    elapsed_seconds: float
    verdict: str


def describe_staleness(experiment: DiscoveredExperiment, cutoff: datetime) -> StaleHint | None:
    """Return a staleness hint for one experiment, or None if it doesn't qualify.

    Only ``inprogress`` experiments with a recorded ``started_at`` before
    ``cutoff`` qualify; everything else (including a fresh ``inprogress``
    experiment) yields no hint.
    """
    status = experiment.status
    if status.state != "inprogress" or status.started_at is None:
        return None
    started_at = parse_utc(status.started_at)
    if started_at >= cutoff:
        return None
    elapsed = (datetime.now(started_at.tzinfo) - started_at).total_seconds()
    return StaleHint(
        started_at=status.started_at,
        elapsed_seconds=elapsed,
        verdict=_liveness_verdict(experiment.path),
    )


def _liveness_verdict(experiment_path: Path) -> str:
    try:
        claim = load_claim(ExperimentDirectory(experiment_path))
    except ClaimError:
        return "cannot verify liveness: claim metadata is missing or unreadable"
    host, _, pid_text = claim.owner.partition(":")
    if host != socket.gethostname():
        return f"cannot verify liveness: claim held by a different host ({host})"
    if not pid_text.isdigit() or int(pid_text) == 0:
        return f"cannot verify liveness: claim owner '{claim.owner}' is not in HOSTNAME:PID form"
    pid = int(pid_text)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return f"process appears dead: PID {pid} not running on {host}"
    except PermissionError:
        # The PID slot is occupied by some process, just not one we can
        # signal -- still "not confirmed dead".
        return f"process appears alive: PID {pid} running on {host}"
    return f"process appears alive: PID {pid} running on {host}"
