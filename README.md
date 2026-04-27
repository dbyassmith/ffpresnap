# ffpresnap

Your personal NFL fantasy-football scratchpad — wired into Claude.

ffpresnap is a small local app that mirrors NFL roster and depth-chart data from the [Sleeper API](https://docs.sleeper.com/) into a SQLite file on your machine and exposes it to Claude (Desktop, Code, or Cowork) as an MCP server. Once it's running, you can ask Claude things like:

> *"Show me the Chiefs depth chart."*
> *"Add a note to Patrick Mahomes: ankle is wrapped on the practice report — watch it."*
> *"Start a study on RB handcuffs to draft late."*
> *"What did I write about the AFC West last week?"*

It also ships a **prompt library** — a catalog of pre-baked prompts that Claude turns into interactive dashboards (depth-chart explorer, study browser, note feed, mention graph, and more). One copy/paste and you're looking at a usable view of your data.

Single-user, local-first, no servers, no accounts, no cloud — your notes live in one SQLite file in your home directory.

---

## What you'll need

- macOS, Linux, or Windows with **Python 3.11 or newer**
- One of the Claude clients:
  - [Claude Desktop](https://claude.ai/download) (recommended for the artifact UX)
  - Claude Code
  - Cowork

---

## Setup

### 1. Get the code

```bash
git clone https://github.com/<you>/ffpresnap.git
cd ffpresnap
```

(Or download the repo as a zip and `cd` into it.)

### 2. Install

A virtualenv keeps the install isolated from your system Python:

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .
```

This installs two console scripts inside the venv:

- **`ffpresnap-mcp`** — the MCP server Claude talks to.
- **`ffpresnap-sync`** — pulls the latest NFL player data from Sleeper into your local DB.

> **Tip:** running `which ffpresnap-mcp` (macOS/Linux) or `where ffpresnap-mcp` (Windows) prints the absolute path. Some Claude clients prefer the absolute path in their config — see the next step.

### 3. Pull the initial player data

```bash
ffpresnap-sync
```

You should see something like `synced 4231 players in 0.4s (run_id=1)`. This filters to fantasy-relevant positions (QB, RB, WR, TE, K, DEF) and writes them to `~/.ffpresnap/notes.db`.

You can rerun this whenever you want fresh roster/injury data. Sleeper recommends no more than once per day; a daily cron is a reasonable default:

```cron
0 9 * * * /absolute/path/to/.venv/bin/ffpresnap-sync
```

You can also ask Claude to run `sync_players` from inside a chat once the MCP is connected.

### 4. Connect to Claude

**Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Create the file if it doesn't exist:

```json
{
  "mcpServers": {
    "ffpresnap": {
      "command": "ffpresnap-mcp"
    }
  }
}
```

If `ffpresnap-mcp` isn't on your shell's PATH (common when using a venv), use the absolute path from step 2:

```json
{
  "mcpServers": {
    "ffpresnap": {
      "command": "/Users/you/Projects/ffpresnap/.venv/bin/ffpresnap-mcp"
    }
  }
}
```

Then **fully quit Claude Desktop** (Cmd+Q on macOS — closing the window isn't enough) and reopen it.

**Claude Code** — add a `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "ffpresnap": { "command": "ffpresnap-mcp" }
  }
}
```

Restart the session.

### 5. Verify the connection

In Claude, ask:

> *"What ffpresnap tools do you have?"*

You should see 19 tools listed (`sync_players`, `list_teams`, `get_depth_chart`, `add_note`, `list_prompts`, etc.). If nothing appears, the client didn't pick up the MCP — re-check the config path and restart fully.

---

## First steps: open the prompt library

The fastest way to see what ffpresnap can do is to open the **prompt library**. Paste this into Claude:

```text
Build me a Claude artifact that displays the ffpresnap prompt library.

Step 1. Call the MCP tool `list_prompts` (no arguments).

Step 2. Embed the JSON result directly into the artifact source as a JavaScript constant — the artifact is a snapshot, not a live fetch. To refresh it later, ask Claude to regenerate the artifact.

