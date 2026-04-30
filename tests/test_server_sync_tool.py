from __future__ import annotations

import json
import time

import pytest

from ffpresnap.db import Database
from ffpresnap.feeds._base import FeedItem
from ffpresnap.feeds._registry import (
    _reset_registry_for_tests,
    register_adapter,
)
from ffpresnap.server import TOOLS, ToolError, handle_tool_call


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


def _seed(db, *, pid="X1", name="Justin Jefferson", team="MIN", position="WR"):
    db.conn.execute(
        "INSERT OR REPLACE INTO players ("
        "  player_id, full_name, team, position, fantasy_positions, "
        "  updated_at, watchlist, source"
        ") VALUES (?, ?, ?, ?, ?, ?, 0, 'sleeper')",
        (pid, name, team, position, json.dumps([position]), "2026-04-29T00:00:00Z"),
    )
    db.conn.commit()


class _SyncFakeAdapter:
    name = "sync-test"
    source_url = "https://example.com"

    def __init__(self, items):
        self._items = items

    def fetch(self, *, full, fetch=None, is_seen=None):
        for it in self._items:
            yield it

    def map_team(self, external_team):
        return {"Minnesota Vikings": "MIN"}.get(external_team)


# --- TOOLS catalog shape ---


def test_sync_tool_in_catalog():
    names = {t["name"] for t in TOOLS}
    assert "sync" in names
    assert "sync_players" not in names  # removed


def test_sync_tool_source_enum_includes_feeds():
    sync_tool = next(t for t in TOOLS if t["name"] == "sync")
    enum = sync_tool["inputSchema"]["properties"]["source"]["enum"]
    assert "sleeper" in enum
    assert "ourlads" in enum
    # 32beatwriters is registered at package import time.
    assert "32beatwriters" in enum


def test_feed_read_tools_in_catalog():
    names = {t["name"] for t in TOOLS}
    assert "list_feed_items" in names
    assert "rematch_feed_items" in names
    assert "delete_auto_notes_from_run" in names


# --- sync dispatch ---


def test_sync_sleeper_synchronous(db, monkeypatch):
    from ffpresnap import sleeper as sleeper_module

    payload = {
        "1": {
            "player_id": "1",
            "full_name": "P One",
            "team": "KC",
            "position": "QB",
            "fantasy_positions": ["QB"],
        }
    }
    monkeypatch.setattr(sleeper_module, "fetch_players", lambda url: payload)
    out = handle_tool_call(db, "sync", {"source": "sleeper"})
    assert out["source"] == "sleeper"
    assert out["status"] == "success"


def test_sync_unknown_source_raises_tool_error(db):
    with pytest.raises(ToolError):
        handle_tool_call(db, "sync", {"source": "bogus"})


def test_sync_missing_source_raises(db):
    with pytest.raises(ToolError):
        handle_tool_call(db, "sync", {})


def test_sync_feed_runs_in_background_and_finishes(db):
    """Spin up a fake feed adapter, call sync, poll get_sync_status until
    success. Verifies the unified tool routes feed sources through the
    background-thread path.
    """
    _reset_registry_for_tests()
    register_adapter(
        _SyncFakeAdapter(
            [
                FeedItem(
                    external_id="t:1",
                    external_player_id="x",
                    external_player_name="Justin Jefferson",
                    external_team="Minnesota Vikings",
                    external_position="WR",
                    source_url="u",
                    source_author="r",
                    raw_html="<p>x</p>",
                    cleaned_text="x",
                    created_at="2026-04-29T00:00:00Z",
                ),
            ]
        )
    )
    _seed(db, pid="JJ1", name="Justin Jefferson", team="MIN")

    out = handle_tool_call(db, "sync", {"source": "sync-test", "full": True})
    assert out["source"] == "sync-test"
    assert "run_id" in out
    run_id = out["run_id"]

    # Wait for completion.
    deadline = time.monotonic() + 5.0
    final = None
    while time.monotonic() < deadline:
        final = handle_tool_call(db, "get_sync_status", {"run_id": run_id})
        if final and final["status"] != "running":
            break
        time.sleep(0.05)
    assert final is not None
    assert final["status"] == "success"
    assert final["items_new"] == 1
    assert final["items_matched"] == 1


# --- list_feed_items / rematch_feed_items / delete_auto_notes_from_run ---


def test_list_feed_items_via_mcp(db):
    _seed(db, pid="JJ1", name="Justin Jefferson")
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        {
            "external_id": "32bw:1",
            "external_player_id": "299",
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
        player_id="JJ1",
        note_body="b",
        run_id=1,
    )
    items = handle_tool_call(db, "list_feed_items", {})
    assert len(items) == 1
    assert items[0]["player_id"] == "JJ1"

    matched = handle_tool_call(db, "list_feed_items", {"matched": True})
    assert len(matched) == 1
    unmatched = handle_tool_call(db, "list_feed_items", {"matched": False})
    assert len(unmatched) == 0


def test_rematch_feed_items_via_mcp(db):
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        {
            "external_id": "32bw:1",
            "external_player_id": "1",
            "external_player_name": "Justin Jefferson",
            "external_team": "Minnesota Vikings",
            "external_position": "WR",
            "team_abbr": "MIN",
            "source_url": "u",
            "source_author": "r",
            "raw_html": "x",
            "cleaned_text": "x",
            "created_at": "2026-04-29T00:00:00Z",
        },
        player_id=None,
        note_body=None,
    )
    _seed(db, pid="JJ1", name="Justin Jefferson")
    out = handle_tool_call(db, "rematch_feed_items", {})
    assert out["matched"] == 1
    assert out["notes_written"] == 1


def test_delete_auto_notes_from_run_via_mcp(db):
    _seed(db, pid="JJ1", name="Justin Jefferson")
    db.add_feed_item_with_auto_note(
        "32beatwriters",
        {
            "external_id": "32bw:1",
            "external_player_id": "1",
            "external_player_name": "Justin Jefferson",
            "external_team": "Minnesota Vikings",
            "external_position": "WR",
            "team_abbr": "MIN",
            "source_url": "u",
            "source_author": "r",
            "raw_html": "x",
            "cleaned_text": "x",
            "created_at": "2026-04-29T00:00:00Z",
        },
        player_id="JJ1",
        note_body="b",
        run_id=42,
    )
    out = handle_tool_call(db, "delete_auto_notes_from_run", {"run_id": 42})
    assert out == {"deleted_notes": 1}
    # Idempotent.
    out2 = handle_tool_call(db, "delete_auto_notes_from_run", {"run_id": 42})
    assert out2 == {"deleted_notes": 0}


# --- get_sync_status surfaces feed counters ---


def test_get_sync_status_includes_feed_counters(db):
    rid = db.record_sync_start("u", source="32beatwriters")
    db.record_sync_finish(
        rid,
        status="success",
        items_fetched=10,
        items_new=4,
        items_matched=3,
        items_unmatched=1,
    )
    row = handle_tool_call(db, "get_sync_status", {"run_id": rid})
    assert row["items_fetched"] == 10
    assert row["items_new"] == 4
    assert row["items_matched"] == 3
    assert row["items_unmatched"] == 1
