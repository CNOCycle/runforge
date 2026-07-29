"""Tests for atomic shared experiment claims."""

from __future__ import annotations

import os
import socket
from dataclasses import replace

import pytest

import runforge.infrastructure.claims as claims_module
from runforge.infrastructure.claims import (
    CLAIM_OWNER_VARIABLE,
    ClaimError,
    ClaimOwnershipError,
    describe_claim_holder,
    load_claim,
    release_claim,
    try_acquire_claim,
    verify_claim_owner,
)
from runforge.infrastructure.storage import ExperimentDirectory


def test_claim_is_exclusive_and_can_be_released(tmp_path):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()

    first = try_acquire_claim(layout, owner="first")
    assert first is not None
    assert first.owner == "first"
    assert load_claim(layout) == first
    assert try_acquire_claim(layout, owner="second") is None

    release_claim(layout, first)
    assert not layout.claim.exists()

    second = try_acquire_claim(layout, owner="second")
    assert second is not None
    assert second.owner == "second"


def test_claim_release_requires_the_current_token(tmp_path):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    claim = try_acquire_claim(layout, owner="first")
    assert claim is not None

    with pytest.raises(ClaimOwnershipError, match="belongs to another owner: held by first since "):
        release_claim(layout, replace(claim, token="different"))

    assert load_claim(layout) == claim
    assert verify_claim_owner(layout, claim) is None


def test_claim_holder_description_names_the_owner_for_an_operator(tmp_path):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    claim = try_acquire_claim(layout, owner="slurm-job-4711")
    assert claim is not None

    description = describe_claim_holder(layout)

    assert description == f"held by slurm-job-4711 since {claim.acquired_at}"


def test_claim_holder_description_reports_an_unreadable_claim(tmp_path):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    layout.claim.mkdir()

    assert describe_claim_holder(layout) == "holder unknown: claim metadata is missing or unreadable"


def test_default_claim_owner_identifies_the_local_process(tmp_path, monkeypatch):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    monkeypatch.delenv(CLAIM_OWNER_VARIABLE, raising=False)

    claim = try_acquire_claim(layout)

    assert claim is not None
    assert claim.owner == f"{socket.gethostname()}:{os.getpid()}"


def test_claim_owner_can_be_configured_for_schedulers_and_containers(tmp_path, monkeypatch):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    monkeypatch.setenv(CLAIM_OWNER_VARIABLE, "slurm:4711")

    claim = try_acquire_claim(layout)

    assert claim is not None
    assert claim.owner == "slurm:4711"


def test_explicit_claim_owner_overrides_the_environment(tmp_path, monkeypatch):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    monkeypatch.setenv(CLAIM_OWNER_VARIABLE, "slurm:4711")

    claim = try_acquire_claim(layout, owner="explicit-caller")

    assert claim is not None
    assert claim.owner == "explicit-caller"


@pytest.mark.parametrize("configured", ["", "   ", "\n\t"])
def test_blank_configured_claim_owner_falls_back_to_the_local_process(tmp_path, monkeypatch, configured):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    monkeypatch.setenv(CLAIM_OWNER_VARIABLE, configured)

    claim = try_acquire_claim(layout)

    assert claim is not None
    assert claim.owner == f"{socket.gethostname()}:{os.getpid()}"


def test_configured_claim_owner_stays_on_one_line_for_operator_messages(tmp_path, monkeypatch):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    monkeypatch.setenv(CLAIM_OWNER_VARIABLE, "  slurm:4711\n  node7  ")

    claim = try_acquire_claim(layout)

    assert claim is not None
    assert claim.owner == "slurm:4711 node7"
    assert "\n" not in describe_claim_holder(layout)


def test_claim_metadata_is_not_treated_as_available_when_corrupt(tmp_path):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()
    layout.claim.mkdir()
    layout.claim_file.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ClaimError, match="Could not load claim"):
        load_claim(layout)

    assert try_acquire_claim(layout) is None


def test_partial_claim_write_removes_claim_directory(tmp_path, monkeypatch):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    layout.root.mkdir()

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(claims_module, "save_json_object", fail_write)

    with pytest.raises(ClaimError, match="Could not write claim"):
        try_acquire_claim(layout)

    assert not layout.claim.exists()
