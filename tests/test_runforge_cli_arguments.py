"""Tests for effective CLI argument summaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

from runforge.cli import main


PLAN_CREATED_PREFIX = "Experiment plan created at: "


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "train.py").write_text("print('ran')\n", encoding="utf-8")
    _git(repository, "add", "train.py")
    _git(repository, "commit", "-m", "initial")
    return repository


def _planned_path(output: str) -> Path:
    line = next(line for line in output.splitlines() if line.startswith(PLAN_CREATED_PREFIX))
    return Path(line.removeprefix(PLAN_CREATED_PREFIX))


def test_plan_summary_shows_resolved_defaults_quoted_command_and_redacted_environment(tmp_path, monkeypatch, capsys):
    repository = _repository(tmp_path)
    environment_file = repository / "runforge.env"
    environment_file.write_text("API_TOKEN=very-secret\nRUN_MODE=test\n", encoding="utf-8")
    monkeypatch.chdir(repository)

    assert main(["plan", "--env-file", str(environment_file), "--", "python", "train.py", "two words"]) == 0

    output = capsys.readouterr().out
    assert "RunForge plan effective arguments:" in output
    assert "  name: exp" in output
    assert f"  output root: {repository / 'reports'}" in output
    assert f"  source path: {repository}" in output
    assert "  source mode: current-head" in output
    assert "  commit/ref: not set" in output
    assert "  patch: not set" in output
    assert f"  environment file: {environment_file}" in output
    assert "  environment keys: API_TOKEN, RUN_MODE" in output
    assert "  command mode: argv" in output
    assert "  shell mode: disabled" in output
    assert "  command: python train.py 'two words'" in output
    assert "very-secret" not in output


def test_run_summary_uses_recorded_configuration_without_environment_values(tmp_path, capsys):
    repository = _repository(tmp_path)
    reports = tmp_path / "reports"
    environment_file = tmp_path / "runforge.env"
    environment_file.write_text("API_TOKEN=very-secret\n", encoding="utf-8")
    assert (
        main(
            [
                "plan",
                "--source-path",
                str(repository),
                "--out-dir",
                str(reports),
                "--env-file",
                str(environment_file),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    experiment = _planned_path(capsys.readouterr().out)

    assert main(["run", str(experiment)]) == 0

    output = capsys.readouterr().out
    assert "RunForge run effective arguments:" in output
    assert f"  experiment: {experiment}" in output
    assert "  stream output: disabled" in output
    assert "  recorded command: python train.py" in output
    assert "  environment keys: API_TOKEN" in output
    assert f"  artifact directory: {experiment / 'artifacts'}" in output
    assert f"  stdout log: {experiment / 'stdout.log'}" in output
    assert f"  stderr log: {experiment / 'stderr.log'}" in output
    assert f"Preparing experiment: {experiment}" in output
    assert "Executing command: python train.py" in output
    assert f"Experiment completed with exit code 0: {experiment}" in output
    assert "very-secret" not in output
