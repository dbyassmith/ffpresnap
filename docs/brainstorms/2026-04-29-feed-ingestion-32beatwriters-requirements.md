---
date: 2026-04-29
topic: feed-ingestion-32beatwriters
---

# Feed Ingestion (first source: 32beatwriters)

> Plan-mode note: this is the brainstorm requirements artifact. After approval, copy to `docs/brainstorms/2026-04-29-feed-ingestion-32beatwriters-requirements.md` and run `/ce:plan` against it.

## Context

ffpresnap currently mirrors structured *player* data from two sources (Sleeper, Ourlads) into the `players` table. The user discovered the [32beatwriters](https://api.32beatwriters.com/api/nuggets) API, which exposes a different shape: a paginated feed of HTML "nuggets" (beat-reporter insights), each tagged to exactly one player, with source author + URL + timestamp. Rather than wire 32beatwriters in as a one-off, the user wants a **generic feed-ingestion layer** so future publications (Athletic, Rotoworld, individual reporter feeds, etc.) can be plugged in the same way. Each synced item should automatically become a `note` on the matched player so it shows up in the existing notes/mentions UX without a manual promotion step.

## Problem Frame

- **Who** — solo fantasy researcher (the user) running ffpresnap locally.
- **What's changing** — adds a third class of sync ("feeds") alongside the existing player-data syncs, plus an adapter for 32beatwriters as the first concrete feed.
- **Why it matters** — beat-reporter nuggets are the highest-signal, most time-sensitive fantasy content. Today they live behind paywalls and scattered apps; pulling them locally and tagging them to players makes them queryable through Claude alongside notes, depth charts, and studies. Building generically means source #2 is a new adapter, not a refactor.

## High-Level Flow

```
                ┌──────────────────────────┐
                │  ffpresnap-sync          │
                │  --source=32beatwriters  │
                └─────────────┬────────────┘
                              │
                              ▼
                  ┌──────────────────────┐
                  │ feed adapter         │
                  │ (32beatwriters.py)   │
                  │ paginate + parse     │
                  └─────────┬────────────┘
                            │ FeedItem records
                            ▼
              ┌───────────────────────────────┐
              │ feed-ingestion core           │
              │  • upsert into feed_items     │
              │  • match player (name+team)   │
              │  • for each NEW item:         │
              │      write notes row          │
              │      + note_player_mentions   │
              └─────────────┬─────────────────┘
                            │
                            ▼
                  existing notes UX
                  (Claude prompts, search,
                   mention graph, watchlist)
```

## Requirements

**Generic feed layer**
- R1. Introduce two new tables: `feed_sources` (catalog of sources, e.g. `32beatwriters`) and `feed_items` (raw items keyed by `(source_id, external_id)` with cleaned text body, original HTML, source URL, author, external player ref, ingested-at, and optional matched `player_id`).
- R2. A feed adapter is a Python module exposing a small interface (e.g. `fetch(since=...) -> Iterable[FeedItem]`). Adding a new source means adding a new adapter file + registering it; no schema changes required.
- R3. Sync is dispatched through the existing `ffpresnap-sync --source=<name>` and `sync_players(source=...)` MCP tool entry points. When `<name>` resolves to a feed adapter, the feed pipeline runs instead of the player-data pipeline. (Consider whether the MCP tool needs a new alias like `sync_feed` for clarity — flagged for planning.)
- R4. Sync runs are tracked through the existing `sync_runs` / `get_sync_status` background-job pattern, including counts (items fetched, new, matched, skipped-unmatched).

**Identity matching (match-or-skip)**
- R5. For each incoming feed item, attempt to match the external player to an existing `players` row by `(name, team, position)` using the same normalization Ourlads uses. On match, set `feed_items.player_id`. On miss, leave `player_id` NULL — do **not** create new `players` rows from feeds.
- R6. Unmatched items are still stored (so they're visible/searchable and re-matchable when Sleeper or Ourlads later picks the player up); they just don't auto-create notes.
- R7. Provide a simple way to retroactively re-match unmatched items after a player-data sync (could be a step at end of every sync; details deferred to planning).

**Item → note conversion**
- R8. For every **new** matched feed item, automatically create one `notes` row with `subject_type='player'`, `subject_id=<matched player_id>`, body containing cleaned plain-text content (HTML stripped) + a footer line with `source author · source url · createdAt`. Insert a corresponding `note_player_mentions` row for the matched player.
- R9. Auto-creation is idempotent: re-running a sync that returns previously-seen items must not duplicate notes. (Enforced via `feed_items` unique key + a `note_id` backref column on `feed_items`.)
- R10. Auto-created notes are normal notes — user can edit, delete, or add mentions to them. Deleting an auto-note does not delete its `feed_items` row (raw remains); deleting the `feed_items` row cascades to the auto-note.

**Sync semantics**
- R11. Default sync mode is **incremental**: walk pages from newest until the first page where every item already exists in `feed_items`, then stop. Bound by a max-pages cap (e.g. 20) to avoid runaway cost on a never-before-synced DB; full backfill is opt-in via a `--full` flag.
- R12. Network politeness: same posture as Ourlads (a small per-request delay, configurable; reasonable default like 0.5–1.0s). Concrete value deferred to planning.
- R13. 32beatwriters API auth posture: today the endpoint returns data unauthenticated. If that changes, support an env var (e.g. `FFPRESNAP_32BEATWRITERS_TOKEN`) sent as a bearer header. **Unverified assumption** — needs to be re-checked at implementation time.

**MCP / surface area**
- R14. Existing MCP tools (`get_player_notes`, note search, mention graph, etc.) must work unchanged on auto-created notes — i.e. no new tool surface is *required* for read paths.
- R15. Add at minimum one new MCP read tool to expose the raw feed: e.g. `list_feed_items(player_id=..., source=..., since=..., matched=...)` so Claude can answer "show me everything 32beatwriters wrote about X this week" without going through notes.

## Success Criteria

- Running `ffpresnap-sync --source=32beatwriters` on a fresh DB pulls items, matches them to existing fantasy players, and produces searchable notes that show up in the existing depth-chart/player-explorer prompts.
- Re-running the sync the next day pulls only new items, creates exactly the new corresponding notes, and produces zero duplicates.
- Asking Claude *"What did beat writers say about Patrick Mahomes this week?"* returns the actual nuggets (via notes or `list_feed_items`) with attribution.
- A second, hypothetical feed source can be added by writing one adapter file (no schema migration, no changes to the dispatcher).

## Scope Boundaries

- **Not** building a UI for triaging/promoting items — auto-note on sync is the workflow.
- **Not** creating new `players` rows from feeds (match-or-skip).
- **Not** generating LLM roll-up summaries in this iteration — per-item notes only. (Roll-ups can be a follow-up; they're a Claude prompt away once items are stored.)
- **Not** rewriting Sleeper/Ourlads sync paths — feeds are an additive layer.
- **No** cross-source deduplication of similar nuggets in this iteration.

## Key Decisions

- **Generic feed layer from day one** — user wants future sources to drop in as adapters, not as one-off integrations. Cost is one extra table and a small interface; payoff is no future refactor.
- **Match-or-skip on identity** — keeps the `players` table fantasy-positions-only and avoids prospect/college-name pollution. Items for unmatched players are still preserved so they can be back-matched later.
- **Auto-create notes on sync** — chosen over a manual promotion step so insights are immediately searchable in the existing notes/mentions UX. Auto-notes are normal notes (editable/deletable).
- **Reuse existing sync entry points** — `ffpresnap-sync --source=...` already dispatches by source; feeds slot into the same dispatcher rather than introducing a parallel CLI.

## Dependencies / Assumptions

- 32beatwriters API is unauthenticated today (verified via live curl on 2026-04-29). May change — see R13.
- The existing `_naming.py` normalization used by Ourlads identity-matching is reusable for feed→player matching. (Confirmed `_naming.py` exists; reuse strategy verified at the file level. Exact match-rate on real beat-writer data is an empirical question for planning.)
- HTML in nugget `content` is light (`<p>`, `<br>`); a stdlib-level strip is sufficient — no need for a heavy parser.

## Outstanding Questions

### Resolve Before Planning
- (none — product shape is settled)

### Deferred to Planning
- [Affects R3][Technical] Should the MCP tool stay as `sync_players(source=...)` for feeds, or should there be a separate `sync_feed(source=...)` for clarity? Naming/UX call.
- [Affects R5][Needs research] What % of 32beatwriters items match cleanly against the existing Sleeper/Ourlads `players` table on the user's current DB? If the rate is low, the matcher may need a `last_name + team` fallback or a manual alias table.
- [Affects R7][Technical] Where to hook re-matching of previously-unmatched items — at end of every Sleeper sync, every feed sync, both, or as a separate `rematch_feed_items` tool?
- [Affects R8][Technical] Note body format — should the source/author/url footer be plain text, a markdown link, or stored in dedicated columns on `notes` (would require a schema change)?
- [Affects R11][Technical] Concrete page-cap and politeness-delay defaults; whether to expose them as CLI flags or env vars.
- [Affects R13][Needs research] Confirm at implementation time that the 32beatwriters endpoint still works unauthenticated, and whether per-request rate limits exist.

## Next Steps

→ `/ce:plan` for structured implementation planning (after copying this doc to `docs/brainstorms/2026-04-29-feed-ingestion-32beatwriters-requirements.md`).
