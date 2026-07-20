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
    assert "  input tree: not set" in output
    assert "  planned inputs: 0" in output
    assert "  command mode: argv" in output
    assert "  shell mode: disabled" in output
    assert "  command: python train.py 'two words'" in output
    assert "very-secret" not in output


def test_plan_captures_a_json_input_tree_and_reports_its_effective_arguments(tmp_path, capsys):
    repository = _repository(tmp_path)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "train.json").write_text('{"output": "{ARTIFACT_DIR}/training"}\n', encoding="utf-8")
    (inputs / "notes.txt").write_text("copied\n", encoding="utf-8")

    assert (
        main(
            [
                "plan",
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--input-tree",
                str(inputs),
                "--",
                "python",
                "train.py",
                "{INPUT_DIR}/train.json",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    experiment = planned_path(output)
    assert f"  input tree: {inputs}" in output
    assert "  planned inputs: 2" in output
    assert (experiment / "inputs/notes.txt").read_text(encoding="utf-8") == "copied\n"
    assert (experiment / "inputs/train.json").read_text(encoding="utf-8") == (
        '{\n  "output": "' + str(experiment / "artifacts/training") + '"\n}\n'
    )


def test_plan_renders_yaml_yml_and_ini_files_from_an_input_tree(tmp_path, capsys):
    repository = _repository(tmp_path)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "train.yaml").write_text("output: {ARTIFACT_DIR}/training\n", encoding="utf-8")
    (inputs / "recipe.yml").write_text("output: {ARTIFACT_DIR}/recipe\n", encoding="utf-8")
    (inputs / "eval.ini").write_text("[eval]\ncheckpoint={ARTIFACT_DIR}/training/model.pt\n", encoding="utf-8")

    assert (
        main(
            [
                "plan",
                "--source-path",
                str(repository),
                "--out-dir",
                str(tmp_path / "reports"),
                "--input-tree",
                str(inputs),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )

    experiment = planned_path(capsys.readouterr().out)
    assert (experiment / "inputs/train.yaml").read_text(encoding="utf-8") == (
        f"output: {experiment}/artifacts/training\n"
    )
    assert (experiment / "inputs/recipe.yml").read_text(encoding="utf-8") == (
        f"output: {experiment}/artifacts/recipe\n"
    )
    assert (experiment / "inputs/eval.ini").read_text(encoding="utf-8") == (
        f"[eval]\ncheckpoint={experiment}/artifacts/training/model.pt\n"
    )


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
