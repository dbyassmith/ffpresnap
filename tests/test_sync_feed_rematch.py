from __future__ import annotations

import json

import pytest

from ffpresnap.db import Database
from ffpresnap.feeds._base import FeedItem
from ffpresnap.feeds._registry import (
    _reset_registry_for_tests,
    register_adapter,
)
from ffpresnap.sync import build_feed_note_body, run_sync


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "notes.db")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _restore_registry():
    yield
    _reset_registry_for_tests()
    import importlib

    from ffpresnap.feeds import _32beatwriters as _bw

    importlib.reload(_bw)


def _seed(db, *, pid, name, team, position="WR"):
    db.conn.execute(
        "INSERT OR REPLACE INTO players ("
        "  player_id, full_name, team, position, fantasy_positions, "
        "  updated_at, watchlist, source"
        ") VALUES (?, ?, ?, ?, ?, ?, 0, 'sleeper')",
        (pid, name, team, position, json.dumps([position]), "2026-04-29T00:00:00Z"),
    )
    db.conn.commit()


def _item(ext_id, name, team_label, position):
    return FeedItem(
        external_id=ext_id,
        external_player_id="ext-" + ext_id,
        external_player_name=name,
        external_team=team_label,
        external_position=position,
        source_url=f"https://example.com/{ext_id}",
        source_author="Reporter",
        raw_html=f"<p>{ext_id}</p>",
        cleaned_text=ext_id,
        created_at="2026-04-29T12:00:00Z",
    )


class _FakeAdapter:
    name = "test-feed"
    source_url = "https://example.com"

    def __init__(self, items):
        self._items = items

    def fetch(self, *, full, fetch=None, is_seen=None):
        for it in self._items:
            yield it

    def map_team(self, external_team):
        return {
            "Minnesota Vikings": "MIN",
            "Dallas Cowboys": "DAL",
            "Prospect": None,
        }.get(external_team)


# --- back-match across sync types ---


def test_back_match_after_sleeper_sync_attaches_note(db):
    """Feed sync runs first while the player is unknown — item lands
    unmatched. Then a Sleeper sync introduces the player. The Sleeper
    sync's tail-of-sync back-match pass attaches the player to the feed
    item and writes the auto-note.
    """
    _reset_registry_for_tests()
    register_adapter(_FakeAdapter([_item("t:1", "Justin Jefferson", "Minnesota Vikings", "WR")]))

    # Step 1: feed sync. Player not in DB yet.
    s1 = run_sync(db, source="test-feed", full=True)
    assert s1["items_unmatched"] == 1
    fi = db.list_feed_items()[0]
    assert fi["player_id"] is None

    # Step 2: Sleeper sync brings the player in.
    payload = {
        "JJ1": {
            "player_id": "JJ1",
            "full_name": "Justin Jefferson",
            "team": "MIN",
            "position": "WR",
            "fantasy_positions": ["WR"],
        },
    }
    run_sync(db, source="sleeper", fetch=lambda url: payload, source_url="u")

    # Back-match attached and wrote the note.
    fi2 = db.list_feed_items()[0]
    assert fi2["player_id"] == "JJ1"
    notes = db.list_notes("JJ1")
    assert len(notes) == 1
    assert "t:1" in notes[0]["body"]


def test_back_match_after_feed_sync_attaches_late_match(db):
    """Two feed items, one matched and one unmatched. After the player for
    the second appears, a *second* feed sync's back-match should attach it.
    """
    _reset_registry_for_tests()
    register_adapter(
        _FakeAdapter(
            [
                _item("t:a", "Justin Jefferson", "Minnesota Vikings", "WR"),
                _item("t:b", "Future Player", "Dallas Cowboys", "WR"),
            ]
        )
    )
    _seed(db, pid="JJ1", name="Justin Jefferson", team="MIN")
    s1 = run_sync(db, source="test-feed", full=True)
    assert s1["items_matched"] == 1
    assert s1["items_unmatched"] == 1

    _seed(db, pid="FP1", name="Future Player", team="DAL")

    # Re-running the feed sync triggers the tail-of-sync back-match.
    s2 = run_sync(db, source="test-feed", full=True)
    assert s2["items_new"] == 0
    fi_b = next(i for i in db.list_feed_items() if i["external_id"] == "t:b")
    assert fi_b["player_id"] == "FP1"
    assert len(db.list_notes("FP1")) == 1


