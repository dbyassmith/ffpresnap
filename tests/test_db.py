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


def test_v2_db_migrates_player_notes_to_polymorphic(tmp_path):
    """A v2 DB (legacy notes table with player_id FK) should upgrade to v3 in place,
    preserving existing player notes under the new (subject_type, subject_id) shape.
    """
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '2');

        CREATE TABLE players (
          player_id TEXT PRIMARY KEY,
          full_name TEXT,
          team TEXT,
          position TEXT,
          updated_at TEXT NOT NULL
        );
        INSERT INTO players (player_id, full_name, team, position, updated_at)
          VALUES ('99', 'Carryover Player', 'KC', 'QB', '2026-04-01T00:00:00+00:00');

        CREATE TABLE notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          player_id TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO notes (player_id, body, created_at, updated_at)
          VALUES ('99', 'preserved', '2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        version = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION

        # Verify the note migrated to the polymorphic shape directly via SQL —
        # the fake v2 players table is intentionally minimal so we don't go
        # through list_notes (which expects the full v3 player columns).
        rows = db.conn.execute(
            "SELECT subject_type, subject_id, body FROM notes WHERE id = 1"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["subject_type"] == "player"
        assert rows[0]["subject_id"] == "99"
        assert rows[0]["body"] == "preserved"
    finally:
        db.close()


def test_v5_db_migrates_to_v6_adds_watchlist_column(tmp_path):
    """Opening a v5 DB upgrades to v6 by adding `watchlist INTEGER NOT NULL
    DEFAULT 0` to players. Existing rows get 0; existing data is otherwise
    untouched.
    """
    db_path = tmp_path / "v5.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '5');
        CREATE TABLE players (
          player_id TEXT PRIMARY KEY,
          full_name TEXT,
          team TEXT,
          position TEXT,
          updated_at TEXT NOT NULL
        );
        INSERT INTO players (player_id, full_name, team, position, updated_at)
          VALUES ('99', 'Holdover', 'KC', 'QB', '2026-04-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        version = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION

        cols = db.conn.execute("PRAGMA table_info(players)").fetchall()
        assert any(c["name"] == "watchlist" for c in cols), "watchlist column missing"

        row = db.conn.execute(
            "SELECT watchlist FROM players WHERE player_id = '99'"
        ).fetchone()
        assert row["watchlist"] == 0
    finally:
        db.close()


def test_v6_db_migrates_to_v7_adds_source_columns(tmp_path):
    """Opening a v6 DB upgrades to v7 by adding `source`, `ourlads_id`,
    `depth_chart_last_observed_at` to players and `source` to sync_runs.
    Existing rows backfill to source='sleeper'; nullable columns stay NULL.
    """
    db_path = tmp_path / "v6.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '6');
        CREATE TABLE players (
          player_id TEXT PRIMARY KEY,
          full_name TEXT,
          team TEXT,
          position TEXT,
          updated_at TEXT NOT NULL,
          watchlist INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO players (player_id, full_name, team, position, updated_at)
          VALUES ('99', 'Holdover', 'KC', 'QB', '2026-04-01T00:00:00+00:00');
        CREATE TABLE sync_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          players_written INTEGER,
          source_url TEXT NOT NULL,
          status TEXT NOT NULL,
          error TEXT
        );
        INSERT INTO sync_runs (started_at, source_url, status)
          VALUES ('2026-04-01T00:00:00+00:00', 'https://example.com', 'success');
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        version = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION

        player_cols = {
            c["name"] for c in db.conn.execute("PRAGMA table_info(players)").fetchall()
        }
        assert "source" in player_cols
        assert "ourlads_id" in player_cols
        assert "depth_chart_last_observed_at" in player_cols

        sync_cols = {
            c["name"]
            for c in db.conn.execute("PRAGMA table_info(sync_runs)").fetchall()
        }
        assert "source" in sync_cols

        player_row = db.conn.execute(
            "SELECT source, ourlads_id, depth_chart_last_observed_at "
            "FROM players WHERE player_id = '99'"
        ).fetchone()
        assert player_row["source"] == "sleeper"
        assert player_row["ourlads_id"] is None
        assert player_row["depth_chart_last_observed_at"] is None

        sync_row = db.conn.execute(
            "SELECT source FROM sync_runs WHERE source_url = 'https://example.com'"
        ).fetchone()
        assert sync_row["source"] == "sleeper"
    finally:
        db.close()


def test_v7_db_idempotent_reopen(tmp_path):
    """Reopening an already-v7 DB does not error and does not re-add columns."""
    db_path = tmp_path / "v7.db"
    db = Database.open(db_path)
    try:
        # Capture initial column counts.
        before_players = db.conn.execute("PRAGMA table_info(players)").fetchall()
        before_sync = db.conn.execute("PRAGMA table_info(sync_runs)").fetchall()
    finally:
        db.close()

    # Reopen and confirm nothing changed.
    db = Database.open(db_path)
    try:
        after_players = db.conn.execute("PRAGMA table_info(players)").fetchall()
        after_sync = db.conn.execute("PRAGMA table_info(sync_runs)").fetchall()
        assert len(after_players) == len(before_players)
        assert len(after_sync) == len(before_sync)
    finally:
        db.close()


def test_v6_to_v7_partial_prior_state_backfills_null_source(tmp_path):
    """Defends against a partial prior migration that left NULL `source` values:
    the v6→v7 arm runs `UPDATE players SET source='sleeper' WHERE source IS NULL`
    unconditionally so the post-migration table is NULL-free.
    """
    db_path = tmp_path / "v6_partial.db"
    conn = sqlite3.connect(str(db_path))
    # Simulate a prior aborted migration: column exists but was added without a
    # default, leaving an existing row with NULL source.
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '6');
        CREATE TABLE players (
          player_id TEXT PRIMARY KEY,
          full_name TEXT,
          team TEXT,
          position TEXT,
          updated_at TEXT NOT NULL,
          watchlist INTEGER NOT NULL DEFAULT 0,
          source TEXT
        );
        INSERT INTO players (player_id, full_name, team, position, updated_at, source)
          VALUES ('99', 'Half-Migrated', 'KC', 'QB', '2026-04-01T00:00:00+00:00', NULL);
        CREATE TABLE sync_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          players_written INTEGER,
          source_url TEXT NOT NULL,
          status TEXT NOT NULL,
          error TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        row = db.conn.execute(
            "SELECT source FROM players WHERE player_id = '99'"
        ).fetchone()
        assert row["source"] == "sleeper"
    finally:
        db.close()


def test_v4_db_migrates_to_v5_preserving_data(tmp_path):
    """A v4 DB should upgrade to v5 in place: existing data untouched, the new
    `prompts` table exists, and schema_version becomes 5.
    """
    db_path = tmp_path / "v4.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '4');
        CREATE TABLE notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject_type TEXT NOT NULL CHECK (subject_type IN ('player', 'team', 'study')),
          subject_id TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO notes (subject_type, subject_id, body, created_at, updated_at)
          VALUES ('team', 'KC', 'preserved', '2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        version = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION

        rows = db.conn.execute(
            "SELECT body FROM notes WHERE subject_type='team' AND subject_id='KC'"
        ).fetchall()
        assert [r["body"] for r in rows] == ["preserved"]

        assert db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prompts'"
        ).fetchone() is not None
    finally:
        db.close()


def test_v3_db_migrates_to_v4_preserving_notes(tmp_path):
    """A v3 DB should upgrade to v4 in place: notes survive (same id, body, subject),
    studies + mentions tables exist, schema_version becomes 4, and the rebuilt
    notes table now accepts subject_type='study'.
    """
    db_path = tmp_path / "v3.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '3');

        CREATE TABLE players (
          player_id TEXT PRIMARY KEY,
          full_name TEXT,
          team TEXT,
          position TEXT,
          updated_at TEXT NOT NULL
        );
        INSERT INTO players (player_id, full_name, team, position, updated_at)
          VALUES ('99', 'Carryover', 'KC', 'QB', '2026-04-01T00:00:00+00:00');

        CREATE TABLE teams (
          abbr TEXT PRIMARY KEY,
          full_name TEXT NOT NULL,
          conference TEXT NOT NULL,
          division TEXT NOT NULL
        );

        CREATE TABLE notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject_type TEXT NOT NULL CHECK (subject_type IN ('player', 'team')),
          subject_id TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO notes (id, subject_type, subject_id, body, created_at, updated_at)
          VALUES
            (1, 'player', '99', 'preserved player note',
             '2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00'),
            (2, 'team',   'KC', 'preserved team note',
             '2026-04-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database.open(db_path)
    try:
        version = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION

        rows = db.conn.execute(
            "SELECT id, subject_type, subject_id, body FROM notes ORDER BY id"
        ).fetchall()
        bodies = [(r["id"], r["subject_type"], r["subject_id"], r["body"]) for r in rows]
        assert bodies == [
            (1, "player", "99", "preserved player note"),
            (2, "team", "KC", "preserved team note"),
        ]

        for table in ("studies", "note_player_mentions", "note_team_mentions"):
            assert db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is not None, f"missing table {table}"

        # The rebuilt CHECK now allows subject_type='study'.
        db.conn.execute(
            "INSERT INTO notes (subject_type, subject_id, body, created_at, updated_at) "
            "VALUES ('study', '1', 'sanity', '2026-04-26T00:00:00+00:00', '2026-04-26T00:00:00+00:00')"
        )
        db.conn.commit()
    finally:
        db.close()


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


# --- team notes ---


def test_team_notes_round_trip(db):
    n1 = db.add_team_note("KC", "AFC West favorite")
    n2 = db.add_team_note("Chiefs", "Strong O-line")
    notes = db.list_team_notes("Kansas City Chiefs")
    assert [n["id"] for n in notes] == [n2["id"], n1["id"]]
    assert all(n["subject_type"] == "team" and n["subject_id"] == "KC" for n in notes)


def test_team_notes_unknown_team_raises(db):
    with pytest.raises(NotFoundError):
        db.add_team_note("Foobar", "x")
    with pytest.raises(NotFoundError):
        db.list_team_notes("Foobar")


def test_team_notes_dont_leak_into_player_notes(db):
    db.replace_players([_player("1")])
    db.add_team_note("KC", "team-level")
    db.add_note("1", "player-level")
    player_notes = db.list_notes("1")
    assert len(player_notes) == 1
    assert player_notes[0]["body"] == "player-level"
    team_notes = db.list_team_notes("KC")
    assert len(team_notes) == 1
    assert team_notes[0]["body"] == "team-level"


def test_list_recent_notes_returns_chronological_feed(db):
    db.replace_players([_player("1", full_name="Patrick Mahomes", team="KC")])
    db.add_note("1", "first player note")
    db.add_team_note("KC", "team note")
    db.add_note("1", "latest player note")

    feed = db.list_recent_notes()
    bodies = [n["body"] for n in feed]
    assert bodies == ["latest player note", "team note", "first player note"]

    player_note = next(n for n in feed if n["body"] == "first player note")
    assert player_note["subject"]["type"] == "player"
    assert player_note["subject"]["full_name"] == "Patrick Mahomes"
    assert player_note["subject"]["team"] == "KC"

    team_note = next(n for n in feed if n["body"] == "team note")
    assert team_note["subject"] == {
        "type": "team",
        "abbr": "KC",
        "full_name": "Kansas City Chiefs",
    }


def test_list_recent_notes_respects_limit(db):
    db.replace_players([_player("1")])
    for i in range(5):
        db.add_note("1", f"note {i}")
    assert len(db.list_recent_notes(limit=3)) == 3


def test_list_recent_notes_empty_when_no_notes(db):
    assert db.list_recent_notes() == []


def test_list_recent_notes_attaches_mentions_and_covers_studies(db):
    db.replace_players(
        [_player("1", full_name="Mahomes"), _player("2", full_name="Allen", team="BUF")]
    )
    s = db.create_study("RB Handcuffs")
    db.add_note("1", "p note", mentions={"player_ids": ["2"], "team_abbrs": ["BUF"]})
    db.add_team_note("KC", "t note")
    db.add_study_note(s["id"], "s note", mentions={"player_ids": ["1"]})

    feed = db.list_recent_notes()
    types = [n["subject"]["type"] for n in feed]
    assert types == ["study", "team", "player"]

    study_note = feed[0]
    assert study_note["subject"]["title"] == "RB Handcuffs"
    assert study_note["subject"]["status"] == "open"
    assert [p["full_name"] for p in study_note["mentions"]["players"]] == ["Mahomes"]

    player_note = feed[2]
    assert [p["full_name"] for p in player_note["mentions"]["players"]] == ["Allen"]
    assert [t["abbr"] for t in player_note["mentions"]["teams"]] == ["BUF"]

    team_note = feed[1]
    assert team_note["mentions"] == {"players": [], "teams": []}


# --- studies ---


def test_create_and_get_study(db):
    s = db.create_study("RB Handcuffs", description="who to draft late")
    assert s["id"] > 0
    assert s["title"] == "RB Handcuffs"
    assert s["status"] == "open"
    assert s["description"] == "who to draft late"
    assert db.get_study(s["id"]) == s


def test_create_study_requires_title(db):
    with pytest.raises(ValueError):
        db.create_study("   ")


def test_list_studies_default_open_only(db):
    a = db.create_study("Open A")
    b = db.create_study("Will archive")
    db.set_study_status(b["id"], "archived")
    open_ids = [s["id"] for s in db.list_studies()]
    assert open_ids == [a["id"]]
    archived_ids = [s["id"] for s in db.list_studies(status="archived")]
    assert archived_ids == [b["id"]]
    all_ids = {s["id"] for s in db.list_studies(status=None)}
    assert all_ids == {a["id"], b["id"]}


def test_update_study_partial(db):
    s = db.create_study("Original")
    updated = db.update_study(s["id"], title="Renamed")
    assert updated["title"] == "Renamed"
    assert updated["description"] == s["description"]
    again = db.update_study(s["id"], description="now described")
    assert again["title"] == "Renamed"
    assert again["description"] == "now described"


def test_update_study_no_args_is_noop(db):
    s = db.create_study("Static")
    assert db.update_study(s["id"]) == s


def test_set_study_status_invalid_raises(db):
    s = db.create_study("x")
    with pytest.raises(ValueError):
        db.set_study_status(s["id"], "closed")


def test_get_study_unknown_raises(db):
    with pytest.raises(NotFoundError):
        db.get_study(9999)


def test_delete_study_cascades_notes_and_mentions(db):
    db.replace_players([_player("1")])
    s = db.create_study("To delete")
    n = db.add_study_note(s["id"], "note body", mentions={"player_ids": ["1"]})
    db.delete_study(s["id"])
    with pytest.raises(NotFoundError):
        db.get_study(s["id"])
    # The note row is gone.
    row = db.conn.execute("SELECT * FROM notes WHERE id = ?", (n["id"],)).fetchone()
    assert row is None
    # And its mention rows are gone (FK cascade from notes).
    rows = db.conn.execute(
        "SELECT * FROM note_player_mentions WHERE note_id = ?", (n["id"],)
    ).fetchall()
    assert rows == []


def test_add_study_note_unknown_study_raises(db):
    with pytest.raises(NotFoundError):
        db.add_study_note(9999, "x")


# --- mentions on player/team notes ---


def test_add_note_with_mentions(db):
    db.replace_players(
        [
            _player("1", full_name="Mahomes"),
            _player("2", full_name="Allen", team="BUF"),
        ]
    )
    note = db.add_note(
        "1",
        "compare arms",
        mentions={"player_ids": ["2"], "team_abbrs": ["Buffalo Bills"]},
    )
    # Team identifier is permissive; stored value is canonical abbr.
    assert [p["full_name"] for p in note["mentions"]["players"]] == ["Allen"]
    assert [t["abbr"] for t in note["mentions"]["teams"]] == ["BUF"]


def test_add_note_dedupes_mentions(db):
    db.replace_players([_player("1"), _player("2")])
    note = db.add_note(
        "1", "x", mentions={"player_ids": ["2", "2", "2"], "team_abbrs": ["KC", "KC"]}
    )
    assert len(note["mentions"]["players"]) == 1
    assert len(note["mentions"]["teams"]) == 1


def test_add_note_unknown_mention_rolls_back(db):
    db.replace_players([_player("1")])
    before = db.list_notes("1")
    with pytest.raises(NotFoundError):
        db.add_note("1", "x", mentions={"player_ids": ["nope"]})
    assert db.list_notes("1") == before


def test_add_note_unknown_team_mention_rolls_back(db):
    db.replace_players([_player("1")])
    with pytest.raises(NotFoundError):
        db.add_note("1", "x", mentions={"team_abbrs": ["Foobar"]})
    assert db.list_notes("1") == []


def test_add_note_ambiguous_team_mention_rolls_back(db):
    from ffpresnap.db import AmbiguousTeamError

    db.replace_players([_player("1")])
    with pytest.raises(AmbiguousTeamError):
        db.add_note("1", "x", mentions={"team_abbrs": ["New York"]})
    assert db.list_notes("1") == []


def test_team_note_with_mentions(db):
    db.replace_players([_player("1", full_name="Mahomes")])
    note = db.add_team_note("KC", "preview", mentions={"player_ids": ["1"]})
    assert [p["full_name"] for p in note["mentions"]["players"]] == ["Mahomes"]


def test_update_note_replaces_mentions(db):
    db.replace_players(
        [_player("1", full_name="A"), _player("2", full_name="B"), _player("3", full_name="C")]
    )
    n = db.add_note("1", "v1", mentions={"player_ids": ["2"]})
    updated = db.update_note(n["id"], "v2", mentions={"player_ids": ["3"]})
    assert [p["full_name"] for p in updated["mentions"]["players"]] == ["C"]


def test_update_note_without_mentions_leaves_them(db):
    db.replace_players([_player("1"), _player("2")])
    n = db.add_note("1", "v1", mentions={"player_ids": ["2"]})
    updated = db.update_note(n["id"], "v2")
    assert [p["player_id"] for p in updated["mentions"]["players"]] == ["2"]


def test_update_note_empty_mentions_clears(db):
    db.replace_players([_player("1"), _player("2")])
    n = db.add_note("1", "v1", mentions={"player_ids": ["2"]})
    updated = db.update_note(
        n["id"], "v2", mentions={"player_ids": [], "team_abbrs": []}
    )
    assert updated["mentions"] == {"players": [], "teams": []}


def test_update_note_invalid_mention_does_not_change_note(db):
    db.replace_players([_player("1"), _player("2")])
    n = db.add_note("1", "before", mentions={"player_ids": ["2"]})
    with pytest.raises(NotFoundError):
        db.update_note(n["id"], "after", mentions={"player_ids": ["nope"]})
    # Body and mentions unchanged.
    rows = db.list_notes("1")
    assert rows[0]["body"] == "before"
    assert [p["player_id"] for p in rows[0]["mentions"]["players"]] == ["2"]


def test_list_player_mentions_excludes_primary_subject(db):
    db.replace_players([_player("1"), _player("2")])
    primary = db.add_note("1", "about player 1")
    cross = db.add_note("2", "mentions 1", mentions={"player_ids": ["1"]})
    mentions = db.list_player_mentions("1")
    ids = [n["id"] for n in mentions]
    assert cross["id"] in ids
    assert primary["id"] not in ids


def test_list_team_mentions_excludes_primary_subject(db):
    db.replace_players([_player("1")])
    primary = db.add_team_note("KC", "about KC")
    cross = db.add_note("1", "mentions KC", mentions={"team_abbrs": ["KC"]})
    mentions = db.list_team_mentions("KC")
    ids = [n["id"] for n in mentions]
    assert cross["id"] in ids
    assert primary["id"] not in ids


def test_player_removal_cascades_mentions(db):
    db.replace_players([_player("1"), _player("2")])
    db.add_note("1", "x", mentions={"player_ids": ["2"]})
    db.replace_players([_player("1")])  # drop player 2
    # The note about player 1 still exists, but its mention of player 2 is gone.
    notes = db.list_notes("1")
    assert notes[0]["mentions"]["players"] == []


# --- watchlist ---


def test_new_player_defaults_to_watchlist_false(db):
    db.replace_players([_player("1")])
    p = db.get_player("1")
    assert p["watchlist"] is False


def test_set_watchlist_toggles(db):
    db.replace_players([_player("1")])
    on = db.set_watchlist("1", True)
    assert on["watchlist"] is True
    off = db.set_watchlist("1", False)
    assert off["watchlist"] is False


def test_set_watchlist_unknown_player_raises(db):
    with pytest.raises(NotFoundError):
        db.set_watchlist("nope", True)


def test_watchlist_preserved_across_replace_players(db):
    db.replace_players([_player("1", full_name="Original")])
    db.set_watchlist("1", True)
    # Sleeper sync re-runs with updated metadata; watchlist must survive.
    db.replace_players([_player("1", full_name="Updated Name")])
    p = db.get_player("1")
    assert p["full_name"] == "Updated Name"
    assert p["watchlist"] is True


def test_list_players_watchlist_filter(db):
    db.replace_players(
        [_player("1", full_name="A"), _player("2", full_name="B"), _player("3", full_name="C")]
    )
    db.set_watchlist("1", True)
    db.set_watchlist("3", True)
    on = db.list_players(watchlist=True)
    off = db.list_players(watchlist=False)
    assert {p["player_id"] for p in on} == {"1", "3"}
    assert {p["player_id"] for p in off} == {"2"}
    # Unfiltered returns everyone.
    assert len(db.list_players()) == 3


# --- prompts ---


def _fake_prompt(slug, body="body"):
    return {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "description": f"{slug} description",
        "body": body,
    }


def test_seed_prompts_inserts_from_loader(db):
    db._seed_prompts(loader=lambda: [_fake_prompt("alpha"), _fake_prompt("beta")])
    rows = db.list_prompts()
    assert {r["slug"] for r in rows} == {"alpha", "beta"}


def test_seed_prompts_is_idempotent(db):
    loader = lambda: [_fake_prompt("alpha")]
    db._seed_prompts(loader=loader)
    db._seed_prompts(loader=loader)
    rows = db.list_prompts()
    assert [r["slug"] for r in rows] == ["alpha"]


def test_seed_prompts_updates_changed_body(db):
    db._seed_prompts(loader=lambda: [_fake_prompt("alpha", body="v1")])
    db._seed_prompts(loader=lambda: [_fake_prompt("alpha", body="v2")])
    rows = db.list_prompts()
    assert rows[0]["body"] == "v2"


def test_seed_prompts_removes_dropped_slugs(db):
    db._seed_prompts(
        loader=lambda: [_fake_prompt("alpha"), _fake_prompt("beta")]
    )
    db._seed_prompts(loader=lambda: [_fake_prompt("alpha")])
    rows = db.list_prompts()
    assert [r["slug"] for r in rows] == ["alpha"]


def test_seed_prompts_empty_loader_clears_table(db):
    db._seed_prompts(loader=lambda: [_fake_prompt("alpha")])
    db._seed_prompts(loader=lambda: [])
    assert db.list_prompts() == []


def test_seed_prompts_propagates_loader_errors(db):
    def bad_loader():
        raise ValueError("malformed prompt")

    with pytest.raises(ValueError, match="malformed"):
        db._seed_prompts(loader=bad_loader)


def test_list_prompts_orders_show_prompt_library_first(db):
    db._seed_prompts(
        loader=lambda: [
            _fake_prompt("study-browser"),
            _fake_prompt("show-prompt-library"),
            _fake_prompt("depth-chart-explorer"),
        ]
    )
    rows = db.list_prompts()
    assert [r["slug"] for r in rows] == [
        "show-prompt-library",
        "depth-chart-explorer",
        "study-browser",
    ]


def test_note_delete_cleans_mention_rows(db):
    db.replace_players([_player("1"), _player("2")])
    n = db.add_note("1", "x", mentions={"player_ids": ["2"], "team_abbrs": ["KC"]})
    db.delete_note(n["id"])
    rows = db.conn.execute(
        "SELECT * FROM note_player_mentions WHERE note_id = ?", (n["id"],)
    ).fetchall()
    assert rows == []
    rows = db.conn.execute(
        "SELECT * FROM note_team_mentions WHERE note_id = ?", (n["id"],)
    ).fetchall()
    assert rows == []


def test_update_and_delete_works_across_note_types(db):
    db.replace_players([_player("1")])
    pn = db.add_note("1", "player")
    tn = db.add_team_note("KC", "team")
    db.update_note(pn["id"], "player updated")
    db.update_note(tn["id"], "team updated")
    assert db.list_notes("1")[0]["body"] == "player updated"
    assert db.list_team_notes("KC")[0]["body"] == "team updated"
    db.delete_note(tn["id"])
    assert db.list_team_notes("KC") == []
    assert len(db.list_notes("1")) == 1


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
