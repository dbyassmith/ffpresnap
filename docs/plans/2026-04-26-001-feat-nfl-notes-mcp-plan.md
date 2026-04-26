---
title: NFL Player Notes SQLite Database with MCP Wrapper
type: feat
status: active
date: 2026-04-26
---

# NFL Player Notes SQLite Database with MCP Wrapper

## Overview

Build a small local SQLite database that stores NFL players and free-form notes about them, then expose it through an MCP server so Claude (Cowork / Code / Desktop) can read and write notes via tool calls.

## Problem Frame

The user wants a personal, persistent scratchpad for NFL players — quick to capture observations (injury news, matchup thoughts, fantasy reactions) and trivially queryable from inside a Claude session. SQLite + MCP keeps it local, zero-infra, and agent-native.

## Requirements Trace

- R1. Persist a list of NFL players (at minimum: name; ideally team + position) in SQLite.
- R2. Persist multiple timestamped notes per player.
- R3. Expose CRUD-ish operations through an MCP server runnable by Claude clients via stdio.
- R4. Setup is one command: clone, install, point Claude at it.
- R5. Data file lives at a stable, user-configurable path so notes survive across sessions.

## Scope Boundaries

- **In:** local single-user SQLite, MCP stdio server, basic player + note CRUD, simple search by name.
- **Out:** auth, multi-user sync, web UI, scheduled scraping of real NFL rosters/stats, fuzzy matching, full-text search, analytics, fantasy projections.
- **Out:** auto-seeding the database with the full NFL roster — players are added on demand (we may add a small optional seed script later, but it is not part of this plan).

## Context & Research

### Relevant Code and Patterns

Greenfield project — no existing code to mirror. Layout follows standard Python package conventions:

```
ffpresnap/
├── pyproject.toml
├── README.md
├── src/ffpresnap/
│   ├── __init__.py
│   ├── db.py        # sqlite connection, schema, query helpers
│   └── server.py    # MCP server: tool definitions + handlers
├── tests/
│   ├── test_db.py
│   └── test_server.py
└── docs/plans/
```

### External References

- Python MCP SDK: `mcp` package on PyPI (official Anthropic SDK), uses `mcp.server.Server` with stdio transport. Claude Desktop / Code register servers via `claude_desktop_config.json` or `.mcp.json` with a `command` + `args` entry.
- SQLite via stdlib `sqlite3` — no extra deps needed; enable `PRAGMA foreign_keys = ON` per connection.

## Key Technical Decisions

- **Language: Python 3.11+.** Stdlib `sqlite3` removes a dependency, official MCP SDK is mature, and the user's environment is Mac with Python readily available. Tradeoff: TypeScript would be equally fine, but Python keeps the surface smaller.
- **Transport: stdio only.** This is what Claude Desktop / Code / Cowork all support out of the box. No HTTP server needed.
- **Database location: `$FFPRESNAP_DB` env var, default `~/.ffpresnap/notes.db`.** Keeps data outside the repo so reinstalling/cloning never wipes notes. Directory is auto-created on first run.
- **Schema migrations: none.** Single `CREATE TABLE IF NOT EXISTS` block run at startup. If the schema ever changes, we add a tiny `schema_version` table later — premature now.
- **Player identity: integer PK, unique `(name, team)` constraint.** Allows two players with the same name on different teams; prevents accidental duplicates. Lookup tools accept either `player_id` or `name` (with optional `team` to disambiguate).
- **Notes: append-only by default, but updatable/deletable by id.** Each note gets `created_at` and `updated_at` (ISO-8601 UTC strings — sqlite has no native datetime).

## Open Questions

### Resolved During Planning

- *Python vs TypeScript?* → Python (see decisions).
- *Bundle a roster seed?* → No, manual add. Can revisit later.
- *Tags on notes?* → Out of scope for v1; revisit if the user starts cramming structure into note bodies.

### Deferred to Implementation

- Exact MCP SDK API shape (decorator vs. registration style) — pin once we read the installed `mcp` package version.
- Whether to expose notes as MCP **resources** (browsable) in addition to tools — decide after the tool surface is working.

## Implementation Units

- [ ] **Unit 1: Project scaffolding**

**Goal:** Initialize the Python package so the MCP server can be installed and run.

**Requirements:** R4

**Dependencies:** None

**Files:**
- Create: `pyproject.toml` (project metadata, `mcp` dependency, `pytest` dev dep, `ffpresnap-mcp` console script entry point pointing at `ffpresnap.server:main`)
- Create: `src/ffpresnap/__init__.py`
- Create: `README.md` (one-paragraph what-it-is + install + Claude config snippet)
- Create: `.gitignore` (Python defaults + `*.db`)

**Approach:**
- Use `src/` layout. Console script `ffpresnap-mcp` is what Claude clients will invoke.

**Test scenarios:**
- Test expectation: none — pure scaffolding.

**Verification:**
- `pip install -e .` succeeds.
- `ffpresnap-mcp --help` (or no-arg) is resolvable on PATH after install.

---

- [ ] **Unit 2: SQLite layer (`db.py`)**

**Goal:** Provide a small, well-tested data-access module that owns schema creation and CRUD for players and notes.

**Requirements:** R1, R2, R5

**Dependencies:** Unit 1

