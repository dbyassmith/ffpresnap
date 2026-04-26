from __future__ import annotations

import json
from typing import Any, Callable

from . import sleeper as _sleeper
from .db import PLAYER_FIELDS, Database
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
    fetch: FetchFn | None = None,
    source_url: str = PLAYERS_URL,
) -> dict[str, Any]:
    if fetch is None:
        fetch = _sleeper.fetch_players
    """Pull Sleeper player data, filter to fantasy positions, and atomically replace
    the players table. Always records a sync_runs row, even on failure.
    """
    run_id = db.record_sync_start(source_url)
    try:
        payload = fetch(source_url)
        rows = [
            _project(pid, p)
            for pid, p in payload.items()
            if isinstance(p, dict) and _is_fantasy_relevant(p)
        ]
        written = db.replace_players(rows)
        finished = db.record_sync_finish(run_id, written, "success")
        return {
            "run_id": run_id,
            "players_written": written,
            "status": "success",
            "started_at": finished["started_at"],
            "finished_at": finished["finished_at"],
        }
    except Exception as e:
        db.record_sync_finish(run_id, 0, "error", error=str(e))
        raise
