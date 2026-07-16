"""Tests for the read-only discovery CLI."""

from __future__ import annotations

from pathlib import Path

from runforge.cli import main
from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.json_store import save_json_object
from runforge.source_metadata import GitSource


CLI_ERROR_EXIT = 2


def _experiment(path: Path, *, name: str, state: str = "created", attempt: int = 0) -> None:
    path.mkdir(parents=True)
    configuration = ExperimentConfiguration(
        experiment_id=path.name,
        name=name,
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


def test_discover_cli_lists_experiments_and_state_totals(tmp_path, capsys):
    root = tmp_path / "reports"
    first = root / "a" / "first"
    second = root / "z" / "second"
    _experiment(second, name="finished run", state="completed", attempt=1)
    _experiment(first, name="new run")

    assert main(["discover", str(root)]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "RunForge discover effective arguments:",
        f"  root: {root.resolve()}",
        f"Experiments discovered under: {root.resolve()}",
        f"created | attempt=0 | name=new run | source=main@aaaaaaaa | path={first.resolve()}",
        f"completed | attempt=1 | name=finished run | source=main@aaaaaaaa | path={second.resolve()}",
        "Summary:",
        "  created: 1",
        "  init: 0",
        "  inprogress: 0",
        "  completed: 1",
        "  failed: 0",
        "  invalid: 0",
    ]


def test_discover_cli_reports_an_empty_default_root(tmp_path, monkeypatch, capsys):
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.chdir(root)

    assert main(["discover"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "RunForge discover effective arguments:",
        f"  root: {root.resolve()}",
        f"Experiments discovered under: {root.resolve()}",
        "No experiments found.",
        "Summary:",
        "  created: 0",
        "  init: 0",
        "  inprogress: 0",
        "  completed: 0",
        "  failed: 0",
        "  invalid: 0",
    ]


def test_discover_cli_lists_valid_plans_and_reports_invalid_candidates(tmp_path, capsys):
    root = tmp_path / "reports"
    valid = root / "valid"
    invalid = root / "invalid"
    _experiment(valid, name="valid")
    invalid.mkdir()
    (invalid / "config.json").write_text("{}\n", encoding="utf-8")

    assert main(["discover", str(root)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert f"created | attempt=0 | name=valid | source=main@aaaaaaaa | path={valid.resolve()}" in captured.out
    assert "  invalid: 1" in captured.out
    assert captured.err == f"invalid | path={invalid.resolve()} | error=missing status.json\n"


def test_discover_cli_rejects_a_missing_root(tmp_path, capsys):
    root = tmp_path / "missing"

    assert main(["discover", str(root)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert f"  root: {root.resolve()}" in captured.out
    assert "error: Discovery root" in captured.err
