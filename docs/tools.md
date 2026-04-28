# MCP tools reference

ffpresnap exposes 20 tools to Claude. You don't need to call these directly — Claude picks the right one when you describe what you want — but it's useful to know what's available.

## Sync

- `sync_players(source?)` — pull current player data from a source. `source` is `"sleeper"` (default, ~5s, returns full summary synchronously) or `"ourlads"` (~1-3 min, runs in a background thread and returns a `run_id` immediately). Sleeper sync overwrites every Sleeper-sourced row; Ourlads sync merges into existing rows by name+team+position and binds `ourlads_id` for stability across runs.
- `last_sync(source?)` — show the most recent sync run. Optional `source` restricts to runs of that source (`"sleeper"` or `"ourlads"`).
- `get_sync_status(run_id)` — read a `sync_runs` row by id. Use after starting an Ourlads sync to poll for completion; the row's `status` becomes `"success"` or `"error"` when the background run finishes. Per-team failures (parse, sanity, network) land in the `error` column.

## Browse

- `list_teams(query?)` — list NFL teams; filter by abbreviation, name, conference, or division.
- `get_team(team)` — team record plus its notes and any notes elsewhere that mention it. `team` accepts `"KC"`, `"Kansas City Chiefs"`, or `"Chiefs"`.
- `get_depth_chart(team)` — depth chart grouped by position; unranked players land in a trailing `Unranked` group.
- `find_player(query)` — case-insensitive name substring search (max 10).
- `get_player(player_id)` — full player detail plus two lists: `notes` (about this player) and `mentions` (notes elsewhere that tag this player).
- `list_players(team?, position?, watchlist?)` — flat listing with optional filters. Pass `watchlist: true` to show only watchlisted players.
- `update_player(player_id, watchlist?)` — toggle a player's watchlist flag. The watchlist is preserved across Sleeper syncs.

## Studies (research containers)

- `create_study(title, description?)` — start a new research thread (defaults to `open`).
- `list_studies(status?)` — defaults to open; pass `"archived"` or `"all"`.
- `get_study(study_id)` — study record plus its notes.
- `update_study(study_id, title?, description?)` — partial update.
- `set_study_status(study_id, status)` — `"open"` or `"archived"`.
- `delete_study(study_id)` — deletes the study and all of its notes (cascades).

## Notes (unified)

- `add_note(target_type, target_id, body, mentions?)` — `target_type` is `"player"`, `"team"`, or `"study"`. `target_id` is the player_id (string), team identifier (abbr / name / nickname), or study id (as a string).
- `list_notes(scope, target_id?, limit?)` — `scope` is `"player"`, `"team"`, `"study"`, or `"recent"`. The first three return primary-subject notes for the given `target_id`; `"recent"` returns a chronological feed across all subjects (default 50, max 200), with each entry carrying a `subject` block.
- `update_note(note_id, body, mentions?)` — replace a note's body. If `mentions` is provided, the stored mention set is replaced wholesale; omit to leave mentions unchanged.
- `delete_note(note_id)` — delete a note.

`add_note` and `update_note` accept an optional `mentions: { player_ids: [...], team_abbrs: [...] }`. Mentions are validated at write time — an unknown player or unresolvable team rejects the whole write.

## Prompt library

- `list_prompts()` — return the curated catalog of dashboard prompts. Prompts ship with the package and are reconciled into the local DB on every open (repo is source of truth — local edits get overwritten).
