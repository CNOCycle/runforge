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


def test_worker_inspect_renders_a_workspace_manifest(tmp_path, capsys):
    workspace = _workspace(tmp_path)

    assert main(["worker-inspect", str(workspace)]) == 0

    output = capsys.readouterr().out
    assert "RunForge worker-inspect effective arguments:" in output
    assert f"  workspace: {workspace}" in output
    assert f"Worker workspace manifest ({workspace.name}):" in output
    assert "  source kind: git" in output
    assert "  source identity: /repositories/repo_A" in output
    assert f"  source reference: {'a' * 40}" in output
    assert "  host: node7" in output
    assert "  pid: 4127" in output
    assert "  owner: node7:4127" in output
    assert "  worker id: t" in output
    assert "  started at: 2026-07-30T18:15:30.000000Z" in output


def test_worker_inspect_leaves_the_manifest_untouched(tmp_path):
    workspace = _workspace(tmp_path)
    before = (workspace / WORKER_MANIFEST_FILE).read_bytes()

    assert main(["worker-inspect", str(workspace)]) == 0

    assert (workspace / WORKER_MANIFEST_FILE).read_bytes() == before


def test_worker_inspect_rejects_a_workspace_with_no_manifest(tmp_path, capsys):
    workspace = tmp_path / "runforge-worker-no-manifest"
    workspace.mkdir()

    assert main(["worker-inspect", str(workspace)]) == CLI_ERROR_EXIT

    assert "Could not read JSON object" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "something_else"),
        ("schema_version", 2),
    ],
)
def test_worker_inspect_rejects_an_unsupported_manifest(tmp_path, capsys, field, value):
    workspace = _workspace(tmp_path)
    payload = load_json_object(workspace / WORKER_MANIFEST_FILE)
    payload[field] = value
    save_json_object(workspace / WORKER_MANIFEST_FILE, payload)

    assert main(["worker-inspect", str(workspace)]) == CLI_ERROR_EXIT

    assert "Unsupported worker workspace manifest" in capsys.readouterr().err


def test_worker_inspect_rejects_a_manifest_missing_fields(tmp_path, capsys):
    workspace = tmp_path / "runforge-worker-incomplete-manifest"
    workspace.mkdir()
    save_json_object(workspace / WORKER_MANIFEST_FILE, {"kind": "runforge_worker_manifest", "schema_version": 1})

    assert main(["worker-inspect", str(workspace)]) == CLI_ERROR_EXIT

    assert "Missing worker workspace manifest field(s)" in capsys.readouterr().err
