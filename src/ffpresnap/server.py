from __future__ import annotations

import asyncio
import json
import sys
import threading
import traceback
from typing import Any

from .db import (
    AmbiguousTeamError,
    ConcurrentSyncError,
    Database,
    NotFoundError,
)
from .sleeper import SleeperFetchError
from .sync import run_sync


# Tracks Ourlads sync background threads so they aren't garbage collected
# mid-run. Thread bodies open their own DB connection via Database.open.
_BACKGROUND_RUNS: dict[int, threading.Thread] = {}


_MENTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "player_ids": {"type": "array", "items": {"type": "string"}},
        "team_abbrs": {"type": "array", "items": {"type": "string"}},
    },
}


TOOLS: list[dict[str, Any]] = [
    # --- sync ---
    {
        "name": "sync_players",
        "description": (
            "Pull the current NFL player set from a source and merge into local "
            "data. `source` is 'sleeper' (default) or 'ourlads'. Sleeper sync is "
            "synchronous (~5s) and returns the full summary. Ourlads sync runs "
            "in a background thread (~1-3 minutes for 33 page fetches) and "
            "returns a `run_id` immediately — poll `get_sync_status(run_id)` "
            "for progress. Records every run in `sync_runs`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["sleeper", "ourlads"],
                    "default": "sleeper",
                },
            },
        },
    },
    {
        "name": "last_sync",
        "description": (
            "Return the most recent sync run, or null if none has run yet. "
            "Optional `source` restricts to runs of that source."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["sleeper", "ourlads"],
                },
            },
        },
    },
    {
        "name": "get_sync_status",
        "description": (
            "Return the sync_runs row for a given `run_id` (or null if not "
            "found). Use after starting an Ourlads sync via `sync_players` to "
            "poll for completion. The row's `status` field becomes 'success' "
            "or 'error' once the background run finishes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
            },
            "required": ["run_id"],
        },
    },
    # --- browse ---
    {
        "name": "list_teams",
        "description": (
            "List NFL teams. Optional `query` filters by abbreviation, full name, "
            "conference (AFC/NFC), or division (North/South/East/West)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "get_team",
        "description": (
            "Return a team record along with two note lists: `notes` (where the team "
            "is the primary subject) and `mentions` (notes elsewhere that tag this team). "
            "`team` accepts abbr, full name, or unique nickname (e.g. 'KC', 'Chiefs')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"team": {"type": "string"}},
            "required": ["team"],
        },
    },
    {
        "name": "get_depth_chart",
        "description": (
            "Return a team's depth chart, grouped by depth_chart_position. Unranked "
            "players land in a trailing 'Unranked' group."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"team": {"type": "string"}},
            "required": ["team"],
        },
    },
    {
        "name": "find_player",
        "description": "Search players by case-insensitive name substring (max 10).",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_player",
        "description": (
            "Return full player detail with two note lists: `notes` (about this "
            "player) and `mentions` (notes elsewhere that tag this player)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"player_id": {"type": "string"}},
            "required": ["player_id"],
        },
    },
    {
        "name": "list_players",
        "description": (
            "List players, optionally filtered by `team` (abbr), `position`, "
            "and/or `watchlist` (true to show only watchlisted players)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "position": {"type": "string"},
                "watchlist": {"type": "boolean"},
            },
        },
    },
    {
        "name": "update_player",
        "description": (
            "Update mutable, user-owned attributes on a player. Currently only "
            "`watchlist` (boolean). Sleeper-sourced fields are not editable here — "
            "they reconcile from Sleeper on every sync."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "watchlist": {"type": "boolean"},
            },
            "required": ["player_id"],
        },
    },
    # --- studies ---
    {
        "name": "create_study",
        "description": (
            "Create a research study (a named container you can attach many notes to). "
            "Status defaults to 'open'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_studies",
        "description": (
            "List studies. `status` defaults to 'open'; pass 'archived' or 'all'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "archived", "all"]},
            },
        },
    },
    {
        "name": "get_study",
        "description": "Return a study with its notes (newest first).",
        "inputSchema": {
            "type": "object",
            "properties": {"study_id": {"type": "integer"}},
            "required": ["study_id"],
        },
    },
    {
        "name": "update_study",
        "description": "Update a study's title and/or description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "study_id": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["study_id"],
        },
    },
    {
        "name": "set_study_status",
        "description": "Set a study's status to 'open' or 'archived'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "study_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["open", "archived"]},
            },
            "required": ["study_id", "status"],
        },
    },
    {
        "name": "delete_study",
        "description": "Delete a study and all of its notes (cascades).",
        "inputSchema": {
            "type": "object",
            "properties": {"study_id": {"type": "integer"}},
            "required": ["study_id"],
        },
    },
    # --- notes (unified) ---
    {
        "name": "add_note",
        "description": (
            "Attach a note to a subject. `target_type` is 'player', 'team', or 'study'. "
            "`target_id` is the Sleeper player_id (string), team identifier "
            "(abbr/name/nickname), or study id (integer-as-string). Optional "
            "`mentions` tags other players and teams referenced in the note body."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "enum": ["player", "team", "study"]},
                "target_id": {"type": "string"},
                "body": {"type": "string"},
                "mentions": _MENTIONS_SCHEMA,
            },
            "required": ["target_type", "target_id", "body"],
        },
    },
    {
        "name": "list_notes",
        "description": (
            "List notes. `scope` selects the source: 'player', 'team', and 'study' "
            "list primary-subject notes for the given `target_id`; 'recent' returns "
            "a chronological feed across all subjects. `limit` (default 50, max 200) "
            "applies to 'recent' only. Notes always include their `mentions` block; "
            "'recent' entries also carry a `subject` block resolving the type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["player", "team", "study", "recent"],
                },
                "target_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["scope"],
        },
    },
    {
        "name": "update_note",
        "description": (
            "Replace the body of an existing note. Optional `mentions` replaces the "
            "stored mention set wholesale; omit it to leave mentions unchanged."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {"type": "integer"},
                "body": {"type": "string"},
                "mentions": _MENTIONS_SCHEMA,
            },
            "required": ["note_id", "body"],
        },
    },
    {
        "name": "delete_note",
        "description": "Delete a note by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"note_id": {"type": "integer"}},
            "required": ["note_id"],
        },
    },
    # --- prompt library ---
    {
        "name": "list_prompts",
        "description": (
            "Return the prompt library: a curated catalog of canned prompts that "
            "direct Claude to build dashboard artifacts (study browser, depth chart "
            "explorer, note feed, etc.). Each item has slug, title, description, and "
            "body. The agent is expected to render the result as an artifact with one "
            "card per prompt and a copy-to-clipboard button per card."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    """Raised by handlers; surfaced to the MCP client as a tool error."""


def _group_depth_chart(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str | None, list[dict[str, Any]]] = {}
    order: list[str | None] = []
    for p in players:
        pos = p.get("depth_chart_position")
        if pos not in groups:
            groups[pos] = []
            order.append(pos)
        groups[pos].append(p)
    out: list[dict[str, Any]] = []
    for pos in order:
        out.append({"position": pos if pos is not None else "Unranked", "players": groups[pos]})
    return out


def _add_note_dispatch(
    db: Database,
    target_type: str,
    target_id: str,
    body: str,
    mentions: dict[str, Any] | None,
) -> dict[str, Any]:
    if target_type == "player":
        return db.add_note(str(target_id), body, mentions=mentions)
    if target_type == "team":
        return db.add_team_note(str(target_id), body, mentions=mentions)
    if target_type == "study":
        try:
            sid = int(target_id)
        except (TypeError, ValueError) as e:
            raise ToolError(
                f"target_id for study must be an integer, got {target_id!r}"
            ) from e
        return db.add_study_note(sid, body, mentions=mentions)
    raise ToolError(f"Unknown target_type: {target_type!r}")


def _list_notes_dispatch(
    db: Database, scope: str, target_id: str | None, limit: int | None
) -> Any:
    if scope == "recent":
        eff = int(limit if limit is not None else 50)
        eff = max(1, min(eff, 200))
        return db.list_recent_notes(limit=eff)
    if target_id is None:
        raise ToolError(f"target_id is required when scope is {scope!r}")
    if scope == "player":
        pid = str(target_id)
        return {"player": db.get_player(pid), "notes": db.list_notes(pid)}
    if scope == "team":
        team = db.get_team(target_id)
        return {"team": team, "notes": db.list_team_notes(team["abbr"])}
    if scope == "study":
        try:
            sid = int(target_id)
        except (TypeError, ValueError) as e:
            raise ToolError(
                f"target_id for study must be an integer, got {target_id!r}"
            ) from e
        return {"study": db.get_study(sid), "notes": db.list_study_notes(sid)}
    raise ToolError(f"Unknown scope: {scope!r}")


def _start_background_ourlads_sync(db: Database) -> dict[str, Any]:
    """Start an Ourlads sync in a daemon thread. Returns a run summary
    immediately containing the run_id — poll get_sync_status to track
    completion. The thread opens its own Database connection (sqlite3
    connections are not safe to share across threads by default).

    The advisory concurrency lock is acquired by run_sync via
    record_sync_start in the worker thread; if a run is already in flight,
    the worker thread surfaces the ConcurrentSyncError via sync_runs and
    this function still returns a run summary. To present a clean error to
    the caller when the lock is contended, we do a foreground pre-check.
    """
    # Pre-check the advisory lock so the caller sees a clean ToolError instead
    # of an opaque "running" status that immediately flips to error.
    from datetime import datetime, timedelta, timezone
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat(timespec="seconds")
    existing = db.conn.execute(
        "SELECT id, source FROM sync_runs WHERE status = 'running' "
        "AND started_at > ?",
        (cutoff,),
    ).fetchone()
    if existing is not None:
        raise ConcurrentSyncError(
            f"Another sync ({existing['source']}, run_id={existing['id']}) "
            "is already running. Wait for it to finish or fail."
        )
    if db.path is None:
        raise RuntimeError(
            "Cannot start a background Ourlads sync: Database has no path. "
            "Open the Database via Database.open(...) so the worker thread "
            "can attach its own connection."
        )

    db_path = str(db.path)

    # Capture the highest existing ourlads run_id so we can detect the
    # worker's record_sync_start landing as soon as the new id appears.
    pre_run = db.last_sync(source="ourlads")
    pre_id = pre_run["id"] if pre_run else 0

    def worker(path: str) -> None:
        bg = Database.open(path)
        try:
            try:
                run_sync(bg, source="ourlads")
            except Exception:
                # run_sync records sync_runs failure on its own; this prints
                # the traceback to stderr for operator visibility.
                traceback.print_exc()
        finally:
            bg.close()

    thread = threading.Thread(target=worker, args=(db_path,), daemon=True)
    thread.start()

    # Wait briefly for the worker's record_sync_start to land. We're done as
    # soon as we observe a new ourlads run_id (regardless of its current
    # status — by the time we poll, it may already have flipped to 'error'
    # for fast-failing runs like the current Unit 4 stub).
    import time
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        latest = db.last_sync(source="ourlads")
        if latest is not None and latest["id"] > pre_id:
            _BACKGROUND_RUNS[latest["id"]] = thread
            return {
                "run_id": latest["id"],
                "status": latest["status"],
                "source": "ourlads",
                "started_at": latest["started_at"],
                "finished_at": latest.get("finished_at"),
                "error": latest.get("error"),
            }
        time.sleep(0.05)
    # Worker didn't register in time — surface that.
    raise ToolError(
        "Ourlads sync background thread failed to start within 2s; "
        "check stderr for traceback."
    )


def handle_tool_call(db: Database, name: str, args: dict[str, Any]) -> Any:
    """Pure dispatch over tool name. Raises ToolError on user-facing failures."""
    args = args or {}
    try:
        if name == "sync_players":
            source = args.get("source", "sleeper")
            if source == "ourlads":
                return _start_background_ourlads_sync(db)
            return run_sync(db, source=source)
        if name == "last_sync":
            return db.last_sync(source=args.get("source"))
        if name == "get_sync_status":
            return db.get_sync_run(int(args["run_id"]))
        if name == "list_teams":
            return db.list_teams(args.get("query"))
        if name == "get_team":
            team = db.get_team(args["team"])
            return {
                "team": team,
                "notes": db.list_team_notes(team["abbr"]),
                "mentions": db.list_team_mentions(team["abbr"]),
            }
        if name == "get_depth_chart":
            team = db.get_team(args["team"])
            players = db.depth_chart(team["abbr"])
            return {"team": team, "groups": _group_depth_chart(players)}
        if name == "find_player":
            return db.find_players(args["query"], limit=10)
        if name == "get_player":
            pid = str(args["player_id"])
            return {
                "player": db.get_player(pid),
                "notes": db.list_notes(pid),
                "mentions": db.list_player_mentions(pid),
            }
        if name == "list_players":
            return db.list_players(
                team=args.get("team"),
                position=args.get("position"),
                watchlist=args.get("watchlist"),
            )
        if name == "update_player":
            pid = str(args["player_id"])
            if "watchlist" in args:
                db.set_watchlist(pid, bool(args["watchlist"]))
            return db.get_player(pid)
        if name == "create_study":
            return db.create_study(args["title"], description=args.get("description"))
        if name == "list_studies":
            status = args.get("status", "open")
            if status == "all":
                status = None
            return db.list_studies(status=status)
        if name == "get_study":
            sid = int(args["study_id"])
            return {
                "study": db.get_study(sid),
                "notes": db.list_study_notes(sid),
                "mentions": [],
            }
        if name == "update_study":
            return db.update_study(
                int(args["study_id"]),
                title=args.get("title"),
                description=args.get("description"),
            )
        if name == "set_study_status":
            return db.set_study_status(int(args["study_id"]), args["status"])
        if name == "delete_study":
            sid = int(args["study_id"])
            db.delete_study(sid)
            return {"ok": True, "deleted_study_id": sid}
        if name == "add_note":
            return _add_note_dispatch(
                db,
                args["target_type"],
                args["target_id"],
                args["body"],
                args.get("mentions"),
            )
        if name == "list_notes":
            return _list_notes_dispatch(
                db, args["scope"], args.get("target_id"), args.get("limit")
            )
        if name == "update_note":
            return db.update_note(
                int(args["note_id"]), args["body"], mentions=args.get("mentions")
            )
        if name == "delete_note":
            db.delete_note(int(args["note_id"]))
            return {"ok": True, "deleted_note_id": int(args["note_id"])}
        if name == "list_prompts":
            return db.list_prompts()
    except AmbiguousTeamError as e:
        candidates = "\n".join(
            f"  - {m['abbr']} ({m['full_name']})" for m in e.matches
        )
        raise ToolError(
            f"Team '{e.query}' is ambiguous. Re-call with a more specific identifier:\n{candidates}"
        ) from e
    except NotFoundError as e:
        raise ToolError(str(e)) from e
    except SleeperFetchError as e:
        raise ToolError(f"Sleeper sync failed: {e}") from e
    except ConcurrentSyncError as e:
        raise ToolError(str(e)) from e
    except KeyError as e:
        raise ToolError(f"Missing required argument: {e.args[0]}") from e

    raise ToolError(f"Unknown tool: {name}")


def _format_result(result: Any) -> str:
    return json.dumps(result, indent=2, default=str)


async def _serve(db: Database) -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    server: Server = Server("ffpresnap")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [Tool(**t) for t in TOOLS]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = handle_tool_call(db, name, arguments or {})
        return [TextContent(type="text", text=_format_result(result))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    db = Database.open()
    print(f"[ffpresnap] db: {Database.resolve_path()}", file=sys.stderr)
    try:
        asyncio.run(_serve(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
