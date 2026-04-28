"""Tests for Database.upsert_players_for_source and the multi-source merge flow.
Covers Sleeper-source upsert, Ourlads identity matching, R13 clear + demotion,
and concurrency advisory lock.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ffpresnap.db import ConcurrentSyncError, Database


def _open(tmp_path):
    return Database.open(tmp_path / "notes.db")


def _sleeper_row(player_id: str, **kwargs) -> dict:
    """Build a Sleeper-shaped player dict for upsert input."""
    base = {
        "player_id": player_id,
        "full_name": "Test Player",
        "first_name": "Test",
        "last_name": "Player",
        "team": "BUF",
        "position": "QB",
        "fantasy_positions": None,
        "number": None,
        "depth_chart_position": "QB",
        "depth_chart_order": 1,
        "status": "Active",
        "injury_status": None,
        "injury_body_part": None,
        "injury_notes": None,
        "practice_participation": None,
        "age": None,
        "birth_date": None,
        "height": None,
        "weight": None,
        "years_exp": None,
        "college": None,
        "espn_id": None,
        "yahoo_id": None,
        "rotowire_id": None,
        "sportradar_id": None,
    }
    base.update(kwargs)
    return base


def _ourlads_row(**kwargs) -> dict:
    """Build an Ourlads-shaped row dict."""
    base = {
        "team": "BUF",
        "full_name": "Josh Allen",
        "position": "QB",
        "ourlads_id": None,
        "number": "17",
        "depth_chart_position": "QB",
        "depth_chart_order": 1,
    }
    base.update(kwargs)
    return base


# --- Sleeper-source upsert ---


def test_sleeper_insert_happy_path(tmp_path):
    db = _open(tmp_path)
    try:
        rows = [_sleeper_row("1"), _sleeper_row("2"), _sleeper_row("3")]
        n = db.upsert_players_for_source("sleeper", rows)
        assert n == 3
        # All three present, all source='sleeper'.
        for pid in ("1", "2", "3"):
            row = db.get_player(pid)
            assert row["source"] == "sleeper"
            assert row["ourlads_id"] is None
    finally:
        db.close()


def test_sleeper_source_scoped_delete_preserves_ourlads(tmp_path):
    """Sleeper sync should only delete sleeper-source rows; Ourlads-only rows survive."""
    db = _open(tmp_path)
    try:
        # Seed: 2 sleeper rows + 1 ourlads-only row.
        db.upsert_players_for_source("sleeper", [_sleeper_row("1"), _sleeper_row("2")])
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Practice Squad Guy", team="ATL")],
        )
        # Sanity: 3 total players.
        all_players = db.list_players()
        assert len(all_players) == 3

        # Re-run Sleeper sync with only player_id '1' present.
        db.upsert_players_for_source("sleeper", [_sleeper_row("1")])

        all_players = db.list_players()
        ids = {p["player_id"] for p in all_players}
        # Sleeper '2' is gone (not in input). Ourlads-only row survives.
        assert "1" in ids
        assert "2" not in ids
        ourlads_rows = [p for p in all_players if p["source"] == "ourlads"]
        assert len(ourlads_rows) == 1
    finally:
        db.close()


def test_sleeper_opt_out_on_merged_row(tmp_path):
    """Sleeper sync of an existing source='merged' row must not overwrite
    depth_chart_position / depth_chart_order (per-field ownership)."""
    db = _open(tmp_path)
    try:
        # Seed sleeper player.
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("1", full_name="Josh Allen", team="BUF", position="QB",
                          depth_chart_position="QB", depth_chart_order=1)],
        )
        # Ourlads picks them up — should become source='merged' with
        # Ourlads-derived depth values.
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Josh Allen", team="BUF", position="QB",
                          depth_chart_position="QB", depth_chart_order=1)],
        )
        merged = db.get_player("1")
        assert merged["source"] == "merged"

        # Now Sleeper sync runs again with a stale/null depth value.
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("1", full_name="Josh Allen", team="BUF", position="QB",
                          depth_chart_position="STALE", depth_chart_order=99)],
        )
        after = db.get_player("1")
        assert after["source"] == "merged"  # still merged
        assert after["depth_chart_position"] == "QB"  # Ourlads value preserved
        assert after["depth_chart_order"] == 1
    finally:
        db.close()


# --- Ourlads-source upsert ---


def test_ourlads_only_insert_no_match(tmp_path):
    db = _open(tmp_path)
    try:
        # Empty DB.
        n = db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Brand New Practice Squad Guy", ourlads_id="9999")],
        )
        assert n == 1
        # Inserted with player_id='ourlads:9999', source='ourlads'.
        row = db.get_player("ourlads:9999")
        assert row["source"] == "ourlads"
        assert row["ourlads_id"] == "9999"
        assert row["full_name"] == "Brand New Practice Squad Guy"
    finally:
        db.close()


def test_ourlads_identity_merge_by_name(tmp_path):
    """Ourlads row matching a Sleeper row by name+team+position merges in place."""
    db = _open(tmp_path)
    try:
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("100", full_name="Bijan Robinson", team="ATL", position="RB",
                          depth_chart_position=None, depth_chart_order=None)],
        )
        n = db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(
                full_name="Bijan Robinson", team="ATL", position="RB",
                depth_chart_position="RB", depth_chart_order=1,
                ourlads_id="42",
            )],
        )
        assert n == 1
        merged = db.get_player("100")
        assert merged["source"] == "merged"
        assert merged["ourlads_id"] == "42"
        assert merged["depth_chart_position"] == "RB"
        assert merged["depth_chart_order"] == 1

        # No duplicate inserted.
        all_players = db.list_players()
        assert len(all_players) == 1
    finally:
        db.close()


def test_ourlads_id_binding_survives_team_change(tmp_path):
    """After binding ourlads_id on first match, subsequent runs find the row
    by ourlads_id even if Ourlads now lists a different team."""
    db = _open(tmp_path)
    try:
        # Seed sleeper player on KC.
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("100", full_name="Trade Target", team="KC", position="WR")],
        )
        # First Ourlads run binds ourlads_id.
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Trade Target", team="KC", position="WR",
                          ourlads_id="42")],
        )
        # Second Ourlads run lists same ourlads_id on SF (post-trade).
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Trade Target", team="SF", position="WR",
                          ourlads_id="42")],
        )
        # One row only; team updated to SF.
        all_players = db.list_players()
        assert len(all_players) == 1
        assert all_players[0]["team"] == "SF"
        assert all_players[0]["ourlads_id"] == "42"
    finally:
        db.close()


def test_ambiguous_match_inserts_as_ourlads_only(tmp_path, capsys):
    """When >1 sleeper rows match the same name+team+position, insert the
    Ourlads row as ourlads-only and log to stderr."""
    db = _open(tmp_path)
    try:
        # Seed two sleeper rows with the same name (legitimate corner case
        # where Sleeper has both a Sr. and Jr. with normalization collision —
        # but our normalization preserves suffixes, so we construct the
        # collision manually with identical names).
        db.upsert_players_for_source(
            "sleeper",
            [
                _sleeper_row("100", full_name="John Smith", team="DAL", position="WR"),
                _sleeper_row("101", full_name="John Smith", team="DAL", position="WR"),
            ],
        )
        n = db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="John Smith", team="DAL", position="WR",
                          ourlads_id="999")],
        )
        assert n == 1
        # Three rows total now: two sleeper + one ourlads-only.
        all_players = db.list_players()
        assert len(all_players) == 3
        ourlads_rows = [p for p in all_players if p["source"] == "ourlads"]
        assert len(ourlads_rows) == 1
        # Stderr captured the ambiguous-match log line.
        captured = capsys.readouterr()
        assert "ourlads:identity:ambiguous" in captured.err
        assert "100" in captured.err
        assert "101" in captured.err
    finally:
        db.close()


# --- R13 clear + demotion ---


def test_r13_clears_and_demotes_merged_when_team_complete(tmp_path):
    """When Ourlads' team chart was successfully observed and a merged player
    is no longer on it, clear depth fields and demote back to source='sleeper'."""
    db = _open(tmp_path)
    try:
        # Seed merged player on BUF.
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("100", full_name="Old RB1", team="BUF", position="RB")],
        )
        old_run = "2026-01-01T00:00:00+00:00"
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Old RB1", team="BUF", position="RB",
                          depth_chart_position="RB", depth_chart_order=1,
                          ourlads_id="100")],
            run_start_at=old_run,
        )
        merged = db.get_player("100")
        assert merged["source"] == "merged"
        assert merged["depth_chart_position"] == "RB"

        # Now run Ourlads again, BUF chart complete, but Old RB1 not in input.
        new_run = "2026-02-01T00:00:00+00:00"
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="New RB1", team="BUF", position="RB",
                          ourlads_id="999", depth_chart_position="RB",
                          depth_chart_order=1)],
            completeness={"BUF": True},
            run_start_at=new_run,
        )

        # Old RB1: depth cleared, source demoted back to 'sleeper'.
        after = db.get_player("100")
        assert after["source"] == "sleeper"
        assert after["depth_chart_position"] is None
        assert after["depth_chart_order"] is None
        assert after["ourlads_id"] is None
    finally:
        db.close()


def test_r13_leaves_alone_when_team_incomplete(tmp_path):
    """When the team's chart slice failed sanity, R13 must NOT clear."""
    db = _open(tmp_path)
    try:
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("100", full_name="Some RB", team="BUF", position="RB")],
        )
        old_run = "2026-01-01T00:00:00+00:00"
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Some RB", team="BUF", position="RB",
                          depth_chart_position="RB", depth_chart_order=1,
                          ourlads_id="100")],
            run_start_at=old_run,
        )
        new_run = "2026-02-01T00:00:00+00:00"
        # New run with completeness=False for BUF.
        db.upsert_players_for_source(
            "ourlads",
            [],  # nothing for BUF
            completeness={"BUF": False},
            run_start_at=new_run,
        )
        after = db.get_player("100")
        assert after["source"] == "merged"  # NOT demoted
        assert after["depth_chart_position"] == "RB"  # NOT cleared
        assert after["ourlads_id"] == "100"
    finally:
        db.close()


