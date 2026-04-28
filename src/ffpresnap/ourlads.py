"""Fetch + parse Ourlads.com depth chart and roster pages.

Two endpoints:
  - Roster:    https://www.ourlads.com/nfldepthcharts/roster/<TEAM>
  - All chart: https://www.ourlads.com/nfldepthcharts/pfdepthcharts.aspx

The roster page yields a player list with team + position + jersey + an
ourlads_id extracted from each row's profile link. The all-teams chart
gives one row per (team, depth_chart_position) with up to five player
slots ordered by depth.

This module exposes a `Fetcher` seam (Callable[[str], bytes]) so tests can
inject canned HTML; the default implementation uses stdlib urllib with
politeness defaults, gzip handling, retry-on-5xx, and an attributed
User-Agent.
"""

from __future__ import annotations

import gzip
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Callable

from bs4 import BeautifulSoup


OURLADS_ROOT = "https://www.ourlads.com"
OURLADS_ROSTER_URL_TEMPLATE = f"{OURLADS_ROOT}/nfldepthcharts/roster/{{team}}"
OURLADS_ALL_CHART_URL = f"{OURLADS_ROOT}/nfldepthcharts/pfdepthcharts.aspx"
OURLADS_ROBOTS_URL = f"{OURLADS_ROOT}/robots.txt"

USER_AGENT = "ffpresnap/0.1 (single-user fantasy-football scratchpad)"
DEFAULT_TIMEOUT = 30.0
DEFAULT_DELAY_SECONDS = 1.5
DEFAULT_RETRY_BACKOFF_SECONDS = 3.0

# Sanity bands. Outside these counts we mark a page failed and apply no writes.
MIN_ROSTER_ROWS = 30
MAX_ROSTER_ROWS = 120
# Empirical baseline: ~3,100 entries observed against the live chart in
# 2026 (32 teams × ~30 depth-chart positions × up to 5 slots, with most
# slots filled). The band is wide enough to absorb roster turnover and
# format tweaks while still flagging structural breakage (a torn page
# returning 100 rows).
MIN_TOTAL_CHART_ROWS = 1500
MAX_TOTAL_CHART_ROWS = 5000
MAX_FAILED_TEAMS = 5

# 32 NFL team abbreviations (Ourlads uses these in roster URLs). ARZ for
# Arizona, JAX for Jacksonville, LAC for Chargers, LAR for Rams.
NFL_TEAM_ABBRS: tuple[str, ...] = (
    "ARZ", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
)

# Strip Ourlads draft / status annotations that appear after a player name on
# chart entries: "24/1" (year/draft round), "U/LAC" (waiver/UDFA from another
# team), "CF25" (college free agent year), "SF25" (street free agent year),
# "R" (rookie), "UDFA". Patterns combine these in any order separated by
# spaces, anchored to end of string.
_ANNOTATION_TOKEN = r"(?:\d+/\d+|U/[A-Z]+|CF\d+|SF\d+|UDFA|R)"
_ANNOTATION_TAIL_RE = re.compile(rf"(?:\s+{_ANNOTATION_TOKEN})+\s*$")
# Player profile id from chart links like `javascript:sp(53232)`.
_CHART_LINK_ID_RE = re.compile(r"sp\((\d+)\)")
# Player profile id from roster links like `/nfldepthcharts/player/55299/`.
_ROSTER_LINK_ID_RE = re.compile(r"/player/(\d+)/?")


class OurladsFetchError(Exception):
    """Raised on network / HTTP / parse failures from Ourlads.com fetches."""


Fetcher = Callable[[str], bytes]


@dataclass
class RosterRow:
    team: str
    full_name: str
    position: str
    number: str | None = None
    ourlads_id: str | None = None


@dataclass
class ChartEntry:
    team: str
    full_name: str
    depth_chart_position: str  # "QB", "LWR", "LT", etc.
    depth_chart_order: int  # 1..5
    number: str | None = None
    ourlads_id: str | None = None


@dataclass
class TeamError:
    team: str
    reason: str


@dataclass
class FetchAllResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    completeness: dict[str, bool] = field(default_factory=dict)
    errors: list[TeamError] = field(default_factory=list)


# ---- HTTP fetcher (default) ----


def _default_fetch(url: str) -> bytes:
    """Fetch a single URL, honoring gzip + a single retry on 5xx/connection
    errors with backoff. Raises OurladsFetchError on hard failure.
    """
    return _fetch_with_retry(url, retries_remaining=1)


def _fetch_with_retry(url: str, *, retries_remaining: int) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
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
        raise OurladsFetchError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        if retries_remaining > 0:
            time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS)
            return _fetch_with_retry(url, retries_remaining=retries_remaining - 1)
        raise OurladsFetchError(f"Network error fetching {url}: {e.reason}") from e


