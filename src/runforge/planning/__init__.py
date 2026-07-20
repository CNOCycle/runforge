"""Experiment planning and source normalization."""

from runforge.planning.inputs import InputRenderingError, InputTemplate, RenderedInput, render_input_templates
from runforge.planning.matrix import MatrixError, expand_matrix
from runforge.planning.planner import (
    MatrixPlanRequest,
    PlanningError,
    PlanRequest,
    plan_experiment,
    plan_matrix,
)
from runforge.planning.source import (
    ResolvedGitSource,
    SourceResolutionError,
    resolve_current_git_source,
    resolve_pinned_git_source,
)
from runforge.planning.text_templates import TextTemplateError, render_text_template


__all__ = [
    "MatrixError",
    "MatrixPlanRequest",
    "InputRenderingError",
    "InputTemplate",
    "PlanRequest",
    "PlanningError",
    "ResolvedGitSource",
    "RenderedInput",
    "SourceResolutionError",
    "TextTemplateError",
    "expand_matrix",
    "plan_experiment",
    "plan_matrix",
    "resolve_current_git_source",
    "resolve_pinned_git_source",
    "render_text_template",
    "render_input_templates",
]
