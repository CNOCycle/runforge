"""Mapping between matrix combinations and the plan directories allocated to them."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runforge.infrastructure.json_store import load_json_object, save_json_object
from runforge.infrastructure.storage import ExperimentDirectory


_MAXIMUM_MAPPING_FILES = 10_000


@dataclass(frozen=True)
class MatrixMappingRow:
    """One matrix combination and the directory allocated for it."""

    index: int
    dir_name: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class MatrixMapping:
    """The stable mapping between one matrix expansion and its plan directories."""

    KIND = "runforge_matrix_mapping"
    SCHEMA_VERSION = 1

    matrix_id: str
    parameters: tuple[str, ...]
    rows: tuple[MatrixMappingRow, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned JSON representation of this mapping."""
        return {
            "kind": self.KIND,
            "schema_version": self.SCHEMA_VERSION,
            "matrix_id": self.matrix_id,
            "parameters": list(self.parameters),
            "rows": [
                {"index": row.index, "dir_name": row.dir_name, "parameters": dict(row.parameters)} for row in self.rows
            ],
        }

    @classmethod
    def from_dict(cls, value: object) -> MatrixMapping:
        """Decode and validate one supported matrix mapping artifact."""
        if not isinstance(value, dict):
            raise ValueError("matrix mapping must be a JSON object")
        if value.get("kind") != cls.KIND:
            raise ValueError(f"unsupported matrix mapping kind: {value.get('kind')!r}")
        if value.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError(f"unsupported matrix mapping schema version: {value.get('schema_version')!r}")
        matrix_id = value.get("matrix_id")
        parameters = value.get("parameters")
        rows = value.get("rows")
        if not isinstance(matrix_id, str) or not matrix_id:
            raise ValueError("matrix mapping matrix_id must be a non-empty string")
        if not isinstance(parameters, list) or not all(isinstance(name, str) and name for name in parameters):
            raise ValueError("matrix mapping parameters must be an array of strings")
        if len(set(parameters)) != len(parameters):
            raise ValueError("matrix mapping parameters must not repeat a name")
        if not isinstance(rows, list):
            raise ValueError("matrix mapping rows must be an array")
        decoded = tuple(_row_from_dict(row, tuple(parameters)) for row in rows)
        # One expansion numbers its rows from zero, so anything else means the
        # artifact was reordered, truncated, or merged from separate matrices.
        if [row.index for row in decoded] != list(range(len(decoded))):
            raise ValueError("matrix mapping rows must be numbered from zero without gaps")
        return cls(matrix_id=matrix_id, parameters=tuple(parameters), rows=decoded)


def _row_from_dict(row: object, parameters: tuple[str, ...]) -> MatrixMappingRow:
    """Decode one mapping row, rejecting missing, mistyped, or mismatched fields."""
    if not isinstance(row, dict):
        raise ValueError("matrix mapping rows must contain objects")
    index = row.get("index")
    dir_name = row.get("dir_name")
    values = row.get("parameters")
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or not isinstance(dir_name, str)
        or not dir_name
        or not isinstance(values, dict)
    ):
        raise ValueError("matrix mapping rows have invalid fields")
    # Rendering reads each row by the declared column names, so a row that does
    # not carry exactly those names would silently display null columns.
    if set(values) != set(parameters):
        raise ValueError(f"matrix mapping row {index} does not match the declared parameters")
    unsupported = sorted(name for name, value in values.items() if not _is_matrix_scalar(value))
    if unsupported:
        raise ValueError(f"matrix mapping row {index} has a non-scalar value for {unsupported[0]!r}")
    return MatrixMappingRow(index=index, dir_name=dir_name, parameters=dict(values))


def _is_matrix_scalar(value: object) -> bool:
    """Report whether one value is a scalar the matrix planner can produce."""
    return isinstance(value, str | int | bool) or (isinstance(value, float) and math.isfinite(value))


def build_matrix_mapping(experiments: Sequence[Path]) -> MatrixMapping:
    """Associate each planned directory with the combination recorded in its configuration."""
    if not experiments:
        raise ValueError("matrix planning produced no experiment directories")

    layouts = [ExperimentDirectory.resolve(experiment) for experiment in experiments]
    configurations = [layout.load_configuration() for layout in layouts]
    # One expansion shares one axis set. Taking the union instead would fill a
    # column with nulls for any directory that never carried that parameter.
    axis_sets = {frozenset(configuration.parameters) for configuration in configurations}
    if len(axis_sets) != 1:
        raise ValueError("experiment directories do not share one matrix parameter set")
    # Matrix expansion orders its axes by sorted parameter name, so the columns
    # here follow the same order as the combinations they describe.
    parameter_names = tuple(sorted(axis_sets.pop()))
    rows = tuple(
        MatrixMappingRow(
            index=index,
            dir_name=layout.root.name,
            parameters={name: configuration.parameters[name] for name in parameter_names},
        )
        for index, (layout, configuration) in enumerate(zip(layouts, configurations, strict=True))
    )
    return MatrixMapping(matrix_id=_matrix_identity(layouts), parameters=parameter_names, rows=rows)


def matrix_directory_prefix(experiment: Path) -> str:
    """Return the shared directory prefix of one matrix expansion, without its count."""
    name = ExperimentDirectory.resolve(experiment).root.name
    prefix, separator, _count = name.rpartition("_")
    if not separator or not prefix:
        raise ValueError(f"experiment directory does not use a matrix name: {name}")
    return prefix


def _matrix_identity(layouts: Sequence[ExperimentDirectory]) -> str:
    """Derive one identity shared by every directory in a single expansion."""
    prefixes = {matrix_directory_prefix(layout.root) for layout in layouts}
    parents = {layout.root.parent for layout in layouts}
    # Every combination of one expansion shares a source and a name, so a split
    # identity means these directories did not come from one matrix.
    if len(prefixes) != 1 or len(parents) != 1:
        raise ValueError("experiment directories do not belong to one matrix expansion")
    return f"{parents.pop().name}_{prefixes.pop()}"


def save_matrix_mapping(experiments: Sequence[Path]) -> Path:
    """Persist a matrix mapping beside its experiment directories."""
    mapping = build_matrix_mapping(experiments)
    first = ExperimentDirectory.resolve(experiments[0]).root
    destination = _reserve_mapping_file(first.parent, matrix_directory_prefix(first))
    try:
        save_json_object(destination, mapping.to_dict())
    except (OSError, ValueError) as error:
        # Release the reserved name so a failed write leaves no empty artifact
        # that later expansions would skip and matrix-show would reject.
        destination.unlink(missing_ok=True)
        raise ValueError(f"could not write matrix mapping {destination}: {error}") from error
    return destination


def _reserve_mapping_file(directory: Path, prefix: str) -> Path:
    """Claim an unused artifact name so concurrent expansions cannot overwrite each other."""
    for attempt in range(_MAXIMUM_MAPPING_FILES):
        suffix = "" if attempt == 0 else f"_{attempt:04d}"
        candidate = directory / f"{prefix}_matrix{suffix}.json"
        try:
            # Exclusive creation reserves the name; testing exists() first would
            # let a second planner choose the same path before either wrote it.
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        except OSError as error:
            raise ValueError(f"could not create matrix mapping beside {directory}: {error}") from error
        return candidate
    raise ValueError(f"too many matrix mappings already stored beside {directory}")


def load_matrix_mapping(path: Path) -> MatrixMapping:
    """Load one persisted matrix mapping artifact."""
    return MatrixMapping.from_dict(load_json_object(path))
