from __future__ import annotations

import json
from typing import Any, Callable

from . import sleeper as _sleeper
from .db import PLAYER_FIELDS, Database, _now
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
) -> dict[str, Any]:
    """Pull player data from the named source, project rows, and write them
    via Database.upsert_players_for_source. Always records a sync_runs row,
    even on failure. Raises ConcurrentSyncError if another run is in flight.

    Currently supports source='sleeper'. Source='ourlads' will land in a
    future unit; this function dispatches based on the source value.
    """
    if source == "sleeper":
        return _run_sleeper_sync(db, fetch=fetch, source_url=source_url)
    if source == "ourlads":
        return _run_ourlads_sync_stub(db, source_url=source_url)
    raise ValueError(f"Unknown sync source: {source!r}")


def _run_ourlads_sync_stub(
    db: Database, *, source_url: str | None
) -> dict[str, Any]:
    """Placeholder Ourlads sync. Records sync_runs lifecycle correctly so
    callers can poll get_sync_status; the actual fetch + parse logic lands
    in Unit 4. For now this records the run start, then immediately records
    a failure with a NotImplementedError-style message so the background
    worker's behavior is observable end-to-end.
    """
    if source_url is None:
        source_url = "https://www.ourlads.com/nfldepthcharts/"
    run_id = db.record_sync_start(source_url, source="ourlads")
    err_msg = "Ourlads sync pipeline lands in Unit 4; not yet wired."
    db.record_sync_finish(run_id, 0, "error", error=err_msg)
    raise NotImplementedError(err_msg)


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
