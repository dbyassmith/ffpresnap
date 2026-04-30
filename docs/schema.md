# Database schema

ffpresnap stores everything in a single SQLite file at `~/.ffpresnap/notes.db` (override with `FFPRESNAP_DB`). The schema is migrated forward in place when the MCP server opens the file — your existing data is preserved across upgrades.

This page documents the current schema. The authoritative source is the `SCHEMA_V2` block in [`src/ffpresnap/db.py`](../src/ffpresnap/db.py).

## Tables

### `meta`

Key/value metadata used internally (e.g. schema version).

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT | Primary key. |
| `value` | TEXT | Not null. |

### `teams`

The 32 NFL teams. Seeded on first open and not mutated by sync.

| Column | Type | Notes |
|---|---|---|
| `abbr` | TEXT | Primary key (e.g. `KC`). |
| `full_name` | TEXT | Not null (e.g. `Kansas City Chiefs`). |
| `conference` | TEXT | Not null (`AFC` / `NFC`). |
| `division` | TEXT | Not null (`North` / `South` / `East` / `West`). |

### `players`

Fantasy-relevant players (QB, RB, WR, TE, K, DEF) mirrored from one or more sources. **Multi-source as of schema v7:** rows can originate from Sleeper, Ourlads, or both. Refreshed by `ffpresnap-sync --source=<name>` or the `sync_players(source=...)` MCP tool. Sleeper-sourced fields are overwritten on every Sleeper sync; **`watchlist`, `source`, `ourlads_id`, and `depth_chart_last_observed_at` are preserved across Sleeper syncs.** The `source` column distinguishes ownership.