Step 3. Render the prompts as a responsive card grid. Each card shows the prompt's `title` as the heading, the `description` as a one-line subhead, and a "Copy prompt" button that calls `navigator.clipboard.writeText(prompt.body)` and briefly shows "Copied!" in place of the button label.
```

Claude will build an artifact with one card per dashboard prompt:

| Card | What it builds |
|------|----------------|
| **Show prompt library** | Re-opens this same artifact (handy for later) |
| **Study Browser** | Browse open studies and drill into their notes |
| **Depth Chart Explorer** | Pick a team, see depth chart grouped by position |
| **Note Recency Feed** | Chronological feed of every note you've written |
| **Player Card** | Detailed card for a single player + their notes/mentions |
| **Team Overview** | Combined depth chart + team notes + mentions |
| **Mention Graph** | Node-link graph of who-mentions-whom across notes |

Click **Copy prompt** on any card, open a new Claude chat, paste, and Claude will build that dashboard for you using your local data.

---

## How to use it day-to-day

You don't have to memorize tools — just talk to Claude. A few natural openings:

- **Take notes during the week**
  > *"Add a note to Travis Kelce: limited Wednesday with a knee. Mention Mahomes too."*
  > *"Open my 'AFC West' team note and add: Bolts looked banged up on tape."*

- **Organize bigger questions as studies**
  > *"Start a study called 'WR3s on cheap teams' with description 'targets I want at the end of drafts'. Then add a note tagging DJ Moore and the Chicago Bears."*

- **Browse before game day**
  > *"Show me KC's depth chart, then read me back any notes mentioning Mahomes."*
  > *"What did I write this week? Just give me the recent feed."*

- **Build a dashboard**
  > Open the prompt library (above), copy `Player Card`, paste into a new chat, and ask Claude to render it for Patrick Mahomes.

Notes can attach to a **player**, **team**, or **study**. Any note can also tag (`mentions`) other players and teams — those tagged subjects will see the note appear in their `mentions` list when you open them.

---

## Tools reference

If you want to be precise (or look something up later), here are all 19 tools.

**Sync**

- `sync_players()` — pull current Sleeper data into the local DB.
- `last_sync()` — show the most recent sync run.

**Browse**

- `list_teams(query?)` — list NFL teams; filter by abbreviation, name, conference, or division.
- `get_team(team)` — team record plus its notes and any notes elsewhere that mention it. `team` accepts `"KC"`, `"Kansas City Chiefs"`, or `"Chiefs"`.
- `get_depth_chart(team)` — depth chart grouped by position; unranked players land in a trailing `Unranked` group.
- `find_player(query)` — case-insensitive name substring search (max 10).
- `get_player(player_id)` — full player detail plus two lists: `notes` (about this player) and `mentions` (notes elsewhere that tag this player).
- `list_players(team?, position?)` — flat listing with optional filters.

**Studies (research containers)**

- `create_study(title, description?)` — start a new research thread (defaults to `open`).
- `list_studies(status?)` — defaults to open; pass `"archived"` or `"all"`.
- `get_study(study_id)` — study record plus its notes.
- `update_study(study_id, title?, description?)` — partial update.
- `set_study_status(study_id, status)` — `"open"` or `"archived"`.
- `delete_study(study_id)` — deletes the study and all of its notes (cascades).

**Notes (unified)**

- `add_note(target_type, target_id, body, mentions?)` — `target_type` is `"player"`, `"team"`, or `"study"`. `target_id` is the player_id (string), team identifier (abbr / name / nickname), or study id (as a string).
- `list_notes(scope, target_id?, limit?)` — `scope` is `"player"`, `"team"`, `"study"`, or `"recent"`. The first three return primary-subject notes for the given `target_id`; `"recent"` returns a chronological feed across all subjects (default 50, max 200), with each entry carrying a `subject` block.
- `update_note(note_id, body, mentions?)` — replace a note's body. If `mentions` is provided, the stored mention set is replaced wholesale; omit to leave mentions unchanged.
- `delete_note(note_id)` — delete a note.

`add_note` and `update_note` accept an optional `mentions: { player_ids: [...], team_abbrs: [...] }`. Mentions are validated at write time — an unknown player or unresolvable team rejects the whole write.

**Prompt library**

- `list_prompts()` — return the curated catalog of dashboard prompts. Prompts ship with the package and are reconciled into the local DB on every open (repo is source of truth — local edits get overwritten).

---

## Where your data lives

Your DB is a single file at `~/.ffpresnap/notes.db` by default. Override the path with the `FFPRESNAP_DB` environment variable. The actual path is logged to stderr when the MCP server starts.

If you want to back up your notes, copy that file. If you want to start over, delete it — the next `Database.open()` will recreate the schema and re-sync teams.

---

## Troubleshooting

- **Claude says it doesn't have ffpresnap tools.** Restart your Claude client *fully* (Cmd+Q on macOS, then reopen — closing the window isn't enough). Confirm the path in your config — if `ffpresnap-mcp` isn't on PATH outside your venv, use an absolute path in the JSON.
- **`ffpresnap-sync` errors on first run.** Make sure you can reach `https://api.sleeper.app/v1/players/nfl` in a browser. The CLI logs the actual error message to stderr.
- **A note disappears after sync.** A player can be removed from Sleeper's roster (released, retired, etc.). Notes attached to a removed player cascade-delete with them. To keep the content, copy it to a study or team note instead.
- **You changed a prompt file in `src/ffpresnap/prompts/` and want it to persist.** It will, until you `git pull` — the prompt library is reconciled from the repo on every DB open. To make a custom prompt stick across pulls, add it as a new file in the repo (and feel free to PR it back).

---

## Upgrading

Pull the latest code, reinstall, and restart Claude:

```bash
git pull
pip install -e .
```

The DB schema upgrades itself in place when the MCP server next opens it. Your existing player notes, team notes, and studies are preserved.

---

## Development

Run the test suite:

```bash
pip install -e ".[dev]"
pytest
```

The codebase lives in `src/ffpresnap/`:

- `db.py` — the SQLite layer (schema, migrations, every read/write).
- `server.py` — the MCP tool surface (19 tools, declarative registration).
- `sync.py` + `sleeper.py` — Sleeper fetch + transactional player replace.
- `prompt_loader.py` + `prompts/*.md` — the prompt library catalog.
- `cli.py` — the `ffpresnap-sync` console script.

Plans and brainstorms for shipped features live under `docs/plans/` and `docs/brainstorms/`.

PRs that add prompts, dashboards, or new MCP tools are welcome — open an issue first if you're not sure whether something is in scope.
