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
from .feeds import FeedFetchError, adapter_names
from .sleeper import SleeperFetchError
from .sync import run_sync


def _all_source_names() -> list[str]:
    """Build the dynamic ``source`` enum for the unified `sync` tool.

    Player-data sources (``sleeper``, ``ourlads``) plus every registered
    feed adapter. Importing :mod:`ffpresnap.feeds` triggers adapter
    registration; new feed adapters appear here automatically without any
    server-side change.
    """
    return ["sleeper", "ourlads", *adapter_names()]


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
        "name": "sync",
        "description": (
            "Pull data from a source and merge it locally. Response shape depends on `source`:\n"
            "  • `sleeper` — synchronous (~5s). Returns `{status: 'success', source, run_id, players_written, ...}` directly.\n"
            "  • `ourlads` — background thread (~1-3 min). Returns `{status: 'running', source, run_id, ...}` immediately; poll `get_sync_status(run_id)` until status is 'success' or 'error'.\n"
            "  • any feed source (e.g. `32beatwriters`) — background thread, paginated. Returns `{status: 'running', source, run_id, ...}` immediately; poll `get_sync_status(run_id)` for the final `items_fetched/new/matched/unmatched` counters.\n"
            "Feed sources also accept `full=true` to backfill the entire feed instead of stopping at the first fully-seen page. Records every run in `sync_runs`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": _all_source_names(),
                },
                "full": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Feed sources only: walk the entire feed instead of "
                        "stopping at the first fully-seen page. Use for first-"
                        "run backfill or reconciliation."
                    ),
                },
            },
            "required": ["source"],
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
                    "enum": _all_source_names(),
                },
            },
        },
    },
    {
        "name": "get_sync_status",
        "description": (
            "Return the sync_runs row for a given `run_id` (or null if not "
            "found). Use after starting a background sync via `sync` to poll "
            "for completion. The row's `status` field becomes 'success' or "
            "'error' once the background run finishes. Feed-sync rows include "
            "`items_fetched`, `items_new`, `items_matched`, `items_unmatched`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
            },
            "required": ["run_id"],
        },
    },
    # --- feeds (read + maintenance) ---
    {
        "name": "list_feed_items",
        "description": (
            "List raw items pulled from feed adapters. Filters AND-combine. "
            "`player_id` restricts to items attached to that player. `source` "
            "restricts to items from a single feed (e.g. '32beatwriters'). "
            "`since` is an ISO timestamp lower bound on `created_at`. "
            "`matched=true` returns only items already linked to a player; "
            "`matched=false` returns only unmatched items (rookies/prospects "
            "awaiting back-match). `limit` defaults to 50."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "source": {"type": "string"},
                "since": {"type": "string"},
                "matched": {"type": "boolean"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "rematch_feed_items",
        "description": (
            "Retry identity matching for unmatched feed items ingested in the "
            "last `window_days` days (default 30). Useful after a Sleeper or "
            "Ourlads sync brings in players that were previously unknown — "
            "the same pass runs at the tail of every sync, but you can also "
            "trigger it manually. Returns `{checked, matched, notes_written}`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "delete_auto_notes_from_run",
        "description": (
            "Bulk-rollback for a misfiring feed sync. Deletes every auto-note "
            "whose backing `feed_items` row was first-attached during the "
            "given sync run. Leaves the raw `feed_items` rows alive (their "
            "`note_id` becomes NULL). Idempotent. Non-feed run IDs are "
            "no-ops. Returns `{deleted_notes: N}`."
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


def _start_background_sync(
    db: Database, *, source: str, full: bool = False
) -> dict[str, Any]:
    """Start a long-running sync in a daemon thread. Returns a run summary
    immediately containing the run_id — poll get_sync_status to track
    completion. The thread opens its own Database connection (sqlite3
    connections are not safe to share across threads by default).

    Used for ourlads (~1-3 min) and feed sources (variable; bounded by
    MAX_PAGES_INCREMENTAL × DELAY_SECONDS). Sleeper sync stays synchronous
    in the request thread.

    The advisory concurrency lock is acquired in the worker via
    record_sync_start; this function does a foreground pre-check so the
    caller sees a clean ToolError instead of polling a brief 'running'
    that immediately flips to 'error'.
    """
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
            f"Cannot start a background {source} sync: Database has no path. "
            "Open the Database via Database.open(...) so the worker thread "
            "can attach its own connection."
        )

    db_path = str(db.path)
    pre_run = db.last_sync(source=source)
    pre_id = pre_run["id"] if pre_run else 0

    def worker(path: str) -> None:
        bg = Database.open(path)
        try:
            try:
                run_sync(bg, source=source, full=full)
            except Exception:
                # run_sync records the sync_runs failure on its own; print the
                # traceback to stderr for operator visibility.
                traceback.print_exc()
        finally:
            bg.close()

    thread = threading.Thread(target=worker, args=(db_path,), daemon=True)
    thread.start()

    # Wait briefly for the worker's record_sync_start to land. We're done as
    # soon as we observe a new run_id for this source.
    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        latest = db.last_sync(source=source)
        if latest is not None and latest["id"] > pre_id:
            _BACKGROUND_RUNS[latest["id"]] = thread
            return {
                "run_id": latest["id"],
                "status": latest["status"],
                "source": source,
                "started_at": latest["started_at"],
                "finished_at": latest.get("finished_at"),
                "error": latest.get("error"),
            }
        time.sleep(0.05)
    raise ToolError(
        f"{source} sync background thread failed to start within 2s; "
        "check stderr for traceback."
    )


def handle_tool_call(db: Database, name: str, args: dict[str, Any]) -> Any:
    """Pure dispatch over tool name. Raises ToolError on user-facing failures."""
    args = args or {}
    try:
        if name == "sync":
            source = args.get("source")
            if not source:
                raise ToolError("Missing required argument: source")
            full = bool(args.get("full", False))
            if source == "sleeper":
                return run_sync(db, source=source)
            if source == "ourlads" or source in adapter_names():
                return _start_background_sync(db, source=source, full=full)
            raise ToolError(f"Unknown sync source: {source!r}")
        if name == "last_sync":
            return db.last_sync(source=args.get("source"))
        if name == "get_sync_status":
            return db.get_sync_run(int(args["run_id"]))
        if name == "list_feed_items":
            return db.list_feed_items(
                player_id=args.get("player_id"),
                source=args.get("source"),
                since=args.get("since"),
                matched=args.get("matched"),
                limit=int(args.get("limit", 50)),
            )
        if name == "rematch_feed_items":
            from .sync import build_feed_note_body

            return db.rematch_recent_unmatched_feed_items(
                window_days=int(args.get("window_days", 30)),
                note_body_for=build_feed_note_body,
            )
        if name == "delete_auto_notes_from_run":
            deleted = db.delete_auto_notes_from_run(int(args["run_id"]))
            return {"deleted_notes": deleted}
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
    except FeedFetchError as e:
        raise ToolError(f"Feed sync failed: {e}") from e
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
