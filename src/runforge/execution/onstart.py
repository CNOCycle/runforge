"""Support for restarting work on a single machine that has just started.

An interruption kills the worker but leaves its experiment ``inprogress`` with a
claim, and forced retry is only safe once nothing else is running it. The lock
here stops a second ``onstart`` on the same host; the caller still has to
confirm that no other machine works on the report root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from runforge.execution.discovery import discover_experiments
from runforge.infrastructure.claims import ClaimError


try:
    import fcntl
except ImportError:  # pragma: no cover - platforms without POSIX file locking
    fcntl = None  # type: ignore[assignment]

LOCK_FILE_NAME = ".onstart.lock"


class ReportRootLock:
    """An exclusive, non-blocking lock on one report root, held while in use."""

    def __init__(self, report_root: Path) -> None:
        self._path = report_root / LOCK_FILE_NAME
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Take the lock or raise ClaimError when another onstart holds it."""
        if fcntl is None:
            raise ClaimError("runforge onstart needs fcntl file locking, which this platform lacks")
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ClaimError(f"Another runforge onstart already holds {self._path}") from None
        self._descriptor = descriptor

    def release(self) -> None:
        """Drop the lock held by this process."""
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> ReportRootLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@dataclass(frozen=True)
class Interrupted:
    """Experiments an interruption left behind."""

    restart: tuple[Path, ...]
    held: tuple[Path, ...]
    unstarted_claimed: tuple[Path, ...]


def find_interrupted(report_root: Path) -> Interrupted:
    """Split a report root into restartable and left-alone experiments.

    An ``inprogress`` experiment is restartable because its worker is gone,
    unless it is held: a scheduling hold takes precedence, so it is only
    reported. A created or init experiment that still holds a claim cannot be
    recovered with a forced retry, so it is only reported too.
    """
    restart: list[Path] = []
    held: list[Path] = []
    unstarted_claimed: list[Path] = []
    for experiment in discover_experiments(report_root).experiments:
        state = experiment.status.state
        if state == "inprogress":
            (held if experiment.held else restart).append(experiment.path)
        elif state in ("created", "init") and (experiment.path / "claim").exists():
            unstarted_claimed.append(experiment.path)
    return Interrupted(restart=tuple(restart), held=tuple(held), unstarted_claimed=tuple(unstarted_claimed))
