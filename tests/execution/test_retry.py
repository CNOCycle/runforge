"""Tests for preparing an existing RunForge experiment for retry."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import runforge.execution.retry as retry_module
import runforge.infrastructure.storage as experiment_storage
from runforge.execution.retry import RetryError, prepare_retry
from runforge.execution.worker import run_experiment
from runforge.infrastructure.claims import try_acquire_claim
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand, ExperimentStatus
from tests.support import create_git_repository


FAILURE_EXIT_CODE = 7
SECOND_ATTEMPT = 2
ACTIVE_ATTEMPT = 3


def _experiment(tmp_path: Path, script: str = "print('done')\n") -> Path:
    repository = create_git_repository(tmp_path / "repository", {"train.py": script})
    return plan_experiment(
        PlanRequest(
            name="retry",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )


def _status(experiment: Path) -> ExperimentStatus:
    return ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))


def _save_status(experiment: Path, status: ExperimentStatus) -> None:
    save_json_object(experiment / "status.json", status.to_dict())


def _failed_status(experiment: Path, *, attempt: int = 1) -> ExperimentStatus:
    status = ExperimentStatus(
        state="failed",
        attempt=attempt,
        updated_at="2026-01-01T00:02:00Z",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:02:00Z",
        exit_code=FAILURE_EXIT_CODE,
        error="Command exited with status 7",
    )
    _save_status(experiment, status)
    return status


@pytest.mark.parametrize("force", [False, True])
def test_prepare_retry_of_an_unclaimed_failed_experiment_is_never_reported_as_forced(tmp_path, force):
    experiment = _experiment(tmp_path)
    _failed_status(experiment)
    layout = experiment_storage.ExperimentDirectory.resolve(experiment)
    assert not layout.claim.exists()

    preparation = prepare_retry(experiment, force=force)

    # Nothing was overridden, so --force must not claim otherwise.
    assert preparation.forced is False
    assert preparation.status.state == "init"


def test_prepare_retry_archives_failed_attempt_and_preserves_attempt_counter(tmp_path):
    experiment = _experiment(
        tmp_path,
        (
            "from pathlib import Path\n"
            "import os\n"
            "import sys\n"
            "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('partial.txt').write_text('partial')\n"
            "print('out')\n"
            "print('err', file=sys.stderr)\n"
            "raise SystemExit(7)\n"
        ),
    )
    configuration_before = (experiment / "config.json").read_bytes()
    assert run_experiment(experiment) == FAILURE_EXIT_CODE
    failed = _status(experiment)

    preparation = prepare_retry(experiment)

    archive = experiment / "attempt-0001"
    prepared = _status(experiment)
    archived = ExperimentStatus.from_dict(load_json_object(archive / "status.snapshot.json"))
    assert preparation.experiment == experiment
    assert preparation.archive == archive
    assert preparation.previous_status == failed
    assert preparation.status == prepared
    assert preparation.forced is False
    assert archived == failed
    assert prepared.state == "init"
    assert prepared.attempt == 1
    assert prepared.started_at is None
    assert prepared.finished_at is None
    assert prepared.exit_code is None
    assert prepared.error is None
    assert (archive / "stdout.log").read_text(encoding="utf-8") == "out\n"
    assert (archive / "stderr.log").read_text(encoding="utf-8") == "err\n"
    assert (archive / "artifacts" / "partial.txt").read_text(encoding="utf-8") == "partial"
    assert not (archive / "status.json").exists()
    assert not (archive / "config.json").exists()
    assert not (experiment / "stdout.log").exists()
    assert not (experiment / "stderr.log").exists()
    assert list((experiment / "artifacts").iterdir()) == []
    assert (experiment / "config.json").read_bytes() == configuration_before

    assert run_experiment(experiment) == FAILURE_EXIT_CODE
    assert _status(experiment).attempt == SECOND_ATTEMPT
    assert archive.is_dir()


def test_prepare_retry_preserves_claimed_failed_experiment_until_forced(tmp_path):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    layout = experiment_storage.ExperimentDirectory.resolve(experiment)
    claim = try_acquire_claim(layout, owner="stopped-worker")
    assert claim is not None

    # The operator must be told which process to check before forcing recovery.
    with pytest.raises(RetryError, match="already claimed.*held by stopped-worker since "):
        prepare_retry(experiment)

    assert _status(experiment) == failed
    assert layout.claim.is_dir()
    assert not tuple(experiment.glob("attempt-*"))

    preparation = prepare_retry(experiment, force=True)

    assert preparation.forced is True
    assert not layout.claim.exists()
    assert preparation.status.state == "init"
    assert (preparation.archive / "status.snapshot.json").is_file()


def test_prepare_retry_requires_force_for_inprogress_experiment(tmp_path):
    experiment = _experiment(tmp_path)
    active = ExperimentStatus(
        state="inprogress",
        attempt=ACTIVE_ATTEMPT,
        updated_at="2026-01-01T00:01:00Z",
        started_at="2026-01-01T00:00:00Z",
    )
    _save_status(experiment, active)
    (experiment / "stdout.log").write_text("partial output\n", encoding="utf-8")
    (experiment / "artifacts" / "partial.txt").write_text("partial", encoding="utf-8")

    with pytest.raises(RetryError, match="force=True"):
        prepare_retry(experiment)

    claim = try_acquire_claim(experiment_storage.ExperimentDirectory.resolve(experiment), owner="dead-worker")
    assert claim is not None
    with pytest.raises(RetryError, match="force=True"):
        prepare_retry(experiment)
    assert (experiment / "claim").is_dir()

    assert _status(experiment) == active
    assert not tuple(experiment.glob("attempt-*"))
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "partial output\n"

    preparation = prepare_retry(experiment, force=True)

    assert preparation.forced is True
    assert not (experiment / "claim").exists()
    assert preparation.previous_status == active
    assert preparation.status.state == "init"
    assert preparation.status.attempt == ACTIVE_ATTEMPT
    assert (preparation.archive / "stdout.log").read_text(encoding="utf-8") == "partial output\n"
    assert (preparation.archive / "artifacts" / "partial.txt").read_text(encoding="utf-8") == "partial"


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("created", "executed with run"),
        ("init", "executed with run"),
        ("completed", "cannot be retried"),
    ],
)
def test_prepare_retry_rejects_ineligible_states_without_changes(tmp_path, state, message):
    experiment = _experiment(tmp_path)
    original = _status(experiment)
    if state == "init":
        original = replace(original, state="init")
    elif state == "completed":
        original = replace(
            original,
            state="completed",
            attempt=1,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
            exit_code=0,
        )
    _save_status(experiment, original)
    (experiment / "artifacts" / "result.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(RetryError, match=message):
        prepare_retry(experiment, force=True)

    assert _status(experiment) == original
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "keep"
    assert not tuple(experiment.glob("attempt-*"))


def test_prepare_retry_rejects_existing_attempt_archive_without_changes(tmp_path):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    (experiment / "stdout.log").write_text("keep\n", encoding="utf-8")
    archive = experiment / "attempt-0001"
    archive.mkdir()
    (archive / "marker.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(RetryError, match="archive already exists"):
        prepare_retry(experiment)

    assert _status(experiment) == failed
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "keep\n"
    assert (archive / "marker.txt").read_text(encoding="utf-8") == "existing"


def test_prepare_retry_rolls_back_outputs_when_status_reset_fails(tmp_path, monkeypatch):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    (experiment / "stdout.log").write_text("out\n", encoding="utf-8")
    (experiment / "stderr.log").write_text("err\n", encoding="utf-8")
    (experiment / "artifacts" / "partial.txt").write_text("partial", encoding="utf-8")
    original_save = experiment_storage.save_json_object

    def fail_status_reset(path: Path, value):
        if Path(path) == experiment / "status.json":
            raise OSError("disk full")
        original_save(path, value)

    monkeypatch.setattr(experiment_storage, "save_json_object", fail_status_reset)

    with pytest.raises(RetryError, match="disk full"):
        prepare_retry(experiment)

    assert _status(experiment) == failed
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "out\n"
    assert (experiment / "stderr.log").read_text(encoding="utf-8") == "err\n"
    assert (experiment / "artifacts" / "partial.txt").read_text(encoding="utf-8") == "partial"
    assert not tuple(experiment.glob("attempt-*"))


def test_archive_rejects_a_dangling_history_symlink(tmp_path):
    """A dangling symlink occupies the path while reporting exists() as false.

    prepare_retry cannot reach this today, because the history directory is the
    experiment directory and that is validated earlier. The guard is exercised
    directly so a later change to that earlier validation cannot silently leave
    mkdir() to fail with a less specific error.
    """
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    history = tmp_path / "dangling"
    history.symlink_to(tmp_path / "missing")
    status = ExperimentStatus(state="failed", attempt=1, updated_at="2026-01-01T00:00:00Z")

    with pytest.raises(RetryError, match="Retry history path is not a directory"):
        retry_module._archive_and_reset(
            experiment_storage.ExperimentDirectory(experiment),
            history / "attempt-0001",
            status,
            status,
        )
