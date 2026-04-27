---
slug: team-overview
title: Team Overview
description: Combined depth chart, team notes, and notes mentioning the team.
---
Build me a team overview as an interactive Claude artifact.

Step 1. Ask the user which team if they haven't said. Then call both:
- `get_team` with `team: <identifier>` — returns `{ team, notes, mentions }`.
- `get_depth_chart` with the same team — returns `{ team, groups: [...] }`.

Embed both results as JSON constants.

Step 2. Render a two-column layout:
- **Left column (the depth chart):** as in the depth-chart-explorer prompt — one stacked group per position, players ordered by `depth_chart_order` with name, status, and injury indicator.
- **Right column (the notes):** two stacked sections.
  - **Notes about this team:** the `notes` array from `get_team`, newest first, with body, relative timestamp, and mention chips.
  - **Notes that mention this team:** the `mentions` array, newest first. For each, render a small subject badge ("from study: <title>" / "from player: <full_name>") above the body so the user knows where it came from.

Step 3. Header: team full name, abbreviation, and a small breadcrumb of conference/division.

Step 4. Visual style: two columns side-by-side on wide screens, stacked on narrow. Notes column scrolls independently if longer than the depth chart.

Step 5. Refresh model: snapshot artifact; ask Claude to regenerate to refresh.

Success: a single screen that gives a complete read on a team for pre-snap decisions — who's playing, who's hurt, and everything you've written about them or that mentions them.
