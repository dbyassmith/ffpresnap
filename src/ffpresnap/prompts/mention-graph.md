---
slug: mention-graph
title: Mention Graph
description: Visualize who-mentions-whom across your notes as a node-link graph.
---
Build me a mention graph as an interactive Claude artifact.

Step 1. Call `list_notes` with `scope: "recent"` and `limit: 200`. Embed the result as a JSON constant.

Step 2. From the embedded notes, derive:
- **Nodes:** the union of every primary `subject` (player/team/study) and every entry inside each note's `mentions.players` and `mentions.teams`. Each node has a stable id (player_id / team abbr / study_id), a display label, and a type used for color.
- **Edges:** for each note, an edge from its primary subject node to each of its mentioned-player and mentioned-team nodes. Edges carry the note's body and timestamp so the UI can show them on hover/click.

Deduplicate edges by `(source, target, note_id)`.

Step 3. Render an SVG node-link graph using a simple force-directed layout (you can implement a tiny ~50-line force simulation: repulsion between nodes, attraction along edges, light damping). Node colors:
- **player** — green
- **team** — blue
- **study** — purple

Node radius scales with degree (number of incident edges). Labels render next to each node, truncated to 18 chars.

Step 4. Interactions:
- Click a node: highlight it and its neighbors; dim the rest. Show a side panel listing the note bodies on incident edges (one card per note, newest first).
- Click empty space: reset highlight.
- A small legend in the top-right shows the three node colors.

Step 5. Visual style: clean white background, soft gray edges, generous SVG viewport. Mobile fallback: render a flat list of notes if the screen is too small for the graph to be useful.

Success: the user can see at a glance which players, teams, and studies are connected through their notes, and click into any node to read the underlying notes.
