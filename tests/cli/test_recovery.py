"""Read-only startup guidance for interrupted and contended executions."""

from __future__ import annotations

import os
import select
import shlex
import signal
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import runforge.execution.recovery as recovery_module
import runforge.execution.staleness as staleness_module
from runforge.cli import commands, main
from runforge.infrastructure.claims import try_acquire_claim
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


SECOND_ATTEMPT = 2
READY_TIMEOUT = 10


def _plan(repository, reports, name):
    return plan_experiment(
        PlanRequest(
            name=name,
            source_path=repository,
            output_root=reports,
            command=ExperimentCommand.argv((sys.executable, "train.py")),
        )
    )


def _existing(layout, *, state="inprogress", owner="prior-container:42"):
    layout.save_status(
        replace(
            layout.load_status(),
            state=state,
            attempt=1,
            started_at="2026-01-01T00:00:00Z",
        )
    )
    claim = try_acquire_claim(layout, owner=owner)
    assert claim is not None
    return claim


@pytest.mark.parametrize("command", ["run", "retry"])
def test_startup_reports_interrupted_work_and_continues_other_experiments(tmp_path, monkeypatch, capsys, command):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    reports = tmp_path / "reports"
    stopped = ExperimentDirectory(_plan(repository, reports, "a-stopped"))
    following = ExperimentDirectory(_plan(repository, reports, "b-following"))
    _existing(stopped)
    if command == "retry":
        following.save_status(replace(following.load_status(), state="failed", attempt=1))
    before = {path.name: path.read_bytes() for path in (stopped.status_file, stopped.claim_file)}
    phases = []
    progress = commands.print_worker_progress

    def record(event):
        phases.append(event.phase)
        progress(event)

    monkeypatch.setattr(commands, "print_worker_progress", record)

    assert main([command, str(reports)]) == 0

    output = capsys.readouterr()
    assert f"after confirmation: runforge retry --force {stopped.root}" in output.err
    assert "assessment: unknown" in output.err
    assert "owner: prior-container:42" in output.err
    assert "different host" in output.err
    assert "age is not proof" in output.err
    assert following.load_status().state == "completed"
    assert phases[0] == "recovery"
    assert "executing" in phases
    assert {path.name: path.read_bytes() for path in (stopped.status_file, stopped.claim_file)} == before
    assert not list(stopped.root.glob("attempt-*"))


