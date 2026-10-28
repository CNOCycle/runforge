"""Tests for the RunForge retry command."""

from __future__ import annotations

from pathlib import Path

import pytest

from runforge.cli import main
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.schemas.experiment import ExperimentStatus
from tests.support import create_git_repository, planned_path


FAILURE_EXIT_CODE = 7
CLI_ERROR_EXIT = 2
SECOND_ATTEMPT = 2
ACTIVE_ATTEMPT = 2
RETRIED_ACTIVE_ATTEMPT = 3


def _repository(tmp_path: Path, script: str) -> Path:
    return create_git_repository(tmp_path / "repository", {"train.py": script})


def _status(experiment: Path) -> ExperimentStatus:
    return ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))


def _plan(repository: Path, reports: Path, environment_file: Path, capsys) -> Path:
    assert (
        main(
            [
                "plan",
                "--source-path",
                str(repository),
                "--out-dir",
                str(reports),
                "--env-file",
                str(environment_file),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    return planned_path(capsys.readouterr().out)


def test_retry_archives_failed_attempt_and_executes_with_aggregate_status(tmp_path, capsys):
    marker = tmp_path / "attempt.marker"
    repository = _repository(
        tmp_path,
        (
            "from pathlib import Path\nimport os\n"
            "marker = Path(os.environ['MARKER'])\n"
            "artifacts = Path(os.environ['RUNFORGE_ARTIFACT_DIR'])\n"
            "if not marker.exists():\n"
            "    marker.write_text('first')\n"
            "    artifacts.joinpath('partial.txt').write_text('partial')\n"
            "    print('first attempt failed')\n"
            "    raise SystemExit(7)\n"
            "artifacts.joinpath('result.txt').write_text('success')\n"
            "print('retry succeeded', flush=True)\n"
        ),
    )
    environment = tmp_path / "runforge.env"
    environment.write_text(f"API_TOKEN=very-secret\nMARKER={marker}\n")
    experiment = _plan(repository, tmp_path / "reports", environment, capsys)
    assert main(["run", str(experiment)]) == 1
    assert _status(experiment).exit_code == FAILURE_EXIT_CODE
    first = capsys.readouterr()
    assert "  environment keys: API_TOKEN, MARKER" in first.out
    assert f"  artifact directory: {experiment / 'artifacts'}" in first.out
    assert "  attempt: 1" in first.out
    assert "current attempt:" not in first.out
    assert "next attempt:" not in first.out
    assert "  force:" not in first.out
    assert "  max tasks: unlimited" in first.out
    assert "very-secret" not in first.out + first.err

    assert main(["retry", "-s", str(experiment)]) == 0

    captured = capsys.readouterr()
    archive = experiment / "attempt-0001"
    assert captured.err == ""
    assert "retry succeeded" in captured.out
    assert "very-secret" not in captured.out + captured.err
    assert "  environment keys: API_TOKEN, MARKER" in captured.out
    assert f"  artifact directory: {experiment / 'artifacts'}" in captured.out
    assert "  attempt: 2" in captured.out
    assert "current attempt:" not in captured.out
    assert "next attempt:" not in captured.out
    assert "  force: disabled" in captured.out
    assert "  max tasks:" not in captured.out
    assert captured.out.index("  attempt: 2") < captured.out.index("Executing command")
    assert captured.out.index("Executing command") < captured.out.index("retry succeeded")
    assert (archive / "status.snapshot.json").exists()
    assert (archive / "stdout.log").read_text() == "first attempt failed\n"
    assert (archive / "artifacts" / "partial.txt").read_text() == "partial"
    assert (experiment / "artifacts" / "result.txt").read_text() == "success"
    assert _status(experiment).state == "completed"
    assert _status(experiment).attempt == SECOND_ATTEMPT


def test_retry_force_on_an_unclaimed_failed_experiment_does_not_warn(tmp_path, capsys):
    repository = _repository(tmp_path, "raise SystemExit(7)\n")
    environment = tmp_path / "runforge.env"
    environment.write_text("MARKER=unused\n")
    experiment = _plan(repository, tmp_path / "reports", environment, capsys)
    assert main(["run", str(experiment)]) == 1
    capsys.readouterr()

    assert main(["retry", "-f", str(experiment)]) == 1

    assert "cannot prove" not in capsys.readouterr().err
    assert _status(experiment).attempt == SECOND_ATTEMPT


def test_retry_skips_inprogress_without_force_and_preserves_prior_attempt(tmp_path, capsys):
    repository = _repository(tmp_path, "print('forced retry ran')\n")
    environment = tmp_path / "runforge.env"
    environment.write_text("RUN_MODE=test\n")
    experiment = _plan(repository, tmp_path / "reports", environment, capsys)
    active = ExperimentStatus(
        state="inprogress", attempt=ACTIVE_ATTEMPT, updated_at="2026-01-01T00:01:00Z", started_at="2026-01-01T00:00:00Z"
    )
    save_json_object(experiment / "status.json", active.to_dict())
    (experiment / "stdout.log").write_text("partial output\n")

    assert main(["retry", str(experiment)]) == 0
    assert _status(experiment) == active
    assert not tuple(experiment.glob("attempt-*"))
    capsys.readouterr()

    assert main(["retry", "-f", str(experiment)]) == 0

    assert "cannot prove that the previous worker has stopped" in capsys.readouterr().err
    assert _status(experiment).state == "completed"
    assert _status(experiment).attempt == RETRIED_ACTIVE_ATTEMPT
    assert (experiment / "attempt-0002" / "stdout.log").read_text() == "partial output\n"


def test_retry_skips_completed_experiment_without_archiving(tmp_path, capsys):
    repository = _repository(tmp_path, "print('done')\n")
    environment = tmp_path / "runforge.env"
    environment.write_text("RUN_MODE=test\n")
    experiment = _plan(repository, tmp_path / "reports", environment, capsys)
    assert main(["run", str(experiment)]) == 0
    completed = _status(experiment)
    capsys.readouterr()

    assert main(["retry", str(experiment)]) == 0

    assert _status(experiment) == completed
    assert not tuple(experiment.glob("attempt-*"))
    assert "non-runnable: 1" in capsys.readouterr().out


@pytest.mark.parametrize("root_selection", [False, True])
def test_retry_archive_collision_is_invalid_work_without_a_new_attempt(tmp_path, capsys, root_selection):
    repository = _repository(tmp_path, "print('failed attempt')\nraise SystemExit(7)\n")
    environment = tmp_path / "runforge.env"
    environment.write_text("RUN_MODE=test\n")
    experiment = _plan(repository, tmp_path / "reports", environment, capsys)
    assert main(["run", str(experiment)]) == 1
    (experiment / "artifacts" / "partial.txt").write_text("partial output\n")
    archive = experiment / "attempt-0001"
    archive.mkdir()
    (archive / "marker.txt").write_text("existing archive\n")
    before = {path.relative_to(experiment): path.read_bytes() for path in experiment.rglob("*") if path.is_file()}
    capsys.readouterr()

    target = tmp_path / "reports" if root_selection else experiment
    assert main(["retry", str(target)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert "Retry archive already exists" in captured.err
    assert "  selected: 0" in captured.out
    assert "  failed: 0" in captured.out
    assert "  invalid: 1" in captured.out
    assert _status(experiment).state == "failed"
    assert _status(experiment).attempt == 1
    assert {
        path.relative_to(experiment): path.read_bytes() for path in experiment.rglob("*") if path.is_file()
    } == before
    assert not (experiment / "claim").exists()
