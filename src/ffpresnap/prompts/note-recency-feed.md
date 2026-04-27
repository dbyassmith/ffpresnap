---
slug: note-recency-feed
title: Note Recency Feed
description: A chronological feed of all your notes across players, teams, and studies.
---
Build me a chronological note feed as an interactive Claude artifact.

Step 1. Call the MCP tool `list_notes` with `scope: "recent"` and `limit: 100`. The result is an array of notes; each note has `body`, `created_at`, a `subject` block (`{ type: "player" | "team" | "study", ... }`), and a `mentions` block with `players` and `teams` arrays. Embed the result as a JSON constant.

Step 2. Render a vertical timeline. Each entry shows:
- A small subject badge on the left, color-coded by type:
  - **player** — green badge, label `<full_name> (<team>, <position>)`.
  - **team** — blue badge, label `<full_name>`.
  - **study** — purple badge, label `<title>` plus a small "archived" tag if `status === "archived"`.
- The note `body` as the main content, rendered with line breaks preserved.
- The `created_at` timestamp formatted as a relative time ("2h ago", "yesterday", "3 days ago") on the right.
- Below the body, a row of small chips for each `mentions.players[].full_name` and `mentions.teams[].abbr` if present.

Step 3. Above the feed, render filter chips for subject types: "All", "Players", "Teams", "Studies". Clicking a chip filters the embedded data client-side; no extra MCP calls needed.

Step 4. Visual style: a single-column scrolling timeline. Use a subtle vertical line on the left to anchor the dots/badges. Generous spacing between entries.

Step 5. To pull more notes or refresh, instruct the user to ask Claude to regenerate.

Success: the user gets a quick scannable view of their recent thinking across the whole project, with full mention context inline.
