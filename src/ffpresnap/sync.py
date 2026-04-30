from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import ourlads as _ourlads
from . import sleeper as _sleeper
from ._naming import normalize_full_name
from .db import PLAYER_FIELDS, Database, _now
from .feeds import FeedFetchError, Fetcher, adapter_names, get_adapter
from .ourlads import OURLADS_ALL_CHART_URL
from .sleeper import PLAYERS_URL


FANTASY_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

# Fields stored as-is from the Sleeper payload. `player_id` and `updated_at` are
# handled separately by the DB layer; `fantasy_positions` is JSON-encoded below.
_PASSTHROUGH_FIELDS = tuple(
    f for f in PLAYER_FIELDS if f not in {"player_id", "updated_at", "fantasy_positions"}
)


FetchFn = Callable[..., dict[str, dict[str, Any]]]


def _is_fantasy_relevant(player: dict[str, Any]) -> bool:
    pos = player.get("position")
    if pos in FANTASY_POSITIONS:
        return True
    fps = player.get("fantasy_positions") or []
    if isinstance(fps, list) and any(p in FANTASY_POSITIONS for p in fps):
        return True
    return False


def _project(player_id: str, player: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"player_id": str(player_id)}
    for field in _PASSTHROUGH_FIELDS:
        row[field] = player.get(field)
    fps = player.get("fantasy_positions")
    row["fantasy_positions"] = json.dumps(fps) if fps else None
    return row


