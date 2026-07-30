"""Execution error types, shared so preparation modules need not import the worker."""

from __future__ import annotations


class WorkerError(RuntimeError):
    """Raised when one planned experiment cannot be prepared or executed."""


class ExperimentNotRunnableError(WorkerError):
    """Raised when a claimed experiment was completed or changed by another worker."""