# --- Notes survive merge ---


def test_notes_survive_identity_merge(tmp_path):
    """A note attached to a Sleeper player remains attached after an Ourlads
    identity merge updates the row in place."""
    db = _open(tmp_path)
    try:
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("100", full_name="Note Subject", team="KC", position="QB")],
        )
        # Add a note.
        db.add_note("100", "ankle wrapped on practice report")

        # Identity merge.
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Note Subject", team="KC", position="QB",
                          ourlads_id="42")],
        )
        merged = db.get_player("100")
        assert merged["source"] == "merged"
        # Notes still attached.
        notes = db.list_notes("100")
        assert len(notes) == 1
        assert "ankle wrapped" in notes[0]["body"]
    finally:
        db.close()


# --- Concurrency advisory lock ---


def test_concurrency_lock_blocks_second_run_within_window(tmp_path):
    db = _open(tmp_path)
    try:
        run_id = db.record_sync_start("https://example.com/x", source="sleeper")
        with pytest.raises(ConcurrentSyncError):
            db.record_sync_start("https://example.com/y", source="ourlads")
        # Finishing the first run releases the lock.
        db.record_sync_finish(run_id, players_written=0, status="success")
        # Now a second run can start.
        run2 = db.record_sync_start("https://example.com/y", source="ourlads")
        assert run2 != run_id
    finally:
        db.close()


