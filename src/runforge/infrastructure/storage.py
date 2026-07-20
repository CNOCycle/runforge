"""Standard paths and metadata persistence for one experiment directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.schemas.experiment import ExperimentConfiguration, ExperimentStatus
from runforge.schemas.inputs import (
    INPUT_MANIFEST_FILE,
    INPUTS_DIRECTORY,
    PlannedInputManifest,
    require_safe_relative_input_path,
)
from runforge.schemas.source import GIT_PATCH_FILE


CONFIGURATION_FILE = "config.json"
STATUS_FILE = "status.json"
COMMAND_FILE = "cmd.sh"
ARTIFACTS_DIRECTORY = "artifacts"
STDOUT_LOG_FILE = "stdout.log"
STDERR_LOG_FILE = "stderr.log"


@dataclass(frozen=True)
class ExperimentDirectory:
    """Paths and typed metadata access for one RunForge experiment."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    @classmethod
    def resolve(cls, path: Path) -> ExperimentDirectory:
        """Create a layout with an expanded, resolved root path."""
        return cls(Path(path).expanduser().resolve())

    @property
    def configuration_file(self) -> Path:
        return self.root / CONFIGURATION_FILE

    @property
    def status_file(self) -> Path:
        return self.root / STATUS_FILE

    @property
    def command_file(self) -> Path:
        return self.root / COMMAND_FILE

    @property
    def git_patch_file(self) -> Path:
        return self.root / GIT_PATCH_FILE

    @property
    def artifacts(self) -> Path:
        return self.root / ARTIFACTS_DIRECTORY

    @property
    def inputs(self) -> Path:
        """Return the immutable per-experiment planned input directory."""
        return self.root / INPUTS_DIRECTORY

    @property
    def input_manifest_file(self) -> Path:
        """Return the immutable planned-input manifest path."""
        return self.root / INPUT_MANIFEST_FILE

    @property
    def stdout_log(self) -> Path:
        return self.root / STDOUT_LOG_FILE

    @property
    def stderr_log(self) -> Path:
        return self.root / STDERR_LOG_FILE

    def source_file(self, filename: str) -> Path:
        """Return a source payload path previously validated as a filename."""
        return self.root / filename

    def input_file(self, relative_path: str) -> Path:
        """Return one validated path below the immutable input directory."""
        return self.inputs / require_safe_relative_input_path(relative_path)

    def load_configuration(self) -> ExperimentConfiguration:
        """Load and validate immutable experiment configuration."""
        return ExperimentConfiguration.from_dict(load_json_object(self.configuration_file))

    def load_status(self) -> ExperimentStatus:
        """Load and validate mutable experiment status."""
        return ExperimentStatus.from_dict(load_json_object(self.status_file))

    def load_input_manifest(self) -> PlannedInputManifest:
        """Load and validate immutable planned-input metadata."""
        return PlannedInputManifest.from_dict(load_json_object(self.input_manifest_file))

    def load_metadata(self) -> tuple[ExperimentConfiguration, ExperimentStatus]:
        """Load the immutable configuration and mutable status together."""
        return self.load_configuration(), self.load_status()

    def save_configuration(self, configuration: ExperimentConfiguration) -> None:
        """Persist immutable experiment configuration atomically."""
        save_json_object(self.configuration_file, configuration.to_dict())

    def save_status(self, status: ExperimentStatus) -> ExperimentStatus:
        """Persist mutable experiment status atomically and return it."""
        save_json_object(self.status_file, status.to_dict())
        return status

    def save_input_manifest(self, manifest: PlannedInputManifest) -> None:
        """Persist immutable planned-input metadata atomically."""
        save_json_object(self.input_manifest_file, manifest.to_dict())
