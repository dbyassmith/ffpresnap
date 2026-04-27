---
slug: team-explorer
title: Team Explorer
description: Drill from all NFL teams into one team's depth chart and notes, then into a single player's detail.
---
Build me a team explorer as an interactive Claude artifact with **three nested screens**: Teams → Team → Player.

The full player roster is too large to embed, so this artifact is **scoped up front**. Before generating, ask me (or infer from my message) for any of:

- a specific **team** abbreviation (e.g. `BUF`) — opens straight to the Team screen
- a specific **player_id** — opens straight to the Player screen (with team context pre-loaded)

If I gave you neither, default to the Teams screen.

Step 1. Make these MCP tool calls based on scope and embed each result as a JSON constant in the artifact source. The artifact is a snapshot — to refresh, ask Claude to regenerate with a new scope.

- Always: `list_teams()` — embed as `TEAMS`. Powers the Teams screen.
- If I gave you a **team**: `get_team(team=<team>)` — embed as `TEAM_DETAIL` (includes the team record, `notes`, and `mentions`). Also `get_depth_chart(team=<team>)` — embed as `DEPTH_CHART`.
- If I gave you a **player_id**: `get_player(player_id=<id>)` — embed as `PLAYER_DETAIL` (includes `notes` and `mentions`). If a team wasn't separately specified, also fetch the player's team via `get_team` + `get_depth_chart` so the back-button lands on a populated Team screen.
- Always: `list_notes(scope="recent", limit=50)` — embed as `RECENT_NOTES` for the activity strip on the Teams screen.

Do **not** call `list_players()` with no filter — the dataset is too large to inline.

Step 2. **Screen 1 — Teams** (default landing if no team/player scope):
- Header: "NFL Teams" with a small subtitle showing total count from `TEAMS`.
- A search input that filters `TEAMS` client-side by case-insensitive substring on abbreviation, full name, conference, or division.
- A grouped grid: group teams by **division** (e.g. "AFC East", "AFC North", …). Each card shows abbreviation (large), full name, and conference/division. Whole card clickable → Team screen for that abbr.
- Beneath the grid, a "Recent activity" strip rendering up to 10 entries from `RECENT_NOTES` (subject label, body excerpt, relative timestamp). Clicking a team-subject note jumps to the Team screen for that team; clicking a player-subject note shows a hint *"Ask Claude: 'regenerate team explorer with player_id=<id>'"* (since per-player detail isn't embedded for arbitrary players).
- If a team card is clicked but no `TEAM_DETAIL` / `DEPTH_CHART` is embedded for it, render a small banner on the next screen saying *"Ask Claude to regenerate scoped to team=<abbr> for the depth chart and team notes"* in lieu of empty data.

Step 3. **Screen 2 — Team** (when a team is selected and `TEAM_DETAIL` + `DEPTH_CHART` are embedded):
- Top: "← All teams" link returning to the Teams screen.
- Header: team full name (large), abbreviation, conference/division on a second line.
- Two-column body on wide screens (stack on narrow):
  - **Left — Depth chart** (from `DEPTH_CHART.groups`): the response is already grouped by `depth_chart_position`. Render groups in order `QB`, `RB`, `WR`, `TE`, `K`, `DEF`, then any remaining positions alphabetically, then a final `Unranked` group. Each row shows depth-chart rank, `full_name` (bold), `status`, and a red `injury_status` pill if present. Whole row clickable → Player screen for that player_id. If the clicked id matches `PLAYER_DETAIL`, render full detail; otherwise render the partial row plus *"Ask Claude to regenerate scoped to this player_id for full bio + notes"*.
  - **Right — Notes**: two stacked sections.
    - **Notes about this team** (`TEAM_DETAIL.notes`, newest first): body, relative timestamp, mention chips.
    - **Notes that mention this team** (`TEAM_DETAIL.mentions`, newest first): each entry prefixed with a small badge — "from study: <title>" / "from player: <full_name>" — above the body.

Step 4. **Screen 3 — Player** (when a player_id is selected; render from `PLAYER_DETAIL`):
- Top: "← Back to <Team Name>" link returning to the Team screen (preserves the team scope). If we arrived without team context, show "← Back to all teams" instead.
- Single-column detail card. Sections (hide rows where every value is null):
  - **Header:** `full_name` (large), `team` and `position` on a second line. Red `injury_status` pill in the header if present. Yellow "★ Watchlist" pill if `watchlist` is true.
  - **Identity & position:** `fantasy_positions`, `number`, `depth_chart_position`, `depth_chart_order` (e.g. "Depth: 2nd at QB").
  - **Status & injury:** `status`, `injury_status`, `injury_body_part`, `injury_notes`, `practice_participation`.
  - **Bio:** `age`, `height`, `weight`, `years_exp`, `college`.
  - **Cross-platform IDs:** small monospaced row, collapsed by default behind a "Show IDs" toggle.
  - **Notes about this player:** `PLAYER_DETAIL.notes`, newest-first, with body, relative timestamp, mention chips.
  - **Mentioned in other notes:** `PLAYER_DETAIL.mentions`, newest-first, each with a "from study: …" / "from team: …" / "from player: …" badge above the body.
- **Compose-note section** at the bottom of the card:
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

Step 5. Visual style: a single content column max ~960px wide on the Teams screen (to fit the division grid), ~720px on the Player screen, two-column on the Team screen. Top-of-screen navigation is sticky. Group headings in the depth chart use small-caps gray with a thin underline. Player rows have a subtle hover state. Watchlist pill is warm yellow; injury pill is muted red. Mobile fallback: stack everything vertically.

Success: the user lands on a Teams overview, drills into one team to see its depth chart alongside team-level notes and mentions, then drills into a single player for full detail and a one-paste note compose — all from a single snapshot artifact, without embedding the full player roster.
