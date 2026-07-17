"""Standalone building blocks for RunForge."""

from runforge.execution import (
    DiscoveredExperiment,
    DiscoveryDiagnostic,
    DiscoveryError,
    DiscoveryResult,
    RetryError,
    RetryPreparation,
    WorkerError,
    WorkerProgressEvent,
    discover_experiments,
    prepare_retry,
    run_experiment,
)
from runforge.infrastructure import GitHead, GitOperationError, GitRepository
from runforge.planning import (
    MatrixPlanRequest,
    PlanningError,
    PlanRequest,
    plan_experiment,
    plan_matrix,
)
from runforge.schemas import ExperimentCommand, PinnedGitSource


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
