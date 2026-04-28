---
date: 2026-04-28
topic: ourlads-sync
---

# Ourlads.com depth-chart & roster sync

## Problem Frame

ffpresnap currently mirrors only the Sleeper API. Sleeper's depth-chart data lags real beat reporting and often disagrees with what Ourlads.com (an independent, hand-curated depth-chart aggregator) publishes the same morning. For a tool whose entire pre-snap value depends on knowing who's *actually* starting, lagging depth charts undercut the user's confidence in everything downstream — depth-chart explorer, watchlist, player-explorer, and notes that reference a player's role.

We want Ourlads as a **second sync source**, capable of overriding stale depth-chart data and introducing players Sleeper hasn't picked up yet (practice squad, recent signings).

## User Flow

```
ffpresnap-sync --source=sleeper        ffpresnap-sync --source=ourlads
        |                                       |
        v                                       v
  Sleeper API JSON                     32 roster pages + 1 all-teams chart
        |                                       |
        v                                       v
   Project & filter             Parse rosters -> upsert; parse chart -> set depth fields
        |                                       |
        +---------------+   +-------------------+
                        v   v
                  players table
                  (single shared, source-tagged)
                        |
                        v
              Existing reads & artifacts
              (no changes required)
```

## Requirements

**Source model**

- R1. `players` becomes a multi-source table. A row may originate from Sleeper, Ourlads, or both, and must survive a sync of the *other* source unchanged.
- R2. Sleeper-only players, Ourlads-only players, and players matched across both sources are all valid states.
- R3. The Sleeper sync's current wholesale-replace semantics must change: it may only delete Sleeper-sourced players that disappeared from the Sleeper payload — never Ourlads-only rows.

**Ingestion**

- R4. A single CLI entry point `ffpresnap-sync` accepts `--source={sleeper|ourlads}` and routes to the matching pipeline. Same shape exposed via an MCP tool (`sync_players(source=...)`).
- R5. The Ourlads pipeline performs **33 fetches per run**: 32 team-roster pages (`https://www.ourlads.com/nfldepthcharts/roster/<TEAM>`) followed by one all-teams depth-chart page (URL TBD — see Outstanding Questions).
- R6. Each Ourlads run records a `sync_runs` row using the same audit semantics as Sleeper, distinguishable by source.
- R7. The Ourlads pipeline runs **on demand only** — no built-in scheduler. README guidance recommends invoking it after the Sleeper sync if both are scheduled in cron.

**Identity matching**

- R8. When parsing an Ourlads page, prefer Ourlads' own per-player id (e.g. from a profile link in the HTML) as the stable Ourlads identifier.
- R9a. *(Roster parsing)* For each Ourlads player on a roster page, attempt to match an existing Sleeper-sourced row by **normalized full name + team + position**. On match, update the existing row in place (no duplicate created).
- R9b. *(Depth-chart parsing)* Apply depth-chart values (`depth_chart_position`, `depth_chart_order`) to players already in the table by name+team+position lookup. No new identity matching or player creation happens during this phase.
- R10. On no match, insert a new player row identified by the Ourlads id (storage form is a planning detail; what matters is the row exists, persists across Sleeper syncs, and is reachable by every existing read path).
- R11. Identity-match failures (ambiguous names, missing Ourlads id) are logged but do not abort the run; the row is inserted as Ourlads-only.

**Conflict & absence behavior**

- R12. Conflicts on `depth_chart_position` and `depth_chart_order` resolve **last-write-wins, regardless of source**. Whichever sync ran most recently sets the values. (Documented consequence: cron ordering matters — running Sleeper after Ourlads will revert the depth chart.)
- R13. Depth-chart fields are populated only during depth-chart-page parsing (R9b). (a) Players extracted from roster pages but not on the depth chart are upserted without depth-chart field values — those fields remain at their existing value (null if never set). (b) Players already in the table but absent from the current depth chart are left completely unchanged — no re-setting of any field.
- R14. Each source writes only the fields it can extract from its own payload — neither source nulls a field it didn't observe.

**Reads & artifacts**

- R15. Existing MCP tools (`get_depth_chart`, `get_player`, `list_players`, `find_player`, etc.) continue to work without callsite changes. They read from the unified `players` table; source is not part of their contract.

## Success Criteria

- After running `ffpresnap-sync --source=ourlads`, depth charts surfaced in artifacts (depth-chart-explorer, team-explorer, player-explorer) reflect Ourlads' rankings for every team.
- A subsequent `ffpresnap-sync --source=sleeper` does **not** delete any Ourlads-only player and does not error on identity overlap.
- A player rostered on Ourlads but not in Sleeper (e.g. a practice-squad call-up Ourlads catches first) is reachable via `find_player` and `get_player` after an Ourlads sync.
- Notes attached to a Sleeper-sourced player remain attached after that player is also matched by an Ourlads run.

## Scope Boundaries

