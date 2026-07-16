"""Tests for the RunForge retry command."""

from __future__ import annotations

from pathlib import Path

from runforge.cli import main
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.models.experiment import ExperimentStatus
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


def test_retry_archives_failed_attempt_prints_effective_arguments_and_executes(tmp_path, capsys):
    marker = tmp_path / "attempt.marker"
    repository = _repository(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "import sys\n"
            "artifact = Path(os.environ['RUNFORGE_ARTIFACT_DIR'])\n"
            "marker = Path(os.environ['MARKER'])\n"
            "if marker.exists():\n"
            "    artifact.joinpath('result.txt').write_text('success')\n"
            "    print('retry succeeded', flush=True)\n"
            "else:\n"
            "    marker.write_text('created')\n"
            "    artifact.joinpath('partial.txt').write_text('partial')\n"
            "    print('first attempt failed', flush=True)\n"
            "    print('first attempt error', file=sys.stderr, flush=True)\n"
            "    raise SystemExit(7)\n"
        ),
    )
    environment_file = tmp_path / "runforge.env"
    environment_file.write_text(f"API_TOKEN=very-secret\nMARKER={marker}\n", encoding="utf-8")
    experiment = _plan(repository, tmp_path / "reports", environment_file, capsys)
    assert main(["run", str(experiment)]) == FAILURE_EXIT_CODE
    capsys.readouterr()

    assert main(["retry", "--stream-output", str(experiment)]) == 0

    captured = capsys.readouterr()
    output = captured.out
    archive = experiment / "attempt-0001"
    assert captured.err == ""
    assert "RunForge retry effective arguments:" in output
    assert f"  experiment: {experiment}" in output
    assert "  force: disabled" in output
    assert "  stream output: enabled" in output
    assert "  current state: failed" in output
    assert "  current attempt: 1" in output
    assert "  next attempt: 2" in output
    assert "  recorded command: python train.py" in output
    assert "  environment keys: API_TOKEN, MARKER" in output
    assert f"  artifact directory: {experiment / 'artifacts'}" in output
    assert f"Previous attempt archived at: {archive}" in output
    assert f"Preparing experiment: {experiment}" in output
    assert "Executing command: python train.py" in output
    assert "retry succeeded" in output
    assert f"Experiment completed with exit code 0: {experiment}" in output
    assert "very-secret" not in output
    assert output.index("RunForge retry effective arguments:") < output.index("Previous attempt archived at:")
    assert output.index("Previous attempt archived at:") < output.index("Preparing experiment:")
    assert output.index("Executing command:") < output.index("retry succeeded")
    assert _status(experiment).state == "completed"
    assert _status(experiment).attempt == SECOND_ATTEMPT
    assert (archive / "stdout.log").read_text(encoding="utf-8") == "first attempt failed\n"
    assert (archive / "stderr.log").read_text(encoding="utf-8") == "first attempt error\n"
    assert (archive / "artifacts" / "partial.txt").read_text(encoding="utf-8") == "partial"
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "success"


def test_retry_requires_force_for_inprogress_and_warns_before_execution(tmp_path, capsys):
    repository = _repository(tmp_path, "print('forced retry ran', flush=True)\n")
    environment_file = tmp_path / "runforge.env"
    environment_file.write_text("RUN_MODE=test\n", encoding="utf-8")
    experiment = _plan(repository, tmp_path / "reports", environment_file, capsys)
    active = ExperimentStatus(
        state="inprogress",
        attempt=ACTIVE_ATTEMPT,
        updated_at="2026-01-01T00:01:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    save_json_object(experiment / "status.json", active.to_dict())
    (experiment / "stdout.log").write_text("partial output\n", encoding="utf-8")
    (experiment / "artifacts" / "partial.txt").write_text("partial", encoding="utf-8")

    assert main(["retry", str(experiment)]) == CLI_ERROR_EXIT
    captured = capsys.readouterr()
    assert "RunForge retry effective arguments:" in captured.out
    assert "  force: disabled" in captured.out
    assert "force=True is required" in captured.err
    assert _status(experiment) == active
    assert not tuple(experiment.glob("attempt-*"))

    assert main(["retry", "--force", "--stream-output", str(experiment)]) == 0

    captured = capsys.readouterr()
    archive = experiment / "attempt-0002"
    assert "  force: enabled" in captured.out
    assert "  current state: inprogress" in captured.out
    assert "  current attempt: 2" in captured.out
    assert "  next attempt: 3" in captured.out
    assert f"Previous attempt archived at: {archive}" in captured.out
    assert "forced retry ran" in captured.out
    assert captured.err.splitlines() == [
        "warning: Forced retry cannot prove that the previous inprogress worker has stopped"
    ]
    assert _status(experiment).state == "completed"
    assert _status(experiment).attempt == RETRIED_ACTIVE_ATTEMPT
    assert (archive / "stdout.log").read_text(encoding="utf-8") == "partial output\n"


def test_retry_rejects_completed_experiment_without_archiving(tmp_path, capsys):
    repository = _repository(tmp_path, "print('done')\n")
    environment_file = tmp_path / "runforge.env"
    environment_file.write_text("RUN_MODE=test\n", encoding="utf-8")
    experiment = _plan(repository, tmp_path / "reports", environment_file, capsys)
    assert main(["run", str(experiment)]) == 0
    completed = _status(experiment)
    capsys.readouterr()

    assert main(["retry", str(experiment)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert "  current state: completed" in captured.out
    assert "Completed experiments cannot be retried" in captured.err
    assert _status(experiment) == completed
    assert not tuple(experiment.glob("attempt-*"))
