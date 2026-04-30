# MCP tools reference

ffpresnap exposes 24 tools to Claude. You don't need to call these directly — Claude picks the right one when you describe what you want — but it's useful to know what's available.

## Sync

- `sync(source, full?)` — pull data from a source and merge it locally. `source` is one of:
  - `"sleeper"` — player data, ~5s, runs synchronously and returns the full summary.
  - `"ourlads"` — depth charts, ~1-3 min, runs in a background thread and returns `{run_id, status: "running", source}` immediately.
  - any registered feed source (e.g. `"32beatwriters"`) — paginated beat-reporter content, runs in a background thread, returns a `run_id`.

  Feed sources also accept `full: true` to walk the entire feed (first-run backfill or reconciliation against API drift). The default is incremental: stop at the first page where every item is already known.

  Sleeper sync overwrites every Sleeper-sourced row. Ourlads sync merges into existing rows by name+team+position and binds `ourlads_id` for stability across runs. Feed syncs store raw items in `feed_items` and auto-create one note per matched item.
- `last_sync(source?)` — show the most recent sync run. Optional `source` restricts to runs of that source.
- `get_sync_status(run_id)` — read a `sync_runs` row by id. Use after starting a background sync to poll for completion; the row's `status` becomes `"success"` or `"error"` when the background run finishes. Feed-sync rows include `items_fetched`, `items_new`, `items_matched`, `items_unmatched`. Per-team Ourlads failures (parse, sanity, network) land in the `error` column.

## Feeds (read & maintenance)

- `list_feed_items(player_id?, source?, since?, matched?, limit?)` — list raw feed items. Filters AND-combine: `player_id` restricts to items attached to one player, `source` to one feed (e.g. `"32beatwriters"`), `since` is an ISO lower bound on `created_at`, `matched: true|false` filters by whether the item is linked to a player. `limit` defaults to 50.
- `rematch_feed_items(window_days?)` — retry identity matching for unmatched items ingested in the last N days (default 30). Useful after a Sleeper or Ourlads sync brings in players that were previously unknown — the same pass runs at the tail of every sync, but you can also trigger it manually. Returns `{checked, matched, notes_written}`.
- `delete_auto_notes_from_run(run_id)` — bulk-rollback for a misfiring feed sync. Deletes every auto-note whose backing `feed_items` row was first-attached during the given sync run; leaves the raw `feed_items` rows alive (their `note_id` becomes NULL). Idempotent — calling twice returns 0 the second time. Non-feed run IDs are no-ops. Returns `{deleted_notes: N}`.

> **Note on idempotency:** auto-notes deleted via the regular `delete_note` tool (or via `delete_auto_notes_from_run`) are *not* recreated on the next sync — the `feed_items` row already exists, so the sync short-circuits. This is intentional: it means manually deleted notes stay deleted across daily syncs.

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