def check_robots(*, fetcher: Fetcher | None = None) -> bool:
    """Return True if Ourlads' robots.txt permits scraping our target paths
    under our User-Agent. Used as a runtime fail-closed guard before fetch.
    """
    rp = urllib.robotparser.RobotFileParser()
    fn = fetcher or _default_fetch
    try:
        body = fn(OURLADS_ROBOTS_URL).decode("utf-8", errors="ignore")
    except OurladsFetchError:
        # If we can't reach robots.txt, default to allowed (Ourlads is up; we
        # can read pages but not robots — likely a transient).
        return True
    rp.parse(body.splitlines())
    return rp.can_fetch(USER_AGENT, OURLADS_ALL_CHART_URL) and rp.can_fetch(
        USER_AGENT, OURLADS_ROSTER_URL_TEMPLATE.format(team="ATL")
    )


# ---- Name handling ----


def _convert_name_lastfirst_to_firstlast(raw: str) -> str:
    """`"Last, First Suffix"` -> `"First Last Suffix"`. If the comma is
    missing (already first-last), return as-is. Suffixes embedded in the
    "last" part (e.g. "Allen Jr., Carlos") are preserved at the end:
    "Allen Jr., Carlos" -> "Carlos Allen Jr.".
    """
    if "," not in raw:
        return raw.strip()
    last, first = raw.split(",", 1)
    return f"{first.strip()} {last.strip()}"


def _strip_annotation(raw: str) -> str:
    """Strip Ourlads' draft/status annotation suffix from a chart-cell name.
    The roster page does not append annotations, so this is a no-op there.
    """
    return _ANNOTATION_TAIL_RE.sub("", raw).strip()


def _extract_ourlads_id_from_anchor(a) -> str | None:  # bs4 Tag, untyped
    if a is None:
        return None
    href = a.get("href", "")
    m = _ROSTER_LINK_ID_RE.search(href) or _CHART_LINK_ID_RE.search(href)
    return m.group(1) if m else None


# ---- Parsers ----


def parse_roster(html: str, *, team: str) -> list[RosterRow]:
    """Parse a single team's roster page. Returns one RosterRow per active
    player; section dividers are skipped. The team identifier is supplied by
    the caller — Ourlads pages don't reliably embed it in the table.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    rows: list[RosterRow] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        # Headers (no <td>) and section dividers (single cell) are skipped.
        if len(cells) < 3:
            continue
        number_text = cells[0].get_text(" ", strip=True)
        player_cell = cells[1]
        position_text = cells[2].get_text(" ", strip=True)
        a = player_cell.find("a")
        raw_name = (a.get_text(" ", strip=True) if a else
                    player_cell.get_text(" ", strip=True))
        if not raw_name or not position_text:
            continue
        full_name = _convert_name_lastfirst_to_firstlast(_strip_annotation(raw_name))
        ourlads_id = _extract_ourlads_id_from_anchor(a)
        rows.append(
            RosterRow(
                team=team,
                full_name=full_name,
                position=position_text,
                number=number_text or None,
                ourlads_id=ourlads_id,
            )
        )
    return rows


def parse_all_chart(html: str) -> list[ChartEntry]:
    """Parse the all-teams depth chart page. Yields ChartEntry per filled
    player slot; up to 5 entries per (team, position) row. Skips dividers
    and incomplete rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    entries: list[ChartEntry] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 12:
            continue
        team_text = cells[0].get_text(" ", strip=True)
        pos_text = cells[1].get_text(" ", strip=True)
        # Header / divider rows have non-team text in cells[0] (e.g. "Team",
        # "Arizona Cardinals Updated: ...").
        if not team_text or len(team_text) > 4 or team_text == "Team":
            continue
        if not pos_text or pos_text == "Pos":
            continue
        # Slots: cells [2,3], [4,5], [6,7], [8,9], [10,11] — (No, Player).
        for slot_idx in range(5):
            num_cell = cells[2 + slot_idx * 2]
            player_cell = cells[3 + slot_idx * 2]
            a = player_cell.find("a")
            raw_name = (
                a.get_text(" ", strip=True) if a
                else player_cell.get_text(" ", strip=True)
            )
            if not raw_name:
                continue
            stripped = _strip_annotation(raw_name)
            full_name = _convert_name_lastfirst_to_firstlast(stripped)
            number = num_cell.get_text(" ", strip=True) or None
            entries.append(
                ChartEntry(
                    team=team_text,
                    full_name=full_name,
                    depth_chart_position=pos_text,
                    depth_chart_order=slot_idx + 1,
                    number=number,
                    ourlads_id=_extract_ourlads_id_from_anchor(a),
                )
            )
    return entries


# ---- Orchestration ----


