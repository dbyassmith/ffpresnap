---
slug: study-browser
title: Study Browser
description: Browse open studies and drill into one to see its notes.
---
Build me a study browser as an interactive Claude artifact.

Step 1. Call the MCP tool `list_studies` with no arguments (defaults to `status: "open"`). For each returned study, also call `list_notes` with `scope: "study"` and `target_id: str(study.id)` so the artifact has note counts and bodies up front.

Step 2. Embed all results as JSON constants in the artifact source. The artifact is a snapshot; to refresh, ask Claude to regenerate.

Step 3. Render a two-pane layout:
- **Left pane:** a vertical list of study cards. Each card shows `title` (large), `description` (smaller, gray), a status pill ("open" / "archived"), and a note count badge. Clicking a card selects it.
- **Right pane:** the selected study's detail view. Show its description, status, and the full list of notes (newest first) with each note's `body`, `created_at`, and any `mentions.players` and `mentions.teams` rendered as small chips.

Step 4. Add a "Show archived" toggle above the list. When toggled on, instruct the user (in a small text hint) to ask Claude to regenerate with `list_studies` called using `status: "all"` — the artifact itself can't re-fetch.

Step 5. Visual style: clean two-column layout, subtle borders, generous spacing. The selected study card has a distinct highlighted state. Mobile fallback: stack vertically.

Success: the user can scan all open studies at a glance and drill into any one to read its full note thread.