def test_back_match_window_excludes_old_items(db):
    """Items older than 30 days are excluded from the back-match scan."""
    _reset_registry_for_tests()
    register_adapter(_FakeAdapter([_item("t:old", "Old Guy", "Dallas Cowboys", "WR")]))
    s1 = run_sync(db, source="test-feed", full=True)
    assert s1["items_unmatched"] == 1

    # Force the row's ingested_at to 90 days ago.
    db.conn.execute(
        "UPDATE feed_items SET ingested_at = datetime('now', '-90 days')"
    )
    db.conn.commit()

    # Player appears now.
    _seed(db, pid="OG1", name="Old Guy", team="DAL")
    counters = db.rematch_recent_unmatched_feed_items(
        window_days=30, run_id=99, note_body_for=build_feed_note_body
    )
    assert counters["matched"] == 0


def test_unknown_team_string_skips_match_no_crash(db):
    """A nugget about a college prospect with team='Prospect' should land
    in feed_items unmatched and not crash the sync.
    """
    _reset_registry_for_tests()
    register_adapter(_FakeAdapter([_item("t:p", "College Kid", "Prospect", "WR")]))
    s = run_sync(db, source="test-feed", full=True)
    assert s["items_unmatched"] == 1
    assert s["status"] == "success"


def test_back_match_failure_does_not_fail_parent_sync(db, capsys):
    """If the back-match call raises, the parent sync should still record
    'success'. Patch the rematch fn to raise.
    """
    _reset_registry_for_tests()
    register_adapter(_FakeAdapter([_item("t:1", "Justin Jefferson", "Minnesota Vikings", "WR")]))
    _seed(db, pid="JJ1", name="Justin Jefferson", team="MIN")

    original = db.rematch_recent_unmatched_feed_items

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated rematch failure")

    db.rematch_recent_unmatched_feed_items = _boom  # type: ignore[assignment]
    try:
        s = run_sync(db, source="test-feed", full=True)
    finally:
        db.rematch_recent_unmatched_feed_items = original  # type: ignore[assignment]

    assert s["status"] == "success"
    err = capsys.readouterr().err
    assert "feed:rematch:error" in err


# --- Sleeper identity-merge fix ---


def test_sleeper_merge_preserves_feed_items_player_id(db):
    """When Sleeper sync identity-merges an Ourlads-only player_id into a
    Sleeper player_id (delete old pid, insert new pid in place), any
    feed_items bound to the old pid must be rewritten to the new pid —
    NOT silently nulled by the FK ON DELETE SET NULL.
    """
    # Seed an Ourlads-only row.
    db.conn.execute(
        "INSERT INTO players ("
        "  player_id, full_name, team, position, fantasy_positions, "
        "  updated_at, watchlist, source"
        ") VALUES ('OL1', 'Justin Jefferson', 'MIN', 'WR', ?, ?, 0, 'ourlads')",
        (json.dumps(["WR"]), "2026-04-29T00:00:00Z"),
    )
    db.conn.commit()
    # Bind a feed_items row to OL1.
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        {
            "external_id": "32bw:99",
            "external_player_id": "ext-99",
            "external_player_name": "Justin Jefferson",
            "external_team": "Minnesota Vikings",
            "external_position": "WR",
            "team_abbr": "MIN",
            "source_url": "u",
            "source_author": "r",
            "raw_html": "<p>x</p>",
            "cleaned_text": "x",
            "created_at": "2026-04-29T00:00:00Z",
        },
        player_id="OL1",
        note_body="Body",
        run_id=1,
    )

    # Now run a Sleeper sync that introduces 'JJ-S' for the same player.
    payload = {
        "JJ-S": {
            "player_id": "JJ-S",
            "full_name": "Justin Jefferson",
            "team": "MIN",
            "position": "WR",
            "fantasy_positions": ["WR"],
        },
    }
    run_sync(db, source="sleeper", fetch=lambda url: payload, source_url="u")

    # OL1 should be gone, JJ-S should exist as 'merged'.
    assert db.conn.execute(
        "SELECT 1 FROM players WHERE player_id = 'OL1'"
    ).fetchone() is None
    new_row = db.conn.execute(
        "SELECT source FROM players WHERE player_id = 'JJ-S'"
    ).fetchone()
    assert new_row is not None
    assert new_row["source"] == "merged"

    # The feed_items row was rewritten — not nulled.
    fi = db.list_feed_items()[0]
    assert fi["player_id"] == "JJ-S"

    # The auto-note still resolves to the right player.
    notes = db.list_notes("JJ-S")
    assert len(notes) == 1
    assert notes[0]["body"].startswith("Body")