def test_concurrency_lock_ignores_stale_running_run(tmp_path):
    """A 'running' row older than 5 minutes is treated as crashed and ignored."""
    db = _open(tmp_path)
    try:
        # Manually insert a stale 'running' row from 10 minutes ago.
        stale = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(timespec="seconds")
        db.conn.execute(
            "INSERT INTO sync_runs (started_at, source_url, status, source) "
            "VALUES (?, 'https://stale', 'running', 'sleeper')",
            (stale,),
        )
        db.conn.commit()
        # Should NOT raise; new run starts fine.
        run_id = db.record_sync_start("https://example.com/new", source="ourlads")
        assert run_id > 0
    finally:
        db.close()


def test_last_sync_filters_by_source(tmp_path):
    db = _open(tmp_path)
    try:
        sleeper_id = db.record_sync_start("https://sleeper", source="sleeper")
        db.record_sync_finish(sleeper_id, players_written=10, status="success")
        ourlads_id = db.record_sync_start("https://ourlads", source="ourlads")
        db.record_sync_finish(ourlads_id, players_written=5, status="success")

        latest = db.last_sync()
        assert latest["id"] == ourlads_id  # most recent overall

        latest_sleeper = db.last_sync(source="sleeper")
        assert latest_sleeper["id"] == sleeper_id

        latest_ourlads = db.last_sync(source="ourlads")
        assert latest_ourlads["id"] == ourlads_id
    finally:
        db.close()


# --- Bidirectional merge: Sleeper picks up Ourlads-first player ---


