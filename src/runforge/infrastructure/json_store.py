"""Generic atomic JSON-object persistence for later RunForge stages."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class JsonStoreError(ValueError):
    """Raised when a JSON object cannot be written or read safely."""


def save_json_object(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically save a JSON object, creating its parent directory if needed."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise JsonStoreError("JSON object must map string keys to JSON-compatible values")
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except (OSError, TypeError, ValueError):
        temporary.unlink(missing_ok=True)
        raise


def load_json_object(path: Path) -> dict[str, Any]:
    """Load a JSON object and reject non-object roots."""
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise JsonStoreError(f"Could not read JSON object {source}: {error}") from error
    if not isinstance(value, dict):
        raise JsonStoreError(f"Expected a JSON object in {source}")
    return value
