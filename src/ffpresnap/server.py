from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .db import AmbiguousTeamError, Database, NotFoundError
from .sleeper import SleeperFetchError
from .sync import run_sync


TOOLS: list[dict[str, Any]] = [
    {
        "name": "sync_players",
        "description": (
            "Pull the current NFL player set from Sleeper and replace local data. "
            "Filters to fantasy positions (QB/RB/WR/TE/K/DEF). Records the run."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "last_sync",
        "description": "Return the most recent sync run, or null if none has run yet.",
        "inputSchema": {"type": "object", "properties": {}},
    },
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
        "name": "get_depth_chart",
        "description": (
            "Return a team's depth chart. `team` accepts an abbreviation (e.g. 'KC'), "
            "full name ('Kansas City Chiefs'), or unique nickname ('Chiefs'). "
            "Players are grouped by depth_chart_position; unranked players are returned "
            "in a trailing 'Unranked' group."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"team": {"type": "string"}},
            "required": ["team"],
        },
    },
    {
        "name": "find_player",
        "description": (
            "Search players by case-insensitive name substring. Returns up to 10 matches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_player",
        "description": (
            "Return full detail for a player along with their notes (newest first). "
            "`player_id` is the Sleeper player id (string)."
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
            "List players, optionally filtered by `team` (abbreviation) and/or `position`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "position": {"type": "string"},
            },
        },
    },
    {
        "name": "add_note",
        "description": (
            "Attach a note to a player. `player_id` is the Sleeper player id (string)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["player_id", "body"],
        },
    },
    {
        "name": "list_notes",
        "description": "List notes for a player, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"player_id": {"type": "string"}},
            "required": ["player_id"],
        },
    },
    {
        "name": "add_team_note",
        "description": (
            "Attach a note to a team. `team` accepts an abbreviation, full name, or "
            "unique nickname (e.g. 'KC', 'Kansas City Chiefs', 'Chiefs')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["team", "body"],
        },
    },
    {
        "name": "list_team_notes",
        "description": "List notes for a team, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"team": {"type": "string"}},
            "required": ["team"],
        },
    },
    {
        "name": "list_recent_notes",
        "description": (
            "List notes across all players and teams in chronological order "
            "(newest first), with subject info resolved. Optional `limit` "
            "(default 50, max 200)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "update_note",
        "description": "Replace the body of an existing note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {"type": "integer"},
                "body": {"type": "string"},
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


def handle_tool_call(db: Database, name: str, args: dict[str, Any]) -> Any:
    """Pure dispatch over tool name. Raises ToolError on user-facing failures."""
    args = args or {}
    try:
        if name == "sync_players":
            return run_sync(db)
        if name == "last_sync":
            return db.last_sync()
        if name == "list_teams":
            return db.list_teams(args.get("query"))
        if name == "get_depth_chart":
            team_query = args["team"]
            team = db.get_team(team_query)
            players = db.depth_chart(team["abbr"])
            return {"team": team, "groups": _group_depth_chart(players)}
        if name == "find_player":
            return db.find_players(args["query"], limit=10)
        if name == "get_player":
            pid = str(args["player_id"])
            return {"player": db.get_player(pid), "notes": db.list_notes(pid)}
        if name == "list_players":
            return db.list_players(team=args.get("team"), position=args.get("position"))
        if name == "add_note":
            return db.add_note(str(args["player_id"]), args["body"])
        if name == "list_notes":
            pid = str(args["player_id"])
            return {"player": db.get_player(pid), "notes": db.list_notes(pid)}
        if name == "add_team_note":
            return db.add_team_note(args["team"], args["body"])
        if name == "list_team_notes":
            team = db.get_team(args["team"])
            return {"team": team, "notes": db.list_team_notes(team["abbr"])}
        if name == "list_recent_notes":
            limit = int(args.get("limit") or 50)
            limit = max(1, min(limit, 200))
            return db.list_recent_notes(limit=limit)
        if name == "update_note":
            return db.update_note(int(args["note_id"]), args["body"])
        if name == "delete_note":
            db.delete_note(int(args["note_id"]))
            return {"ok": True, "deleted_note_id": int(args["note_id"])}
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
