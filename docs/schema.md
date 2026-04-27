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

Fantasy-relevant players (QB, RB, WR, TE, K, DEF) mirrored from the [Sleeper API](https://docs.sleeper.com/). Refreshed by `ffpresnap-sync` or the `sync_players` MCP tool. Sleeper-sourced fields are overwritten on every sync; **`watchlist` is preserved across syncs.**

| Column | Type | Notes |
|---|---|---|
| `player_id` | TEXT | Primary key. Sleeper's `player_id`. |
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

Indexes: `team`, `position`, `full_name COLLATE NOCASE`.

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

Audit log of every `ffpresnap-sync` invocation.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, autoincrement. |
| `started_at` | TEXT | ISO timestamp. |
| `finished_at` | TEXT | Null while in flight. |
| `players_written` | INTEGER | Final count on success. |
| `source_url` | TEXT | Sleeper endpoint that was fetched. |
| `status` | TEXT | `running` \| `success` \| `error`. |
| `error` | TEXT | Error message on failure. |

Surfaced via the `last_sync` MCP tool.

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
