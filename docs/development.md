# Development

## Running the test suite

```bash
pip install -e ".[dev]"
pytest
```

## Codebase layout

The codebase lives in `src/ffpresnap/`:

- `db.py` — the SQLite layer (schema, migrations, every read/write). See [the schema doc](./schema.md) for a column-by-column reference.
- `server.py` — the MCP tool surface (19 tools, declarative registration). See [the tools reference](./tools.md).
- `sync.py` + `sleeper.py` — Sleeper fetch + transactional player replace.
- `prompt_loader.py` + `prompts/*.md` — the prompt library catalog.
- `cli.py` — the `ffpresnap-sync` console script.

Plans and brainstorms for shipped features live under `docs/plans/` and `docs/brainstorms/`.

## Adding a prompt

Drop a new `.md` file in `src/ffpresnap/prompts/` with frontmatter:

```markdown
---
slug: my-prompt
title: My Prompt
description: Short one-line description.
---
Body of the prompt — the instructions Claude should follow when the user pastes this into a chat.
```

It'll be reconciled into the local DB on the next MCP server open and appear in `list_prompts`.

## Contributing

PRs that add prompts, dashboards, or new MCP tools are welcome — open an issue first if you're not sure whether something is in scope.
