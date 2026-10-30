"""Tests for preparing an existing RunForge experiment for retry."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import runforge.execution.retry as retry_module
import runforge.infrastructure.storage as experiment_storage
from runforge.execution.errors import ExperimentNotRunnableError
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
INTERRUPTED_EXIT_CODE = 9


def _experiment(tmp_path: Path, script: str = "print('done')\n") -> Path:
    repository = create_git_repository(tmp_path / "repository", {"train.py": script})
    return plan_experiment(
        PlanRequest(
            name="retry",
            command=ExperimentCommand.argv((sys.executable, "train.py")),
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

    with pytest.raises(ExperimentNotRunnableError, match=message):
        retry_module.acquire_retry_claim(experiment_storage.ExperimentDirectory.resolve(experiment), force=True)

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


@pytest.mark.parametrize("reuse_artifacts", [False, True])
def test_prepare_retry_rolls_back_outputs_when_status_reset_fails(tmp_path, monkeypatch, reuse_artifacts):
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
        prepare_retry(experiment, reuse_artifacts=reuse_artifacts)

    assert _status(experiment) == failed
    assert (experiment / "stdout.log").read_text(encoding="utf-8") == "out\n"
    assert (experiment / "stderr.log").read_text(encoding="utf-8") == "err\n"
    assert (experiment / "artifacts" / "partial.txt").read_text(encoding="utf-8") == "partial"
    assert not tuple(experiment.glob("attempt-*"))
    assert not tuple(experiment.glob(".attempt-*.tmp-*"))


def test_prepare_retry_reuses_an_independent_tree_and_preserves_logs_and_status(tmp_path):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    artifacts = experiment / "artifacts"
    (artifacts / "nested" / "empty").mkdir(parents=True)
    checkpoint = artifacts / "nested" / "checkpoint.bin"
    checkpoint.write_bytes(b"saved\x00progress")
    (experiment / "stdout.log").write_text("old stdout\n")
    (experiment / "stderr.log").write_text("old stderr\n")
    immutable = {name: (experiment / name).read_bytes() for name in ("config.json", "cmd.sh")}

    preparation = prepare_retry(experiment, reuse_artifacts=True)

    archive = preparation.archive
    assert _status(experiment).state == "init"
    assert _status(experiment).attempt == 1
    assert ExperimentStatus.from_dict(load_json_object(archive / "status.snapshot.json")) == failed
    assert checkpoint.read_bytes() == b"saved\x00progress"
    assert (artifacts / "nested" / "empty").is_dir()
    checkpoint.write_bytes(b"new progress")
    assert (archive / "artifacts" / "nested" / "checkpoint.bin").read_bytes() == b"saved\x00progress"
    assert (archive / "stdout.log").read_text() == "old stdout\n"
    assert (archive / "stderr.log").read_text() == "old stderr\n"
    assert not (experiment / "stdout.log").exists()
    assert not (experiment / "stderr.log").exists()
    assert {name: (experiment / name).read_bytes() for name in immutable} == immutable
    assert {path.name for path in archive.iterdir()} == {
        "artifacts",
        "status.snapshot.json",
        "stdout.log",
        "stderr.log",
    }
    assert not tuple(experiment.glob(".attempt-*.tmp-*"))


def test_prepare_retry_can_reuse_an_empty_artifacts_directory(tmp_path):
    experiment = _experiment(tmp_path)
    _failed_status(experiment)

    preparation = prepare_retry(experiment, reuse_artifacts=True)

    assert list((experiment / "artifacts").iterdir()) == []
    assert list((preparation.archive / "artifacts").iterdir()) == []


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "nested-symlink", "fifo"])
def test_reuse_rejects_unsupported_artifact_trees_before_moving_outputs(tmp_path, kind):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    artifacts = experiment / "artifacts"
    (experiment / "stdout.log").write_text("keep output\n")
    external = tmp_path / "external"
    external.mkdir()
    (external / "checkpoint").write_text("keep external\n")
    if kind in {"missing", "file", "symlink"}:
        artifacts.rmdir()
    if kind == "file":
        artifacts.write_text("not a directory")
    elif kind == "symlink":
        artifacts.symlink_to(external, target_is_directory=True)
    elif kind == "nested-symlink":
        (artifacts / "checkpoint").symlink_to(external / "checkpoint")
    elif kind == "fifo":
        os.mkfifo(artifacts / "checkpoint")

    with pytest.raises(RetryError, match="Artifact reuse"):
        prepare_retry(experiment, reuse_artifacts=True)

    assert _status(experiment) == failed
    assert (experiment / "stdout.log").read_text() == "keep output\n"
    assert (external / "checkpoint").read_text() == "keep external\n"
    assert not tuple(experiment.glob("attempt-*"))
    assert not tuple(experiment.glob(".attempt-*.tmp-*"))


def test_reuse_copy_failure_removes_partial_seed_and_preserves_originals(tmp_path, monkeypatch):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    (experiment / "artifacts" / "checkpoint").write_text("original progress")
    (experiment / "stdout.log").write_text("old output")

    def fail_copy(_source, destination, **_options):
        destination.mkdir()
        (destination / "checkpoint").write_text("partial copy")
        raise OSError("copy ran out of space")

    monkeypatch.setattr(retry_module.shutil, "copytree", fail_copy)
    with pytest.raises(RetryError, match="copy ran out of space"):
        prepare_retry(experiment, reuse_artifacts=True)

    assert _status(experiment) == failed
    assert (experiment / "artifacts" / "checkpoint").read_text() == "original progress"
    assert (experiment / "stdout.log").read_text() == "old output"
    assert not tuple(experiment.glob("attempt-*"))
    assert not tuple(experiment.glob(".attempt-*.tmp-*"))


def test_reuse_revalidates_the_copied_tree_before_publishing(tmp_path, monkeypatch):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    original_copy = retry_module.shutil.copytree
    external = tmp_path / "external"
    external.write_text("keep")

    def copy_with_link(source, destination, **options):
        original_copy(source, destination, **options)
        (destination / "unexpected-link").symlink_to(external)

    monkeypatch.setattr(retry_module.shutil, "copytree", copy_with_link)
    with pytest.raises(RetryError, match="only regular files and directories"):
        prepare_retry(experiment, reuse_artifacts=True)

    assert _status(experiment) == failed
    assert list((experiment / "artifacts").iterdir()) == []
    assert external.read_text() == "keep"
    assert not tuple(experiment.glob("attempt-*"))
    assert not tuple(experiment.glob(".attempt-*.tmp-*"))


def test_reuse_rollback_failure_diagnoses_and_preserves_the_archive(tmp_path, monkeypatch):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    artifacts = experiment / "artifacts"
    (artifacts / "checkpoint").write_text("saved progress")
    original_remove = retry_module.shutil.rmtree

    def fail_status_reset(_layout, _status):
        raise OSError("status write failed")

    def fail_seed_removal(path, *args, **kwargs):
        if path == artifacts:
            raise OSError("cannot remove copied artifacts")
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(experiment_storage.ExperimentDirectory, "save_status", fail_status_reset)
    monkeypatch.setattr(retry_module.shutil, "rmtree", fail_seed_removal)
    with pytest.raises(RetryError, match="rollback also failed.*preserve and inspect") as raised:
        prepare_retry(experiment, reuse_artifacts=True)

    archive = experiment / "attempt-0001"
    assert str(archive) in str(raised.value)
    assert _status(experiment) == failed
    assert (archive / "artifacts" / "checkpoint").read_text() == "saved progress"
    assert (artifacts / "checkpoint").read_text() == "saved progress"


@pytest.mark.parametrize("boundary", ["copy", "status-reset"])
def test_abrupt_retry_preparation_preserves_checkpoints_and_blocks_reentry(tmp_path, boundary):
    experiment = _experiment(tmp_path)
    failed = _failed_status(experiment)
    (experiment / "artifacts" / "checkpoint").write_text("original progress")
    script = """
