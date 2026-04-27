from __future__ import annotations

import pytest

from ffpresnap.prompt_loader import PromptParseError, _parse_prompt, _sort_key


def test_parse_prompt_happy_path():
    text = (
        "---\n"
        "slug: study-browser\n"
        "title: Study Browser\n"
        "description: Browse studies and drill in.\n"
        "---\n"
        "Build me a study browser.\n"
        "Step 1: call list_studies.\n"
    )
    result = _parse_prompt("study-browser.md", text)
    assert result == {
        "slug": "study-browser",
        "title": "Study Browser",
        "description": "Browse studies and drill in.",
        "body": "Build me a study browser.\nStep 1: call list_studies.",
    }


def test_parse_prompt_handles_crlf_line_endings():
    text = (
        "---\r\n"
        "slug: x\r\n"
        "title: X\r\n"
        "description: y\r\n"
        "---\r\n"
        "body line 1\r\n"
        "body line 2\r\n"
    )
    result = _parse_prompt("x.md", text)
    assert result["body"] == "body line 1\nbody line 2"


def test_parse_prompt_blank_body_is_allowed():
    text = "---\nslug: x\ntitle: X\ndescription: y\n---\n"
    result = _parse_prompt("x.md", text)
    assert result["body"] == ""


def test_parse_prompt_missing_opening_delimiter_raises():
    with pytest.raises(PromptParseError, match="opening"):
        _parse_prompt("bad.md", "slug: x\ntitle: X\ndescription: y\n")


def test_parse_prompt_missing_closing_delimiter_raises():
    with pytest.raises(PromptParseError, match="closing"):
        _parse_prompt("bad.md", "---\nslug: x\ntitle: X\ndescription: y\nbody")


def test_parse_prompt_missing_required_field_raises():
    text = "---\nslug: x\ntitle: X\n---\nbody"
    with pytest.raises(PromptParseError, match="description"):
        _parse_prompt("bad.md", text)


def test_parse_prompt_invalid_slug_raises():
    text = "---\nslug: Bad Slug!\ntitle: X\ndescription: y\n---\nbody"
    with pytest.raises(PromptParseError, match="invalid slug"):
        _parse_prompt("bad.md", text)


def test_parse_prompt_frontmatter_missing_separator_raises():
    text = "---\nslug study-browser\ntitle: X\ndescription: y\n---\nbody"
    with pytest.raises(PromptParseError, match="missing ':'"):
        _parse_prompt("bad.md", text)


def test_sort_key_puts_show_prompt_library_first():
    items = [
        {"slug": "study-browser"},
        {"slug": "show-prompt-library"},
        {"slug": "depth-chart-explorer"},
    ]
    items.sort(key=_sort_key)
    assert [i["slug"] for i in items] == [
        "show-prompt-library",
        "depth-chart-explorer",
        "study-browser",
    ]
