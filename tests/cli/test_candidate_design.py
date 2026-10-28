"""End-to-end differences between the independently runnable CLI candidates."""

from __future__ import annotations

import pytest

import runforge.execution.worker as worker_module
from runforge.cli import commands, main
from runforge.execution.scheduling import set_hold
from runforge.infrastructure.claims import ClaimError, try_acquire_claim
from runforge.infrastructure.json_store import load_json_object
from runforge.infrastructure.storage import ExperimentDirectory
from tests.support import create_git_repository, planned_path


HOLD = ("schedule", "--hold")
RELEASE = ("schedule", "--release")
MATRIX = ("plan", "--matrix")
HELD_EXIT = 0
FAILURE_EXIT = 1

CLI_ERROR = 2
CHILD_FAILURE = 7
MATRIX_SIZE = 2


def _plan(tmp_path, capsys, script="print('done')\n"):
    repository = create_git_repository(tmp_path / "repository", {"train.py": script})
    assert (
        main(
            [
                "plan",
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    return planned_path(capsys.readouterr().out)


def test_hold_and_release_change_future_execution_without_changing_inputs(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys)
    configuration = (experiment / "config.json").read_bytes()
    status = (experiment / "status.json").read_bytes()

    assert main([*HOLD, str(experiment)]) == 0
    assert main(["run", str(experiment)]) == HELD_EXIT
    assert (experiment / "config.json").read_bytes() == configuration
    assert (experiment / "status.json").read_bytes() == status
    assert not (experiment / "claim").exists()
    assert main([*RELEASE, str(experiment)]) == 0
    assert main(["run", str(experiment)]) == 0
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 1


def test_launch_failure_uses_candidate_exit_contract_but_records_child_code(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"train.py": "raise SystemExit(7)\n"})

    result = main(
        ["launch", "--source-path", str(repository), "--out-dir", str(tmp_path / "reports"), "--", "python", "train.py"]
    )

    experiment = planned_path(capsys.readouterr().out)
    assert result == FAILURE_EXIT
    assert ExperimentDirectory.resolve(experiment).load_status().exit_code == CHILD_FAILURE


def test_held_retry_does_not_archive_or_increment_attempt(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys, "raise SystemExit(7)\n")
    assert main(["run", str(experiment)]) == FAILURE_EXIT
    failed = (experiment / "status.json").read_bytes()
    capsys.readouterr()
    assert main([*HOLD, str(experiment)]) == 0

    assert main(["retry", str(experiment)]) == HELD_EXIT

    assert (experiment / "status.json").read_bytes() == failed
    assert not list(experiment.glob("attempt-*"))
    assert not (experiment / "claim").exists()


def test_matrix_child_tokens_are_recorded_literally_and_never_executed(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"tracked.txt": "source\n"})
    axes = tmp_path / "axes.json"
    axes.write_text('{"SEED": [1, 2]}\n')
    child = ["matrix", "show", "--force", "two words", "{SEED}"]

    assert (
        main(
            [*MATRIX, str(axes), "--source-path", str(repository), "--out-dir", str(tmp_path / "reports"), "--", *child]
        )
        == 0
    )

    plans = sorted((tmp_path / "reports").glob("*/*/config.json"))
    assert len(plans) == MATRIX_SIZE
    for configuration in plans:
        saved = load_json_object(configuration)
        assert saved["command"]["arguments"][:-1] == child[:-1]
        assert saved["command"]["arguments"][-1] in ("1", "2")
        assert not (configuration.parent / "stdout.log").exists()


def test_repeated_completed_run_has_candidate_specific_skip_contract(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys)
    assert main(["run", str(experiment)]) == 0
    before = (experiment / "status.json").read_bytes()

    assert main(["run", str(experiment)]) == HELD_EXIT

    assert (experiment / "status.json").read_bytes() == before
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 1


def test_no_legacy_workspace_leaf_or_priority_command_is_advertised(capsys):
    with pytest.raises(SystemExit) as result:
        main(["worker-inspect", "workspace"])
    assert result.value.code == CLI_ERROR


def test_missing_later_path_prevents_execution_of_an_earlier_valid_target(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys)

    assert main(["run", str(experiment), str(tmp_path / "missing")]) == CLI_ERROR

    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 0
    assert not (experiment / "stdout.log").exists()


def test_empty_execution_root_is_an_error_not_a_successful_skip(tmp_path, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()

    assert main(["run", str(reports)]) == CLI_ERROR

    assert "No experiments found" in capsys.readouterr().err


def test_cli_continues_after_claim_release_error_and_preserves_history(tmp_path, capsys, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    experiment = _plan(tmp_path / "a", capsys)
    following = _plan(tmp_path / "b", capsys)
    before = (experiment / "status.json").read_bytes()
    capsys.readouterr()
    acquire_claim = worker_module.try_acquire_claim
    release_claim = worker_module.release_claim

    def hold_before_claim(layout):
        if layout.root == experiment:
            set_hold(layout.root, held=True)
        return acquire_claim(layout)

    def fail_release(layout, claim):
        if layout.root == experiment:
            raise ClaimError("could not release execution claim")
        release_claim(layout, claim)

    monkeypatch.setattr(worker_module, "release_claim", fail_release)
    monkeypatch.setattr(worker_module, "try_acquire_claim", hold_before_claim)

    assert main(["run", str(experiment), str(following)]) == 0

    captured = capsys.readouterr()
    assert captured.err.count("Could not release claim: could not release execution claim") == 1
    assert "Execution summary:" in captured.out
    assert "  completed: 1" in captured.out
    assert "  failed: 0" in captured.out
    assert "  held: 1" in captured.out
    assert (experiment / "status.json").read_bytes() == before
    assert (experiment / "claim").is_dir()
    assert not (experiment / "stdout.log").exists()
    assert ExperimentDirectory.resolve(following).load_status().state == "completed"


def test_cli_reports_unhandled_claim_errors(tmp_path, capsys, monkeypatch):
    def fail_claim(*_args, **_kwargs):
        raise ClaimError("could not acquire execution claim")

    monkeypatch.setattr(commands, "execute_paths", fail_claim)

    assert main(["run", str(tmp_path)]) == CLI_ERROR

    assert capsys.readouterr().err == "error: could not acquire execution claim\n"


def test_force_retry_rejects_an_unusable_claim_without_counting_an_execution_failure(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys, "raise SystemExit(7)\n")
    assert main(["run", str(experiment)]) == FAILURE_EXIT
    before = (experiment / "status.json").read_bytes()
    output = (experiment / "stdout.log").read_bytes()
    (experiment / "claim").write_text("not a claim directory\n")
    capsys.readouterr()

    assert main(["retry", "--force", str(experiment)]) == CLI_ERROR

    captured = capsys.readouterr()
    assert "Claim path is not a directory" in captured.err
    assert "  selected: 0" in captured.out
    assert "  failed: 0" in captured.out
    assert "  invalid: 1" in captured.out
    assert (experiment / "status.json").read_bytes() == before
    assert (experiment / "stdout.log").read_bytes() == output
    assert (experiment / "claim").is_file()
    assert not list(experiment.glob("attempt-*"))


@pytest.mark.parametrize("explicit", [True, False])
def test_retry_claim_contention_keeps_history_and_guides_explicit_targets(tmp_path, capsys, explicit):
    experiment = _plan(tmp_path, capsys, "raise SystemExit(7)\n")
    assert main(["run", str(experiment)]) == FAILURE_EXIT
    layout = ExperimentDirectory.resolve(experiment)
    assert try_acquire_claim(layout, owner="node7:123") is not None
    status = layout.status_file.read_bytes()
    claim = layout.claim_file.read_bytes()
    capsys.readouterr()

    target = experiment if explicit else tmp_path / "reports"
    assert main(["retry", str(target)]) == 0

    captured = capsys.readouterr()
    assert "claim contention: 1" in captured.out
    assert "  selected: 0" in captured.out
    if explicit:
        assert "node7:123" in captured.err
        assert "--force" in captured.err
        assert "confirming" in captured.err
    else:
        assert captured.err == ""
    assert layout.status_file.read_bytes() == status
    assert layout.claim_file.read_bytes() == claim
    assert not list(experiment.glob("attempt-*"))


@pytest.mark.parametrize(("action", "held"), [(HOLD, True), (RELEASE, False)])
def test_schedule_repairs_invalid_controls_without_changing_execution_metadata(tmp_path, capsys, action, held):
    experiment = _plan(tmp_path, capsys)
    before = {name: (experiment / name).read_bytes() for name in ("config.json", "status.json")}
    (experiment / "schedule.json").write_text('{"held": "broken"}\n')

    assert main([*action, str(experiment)]) == 0

    assert load_json_object(experiment / "schedule.json")["held"] is held
    assert {name: (experiment / name).read_bytes() for name in before} == before
    assert not (experiment / "claim").exists()


def test_bulk_schedule_updates_valid_plans_and_reports_damaged_metadata(tmp_path, capsys):
    (tmp_path / "valid").mkdir()
    (tmp_path / "broken").mkdir()
    valid = _plan(tmp_path / "valid", capsys)
    broken = _plan(tmp_path / "broken", capsys)
    (valid / "schedule.json").write_text("{bad control\n")
    (broken / "status.json").write_text("{bad status\n")

    assert main([*HOLD, str(tmp_path)]) == CLI_ERROR

    assert load_json_object(valid / "schedule.json")["held"] is True
    assert (broken / "status.json").read_text() == "{bad status\n"
    assert not (broken / "schedule.json").exists()
    assert str(broken) in capsys.readouterr().err
    assert not (valid / "claim").exists()
    assert not (broken / "claim").exists()


def test_schedule_cannot_repair_controls_while_an_execution_claim_is_held(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys)
    (experiment / "schedule.json").write_text("{bad control\n")
    assert try_acquire_claim(ExperimentDirectory.resolve(experiment), owner="busy-worker") is not None

    assert main([*RELEASE, str(experiment)]) == CLI_ERROR

    assert "busy-worker" in capsys.readouterr().err
    assert (experiment / "schedule.json").read_text() == "{bad control\n"
    assert (experiment / "claim").exists()


def test_refill_discovery_error_keeps_completed_work_and_prints_the_summary(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    held = _plan(tmp_path / "b", capsys)
    set_hold(held, held=True)
    report_root = tmp_path / "b" / "reports"
    moved = tmp_path / "moved-reports"
    first = _plan(
        tmp_path / "a",
        capsys,
        f"from pathlib import Path\nPath({str(report_root)!r}).rename({str(moved)!r})\nprint('done')\n",
    )

    assert main(["run", str(first), str(report_root)]) == CLI_ERROR

    captured = capsys.readouterr()
    assert "Execution summary:" in captured.out
    assert "  selected: 1" in captured.out
    assert "  completed: 1" in captured.out
    assert "  failed: 0" in captured.out
    assert "  invalid: 1" in captured.out
    assert "Could not refill queue" in captured.err
    assert str(report_root) in captured.err
    assert ExperimentDirectory.resolve(first).load_status().state == "completed"
    relocated = moved / held.relative_to(report_root)
    assert ExperimentDirectory.resolve(relocated).load_status().attempt == 0


def test_command_start_failure_is_reported_once_and_keeps_the_summary(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "repository", {"tracked.txt": "source\n"})
    missing_command = tmp_path / "missing-executable"
    assert (
        main(
            [
                "plan",
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                str(missing_command),
            ]
        )
        == 0
    )
    experiment = planned_path(capsys.readouterr().out)

    assert main(["run", str(experiment)]) == FAILURE_EXIT

    captured = capsys.readouterr()
    assert captured.err.count("Could not start command") == 1
    assert "  selected: 1" in captured.out
    assert "  failed: 1" in captured.out
    assert "  invalid: 0" in captured.out
    status = ExperimentDirectory.resolve(experiment).load_status()
    assert status.state == "failed"
    assert status.attempt == 1


def test_malformed_report_root_config_does_not_hide_or_prevent_valid_work(tmp_path, capsys):
    experiment = _plan(tmp_path, capsys)
    root = tmp_path / "reports"
    assert main(["discover", str(root)]) == 0
    assert "  created: 1" in capsys.readouterr().out
    unrelated = root / "config.json"
    unrelated.write_text("{\n // ordinary application comment\n}\n")

    assert main(["discover", str(root)]) == CLI_ERROR

    discovered = capsys.readouterr()
    assert "  created: 1" in discovered.out
    assert "  invalid: 1" in discovered.out
    assert str(unrelated) in discovered.err
    assert main(["run", str(root)]) == CLI_ERROR
    executed = capsys.readouterr()
    assert "  completed: 1" in executed.out
    assert "  invalid: 1" in executed.out
    assert ExperimentDirectory.resolve(experiment).load_status().attempt == 1