def test_forced_retry_does_not_repeat_the_guidance_to_run_the_forced_retry(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    stopped = ExperimentDirectory(_plan(repository, tmp_path / "reports", "a-stopped"))
    _existing(stopped)

    assert main(["retry", "--force", str(stopped.root)]) == 0

    error = capsys.readouterr().err
    assert "after confirmation: runforge retry --force" not in error
    assert "Forced retry cannot prove that the previous worker has stopped" in error
    assert stopped.load_status().state == "completed"


def test_forced_retry_reports_a_cleared_claim_that_another_worker_took_first(tmp_path, monkeypatch, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    stopped = ExperimentDirectory(_plan(repository, tmp_path / "reports", "a-stopped"))
    _existing(stopped)
    monkeypatch.setattr("runforge.execution.retry.try_acquire_claim", lambda experiment: None)

    main(["retry", "--force", str(stopped.root)])

    assert "another worker acquired the experiment first" in capsys.readouterr().err
    assert stopped.load_status().state == "inprogress"


INVALID_EXIT_CODE = 2
HOLD_VICTIM_SCRIPT = """\
import json, os, pathlib
victim = pathlib.Path(pathlib.Path(VICTIM_FILE).read_text().strip())
if pathlib.Path(os.environ["RUNFORGE_ARTIFACT_DIR"]).parent != victim:
    (victim / "schedule.json").write_text(json.dumps(
        {"kind": "runforge_schedule", "schema_version": 1, "held": True, "updated_at": "2026-11-10T00:00:00Z"}))
"""


def test_forced_retry_keeps_the_claim_of_an_experiment_held_after_discovery(tmp_path, capsys):
    # The runner's command writes the hold while the batch is already running, after discovery built its queue.
    repository = create_git_repository(
        tmp_path / "repository",
        {"train.py": HOLD_VICTIM_SCRIPT.replace("VICTIM_FILE", repr(str(tmp_path / "victim.txt")))},
    )
    reports = tmp_path / "reports"
    first = ExperimentDirectory(_plan(repository, reports, "a-first"))
    second = ExperimentDirectory(_plan(repository, reports, "b-second"))
    runner, victim = sorted((first, second), key=lambda layout: layout.root)
    (tmp_path / "victim.txt").write_text(str(victim.root), encoding="utf-8")
    runner.save_status(replace(runner.load_status(), state="failed", attempt=1))
    _existing(victim)
    before = (victim.status_file.read_bytes(), victim.claim_file.read_bytes())

    main(["retry", "--force", str(runner.root), str(victim.root)])

    capsys.readouterr()
    assert runner.load_status().state == "completed"
    assert victim.claim_file.exists()
    assert (victim.status_file.read_bytes(), victim.claim_file.read_bytes()) == before
    assert not list(victim.root.glob("attempt-*"))


def test_malformed_hold_written_after_discovery_invalidates_only_that_candidate(tmp_path, capsys):
    hold = HOLD_VICTIM_SCRIPT[HOLD_VICTIM_SCRIPT.index("    (victim"):]
    script = HOLD_VICTIM_SCRIPT.replace(hold, '    (victim / "schedule.json").write_text("{broken JSON")\n')
    assert script != HOLD_VICTIM_SCRIPT
    repository = create_git_repository(
        tmp_path / "repository",
        {"train.py": script.replace("VICTIM_FILE", repr(str(tmp_path / "victim.txt")))},
    )
    reports = tmp_path / "reports"
    first = ExperimentDirectory(_plan(repository, reports, "a-first"))
    second = ExperimentDirectory(_plan(repository, reports, "b-second"))
    third = ExperimentDirectory(_plan(repository, reports, "c-third"))
    victim = second
    (tmp_path / "victim.txt").write_text(str(victim.root), encoding="utf-8")
    for layout in (first, third):
        layout.save_status(replace(layout.load_status(), state="failed", attempt=1))
    _existing(victim)

    exit_code = main(["retry", "--force", str(first.root), str(victim.root), str(third.root)])

    capsys.readouterr()
    assert exit_code == INVALID_EXIT_CODE
    assert first.load_status().state == "completed"
    assert third.load_status().state == "completed"
    assert victim.claim_file.exists()


def test_startup_names_the_workdir_left_inside_an_interrupted_experiment(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    with_workdir = ExperimentDirectory(_plan(repository, tmp_path / "reports", "a-with-workdir"))
    without_workdir = ExperimentDirectory(_plan(repository, tmp_path / "reports", "b-without-workdir"))
    _existing(with_workdir)
    _existing(without_workdir)
    with_workdir.workspace.mkdir()

    assert main(["run", str(tmp_path / "reports")]) == 0

    first_report, second_report = capsys.readouterr().err.split("Existing execution:")[1:]
    assert f"workdir present: {with_workdir.workspace}" in first_report
    assert "workdir present" not in second_report
    assert with_workdir.workspace.is_dir()  # Recovery only reports; it never deletes.


@pytest.mark.parametrize("claim_metadata", [None, "{broken JSON"])
def test_startup_reports_unknown_owner_without_claim_and_keeps_record(tmp_path, capsys, claim_metadata):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "stopped"))
    _existing(layout)
    if claim_metadata is None:
        layout.claim_file.unlink()
    else:
        layout.claim_file.write_text(claim_metadata)
    before = layout.status_file.read_bytes()

    assert main(["run", str(layout.root)]) == 0

    assert "owner/activity unknown" in capsys.readouterr().err
    assert layout.status_file.read_bytes() == before
    assert layout.claim.exists()


def test_startup_recovery_command_quotes_experiment_paths(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports with spaces", "stopped"))
    _existing(layout)

    assert main(["run", str(layout.root)]) == 0

    output = capsys.readouterr().err
    line = next(line for line in output.splitlines() if line.startswith("  after confirmation: "))
    assert shlex.split(line.removeprefix("  after confirmation: ")) == [
        "runforge",
        "retry",
        "--force",
        str(layout.root),
    ]


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert process.wait() == 0
    return process.pid


def _backdate_claim(layout, acquired_at):
    claim = load_json_object(layout.claim_file)
    save_json_object(layout.claim_file, {**claim, "acquired_at": acquired_at})


def test_claimed_initial_state_is_reported_without_unusable_retry_advice(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "claimed"))
    _existing(layout, state="init", owner=f"{socket.gethostname()}:{_dead_pid()}")
    before = layout.claim_file.read_bytes()

    assert main(["run", str(layout.root)]) == 0

    output = capsys.readouterr().err
    assert "process appears dead" in output
    assert "assessment: stale" in output
    assert "This state cannot be force-retried" in output
    assert "after confirmation: runforge retry" not in output
    assert layout.claim_file.read_bytes() == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Process start time uses Linux /proc")
@pytest.mark.parametrize("state", ["init", "inprogress"])
def test_verifiably_active_claim_gets_no_recovery_report(tmp_path, capsys, state):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "active"))
    _existing(layout, state=state, owner=f"{socket.gethostname()}:{os.getpid()}")
    before = (layout.status_file.read_bytes(), layout.claim_file.read_bytes())

    assert main(["run", str(layout.root)]) == 0

    output = capsys.readouterr().err
    assert "Existing execution" not in output
    assert "retry --force" not in output
    assert (layout.status_file.read_bytes(), layout.claim_file.read_bytes()) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Process start time uses Linux /proc")
