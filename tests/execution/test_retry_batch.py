"""Durable preparation, ownership, and worker handoff for a fixed retry batch."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import runforge.execution.retry as retry_module
from runforge.cli import main
from runforge.execution.retry import prepare_retries
from runforge.infrastructure.claims import ClaimError, clear_claim, load_claim, try_acquire_claim
from runforge.infrastructure.json_store import save_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


SECOND_ATTEMPT = 2
CLI_ERROR = 2
THREE_TASKS = 3
FLAGS = [(False, False), (False, True), (True, False), (True, True)]
PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def factory(tmp_path):
    marker = tmp_path / "executed"
    repository = create_git_repository(
        tmp_path / "source",
        {"train.py": "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ran')\n"},
    )

    def create(name="task", state="failed"):
        layout = ExperimentDirectory(
            plan_experiment(
                PlanRequest(
                    name=name,
                    source_path=repository,
                    output_root=tmp_path / "reports",
                    command=ExperimentCommand.argv((sys.executable, "train.py", str(marker))),
                )
            )
        )
        layout.save_status(replace(layout.load_status(), state=state, attempt=1, started_at="2026-01-01T00:00:00Z"))
        (layout.artifacts / "checkpoint").write_text(name)
        return layout

    return create, marker


@pytest.mark.parametrize(("force", "reuse"), FLAGS)
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize(
    "eligibility",
    [
        ("failed", True, True),
        ("inprogress", False, True),
        ("created", False, False),
        ("init", False, False),
        ("completed", False, False),
    ],
)
def test_flag_state_and_claim_contract(factory, force, reuse, claimed, eligibility):
    state, normal, forced = eligibility
    create, marker = factory
    layout = create(state=state)
    if claimed:
        assert try_acquire_claim(layout, owner="confirmed-stopped-worker") is not None
    before = {path: path.read_bytes() for path in layout.root.rglob("*") if path.is_file()}

    result = prepare_retries((layout.root,), force=force, reuse_artifacts=reuse)

    eligible = forced if force else normal and not claimed
    assert result.prepared == int(eligible)
    assert result.invalid == result.exit_code == 0
    assert not marker.exists()
    assert layout.load_status().attempt == 1
    assert layout.configuration_file.read_bytes() == before[layout.configuration_file]
    if eligible:
        assert layout.load_status().state == "init"
        assert not layout.claim.exists()
        archive = layout.root / "attempt-0001"
        assert (archive / "artifacts/checkpoint").read_text() == "task"
        assert result.forced == int(force and (claimed or state == "inprogress"))
        if reuse:
            (layout.artifacts / "checkpoint").write_text("new attempt")
            assert (archive / "artifacts/checkpoint").read_text() == "task"
        else:
            assert list(layout.artifacts.iterdir()) == []
    else:
        assert {path: path.read_bytes() for path in layout.root.rglob("*") if path.is_file()} == before
        assert not list(layout.root.glob("attempt-*"))


@pytest.mark.parametrize(("force", "reuse"), FLAGS)
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("state", ["failed", "inprogress"])
def test_hold_wins_over_every_recovery_combination(factory, force, reuse, claimed, state):
    create, marker = factory
    layout = create(state=state)
    save_json_object(
        layout.schedule_file,
        {"kind": "runforge_schedule", "schema_version": 1, "held": True, "updated_at": "2026-11-15T00:00:00Z"},
    )
    if claimed:
        assert try_acquire_claim(layout, owner="stopped-worker") is not None
    before = {path: path.read_bytes() for path in layout.root.rglob("*") if path.is_file()}

    result = prepare_retries((layout.root,), force=force, reuse_artifacts=reuse)

    assert result.outcomes[0].outcome == "held"
    assert result.prepared == result.invalid == result.forced == 0
    assert {path: path.read_bytes() for path in layout.root.rglob("*") if path.is_file()} == before
    assert not marker.exists()


def test_fixed_selection_deduplicates_and_keeps_partial_success(factory):
    create, marker = factory
    first, broken, following = [create(name) for name in ("a", "b", "c")]
    (broken.root / "attempt-0001").mkdir()
    published = []

    def progress(outcome):
        if outcome.experiment == first.root:
            published.append(create("published-later"))

    result = prepare_retries((first.root, first.root.parents[1]), reuse_artifacts=True, progress=progress)

    assert result.prepared == len((first, following))
    assert result.invalid == 1
    assert result.exit_code == CLI_ERROR
    assert len(result.outcomes) == THREE_TASKS
    assert first.load_status().state == following.load_status().state == "init"
    assert broken.load_status().state == published[0].load_status().state == "failed"
    assert not marker.exists()


def test_retry_of_prepared_work_preserves_seed_even_if_options_change(factory):
    create, _marker = factory
    layout = create()
    assert prepare_retries((layout.root,), reuse_artifacts=True).prepared == 1
    before = {path: path.read_bytes() for path in layout.root.rglob("*") if path.is_file()}

    result = prepare_retries((layout.root,), force=True)

    assert result.skipped == 1
    assert {path: path.read_bytes() for path in layout.root.rglob("*") if path.is_file()} == before


def test_worker_cannot_consume_partial_copy_but_can_consume_after_handoff(factory, monkeypatch):
    create, marker = factory
    layout = create()
    copytree = retry_module.shutil.copytree
    environment = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}

    def worker():
        return subprocess.run(
            [sys.executable, "-m", "runforge", "run", str(layout.root)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def copy_while_worker_contends(source, target, **options):
        assert layout.claim.exists()
        assert layout.load_status().state == "failed"
        assert worker().returncode == 0
        assert not marker.exists()
        return copytree(source, target, **options)

    monkeypatch.setattr(retry_module.shutil, "copytree", copy_while_worker_contends)
    assert prepare_retries((layout.root,), reuse_artifacts=True).prepared == 1
    assert not layout.claim.exists()
    assert not marker.exists()
    assert worker().returncode == 0
    assert marker.read_text() == "ran"
    assert layout.load_status().attempt == SECOND_ATTEMPT


def test_failed_force_preparation_reports_cleared_claim(factory):
    create, _marker = factory
    layout = create(state="inprogress")
    assert try_acquire_claim(layout, owner="stopped") is not None
    (layout.artifacts / "link").symlink_to(layout.artifacts / "checkpoint")

    result = prepare_retries((layout.root,), force=True, reuse_artifacts=True)

    assert result.invalid == 1
    assert "already cleared the previous claim" in result.outcomes[0].message
    assert not layout.claim.exists()
    assert layout.load_status().state == "inprogress"
    assert layout.load_status().attempt == 1


def test_foreign_claim_is_preserved_when_ownership_changes_before_release(factory, monkeypatch):
    create, _marker = factory
    layout = create()
    prepare = retry_module.prepare_retry
    foreign = []

    def replaced_owner(*args, **options):
        result = prepare(*args, **options)
        clear_claim(layout)
        foreign.append(try_acquire_claim(layout, owner="another-worker"))
        return result

    monkeypatch.setattr(retry_module, "prepare_retry", replaced_owner)
    result = prepare_retries((layout.root,))

    assert result.invalid == 1
    assert "could not release preparation claim" in result.outcomes[0].message
    assert load_claim(layout) == foreign[0]


def test_force_clear_then_contention_never_mutates_peer_history(factory, monkeypatch):
    create, _marker = factory
    layout = create(state="inprogress")
    assert try_acquire_claim(layout, owner="stopped") is not None
    acquire = retry_module.try_acquire_claim
    before = layout.status_file.read_bytes()

    def peer_acquires_first(current):
        assert acquire(current, owner="peer") is not None

    monkeypatch.setattr(retry_module, "try_acquire_claim", peer_acquires_first)
    result = prepare_retries((layout.root,), force=True)

    assert result.outcomes[0].outcome == "claimed"
    assert "already cleared the previous claim" in result.outcomes[0].message
    assert layout.status_file.read_bytes() == before
    assert load_claim(layout).owner == "peer"
    assert not list(layout.root.glob("attempt-*"))


def test_invalid_later_selection_prevents_earlier_preparation(factory, tmp_path):
    create, _marker = factory
    layout = create()
    before = layout.status_file.read_bytes()

    assert main(["retry", str(layout.root), str(tmp_path / "missing")]) == CLI_ERROR
    assert layout.status_file.read_bytes() == before
    assert not list(layout.root.glob("attempt-*"))


def test_invalid_forced_leaf_does_not_block_other_valid_leaves(factory):
    create, _marker = factory
    broken, valid = create("broken"), create("valid")
    broken.status_file.write_text("{broken json")

    result = prepare_retries((broken.root, valid.root), force=True)

    assert result.invalid == result.prepared == 1
    assert valid.load_status().state == "init"


@pytest.mark.parametrize("old_claim", [False, True])
def test_force_preserves_peer_preparation_and_new_execution_claim(factory, monkeypatch, old_claim):
    create, _marker = factory
    layout = create()
    if old_claim:
        assert try_acquire_claim(layout, owner="stopped-worker") is not None
    clear_existing = retry_module._clear_existing_claim  # noqa: SLF001
    peer_claims = []

    def peer_prepares_before_clear(current, *, expected_identity):
        monkeypatch.setattr(retry_module, "_clear_existing_claim", clear_existing)
        assert prepare_retries((current.root,), force=True, reuse_artifacts=True).prepared == 1
        peer_claims.append(try_acquire_claim(current, owner="new-worker"))
        return clear_existing(current, expected_identity=expected_identity)

    monkeypatch.setattr(retry_module, "_clear_existing_claim", peer_prepares_before_clear)
    result = prepare_retries((layout.root,), force=True)

    assert result.outcomes[0].outcome == "stale"
    assert result.forced == 0
    assert layout.load_status().state == "init"
    assert load_claim(layout) == peer_claims[0]
    assert (layout.artifacts / "checkpoint").read_text() == "task"
    assert len(list(layout.root.glob("attempt-*"))) == 1


def test_force_rechecks_initial_status_even_if_claim_identity_is_unchanged(factory, monkeypatch):
    create, _marker = factory
    layout = create()
    original_claim = try_acquire_claim(layout, owner="peer")
    clear_existing = retry_module._clear_existing_claim  # noqa: SLF001

    def peer_updates_status_before_clear(current, *, expected_identity):
        current.save_status(replace(current.load_status(), state="init"))
        return clear_existing(current, expected_identity=expected_identity)

    monkeypatch.setattr(retry_module, "_clear_existing_claim", peer_updates_status_before_clear)
    result = prepare_retries((layout.root,), force=True)

    assert result.outcomes[0].outcome == "stale"
    assert load_claim(layout) == original_claim
    assert not list(layout.root.glob("attempt-*"))


def test_force_acquisition_error_keeps_clear_accounting_and_continues_batch(factory, monkeypatch):
    create, _marker = factory
    broken, following = create("a-broken", state="inprogress"), create("b-following")
    assert try_acquire_claim(broken, owner="confirmed-stopped") is not None
    acquire = retry_module.try_acquire_claim

    def cannot_acquire_after_clear(current):
        if current.root == broken.root:
            assert not current.claim.exists()
            raise ClaimError("replacement claim cannot be written")
        return acquire(current)

    monkeypatch.setattr(retry_module, "try_acquire_claim", cannot_acquire_after_clear)
    result = prepare_retries((broken.root, following.root), force=True)

    assert result.invalid == result.prepared == result.forced == 1
    assert result.exit_code == CLI_ERROR
    assert result.outcomes[0].forced
    assert "already cleared the previous claim" in result.outcomes[0].message
    assert broken.load_status().state == "inprogress"
    assert not broken.claim.exists()
    assert following.load_status().state == "init"


def test_force_can_recover_an_abandoned_claim_with_missing_owner_metadata(factory):
    create, _marker = factory
    layout = create(state="inprogress")
    layout.claim.mkdir()

    result = prepare_retries((layout.root,), force=True)

    assert result.prepared == result.forced == 1
    assert not layout.claim.exists()
    assert layout.load_status().state == "init"
