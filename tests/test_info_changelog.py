from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException

from backend.api.routes.info import info as info_module


def test_parse_changelog_splits_releases() -> None:
    text = """# Changelog

## [Unreleased] - rolling
- working

## [0.1.0-beta.13] - 2026-04-25
- shipped
"""
    releases = info_module._parse_changelog(text)
    assert len(releases) == 2
    assert releases[0]["version"] == "Unreleased"
    assert releases[0]["date"] == "rolling"
    assert "- working" in releases[0]["body"]
    assert releases[1]["version"] == "0.1.0-beta.13"
    assert releases[1]["date"] == "2026-04-25"
    assert "- shipped" in releases[1]["body"]


def test_parse_changelog_returns_empty_for_no_release_headers() -> None:
    releases = info_module._parse_changelog("# Changelog\n\nNo releases yet.\n")
    assert releases == []


def test_parse_changelog_undated_header_consumes_next_line_as_date() -> None:
    text = """## [Unreleased]
- first item
"""
    releases = info_module._parse_changelog(text)
    assert len(releases) == 1
    # This documents current parser behavior: without " - <date>" on the
    # header line, the next non-newline text can be captured as `date`.
    assert releases[0]["date"] == "first item"
    assert releases[0]["body"] == ""


def test_get_changelog_returns_release_list(monkeypatch, tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [Unreleased] - rolling\n- test entry\n\n## [0.1.0] - 2026-01-01\n- older\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(info_module, "_find_changelog", lambda: changelog)

    result = asyncio.run(info_module.get_changelog())
    assert isinstance(result, list)
    assert result[0]["version"] == "Unreleased"
    assert "- test entry" in result[0]["body"]


def test_get_changelog_raises_404_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(info_module, "_find_changelog", lambda: None)
    try:
        asyncio.run(info_module.get_changelog())
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404
        assert "Changelog not found" in str(exc.detail)


def test_get_changelog_raises_404_when_no_releases(monkeypatch, tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\nNo releases\n", encoding="utf-8")
    monkeypatch.setattr(info_module, "_find_changelog", lambda: changelog)
    try:
        asyncio.run(info_module.get_changelog())
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404
        assert "contains no releases" in str(exc.detail)
