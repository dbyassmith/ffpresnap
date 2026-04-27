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

Step 4. Above the grid, include a one-sentence header: "Click 'Copy prompt' on any card, paste it into a new chat with the ffpresnap MCP connected, and Claude will build the dashboard."

Step 5. Visual style: clean cards with subtle borders, generous padding, readable typography. No frameworks needed beyond plain React + inline styles. Mobile-friendly grid (1 column on narrow screens, 2–3 on wider).

Success: the user sees one card per prompt, can copy any prompt with one click, and can re-summon this very artifact by copying the "Show prompt library" card.
