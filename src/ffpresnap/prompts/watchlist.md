---
slug: watchlist
title: Watchlist
description: Browse watchlisted players, drill into details, and copy commands to add or remove watchlist entries.
---
Build me a watchlist as an interactive Claude artifact with **two screens**: List → Player.

The artifact is scoped to my watchlist, plus optionally a single player I want detail for. Before generating, infer from my message:

- a specific **player_id** — opens straight to the Player screen with full detail
- otherwise default to the List screen

Step 1. Make these MCP tool calls and embed each result as a JSON constant in the artifact source. The artifact is a snapshot — to refresh, ask Claude to regenerate.

- Always: `list_players(watchlist=true)` — embed as `WATCHLIST`. The full list of watchlisted players with team, position, depth_chart_position, depth_chart_order, status, injury fields, and bio.
- If I gave you a **player_id**: `get_player(player_id=<id>)` — embed as `PLAYER_DETAIL` (includes `notes` and `mentions`).

Do **not** call unfiltered `list_players()` — the dataset is too large.

Step 2. **Screen 1 — List** (default):
- Header: "Watchlist" with a small subtitle showing total count from `WATCHLIST`. In the top-right of the header, render a **refresh icon button** (↻) that copies `"Show me my watchlist"` to clipboard with a brief "Copied! Paste in Claude to pull the latest." flash. Aria-label: "Refresh watchlist (copies regenerate command)".
- A search input that filters `WATCHLIST` client-side by case-insensitive substring on `full_name`, `team`, or `position`.
- A small filter row below the search: position chips (`ALL`, `QB`, `RB`, `WR`, `TE`, `K`, `DEF`) — clicking toggles a single-select position filter on the embedded list.
- The list itself, grouped by **team** (alphabetical by abbreviation), each group showing the team abbreviation as a small-caps gray heading. Within a group, sort by `depth_chart_position` (QB, RB, WR, TE, K, DEF, others alphabetical) then by `depth_chart_order` ascending with nulls last. Each row shows:
  - `full_name` (bold) on the left
  - `position` and depth-chart rank ("QB1", "RB2", or "—" if unranked) as a small muted label
  - `status` and an `injury_status` red pill if present
  - A small "Remove" button on the right (see Step 4)
  - The rest of the row (name area) is clickable → Player screen for that player_id. The "Remove" button stops propagation so it doesn't navigate.
- If `WATCHLIST` is empty, render a friendly empty state: "No players on your watchlist yet." with an "+ Add a player" button (see Step 4).
- At the bottom of the list, always render a footer "+ Add a player to watchlist" button (see Step 4).

Step 3. **Screen 2 — Player** (when a player is selected):
- Top: "← Back to watchlist" link returning to the List screen.
- If `PLAYER_DETAIL` is embedded and matches the selected player_id, render the full detail card. Otherwise render the partial row data from `WATCHLIST` plus a banner *"Ask Claude to regenerate scoped to this player_id for full bio + notes"*.
- Detail card sections (hide rows where every value is null):
  - **Header:** `full_name` (large), `team` and `position` on a second line. Red `injury_status` pill if present. Yellow "★ Watchlist" pill (always shown here, since every player on this screen is watchlisted).
  - **Identity & position:** `fantasy_positions`, `number`, `depth_chart_position`, `depth_chart_order` (e.g. "Depth: 2nd at QB").
  - **Status & injury:** `status`, `injury_status`, `injury_body_part`, `injury_notes`, `practice_participation`.
  - **Bio:** `age`, `height`, `weight`, `years_exp`, `college`.
  - **Cross-platform IDs:** small monospaced row, collapsed by default behind a "Show IDs" toggle.
  - **Notes about this player:** `PLAYER_DETAIL.notes` newest-first, with body, relative timestamp, and mention chips.
  - **Mentioned in other notes:** `PLAYER_DETAIL.mentions` newest-first, each prefixed with a small badge ("from study: …", "from team: …", "from player: …").
- Render a prominent **"Remove from watchlist"** button at the top of the card (see Step 4).

Step 4. Add / remove watchlist commands. Artifacts can't call MCP directly, so all mutations are copy-to-clipboard:

- **Remove from watchlist** (button on each list row and on the player detail header): on click, call `navigator.clipboard.writeText` with this exact string:

  ```
  Remove <full_name> (player_id "<player_id>") from my watchlist.
  ```

  Briefly flash "Copied! Paste in Claude to remove." on the button (~2 seconds), then revert. Add an aria-label "Remove <full_name> from watchlist (copies remove command)".

- **Add a player to watchlist** (the "+ Add a player to watchlist" footer button and empty-state button): on click, open a small inline panel with:
  - A text input labeled "Player name or player_id".
  - A "Copy add command" button. On click, build a string of this exact shape:

    ```
    Add <input value> to my watchlist.
    ```

    …call `navigator.clipboard.writeText(...)` with that string and briefly flash "Copied! Paste in Claude to add." on the button.
  - Below the button, a one-line muted note: "Claude will resolve the name to a player_id and call `update_player` with `watchlist: true`. Then ask Claude to regenerate this artifact to see the new entry."

Step 5. Visual style: a single-column card max ~720px wide. Sticky header with the search input and refresh button. Group headings (team abbreviations) in small-caps gray with a thin underline. Player rows have a subtle hover state. The "Remove" button on each row is small and muted by default, turning red on hover. The footer "+ Add a player to watchlist" button is a dashed-border full-width button. Watchlist pill is warm yellow; injury pill is muted red. Mobile fallback: stack search and filter chips vertically.

Success: the user sees their full watchlist grouped by team, can drill into any player for full detail and notes, and can copy-paste a one-line command to add or remove a player from the watchlist — without the artifact ever calling MCP itself.