def run_sync(
    db: Database,
    *,
    source: str = "sleeper",
    fetch: FetchFn | None = None,
    source_url: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Pull data from the named source and persist it.

    Three source classes:
      * ``'sleeper'`` — wholesale player-data sync (synchronous).
      * ``'ourlads'`` — depth-chart merge sync (synchronous; long-running).
      * any registered feed adapter (e.g. ``'32beatwriters'``) — paginated
        feed ingestion that stores raw items in ``feed_items`` and writes
        auto-notes for matched players.

    ``full=True`` is meaningful only for feed sources — it disables the
    incremental-stop loop and walks the entire feed. Player-data sources
    ignore it.

    Always records a ``sync_runs`` row, even on failure. Raises
    ``ConcurrentSyncError`` if another run is in flight.
    """
    if source == "sleeper":
        return _run_sleeper_sync(db, fetch=fetch, source_url=source_url)
    if source == "ourlads":
        return _run_ourlads_sync(db, fetch=fetch, source_url=source_url)
    if source in adapter_names():
        return _run_feed_sync(
            db,
            adapter_name=source,
            fetch=fetch,
            source_url=source_url,
            full=full,
        )
    raise ValueError(f"Unknown sync source: {source!r}")


def _run_ourlads_sync(
    db: Database,
    *,
    fetch: FetchFn | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Pull rosters + the all-teams chart from Ourlads.com, merge into the
    players table via upsert_players_for_source('ourlads'). Records
    sync_runs.error with the per-team failure list when partial.
    """
    if source_url is None:
        source_url = OURLADS_ALL_CHART_URL
    run_start_at = _now()
    run_id = db.record_sync_start(source_url, source="ourlads")
    try:
        # `fetch` here is the raw bytes-returning Fetcher seam. _ourlads
        # owns its own default; tests inject a fake.
        result = _ourlads.fetch_all(fetcher=fetch)
        if len(result.errors) > _ourlads.MAX_FAILED_TEAMS:
            err_summary = ",".join(
                f"{e.team}:{e.reason}" for e in result.errors[:10]
            )
            raise RuntimeError(
                f"Ourlads sync exceeded MAX_FAILED_TEAMS={_ourlads.MAX_FAILED_TEAMS} "
                f"(got {len(result.errors)}): {err_summary}"
            )
        written = db.upsert_players_for_source(
            "ourlads",
            result.rows,
            completeness=result.completeness,
            run_start_at=run_start_at,
        )
        # Surface per-team failures even on a success run via sync_runs.error.
        error_text = (
            ",".join(f"{e.team}:{e.reason}" for e in result.errors)
            if result.errors
            else None
        )
        _run_feed_rematch(db, run_id=run_id)
        finished = db.record_sync_finish(run_id, written, "success", error=error_text)
        return {
            "run_id": run_id,
            "players_written": written,
            "status": "success",
            "source": "ourlads",
            "started_at": finished["started_at"],
            "finished_at": finished["finished_at"],
            "error": error_text,
            "team_errors": [
                {"team": e.team, "reason": e.reason} for e in result.errors
            ],
        }
    except Exception as e:
        db.record_sync_finish(run_id, 0, "error", error=str(e))
        raise


def _run_feed_rematch(db: Database, *, run_id: int) -> None:
    """Tail-of-sync back-match pass.

    Called from every successful sync (sleeper / ourlads / feed) so feed
    items that arrived before their player did get attached as soon as
    the player appears. Wrapped in a broad ``try/except`` because a
    feeds-side bug must never fail a player-data sync — failures here
    are degraded but non-blocking. The error is logged to stderr; the
    parent sync still records ``'success'``.
    """
    try:
        db.rematch_recent_unmatched_feed_items(
            window_days=30,
            run_id=run_id,
            note_body_for=build_feed_note_body,
        )
    except Exception as exc:
        sys.stderr.write(f"feed:rematch:error: {exc}\n")


def build_feed_note_body(item: dict[str, Any]) -> str:
    """Render an auto-note body from a feed item dict.

    Format: cleaned text body + a plain-text footer line carrying author /
    URL / publication date. The footer is what makes auto-notes visually
    distinguishable from hand-written notes when read through
    ``get_player_notes`` and the like.
    """
    cleaned = item.get("cleaned_text") or ""
    author = item.get("source_author") or "unknown"
    url = item.get("source_url") or ""
    created = (item.get("created_at") or "")[:10]
    return f"{cleaned.strip()}\n\n— {author} · {url} · {created}".strip()


def _run_feed_sync(
    db: Database,
    *,
    adapter_name: str,
    fetch: Fetcher | None = None,
    source_url: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Walk a feed adapter newest-first, persist each item idempotently,
    auto-create notes for matched players, and run a back-match pass for
    older unmatched items. Errors during fetch/parse are surfaced via
    sync_runs.error and re-raised; back-match errors are non-fatal.
    """
    adapter = get_adapter(adapter_name)
    if source_url is None:
        source_url = adapter.source_url

    def _is_seen(ext_id: str) -> bool:
        return db.feed_item_exists(adapter_name, ext_id)

    run_id = db.record_sync_start(source_url, source=adapter_name)
    items_fetched = 0
    items_new = 0
    items_matched = 0
    items_unmatched = 0
    try:
        for fi in adapter.fetch(full=full, fetch=fetch, is_seen=_is_seen):
            items_fetched += 1
            team_abbr = adapter.map_team(fi.external_team)
            player_id: str | None = None
            if team_abbr and fi.external_position and fi.external_player_name:
                normalized = normalize_full_name(fi.external_player_name)
                candidates = db.find_player_for_match(
                    normalized, team_abbr, fi.external_position
                )
                if len(candidates) == 1:
                    player_id = candidates[0]["player_id"]

            item_dict = fi.to_dict()
            item_dict["team_abbr"] = team_abbr
            note_body = build_feed_note_body(item_dict) if player_id else None

            result = db.add_feed_item_with_auto_note(
                adapter_name,
                item_dict,
                player_id=player_id,
                note_body=note_body,
                run_id=run_id,
            )
            if result["was_new"]:
                items_new += 1
                if result["matched_now"]:
                    items_matched += 1
                else:
                    items_unmatched += 1

        # Back-match recently-unmatched items in case a player got picked up
        # by Sleeper / Ourlads since this row was first seen.
        _run_feed_rematch(db, run_id=run_id)

        finished = db.record_sync_finish(
            run_id,
            status="success",
            items_fetched=items_fetched,
            items_new=items_new,
            items_matched=items_matched,
            items_unmatched=items_unmatched,
        )
        return {
            "run_id": run_id,
            "status": "success",
            "source": adapter_name,
            "items_fetched": items_fetched,
            "items_new": items_new,
            "items_matched": items_matched,
            "items_unmatched": items_unmatched,
            "started_at": finished["started_at"],
            "finished_at": finished["finished_at"],
        }
    except Exception as e:
        # Both expected (FeedFetchError, ConcurrentSyncError) and unexpected
        # exceptions get recorded with whatever counters had accumulated
        # before the failure, then re-raised so the caller / CLI sees them.
        db.record_sync_finish(
            run_id,
            status="error",
            error=str(e),
            items_fetched=items_fetched,
            items_new=items_new,
            items_matched=items_matched,
            items_unmatched=items_unmatched,
        )
        raise


def _run_sleeper_sync(
    db: Database,
    *,
    fetch: FetchFn | None,
    source_url: str | None,
) -> dict[str, Any]:
    if fetch is None:
        fetch = _sleeper.fetch_players
    if source_url is None:
        source_url = PLAYERS_URL
    run_start_at = _now()
    run_id = db.record_sync_start(source_url, source="sleeper")
    try:
        payload = fetch(source_url)
        rows = [
            _project(pid, p)
            for pid, p in payload.items()
            if isinstance(p, dict) and _is_fantasy_relevant(p)
        ]
        written = db.upsert_players_for_source(
            "sleeper", rows, run_start_at=run_start_at
        )
        _run_feed_rematch(db, run_id=run_id)
        finished = db.record_sync_finish(run_id, written, "success")
        return {
            "run_id": run_id,
            "players_written": written,
            "status": "success",
            "source": "sleeper",
            "started_at": finished["started_at"],
            "finished_at": finished["finished_at"],
        }
    except Exception as e:
        db.record_sync_finish(run_id, 0, "error", error=str(e))
        raise
