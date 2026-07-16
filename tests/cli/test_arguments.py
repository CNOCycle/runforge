"""Tests for effective CLI argument summaries."""

from __future__ import annotations

from pathlib import Path

from runforge.cli import main
from tests.support import create_git_repository, planned_path


def _repository(tmp_path: Path) -> Path:
    return create_git_repository(tmp_path / "repository", {"train.py": "print('ran')\n"})


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
    experiment = planned_path(capsys.readouterr().out)

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
