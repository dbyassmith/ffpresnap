---
slug: show-prompt-library
title: Show prompt library
description: Re-open the prompt library artifact with copy-to-clipboard cards.
---
Build me a Claude artifact that displays the ffpresnap prompt library.

Step 1. Call the MCP tool `list_prompts` (no arguments).

Step 2. Embed the JSON result directly into the artifact source as a JavaScript constant — the artifact is a snapshot, not a live fetch. To refresh it later, ask Claude to regenerate the artifact.

Step 3. Render the prompts as a responsive card grid. Each card shows:
- The prompt's `title` as the card heading.
- The prompt's `description` as a one-line subhead.
- A "Copy prompt" button that calls `navigator.clipboard.writeText(prompt.body)` and briefly shows a "Copied!" confirmation in place of the button label.

Step 4. Above the grid, render an intro block in this order:
- A small wordmark / title: "ffpresnap".
- A short paragraph (rendered in normal-weight body text, max ~640px wide): *"ffpresnap is an un-opinionated scratchpad for all of your fantasy football research. Using the MCP, you can ask Claude to build whatever dashboards best suit your needs — the prompts below are just a starting point."*
- A one-line instruction beneath it in muted text: "Click 'Copy prompt' on any card, paste it into a new chat with the ffpresnap MCP connected, and Claude will build the dashboard."

Step 5. In the top-right of the header, render a **refresh icon button** (a circular-arrow ↻ glyph in a small square button). Because the artifact is a snapshot and cannot call MCP tools directly, the refresh button works by copy-to-clipboard:
- On click, call `navigator.clipboard.writeText("Show me the prompt library")` (or equivalent — the canonical phrase that re-runs `list_prompts` and rebuilds this artifact).
- Briefly flash a tooltip or inline label next to the button: "Copied! Paste in Claude to pull fresh prompts." that auto-dismisses after ~2 seconds.
- Add a `title` / aria-label of "Refresh prompts (copies regenerate command)" for accessibility.
- Below the header, in small muted text, render: *"This is a snapshot. Click ↻ to copy the regenerate command — paste it in Claude to pull the latest prompts."*

Step 6. Visual style: clean cards with subtle borders, generous padding, readable typography. No frameworks needed beyond plain React + inline styles. Mobile-friendly grid (1 column on narrow screens, 2–3 on wider). The refresh button stays visible and aligned to the right of the header on all screen sizes.

Success: the user sees one card per prompt, can copy any prompt with one click, can refresh the library via the ↻ icon (which copies the regenerate command), and can re-summon this artifact by copying the "Show prompt library" card.
