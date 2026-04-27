from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .db import AmbiguousTeamError, Database, NotFoundError
from .sleeper import SleeperFetchError
from .sync import run_sync


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
        "description": "List players, optionally filtered by `team` (abbr) and/or `position`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "position": {"type": "string"},
            },
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
            return db.list_players(team=args.get("team"), position=args.get("position"))
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
