"""32beatwriters feed adapter.

Pulls beat-reporter "nuggets" from
``https://api.32beatwriters.com/api/nuggets`` newest-first. Each nugget is
HTML content tagged to one player, with source author + URL + timestamp.

The endpoint is unauthenticated as of 2026-04-29; if that changes, set
``FFPRESNAP_32BEATWRITERS_TOKEN`` and the adapter will send it as a bearer
header on every request.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from bs4 import BeautifulSoup

from ..teams import TEAMS
from ._base import FeedAdapter, FeedFetchError, FeedItem, Fetcher, IsSeen
from ._registry import register_adapter


BASE_URL = "https://api.32beatwriters.com/api/nuggets"
SOURCE_URL = "https://api.32beatwriters.com"

LIMIT = 50
"""Items per page request (the API caps it well above this)."""

MAX_PAGES_INCREMENTAL = 20
"""Safety cap on incremental walks. With LIMIT=50 this is up to 1,000
items per run — well past what a daily incremental should ever need."""

DELAY_SECONDS = 0.75
"""Politeness delay between page requests."""

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRY_BACKOFF_SECONDS = 3.0

USER_AGENT = "ffpresnap/0.1 (single-user fantasy-football scratchpad)"

# Reverse lookup: "Cincinnati Bengals" → "CIN". Built once at import time.
# Use Sleeper-canonical abbreviations (ARI for Arizona, not Ourlads' ARZ),
# because that's what `players.team` stores.
_TEAM_NAME_TO_ABBR: dict[str, str] = {full_name: abbr for abbr, full_name, _, _ in TEAMS}

# Collapse runs of 3+ blank lines down to 2 — bs4's get_text leaves vertical
# whitespace from <br> tags that looks ugly in note bodies otherwise.
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _default_fetch(url: str) -> bytes:
    """Single-URL fetcher with gzip + retry-on-5xx/429. Mirrors the Ourlads
    fetcher posture; intentionally not extracted into a shared module yet
    (see plan: HTTP utility refactor waits until feed source #3).
    """
    return _fetch_with_retry(url, retries_remaining=1)


def _fetch_with_retry(url: str, *, retries_remaining: int) -> bytes:
    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    token = os.environ.get("FFPRESNAP_32BEATWRITERS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600 and retries_remaining > 0:
            time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS)
            return _fetch_with_retry(url, retries_remaining=retries_remaining - 1)
        if e.code == 429 and retries_remaining > 0:
            retry_after = e.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else DEFAULT_RETRY_BACKOFF_SECONDS
            except (TypeError, ValueError):
                wait = DEFAULT_RETRY_BACKOFF_SECONDS
            time.sleep(wait)
            return _fetch_with_retry(url, retries_remaining=retries_remaining - 1)
        raise FeedFetchError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        if retries_remaining > 0:
            time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS)
            return _fetch_with_retry(url, retries_remaining=retries_remaining - 1)
        raise FeedFetchError(f"Network error fetching {url}: {e.reason}") from e


def _strip_html(html: str) -> str:
    """Strip the light HTML 32beatwriters uses (`<p>`, `<br>`) into plain
    text suitable for a note body. Preserves paragraph breaks but collapses
    runs of 3+ blank lines so the output reads cleanly.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Use a newline separator so <br> and </p> turn into line breaks.
    text = soup.get_text(separator="\n").strip()
    return _MULTI_BLANK_RE.sub("\n\n", text)


def _parse_nugget(raw: dict[str, Any]) -> FeedItem:
    """Convert one API nugget into a FeedItem. The `external_id` is
    namespaced (``32bw:<id>``) so the (source_id, external_id) idempotency
    key in feed_items remains stable even if the adapter name ever changes.
    """
    player = raw.get("player") or {}
    return FeedItem(
        external_id=f"32bw:{raw['id']}",
        external_player_id=str(player.get("id")) if player.get("id") is not None else None,
        external_player_name=player.get("name") or "",
        external_team=player.get("team") or "",
        external_position=player.get("position") or "",
        source_url=raw.get("sourceUrl"),
        source_author=raw.get("sourceName"),
        raw_html=raw.get("content") or "",
        cleaned_text=_strip_html(raw.get("content") or ""),
        created_at=raw.get("createdAt") or "",
    )


class _32BeatwritersAdapter:
    """Concrete feed adapter for the 32beatwriters API."""

    name = "32beatwriters"
    source_url = SOURCE_URL

    def fetch(
        self,
        *,
        full: bool,
        fetch: Fetcher | None = None,
        is_seen: IsSeen | None = None,
    ) -> Iterable[FeedItem]:
        """Walk pages newest-first. In incremental mode, stops as soon as a
        full page contains only items the orchestrator already has.
        """
        fetcher = fetch if fetch is not None else _default_fetch
        page = 1
        max_pages = MAX_PAGES_INCREMENTAL if not full else 10**9
        first_page = True
        while page <= max_pages:
            url = f"{BASE_URL}?{urllib.parse.urlencode({'page': page, 'limit': LIMIT, 'sortBy': 'createdAt', 'sortOrder': 'desc'})}"
            raw_bytes = fetcher(url)
            try:
                payload = json.loads(raw_bytes)
            except json.JSONDecodeError as exc:
                raise FeedFetchError(
                    f"32beatwriters returned non-JSON for {url}: {exc}"
                ) from exc
            data = payload.get("data") or {}
            nuggets = data.get("nuggets") or []
            pagination = data.get("pagination") or {}
            if not nuggets:
                break

            page_all_seen = True
            for raw in nuggets:
                item = _parse_nugget(raw)
                if is_seen is not None and not is_seen(item.external_id):
                    page_all_seen = False
                yield item
            # In incremental mode: stop after this page if every item on it was already seen.
            # Don't apply the rule to page 1 — partial overlap on a fresh DB is normal.
            if not full and is_seen is not None and not first_page and page_all_seen:
                break
            first_page = False

            total_pages = int(pagination.get("totalPages") or 0)
            if total_pages and page >= total_pages:
                break
            page += 1
            if page <= max_pages:
                time.sleep(DELAY_SECONDS)

    def map_team(self, external_team: str) -> str | None:
        """Translate ``"Cincinnati Bengals"`` → ``"CIN"``. Returns ``None``
        for non-NFL strings (``"Prospect"``, college affiliations, empty).
        """
        if not external_team:
            return None
        return _TEAM_NAME_TO_ABBR.get(external_team)


register_adapter(_32BeatwritersAdapter())
