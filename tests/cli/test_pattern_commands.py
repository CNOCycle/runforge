"""Wildcard patterns accepted by discover, run and retry through the shared expansion."""

from __future__ import annotations

import json
import re
import sys

import pytest

from runforge.cli import main
from tests.support import create_git_repository


GROUP_SIZE = 15
GROUPS = ("baseline", "ablation", "control")
CLI_ERROR = 2


@pytest.fixture
def reports(tmp_path, capsys):
    """Forty-five failing plans in three named groups, none executed yet."""
    repository = create_git_repository(tmp_path / "source", {"train.py": "import sys\nsys.exit(1)\n"})
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"SEED": list(range(GROUP_SIZE))}))
    root = tmp_path / "reports"
    for name in GROUPS:
        arguments = ["--name", name, "--matrix", str(matrix), "--out-dir", str(root), "--source-path", str(repository)]
        assert main(["plan", *arguments, "--", sys.executable, "train.py", "{SEED}"]) == 0
    capsys.readouterr()
    return root


def _attempts(root, capsys):
    """Attempt numbers per group, read from discover output."""
    assert main(["discover", str(root)]) == 0
    output = capsys.readouterr().out
    found = {name: [] for name in GROUPS}
    for attempt, name in re.findall(r"attempt=(\d+) \| name=(\w+)", output):
        found[name].append(int(attempt))
    return found


def _expect(attempts, **expected):
    assert {name: sorted(set(values)) for name, values in attempts.items()} == {
        name: [expected.get(name, 0)] for name in GROUPS
    }
    assert all(len(values) == GROUP_SIZE for values in attempts.values())


def test_discover_pattern_lists_only_the_matching_group(reports, capsys):
    assert main(["discover", str(reports / "**" / "*baseline*")]) == 0
    output = capsys.readouterr().out
    assert output.count("name=baseline") == GROUP_SIZE
    assert "name=ablation" not in output
    assert "name=control" not in output


def test_run_pattern_executes_only_the_matching_group(reports, capsys):
    assert main(["run", str(reports / "**" / "*baseline*")]) == 1
    capsys.readouterr()
    _expect(_attempts(reports, capsys), baseline=1)


def test_retry_pattern_retries_only_the_matching_failed_group(reports, capsys):
    assert main(["run", str(reports)]) == 1
    capsys.readouterr()
    assert main(["retry", str(reports / "**" / "*baseline*")]) == 1
    capsys.readouterr()
    _expect(_attempts(reports, capsys), baseline=2, ablation=1, control=1)


def test_forced_retry_accepts_a_pattern_and_keeps_its_directory_check(reports, capsys):
    assert main(["run", str(reports)]) == 1
    capsys.readouterr()
    assert main(["retry", "--force", str(reports / "**" / "*ablation*")]) == 1
    capsys.readouterr()
    _expect(_attempts(reports, capsys), baseline=1, ablation=2, control=1)
    assert main(["retry", "--force", str(reports / "**")]) == CLI_ERROR
    assert "explicit experiment-directory paths" in capsys.readouterr().err
    _expect(_attempts(reports, capsys), baseline=1, ablation=2, control=1)


@pytest.mark.parametrize("command", [["discover"], ["run"], ["retry"], ["retry", "--force"]])
def test_pattern_without_matches_exits_2_before_any_change(reports, capsys, command):
    assert main([*command, str(reports / "**" / "*no-such-group*")]) == CLI_ERROR
    assert "No directories match pattern" in capsys.readouterr().err
    _expect(_attempts(reports, capsys))


def test_existing_literal_path_keeps_precedence_over_pattern_reading(tmp_path, capsys):
    repository = create_git_repository(tmp_path / "source", {"train.py": "print('ok')\n"})
    root = tmp_path / "reports [2026]"
    assert (
        main(["plan", "--source-path", str(repository), "--out-dir", str(root), "--", sys.executable, "train.py"]) == 0
    )
    capsys.readouterr()
    assert main(["run", str(root)]) == 0
    assert "completed: 1" in capsys.readouterr().out