def test_reused_pid_after_container_restart_is_reported_stale(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "restarted"))
    # Same hostname and PID as this process, but claimed before it started.
    _existing(layout, owner=f"{socket.gethostname()}:{os.getpid()}")
    _backdate_claim(layout, "2000-01-01T00:00:00Z")
    before = (layout.status_file.read_bytes(), layout.claim_file.read_bytes())

    assert main(["run", str(layout.root)]) == 0

    output = capsys.readouterr().err
    assert "reused by a process started after the claim" in output
    assert "assessment: stale" in output
    assert f"after confirmation: runforge retry --force {layout.root}" in output
    assert (layout.status_file.read_bytes(), layout.claim_file.read_bytes()) == before


def test_running_pid_with_unknown_start_time_gets_a_manual_check_not_a_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(staleness_module, "process_start_time", lambda _pid: None)
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "unverified"))
    _existing(layout, owner=f"{socket.gethostname()}:{os.getpid()}")

    assert main(["run", str(layout.root)]) == 0

    output = capsys.readouterr().err
    assert "assessment: unknown" in output
    assert f"ps -o lstart= -p {os.getpid()}" in output
    assert "retry --force" not in output


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Uses Linux zombie processes and /proc state")
def test_unreaped_exited_worker_is_reported_stale_without_changing_records(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "unreaped"))
    worker = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        # Wait for exit without reaping: kill(pid, 0) still succeeds for this PID.
        os.waitid(os.P_PID, worker.pid, os.WEXITED | os.WNOWAIT)
        _existing(layout, owner=f"{socket.gethostname()}:{worker.pid}")
        before = (layout.status_file.read_bytes(), layout.claim_file.read_bytes())

        assert main(["run", str(tmp_path / "reports")]) == 0

        output = capsys.readouterr().err
        assert "assessment: stale" in output
        assert "process appears dead" in output
        assert f"after confirmation: runforge retry --force {layout.root}" in output
        assert (layout.status_file.read_bytes(), layout.claim_file.read_bytes()) == before
    finally:
        worker.wait(timeout=READY_TIMEOUT)


