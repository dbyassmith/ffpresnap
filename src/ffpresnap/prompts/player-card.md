---
slug: player-card
title: Player Card
description: Open a detailed card for a single player with notes and mentions.
---
Build me a player card as an interactive Claude artifact.

Step 1. Ask the user which player they want to view if they haven't said. Then call `find_player` with their query. If exactly one match, proceed with that `player_id`. If multiple matches, list them and ask the user to pick one. If zero matches, surface a helpful error.

Step 2. Call `get_player` with the chosen `player_id`. The result is `{ player: {...}, notes: [...], mentions: [...] }`. Embed it as a JSON constant.

Step 3. Render a single-column card with these sections:
- **Header:** `full_name` (large), `team` and `position` on a second line. If `injury_status` is present, render it as a red pill in the header.
- **Identity & position:** `fantasy_positions`, `number`, `depth_chart_position`, `depth_chart_order` (as "Depth: 2nd at QB", etc.).
- **Status & injury:** `status`, `injury_status`, `injury_body_part`, `injury_notes`, `practice_participation`. Hide rows with null values.
- **Bio:** `age`, `height`, `weight`, `years_exp`, `college`. Hide nulls.
- **Cross-platform IDs:** small monospaced row with `espn_id`, `yahoo_id`, etc. — collapsed by default behind a "Show IDs" toggle.
- **Notes about this player:** the `notes` array, newest first, body + relative timestamp + any mention chips.
- **Mentioned in other notes:** the `mentions` array, same shape, but each entry shows the *primary subject* of the note as a small badge above the body so the user knows where the note "lives" (player/team/study).

Step 4. Visual style: a single readable card, max ~720px wide, plenty of whitespace between sections. Section headings small-caps, gray, separated by thin rules.

Success: the user can open any player and see, on one screen, who they are, how they're doing, what notes they've written about them, and what other notes have tagged them.
