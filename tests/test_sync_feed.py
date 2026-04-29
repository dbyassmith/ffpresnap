from __future__ import annotations

import json

import pytest

from ffpresnap.db import Database
from ffpresnap.feeds._base import FeedFetchError, FeedItem
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
    """Reset registry around each test so the fake adapter doesn't leak,
    then re-register the real 32beatwriters adapter for downstream tests.
    """
    yield
    _reset_registry_for_tests()
    import importlib

    from ffpresnap.feeds import _32beatwriters as _bw

    importlib.reload(_bw)


def _seed(db, *, pid="X1", name="Justin Jefferson", team="MIN", position="WR"):
    db.conn.execute(
        "INSERT OR REPLACE INTO players ("
        "  player_id, full_name, team, position, fantasy_positions, "
        "  updated_at, watchlist, source"
        ") VALUES (?, ?, ?, ?, ?, ?, 0, 'sleeper')",
        (pid, name, team, position, json.dumps([position]), "2026-04-29T00:00:00Z"),
    )
    db.conn.commit()


def _make_item(ext_id, name, team_label, position):
    return FeedItem(
        external_id=ext_id,
        external_player_id=ext_id.split(":")[-1],
        external_player_name=name,
        external_team=team_label,
        external_position=position,
        source_url=f"https://example.com/{ext_id}",
        source_author="Test Reporter",
        raw_html=f"<p>Body of {ext_id}.</p>",
        cleaned_text=f"Body of {ext_id}.",
        created_at="2026-04-29T12:00:00Z",
    )


class _FakeAdapter:
    """Fixture-backed fake adapter. ``pages`` is a list of lists of FeedItem;
    each inner list represents one page in newest-first order.
    """

    name = "test-feed"
    source_url = "https://example.com"

    def __init__(self, pages: list[list[FeedItem]], *, fail_after: int | None = None):
        self.pages = pages
        self.fail_after = fail_after
        self._yielded = 0

    def fetch(self, *, full, fetch=None, is_seen=None):
        for idx, page in enumerate(self.pages):
            page_all_seen = True
            for item in page:
                if self.fail_after is not None and self._yielded >= self.fail_after:
                    raise FeedFetchError(f"simulated failure on page {idx + 1}")
                yield item
                self._yielded += 1
                if is_seen is not None and not is_seen(item.external_id):
                    page_all_seen = False
                else:
                    if is_seen is None:
                        page_all_seen = False
            if not full and is_seen is not None and page_all_seen and idx > 0:
                break

    def map_team(self, external_team):
        return {
            "Minnesota Vikings": "MIN",
            "Dallas Cowboys": "DAL",
            "Arizona Cardinals": "ARI",
        }.get(external_team)


def _register_fake(pages, **kwargs):
    _reset_registry_for_tests()
    adapter = _FakeAdapter(pages, **kwargs)
    register_adapter(adapter)
    return adapter


# --- happy paths ---


def test_feed_sync_happy_path_writes_items_and_notes(db):
    _seed(db, pid="X1", name="Justin Jefferson")
    items = [_make_item("t:1", "Justin Jefferson", "Minnesota Vikings", "WR")]
    _register_fake([items])

    summary = run_sync(db, source="test-feed", full=True)
    assert summary["status"] == "success"
    assert summary["items_fetched"] == 1
    assert summary["items_new"] == 1
    assert summary["items_matched"] == 1
    assert summary["items_unmatched"] == 0

    feed_items = db.list_feed_items()
    assert len(feed_items) == 1
    assert feed_items[0]["player_id"] == "X1"

    notes = db.list_notes("X1")
    assert len(notes) == 1
    assert "Body of t:1." in notes[0]["body"]
    assert "— Test Reporter" in notes[0]["body"]


def test_feed_sync_unmatched_player_stays_unmatched(db):
    items = [_make_item("t:2", "Unknown Rookie", "Prospect", "WR")]
    _register_fake([items])

    summary = run_sync(db, source="test-feed", full=True)
    assert summary["items_matched"] == 0
    assert summary["items_unmatched"] == 1
    feed_items = db.list_feed_items(matched=False)
    assert len(feed_items) == 1
    assert feed_items[0]["player_id"] is None


def test_feed_sync_idempotent_re_run(db):
    _seed(db, pid="X1")
    items = [_make_item("t:1", "Justin Jefferson", "Minnesota Vikings", "WR")]
    _register_fake([items])

    s1 = run_sync(db, source="test-feed", full=True)
    s2 = run_sync(db, source="test-feed", full=True)
    assert s1["items_new"] == 1
    assert s2["items_new"] == 0
    assert len(db.list_notes("X1")) == 1


