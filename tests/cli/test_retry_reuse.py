"""Checkpoint continuation and ownership through the artifact-reuse retry CLI."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

import runforge.execution.retry as retry_module
from runforge.cli import main
from runforge.execution.scheduling import set_hold
from runforge.execution.worker import BatchRequest, WorkerError, execute_paths
from runforge.infrastructure.claims import try_acquire_claim
from runforge.infrastructure.storage import ExperimentDirectory
from runforge.planning.planner import PlanRequest, plan_experiment
from runforge.schemas.experiment import ExperimentCommand
from tests.support import create_git_repository


SECOND_ATTEMPT = 2
CLI_ERROR = 2
_APPLICATION = """
import os
import sys
from pathlib import Path
checkpoint = Path(os.environ['RUNFORGE_ARTIFACT_DIR']) / 'checkpoint.txt'
if checkpoint.exists():
    previous = int(checkpoint.read_text())
    checkpoint.write_text(str(previous + 2))
    print(f'continued from {previous}')
else:
    checkpoint.write_text(sys.argv[1])
    print('saved checkpoint')
    raise SystemExit(7)
"""


def _plan(repository, reports, *, name="checkpoint", start="3"):
    return ExperimentDirectory(
        plan_experiment(
            PlanRequest(
                name=name,
                source_path=repository,
                output_root=reports,
                command=ExperimentCommand.argv((sys.executable, "train.py", start)),
            )
        )
    )


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("root_selection", [False, True])
def test_retry_checkpoint_continuation_is_opt_in(tmp_path, capsys, reuse, root_selection):
    repository = create_git_repository(tmp_path / "repository", {"train.py": _APPLICATION})
    reports = tmp_path / "reports"
    layout = _plan(repository, reports)
    immutable = {path: path.read_bytes() for path in (layout.configuration_file, layout.root / "cmd.sh")}
    assert main(["run", str(layout.root)]) == 1
    previous_status = layout.status_file.read_bytes()
    capsys.readouterr()
    target = reports if root_selection else layout.root
    options = ["--reuse-artifacts"] if reuse else []

    assert main(["retry", *options, str(target)]) == (0 if reuse else 1)

    captured = capsys.readouterr()
    archive = layout.root / "attempt-0001"
    assert layout.load_status().attempt == SECOND_ATTEMPT
    assert layout.load_status().state == ("completed" if reuse else "failed")
    assert (layout.artifacts / "checkpoint.txt").read_text() == ("5" if reuse else "3")
    assert (archive / "artifacts" / "checkpoint.txt").read_text() == "3"
    assert (archive / "status.snapshot.json").read_bytes() == previous_status
    assert (archive / "stdout.log").read_text() == "saved checkpoint\n"
    assert layout.stdout_log.read_text() == ("continued from 3\n" if reuse else "saved checkpoint\n")
    assert layout.stderr_log.read_text() == ""
    assert {path: path.read_bytes() for path in immutable} == immutable
    assert f"  reuse artifacts: {'enabled' if reuse else 'disabled'}" in captured.out
    assert "restored checkpoint" not in captured.out
    assert not layout.claim.exists()


def test_bulk_retry_reuses_each_experiments_own_artifacts(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": _APPLICATION})
    reports = tmp_path / "reports"
    first = _plan(repository, reports, name="first", start="3")
    second = _plan(repository, reports, name="second", start="7")
    assert main(["run", str(reports)]) == 1
    capsys.readouterr()

    assert main(["retry", "--reuse-artifacts", str(reports)]) == 0

    assert "  completed: 2" in capsys.readouterr().out
    for layout, old, new in [(first, "3", "5"), (second, "7", "9")]:
        assert layout.load_status().attempt == SECOND_ATTEMPT
        assert (layout.artifacts / "checkpoint.txt").read_text() == new
        assert (layout.root / "attempt-0001" / "artifacts" / "checkpoint.txt").read_text() == old


@pytest.mark.parametrize("failure", ["preflight", "copy", "status-reset"])
def test_reuse_preparation_errors_are_invalid_without_an_execution(tmp_path, monkeypatch, capsys, failure):
    repository = create_git_repository(tmp_path / "repository", {"train.py": _APPLICATION})
    reports = tmp_path / "reports"
    layout = _plan(repository, reports)
    assert main(["run", str(layout.root)]) == 1
    before = (layout.status_file.read_bytes(), layout.stdout_log.read_bytes(), layout.stderr_log.read_bytes())
    capsys.readouterr()

    def fail_copy(_source, destination, **_options):
        destination.mkdir()
        (destination / "checkpoint.txt").write_text("partial")
        raise OSError("copy failure")

    def fail_reset(_layout, _status):
        raise OSError("status reset failure")

    if failure == "preflight":
        (layout.artifacts / "link").symlink_to(layout.artifacts / "checkpoint.txt")
    elif failure == "copy":
        monkeypatch.setattr(retry_module.shutil, "copytree", fail_copy)
    else:
        monkeypatch.setattr(ExperimentDirectory, "save_status", fail_reset)

    assert main(["retry", "--reuse-artifacts", str(reports)]) == CLI_ERROR

    captured = capsys.readouterr()
    assert "  selected: 0" in captured.out
    assert "  failed: 0" in captured.out
    assert "  invalid: 1" in captured.out
    assert "Executing command" not in captured.out
    assert str(layout.root) in captured.err
    assert (layout.status_file.read_bytes(), layout.stdout_log.read_bytes(), layout.stderr_log.read_bytes()) == before
    assert (layout.artifacts / "checkpoint.txt").read_text() == "3"
    assert not layout.claim.exists()
    assert not tuple(layout.root.glob("attempt-*"))
    assert not tuple(layout.root.glob(".attempt-*.tmp-*"))


@pytest.mark.parametrize("restriction", ["held", "claimed", "completed", "inprogress"])
def test_reuse_does_not_bypass_selection_or_ownership(tmp_path, capsys, restriction):
    repository = create_git_repository(tmp_path / "repository", {"train.py": _APPLICATION})
    layout = _plan(repository, tmp_path / "reports")
    assert main(["run", str(layout.root)]) == 1
    if restriction == "held":
        set_hold(layout.root, held=True)
    elif restriction == "claimed":
        assert try_acquire_claim(layout, owner="other-worker") is not None
    else:
        layout.save_status(replace(layout.load_status(), state=restriction))
    paths = [path for path in layout.root.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    capsys.readouterr()

    assert main(["retry", "--reuse-artifacts", str(layout.root)]) == 0

    output = capsys.readouterr().out
    assert "  selected: 0" in output
    assert {path: path.read_bytes() for path in paths} == before
    assert not tuple(layout.root.glob("attempt-*"))


def test_force_reuse_still_requires_explicit_paths_and_confirmation(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": _APPLICATION})
    reports = tmp_path / "reports"
    layout = _plan(repository, reports)
    assert main(["run", str(layout.root)]) == 1
    layout.save_status(replace(layout.load_status(), state="inprogress"))
    assert try_acquire_claim(layout, owner="confirmed-stopped-worker") is not None
    before = (layout.status_file.read_bytes(), layout.claim_file.read_bytes())
    capsys.readouterr()

    assert main(["retry", "--force", "--reuse-artifacts", str(reports)]) == CLI_ERROR
    assert (layout.status_file.read_bytes(), layout.claim_file.read_bytes()) == before
    assert not tuple(layout.root.glob("attempt-*"))
    capsys.readouterr()

    assert main(["retry", "--force", "--reuse-artifacts", str(layout.root)]) == 0

    assert "cannot prove that the previous worker has stopped" in capsys.readouterr().err
    assert layout.load_status().attempt == SECOND_ATTEMPT
    assert layout.load_status().state == "completed"
    assert (layout.artifacts / "checkpoint.txt").read_text() == "5"
    assert not layout.claim.exists()


def test_artifact_reuse_requires_retry_in_the_shared_api(tmp_path):
    with pytest.raises(WorkerError, match="Artifact reuse requires retry"):
        execute_paths(BatchRequest((tmp_path,), reuse_artifacts=True))
