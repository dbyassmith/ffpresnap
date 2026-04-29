from __future__ import annotations

import json

import pytest

from ffpresnap.db import Database, NotFoundError


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "notes.db")
    yield d
    d.close()


def _seed_player(db, *, pid="X1", name="Justin Jefferson", team="MIN", position="WR"):
    """Insert a player directly via SQL — bypasses Sleeper-only validation
    paths in upsert_players_for_source so feed tests can pin a fixed pid.
    """
    db.conn.execute(
        "INSERT OR REPLACE INTO players ("
        "  player_id, full_name, team, position, fantasy_positions, "
        "  updated_at, watchlist, source"
        ") VALUES (?, ?, ?, ?, ?, ?, 0, 'sleeper')",
        (pid, name, team, position, json.dumps([position]), "2026-04-29T00:00:00Z"),
    )
    db.conn.commit()


def _item(**overrides):
    base = {
        "external_id": "32bw:42",
        "external_player_id": "299",
        "external_player_name": "Justin Jefferson",
        "external_team": "Minnesota Vikings",
        "external_position": "WR",
        "team_abbr": "MIN",
        "source_url": "https://example.com/article",
        "source_author": "Test Reporter",
        "raw_html": "<p>Hello.</p>",
        "cleaned_text": "Hello.",
        "created_at": "2026-04-29T12:00:00.000Z",
    }
    base.update(overrides)
    return base


# --- schema migration ---


def test_schema_version_is_8(db):
    row = db.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert int(row["value"]) == 8


def test_feed_sources_seeded(db):
    rows = db.conn.execute(
        "SELECT name, source_url FROM feed_sources ORDER BY name"
    ).fetchall()
    names = [r["name"] for r in rows]
    assert "32beatwriters" in names


def test_sync_runs_has_feed_counter_columns(db):
    cols = {
        c["name"]
        for c in db.conn.execute("PRAGMA table_info(sync_runs)").fetchall()
    }
    assert {"items_fetched", "items_new", "items_matched", "items_unmatched"} <= cols


def test_migration_from_v7_adds_columns_without_data_loss(tmp_path):
    """Open a fresh DB, simulate v7 by removing the new tables/columns and
    setting schema_version=7, close, reopen — Database.open() runs the v7→v8
    migration and rebuilds the new tables + columns idempotently.
    """
    path = tmp_path / "v7.db"
    db = Database.open(path)
    # Seed a player + a sync_run so we can verify nothing is lost.
    _seed_player(db, pid="ABC", name="Test Player")
    db.record_sync_start("https://api.example.com", source="sleeper")
    # Simulate v7: drop feed tables and feed counter columns, reset version.
    db.conn.execute("DROP TABLE IF EXISTS feed_items")
    db.conn.execute("DROP TABLE IF EXISTS feed_sources")
    # SQLite < 3.35 can't DROP COLUMN — instead, copy table without them.
    db.conn.executescript(
        """
        CREATE TABLE sync_runs_old AS SELECT
            id, started_at, finished_at, players_written, source_url,
            status, error, source FROM sync_runs;
        DROP TABLE sync_runs;
        ALTER TABLE sync_runs_old RENAME TO sync_runs;
        UPDATE meta SET value = '7' WHERE key = 'schema_version';
        """
    )
    db.conn.commit()
    db.close()

    db2 = Database.open(path)
    # Player still there.
    assert db2.get_player("ABC")["full_name"] == "Test Player"
    # New tables exist again.
    cols = {
        c["name"]
        for c in db2.conn.execute("PRAGMA table_info(feed_items)").fetchall()
    }
    assert "external_id" in cols
    # Counter columns are back.
    sync_cols = {
        c["name"]
        for c in db2.conn.execute("PRAGMA table_info(sync_runs)").fetchall()
    }
    assert "items_fetched" in sync_cols
    db2.close()


# --- upsert / idempotency ---


def test_add_feed_item_with_auto_note_happy_path(db):
    _seed_player(db, pid="X1", name="Justin Jefferson")
    result = db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(),
        player_id="X1",
        note_body="Body text",
        run_id=1,
    )
    assert result["was_new"] is True
    assert result["matched_now"] is True
    assert result["note_id"] is not None
    note_id = result["note_id"]

    # Note row exists, mention row exists, feed_items.note_id set, note_run_id set.
    note = db.conn.execute(
        "SELECT * FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    assert note["body"] == "Body text"
    assert note["subject_type"] == "player"
    assert note["subject_id"] == "X1"

    mention = db.conn.execute(
        "SELECT * FROM note_player_mentions WHERE note_id = ?", (note_id,)
    ).fetchone()
    assert mention["player_id"] == "X1"

    fi = db.conn.execute(
        "SELECT * FROM feed_items WHERE id = ?", (result["feed_item_id"],)
    ).fetchone()
    assert fi["note_id"] == note_id
    assert fi["note_run_id"] == 1
    assert fi["player_id"] == "X1"


def test_add_feed_item_idempotent_re_run_no_duplicates(db):
    _seed_player(db, pid="X1")
    r1 = db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id="X1", note_body="Body", run_id=1
    )
    assert r1["was_new"] is True

    r2 = db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id="X1", note_body="Body", run_id=2
    )
    assert r2["was_new"] is False
    assert r2["matched_now"] is False
    assert r2["note_id"] is None  # no second note

    # Only one feed_items row, only one notes row.
    assert (
        db.conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 1
    )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE subject_type='player' AND subject_id='X1'"
        ).fetchone()[0]
        == 1
    )