def test_non_ascii_pid_is_unknown_and_other_work_continues(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    reports = tmp_path / "reports"
    stopped = ExperimentDirectory(_plan(repository, reports, "a-unknown-owner"))
    following = ExperimentDirectory(_plan(repository, reports, "b-following"))
    _existing(stopped, owner=f"{socket.gethostname()}:\N{SUPERSCRIPT TWO}")
    before = (stopped.status_file.read_bytes(), stopped.claim_file.read_bytes())

    assert main(["run", str(reports)]) == 0

    output = capsys.readouterr().err
    assert "assessment: unknown" in output
    assert "is not in HOSTNAME:PID form" in output
    assert "Could not inspect existing execution" not in output
    assert str(stopped.root) in output
    assert (stopped.status_file.read_bytes(), stopped.claim_file.read_bytes()) == before
    assert following.load_status().state == "completed"


def test_inspection_error_names_experiment_and_continues_other_work(tmp_path, monkeypatch, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    reports = tmp_path / "reports"
    stopped = ExperimentDirectory(_plan(repository, reports, "a-unreadable-claim"))
    following = ExperimentDirectory(_plan(repository, reports, "b-following"))
    _existing(stopped)
    before = (stopped.status_file.read_bytes(), stopped.claim_file.read_bytes())

    def unreadable_claim(_layout):
        raise PermissionError("claim inspection denied")

    monkeypatch.setattr(recovery_module, "load_claim", unreadable_claim)

    assert main(["run", str(reports)]) == 0

    output = capsys.readouterr().err
    assert "Could not inspect existing execution" in output
    assert "claim inspection denied" in output
    assert str(stopped.root) in output
    assert (stopped.status_file.read_bytes(), stopped.claim_file.read_bytes()) == before
    assert following.load_status().state == "completed"


def test_recovery_block_is_printed_without_an_appended_path(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('done')\n"})
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "stopped"))
    _existing(layout)

    assert main(["run", str(layout.root)]) == 0

    block = capsys.readouterr().err.split("Existing execution:")[1]
    assert "workdir.\n" in block or block.rstrip().endswith("workdir.")
    assert f"workdir.: {layout.root}" not in block


@pytest.mark.skipif(os.name != "posix", reason="Uses POSIX process groups and a readiness FIFO")
def test_killed_worker_is_identified_and_force_retry_starts_new_attempt(tmp_path, monkeypatch, capsys):
    fifo = tmp_path / "ready"
    os.mkfifo(fifo)
    flag = tmp_path / "already-started"
    repository = create_git_repository(
        tmp_path / "repository",
        {
            "train.py": (
                "from pathlib import Path\nimport signal\n"
                f"flag = Path({str(flag)!r})\n"
                "if not flag.exists():\n"
                "    flag.write_text('started')\n"
                f"    Path({str(fifo)!r}).write_text('ready')\n"
                "    signal.pause()\n"
                "print('recovered')\n"
            )
        },
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    layout = ExperimentDirectory(_plan(repository, tmp_path / "reports", "interrupted"))
    ready = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    process = subprocess.Popen(
        [sys.executable, "-m", "runforge", "run", str(layout.root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert select.select([ready], [], [], READY_TIMEOUT)[0], "Child never reached its running state"
        assert os.read(ready, 32) == b"ready"
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=READY_TIMEOUT)
        os.close(ready)
    assert layout.load_status().state == "inprogress"
    old_status = layout.status_file.read_bytes()
    old_claim = layout.claim_file.read_bytes()
    workspace = layout.workspace
    assert workspace.is_dir()

    assert main(["run", str(layout.root)]) == 0
    report = capsys.readouterr().err
    assert f"workdir present: {workspace}" in report
    assert "process appears dead" in report
    assert "assessment: stale" in report
    assert layout.status_file.read_bytes() == old_status
    assert layout.claim_file.read_bytes() == old_claim

    assert main(["retry", "--force", str(layout.root)]) == 0
    capsys.readouterr()
    assert layout.load_status().state == "completed"
    assert layout.load_status().attempt == SECOND_ATTEMPT
    snapshot = load_json_object(layout.root / "attempt-0001" / "status.snapshot.json")
    assert snapshot["state"] == "inprogress"
    assert not layout.claim.exists()
    # The retry deleted the killed run's workdir, built a fresh one, and removed it.
    assert not workspace.exists()
    assert "recovered" in layout.stdout_log.read_text()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Uses /proc start times and a readiness FIFO")
def test_second_worker_stays_silent_about_a_live_peer_execution(tmp_path, monkeypatch, capsys):
    fifo = tmp_path / "ready"
    os.mkfifo(fifo)
    repository = create_git_repository(
        tmp_path / "repository",
        {
            "train.py": (
                f"from pathlib import Path\nimport signal\nPath({str(fifo)!r}).write_text('ready')\nsignal.pause()\n"
            )
        },
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    reports = tmp_path / "reports"
    layout = ExperimentDirectory(_plan(repository, reports, "live"))
    ready = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    peer = subprocess.Popen(
        [sys.executable, "-m", "runforge", "run", str(reports)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert select.select([ready], [], [], READY_TIMEOUT)[0], "Peer child never reached its running state"
        assert layout.load_status().state == "inprogress"
        before = (layout.status_file.read_bytes(), layout.claim_file.read_bytes())

        assert main(["run", str(reports)]) == 0

        output = capsys.readouterr().err
        assert "Existing execution" not in output
        assert "retry --force" not in output
        assert (layout.status_file.read_bytes(), layout.claim_file.read_bytes()) == before
    finally:
        os.killpg(peer.pid, signal.SIGKILL)
        peer.wait(timeout=READY_TIMEOUT)
        os.close(ready)