def test_feed_sync_full_walks_all_pages(db):
    _seed(db, pid="X1")
    _seed(db, pid="X2", name="Other Guy", team="DAL")
    page1 = [_make_item("t:p1a", "Justin Jefferson", "Minnesota Vikings", "WR")]
    page2 = [_make_item("t:p2a", "Other Guy", "Dallas Cowboys", "WR")]
    _register_fake([page1, page2])

    summary = run_sync(db, source="test-feed", full=True)
    assert summary["items_fetched"] == 2
    assert summary["items_new"] == 2


def test_feed_sync_incremental_stops_after_fully_seen_page(db):
    _seed(db, pid="X1")
    _seed(db, pid="X2", name="Other Guy", team="DAL")
    page1 = [_make_item("t:p1a", "Justin Jefferson", "Minnesota Vikings", "WR")]
    page2 = [_make_item("t:p2a", "Other Guy", "Dallas Cowboys", "WR")]

    # First sync: full walk to backfill both pages.
    _register_fake([page1, page2])
    s1 = run_sync(db, source="test-feed", full=True)
    assert s1["items_new"] == 2

    # Second sync: incremental — page 1 has only known items, then page 2
    # is also fully seen, so the adapter stops. items_fetched > 0 because
    # the adapter still yielded page 1's items, but items_new == 0.
    _register_fake([page1, page2])
    s2 = run_sync(db, source="test-feed", full=False)
    assert s2["items_new"] == 0


def test_feed_sync_records_concurrent_sync_error(db):
    _seed(db, pid="X1")
    items = [_make_item("t:1", "Justin Jefferson", "Minnesota Vikings", "WR")]
    _register_fake([items])

    db.record_sync_start("https://example.com", source="test-feed")  # locks
    from ffpresnap.db import ConcurrentSyncError

    with pytest.raises(ConcurrentSyncError):
        run_sync(db, source="test-feed", full=True)


def test_feed_sync_fetch_error_records_error_status(db):
    items_p1 = [_make_item("t:p1a", "Justin Jefferson", "Minnesota Vikings", "WR")]
    items_p2 = [_make_item("t:p2a", "Other Guy", "Dallas Cowboys", "WR")]
    _register_fake([items_p1, items_p2], fail_after=1)
    _seed(db, pid="X1")

    with pytest.raises(FeedFetchError):
        run_sync(db, source="test-feed", full=True)

    last = db.last_sync(source="test-feed")
    assert last["status"] == "error"
    assert "simulated failure" in (last["error"] or "")
    # Items already yielded before the failure survive.
    assert len(db.list_feed_items()) == 1


def test_unknown_source_raises_value_error(db):
    with pytest.raises(ValueError):
        run_sync(db, source="not-a-source")


def test_feed_sync_ambiguous_match_leaves_unmatched(db):
    """Two players in the same team+position with the same normalized
    name → orchestrator's `len == 1` check fails, item stays unmatched.
    No note is written, no crash. Mirrors Ourlads' ambiguous-match
    posture (db.py:805-817).
    """
    _seed(db, pid="A1", name="John Smith", team="MIN", position="WR")
    _seed(db, pid="A2", name="John Smith", team="MIN", position="WR")
    items = [_make_item("t:amb", "John Smith", "Minnesota Vikings", "WR")]
    _register_fake([items])

    summary = run_sync(db, source="test-feed", full=True)
    assert summary["status"] == "success"
    assert summary["items_unmatched"] == 1
    assert summary["items_matched"] == 0
    fi = db.list_feed_items()[0]
    assert fi["player_id"] is None
    # No auto-note created on either ambiguous candidate.
    assert db.list_notes("A1") == []
    assert db.list_notes("A2") == []


# --- build_feed_note_body ---


def test_build_feed_note_body_format():
    body = build_feed_note_body(
        {
            "cleaned_text": "Some news here.",
            "source_author": "Reporter",
            "source_url": "https://example.com/x",
            "created_at": "2026-04-29T12:34:56Z",
        }
    )
    assert body.startswith("Some news here.")
    assert body.endswith("— Reporter · https://example.com/x · 2026-04-29")


def test_build_feed_note_body_handles_missing_fields():
    body = build_feed_note_body(
        {"cleaned_text": "x", "source_author": None, "source_url": None, "created_at": None}
    )
    assert "x" in body
    assert "unknown" in body  # graceful fallback
