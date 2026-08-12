"""Tests for read-only staleness diagnosis of inprogress experiments."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from runforge.execution.discovery import discover_experiments
from runforge.execution.staleness import describe_staleness
from runforge.infrastructure.claims import ExperimentClaim
from runforge.infrastructure.clock import parse_utc, utc_now
from runforge.infrastructure.json_store import save_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


NOW = datetime.now(timezone.utc)
FAR_FUTURE_CUTOFF = NOW + timedelta(days=365)
FAR_PAST_CUTOFF = NOW - timedelta(days=365)
ELAPSED_TOLERANCE_SECONDS = 5


def _experiment(tmp_path, *, started_at: str | None, state: str = "inprogress"):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "print('ok')\n"})
    plan = plan_experiment(
        PlanRequest(
            name="x",
            command=ExperimentCommand.argv(("python", "train.py")),
            source_path=repository,
            output_root=tmp_path / "reports",
        )
    )
    layout = ExperimentDirectory.resolve(plan)
    _, status = layout.load_metadata()
    layout.save_status(replace(status, state=state, started_at=started_at))
    (discovered,) = discover_experiments(tmp_path / "reports").experiments
    return discovered


def _write_claim(discovered, owner: str) -> None:
    layout = ExperimentDirectory(discovered.path)
    layout.claim.mkdir()
    save_json_object(layout.claim_file, ExperimentClaim(token="t", owner=owner, acquired_at=utc_now()).to_dict())


def test_describe_staleness_ignores_a_non_inprogress_experiment(tmp_path):
    stale_started_at = (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    experiment = _experiment(tmp_path, started_at=stale_started_at, state="failed")

    assert describe_staleness(experiment, FAR_FUTURE_CUTOFF) is None


def test_describe_staleness_ignores_an_inprogress_experiment_with_no_started_at(tmp_path):
    experiment = _experiment(tmp_path, started_at=None, state="inprogress")

    assert describe_staleness(experiment, FAR_FUTURE_CUTOFF) is None


def test_describe_staleness_ignores_an_experiment_started_at_or_after_the_cutoff(tmp_path):
    experiment = _experiment(tmp_path, started_at=utc_now(), state="inprogress")

    assert describe_staleness(experiment, FAR_PAST_CUTOFF) is None


def test_describe_staleness_reports_elapsed_time_for_a_qualifying_experiment(tmp_path):
    started_at = (NOW - timedelta(hours=8, minutes=12)).isoformat().replace("+00:00", "Z")
    experiment = _experiment(tmp_path, started_at=started_at)

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    assert hint is not None
    assert hint.started_at == started_at
    assert hint.elapsed_seconds >= timedelta(hours=8, minutes=12).total_seconds()


def test_describe_staleness_reports_missing_claim(tmp_path):
    experiment = _experiment(tmp_path, started_at=utc_now())

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    assert hint.verdict == "cannot verify liveness: claim metadata is missing or unreadable"


def test_describe_staleness_reports_a_different_host(tmp_path):
    experiment = _experiment(tmp_path, started_at=utc_now())
    _write_claim(experiment, owner="some-other-host:1234")

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    assert hint.verdict == "cannot verify liveness: claim held by a different host (some-other-host)"


def test_describe_staleness_reports_an_owner_not_in_hostname_pid_form(tmp_path):
    experiment = _experiment(tmp_path, started_at=utc_now())
    owner = f"{socket.gethostname()}:not-a-pid"
    _write_claim(experiment, owner=owner)

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    assert hint.verdict == f"cannot verify liveness: claim owner '{owner}' is not in HOSTNAME:PID form"


def test_describe_staleness_reports_a_dead_process_on_this_host(tmp_path):
    experiment = _experiment(tmp_path, started_at=utc_now())
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = process.pid
    assert process.wait() == 0
    _write_claim(experiment, owner=f"{socket.gethostname()}:{dead_pid}")

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    assert hint.verdict == f"process appears dead: PID {dead_pid} not running on {socket.gethostname()}"


def test_describe_staleness_reports_an_alive_process_on_this_host(tmp_path):
    experiment = _experiment(tmp_path, started_at=utc_now())
    _write_claim(experiment, owner=f"{socket.gethostname()}:{os.getpid()}")

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    assert hint.verdict == f"process appears alive: PID {os.getpid()} running on {socket.gethostname()}"


def test_describe_staleness_elapsed_matches_parsed_started_at(tmp_path):
    started_at = utc_now()
    experiment = _experiment(tmp_path, started_at=started_at)

    hint = describe_staleness(experiment, FAR_FUTURE_CUTOFF)

    expected = (datetime.now(timezone.utc) - parse_utc(started_at)).total_seconds()
    assert abs(hint.elapsed_seconds - expected) < ELAPSED_TOLERANCE_SECONDS
