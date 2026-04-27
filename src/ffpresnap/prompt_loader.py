from __future__ import annotations

import re
from importlib import resources
from typing import Any


_REQUIRED_FIELDS = ("slug", "title", "description")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PROMPTS_PACKAGE = "ffpresnap.prompts"


class PromptParseError(Exception):
    """Raised when a prompt file is malformed.

    Repo-shipped files only — a parse failure is a developer error, surfaced
    loudly during PR review and CI.
    """


def _parse_prompt(filename: str, text: str) -> dict[str, Any]:
    # Normalize line endings so Windows-authored files parse correctly.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise PromptParseError(
            f"{filename}: missing opening '---' frontmatter delimiter"
        )
    body_split = text[len("---\n"):].split("\n---\n", 1)
    if len(body_split) != 2:
        raise PromptParseError(
            f"{filename}: missing closing '---' frontmatter delimiter"
        )
    frontmatter_text, body = body_split

    fields: dict[str, str] = {}
    for raw in frontmatter_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" not in line:
            raise PromptParseError(
                f"{filename}: frontmatter line missing ':' separator: {raw!r}"
            )
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    for required in _REQUIRED_FIELDS:
        if required not in fields:
            raise PromptParseError(
                f"{filename}: missing required frontmatter field {required!r}"
            )

    slug = fields["slug"]
    if not _SLUG_RE.match(slug):
        raise PromptParseError(
            f"{filename}: invalid slug {slug!r} (must match [a-z0-9][a-z0-9-]*)"
        )

    return {
        "slug": slug,
        "title": fields["title"],
        "description": fields["description"],
        "body": body.strip(),
    }


def _sort_key(prompt: dict[str, Any]) -> tuple[int, str]:
    # show-prompt-library always renders first; everything else sorts by slug.
    return (0 if prompt["slug"] == "show-prompt-library" else 1, prompt["slug"])


def load_prompts(package: str = _PROMPTS_PACKAGE) -> list[dict[str, Any]]:
    """Read every ``*.md`` file in the prompts package and return parsed prompts.

    Raises ``PromptParseError`` on malformed or duplicate-slug files. The caller
    is expected to fail fast — a bad prompt file is a developer error.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    root = resources.files(package)
    for entry in root.iterdir():
        name = entry.name
        if not name.endswith(".md"):
            continue
        text = entry.read_text(encoding="utf-8")
        prompt = _parse_prompt(name, text)
        if prompt["slug"] in seen:
            raise PromptParseError(
                f"duplicate slug {prompt['slug']!r} (second occurrence in {name})"
            )
        seen.add(prompt["slug"])
        out.append(prompt)
    out.sort(key=_sort_key)
    return out
