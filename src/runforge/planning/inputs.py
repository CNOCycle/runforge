"""Deterministic rendering for immutable planned input files."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from configparser import Error as ConfigParserError
from configparser import RawConfigParser
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from runforge.planning.text_templates import TextTemplateError, render_text_template
from runforge.schemas.inputs import PlannedInputError, require_safe_relative_input_path


_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")
_INPUT_KINDS = frozenset({"copy", "json-template", "text-template"})


class InputRenderingError(ValueError):
    """Raised when an immutable input template cannot be rendered."""


@dataclass(frozen=True)
class InputTemplate:
    """One UTF-8 source entry for an immutable planned input tree."""

    path: str
    kind: str
    content: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "path", require_safe_relative_input_path(self.path, "input template path"))
        except PlannedInputError as error:
            raise InputRenderingError(str(error)) from error
        if self.kind not in _INPUT_KINDS:
            choices = ", ".join(sorted(_INPUT_KINDS))
            raise InputRenderingError(f"input template kind must be one of: {choices}")
        if not isinstance(self.content, str):
            raise InputRenderingError("input template content must be UTF-8 text")


@dataclass(frozen=True)
class RenderedInput:
    """One rendered byte sequence ready for immutable plan publication."""

    path: str
    kind: str
    content: bytes

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "path", require_safe_relative_input_path(self.path, "rendered input path"))
        except PlannedInputError as error:
            raise InputRenderingError(str(error)) from error
        if self.kind not in _INPUT_KINDS:
            raise InputRenderingError("rendered input kind is not supported")
        if not isinstance(self.content, bytes):
            raise InputRenderingError("rendered input content must be bytes")

    @property
    def sha256(self) -> str:
        """Return the lowercase digest that will be stored in the input manifest."""
        return hashlib.sha256(self.content).hexdigest()


def render_input_templates(
    templates: Sequence[InputTemplate],
    replacements: Mapping[str, object],
) -> tuple[RenderedInput, ...]:
    """Render a sorted input set from one one-pass placeholder mapping."""
    if isinstance(templates, (str, bytes)) or not isinstance(templates, Sequence):
        raise InputRenderingError("input templates must be an array")
    entries = tuple(templates)
    if not entries:
        raise InputRenderingError("input templates must not be empty")
    if not all(isinstance(entry, InputTemplate) for entry in entries):
        raise InputRenderingError("input templates must be InputTemplate metadata")
    paths = tuple(entry.path for entry in entries)
    if len(set(paths)) != len(paths):
        raise InputRenderingError("input template paths must not contain duplicates")
    normalized_replacements = _replacements(replacements)
    rendered = tuple(
        _render_template(entry, normalized_replacements) for entry in sorted(entries, key=lambda entry: entry.path)
    )
    return rendered


def _replacements(values: Mapping[str, object]) -> dict[str, str | int | float | bool]:
    if not isinstance(values, Mapping):
        raise InputRenderingError("input placeholder values must be a mapping")
    replacements: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or not _PLACEHOLDER_PATTERN.fullmatch(f"{{{key}}}"):
            raise InputRenderingError("input placeholder names must be non-empty brace-safe strings")
        if isinstance(value, bool):
            replacements[key] = value
        elif isinstance(value, int):
            replacements[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            replacements[key] = value
        elif isinstance(value, str):
            replacements[key] = value
        else:
            raise InputRenderingError("input placeholder values must be JSON strings, numbers, or booleans")
    return replacements


def _render_template(template: InputTemplate, replacements: Mapping[str, str | int | float | bool]) -> RenderedInput:
    if template.kind == "copy":
        return RenderedInput(path=template.path, kind=template.kind, content=template.content.encode("utf-8"))
    if template.kind == "text-template":
        try:
            content = render_text_template(template.content, replacements)
        except TextTemplateError as error:
            raise InputRenderingError(str(error)) from error
        _validate_text_template(template.path, content)
        return RenderedInput(path=template.path, kind=template.kind, content=content.encode("utf-8"))
    try:
        payload = json.loads(
            template.content, object_pairs_hook=_unique_object, parse_constant=_reject_non_json_constant
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise InputRenderingError(f"JSON template {template.path!r} is invalid: {error}") from error
    rendered = _render_json(payload, replacements)
    content = (json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    return RenderedInput(path=template.path, kind=template.kind, content=content)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant: {value}")


def _validate_text_template(path: str, content: str) -> None:
    """Validate supported text formats without rewriting their rendered bytes."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            tuple(yaml.compose_all(content))
        except yaml.YAMLError as error:
            raise InputRenderingError(f"YAML template {path!r} is invalid: {error}") from error
    elif suffix == ".ini":
        parser = RawConfigParser(interpolation=None)
        try:
            parser.read_string(content, source=path)
        except ConfigParserError as error:
            raise InputRenderingError(f"INI template {path!r} is invalid: {error}") from error


def _render_json(value: Any, replacements: Mapping[str, str | int | float | bool]) -> Any:
    if isinstance(value, str):
        match = _PLACEHOLDER_PATTERN.fullmatch(value)
        if match is not None:
            return _replacement(match.group(1), replacements)
        return _PLACEHOLDER_PATTERN.sub(
            lambda placeholder: _replacement_text(placeholder.group(1), replacements),
            value,
        )
    if isinstance(value, list):
        return [_render_json(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _render_json(item, replacements) for key, item in value.items()}
    return value


def _replacement(key: str, replacements: Mapping[str, str | int | float | bool]) -> str | int | float | bool:
    try:
        return replacements[key]
    except KeyError as error:
        raise InputRenderingError(f"unknown input placeholder: {key}") from error


def _replacement_text(key: str, replacements: Mapping[str, str | int | float | bool]) -> str:
    value = _replacement(key, replacements)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
