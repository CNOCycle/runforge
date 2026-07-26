"""Integration tests for the verified-directory and directory-snapshot CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from runforge.cli import main
from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.schemas.experiment import ExperimentConfiguration
from tests.support import planned_path


CLI_ERROR_EXIT = 2
MATRIX_PLAN_COUNT = 2


def _source(tmp_path: Path, script: str) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "train.py").write_text(script, encoding="utf-8")
    return source


def test_cli_plans_and_runs_a_verified_directory_experiment(tmp_path, capsys):
    source = _source(
        tmp_path,
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('result.txt').write_text('ran')\n",
    )

    assert (
        main(
            [
                "plan",
                "--name",
                "verified",
                "--source-mode",
                "verified-directory",
                "--source-path",
                str(source),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    experiment = planned_path(output)
    assert "  source mode: verified-directory" in output
    assert "  commit/ref: not set" in output
    configuration = ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))
    assert configuration.source.to_dict()["kind"] == "runforge_verified_directory_source"
    assert (experiment / "source-manifest.json").is_file()
    assert not (experiment / "git.patch").exists()
    assert experiment.parent.name == "verified"

    assert main(["run", str(experiment)]) == 0
    run_output = capsys.readouterr().out
    assert f"Experiment completed with exit code 0: {experiment}" in run_output
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "ran"


def test_cli_plans_a_directory_snapshot_experiment_that_outlives_and_runs_after_its_source_is_removed(tmp_path, capsys):
    source = _source(
        tmp_path,
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['RUNFORGE_ARTIFACT_DIR']).joinpath('result.txt').write_text('ran')\n",
    )

    assert (
        main(
            [
                "plan",
                "--name",
                "snapshot",
                "--source-mode",
                "directory-snapshot",
                "--source-path",
                str(source),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    experiment = planned_path(output)
    assert "  source mode: directory-snapshot" in output
    assert (experiment / "source" / "train.py").is_file()
    assert experiment.parent.name == "snapshot"

    shutil.rmtree(source)

    assert main(["run", str(experiment)]) == 0
    run_output = capsys.readouterr().out
    assert f"Experiment completed with exit code 0: {experiment}" in run_output
    assert (experiment / "artifacts" / "result.txt").read_text(encoding="utf-8") == "ran"


def test_cli_creates_a_verified_directory_matrix(tmp_path, capsys):
    source = _source(tmp_path, "print('ran')\n")
    matrix_file = tmp_path / "matrix.json"
    save_json_object(matrix_file, {"SEED": [1, 2]})

    assert (
        main(
            [
                "matrix",
                "--matrix-file",
                str(matrix_file),
                "--source-mode",
                "verified-directory",
                "--source-path",
                str(source),
                "--out-dir",
                str(tmp_path / "reports"),
                "--",
                "python",
                "train.py",
                "--seed={SEED}",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    lines = output.splitlines()
    summary_index = lines.index(f"Experiment plans created ({MATRIX_PLAN_COUNT}):")
    experiments = tuple(Path(line.strip()) for line in lines[summary_index + 1 :])
    assert len(experiments) == MATRIX_PLAN_COUNT
    assert all(experiment.parent.name == "verified" for experiment in experiments)
    assert len({experiment.name for experiment in experiments}) == MATRIX_PLAN_COUNT


def test_discover_cli_lists_non_git_sourced_experiments(tmp_path, capsys):
    verified_source = _source(tmp_path, "print('ran')\n")
    reports = tmp_path / "reports"
    assert (
        main(
            [
                "plan",
                "--name",
                "verified",
                "--source-mode",
                "verified-directory",
                "--source-path",
                str(verified_source),
                "--out-dir",
                str(reports),
                "--",
                "python",
                "train.py",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["discover", str(reports)]) == 0

    output = capsys.readouterr().out
    assert "source=verified@" in output
    assert "invalid: 0" in output


def test_cli_rejects_missing_out_dir_for_verified_directory_mode(tmp_path, capsys):
    source = _source(tmp_path, "print('should not run')\n")

    exit_code = main(
        [
            "plan",
            "--source-mode",
            "verified-directory",
            "--source-path",
            str(source),
            "--",
            "python",
            "train.py",
        ]
    )

    assert exit_code == CLI_ERROR_EXIT
    assert "output_root is required" in capsys.readouterr().err


def test_cli_rejects_output_root_inside_directory_snapshot_source(tmp_path, capsys):
    source = _source(tmp_path, "print('should not run')\n")

    exit_code = main(
        [
            "plan",
            "--source-mode",
            "directory-snapshot",
            "--source-path",
            str(source),
            "--out-dir",
            str(source / "reports"),
            "--",
            "python",
            "train.py",
        ]
    )

    assert exit_code == CLI_ERROR_EXIT
    assert "outside the source directory" in capsys.readouterr().err


def test_cli_rejects_commit_and_patch_with_a_non_git_source_mode(tmp_path, capsys):
    source = _source(tmp_path, "print('should not run')\n")

    exit_code = main(
        [
            "plan",
            "--source-mode",
            "verified-directory",
            "--source-path",
            str(source),
            "--out-dir",
            str(tmp_path / "reports"),
            "--commit",
            "HEAD",
            "--",
            "python",
            "train.py",
        ]
    )

    assert exit_code == CLI_ERROR_EXIT
    assert "--commit and --patch require --source-mode pinned-git" in capsys.readouterr().err
