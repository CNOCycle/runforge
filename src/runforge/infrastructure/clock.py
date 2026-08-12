"""Time helpers shared by planning and execution."""

from datetime import datetime, timezone


def utc_now() -> str:
    """Return the current UTC time in the persisted RunForge format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_compact_timestamp() -> str:
    """Return the current UTC time as a filesystem-safe, colon-free timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_utc(value: str) -> datetime:
    """Parse a RunForge-persisted UTC timestamp (utc_now's format) back into an aware datetime.

    ``datetime.fromisoformat`` only accepts a literal trailing ``Z`` from
    Python 3.11 -- this project supports 3.10, so the ``Z``/``+00:00`` swap
    that ``utc_now`` performs on the way out is reversed explicitly here
    rather than relying on the newer parser behavior.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