def fetch_all(
    *,
    fetcher: Fetcher | None = None,
    delay_seconds: float | None = None,
    teams: tuple[str, ...] = NFL_TEAM_ABBRS,
) -> FetchAllResult:
    if delay_seconds is None:
        delay_seconds = DEFAULT_DELAY_SECONDS
    """Fetch every team roster + the all-teams chart, parse each, apply
    sanity bands, and merge into flat upsert-ready rows.

    Returns a FetchAllResult with:
      - rows: dicts ready for Database.upsert_players_for_source('ourlads')
      - completeness: {team_abbr: was_chart_observed_for_team}
      - errors: per-team failure entries (parse / sanity / network)

    Ordering: rosters first, all-teams chart last. Chart entries whose team
    disagrees with the just-fetched roster set are dropped (the brainstorm
    review's mid-run-trade race).
    """
    fetch = fetcher or _default_fetch
    result = FetchAllResult()

    # Phase 1: rosters per team.
    team_roster_rows: dict[str, list[RosterRow]] = {}
    for i, team in enumerate(teams):
        if i > 0:
            time.sleep(delay_seconds)
        url = OURLADS_ROSTER_URL_TEMPLATE.format(team=team)
        try:
            body = fetch(url).decode("utf-8", errors="ignore")
            rows = parse_roster(body, team=team)
        except OurladsFetchError as e:
            result.errors.append(TeamError(team=team, reason=f"fetch:{e}"))
            continue
        except Exception as e:  # parse error
            result.errors.append(TeamError(team=team, reason=f"parse:{e}"))
            continue
        if not (MIN_ROSTER_ROWS <= len(rows) <= MAX_ROSTER_ROWS):
            result.errors.append(
                TeamError(
                    team=team,
                    reason=f"sanity:{len(rows)}_rows_outside_band",
                )
            )
            continue
        team_roster_rows[team] = rows

    # Phase 2: all-teams chart (last).
    chart_entries: list[ChartEntry] = []
    chart_ok = False
    if delay_seconds > 0 and team_roster_rows:
        time.sleep(delay_seconds)
    try:
        body = fetch(OURLADS_ALL_CHART_URL).decode("utf-8", errors="ignore")
        chart_entries = parse_all_chart(body)
        chart_ok = MIN_TOTAL_CHART_ROWS <= len(chart_entries) <= MAX_TOTAL_CHART_ROWS
        if not chart_ok:
            result.errors.append(
                TeamError(
                    team="*chart*",
                    reason=f"sanity:{len(chart_entries)}_chart_rows_outside_band",
                )
            )
    except OurladsFetchError as e:
        result.errors.append(TeamError(team="*chart*", reason=f"fetch:{e}"))
        chart_ok = False
    except Exception as e:
        result.errors.append(TeamError(team="*chart*", reason=f"parse:{e}"))
        chart_ok = False

    # Build chart lookup: {(team, normalized_name): ChartEntry}. Reconcile
    # against rosters: drop chart entries whose team disagrees with that
    # team's roster (mid-run trade defense). When chart_ok is False we don't
    # apply chart writes; rosters still flow through.
    chart_by_team: dict[str, list[ChartEntry]] = {}
    if chart_ok:
        for entry in chart_entries:
            chart_by_team.setdefault(entry.team, []).append(entry)

    # Merge: for each successfully-fetched team, emit roster rows; if the
    # team has chart entries, decorate matched roster rows with depth fields
    # (and emit chart-only entries as Ourlads-only inserts the upsert can
    # resolve via name+team+position match).
    for team, rosters in team_roster_rows.items():
        chart_for_team = chart_by_team.get(team, [])
        # Index chart by simple name+position for fast lookup.
        chart_index: dict[tuple[str, str], ChartEntry] = {}
        for ce in chart_for_team:
            chart_index[(ce.full_name, ce.depth_chart_position)] = ce
        # Roster row → output, optionally enriched with depth from chart.
        seen_keys: set[tuple[str, str]] = set()
        for rr in rosters:
            key = (rr.full_name, rr.position)
            ce = chart_index.get(key)
            row = {
                "team": rr.team,
                "full_name": rr.full_name,
                "position": rr.position,
                "number": rr.number,
                "ourlads_id": rr.ourlads_id,
            }
            if ce is not None:
                row["depth_chart_position"] = ce.depth_chart_position
                row["depth_chart_order"] = ce.depth_chart_order
                seen_keys.add(key)
            result.rows.append(row)
        # Chart-only entries (positions on chart not in roster, e.g. an
        # opening-week starter listed at a position other than their
        # listed roster position): emit as separate rows so the upsert
        # can try identity match.
        for ce in chart_for_team:
            if (ce.full_name, ce.depth_chart_position) in seen_keys:
                continue
            # Chart's "position" semantically IS depth_chart_position;
            # we set position to that for matching purposes when no
            # roster row exists.
            result.rows.append(
                {
                    "team": ce.team,
                    "full_name": ce.full_name,
                    "position": ce.depth_chart_position,
                    "number": ce.number,
                    "ourlads_id": ce.ourlads_id,
                    "depth_chart_position": ce.depth_chart_position,
                    "depth_chart_order": ce.depth_chart_order,
                }
            )
        # Completeness: only true if we got both a healthy roster AND
        # a healthy chart for this team.
        result.completeness[team] = chart_ok and team in chart_by_team

    return result
