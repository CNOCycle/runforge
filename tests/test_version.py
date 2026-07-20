"""Tests for user-visible RunForge version reporting."""

from __future__ import annotations

import subprocess
from pathlib import Path

import runforge.version as package_version


def test_display_version_reads_project_toml_and_appends_git_revision(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")
    monkeypatch.setattr(package_version, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(package_version, "_git_revision", lambda source_root: "a1b2")

    assert package_version.display_version() == "1.2.3+a1b2"


def test_project_version_requires_a_quoted_value_in_the_project_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "version = 'ignored'\n[project]\nname = 'runforge'\nversion = \"1.2.3\" # source of truth\n[tool.ruff]\n",
        encoding="utf-8",
    )

    assert package_version._project_version(tmp_path) == "1.2.3"


def test_project_version_falls_back_for_an_unsupported_toml_value(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nversion = { attr = 'runforge.version' }\n", encoding="utf-8")

    assert package_version._project_version(tmp_path) is None


def test_display_version_falls_back_to_installed_metadata_without_source_tree(monkeypatch):
    monkeypatch.setattr(package_version, "_source_root", lambda: None)
    monkeypatch.setattr(package_version, "_installed_version", lambda: "1.2.3")

    assert package_version.display_version() == "1.2.3"


def test_display_version_returns_unknown_when_no_version_source_is_available(monkeypatch):
    monkeypatch.setattr(package_version, "_source_root", lambda: None)
    monkeypatch.setattr(package_version, "_installed_version", lambda: None)

    assert package_version.display_version() == "unknown"


def test_git_revision_uses_the_first_four_characters_of_head(tmp_path, monkeypatch):
    monkeypatch.setattr(
        package_version.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "A1B2C3D4\n", ""),
    )

    assert package_version._git_revision(tmp_path) == "a1b2"


def test_git_revision_ignores_failed_or_unavailable_git(tmp_path, monkeypatch):
    monkeypatch.setattr(
        package_version.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "not a repository"),
    )
    assert package_version._git_revision(tmp_path) is None

    def unavailable(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(package_version.subprocess, "run", unavailable)
    assert package_version._git_revision(Path(tmp_path)) is None
