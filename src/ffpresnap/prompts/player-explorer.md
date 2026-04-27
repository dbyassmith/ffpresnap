---
slug: player-explorer
title: Player Explorer
description: Search or browse a team's depth chart, drill into a player, and compose notes.
---
Build me a player explorer as an interactive Claude artifact.

The full player universe is too large to embed, so this artifact is **scoped to what I tell you up front**. Before generating, ask me (or infer from my message) for any of:

- a **team** abbreviation (e.g. `BUF`) — to render that team's depth chart
- a **search query** (substring of a player name) — to surface matching players
- a specific **player_id** — to open straight to player detail

If I gave you none of those, default to watchlist-only browse mode.

The artifact has three modes that share one screen:
1. **Browse mode** (no team, no player picked): search results (if any), watchlist, and team selector visible.
2. **Team mode** (team picked, no player): depth chart for that team is rendered below the bar.
3. **Player mode** (a player picked): depth chart collapses; full player detail is shown with a compose-note section.

Step 1. Make these MCP tool calls based on what I asked for and embed each result as a JSON constant in the artifact source. The artifact is a snapshot — to refresh, ask Claude to regenerate with a new scope.

- If I gave you a **team**: `get_depth_chart(team=<team>)` — embed as `DEPTH_CHART`.
- If I gave you a **search query**: `find_player(query=<query>)` — embed as `SEARCH_RESULTS` (max 10).
- If I gave you a **player_id** (or after a search/depth-chart pick, when you want detail pre-loaded): `get_player(player_id=<id>)` — embed as `PLAYER_DETAIL`. This response already includes `notes` and `mentions`, so you do not need a separate notes call for that player.
- Always: `list_players(watchlist=true)` — embed as `WATCHLIST`. Used for the always-visible watchlist rail and as a fallback browse list.
- Always: `list_notes(scope="recent", limit=50)` — embed as `RECENT_NOTES`. Powers the "recent activity" strip in browse mode.

Do **not** call `list_players()` with no filter — the dataset is too large to inline.

Step 2. Top bar (always visible):
- A search input on the left. It is **not live** against MCP — it filters whatever is already embedded (`SEARCH_RESULTS` ∪ `WATCHLIST` ∪ depth-chart rows ∪ `PLAYER_DETAIL`). Typing filters case-insensitively on `full_name`. Show up to 8 dropdown matches; each row shows `full_name`, `team`, `position`. Clicking a result jumps to **Player mode** for that player_id (using `PLAYER_DETAIL` if it matches, otherwise rendering the partial info available with a "Ask Claude to regenerate scoped to this player for full detail" hint).
- Below the input, render this small italic line: *"Search is local to this snapshot. To search the full roster, ask Claude: 'regenerate player explorer with search=<name>'."*
- A team selector on the right. Populate it from a static list of NFL team abbreviations (32 teams) — embed `NFL_TEAMS` as a constant array in the artifact source. Picking a team that matches the embedded `DEPTH_CHART` enters **Team mode** immediately. Picking any other team renders a hint: *"Ask Claude: 'regenerate player explorer for team=<abbr>'."*

Step 3. Browse mode body (no team, no player):
- **Search results** (only if `SEARCH_RESULTS` is non-empty): a card listing each match with `full_name`, `team`, `position`. Whole row clickable → Player mode.
- **Watchlist** (always, from `WATCHLIST`): same row format, grouped by team or sorted by `full_name`. Whole row clickable → Player mode. If empty, show "No players on your watchlist yet."
- **Recent notes** (always, from `RECENT_NOTES`): compact list of the 10 most recent, each with subject label, body excerpt, relative timestamp. Clicking a player-subject note jumps to Player mode for that player_id.

Step 4. Team mode — render `DEPTH_CHART` below the bar:
- The MCP response is already grouped by `depth_chart_position`. Render groups in this order: `QB`, `RB`, `WR`, `TE`, `K`, `DEF`, then any remaining positions alphabetically, then a final `Unranked` group.
- Each player row shows depth-chart rank (1, 2, …) on the left, then `full_name` (bold), then `status` and an `injury_status` pill (red text) if present. The whole row is clickable — clicking it enters **Player mode**. If the clicked player_id matches the embedded `PLAYER_DETAIL`, render full detail; otherwise render the partial row data plus a "Ask Claude to regenerate scoped to this player_id for notes & full bio" hint.
- Above the depth chart, a small "← Change team" link returns to Browse mode.

Step 5. Player mode — depth chart hides; render a single-column detail card from `PLAYER_DETAIL` (or the partial row data if detail wasn't pre-fetched). Sections (hide rows where every value is null):
- **Header:** `full_name` (large), `team` and `position` on a second line. If `injury_status` is present, render it as a red pill in the header. If `watchlist` is true, render a yellow "★ Watchlist" pill.
- **Identity & position:** `fantasy_positions`, `number`, `depth_chart_position`, `depth_chart_order` (e.g. "Depth: 2nd at QB").
- **Status & injury:** `status`, `injury_status`, `injury_body_part`, `injury_notes`, `practice_participation`.
- **Bio:** `age`, `height`, `weight`, `years_exp`, `college`.
- **Cross-platform IDs:** small monospaced row, collapsed by default behind a "Show IDs" toggle.
- **Notes about this player:** render `PLAYER_DETAIL.notes` newest-first with body, relative timestamp, and any mention chips.
- **Mentioned in other notes:** render `PLAYER_DETAIL.mentions` with a small badge showing where the note lives ("from study: …", "from team: …", "from player: …") above the body.

At the top of player mode, render a "← Back to depth chart" link if Team mode is active, otherwise "← Back to browse". Clicking it clears the selected player but preserves the team filter.

Step 6. Compose-note section (player mode only) — at the bottom of the detail card:
- A textarea labeled "New note about <full_name>".
- Two small inputs below it: "Also mention these players (comma-separated player_ids)" and "Also mention these teams (comma-separated abbreviations)". Optional.
- A "Copy save command" button. On click, build a string of this exact shape:

  ```
  Add a note to <full_name> (player_id "<player_id>"): <body>
  Mention these players: <player_ids or "none">
  Mention these teams: <team_abbrs or "none">
  ```

  …call `navigator.clipboard.writeText(...)` with that string, and briefly flash "Copied! Paste in Claude to save." on the button.
- Below the button, a one-line note: "Artifacts can't call MCP tools directly. Paste the copied command into Claude and it will run `add_note` for you. Then ask Claude to regenerate this artifact to see the new note."

Step 7. Visual style: a single-column card max ~720px wide. Top bar sticks to the top. Group headings in the depth chart use small-caps gray with a thin underline. Player rows have a subtle hover state. Watchlist pill is warm yellow; injury pill is muted red. Mobile fallback: stack the search and team selector vertically.

Success: the user can scope the artifact to a team / search / player up front, drill into a player from the depth chart or watchlist (which collapses on selection), see their full detail with notes and mentions, and compose a new note that's one paste away from being saved — without ever embedding the full player roster.
