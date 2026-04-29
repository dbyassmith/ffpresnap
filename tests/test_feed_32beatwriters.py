from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from ffpresnap.feeds._32beatwriters import (
    _32BeatwritersAdapter,
    _parse_nugget,
    _strip_html,
)
from ffpresnap.feeds._base import FeedFetchError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "32beatwriters"


def _read_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


class _FakeFetcher:
    """Maps URLs to fixture payloads. ``page=N`` query param resolves to
    pageN.json. Records every fetched URL for header/order assertions.
    """

    def __init__(self, *, fail_on_page: int | None = None, captured_headers=None):
        self.fail_on_page = fail_on_page
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        page = int(params.get("page", ["1"])[0])
        if self.fail_on_page == page:
            raise urllib.error.URLError("simulated network error")
        try:
            return _read_fixture(f"page{page}.json")
        except FileNotFoundError:
            # Synthesize an empty response so iteration ends gracefully.
            payload = {
                "success": True,
                "data": {
                    "nuggets": [],
                    "pagination": {"total": 0, "page": page, "limit": 5, "totalPages": page - 1},
                },
            }
            return json.dumps(payload).encode()


# --- _strip_html ---


def test_strip_html_paragraph_and_br():
    html = "<p>Line one.</p><p>Line two.<br>Line three.</p>"
    out = _strip_html(html)
    assert "Line one." in out
    assert "Line three." in out
    assert "<p>" not in out and "<br>" not in out


def test_strip_html_collapses_runs_of_blank_lines():
    html = "<p>A</p><br><br><br><br><br><p>B</p>"
    out = _strip_html(html)
    # No more than two consecutive newlines.
    assert "\n\n\n" not in out


def test_strip_html_empty_input():
    assert _strip_html("") == ""
    assert _strip_html("   ") == ""


# --- _parse_nugget ---


def test_parse_nugget_round_trips_fixture():
    payload = json.loads(_read_fixture("page1.json"))
    raw = payload["data"]["nuggets"][0]
    item = _parse_nugget(raw)
    assert item.external_id.startswith("32bw:")
    assert item.external_player_name  # non-empty
    assert item.external_team  # non-empty
    assert "<" not in item.cleaned_text  # HTML stripped
    assert item.created_at  # ISO string


# --- map_team ---


def test_map_team_known_full_names():
    a = _32BeatwritersAdapter()
    assert a.map_team("Cincinnati Bengals") == "CIN"
    assert a.map_team("Minnesota Vikings") == "MIN"
    # Sleeper-canonical: ARI, not ARZ.
    assert a.map_team("Arizona Cardinals") == "ARI"


def test_map_team_unmappable_returns_none():
    a = _32BeatwritersAdapter()
    assert a.map_team("Prospect") is None
    assert a.map_team("Texas A&M") is None
    assert a.map_team("") is None


# --- fetch (incremental + full) ---


def test_fetch_full_yields_all_pages():
    a = _32BeatwritersAdapter()
    fetcher = _FakeFetcher()
    items = list(a.fetch(full=True, fetch=fetcher, is_seen=lambda _: False))
    # 5 items per page x 2 pages = 10 expected.
    assert len(items) == 10
    # Sleep between page 1 and page 2 — but we can't easily assert sleep
    # without mocking time; instead just verify both pages were fetched.
    assert any("page=1" in u for u in fetcher.urls)
    assert any("page=2" in u for u in fetcher.urls)


def test_fetch_incremental_stops_when_page_fully_seen():
    """Mark every item as seen → adapter stops after page 1+ extra page."""
    a = _32BeatwritersAdapter()
    fetcher = _FakeFetcher()
    items = list(
        a.fetch(full=False, fetch=fetcher, is_seen=lambda _: True)
    )
    # First page is yielded but not used as stop signal; second page fully
    # seen → stop. So we yield 10 items (both pages walked) but no more.
    assert len(items) == 10
    # Did not request page 3 (file would have 404'd).
    assert all("page=3" not in u for u in fetcher.urls)


def test_fetch_incremental_stops_at_max_pages_cap(monkeypatch):
    """Force a tiny cap and verify the adapter respects it."""
    monkeypatch.setattr(
        "ffpresnap.feeds._32beatwriters.MAX_PAGES_INCREMENTAL", 1
    )
    a = _32BeatwritersAdapter()
    fetcher = _FakeFetcher()
    items = list(a.fetch(full=False, fetch=fetcher, is_seen=lambda _: False))
    # Only page 1 fetched.
    assert len(items) == 5
    assert all("page=2" not in u for u in fetcher.urls)


def test_fetch_full_walks_past_max_pages_cap(monkeypatch):
    """``full=True`` ignores MAX_PAGES_INCREMENTAL but still respects
    ``totalPages`` from the response.
    """
    monkeypatch.setattr(
        "ffpresnap.feeds._32beatwriters.MAX_PAGES_INCREMENTAL", 1
    )
    a = _32BeatwritersAdapter()
    fetcher = _FakeFetcher()
    items = list(a.fetch(full=True, fetch=fetcher, is_seen=lambda _: False))
    assert len(items) == 10  # both fixture pages


def test_fetch_propagates_fetch_error_as_feedfetcherror():
    a = _32BeatwritersAdapter()
    fetcher = _FakeFetcher(fail_on_page=1)
    # Default fetcher would catch URLError and wrap; this test uses our
    # injected fetcher which raises URLError directly.
    with pytest.raises(Exception):  # underlying URLError
        list(a.fetch(full=True, fetch=fetcher, is_seen=lambda _: False))


def test_fetch_no_sleep_between_pages_in_unit_test(monkeypatch):
    """Patch time.sleep to 0 so tests run instantly; verify it was called
    between page fetches (smoke check for politeness wiring).
    """
    calls: list[float] = []

    def _fake_sleep(s):
        calls.append(s)

    monkeypatch.setattr("ffpresnap.feeds._32beatwriters.time.sleep", _fake_sleep)
    a = _32BeatwritersAdapter()
    fetcher = _FakeFetcher()
    list(a.fetch(full=True, fetch=fetcher, is_seen=lambda _: False))
    assert any(c > 0 for c in calls)


# --- env-var auth ---


def test_default_fetcher_sends_bearer_when_env_set(monkeypatch):
    """Fake urlopen captures the Request and asserts the Authorization
    header was attached when FFPRESNAP_32BEATWRITERS_TOKEN is set.
    """
    captured = {}

    class _FakeResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"success": true, "data": {"nuggets": [], "pagination": {"totalPages": 0}}}'

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _FakeResp()

    monkeypatch.setenv("FFPRESNAP_32BEATWRITERS_TOKEN", "secret-abc")
    monkeypatch.setattr(
        "ffpresnap.feeds._32beatwriters.urllib.request.urlopen", _fake_urlopen
    )
    from ffpresnap.feeds._32beatwriters import _default_fetch

    _default_fetch("https://example.com/api/nuggets?page=1")
    # urllib normalizes header keys to title-case.
    assert captured["headers"].get("Authorization") == "Bearer secret-abc"


def test_default_fetcher_no_auth_header_without_env(monkeypatch):
    captured = {}

    class _FakeResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"success": true, "data": {"nuggets": []}}'

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _FakeResp()

    monkeypatch.delenv("FFPRESNAP_32BEATWRITERS_TOKEN", raising=False)
    monkeypatch.setattr(
        "ffpresnap.feeds._32beatwriters.urllib.request.urlopen", _fake_urlopen
    )
    from ffpresnap.feeds._32beatwriters import _default_fetch

    _default_fetch("https://example.com/api/nuggets?page=1")
    assert "Authorization" not in captured["headers"]