**Files:**
- Create: `src/ffpresnap/db.py`
- Test: `tests/test_db.py`

**Approach:**
- Single `Database` class wrapping a `sqlite3.Connection`. Constructor takes a path; `Database.open()` classmethod resolves `$FFPRESNAP_DB` or default, creates parent dir, opens connection, sets `PRAGMA foreign_keys = ON`, runs schema bootstrap.
- Schema:
  - `players(id INTEGER PK, name TEXT NOT NULL, team TEXT, position TEXT, created_at TEXT NOT NULL, UNIQUE(name, team))`
  - `notes(id INTEGER PK, player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`
  - Index on `notes(player_id, created_at DESC)`.
- Methods: `add_player`, `get_player(id_or_name, team=None)`, `list_players(search=None)`, `add_note`, `list_notes(player_id)`, `update_note`, `delete_note`, `delete_player`.
- Each method returns plain dicts (easy to JSON-serialize for MCP).

**Test scenarios:**
- Happy path: add a player, fetch by id, fetch by name → returns the inserted row.
- Happy path: add two notes for one player, list → returns both, newest first.
- Edge case: `add_player` with same `(name, team)` raises a clear duplicate error; same name + different team succeeds.
- Edge case: `list_players(search="mah")` matches `Patrick Mahomes` case-insensitively.
- Error path: `add_note` for a non-existent `player_id` raises a clear not-found error.
- Error path: `update_note` / `delete_note` on missing id raises not-found.
- Integration: `delete_player` cascades and removes that player's notes.
- Integration: opening the DB twice against the same path sees the same rows (persistence sanity check using a `tmp_path` fixture).

**Verification:**
- `pytest tests/test_db.py` is green.
- A fresh DB file appears at the resolved path on first use.

---

- [ ] **Unit 3: MCP server (`server.py`)**

**Goal:** Wrap the `Database` in an MCP stdio server exposing player + note tools to Claude.

**Requirements:** R3, R4

**Dependencies:** Unit 2

**Files:**
- Create: `src/ffpresnap/server.py`
- Test: `tests/test_server.py`

**Approach:**
- `main()` constructs `Database.open()`, builds an `mcp.server.Server("ffpresnap")`, registers tool handlers, and runs stdio transport.
- Tools (each with a JSON schema and a one-line description; handlers translate args → `Database` calls → JSON-serialized result):
  - `add_player(name, team?, position?)`
  - `find_player(query, team?)` — search by name substring; returns up to 10 matches.
  - `list_players()` — all players, alphabetical.
  - `delete_player(player_id)`
  - `add_note(player_id_or_name, body, team?)` — accepts name for ergonomics; if name is ambiguous, returns an error listing matches.
  - `list_notes(player_id_or_name, team?)`
  - `update_note(note_id, body)`
  - `delete_note(note_id)`
- All tool errors are returned as MCP tool errors with human-readable messages — Claude surfaces these directly.
- README includes a copy-pasteable `claude_desktop_config.json` (and `.mcp.json` for Claude Code) snippet:
  ```json
  { "mcpServers": { "ffpresnap": { "command": "ffpresnap-mcp" } } }
  ```

**Test scenarios:**
- Happy path: calling the `add_player` handler with `{"name":"Patrick Mahomes","team":"KC"}` inserts and returns the new player dict.
- Happy path: `add_note` by name when exactly one player matches creates the note against that player.
- Edge case: `add_note` by name when multiple players match returns an error message that lists the candidates (id + name + team) and does not create a note.
- Edge case: `find_player` with no matches returns an empty list, not an error.
- Error path: `update_note` on missing id returns a tool error with "note not found".
- Integration: end-to-end — `add_player` → `add_note` (by name) → `list_notes` returns the note. Use the `Database` against a `tmp_path` SQLite file; call handlers directly (no need to spawn a subprocess).

**Verification:**
- `pytest tests/test_server.py` is green.
- Manually: `ffpresnap-mcp` started from Claude Desktop appears as a connected server, all tools are listed, and a round-trip add-note / list-notes works in a chat.

## System-Wide Impact

- **Interaction graph:** Claude client (Desktop / Code / Cowork) ↔ stdio ↔ `ffpresnap-mcp` ↔ SQLite file on disk. No other surfaces.
- **State lifecycle risks:** The DB file is the only durable state. Losing it loses all notes — README should call out the path so users can back it up.
- **Unchanged invariants:** Nothing exists yet, so nothing to preserve.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| MCP SDK API shifts between versions and breaks the server. | Pin a minimum version in `pyproject.toml`; verify against the installed version when wiring `server.py`. |
| User runs the server from two clients concurrently and races on writes. | SQLite handles concurrent readers fine and serializes writers; default `BEGIN` is sufficient. Document that concurrent heavy writes aren't a goal. |
| DB path env var typo silently creates a second empty DB. | Log the resolved DB path at startup to stderr so it's visible in MCP server logs. |

## Documentation / Operational Notes

- README sections: What it is, Install (`pip install -e .`), Configure Claude (config snippet for Desktop and Code), Tools (one-line description of each), Data location (env var + default, backup tip).

## Sources & References

- Plan written from user request on 2026-04-26; no origin requirements doc.
- Python MCP SDK: https://github.com/modelcontextprotocol/python-sdk
- SQLite `sqlite3` stdlib docs.
