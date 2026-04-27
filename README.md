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
- `get_team(team)` — team record plus its notes and any notes elsewhere that mention it.
- `get_depth_chart(team)` — depth chart for a team. `team` accepts `"KC"`, `"Kansas City Chiefs"`, or `"Chiefs"`. Players group by depth-chart position; unranked players appear in a trailing `Unranked` group.
- `find_player(query)` — case-insensitive name substring search (max 10).
- `get_player(player_id)` — full player detail plus two note lists: `notes` (about this player) and `mentions` (notes elsewhere that tag this player).
- `list_players(team?, position?)` — flat listing with optional filters.

Studies (research containers):

- `create_study(title, description?)` — start a new research thread (defaults to `open`).
- `list_studies(status?)` — defaults to open; pass `"archived"` or `"all"`.
- `get_study(study_id)` — study record plus its notes.
- `update_study(study_id, title?, description?)` — partial update.
- `archive_study(study_id)` / `unarchive_study(study_id)` — toggle status.
- `delete_study(study_id)` — deletes the study and all of its notes (cascades).
- `add_study_note(study_id, body, mentions?)` / `list_study_notes(study_id)` — attach and list notes on a study.

Notes:

- `add_note(player_id, body)` — attach a note to a player. `player_id` is the Sleeper id (string).
- `list_notes(player_id)` — newest first.
- `add_team_note(team, body)` — attach a note to a team. `team` accepts abbr / full name / nickname.
- `list_team_notes(team)` — newest first.
- `list_recent_notes(limit?)` — chronological feed across all players and teams (default 50, max 200), with subject info resolved.
- `update_note(note_id, body, mentions?)` — replace a note's body. If `mentions` is provided, the stored mention set is replaced wholesale; omit to leave mentions unchanged. Works for player, team, and study notes.
- `delete_note(note_id)` — delete a note (works for player, team, and study notes).

**Mentions.** Every `add_*_note` and `update_note` accepts an optional `mentions: { player_ids: [...], team_abbrs: [...] }`. Mentions are explicit (Claude passes them in the tool call) and validated at write time — an unknown player or unresolvable team rejects the whole write. From a player or team's view, notes that tag them appear in the `mentions` list, separate from notes written *about* them.

## Data location

The local DB lives at `~/.ffpresnap/notes.db` by default; override with `FFPRESNAP_DB`. The path is logged to stderr on MCP startup.

**Upgrade note:** This version is keyed on Sleeper player ids and drops the previous hand-maintained players table on first open. Existing players and notes from older versions are wiped — back up the DB before upgrading if you need them.

## Tests

```bash
pytest
```
