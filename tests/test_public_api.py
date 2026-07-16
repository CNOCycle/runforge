"""Regression tests for stable package and console entry points."""

from __future__ import annotations

import runforge
from runforge.cli import main
from runforge.execution import discover_experiments, prepare_retry, run_experiment
from runforge.planning import plan_experiment, plan_matrix


def test_root_package_reexports_stable_workflow_api():
    assert runforge.plan_experiment is plan_experiment
    assert runforge.plan_matrix is plan_matrix
    assert runforge.discover_experiments is discover_experiments
    assert runforge.prepare_retry is prepare_retry
    assert runforge.run_experiment is run_experiment
    assert callable(main)
