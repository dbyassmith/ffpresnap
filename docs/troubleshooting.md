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
- **A note disappears after a Sleeper sync.** A player can be removed from Sleeper's roster (released, retired, etc.). Sleeper sync deletes Sleeper-sourced rows that are no longer in the payload, and notes for those players cascade-delete with them. **Ourlads-only and `'merged'` rows survive Sleeper sync** — if the player you care about is also tracked on Ourlads, run `ffpresnap-sync --source=ourlads` first to bind them, and the Sleeper drop won't lose your notes.
- **You changed a prompt file in `src/ffpresnap/prompts/` and want it to persist.** It will, until you `git pull` — the prompt library is reconciled from the repo on every DB open. To make a custom prompt stick across pulls, add it as a new file in the repo (and feel free to PR it back).

## Ourlads sync issues

- **`Ourlads sync exceeded MAX_FAILED_TEAMS`.** More than 5 of the 32 team roster pages tripped a sanity check (HTML parse failure, fewer than 30 rows, or a network error). Likely an Ourlads layout change or a transient outage. Re-run later; if it persists, check the saved HTML structure against `tests/fixtures/ourlads/roster_ATL.html` and update the parser.
- **`ConcurrentSyncError: Another sync (sleeper, run_id=...) is already running`.** The advisory lock found a recent (<5 min) `sync_runs` row with `status='running'`. Either wait for it to finish, or if you're sure it crashed, force a re-sync after 5 minutes (the lock auto-expires). Manually clean it up with `UPDATE sync_runs SET status='error' WHERE status='running'` if you need to unblock immediately.
- **Ourlads sync looks stuck.** It runs in a background thread and takes ~1-3 minutes for 33 page fetches at the 1.5s politeness delay. Poll with `get_sync_status(run_id)` to see progress. If status stays `running` past 5 minutes, the worker thread likely crashed — check stderr for a traceback and clean up the stale row.
- **Players show up twice in `find_player`.** An identity-match collision happened (same normalized name on the same team and position in both sources, ambiguous match). The merged row never bound. Resolution today is manual — pick one row to keep and use the SQLite CLI to fix it (a `merge_players` MCP tool isn't built yet).
- **Depth chart reverts to stale Sleeper values.** Per-field ownership should prevent this — Sleeper sync skips depth-chart writes on `source IN ('ourlads','merged')` rows. If you're seeing reversion, check whether the row's `source` is correctly set; query `SELECT player_id, source, ourlads_id FROM players WHERE full_name LIKE '%name%'`.
- **Ourlads' robots.txt or ToS changed.** Run `python -c "from ffpresnap.ourlads import check_robots; print(check_robots())"`. If it returns `False`, stop running Ourlads syncs until the policy clarifies.