import os
import sys
from pathlib import Path
import runforge.execution.retry as retry
def stop_copy(source, destination, **options):
    destination.mkdir()
    (destination / 'checkpoint').write_text('partial copy')
    os._exit(9)
def stop_reset(self, status):
    os._exit(9)
if sys.argv[2] == 'copy':
    retry.shutil.copytree = stop_copy
else:
    retry.ExperimentDirectory.save_status = stop_reset
retry.prepare_retry(Path(sys.argv[1]), reuse_artifacts=True)
"""

    result = subprocess.run([sys.executable, "-c", script, str(experiment), boundary], check=False)

    assert result.returncode == INTERRUPTED_EXIT_CODE
    assert _status(experiment) == failed
    if boundary == "copy":
        assert (experiment / "artifacts" / "checkpoint").read_text() == "original progress"
        assert tuple(experiment.glob(".attempt-*.tmp-*"))
        diagnosis = "Interrupted retry preparation"
    else:
        assert (experiment / "attempt-0001" / "artifacts" / "checkpoint").read_text() == "original progress"
        assert (experiment / "artifacts" / "checkpoint").read_text() == "original progress"
        diagnosis = "Retry archive already exists"
    with pytest.raises(RetryError, match=diagnosis):
        prepare_retry(experiment, reuse_artifacts=True)
    assert _status(experiment) == failed


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
