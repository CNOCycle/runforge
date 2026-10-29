"""Tests for worker workspace naming, reservation, and manifests."""

from __future__ import annotations

import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

import runforge.execution.sources as sources_module
from runforge.execution.errors import WorkerError
from runforge.infrastructure.claims import ExperimentClaim
from runforge.infrastructure.json_store import JsonStoreError, load_json_object, save_json_object
from runforge.schemas.worker_manifest import WORKER_MANIFEST_FILE, WorkerManifestError


def test_workspace_name_contains_project_and_unique_id():
    name = sources_module._worker_workspace_name("repo_A")
    assert re.fullmatch(r"repo_A-[0-9a-f]{32}", name)


@pytest.mark.parametrize("raw_project", ["repo with spaces", "repo:windows?", "repo/with/slashes", "????"])
def test_workspace_names_are_filesystem_safe(raw_project):
    name = sources_module._worker_workspace_name(raw_project)
    assert re.fullmatch(r"[A-Za-z0-9._-]+-[0-9a-f]{32}", name)
    assert "/" not in name


def test_reserve_worker_workspace_creates_a_directory(tmp_path):
    result = sources_module._reserve_worker_workspace(tmp_path, "repo_A")
    assert result.is_dir()
    assert result.parent == tmp_path
    assert re.fullmatch(r"repo_A-[0-9a-f]{32}", result.name)


def test_concurrent_workers_reserve_distinct_directories(tmp_path):
    worker_count = 24
    with ThreadPoolExecutor(max_workers=8) as pool:
        workdirs = list(
            pool.map(lambda _: sources_module._reserve_worker_workspace(tmp_path, "repo_A"), range(worker_count))
        )
    assert len(set(workdirs)) == worker_count
    assert all(path.is_dir() and path.parent == tmp_path for path in workdirs)


def test_reserve_worker_workspace_retries_past_a_name_collision(tmp_path, monkeypatch):
    (tmp_path / "taken").mkdir()
    names = iter(["taken", "free"])
    monkeypatch.setattr(sources_module, "_worker_workspace_name", lambda _: next(names))
    assert sources_module._reserve_worker_workspace(tmp_path, "repo_A") == tmp_path / "free"


def test_reserve_worker_workspace_gives_up_after_repeated_collisions(tmp_path, monkeypatch):
    (tmp_path / "taken").mkdir()
    monkeypatch.setattr(sources_module, "_worker_workspace_name", lambda _: "taken")
    with pytest.raises(WorkerError, match="Could not create a unique worker workspace"):
        sources_module._reserve_worker_workspace(tmp_path, "repo_A")


def test_reserve_worker_workspace_reports_an_unusable_root_clearly(tmp_path):
    with pytest.raises(WorkerError, match="Could not create worker workspace under"):
        sources_module._reserve_worker_workspace(tmp_path / "does-not-exist", "repo_A")


def _claim() -> ExperimentClaim:
    return ExperimentClaim(
        token="81cf070577f54cfb991791368f7fc2c3", owner="node7:4127", acquired_at="2026-07-30T18:15:30.000000Z"
    )


def test_write_worker_manifest_then_load_round_trips(tmp_path):
    claim = _claim()

    sources_module._write_worker_manifest(
        tmp_path,
        source_kind="git",
        source_identity="/repositories/repo_A",
        source_reference="a" * 40,
        claim=claim,
    )

    assert (tmp_path / WORKER_MANIFEST_FILE).is_file()
    manifest = sources_module.load_worker_manifest(tmp_path)
    assert manifest.source_kind == "git"
    assert manifest.source_identity == "/repositories/repo_A"
    assert manifest.source_reference == "a" * 40
    assert manifest.host == socket.gethostname()
    assert manifest.pid == os.getpid()
    assert manifest.owner == claim.owner
    assert manifest.worker_id == claim.token
    assert manifest.started_at == claim.acquired_at


def test_load_worker_manifest_rejects_a_missing_manifest(tmp_path):
    with pytest.raises(JsonStoreError, match="Could not read JSON object"):
        sources_module.load_worker_manifest(tmp_path)


def test_load_worker_manifest_rejects_an_invalid_manifest(tmp_path):
    sources_module._write_worker_manifest(
        tmp_path,
        source_kind="git",
        source_identity="/repositories/repo_A",
        source_reference="a" * 40,
        claim=_claim(),
    )
    payload = load_json_object(tmp_path / WORKER_MANIFEST_FILE)
    payload["kind"] = "something_else"
    save_json_object(tmp_path / WORKER_MANIFEST_FILE, payload)

    with pytest.raises(WorkerManifestError, match="Unsupported worker workspace manifest"):
        sources_module.load_worker_manifest(tmp_path)
