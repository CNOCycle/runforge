"""Tests for the read-only discovery CLI."""

from __future__ import annotations

from pathlib import Path

from runforge.cli import main
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.models.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.models.source import GitSource
from runforge.planning.planner import PlanRequest, plan_experiment
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
        "  execute: disabled",
        "  stream output: disabled",
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
        "  execute: disabled",
        "  stream output: disabled",
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


def test_discover_execute_runs_created_plans_and_skips_other_states(tmp_path, capsys):
    repository = _repository(tmp_path)
    reports = tmp_path / "reports"
    created = _plan(
        repository,
        reports,
        name="created",
        script="print('streamed output', flush=True)",
    )
    skipped = _plan(repository, reports, name="initialized", script="raise SystemExit(9)")
    skipped_status = ExperimentStatus(
        state="init",
        attempt=0,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_json_object(skipped / "status.json", skipped_status.to_dict())

    assert main(["discover", str(reports), "--execute", "--stream-output"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "  execute: enabled" in captured.out
    assert "  stream output: enabled" in captured.out
    assert f"Selected experiment (1/1): {created}" in captured.out
    assert f"Preparing experiment: {created}" in captured.out
    assert "Executing command: python -c" in captured.out
    assert "  output mode: streaming and logging" in captured.out
    assert "streamed output" in captured.out
    assert f"Experiment completed with exit code 0: {created}" in captured.out
    assert "  selected: 1" in captured.out
    assert "  completed: 1" in captured.out
    assert "  failed: 0" in captured.out
    assert "  skipped: 1" in captured.out
    assert ExperimentStatus.from_dict(load_json_object(created / "status.json")).state == "completed"
    assert ExperimentStatus.from_dict(load_json_object(skipped / "status.json")) == skipped_status


def test_discover_execute_continues_after_worker_and_command_failures(tmp_path, capsys):
    repository = _repository(tmp_path)
    reports = tmp_path / "reports"
    worker_failure = reports / "a-worker-failure"
    _experiment(worker_failure, name="worker failure")
    command_failure = _plan(repository, reports, name="b-command-failure", script="raise SystemExit(7)")
    success = _plan(repository, reports, name="z-success", script="print('success')")

    assert main(["discover", str(reports), "--execute"]) == COMMAND_FAILURE_EXIT

    captured = capsys.readouterr()
    first = captured.out.index(f"Selected experiment (1/3): {worker_failure.resolve()}")
    second = captured.out.index(f"Selected experiment (2/3): {command_failure}")
    third = captured.out.index(f"Selected experiment (3/3): {success}")
    assert first < second < third
    assert "  selected: 3" in captured.out
    assert "  completed: 1" in captured.out
    assert "  failed: 2" in captured.out
    assert "  skipped: 0" in captured.out
    assert f"Experiment failed: {worker_failure.resolve()}:" in captured.err
    assert f"Experiment failed with exit code 7: {command_failure}" in captured.err
    assert ExperimentStatus.from_dict(load_json_object(worker_failure / "status.json")).state == "failed"
    assert ExperimentStatus.from_dict(load_json_object(command_failure / "status.json")).state == "failed"
    assert ExperimentStatus.from_dict(load_json_object(success / "status.json")).state == "completed"


def test_discover_execute_gives_invalid_metadata_exit_precedence(tmp_path, capsys):
    repository = _repository(tmp_path)
    reports = tmp_path / "reports"
    failed = _plan(repository, reports, name="failure", script="raise SystemExit(4)")
    invalid = reports / "invalid"
    invalid.mkdir()
    (invalid / "config.json").write_text("{}\n", encoding="utf-8")

    assert main(["discover", str(reports), "--execute"]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert f"Experiment failed with exit code 4: {failed}" in captured.err
    assert f"invalid | path={invalid.resolve()} | error=missing status.json" in captured.err
    assert "  failed: 1" in captured.out
    assert "  invalid: 1" in captured.out


def test_discover_execute_succeeds_when_no_created_plan_is_eligible(tmp_path, capsys):
    reports = tmp_path / "reports"
    completed = reports / "completed"
    _experiment(completed, name="completed", state="completed", attempt=1)
    status_before = (completed / "status.json").read_bytes()

    assert main(["discover", str(reports), "--execute"]) == 0

    captured = capsys.readouterr()
    assert "  selected: 0" in captured.out
    assert "  completed: 0" in captured.out
    assert "  failed: 0" in captured.out
    assert "  skipped: 1" in captured.out
    assert (completed / "status.json").read_bytes() == status_before


def test_discover_stream_output_requires_execute(tmp_path, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()

    assert main(["discover", str(reports), "--stream-output"]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert "  execute: disabled" in captured.out
    assert "  stream output: enabled" in captured.out
    assert captured.err == "error: --stream-output requires --execute\n"
