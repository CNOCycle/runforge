"""Tests for standard experiment paths and typed metadata persistence."""

from __future__ import annotations

from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.experiment import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.schemas.inputs import PlannedInput, PlannedInputManifest
from runforge.schemas.source import GitSource


def test_experiment_directory_centralizes_paths_and_metadata_round_trip(tmp_path):
    layout = ExperimentDirectory.resolve(tmp_path / "experiment")
    configuration = ExperimentConfiguration(
        experiment_id="experiment",
        name="layout",
        command=ExperimentCommand.argv(("python", "train.py")),
        environment={},
        source=GitSource(
            repository=tmp_path.resolve(),
            commit="a" * 40,
            branch="main",
        ),
        created_at="2026-01-01T00:00:00Z",
    )
    status = ExperimentStatus(state="created", attempt=0, updated_at="2026-01-01T00:00:00Z")
    manifest = PlannedInputManifest(
        entries=(PlannedInput(path="configs/train.yaml", kind="text-template", sha256="b" * 64),)
    )

    layout.save_configuration(configuration)
    assert layout.save_status(status) == status
    layout.save_input_manifest(manifest)
    layout.artifacts.mkdir()

    assert layout.load_metadata() == (configuration, status)
    assert layout.configuration_file == layout.root / "config.json"
    assert layout.status_file == layout.root / "status.json"
    assert layout.command_file == layout.root / "cmd.sh"
    assert layout.git_patch_file == layout.root / "git.patch"
    assert layout.artifacts == layout.root / "artifacts"
    assert layout.inputs == layout.root / "inputs"
    assert layout.input_manifest_file == layout.root / "input-manifest.json"
    assert layout.input_file("configs/train.yaml") == layout.inputs / "configs/train.yaml"
    assert layout.stdout_log == layout.root / "stdout.log"
    assert layout.stderr_log == layout.root / "stderr.log"
    assert layout.source_file("git.patch") == layout.git_patch_file
    assert layout.load_input_manifest() == manifest
