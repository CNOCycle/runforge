"""Tests for format-preserving UTF-8 text template rendering."""

from __future__ import annotations

import pytest

from runforge.planning.text_templates import TextTemplateError, render_text_template


def test_text_template_preserves_yaml_ini_comments_and_layout():
    content = "# retained\noutput: {ARTIFACT_DIR}/training\n; retained\n[eval]\namp={AMP}\n"

    rendered = render_text_template(content, {"ARTIFACT_DIR": "/reports/a/artifacts", "AMP": False})

    assert rendered == "# retained\noutput: /reports/a/artifacts/training\n; retained\n[eval]\namp=false\n"


def test_text_template_is_one_pass_and_rejects_unknown_or_non_scalar_values():
    assert render_text_template("value={FIRST}", {"FIRST": "{SECOND}", "SECOND": "done"}) == "value={SECOND}"
    with pytest.raises(TextTemplateError, match="unknown text placeholder"):
        render_text_template("value={MISSING}", {})
    with pytest.raises(TextTemplateError, match="strings, numbers, or booleans"):
        render_text_template("value={VALUES}", {"VALUES": [1]})
