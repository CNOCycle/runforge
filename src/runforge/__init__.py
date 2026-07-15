"""Standalone building blocks for RunForge."""

from runforge.discovery import (
    DiscoveredExperiment,
    DiscoveryDiagnostic,
    DiscoveryError,
    DiscoveryResult,
    discover_experiments,
)
from runforge.experiment_schema import ExperimentCommand
from runforge.git_ops import GitHead, GitOperationError, GitRepository
from runforge.planner import (
    MatrixPlanRequest,
    PlanningError,
    PlanRequest,
    plan_experiment,
    plan_matrix,
)
from runforge.retry import RetryError, RetryPreparation, prepare_retry
from runforge.source_metadata import PinnedGitSource
from runforge.worker import WorkerError, WorkerProgressEvent, run_experiment


__all__ = [
    "DiscoveredExperiment",
    "DiscoveryDiagnostic",
    "DiscoveryError",
    "DiscoveryResult",
    "ExperimentCommand",
    "GitHead",
    "GitOperationError",
    "GitRepository",
    "MatrixPlanRequest",
    "PinnedGitSource",
    "PlanRequest",
    "PlanningError",
    "RetryError",
    "RetryPreparation",
    "WorkerError",
    "WorkerProgressEvent",
    "discover_experiments",
    "plan_experiment",
    "plan_matrix",
    "prepare_retry",
    "run_experiment",
]

__version__ = "0.1.0"