| Column | Type | Notes |
|---|---|---|
| `player_id` | TEXT | Primary key. Stable across the row's lifetime. Sleeper's `player_id` for Sleeper-originated rows; `ourlads:<id>` or `<TEAM>:<jersey>:<name>` for Ourlads-only rows. |
| `full_name` | TEXT | |
| `first_name` | TEXT | |
| `last_name` | TEXT | |
| `team` | TEXT | Team abbreviation. Indexed. |
| `position` | TEXT | Indexed. |
| `fantasy_positions` | TEXT | JSON-encoded list. |
| `number` | INTEGER | Jersey number. |
| `depth_chart_position` | TEXT | e.g. `QB`, `LWR`, `RWR`, `SWR`, `RB`, `TE`, `K`, `DEF`. |
| `depth_chart_order` | INTEGER | 1 = starter; nulls for unranked. |
| `status` | TEXT | e.g. `Active`, `Inactive`. |
| `injury_status` | TEXT | e.g. `Questionable`, `Out`, `IR`. |
| `injury_body_part` | TEXT | |
| `injury_notes` | TEXT | |
| `practice_participation` | TEXT | |
| `age` | INTEGER | |
| `birth_date` | TEXT | ISO date. |
| `height` | TEXT | |
| `weight` | TEXT | |
| `years_exp` | INTEGER | |
| `college` | TEXT | |
| `espn_id` | TEXT | Cross-platform id. |
| `yahoo_id` | TEXT | Cross-platform id. |
| `rotowire_id` | TEXT | Cross-platform id. |
| `sportradar_id` | TEXT | Cross-platform id. |
| `updated_at` | TEXT | ISO timestamp set on each sync. |
| `watchlist` | INTEGER | 0/1 boolean. **User-owned, preserved across syncs.** |
| `source` | TEXT | NOT NULL. `'sleeper'` (Sleeper-only), `'ourlads'` (Ourlads-only — Sleeper hasn't picked them up), or `'merged'` (matched in both). Drives source-scoped DELETE on Sleeper sync and per-field ownership of `depth_chart_position` / `_order` (Ourlads owns these on rows it touched). |
| `ourlads_id` | TEXT | Ourlads' internal player profile id, captured from the page. Set on identity merge so subsequent Ourlads runs find the row even if the player is traded. Null for Sleeper-only rows. |
| `depth_chart_last_observed_at` | TEXT | ISO timestamp of the last Ourlads run that observed this player on the depth chart. Drives R13: when Ourlads' team chart was successfully synced and a player isn't on it, their depth fields clear and `'merged'` rows demote to `'sleeper'`. |

Indexes: `team`, `position`, `full_name COLLATE NOCASE`.

#### Source semantics

- **Sleeper sync** (`source='sleeper'`): wholesale replace of Sleeper-sourced rows. Rows where `source IN ('ourlads','merged')` survive. Sleeper does **not** overwrite `depth_chart_position` / `_order` on `'merged'` rows — Ourlads owns those (per-field ownership).
- **Ourlads sync** (`source='ourlads'`): identity-matches incoming rows to existing Sleeper rows by name+team+position (or by previously-bound `ourlads_id`). On match, the existing row updates in place and bumps `source` to `'merged'`. Practice-squad-promotion is bidirectional: a Sleeper sync that picks up a player Ourlads already had transfers notes/mentions and deletes the Ourlads-only row in one transaction.
- **Notes survive** identity merges because `player_id` is immutable post-insert.

### `studies`

Named research containers (e.g. *"WR3s on cheap teams"*). Notes can attach to a study via `subject_type='study'`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, autoincrement. |
| `title` | TEXT | Not null. |
| `description` | TEXT | Optional. |
| `status` | TEXT | `open` or `archived`. |
| `created_at` | TEXT | ISO timestamp. |
| `updated_at` | TEXT | ISO timestamp. |

Indexes: `(status, updated_at DESC)`.

Deleting a study cascades to its notes.

### `notes`

A single notes table covers all three subject types. The pair `(subject_type, subject_id)` identifies what the note is *about*; `subject_id` is the player_id, team abbreviation, or study id (as text) depending on type.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, autoincrement. |
| `subject_type` | TEXT | `player` \| `team` \| `study` (CHECK constraint). |
| `subject_id` | TEXT | player_id, team abbr, or stringified study id. |
| `body` | TEXT | The note text. |
| `created_at` | TEXT | ISO timestamp. |
| `updated_at` | TEXT | ISO timestamp. |

Indexes: `(subject_type, subject_id, created_at DESC)`.

Notes do **not** carry a foreign key on `subject_id` directly (since the column is polymorphic). Cascade deletion is enforced application-side: deleting a player or study deletes its notes; team rows are never deleted.

### `note_player_mentions`

Many-to-many tagging of players inside any note. `add_note(... mentions: { player_ids: [...] })` writes into this table.

| Column | Type | Notes |
|---|---|---|
| `note_id` | INTEGER | FK → `notes.id`, ON DELETE CASCADE. |
| `player_id` | TEXT | FK → `players.player_id`, ON DELETE CASCADE. |
| | | Primary key: `(note_id, player_id)`. |

Index: `player_id`.

When a player is removed from Sleeper's roster, both their `players` row and any mentions of them are cascaded out.

### `note_team_mentions`

Many-to-many tagging of teams inside any note.

| Column | Type | Notes |
|---|---|---|
| `note_id` | INTEGER | FK → `notes.id`, ON DELETE CASCADE. |
| `team_abbr` | TEXT | FK → `teams.abbr` (no cascade — teams are static). |
| | | Primary key: `(note_id, team_abbr)`. |

Index: `team_abbr`.

### `sync_runs`

Audit log of every `ffpresnap-sync` invocation. Also acts as an advisory concurrency lock — a row with `status='running'` and `started_at` within the last 5 minutes blocks new sync starts (`ConcurrentSyncError`).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, autoincrement. |
| `started_at` | TEXT | ISO timestamp. |
| `finished_at` | TEXT | Null while in flight. |
| `players_written` | INTEGER | Final count on success. |
| `source_url` | TEXT | Endpoint that was fetched (Sleeper API URL or Ourlads chart URL). |
| `status` | TEXT | `running` \| `success` \| `error`. |
| `error` | TEXT | Error message on failure. For partial Ourlads runs, also carries a comma-separated per-team error list. |
| `source` | TEXT | NOT NULL. `'sleeper'`, `'ourlads'`, or a feed source name (e.g. `'32beatwriters'`). |
| `items_fetched` | INTEGER | Feed-sync only. Total items pulled from the adapter for this run. NULL on player-data syncs. |
| `items_new` | INTEGER | Feed-sync only. Items inserted for the first time (idempotent re-syncs report 0 here). |
| `items_matched` | INTEGER | Feed-sync only. New items whose external player matched a row in `players` and got an auto-note. |
| `items_unmatched` | INTEGER | Feed-sync only. New items with no `players` match (stored anyway; back-matched on a later sync). |

Surfaced via the `last_sync(source?)` and `get_sync_status(run_id)` MCP tools.

### `feed_sources`

Catalog of registered feed adapters. Seeded on every open from the in-code list in `Database._seed_feed_sources` — adding a new feed = registering an adapter (Unit 2) plus seeding its row here.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, autoincrement. |
| `name` | TEXT | NOT NULL UNIQUE (e.g. `32beatwriters`). |
| `source_url` | TEXT | NOT NULL. Canonical homepage; stamped into `sync_runs.source_url` on each run. |

### `feed_items`

Raw items pulled from a feed adapter. Each row is a single nugget / post / insight tagged to one external player. When a row's player can be identity-matched to a `players` row at sync time, the orchestrator atomically writes a corresponding `notes` entry and stamps both `note_id` and `note_run_id` on the row (see `add_feed_item_with_auto_note` in `db.py`).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, autoincrement. |
| `source_id` | INTEGER | FK → `feed_sources.id`, ON DELETE CASCADE. |
| `external_id` | TEXT | NOT NULL. Adapter-stable id (e.g. `32bw:2769`). Together with `source_id` forms the idempotency key. |
| `external_player_id` | TEXT | Adapter's own player id, captured for traceability. |
| `external_player_name` | TEXT | NOT NULL. As provided by the adapter (e.g. `Justin Jefferson`). |
| `external_team` | TEXT | Adapter-native team label (e.g. `Minnesota Vikings`). |
| `external_position` | TEXT | E.g. `WR`. |
| `team_abbr` | TEXT | The adapter-translated NFL abbr (e.g. `MIN`). Set at insert; lets the back-match pass run as pure SQL without calling back into adapter code. |
| `source_url` | TEXT | Original article/source URL. |
| `source_author` | TEXT | Reporter or byline. |
| `raw_html` | TEXT | Original HTML body, stored verbatim for re-rendering. |
| `cleaned_text` | TEXT | NOT NULL. HTML-stripped body used as the auto-note body. |
| `created_at` | TEXT | NOT NULL. ISO timestamp from the adapter (the publication time). |
| `ingested_at` | TEXT | NOT NULL. ISO timestamp set when this row was inserted. |
| `player_id` | TEXT | FK → `players.player_id`, ON DELETE SET NULL. Null when the item is unmatched (rookie/prospect not yet in `players`). |
| `note_id` | INTEGER | FK → `notes.id`, ON DELETE SET NULL. Null when no auto-note has been written. |
| `note_run_id` | INTEGER | The `sync_runs.id` that wrote the auto-note; used by `delete_auto_notes_from_run` for bulk rollback of a misfiring sync. |

Unique: `(source_id, external_id)` — re-running a sync of an already-seen item is a no-op (no duplicate row, no duplicate note).

Indexes: `player_id`, `(source_id, created_at DESC)`, partial index on `ingested_at WHERE player_id IS NULL` (drives the back-match query).

#### Cascade semantics

The two delete directions are intentionally asymmetric (see `docs/plans/2026-04-29-001-feat-feed-ingestion-32beatwriters-plan.md`):

- **Deleting a `notes` row** (e.g. via `delete_note`) leaves its `feed_items` row alive with `note_id` cleared to NULL (FK ON DELETE SET NULL fires). Re-running the sync does **not** restore the deleted note — `feed_items` row already exists, idempotent short-circuit.
- **Deleting a `feed_items` row** must go through `Database.delete_feed_item(id)` which deletes both the row and any linked auto-note in one transaction. SQLite cannot enforce this with FKs alone (no link from `notes` back to `feed_items`), so application code is the source of truth here.

### `prompts`

The dashboard prompt library. **Repo is the source of truth** — `src/ffpresnap/prompts/*.md` is reconciled into this table on every DB open. Local edits to this table are overwritten on the next open.

| Column | Type | Notes |
|---|---|---|
| `slug` | TEXT | Primary key. Matches the markdown filename. |
| `title` | TEXT | Not null. From frontmatter. |
| `description` | TEXT | Not null. From frontmatter. |
| `body` | TEXT | Not null. The markdown body (everything after the frontmatter). |
| `updated_at` | TEXT | ISO timestamp set during reconciliation. |

To add a prompt, drop a new `.md` file in `src/ffpresnap/prompts/` with `slug`, `title`, and `description` frontmatter — it'll appear in `list_prompts` on next open.

## Cascade & integrity rules at a glance

- **Delete a player** (Sleeper-driven) → their notes and mentions cascade out.
- **Delete a study** → its notes (and their mentions) cascade out.
- **Delete a team** → never happens; teams are static.
- **Delete a note** → its `note_player_mentions` and `note_team_mentions` rows cascade out.
- **`add_note` / `update_note`** validates every mention at write time. An unknown player_id or unresolvable team rejects the entire write.

## Backup & reset

- **Back up:** copy `~/.ffpresnap/notes.db`.
- **Start over:** delete the file. The next `Database.open()` recreates the schema, re-seeds teams, and re-loads the prompt library. You'll need to run `ffpresnap-sync` again to populate players.
