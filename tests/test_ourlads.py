"""Tests for the Ourlads fetch + parse pipeline. Uses real HTML fixtures
saved from ourlads.com to avoid network access during the test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffpresnap.ourlads import (
    NFL_TEAM_ABBRS,
    OURLADS_ALL_CHART_URL,
    OURLADS_ROSTER_URL_TEMPLATE,
    OurladsFetchError,
    _convert_name_lastfirst_to_firstlast,
    _strip_annotation,
    fetch_all,
    parse_all_chart,
    parse_roster,
)


FIXTURES = Path(__file__).parent / "fixtures" / "ourlads"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- Name handling ---


def test_convert_lastfirst_basic():
    assert _convert_name_lastfirst_to_firstlast("Andersen, Troy") == "Troy Andersen"


def test_convert_preserves_suffix():
    assert (
        _convert_name_lastfirst_to_firstlast("Allen Jr., Carlos")
        == "Carlos Allen Jr."
    )


def test_convert_no_comma_pass_through():
    assert _convert_name_lastfirst_to_firstlast("First Last") == "First Last"


def test_strip_year_round_annotation():
    assert _strip_annotation("Harrison Jr., Marvin 24/1") == "Harrison Jr., Marvin"


def test_strip_waiver_annotation():
    assert _strip_annotation("Fehoko, Simi U/LAC") == "Fehoko, Simi"


def test_strip_cf_annotation():
    assert _strip_annotation("Wallace III, Harrison CF26") == "Wallace III, Harrison"


def test_strip_combined_annotations_at_tail():
    """A name with multiple trailing tokens drops them all."""
    assert _strip_annotation("Smith, Joe 24/1 R") == "Smith, Joe"


def test_strip_no_annotation_pass_through():
    assert _strip_annotation("Andersen, Troy") == "Andersen, Troy"


# --- Roster parser ---


def test_parse_roster_atl_smoke():
    html = _load("roster_ATL.html")
    rows = parse_roster(html, team="ATL")
    assert 30 <= len(rows) <= 120
    # Spot-check one known player.
    troy = [r for r in rows if r.full_name == "Troy Andersen"]
    assert len(troy) == 1
    r = troy[0]
    assert r.team == "ATL"
    assert r.position == "ILB"
    assert r.number == "44"
    assert r.ourlads_id == "46885"


def test_parse_roster_extracts_ourlads_id_from_profile_link():
    html = _load("roster_ATL.html")
    rows = parse_roster(html, team="ATL")
    with_ids = [r for r in rows if r.ourlads_id is not None]
    # Most active players have profile links; expect very high coverage.
    assert len(with_ids) / len(rows) > 0.8


def test_parse_roster_skips_section_dividers():
    """Rows like 'Active Players' (single cell) must not produce a player."""
    html = _load("roster_ATL.html")
    rows = parse_roster(html, team="ATL")
    # No row should have full_name == "Active Players" or similar divider text.
    for r in rows:
        assert r.full_name not in {"Active Players", ""}


def test_parse_roster_handles_suffix_in_name():
    html = _load("roster_ATL.html")
    rows = parse_roster(html, team="ATL")
    # "Allen Jr., Carlos" -> "Carlos Allen Jr."
    assert any(r.full_name == "Carlos Allen Jr." for r in rows)


def test_parse_roster_returns_empty_on_no_table():
    assert parse_roster("<html><body>no table here</body></html>", team="X") == []


# --- All-teams chart parser ---


def test_parse_all_chart_smoke():
    html = _load("all_chart.html")
    entries = parse_all_chart(html)
    # Sanity band: empirical 2026 chart has ~3,100 entries.
    assert 1500 <= len(entries) <= 5000
    teams = {e.team for e in entries}
    # At least 30 of the 32 teams should appear.
    assert len(teams) >= 30


def test_parse_all_chart_extracts_depth_position_and_order():
    html = _load("all_chart.html")
    entries = parse_all_chart(html)
    # ARZ LWR slot 1 should be Marvin Harrison Jr. (with annotation stripped).
    arz_lwr_1 = [
        e for e in entries
        if e.team == "ARZ" and e.depth_chart_position == "LWR" and e.depth_chart_order == 1
    ]
    assert len(arz_lwr_1) == 1
    e = arz_lwr_1[0]
    assert e.full_name == "Marvin Harrison Jr."
    assert e.number == "18"
    assert e.ourlads_id == "53232"


def test_parse_all_chart_handles_chart_link_format():
    """Chart links use `javascript:sp(<id>)` rather than the roster page's
    `/player/<id>/` format. The id extractor must accept both."""
    html = _load("all_chart.html")
    entries = parse_all_chart(html)
    with_ids = [e for e in entries if e.ourlads_id is not None]
    assert len(with_ids) / len(entries) > 0.5


def test_parse_all_chart_skips_team_dividers():
    """Rows like 'Arizona Cardinals Updated: ...' are not data rows."""
    html = _load("all_chart.html")
    entries = parse_all_chart(html)
    for e in entries:
        assert "Updated" not in e.team
        assert len(e.team) <= 4  # Ourlads abbreviations are 2-3 chars.


# --- fetch_all orchestration ---


class _FakeFetcher:
    """Replays saved HTML fixtures by URL."""

    def __init__(self):
        self.calls: list[str] = []
        self._roster_html = _load("roster_ATL.html").encode()
        self._chart_html = _load("all_chart.html").encode()

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url == OURLADS_ALL_CHART_URL:
            return self._chart_html
        return self._roster_html  # all 32 teams reuse the ATL fixture


def test_fetch_all_happy_path():
    fetcher = _FakeFetcher()
    result = fetch_all(fetcher=fetcher, delay_seconds=0)
    # 32 roster fetches + 1 chart = 33 URLs.
    assert len(fetcher.calls) == 33
    # All teams should appear in completeness map.
    assert len(result.completeness) == 32
    # Healthy run produces no errors.
    assert result.errors == []
    # Rows generated for every team (since they all replay ATL fixture).
    assert len(result.rows) > 0


def test_fetch_all_records_per_team_on_fetch_failure():
    """When one team's roster fetch raises, other teams still proceed and the
    failure is recorded."""

    failing_team = "BUF"

    def flaky_fetcher(url: str) -> bytes:
        if f"/roster/{failing_team}" in url:
            raise OurladsFetchError("simulated 503")
        if url == OURLADS_ALL_CHART_URL:
            return _load("all_chart.html").encode()
        return _load("roster_ATL.html").encode()

    result = fetch_all(fetcher=flaky_fetcher, delay_seconds=0)
    # The failing team is in errors but not in completeness.
    assert any(e.team == failing_team for e in result.errors)
    assert failing_team not in result.completeness
    # Other teams still get rows.
    assert len(result.rows) > 0


def test_fetch_all_handles_chart_failure_gracefully():
    """If the all-teams chart fetch fails, rosters still upload but
    completeness flags everywhere are False."""

    def chart_fails(url: str) -> bytes:
        if url == OURLADS_ALL_CHART_URL:
            raise OurladsFetchError("chart 500")
        return _load("roster_ATL.html").encode()

    result = fetch_all(fetcher=chart_fails, delay_seconds=0)
    # Chart-level error logged.
    assert any(e.team == "*chart*" for e in result.errors)
    # All team completeness flags False.
    assert all(not v for v in result.completeness.values())
    # Roster rows still emitted.
    assert len(result.rows) > 0


def test_fetch_all_team_count_matches_constant():
    """Should fetch exactly len(NFL_TEAM_ABBRS) roster URLs."""
    fetcher = _FakeFetcher()
    fetch_all(fetcher=fetcher, delay_seconds=0)
    roster_calls = [c for c in fetcher.calls if "/roster/" in c]
    assert len(roster_calls) == len(NFL_TEAM_ABBRS)


def test_fetch_all_chart_fetched_last():
    """The all-teams chart MUST be fetched after every roster (mid-run trade
    defense — chart entries reconcile against just-fetched rosters)."""
    fetcher = _FakeFetcher()
    fetch_all(fetcher=fetcher, delay_seconds=0)
    chart_indices = [
        i for i, c in enumerate(fetcher.calls) if c == OURLADS_ALL_CHART_URL
    ]
    assert len(chart_indices) == 1
    # Chart call must be the last one.
    assert chart_indices[0] == len(fetcher.calls) - 1


def test_url_construction():
    url = OURLADS_ROSTER_URL_TEMPLATE.format(team="ATL")
    assert url == "https://www.ourlads.com/nfldepthcharts/roster/ATL"


# --- Sanity bands ---


def test_fetch_all_sanity_band_short_roster_marks_team_failed():
    """A roster page that parses to 0 rows trips MIN_ROSTER_ROWS."""
    short_html = b"<html><body><table><tr><td>only</td><td>one</td><td>row</td></tr></table></body></html>"

    def fetcher(url: str) -> bytes:
        if url == OURLADS_ALL_CHART_URL:
            return _load("all_chart.html").encode()
        if "/roster/SF" in url:
            return short_html
        return _load("roster_ATL.html").encode()

    result = fetch_all(fetcher=fetcher, delay_seconds=0)
    assert any(e.team == "SF" and "sanity" in e.reason for e in result.errors)
    assert "SF" not in result.completeness