def test_sleeper_picks_up_ourlads_only_player_merges_in_place(tmp_path):
    """When Sleeper sync inserts a new player_id whose name+team+position
    matches an existing Ourlads-only row, the Ourlads-only row's metadata
    (ourlads_id, depth chart) and any notes/mentions transfer to the new
    Sleeper player_id, and the Ourlads-only row is deleted. No duplicate
    rows; notes survive."""
    db = _open(tmp_path)
    try:
        # Practice-squad guy on Ourlads first.
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(
                full_name="Practice Squad Promotion",
                team="ATL",
                position="WR",
                ourlads_id="9999",
                depth_chart_position="WR",
                depth_chart_order=4,
            )],
        )
        ourlads_pid = "ourlads:9999"
        ourlads_row = db.get_player(ourlads_pid)
        assert ourlads_row["source"] == "ourlads"

        # User attaches a note to the Ourlads-only row.
        db.add_note(ourlads_pid, "watch this guy — could pop late round")
        notes_before = db.list_notes(ourlads_pid)
        assert len(notes_before) == 1

        # Sleeper picks them up — new Sleeper player_id 'S100', same name+team+pos.
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row(
                "S100",
                full_name="Practice Squad Promotion",
                team="ATL",
                position="WR",
                # Sleeper would write null/different depth — should NOT clobber.
                depth_chart_position=None,
                depth_chart_order=None,
            )],
        )

        # Old ourlads-only row is gone.
        with pytest.raises(Exception):
            db.get_player(ourlads_pid)

        # New row at S100, source='merged', ourlads_id transferred,
        # depth chart from Ourlads preserved (per-field ownership).
        merged = db.get_player("S100")
        assert merged["source"] == "merged"
        assert merged["ourlads_id"] == "9999"
        assert merged["depth_chart_position"] == "WR"
        assert merged["depth_chart_order"] == 4

        # Notes migrated.
        notes_after = db.list_notes("S100")
        assert len(notes_after) == 1
        assert "watch this guy" in notes_after[0]["body"]

        # Only one row total.
        all_players = db.list_players()
        assert len(all_players) == 1
    finally:
        db.close()


def test_sleeper_picks_up_with_team_mention_preserves_mention_fk(tmp_path):
    """A note on a different player that mentions the Ourlads-only player
    should still mention the merged Sleeper player_id after pickup."""
    db = _open(tmp_path)
    try:
        # Existing Sleeper player who'll have a note that mentions the Ourlads guy.
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("MENTIONER", full_name="Some Other Guy", team="KC",
                          position="QB")],
        )
        # Ourlads-only player.
        db.upsert_players_for_source(
            "ourlads",
            [_ourlads_row(full_name="Promotion Target", team="ATL", position="WR",
                          ourlads_id="42")],
        )
        # Add a note on MENTIONER that mentions the Ourlads-only player.
        db.add_note(
            "MENTIONER",
            "I'm bullish on Promotion Target",
            mentions={"player_ids": ["ourlads:42"], "team_abbrs": []},
        )

        # Sleeper picks up the promoted player. A realistic Sleeper sync
        # includes every Sleeper player in the input — including MENTIONER —
        # so the source-scoped DELETE doesn't wipe MENTIONER and its mentions.
        db.upsert_players_for_source(
            "sleeper",
            [
                _sleeper_row("MENTIONER", full_name="Some Other Guy", team="KC",
                             position="QB"),
                _sleeper_row("PROMOTED", full_name="Promotion Target", team="ATL",
                             position="WR"),
            ],
        )

        merged = db.get_player("PROMOTED")
        assert merged["source"] == "merged"

        # The mention now points at PROMOTED, not 'ourlads:42'.
        mentions_for_promoted = db.list_player_mentions("PROMOTED")
        assert len(mentions_for_promoted) == 1
        assert "Promotion Target" in mentions_for_promoted[0]["body"]
    finally:
        db.close()


# --- find_player_for_match ---


def test_find_player_for_match_handles_diacritics(tmp_path):
    db = _open(tmp_path)
    try:
        db.upsert_players_for_source(
            "sleeper",
            [_sleeper_row("100", full_name="José García", team="MIA", position="WR")],
        )
        # Look up using the unaccented form.
        candidates = db.find_player_for_match("jose garcia", "MIA", "WR")
        assert len(candidates) == 1
        assert candidates[0]["player_id"] == "100"
    finally:
        db.close()


def test_find_player_for_match_distinguishes_suffixes(tmp_path):
    db = _open(tmp_path)
    try:
        db.upsert_players_for_source(
            "sleeper",
            [
                _sleeper_row("100", full_name="Marvin Harrison", team="ARI",
                             position="WR"),
                _sleeper_row("101", full_name="Marvin Harrison Jr.", team="ARI",
                             position="WR"),
            ],
        )
        senior = db.find_player_for_match("marvin harrison", "ARI", "WR")
        junior = db.find_player_for_match("marvin harrison jr", "ARI", "WR")
        assert len(senior) == 1 and senior[0]["player_id"] == "100"
        assert len(junior) == 1 and junior[0]["player_id"] == "101"
    finally:
        db.close()
