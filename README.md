# ffpresnap

Local SQLite store for NFL player data and notes, mirrored from the [Sleeper API](https://docs.sleeper.com/) and exposed to Claude (Desktop / Code / Cowork) via an MCP server.

## Install

Requires Python 3.11+.

```bash
pip install -e .
# or with dev deps for tests
pip install -e ".[dev]"
```

This installs two console scripts:

- `ffpresnap-mcp` — the MCP server.
- `ffpresnap-sync` — pulls the latest NFL player data from Sleeper into the local DB.

## Initial sync

Players are loaded from Sleeper, not entered by hand. Run sync once after install:

```bash
ffpresnap-sync
```

You can also invoke `sync_players` from inside Claude. Sleeper recommends polling the player dump no more than once per day; a daily cron is a reasonable default:

```cron
0 9 * * * ffpresnap-sync
```

Sync filters to fantasy-relevant positions (QB, RB, WR, TE, K, DEF). Each run is recorded; ask Claude to call `last_sync` to see when it last ran.

## Configure Claude

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ffpresnap": { "command": "ffpresnap-mcp" }
  }
}
```

**Claude Code** — add to `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "ffpresnap": { "command": "ffpresnap-mcp" }
  }
}
```

Restart the client. The `ffpresnap` tools should appear.

## Tools

Sync:

- `sync_players()` — pull current Sleeper data into the local DB.
- `last_sync()` — show the most recent sync run.

Browse:

- `list_teams(query?)` — list NFL teams; filter by abbreviation, name, conference, or division.
- `get_depth_chart(team)` — depth chart for a team. `team` accepts `"KC"`, `"Kansas City Chiefs"`, or `"Chiefs"`. Players group by depth-chart position; unranked players appear in a trailing `Unranked` group.
- `find_player(query)` — case-insensitive name substring search (max 10).
- `get_player(player_id)` — full player detail plus notes (newest first).
- `list_players(team?, position?)` — flat listing with optional filters.

Notes:

- `add_note(player_id, body)` — attach a note. `player_id` is the Sleeper id (string).
- `list_notes(player_id)` — newest first.
- `update_note(note_id, body)` — replace a note's body.
- `delete_note(note_id)` — delete a note.

## Data location

The local DB lives at `~/.ffpresnap/notes.db` by default; override with `FFPRESNAP_DB`. The path is logged to stderr on MCP startup.

**Upgrade note:** This version is keyed on Sleeper player ids and drops the previous hand-maintained players table on first open. Existing players and notes from older versions are wiped — back up the DB before upgrading if you need them.

## Tests

```bash
pytest
```
