"""Tests for deterministic immutable input rendering."""

from __future__ import annotations

import json

import pytest

from runforge.planning.inputs import InputRenderingError, InputTemplate, render_input_templates


def test_json_templates_render_paths_and_typed_exact_placeholders_deterministically():
    templates = (
        InputTemplate(
            path="configs/train.json",
            kind="json-template",
            content='{"output": "{ARTIFACT_DIR}/training", "epochs": "{EPOCHS}", "amp": "{AMP}"}',
        ),
        InputTemplate(path="configs/component.json", kind="copy", content='{"unchanged": "{EPOCHS}"}\n'),
    )

    rendered = render_input_templates(
        templates,
        {"ARTIFACT_DIR": "/reports/baseline/artifacts", "EPOCHS": 50, "AMP": False},
    )

    assert [entry.path for entry in rendered] == ["configs/component.json", "configs/train.json"]
    assert rendered[0].content == b'{"unchanged": "{EPOCHS}"}\n'
    assert json.loads(rendered[1].content) == {
        "amp": False,
        "epochs": 50,
        "output": "/reports/baseline/artifacts/training",
    }


def test_json_template_rendering_is_one_pass_and_leaves_object_keys_unchanged():
    template = InputTemplate(
        path="config.json",
        kind="json-template",
        content='{"{KEY}": "{FIRST}", "value": "x-{SECOND}"}',
    )

    rendered = render_input_templates((template,), {"FIRST": "{SECOND}", "SECOND": 3, "KEY": "ignored"})

    assert json.loads(rendered[0].content) == {"{KEY}": "{SECOND}", "value": "x-3"}


@pytest.mark.parametrize(
    "template, replacements, match",
    [
        (InputTemplate(path="bad.json", kind="json-template", content="{not json}"), {}, "is invalid"),
        (
            InputTemplate(path="bad.json", kind="json-template", content='{"x": "{MISSING}"}'),
            {},
            "unknown input placeholder",
        ),
        (InputTemplate(path="bad.json", kind="json-template", content='{"x": 1, "x": 2}'), {}, "duplicate object key"),
    ],
)
def test_json_templates_reject_invalid_or_unresolved_content(template, replacements, match):
    with pytest.raises(InputRenderingError, match=match):
        render_input_templates((template,), replacements)


@pytest.mark.parametrize(
    "template, match",
    [
        (InputTemplate(path="bad.yaml", kind="text-template", content="output: [unterminated\n"), "YAML template"),
        (InputTemplate(path="bad.yml", kind="text-template", content="output: [unterminated\n"), "YAML template"),
        (InputTemplate(path="bad.ini", kind="text-template", content="value=without-section\n"), "INI template"),
    ],
)
def test_text_templates_validate_supported_configuration_syntax(template, match):
    with pytest.raises(InputRenderingError, match=match):
        render_input_templates((template,), {"ARTIFACT_DIR": "/reports/baseline/artifacts"})


def test_text_templates_preserve_valid_yaml_and_ini_bytes_after_validation():
    templates = (
        InputTemplate(
            path="configs/train.yaml",
            kind="text-template",
            content='# retained\noutput: "{ARTIFACT_DIR}/training"\n',
        ),
        InputTemplate(
            path="configs/eval.ini",
            kind="text-template",
            content="; retained\n[eval]\ncheckpoint={ARTIFACT_DIR}/training/model.pt\n",
        ),
    )

    rendered = render_input_templates(templates, {"ARTIFACT_DIR": "/reports/baseline/artifacts"})

    assert rendered[0].content == b"; retained\n[eval]\ncheckpoint=/reports/baseline/artifacts/training/model.pt\n"
    assert rendered[1].content == b'# retained\noutput: "/reports/baseline/artifacts/training"\n'


def test_templates_reject_unsafe_paths_duplicates_and_non_scalar_values():
    entry = InputTemplate(path="config.json", kind="copy", content="{}")

    with pytest.raises(InputRenderingError, match="safe relative"):
        InputTemplate(path="../config.json", kind="copy", content="{}")
    with pytest.raises(InputRenderingError, match="duplicates"):
        render_input_templates((entry, entry), {})
    with pytest.raises(InputRenderingError, match="JSON strings, numbers, or booleans"):
        render_input_templates((entry,), {"VALUES": [1]})
