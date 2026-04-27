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
| **Player Explorer** | Search or browse a team's depth chart, drill into a player, compose notes |
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

## More

- [**MCP tools reference**](docs/tools.md) — all 19 tools, grouped by area.
- [**Database schema**](docs/schema.md) — every table, column, index, and cascade rule.
- [**Troubleshooting & data**](docs/troubleshooting.md) — where your data lives, upgrading, and common issues.
- [**Development**](docs/development.md) — running tests, codebase layout, contributing prompts.