- **Not** introducing real-time / push-based sync. Manual or cron-triggered only.
- **Not** building an alias / manual-correction UI for identity-match failures. Failures are logged; correcting them by hand can be a follow-up if it actually hurts.
- **Not** reconciling bio fields (height, weight, age, college) from Ourlads. Whatever Ourlads exposes there is opportunistic; Sleeper remains the canonical bio source for matched players.
- **Not** changing the prompt-library or artifact prompts. They keep reading the same fields.
- **Not** building a third source or a generalized source plugin system. Just Sleeper + Ourlads, as concrete pipelines.

## Key Decisions

- **Last-write-wins on depth-chart fields**: chosen for simplicity over "Ourlads owns depth-chart fields exclusively." Cron ordering becomes a documented dependency rather than a system-enforced invariant.
- **Leave-alone on absence**: Ourlads only writes for players it actively lists. Falling off the chart does not silently null fields.
- **One CLI, `--source` routes**: the existing `ffpresnap-sync` becomes the single ops surface; pipelines stay decoupled internally.
- **33 fetches per Ourlads run**: 32 team rosters + 1 all-teams chart. Cheaper than 64 per-team chart fetches and uses the page Ourlads already aggregates.

## Dependencies / Assumptions

- **[Unverified]** Ourlads' HTML exposes a stable per-player profile id we can extract (e.g. a `/players/<id>` link). If it doesn't, R8 falls back to a synthesized id derived from name+team+position+jersey, and we accept higher rename collisions.
- **[Unverified]** Ourlads has an "all teams" depth-chart page in addition to the per-team one. The user described pulling depth chart in a single fetch — exact URL needs verification before planning.
- Ourlads' Terms of Service and rate limits permit automated, attributed scraping at the proposed cadence (1 run, 33 fetches). Worth a quick read during planning; not blocking the brainstorm.

## Outstanding Questions

### Resolve Before Planning

- *(none — user opted to proceed; the all-teams URL is now a planning-time research item, see below.)*

### Deferred to Planning

- [Affects R5][Needs research] Confirm the all-teams depth-chart page URL exists and parse a sample to identify its row structure (team, name, position, depth rank). If it does not exist, R5 falls back to 64 fetches (per-team chart in addition to per-team roster) and politeness budget adjusts accordingly.
- [Affects R1, R3][Technical] How is "source" tracked on the `players` table? Candidates: a `source` column, an id-prefix convention (`ourlads:<id>`), a join table. **Constraint:** the choice must preserve note FKs across identity merges — option chosen must allow updating an existing row's metadata without changing its primary key.
- [Affects R8, R10][Needs research] Does Ourlads' HTML actually expose a stable per-player id? Inspect a real page during planning. If not, define the synthesized fallback id format.
- [Affects R7][Technical] Rate-limiting / politeness for the per-run fetch budget (delay between requests, User-Agent, gzip, retries). Borrow conventions from `sleeper.py` where appropriate.
- [Affects R11][Technical] Logging surface for identity-match failures — stderr, a dedicated table, or a field on `sync_runs`?
- [Affects R6][Technical] Whether `sync_runs` gets a `source` column (v7 migration) or source is encoded into existing `source_url`. Depends on whether per-source `last_sync()` lookups become a real read path.

### Open Design Questions Surfaced by Document Review

The document review surfaced design tensions that should be revisited in planning. They are not blockers, but planning should make explicit decisions on each:

- **R12 conflict resolution.** Last-write-wins on depth-chart fields produces a known footgun: running Sleeper after Ourlads silently reverts depth charts (the exact problem this feature exists to solve). Consider per-field source ownership instead — Ourlads owns `depth_chart_position` / `_order` on rows it touches; Sleeper sync skips writing those fields.
- **R13 zombies.** "Leave fields alone" guarantees retired/cut/IR players keep their last depth-chart values forever. Consider tracking `depth_chart_last_observed_at` and clearing/marking-stale when a team's chart syncs successfully but the player isn't on it.
- **Identity collisions.** R9a's name+team+position match is undefined when multiple existing rows match. Define normalization explicitly (NFKD strip diacritics, drop Jr/Sr/II/III, collapse whitespace, lowercase). On >1 match, treat as ambiguous per R11; do not auto-merge.
- **Mid-week trades.** Same-name-different-team breaks R9a and creates phantom duplicates. Once an Ourlads-id ↔ Sleeper-id binding is established on first match, persist it so subsequent runs survive trades.
- **Parse-failure / partial-page.** A torn page returning N rows instead of expected silently loses players. Add a per-page row-count band; below threshold, mark that team's slice failed and apply no writes from it. Run-level: if K teams fail, abort and roll back.
- **Notes-survival mechanism.** Success criterion 4 requires that a row's primary key is stable across source matches. The id-strategy decision (above) must respect this constraint — discard candidates that change PKs on match.
- **Paste-driven alternative.** A `set_depth_chart(team, paste_text)` MCP tool was raised as an 80/20 alternative to scraping. Planning should briefly weigh and rule on it before committing to scraping infrastructure.

## Next Steps

→ Resume `/ce:brainstorm` to confirm the all-teams depth-chart URL, then `/ce:plan`.
