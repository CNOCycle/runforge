"""Mapping between matrix combinations and the plan directories allocated to them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runforge.infrastructure.storage import ExperimentDirectory


@dataclass(frozen=True)
class MatrixMappingRow:
    """One matrix combination and the directory allocated for it."""

    index: int
    dir_name: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class MatrixMapping:
    """The stable mapping between one matrix expansion and its plan directories."""

    matrix_id: str
    parameters: tuple[str, ...]
    rows: tuple[MatrixMappingRow, ...]


def build_matrix_mapping(experiments: Sequence[Path]) -> MatrixMapping:
    """Associate each planned directory with the combination recorded in its configuration."""
    if not experiments:
        raise ValueError("matrix planning produced no experiment directories")

    layouts = [ExperimentDirectory.resolve(experiment) for experiment in experiments]
    configurations = [layout.load_configuration() for layout in layouts]
    # Matrix expansion orders its axes by sorted parameter name, so the columns
    # here follow the same order as the combinations they describe.
    parameter_names = tuple(sorted({name for configuration in configurations for name in configuration.parameters}))
    rows = tuple(
        MatrixMappingRow(
            index=index,
            dir_name=layout.root.name,
            parameters={name: configuration.parameters.get(name) for name in parameter_names},
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
