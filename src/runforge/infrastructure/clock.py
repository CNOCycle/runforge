"""Time helpers shared by planning and execution."""

from datetime import datetime, timezone


def utc_now() -> str:
    """Return the current UTC time in the persisted RunForge format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_compact_timestamp() -> str:
    """Return the current UTC time as a filesystem-safe, colon-free timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
