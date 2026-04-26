from __future__ import annotations

import sqlite3

import pytest

from ffpresnap.db import (
    AmbiguousTeamError,
    Database,
    NotFoundError,
    SCHEMA_VERSION,
)


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "notes.db")
    yield d
    d.close()


def _player(player_id: str, **kwargs) -> dict:
    base = {
        "player_id": player_id,
        "full_name": kwargs.pop("full_name", f"Player {player_id}"),
        "team": kwargs.pop("team", "KC"),
        "position": kwargs.pop("position", "QB"),
    }
    base.update(kwargs)
    return base


# --- teams ---


def test_teams_seeded_on_open(db):
    teams = db.list_teams()
    assert len(teams) == 32
    abbrs = {t["abbr"] for t in teams}
    assert "KC" in abbrs and "BUF" in abbrs and "SF" in abbrs


def test_get_team_by_abbr_full_name_and_suffix(db):
    by_abbr = db.get_team("KC")
    by_full = db.get_team("Kansas City Chiefs")
    by_suffix = db.get_team("Chiefs")
    assert by_abbr == by_full == by_suffix
    assert by_abbr["abbr"] == "KC"


def test_get_team_unknown_raises(db):
    with pytest.raises(NotFoundError):
        db.get_team("Foobar")


def test_get_team_ambiguous_raises(db):
    with pytest.raises(AmbiguousTeamError) as exc:
        db.get_team("New York")
    assert len(exc.value.matches) == 2


def test_list_teams_query_filters(db):
    afc = db.list_teams("AFC")
    assert len(afc) == 16
    assert all(t["conference"] == "AFC" for t in afc)
    chiefs = db.list_teams("Chiefs")
    assert [t["abbr"] for t in chiefs] == ["KC"]


# --- migration / idempotency ---


def test_reopen_does_not_drop_data(db, tmp_path):
    db.replace_players([_player("1", full_name="Patrick Mahomes")])
    db.add_note("1", "first")
    db.close()

    reopened = Database.open(tmp_path / "notes.db")
    try:
        assert reopened.get_player("1")["full_name"] == "Patrick Mahomes"
        assert len(reopened.list_notes("1")) == 1
    finally:
        reopened.close()


def test_legacy_v1_db_is_wiped_on_open(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE players (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          team TEXT,
          position TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(name, team)
        );
        CREATE TABLE notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          player_id INTEGER NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO players (name, team, position, created_at)
          VALUES ('Old Player', 'KC', 'QB', '2024-01-01T00:00:00+00:00');
        INSERT INTO notes (player_id, body, created_at, updated_at)
          VALUES (1, 'old', '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        assert db.list_players() == []
        version_row = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version_row["value"]) == SCHEMA_VERSION
    finally:
        db.close()


# --- replace_players / players API ---


def test_replace_players_replaces_set(db):
    db.replace_players(
        [
            _player("1", full_name="A"),
            _player("2", full_name="B"),
            _player("3", full_name="C"),
        ]
    )
    db.replace_players(
        [
            _player("2", full_name="B"),
            _player("4", full_name="D"),
        ]
    )
    ids = {p["player_id"] for p in db.list_players()}
    assert ids == {"2", "4"}


def test_replace_players_is_transactional_on_failure(db):
    db.replace_players([_player("1", full_name="Original")])
    with pytest.raises(ValueError):
        db.replace_players(
            [
                _player("9", full_name="New"),
                _player("9", full_name="Dup PK"),
            ]
        )
    assert db.get_player("1")["full_name"] == "Original"


def test_replace_players_cascades_notes_for_missing_players(db):
    db.replace_players([_player("1"), _player("2")])
    db.add_note("1", "keep me")
    db.add_note("2", "drop me")
    db.replace_players([_player("1")])
    assert db.list_notes("1")[0]["body"] == "keep me"
    with pytest.raises(NotFoundError):
        db.list_notes("2")


def test_depth_chart_orders_with_unranked_last(db):
    db.replace_players(
        [
            _player("a", full_name="Backup", team="KC", depth_chart_position="QB", depth_chart_order=2),
            _player("b", full_name="Starter", team="KC", depth_chart_position="QB", depth_chart_order=1),
            _player("c", full_name="No Rank", team="KC", depth_chart_position=None, depth_chart_order=None),
            _player("d", full_name="WR1", team="KC", depth_chart_position="WR", depth_chart_order=1),
            _player("e", full_name="Other Team", team="BUF", depth_chart_position="QB", depth_chart_order=1),
        ]
    )
    chart = db.depth_chart("KC")
    names = [p["full_name"] for p in chart]
    assert names == ["Starter", "Backup", "WR1", "No Rank"]


def test_find_players_substring(db):
    db.replace_players(
        [
            _player("1", full_name="Patrick Mahomes"),
            _player("2", full_name="Travis Kelce"),
            _player("3", full_name="Patrick Surtain"),
        ]
    )
    results = db.find_players("patrick")
    assert {p["full_name"] for p in results} == {"Patrick Mahomes", "Patrick Surtain"}


def test_find_players_caps_at_limit(db):
    db.replace_players([_player(str(i), full_name=f"Player {i}") for i in range(20)])
    assert len(db.find_players("Player", limit=5)) == 5


# --- notes ---


def test_notes_attach_and_list_newest_first(db):
    db.replace_players([_player("1")])
    n1 = db.add_note("1", "first")
    n2 = db.add_note("1", "second")
    notes = db.list_notes("1")
    assert [n["id"] for n in notes] == [n2["id"], n1["id"]]


def test_add_note_unknown_player_raises(db):
    with pytest.raises(NotFoundError):
        db.add_note("does-not-exist", "x")


def test_update_and_delete_note(db):
    db.replace_players([_player("1")])
    n = db.add_note("1", "before")
    updated = db.update_note(n["id"], "after")
    assert updated["body"] == "after"
    db.delete_note(n["id"])
    with pytest.raises(NotFoundError):
        db.update_note(n["id"], "again")


# --- sync_runs ---


def test_sync_run_round_trip(db):
    rid = db.record_sync_start("https://example.test/players")
    finished = db.record_sync_finish(rid, players_written=42, status="success")
    assert finished["players_written"] == 42
    assert finished["status"] == "success"
    assert finished["finished_at"] is not None


def test_last_sync_returns_most_recent_including_errors(db):
    r1 = db.record_sync_start("u")
    db.record_sync_finish(r1, 10, "success")
    r2 = db.record_sync_start("u")
    db.record_sync_finish(r2, 0, "error", error="boom")
    last = db.last_sync()
    assert last["id"] == r2
    assert last["status"] == "error"
    assert last["error"] == "boom"


def test_last_sync_none_when_empty(db):
    assert db.last_sync() is None
