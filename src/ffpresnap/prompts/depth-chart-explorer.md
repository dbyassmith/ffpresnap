---
slug: depth-chart-explorer
title: Depth Chart Explorer
description: Pick a team and see its depth chart grouped by position.
---
Build me a depth chart explorer as an interactive Claude artifact.

Step 1. First, ask the user which team they want to view (accept abbreviation, full name, or nickname — e.g. "KC", "Kansas City Chiefs", or "Chiefs"). If they haven't said yet, prompt for it.

Step 2. Call the MCP tool `get_depth_chart` with `team: <whatever the user said>`. The result is `{ team: {...}, groups: [{ position, players: [...] }, ...] }`. The last group is always "Unranked" if any players have no depth chart position. Embed the result as a JSON constant in the artifact source.

Step 3. Render:
- **Header:** team full name and abbreviation.
- **Position columns:** one column per group, in the order returned. Each column header shows the position code (QB, RB, WR, TE, K, DEF, LWR, SWR, etc.) and a count of players.
- **Player rows within a column:** ordered by `depth_chart_order`, with depth-chart rank (1, 2, 3…) on the left, then `full_name` (bold), then `status` (Active / IR / etc.) and `injury_status` if present (red text). The "Unranked" group renders the same way but without the rank number.

Step 4. Visual style: each column is a card with subtle borders. Use color sparingly — red only for injury, neutral grays otherwise. Wrap on narrow screens.

Step 5. To explore a different team, instruct the user to ask Claude to regenerate with the new team. The artifact is a snapshot.

Success: the user can scan a full team's fantasy-relevant depth chart at a glance, see who's hurt or on IR, and quickly identify backups and unranked depth pieces.
