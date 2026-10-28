"""Tests for the read-only worker workspace manifest inspection command."""

from __future__ import annotations

from pathlib import Path

import pytest

from runforge.cli import main
from runforge.infrastructure.claims import ExperimentClaim
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.schemas.worker_manifest import WORKER_MANIFEST_FILE, WorkerManifest


CLI_ERROR_EXIT = 2


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "runforge-worker-repo_A-865dc691-20260730T181530Z-p4127-a13f"
    workspace.mkdir()
    manifest = WorkerManifest(
        source_kind="git",
        source_identity="/repositories/repo_A",
        source_reference="a" * 40,
        host="node7",
        pid=4127,
        owner="node7:4127",
        worker_id=ExperimentClaim(token="t", owner="o", acquired_at="a").token,
        started_at="2026-07-30T18:15:30.000000Z",
    )
    save_json_object(workspace / WORKER_MANIFEST_FILE, manifest.to_dict())
    return workspace


def test_workspace_listing_provides_identity_without_separate_inspection(tmp_path, capsys):
    workspace = _workspace(tmp_path)

    assert main(["workspaces", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    for text in (
        workspace.name,
        "/repositories/repo_A",
        "a" * 40,
        "host: node7",
        "pid: 4127",
        "owner: node7:4127",
        "2026-07-30T18:15:30.000000Z",
        "process activity is not verified",
    ):
        assert text in output


def test_workspace_listing_leaves_manifest_untouched(tmp_path):
    workspace = _workspace(tmp_path)
    before = (workspace / WORKER_MANIFEST_FILE).read_bytes()

    assert main(["workspaces", str(tmp_path)]) == 0

    assert workspace.exists()
    assert (workspace / WORKER_MANIFEST_FILE).read_bytes() == before


def test_workspace_listing_reports_unknown_identity_without_removing_it(tmp_path, capsys):
    workspace = tmp_path / "runforge-worker-no-manifest"
    workspace.mkdir()

    assert main(["workspaces", str(tmp_path)]) == 0

    assert "manifest unavailable" in capsys.readouterr().out
    assert workspace.exists()


@pytest.mark.parametrize(("field", "value"), [("kind", "something_else"), ("schema_version", 2)])
def test_workspace_listing_reports_unsupported_manifest(tmp_path, capsys, field, value):
    workspace = _workspace(tmp_path)
    payload = load_json_object(workspace / WORKER_MANIFEST_FILE)
    payload[field] = value
    save_json_object(workspace / WORKER_MANIFEST_FILE, payload)

    assert main(["workspaces", str(tmp_path)]) == 0

    assert "Unsupported worker workspace manifest" in capsys.readouterr().out
    assert workspace.exists()


def test_workspace_listing_missing_root_is_an_error(tmp_path, capsys):
    assert main(["workspaces", str(tmp_path / "missing")]) == CLI_ERROR_EXIT
    assert "does not exist" in capsys.readouterr().err
