"""runforge onstart: confirmation, recovery after an interruption, and the lock."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from runforge.cli import commands, main
from runforge.execution.onstart import Interrupted
from runforge.execution.retry import RetryError
from runforge.execution.worker import WorkerResult
from tests.support import planned_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_ERROR_EXIT = 2
SECOND_ATTEMPT = 2
LISTED_TWICE = 2  # before the restarts and again after the final run

# First attempt: write a checkpoint, then hang until killed. A later attempt
# resumes from the checkpoint and finishes.
TRAIN = """#!/usr/bin/env bash
set -eu
checkpoint="$RUNFORGE_ARTIFACT_DIR/checkpoint.txt"
if [ -f "$SEEN_MARKER" ]; then
    if [ -f "$checkpoint" ]; then echo resumed > "$RUNFORGE_ARTIFACT_DIR/result.txt"
    else echo fresh > "$RUNFORGE_ARTIFACT_DIR/result.txt"; fi
    exit 0
fi
echo step-1 > "$checkpoint"
touch "$SEEN_MARKER"
exec sleep 600
"""


class Machine:
    """A temporary report root, a plan source, and the onstart processes started on it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reports = root / "reports"
        self.source = root / "source"
        self.marker = root / "seen"
        self.reports.mkdir()
        self.source.mkdir()
        (self.source / "train.sh").write_text(TRAIN, encoding="utf-8")
        monkeypatch.setenv("SEEN_MARKER", str(self.marker))
        monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT / "src"))
        self.processes: list[subprocess.Popen[str]] = []

    def plan(self) -> Path:
        result = subprocess.run(
            [
                sys.executable, "-m", "runforge", "plan", "--name", "ablation", "--source-mode", "directory-snapshot",
                "--source-path", str(self.source), "--out-dir", str(self.reports), "--", "bash", "train.sh",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        return planned_path(result.stdout)

    def start_onstart(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-m", "runforge", "onstart", "--single-node", str(self.reports)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )  # fmt: skip
        self.processes.append(process)
        return process

    def kill_everything(self) -> None:
        """SIGKILL every started onstart process group, like a machine that vanished."""
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()
        self.processes.clear()

    def status(self, experiment: Path) -> dict:
        return json.loads((experiment / "status.json").read_text(encoding="utf-8"))


@pytest.fixture
def machine(tmp_path, monkeypatch):
    instance = Machine(tmp_path, monkeypatch)
    yield instance
    instance.kill_everything()
    subprocess.run(["pkill", "-9", "-f", str(tmp_path)], check=False)


def _result(*, completed: int = 0, failed: int = 0, invalid: int = 0) -> WorkerResult:
    return WorkerResult(
        root=Path("."), candidates=1, selected=completed + failed, completed=completed, failed=failed,
        not_runnable=0, claim_contended=0, stale_skipped=0, deferred=0, invalid=invalid,
    )  # fmt: skip


def _wait_for(condition: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def test_without_the_confirmation_flag_it_warns_and_changes_nothing(machine, capsys):
    experiment = machine.plan()

    assert main(["onstart", str(machine.reports)]) == CLI_ERROR_EXIT

    error = capsys.readouterr().err
    assert "warning: runforge onstart force-retries" in error
    assert "single machine that has just started" in error
    assert "must be resumable" in error
    assert "--single-node" in error
    assert machine.status(experiment)["state"] == "created"
    assert not (machine.reports / ".onstart.lock").exists()


def test_a_missing_report_root_is_an_error(tmp_path, capsys):
    assert main(["onstart", "--single-node", str(tmp_path / "missing")]) == CLI_ERROR_EXIT
    assert "not a directory" in capsys.readouterr().err


def test_nothing_interrupted_runs_the_queue_like_run(machine, capsys):
    machine.marker.touch()  # the stand-in finishes immediately
    experiment = machine.plan()

    assert main(["onstart", "--single-node", str(machine.reports)]) == 0

    assert machine.status(experiment)["state"] == "completed"
    assert "Interrupted experiments to restart under" in capsys.readouterr().out


def test_an_interrupted_experiment_is_retried_and_resumes_from_its_checkpoint(machine):
    experiment = machine.plan()
    machine.start_onstart()
    _wait_for(lambda: machine.marker.exists() and machine.status(experiment)["state"] == "inprogress", "the hang")
    assert (experiment / "claim").is_dir()

    machine.kill_everything()
    assert machine.status(experiment)["state"] == "inprogress"  # stale, as after a lost machine

    assert main(["onstart", "--single-node", str(machine.reports)]) == 0

    status = machine.status(experiment)
    assert status["state"] == "completed"
    assert status["attempt"] == SECOND_ATTEMPT
    assert not (experiment / "claim").exists()
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8").strip() == "resumed"


def test_a_claimed_but_unstarted_record_is_left_alone_and_reported(machine, capsys):
    experiment = machine.plan()
    claim = experiment / "claim"
    claim.mkdir()
    (claim / "owner.json").write_text("{}", encoding="utf-8")

    main(["onstart", "--single-node", str(machine.reports)])

    assert "needs inspection, left unchanged" in capsys.readouterr().out
    assert claim.is_dir()
    assert machine.status(experiment)["state"] == "created"


def test_a_second_onstart_while_one_runs_is_refused(machine, capsys):
    experiment = machine.plan()
    first = machine.start_onstart()
    _wait_for(lambda: machine.status(experiment)["state"] == "inprogress", "the first onstart to start working")

    assert main(["onstart", "--single-node", str(machine.reports)]) == CLI_ERROR_EXIT

    assert "already holds" in capsys.readouterr().err
    assert first.poll() is None
    assert machine.status(experiment)["attempt"] == 1


def _interrupt(machine: Machine) -> Path:
    """Leave one real experiment inprogress with a stale claim, as after a lost machine."""
    experiment = machine.plan()
    machine.start_onstart()
    _wait_for(lambda: machine.marker.exists() and machine.status(experiment)["state"] == "inprogress", "the hang")
    machine.kill_everything()
    return experiment


def test_a_held_interrupted_experiment_is_left_alone_then_restarted_once_its_hold_is_removed(machine, capsys):
    experiment = _interrupt(machine)
    schedule = experiment / "schedule.json"
    schedule.write_text(
        json.dumps(
            {"kind": "runforge_schedule", "schema_version": 1, "held": True, "updated_at": "2026-11-10T00:00:00Z"}
        ),
        encoding="utf-8",
    )
    # RunForge itself refuses to change scheduling for an inprogress experiment.
    assert main(["schedule", "--release", str(experiment)]) == CLI_ERROR_EXIT
    capsys.readouterr()

    assert main(["onstart", "--single-node", str(machine.reports)]) == 0

    output = capsys.readouterr().out
    assert f"remove its schedule.json, then run onstart again): {experiment}" in output
    assert machine.status(experiment)["state"] == "inprogress"
    assert machine.status(experiment)["attempt"] == 1
    assert (experiment / "claim").is_dir()

    schedule.unlink()
    assert main(["onstart", "--single-node", str(machine.reports)]) == 0

    status = machine.status(experiment)
    assert status["state"] == "completed"
    assert status["attempt"] == SECOND_ATTEMPT
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8").strip() == "resumed"


def test_a_restart_that_cannot_be_prepared_is_reported_once_and_changes_no_attempt(machine, capsys):
    experiment = _interrupt(machine)
    (experiment / "artifacts" / "link").symlink_to(experiment / "artifacts" / "checkpoint.txt")

    assert main(["onstart", "--single-node", str(machine.reports)]) == CLI_ERROR_EXIT

    captured = capsys.readouterr()
    assert "supports only regular files and directories" in captured.err + captured.out
    status = machine.status(experiment)
    assert status["state"] == "inprogress"  # not rewritten
    assert status["attempt"] == 1  # no second attempt
    assert not (experiment / "attempt-0001").exists()  # nothing was archived
    assert not (experiment / "claim").exists()  # the forced retry had already cleared the old claim


def test_a_failed_restart_is_reported_and_not_repeated_without_artifacts(tmp_path, monkeypatch):
    calls = []

    def execute(request, *, progress=None):
        calls.append((request.retry, request.force, request.reuse_artifacts))
        return _result(invalid=1)

    monkeypatch.setattr(commands, "execute_paths", execute)

    assert commands._restart_interrupted(tmp_path, False) == CLI_ERROR_EXIT  # noqa: SLF001
    assert calls == [(True, True, True)]


def test_a_retry_error_is_reported_without_stopping_the_command(tmp_path, monkeypatch, capsys):
    def execute(request, *, progress=None):
        raise RetryError("Could not prepare retry; rollback also failed: inspect the files")

    monkeypatch.setattr(commands, "execute_paths", execute)

    assert commands._restart_interrupted(tmp_path, False) == CLI_ERROR_EXIT  # noqa: SLF001
    assert "rollback also failed" in capsys.readouterr().err


def test_a_held_interrupted_experiment_is_not_restarted_and_is_listed_before_and_after(tmp_path, monkeypatch, capsys):
    held = tmp_path / "held-experiment"
    requests = []

    def execute(request, *, progress=None):
        requests.append(request)
        return _result()

    monkeypatch.setattr(commands, "execute_paths", execute)
    monkeypatch.setattr(commands, "find_interrupted", lambda root: Interrupted((), (held,), ()))

    assert main(["onstart", "--single-node", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert (
        output.count(f"held, left inprogress (remove its schedule.json, then run onstart again): {held}")
        == LISTED_TWICE
    )
    assert "Left unchanged:" in output
    assert [request.retry for request in requests] == [False]  # only the final run; nothing was force-retried