def test_unmatched_then_back_match_creates_note(db):
    # First insert: unmatched.
    r1 = db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(),
        player_id=None,
        note_body=None,
        run_id=1,
    )
    assert r1["was_new"] is True
    assert r1["matched_now"] is False
    assert r1["note_id"] is None

    # Player appears later.
    _seed_player(db, pid="X1", name="Justin Jefferson")

    # Second call (back-match): same external_id, now with player_id + note_body.
    r2 = db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(),
        player_id="X1",
        note_body="Late body",
        run_id=2,
    )
    assert r2["was_new"] is False
    assert r2["matched_now"] is True
    assert r2["note_id"] is not None

    fi = db.conn.execute(
        "SELECT * FROM feed_items WHERE id = ?", (r1["feed_item_id"],)
    ).fetchone()
    assert fi["player_id"] == "X1"
    assert fi["note_id"] == r2["note_id"]
    assert fi["note_run_id"] == 2


def test_existing_match_is_not_overwritten_by_null(db):
    _seed_player(db, pid="X1")
    db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id="X1", note_body="b", run_id=1
    )
    # Re-insert with player_id=None — should NOT clobber the existing match.
    r = db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id=None, note_body=None, run_id=2
    )
    assert r["was_new"] is False
    fi = db.conn.execute("SELECT * FROM feed_items WHERE id = ?", (r["feed_item_id"],)).fetchone()
    assert fi["player_id"] == "X1"


def test_unknown_feed_source_raises(db):
    with pytest.raises(NotFoundError):
        db.add_feed_item_with_auto_note("nonexistent-source", _item(), player_id=None)


# --- feed_item_exists ---


def test_feed_item_exists(db):
    assert db.feed_item_exists("32beatwriters", "32bw:42") is False
    db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id=None, note_body=None
    )
    assert db.feed_item_exists("32beatwriters", "32bw:42") is True
    assert db.feed_item_exists("32beatwriters", "32bw:99") is False


# --- list / filter ---


def test_list_feed_items_filters(db):
    _seed_player(db, pid="X1")
    _seed_player(db, pid="X2", name="Other Guy", team="DAL")
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(external_id="a", created_at="2026-04-01T00:00:00.000Z"),
        player_id="X1",
        note_body="b1",
    )
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(
            external_id="b",
            external_player_name="Other Guy",
            external_team="Dallas Cowboys",
            team_abbr="DAL",
            created_at="2026-04-15T00:00:00.000Z",
        ),
        player_id="X2",
        note_body="b2",
    )
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(external_id="c", created_at="2026-04-20T00:00:00.000Z"),
        player_id=None,  # unmatched
        note_body=None,
    )

    all_items = db.list_feed_items()
    assert len(all_items) == 3

    only_x1 = db.list_feed_items(player_id="X1")
    assert [i["external_id"] for i in only_x1] == ["a"]

    matched = db.list_feed_items(matched=True)
    assert {i["external_id"] for i in matched} == {"a", "b"}

    unmatched = db.list_feed_items(matched=False)
    assert [i["external_id"] for i in unmatched] == ["c"]

    since = db.list_feed_items(since="2026-04-10T00:00:00.000Z")
    assert {i["external_id"] for i in since} == {"b", "c"}


# --- find_unmatched_feed_items_since ---


def test_find_unmatched_feed_items_since_window(db):
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(external_id="recent", created_at="2026-04-29T00:00:00Z"),
        player_id=None,
        note_body=None,
    )
    # Force an old ingested_at.
    db.conn.execute(
        "UPDATE feed_items SET ingested_at = datetime('now', '-90 days') "
        "WHERE external_id = 'recent'"
    )
    db.conn.commit()
    out = db.find_unmatched_feed_items_since(window_days=30)
    assert out == []


# --- delete_feed_item cascade ---


