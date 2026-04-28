# ffpresnap

Your personal NFL fantasy-football scratchpad — wired into Claude.

ffpresnap is a small local app that mirrors NFL roster and depth-chart data from the [Sleeper API](https://docs.sleeper.com/) into a SQLite file on your machine, with an optional second sync from [ourlads.com](https://www.ourlads.com/nfldepthcharts/) for hand-curated depth charts that often beat Sleeper to lineup changes. It exposes everything to Claude (Desktop, Code, or Cowork) as an MCP server. Once it's running, you can ask Claude things like:

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
ffpresnap-sync                      # default source: sleeper
ffpresnap-sync --source=ourlads     # optional: layer Ourlads depth charts on top
```

You should see something like `synced 4231 players in 0.4s (run_id=1, source=sleeper)`. The Sleeper sync filters to fantasy-relevant positions (QB, RB, WR, TE, K, DEF) and writes them to `~/.ffpresnap/notes.db`.

The optional **Ourlads** sync layers hand-curated, beat-reporter-driven depth charts on top of Sleeper's data. It fetches 32 team rosters plus one all-teams depth-chart page from [ourlads.com](https://www.ourlads.com/nfldepthcharts/) (~1-3 minutes total at the polite 1.5s/request default) and merges them into the same `players` table — without disturbing your Sleeper-sourced notes. Players Ourlads tracks but Sleeper hasn't picked up yet (practice-squad call-ups, recent signings) become first-class entries you can attach notes to.

You can rerun either source whenever you want fresh data. A reasonable daily cron does both, in order:

```cron
0 9 * * * /absolute/path/to/.venv/bin/ffpresnap-sync --source=sleeper && /absolute/path/to/.venv/bin/ffpresnap-sync --source=ourlads
```

(Per-field ownership means Ourlads owns `depth_chart_position` / `_order` once it has touched a row, so cron ordering doesn't actually matter — but Sleeper-then-Ourlads is the more intuitive sequence.)

You can also ask Claude to run `sync_players` from inside a chat once the MCP is connected. Sleeper sync returns a summary inline (~5s); Ourlads sync runs in a background thread and returns a `run_id` immediately — Claude polls `get_sync_status(run_id)` to track completion.

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

You should see 21 tools listed (`sync_players`, `get_sync_status`, `list_teams`, `get_depth_chart`, `add_note`, `list_prompts`, etc.). If nothing appears, the client didn't pick up the MCP — re-check the config path and restart fully.

---

## First steps: open the prompt library

The fastest way to see what ffpresnap can do is to open the **prompt library**. Paste this into Claude:

```text
Build me a Claude artifact that displays the ffpresnap prompt library.

Step 1. Call the MCP tool `list_prompts` (no arguments).

Step 2. Embed the JSON result directly into the artifact source as a JavaScript constant — the artifact is a snapshot, not a live fetch.

Step 3. Render the prompts as a responsive card grid. Each card shows the prompt's `title` as the heading, the `description` as a one-line subhead, and a "Copy prompt" button that calls `navigator.clipboard.writeText(prompt.body)` and briefly shows "Copied!" in place of the button label.

Step 4. Above the grid, render an intro block in this order:
- A small wordmark / title: "ffpresnap".
- A short paragraph (max ~640px wide): *"ffpresnap is an un-opinionated scratchpad for all of your fantasy football research. Using the MCP, you can ask Claude to build whatever dashboards best suit your needs — the prompts below are just a starting point."*
- A one-line instruction beneath it in muted text: "Click 'Copy prompt' on any card, paste it into a new chat with the ffpresnap MCP connected, and Claude will build the dashboard."

Step 5. In the top-right of the header, render a refresh icon button (↻). Because the artifact is a snapshot and cannot call MCP tools directly, the refresh button works by copy-to-clipboard: on click, call `navigator.clipboard.writeText("Show me the prompt library")` and briefly flash "Copied! Paste in Claude to pull fresh prompts." next to the button. Aria-label: "Refresh prompts (copies regenerate command)". Below the header in small muted text: *"This is a snapshot. Click ↻ to copy the regenerate command — paste it in Claude to pull the latest prompts."*

Step 6. Visual style: clean cards with subtle borders, generous padding, readable typography. Plain React + inline styles. Mobile-friendly grid (1 column on narrow screens, 2–3 on wider). The refresh button stays aligned to the right of the header on all screen sizes.
```

Claude will build an artifact with one card per dashboard prompt:

| Card | What it builds |
|------|----------------|
| **Show prompt library** | Re-opens this same artifact (handy for later) |
| **Study Browser** | Browse open studies and drill into their notes |
| **Depth Chart Explorer** | Pick a team, see depth chart grouped by position |
| **Note Recency Feed** | Chronological feed of every note you've written |
| **Player Explorer** | Search or browse a team's depth chart, drill into a player, compose notes |
| **Team Explorer** | Drill from all NFL teams into one team's depth chart and notes, then into a single player |
| **Team Overview** | Combined depth chart + team notes + mentions |
| **Watchlist** | Browse watchlisted players, drill into details, copy add/remove commands |
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
