"""Scheduling experiments using wildcard and explicit paths."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest

from runforge.cli import main
from runforge.infrastructure.claims import try_acquire_claim
from runforge.infrastructure.storage import ExperimentDirectory
from tests.support import create_git_repository


GROUP_SIZE = 15
CLI_ERROR = 2
EXPLICIT_PATHS = 2
HANG_SECONDS = 20
SHALLOW_DEPTH = 3  # reports/a/<branch>/<experiment>


@pytest.fixture
def groups(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "source", {"train.py": "print('unused')\n"})
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"SEED": list(range(GROUP_SIZE))}))
    reports = tmp_path / "reports with spaces"
    for name in ("baseline", "ablation", "control"):
        assert (
            main(
                [
                    "plan",
                    "--name",
                    name,
                    "--matrix",
                    str(matrix),
                    "--out-dir",
                    str(reports),
                    "--source-path",
                    str(repository),
                    "--",
                    sys.executable,
                    "train.py",
                    "{SEED}",
                ]
            )
            == 0
        )
    capsys.readouterr()
    plans = {
        name: sorted(path.parent for path in reports.glob(f"*/*_{name}_*/config.json"))
        for name in ("baseline", "ablation", "control")
    }
    assert [len(group) for group in plans.values()] == [GROUP_SIZE] * 3
    return reports, plans


def _held(path):
    schedule = path / "schedule.json"
    return json.loads(schedule.read_text())["held"] if schedule.exists() else False


def test_literal_pattern_holds_and_releases_only_matching_group(groups, capsys):
    reports, plans = groups
    branch = plans["baseline"][0].parent
    pattern = str(branch / "*baseline*")
    assert list(branch.glob("*baseline*.json"))
    original = {
        path: ((path / "config.json").read_bytes(), (path / "status.json").read_bytes())
        for group in plans.values()
        for path in group
    }
    for action, held in (("--hold", True), ("--release", False)):
        assert main(["schedule", action, pattern]) == 0
        assert capsys.readouterr().out.count("Held: " if held else "Released: ") == GROUP_SIZE
        assert all(_held(path) is held for path in plans["baseline"])
        assert not any(_held(path) for path in plans["ablation"] + plans["control"])
        assert main(["discover", str(reports)]) == 0
        discovered = capsys.readouterr().out
        assert discovered.count("held=true") == (GROUP_SIZE if held else 0)
    assert all(
        ((path / "config.json").read_bytes(), (path / "status.json").read_bytes()) == before
        for path, before in original.items()
    )


def test_multiple_explicit_paths_and_mixed_overlapping_targets(groups, capsys):
    reports, plans = groups
    baseline = plans["baseline"][0]
    ablation = plans["ablation"][0]
    assert main(["schedule", "--hold", str(baseline), str(ablation)]) == 0
    assert capsys.readouterr().out.count("Held: ") == EXPLICIT_PATHS
    assert _held(baseline) and _held(ablation)

    pattern = str(baseline.parent / "*baseline*")
    assert main(["schedule", "--hold", pattern, str(baseline), str(ablation)]) == 0
    assert capsys.readouterr().out.count("Held: ") == GROUP_SIZE + 1
    assert all(_held(path) for path in plans["baseline"])
    assert main(["schedule", "--release", str(reports), pattern, str(ablation)]) == 0
    assert capsys.readouterr().out.count("Released: ") == 3 * GROUP_SIZE
    assert not any(_held(path) for group in plans.values() for path in group)


@pytest.mark.parametrize("bad_path", ["*does-not-exist*", "missing-literal"])
def test_unmatched_or_missing_path_prevents_all_edits(groups, capsys, bad_path):
    _, plans = groups
    target = plans["baseline"][0]
    assert main(["schedule", "--hold", str(target), str(target.parent / bad_path)]) == CLI_ERROR
    error = capsys.readouterr().err
    assert ("No directories match schedule pattern" if "*" in bad_path else "does not exist") in error
    assert not any((path / "schedule.json").exists() for group in plans.values() for path in group)


def test_wildcard_question_and_character_class_select_paths(groups, capsys):
    _, plans = groups
    pattern = str(plans["ablation"][0].parent / "*abl[a]tio?*")
    assert main(["schedule", "--hold", pattern]) == 0
    assert capsys.readouterr().out.count("Held: ") == GROUP_SIZE
    assert all(_held(path) for path in plans["ablation"])
    assert not any(_held(path) for path in plans["baseline"] + plans["control"])


def test_relative_pattern_and_relative_explicit_path(groups, capsys, monkeypatch):
    _, plans = groups
    branch = plans["baseline"][0].parent
    control = plans["control"][0]
    monkeypatch.chdir(branch)
    assert main(["schedule", "--hold", "*baseline*", control.name]) == 0
    assert capsys.readouterr().out.count("Held: ") == GROUP_SIZE + 1
    assert all(_held(path) for path in plans["baseline"])
    assert _held(control)


def test_existing_literal_path_containing_wildcard_characters(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "source", {"train.py": "print('unused')\n"})
    reports = tmp_path / "reports [literal]"
    assert main(["plan", "--source-path", str(repository), "--out-dir", str(reports), "--", "echo", "sample"]) == 0
    capsys.readouterr()
    assert main(["schedule", "--hold", str(reports)]) == 0
    assert capsys.readouterr().out.count("Held: ") == 1
    assert _held(next(reports.glob("*/*/config.json")).parent)


def test_pattern_preserves_busy_and_invalid_partial_results(groups, capsys):
    _, plans = groups
    busy, broken = plans["baseline"][:2]
    assert try_acquire_claim(ExperimentDirectory.resolve(busy), owner="busy-worker") is not None
    (broken / "status.json").write_text("{broken\n")
    assert main(["schedule", "--hold", str(busy.parent / "*baseline*")]) == CLI_ERROR
    result = capsys.readouterr()
    assert "busy-worker" in result.err
    assert str(broken) in result.err
    assert result.out.count("Held: ") == GROUP_SIZE - 2
    assert not _held(busy)
    assert not _held(broken)
    assert all(_held(path) for path in plans["baseline"][2:])
    assert not any(_held(path) for path in plans["ablation"] + plans["control"])


@pytest.fixture
def nested(tmp_path, capsys):
    """Baseline plans at two depths plus an ablation plan, all under one root."""
    repository = create_git_repository(tmp_path / "source", {"train.py": "print('unused')\n"})
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"SEED": [0, 1]}))
    reports = tmp_path / "reports"
    for name, location in (("baseline", "a"), ("baseline", "deep/er/b"), ("ablation", "a")):
        arguments = ["--name", name, "--matrix", str(matrix), "--out-dir", str(reports / location)]
        arguments += ["--source-path", str(repository), "--", sys.executable, "train.py", "{SEED}"]
        assert main(["plan", *arguments]) == 0
    capsys.readouterr()
    plans = {
        name: sorted(path.parent for path in reports.glob("**/config.json") if f"_{name}_" in path.parent.name)
        for name in ("baseline", "ablation")
    }
    assert [len(group) for group in plans.values()] == [4, 2]
    return reports, plans


def test_globstar_selects_experiments_at_every_depth(nested, capsys):
    reports, plans = nested
    assert list(reports.glob("**/*baseline*.json"))
    pattern = str(reports / "**" / "*baseline*")
    for action, held in (("--hold", True), ("--release", False)):
        assert main(["schedule", action, pattern]) == 0
        assert capsys.readouterr().out.count("Held: " if held else "Released: ") == len(plans["baseline"])
        assert all(_held(path) is held for path in plans["baseline"])
        assert not any(_held(path) for path in plans["ablation"])


def test_globstar_skips_payload_hidden_and_symlinked_directories(nested, capsys):
    reports, plans = nested
    target = plans["baseline"][0]
    (target / "outputs_baseline_copy").mkdir()
    staging = target.parent / ".staging_baseline.tmp-1"
    staging.mkdir()
    (staging / "config.json").write_text("{broken\n")
    (reports / "link_baseline").symlink_to(plans["ablation"][0], target_is_directory=True)
    assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == 0
    result = capsys.readouterr()
    assert result.out.count("Held: ") == len(plans["baseline"])
    assert result.err == ""
    assert all(_held(path) for path in plans["baseline"])
    assert not any(_held(path) for path in plans["ablation"])


def test_globstar_without_matches_prevents_all_edits(nested, capsys):
    reports, plans = nested
    target = plans["baseline"][0]
    assert main(["schedule", "--hold", str(target), str(reports / "**" / "*no-such-group*")]) == CLI_ERROR
    assert "No directories match schedule pattern" in capsys.readouterr().err
    assert not any((path / "schedule.json").exists() for group in plans.values() for path in group)


def test_single_star_and_mid_path_star_stay_within_one_level(nested, capsys):
    reports, plans = nested
    assert main(["schedule", "--hold", str(reports / "*" / "*" / "*baseline*")]) == 0
    capsys.readouterr()
    assert {path for path in plans["baseline"] if _held(path)} == {
        path for path in plans["baseline"] if len(path.relative_to(reports).parts) == SHALLOW_DEPTH
    }


def test_globstar_does_not_cross_symlinked_parent_directories(nested, tmp_path, capsys):
    reports, plans = nested
    outside_root = tmp_path / "outside"
    outside = outside_root / "master"
    outside.mkdir(parents=True)
    external = outside / plans["baseline"][0].name
    shutil.copytree(plans["baseline"][0], external)
    (reports / "linked").symlink_to(outside_root, target_is_directory=True)
    assert list(reports.glob("linked/*/*baseline*"))
    assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == 0
    capsys.readouterr()
    assert not _held(external)
    assert all(_held(path) for path in plans["baseline"])


def test_globstar_stops_at_damaged_experiment_boundary(nested, capsys):
    reports, plans = nested
    outer = plans["baseline"][0]
    (outer / "status.json").unlink()
    (outer / "outputs").mkdir()
    payload = outer / "outputs" / plans["baseline"][1].name
    shutil.copytree(plans["baseline"][1], payload)
    assert main(["schedule", "--hold", str(reports)]) == CLI_ERROR
    capsys.readouterr()
    (payload / "schedule.json").unlink(missing_ok=True)
    assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == CLI_ERROR
    result = capsys.readouterr()
    assert str(outer) in result.err
    assert not (payload / "schedule.json").exists()


def test_globstar_ignores_unrelated_metadata_named_files(nested, capsys):
    reports, plans = nested
    application = reports / "application"
    application.mkdir()
    (application / "config.json").write_text('{"setting": 1}\n')
    (application / "status.json").write_text('{"state": "idle"}\n')
    nested_plan = application / "sub" / "copy_baseline_0000"
    shutil.copytree(plans["baseline"][0], nested_plan)
    assert main(["schedule", "--hold", str(reports)]) == 0
    capsys.readouterr()
    assert main(["schedule", "--release", str(reports)]) == 0
    capsys.readouterr()
    assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == 0
    assert capsys.readouterr().out.count("Held: ") == len(plans["baseline"]) + 1
    assert _held(nested_plan)


class ScheduleHangError(Exception):
    """Raised when a traversal exceeds its deadline."""


@pytest.fixture
def deadline():
    """Fail instead of hanging when a traversal does not terminate."""

    def expired(*_):
        # Not an OSError subclass: directory walkers swallow those.
        raise ScheduleHangError("schedule did not finish")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(HANG_SECONDS)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def test_globstar_terminates_on_self_referencing_symlinks(nested, capsys, deadline):
    reports, plans = nested
    (reports / "loop_one").symlink_to(reports, target_is_directory=True)
    (reports / "loop_two").symlink_to(reports, target_is_directory=True)
    assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == 0
    assert capsys.readouterr().out.count("Held: ") == len(plans["baseline"])


def test_globstar_never_lists_experiment_payload_directories(nested, capsys, monkeypatch):
    reports, plans = nested
    payload = plans["baseline"][0] / "outputs"
    (payload / "deeper" / "nested_baseline").mkdir(parents=True)
    listed = []
    real_scandir = os.scandir

    def recording_scandir(path="."):
        listed.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == 0
    capsys.readouterr()
    assert not [path for path in listed if payload in (path, *path.parents)]
    assert all(_held(path) for path in plans["baseline"])


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("**/.hidden/**/*baseline*", CLI_ERROR), ("**/*baseline*", 0)],
)
def test_globstar_never_selects_inside_hidden_directories(nested, capsys, pattern, expected):
    reports, plans = nested
    hidden = reports / ".hidden" / "master"
    hidden.mkdir(parents=True)
    hidden_copy = hidden / plans["baseline"][0].name
    shutil.copytree(plans["baseline"][0], hidden_copy)
    assert main(["schedule", "--hold", str(reports / pattern)]) == expected
    capsys.readouterr()
    assert not _held(hidden_copy)


def test_explicit_hidden_prefix_is_a_named_root_like_a_plain_path(nested, capsys):
    reports, plans = nested
    hidden = reports / ".hidden" / "master"
    hidden.mkdir(parents=True)
    hidden_copy = hidden / plans["baseline"][0].name
    shutil.copytree(plans["baseline"][0], hidden_copy)
    assert main(["schedule", "--hold", str(reports / ".hidden" / "**" / "*baseline*")]) == 0
    capsys.readouterr()
    assert _held(hidden_copy)
    assert not any(_held(path) for path in plans["baseline"])


def test_more_than_one_globstar_is_rejected_before_any_edit(nested, capsys):
    reports, plans = nested
    target = plans["baseline"][0]
    assert main(["schedule", "--hold", str(target), str(reports / "**" / "master" / "**" / "*baseline*")]) == CLI_ERROR
    assert "At most one **" in capsys.readouterr().err
    assert not any((path / "schedule.json").exists() for group in plans.values() for path in group)


def test_single_star_patterns_follow_the_same_pruning_rules(nested, tmp_path, capsys):
    reports, plans = nested
    outside_root = tmp_path / "outside"
    (outside_root / "master").mkdir(parents=True)
    external = outside_root / "master" / plans["baseline"][0].name
    shutil.copytree(plans["baseline"][0], external)
    (reports / "linked").symlink_to(outside_root, target_is_directory=True)
    hidden = reports / ".hidden" / "master"
    hidden.mkdir(parents=True)
    hidden_copy = hidden / plans["baseline"][0].name
    shutil.copytree(plans["baseline"][0], hidden_copy)
    assert main(["schedule", "--hold", str(reports / "*" / "master" / "*baseline*")]) == 0
    capsys.readouterr()
    assert not _held(external)
    assert not _held(hidden_copy)
    assert any(_held(path) for path in plans["baseline"])


def test_existing_directory_with_wildcard_characters_is_a_literal_prefix(nested, tmp_path, capsys):
    reports, _ = nested
    bracket = tmp_path / "reports [2026]"
    sibling = tmp_path / "reports 2"
    shutil.copytree(reports, bracket)
    shutil.copytree(reports, sibling)
    intended = sorted(path.parent for path in bracket.glob("**/config.json") if "_baseline_" in path.parent.name)
    other = sorted(path.parent for path in sibling.glob("**/config.json") if "_baseline_" in path.parent.name)
    assert intended and other
    assert main(["schedule", "--hold", str(bracket / "**" / "*baseline*")]) == 0
    assert capsys.readouterr().out.count("Held: ") == len(intended)
    assert all(_held(path) for path in intended)
    assert not any(_held(path) for path in other)


@pytest.mark.parametrize("pattern", ["**", "**/*baseline*"])
def test_empty_matched_directory_does_not_block_valid_matches(nested, capsys, pattern):
    reports, plans = nested
    (reports / "baseline-backup").mkdir()
    (reports / "a" / "empty-baseline").mkdir()
    selected = plans["baseline"] + (plans["ablation"] if pattern == "**" else [])
    assert main(["schedule", "--hold", str(reports / pattern)]) == 0
    result = capsys.readouterr()
    assert result.out.count("Held: ") == len(selected)
    assert result.err == ""
    assert all(_held(path) for path in selected)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
@pytest.mark.parametrize("blocked_kind", ["intermediate directory", "matching experiment"])
def test_unreadable_directory_stops_the_command_before_any_edit(nested, capsys, blocked_kind):
    reports, plans = nested
    blocked = reports / "deep" if blocked_kind == "intermediate directory" else plans["baseline"][0]
    blocked.chmod(0)
    try:
        assert main(["schedule", "--hold", str(reports / "**" / "*baseline*")]) == CLI_ERROR
        error = capsys.readouterr().err
    finally:
        blocked.chmod(0o755)
    assert error.count(f"Could not scan directory {blocked}") == 1
    assert not any((path / "schedule.json").exists() for group in plans.values() for path in group)


def test_pattern_matching_only_empty_directories_is_an_error(nested, capsys):
    reports, plans = nested
    (reports / "baseline-backup").mkdir()
    assert main(["schedule", "--hold", str(reports / "*backup*")]) == CLI_ERROR
    assert "No experiments found" in capsys.readouterr().err
    assert not any((path / "schedule.json").exists() for group in plans.values() for path in group)
