"""Tests for read-only experiment discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from runforge.execution.discovery import DiscoveryError, discover_experiments
from runforge.infrastructure.json_store import save_json_object
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.source import GitSource


def _experiment(path: Path, *, state: str = "created", attempt: int = 0) -> None:
    path.mkdir(parents=True)
    configuration = ExperimentConfiguration(
        experiment_id=path.name,
        name=path.name,
        command=ExperimentCommand.argv(("python", "train.py")),
        environment={},
        source=GitSource(
            repository=path.parent.resolve(),
            commit="a" * 40,
            branch="main",
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )
    status = ExperimentStatus(
        state=state,
        attempt=attempt,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_json_object(path / "config.json", configuration.to_dict())
    save_json_object(path / "status.json", status.to_dict())


def test_discovery_returns_valid_experiments_in_resolved_path_order(tmp_path):
    root = tmp_path / "reports"
    second = root / "z-branch" / "second"
    first = root / "a-branch" / "first"
    _experiment(second, state="completed", attempt=1)
    _experiment(first)

    result = discover_experiments(root)

    assert result.root == root.resolve()
    assert [experiment.path for experiment in result.experiments] == [first.resolve(), second.resolve()]
    assert [experiment.status.state for experiment in result.experiments] == ["created", "completed"]
    assert result.diagnostics == ()


def test_discovery_ignores_in_flight_planning_staging_directories(tmp_path):
    root = tmp_path / "reports"
    published = root / "main" / "01234567_baseline_0000"
    # Planning writes config.json and status.json into this shape before renaming it
    # into place, so a discovered staging tree could be claimed and executed.
    staging = root / "main" / ".89abcdef_baseline_0001.tmp-deadbeefcafe"
    _experiment(published)
    _experiment(staging)

    result = discover_experiments(root)

    assert [experiment.path for experiment in result.experiments] == [published.resolve()]
    assert result.diagnostics == ()


def test_discovery_scans_a_report_root_that_is_itself_dot_prefixed(tmp_path):
    root = tmp_path / ".experiments"
    experiment = root / "main" / "01234567_baseline_0000"
    _experiment(experiment)

    result = discover_experiments(root)

    assert [record.path for record in result.experiments] == [experiment.resolve()]


def test_discovery_includes_the_root_when_it_is_an_experiment(tmp_path):
    experiment = tmp_path / "experiment"
    _experiment(experiment)

    result = discover_experiments(experiment)

    assert [record.path for record in result.experiments] == [experiment.resolve()]


def test_discovery_reports_invalid_candidates_and_keeps_valid_ones(tmp_path):
    root = tmp_path / "reports"
    valid = root / "valid"
    missing_status = root / "missing-status"
    invalid_configuration = root / "invalid-configuration"
    _experiment(valid)
    missing_status.mkdir(parents=True)
    (missing_status / "config.json").write_text("{}\n", encoding="utf-8")
    invalid_configuration.mkdir()
    (invalid_configuration / "config.json").write_text("{broken\n", encoding="utf-8")
    (invalid_configuration / "status.json").write_text("{}\n", encoding="utf-8")

    result = discover_experiments(root)

    assert [experiment.path for experiment in result.experiments] == [valid.resolve()]
    assert [diagnostic.path for diagnostic in result.diagnostics] == [
        invalid_configuration.resolve(),
        missing_status.resolve(),
    ]
    assert "Could not read JSON object" in result.diagnostics[0].message
    assert result.diagnostics[1].message == "missing status.json"


def test_discovery_does_not_follow_directory_symlinks(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    outside = tmp_path / "outside" / "experiment"
    _experiment(outside)
    (root / "linked").symlink_to(outside.parent, target_is_directory=True)

    result = discover_experiments(root)

    assert result.experiments == ()
    assert result.diagnostics == ()


def test_discovery_does_not_modify_experiment_metadata(tmp_path):
    experiment = tmp_path / "experiment"
    _experiment(experiment)
    before = {name: (experiment / name).read_bytes() for name in ("config.json", "status.json")}

    discover_experiments(tmp_path)

    assert {name: (experiment / name).read_bytes() for name in ("config.json", "status.json")} == before


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_discovery_rejects_an_invalid_root(tmp_path, root_kind):
    root = tmp_path / root_kind
    if root_kind == "file":
        root.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(DiscoveryError, match="Discovery root"):
        discover_experiments(root)
