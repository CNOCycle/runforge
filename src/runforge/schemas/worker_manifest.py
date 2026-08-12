"""Versioned worker workspace manifest schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from runforge.schemas.validation import require_exact_fields, require_object, require_text


WORKER_MANIFEST_FILE = "runforge-worker.json"
WORKER_MANIFEST_KIND = "runforge_worker_manifest"
WORKER_MANIFEST_SCHEMA_VERSION = 1

_SOURCE_KINDS = ("git", "directory-snapshot")


class WorkerManifestError(ValueError):
    """Raised when a worker workspace manifest is invalid or cannot be decoded."""


@dataclass(frozen=True)
class WorkerManifest:
    """Identifies which source, host, and process built one worker workspace.

    Names and manifest fields improve identification; they do not prove
    liveness. PID reuse, containers, schedulers, and shared filesystems still
    require operator confirmation before treating a workspace as abandoned.
    """

    source_kind: Literal["git", "directory-snapshot"]
    source_identity: str
    source_reference: str
    host: str
    pid: int
    owner: str
    worker_id: str
    started_at: str

    def __post_init__(self) -> None:
        if self.source_kind not in _SOURCE_KINDS:
            raise WorkerManifestError(f"source_kind must be one of {', '.join(_SOURCE_KINDS)}")
        require_text(self.source_identity, "source_identity", WorkerManifestError)
        require_text(self.source_reference, "source_reference", WorkerManifestError)
        require_text(self.host, "host", WorkerManifestError)
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise WorkerManifestError("pid must be a positive integer")
        require_text(self.owner, "owner", WorkerManifestError)
        require_text(self.worker_id, "worker_id", WorkerManifestError)
        require_text(self.started_at, "started_at", WorkerManifestError)

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned JSON object persisted alongside the workspace."""
        return {
            "kind": WORKER_MANIFEST_KIND,
            "schema_version": WORKER_MANIFEST_SCHEMA_VERSION,
            "source_kind": self.source_kind,
            "source_identity": self.source_identity,
            "source_reference": self.source_reference,
            "host": self.host,
            "pid": self.pid,
            "owner": self.owner,
            "worker_id": self.worker_id,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> WorkerManifest:
        """Decode one exact supported version of a worker workspace manifest."""
        data = require_object(value, "worker workspace manifest", WorkerManifestError)
        require_exact_fields(
            data,
            {
                "kind",
                "schema_version",
                "source_kind",
                "source_identity",
                "source_reference",
                "host",
                "pid",
                "owner",
                "worker_id",
                "started_at",
            },
            "worker workspace manifest",
            WorkerManifestError,
        )
        if data["kind"] != WORKER_MANIFEST_KIND or data["schema_version"] != WORKER_MANIFEST_SCHEMA_VERSION:
            raise WorkerManifestError("Unsupported worker workspace manifest")
        return cls(
            source_kind=require_text(data["source_kind"], "source_kind", WorkerManifestError),
            source_identity=require_text(data["source_identity"], "source_identity", WorkerManifestError),
            source_reference=require_text(data["source_reference"], "source_reference", WorkerManifestError),
            host=require_text(data["host"], "host", WorkerManifestError),
            pid=data["pid"],
            owner=require_text(data["owner"], "owner", WorkerManifestError),
            worker_id=require_text(data["worker_id"], "worker_id", WorkerManifestError),
            started_at=require_text(data["started_at"], "started_at", WorkerManifestError),
        )
