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
from typing import Literal

from runforge.execution.discovery import DiscoveredExperiment
from runforge.infrastructure.claims import ClaimError, ExperimentClaim, load_claim
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
    return describe_claim_liveness(claim)


def describe_claim_liveness(claim: ExperimentClaim) -> str:
    """Describe local PID evidence without treating it as proof of abandonment."""
    return assess_claim_liveness(claim).description


# A worker starts before it claims, so its start time can never be meaningfully
# later than acquired_at; the margin absorbs boot-time and tick rounding.
_START_TIME_TOLERANCE_SECONDS = 2.0


@dataclass(frozen=True)
class ClaimLiveness:
    """Local evidence about a claim owner; never proof that a child stopped.

    ``active``: this host's recorded PID runs and started no later than the claim.
    ``stale``: the process exited, the PID is gone, or a later process reused it.
    ``unknown``: the evidence cannot be checked from this host.
    ``pid_running`` reports whether some process currently holds the PID.
    """

    verdict: Literal["active", "stale", "unknown"]
    description: str
    pid_running: bool = False


def assess_claim_liveness(claim: ExperimentClaim) -> ClaimLiveness:
    """Classify a claim owner as active, stale, or unknown from local evidence."""
    host, _, pid_text = claim.owner.partition(":")
    if host != socket.gethostname():
        return ClaimLiveness("unknown", f"cannot verify liveness: claim held by a different host ({host})")
    if not (pid_text.isascii() and pid_text.isdigit()) or int(pid_text) == 0:
        return ClaimLiveness(
            "unknown", f"cannot verify liveness: claim owner '{claim.owner}' is not in HOSTNAME:PID form"
        )
    pid = int(pid_text)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ClaimLiveness("stale", f"process appears dead: PID {pid} not running on {host}")
    except PermissionError:
        # The PID slot is occupied by some process, just not one we can
        # signal -- still "not confirmed dead".
        pass
    except (OSError, OverflowError) as error:
        return ClaimLiveness("unknown", f"cannot verify liveness for {claim.owner}: {error}")
    return _running_pid_liveness(claim, host, pid)


def _running_pid_liveness(claim: ExperimentClaim, host: str, pid: int) -> ClaimLiveness:
    """Separate the claim's own worker from a later process reusing its PID."""
    alive = f"process appears alive: PID {pid} running on {host}"
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, ValueError, IndexError):
        state = None
    if state in {"Z", "X", "x"}:
        return ClaimLiveness(
            "stale", f"process appears dead: PID {pid} on {host} has exited (state {state})", pid_running=True
        )
    if state is None:
        return ClaimLiveness("unknown", f"{alive} (process state unavailable; liveness not verified)", pid_running=True)
    started = process_start_time(pid)
    try:
        acquired = parse_utc(claim.acquired_at).timestamp()
    except ValueError:
        acquired = None
    if started is None or acquired is None:
        missing = "start time" if started is None else "claim time"
        return ClaimLiveness("unknown", f"{alive} ({missing} unavailable; PID reuse not excluded)", pid_running=True)
    if started > acquired + _START_TIME_TOLERANCE_SECONDS:
        return ClaimLiveness(
            "stale",
            f"process appears dead: PID {pid} on {host} was reused by a process started after the claim",
            pid_running=True,
        )
    return ClaimLiveness("active", alive, pid_running=True)


def process_start_time(pid: int) -> float | None:
    """Return a local process's start as a Unix timestamp, or None if unknown.

    Uses Linux /proc; other platforms, hidden PIDs, and parse failures yield None.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
            if line.startswith("btime ")
        )
        # Field 2 (comm) may contain spaces or parentheses; fields resume after the last ")".
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