def test_delete_feed_item_cascades_to_note(db):
    _seed_player(db, pid="X1")
    r = db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id="X1", note_body="b", run_id=1
    )
    note_id = r["note_id"]
    db.delete_feed_item(r["feed_item_id"])
    assert (
        db.conn.execute(
            "SELECT 1 FROM feed_items WHERE id = ?", (r["feed_item_id"],)
        ).fetchone()
        is None
    )
    assert (
        db.conn.execute(
            "SELECT 1 FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        is None
    )


def test_delete_feed_item_unknown_raises(db):
    with pytest.raises(NotFoundError):
        db.delete_feed_item(99999)


def test_deleting_note_leaves_feed_item_with_null_note_id(db):
    _seed_player(db, pid="X1")
    r = db.add_feed_item_with_auto_note(
        "32beatwriters", _item(), player_id="X1", note_body="b", run_id=1
    )
    db.delete_note(r["note_id"])
    fi = db.conn.execute(
        "SELECT * FROM feed_items WHERE id = ?", (r["feed_item_id"],)
    ).fetchone()
    assert fi is not None
    assert fi["note_id"] is None


# --- record_sync_finish counters ---


def test_record_sync_finish_with_feed_counters(db):
    run_id = db.record_sync_start("u", source="32beatwriters")
    finished = db.record_sync_finish(
        run_id,
        status="success",
        items_fetched=10,
        items_new=4,
        items_matched=3,
        items_unmatched=1,
    )
    assert finished["players_written"] is None
    assert finished["items_fetched"] == 10
    assert finished["items_new"] == 4
    assert finished["items_matched"] == 3
    assert finished["items_unmatched"] == 1


def test_record_sync_finish_legacy_player_path_unchanged(db):
    run_id = db.record_sync_start("u", source="sleeper")
    finished = db.record_sync_finish(run_id, 42, "success")
    assert finished["players_written"] == 42
    assert finished["items_fetched"] is None


# --- delete_auto_notes_from_run ---


def test_delete_auto_notes_from_run_bulk_rollback(db):
    _seed_player(db, pid="X1")
    _seed_player(db, pid="X2", name="Other Guy", team="DAL")
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(external_id="a"),
        player_id="X1",
        note_body="b1",
        run_id=10,
    )
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(
            external_id="b",
            external_player_name="Other Guy",
            external_team="Dallas Cowboys",
            team_abbr="DAL",
        ),
        player_id="X2",
        note_body="b2",
        run_id=10,
    )
    # Different run.
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        _item(external_id="c"),
        player_id="X1",
        note_body="b3",
        run_id=11,
    )

    # Wait — re-running for external_id 'a' would short-circuit. The third
    # call uses a new external_id; it should attach to run 11.

    deleted = db.delete_auto_notes_from_run(10)
    assert deleted == 2

    # The two run=10 feed_items survive with note_id=NULL.
    rows = db.conn.execute(
        "SELECT external_id, note_id FROM feed_items "
        "WHERE note_run_id = 10 OR external_id IN ('a','b') ORDER BY external_id"
    ).fetchall()
    for r in rows:
        assert r["note_id"] is None

    # The run=11 note is intact.
    fi_c = db.conn.execute(
        "SELECT note_id FROM feed_items WHERE external_id = 'c'"
    ).fetchone()
    assert fi_c["note_id"] is not None

    # Idempotent: second call against the same run returns 0.
    assert db.delete_auto_notes_from_run(10) == 0


def test_delete_auto_notes_from_run_non_feed_run_id_is_noop(db):
    run_id = db.record_sync_start("u", source="sleeper")
    db.record_sync_finish(run_id, 0, "success")
    assert db.delete_auto_notes_from_run(run_id) == 0


# --- transaction atomicity ---


def test_add_feed_item_with_auto_note_rolls_back_on_failure(db):
    """If any write inside add_feed_item_with_auto_note's transaction
    raises, the entire helper rolls back — no orphan feed_items row,
    no orphan note. Verifies the single-transaction guarantee documented
    in the plan's Key Decisions.

    Wraps the live sqlite3.Connection in a proxy that injects a failure
    on the post-note feed_items UPDATE (the latest write inside the
    transaction). sqlite3.Connection.execute is read-only on the C
    object, so we can't monkeypatch it directly — replacing db.conn with
    a proxy works because Database accesses self.conn dynamically.
    """
    _seed_player(db, pid="X1")
    real_conn = db.conn

    class _ExplodingProxy:
        def __init__(self, target):
            self._target = target

        def execute(self, sql, *args, **kwargs):
            if "UPDATE feed_items SET note_id" in sql:
                raise RuntimeError("simulated mid-transaction failure")
            return self._target.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._target, name)

    db.conn = _ExplodingProxy(real_conn)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            db.add_feed_item_with_auto_note(
                "32beatwriters",
                _item(),
                player_id="X1",
                note_body="b",
                run_id=1,
            )
    finally:
        db.conn = real_conn

    # No feed_items row, no notes row — full rollback.
    assert (
        db.conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 0
    )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM notes WHERE subject_type='player' AND subject_id='X1'"
        ).fetchone()[0]
        == 0
    )
