"""Tests for the read-only discovery CLI."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from runforge.cli import main
from runforge.infrastructure.claims import ExperimentClaim
from runforge.infrastructure.clock import utc_now
from runforge.infrastructure.json_store import save_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.source import GitSource
from tests.support import create_git_repository


CLI_ERROR_EXIT = 2
COMMAND_FAILURE_EXIT = 1


def _repository(tmp_path: Path) -> Path:
    return create_git_repository(tmp_path / "repository", {"tracked.txt": "tracked\n"})


def _plan(repository: Path, reports: Path, *, name: str, script: str) -> Path:
    return plan_experiment(
        PlanRequest(
            name=name,
            command=ExperimentCommand.argv(("python", "-c", script)),
            source_path=repository,
            output_root=reports,
        )
    )


def _experiment(
    path: Path, *, name: str, state: str = "created", attempt: int = 0, started_at: str | None = None
) -> None:
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
        started_at=started_at,
    )
    save_json_object(path / "config.json", configuration.to_dict())
    save_json_object(path / "status.json", status.to_dict())


def _claim(path: Path, *, owner: str) -> None:
    layout = ExperimentDirectory(path)
    layout.claim.mkdir()
    save_json_object(layout.claim_file, ExperimentClaim(token="t", owner=owner, acquired_at=utc_now()).to_dict())


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
        f"  paths: {root.resolve()}",
        "  stale cutoff: not set",
        f"Experiments discovered under: {root.resolve()}",
        f"created | attempt=0 | name=new run | source=main@aaaaaaaa | held=false | path={first.resolve()}",
        f"completed | attempt=1 | name=finished run | source=main@aaaaaaaa | held=false | path={second.resolve()}",
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
        f"  paths: {root.resolve()}",
        "  stale cutoff: not set",
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
    _experiment(invalid, name="invalid")
    (invalid / "status.json").unlink()

    assert main(["discover", str(root)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert (
        f"created | attempt=0 | name=valid | source=main@aaaaaaaa | held=false | path={valid.resolve()}" in captured.out
    )
    assert "  invalid: 1" in captured.out
    assert captured.err == f"invalid | path={invalid.resolve()} | error=missing status.json\n"


def test_discover_cli_rejects_a_missing_root(tmp_path, capsys):
    root = tmp_path / "missing"

    assert main(["discover", str(root)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert f"  paths: {root.resolve()}" in captured.out
    assert "error: Discovery root" in captured.err


def test_discover_cli_rejects_both_older_than_and_updated_before(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["discover", "--older-than", "6h", "--started-before", "2026-08-01T00:00:00Z"])

    assert raised.value.code == CLI_ERROR_EXIT
    assert "not allowed with argument" in capsys.readouterr().err


def test_discover_cli_rejects_a_malformed_duration(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["discover", "--older-than", "6"])

    assert raised.value.code == CLI_ERROR_EXIT
    assert "must be a positive integer followed by s, m, h, or d" in capsys.readouterr().err


def test_discover_cli_rejects_a_malformed_timestamp(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["discover", "--started-before", "not-a-timestamp"])

    assert raised.value.code == CLI_ERROR_EXIT
    assert "must be a UTC ISO 8601 timestamp" in capsys.readouterr().err


def test_discover_cli_with_older_than_flags_a_stale_inprogress_experiment(tmp_path, capsys):
    root = tmp_path / "reports"
    started_at = (datetime.now(timezone.utc) - timedelta(hours=8, minutes=12)).isoformat().replace("+00:00", "Z")
    stale = root / "stale"
    _experiment(stale, name="stale-run", state="inprogress", started_at=started_at)
    _claim(stale, owner=f"{socket.gethostname()}:{os.getpid()}")
    fresh = root / "fresh"
    _experiment(
        fresh,
        name="fresh-run",
        state="inprogress",
        started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )

    assert main(["discover", str(root), "--older-than", "6h"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "  stale cutoff: " in "\n".join(lines)
    stale_line_index = next(
        index for index, line in enumerate(lines) if line.startswith("inprogress") and "name=stale-run" in line
    )
    assert lines[stale_line_index + 1] == (
        f"  stale: inprogress for 8h12m, started {started_at} — "
        f"process appears alive: PID {os.getpid()} running on {socket.gethostname()}"
    )
    fresh_line_index = next(
        index for index, line in enumerate(lines) if line.startswith("inprogress") and "name=fresh-run" in line
    )
    assert not lines[fresh_line_index + 1].startswith("  stale:")
    assert "  stale: 1" in lines


def test_discover_cli_with_updated_before_uses_an_explicit_cutoff(tmp_path, capsys):
    root = tmp_path / "reports"
    stale = root / "stale"
    _experiment(stale, name="stale-run", state="inprogress", started_at="2020-01-01T00:00:00.000000Z")

    assert main(["discover", str(root), "--started-before", "2026-01-01T00:00:00Z"]) == 0

    output = capsys.readouterr().out
    assert "  stale: 1" in output
    assert "cannot verify liveness: claim metadata is missing or unreadable" in output


def test_discover_cli_without_staleness_flags_never_reads_a_claim_file(tmp_path, capsys):
    root = tmp_path / "reports"
    old = root / "old-inprogress"
    _experiment(old, name="old-run", state="inprogress", started_at="2020-01-01T00:00:00.000000Z")

    assert main(["discover", str(root)]) == 0

    output = capsys.readouterr().out
    assert "  stale cutoff: not set" in output
    assert "  stale:" not in output
