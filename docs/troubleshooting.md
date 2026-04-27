# Troubleshooting & data

## Where your data lives

Your DB is a single file at `~/.ffpresnap/notes.db` by default. Override the path with the `FFPRESNAP_DB` environment variable. The actual path is logged to stderr when the MCP server starts.

If you want to back up your notes, copy that file. If you want to start over, delete it — the next `Database.open()` will recreate the schema and re-sync teams.

For a column-by-column reference, see [the schema doc](./schema.md).

## Upgrading

Pull the latest code, reinstall, and restart Claude:

```bash
git pull
pip install -e .
```

The DB schema upgrades itself in place when the MCP server next opens it. Your existing player notes, team notes, and studies are preserved.

## Common issues

- **Claude says it doesn't have ffpresnap tools.** Restart your Claude client *fully* (Cmd+Q on macOS, then reopen — closing the window isn't enough). Confirm the path in your config — if `ffpresnap-mcp` isn't on PATH outside your venv, use an absolute path in the JSON.
- **`ffpresnap-sync` errors on first run.** Make sure you can reach `https://api.sleeper.app/v1/players/nfl` in a browser. The CLI logs the actual error message to stderr.
- **A note disappears after sync.** A player can be removed from Sleeper's roster (released, retired, etc.). Notes attached to a removed player cascade-delete with them. To keep the content, copy it to a study or team note instead.
- **You changed a prompt file in `src/ffpresnap/prompts/` and want it to persist.** It will, until you `git pull` — the prompt library is reconciled from the repo on every DB open. To make a custom prompt stick across pulls, add it as a new file in the repo (and feel free to PR it back).
