---
slug: player-explorer
title: Player Explorer
description: Search or browse a team's depth chart, drill into a player, and compose notes.
---
Build me a player explorer as an interactive Claude artifact.

The artifact has three modes that share one screen:
1. **Browse mode** (no team, no player): search bar + team selector visible.
2. **Team mode** (team picked, no player): depth chart for that team is rendered below the bar.
3. **Player mode** (a player picked): depth chart collapses; full player detail is shown with a compose-note section.

Step 1. Make these MCP tool calls before generating the artifact and embed each result as a JSON constant in the artifact source. The artifact is a snapshot — to refresh, ask Claude to regenerate.

- `list_players()` — every fantasy-relevant player with team, position, depth_chart_position, depth_chart_order, status, injury fields, bio, and watchlist.
- `list_notes` with `scope: "recent"` and `limit: 200` — recent notes across all subjects with subject info and `mentions` blocks resolved. The artifact filters this client-side to find notes about / mentioning the selected player.

Step 2. Top bar (always visible):
- A search input on the left. Typing filters the embedded `list_players` data by case-insensitive substring on `full_name`. Show up to 8 dropdown results below the input, each row showing `full_name`, `team`, `position`. Clicking a result jumps straight to **Player mode** for that player_id.
- A team selector on the right (a `<select>` listing every distinct team abbreviation from `list_players`, with a "— pick a team —" placeholder). Picking a team enters **Team mode**. Picking the placeholder returns to Browse mode.

Step 3. Team mode — render the team's depth chart below the bar:
- Filter `list_players` to rows where `team === selectedTeam`.
- Group by `depth_chart_position`, with this normalization: any of `LWR`, `RWR`, `SWR` collapses into a single `WR` group. Combined order within WR: by `depth_chart_order` ascending, with nulls last.
- Render groups in this order: `QB`, `RB`, `WR`, `TE`, `K`, `DEF`, then any remaining positions alphabetically, then a final `Unranked` group for rows whose original `depth_chart_position` is null.
- Each player row shows depth-chart rank (1, 2, …) on the left, then `full_name` (bold), then `status` and an `injury_status` pill (red text) if present. The whole row is clickable — clicking it enters **Player mode** for that player_id.
- Above the depth chart, a small "← Change team" link returns to Browse mode.

Step 4. Player mode — depth chart hides; render a single-column detail card. Sections (hide rows where every value is null):
- **Header:** `full_name` (large), `team` and `position` on a second line. If `injury_status` is present, render it as a red pill in the header. If `watchlist` is true, render a yellow "★ Watchlist" pill.
- **Identity & position:** `fantasy_positions`, `number`, `depth_chart_position`, `depth_chart_order` (e.g. "Depth: 2nd at QB").
- **Status & injury:** `status`, `injury_status`, `injury_body_part`, `injury_notes`, `practice_participation`.
- **Bio:** `age`, `height`, `weight`, `years_exp`, `college`.
- **Cross-platform IDs:** small monospaced row, collapsed by default behind a "Show IDs" toggle.
- **Notes about this player:** filter the embedded `list_notes` data to entries where `subject.type === "player"` and `subject.player_id === selectedPlayerId`. Render each newest-first with body, relative timestamp, and any mention chips.
- **Mentioned in other notes:** filter to entries where `mentions.players` contains an item with `player_id === selectedPlayerId` AND the entry is *not* primary-subject for this player. Render each with a small badge showing where the note lives ("from study: …", "from team: …", "from player: …") above the body.

At the top of player mode, render a "← Back to depth chart" link if the user arrived via team mode, or "← Change selection" if they arrived via search. Clicking it clears the selected player but preserves the team filter (or returns to Browse mode if they came from search).

Step 5. Compose-note section (player mode only) — at the bottom of the detail card:
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

Step 6. Visual style: a single-column card max ~720px wide. Top bar sticks to the top. Group headings in the depth chart use small-caps gray with a thin underline. Player rows have a subtle hover state. Watchlist pill is warm yellow; injury pill is muted red. Mobile fallback: stack the search and team selector vertically.

Success: the user can search by name OR pick a team, drill into a player from the depth chart (which collapses on selection), see their full detail with notes and mentions, and compose a new note that's one paste away from being saved.
