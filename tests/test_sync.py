from __future__ import annotations

import json

import pytest

from ffpresnap.db import Database
from ffpresnap.sleeper import SleeperFetchError
from ffpresnap.sync import run_sync


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "notes.db")
    yield d
    d.close()


def _sleeper_player(pid: str, **kwargs) -> dict:
    base = {
        "player_id": pid,
        "full_name": kwargs.pop("full_name", f"Player {pid}"),
        "team": kwargs.pop("team", "KC"),
        "position": kwargs.pop("position", "QB"),
        "fantasy_positions": kwargs.pop("fantasy_positions", ["QB"]),
    }
    base.update(kwargs)
    return base


def test_run_sync_filters_to_fantasy_positions_and_writes(db):
    payload = {
        "1": _sleeper_player("1", position="QB"),
        "2": _sleeper_player("2", position="WR"),
        "3": _sleeper_player("3", position="OL", fantasy_positions=["OL"]),
        "4": _sleeper_player("4", position="LB", fantasy_positions=["LB"]),
    }
    summary = run_sync(db, fetch=lambda url: payload, source_url="u")
    assert summary["status"] == "success"
    assert summary["players_written"] == 2
    ids = {p["player_id"] for p in db.list_players()}
    assert ids == {"1", "2"}


def test_run_sync_keeps_player_when_fantasy_positions_intersects(db):
    payload = {
        "5": _sleeper_player("5", position="ATH", fantasy_positions=["RB", "WR"]),
    }
    summary = run_sync(db, fetch=lambda url: payload)
    assert summary["players_written"] == 1


def test_run_sync_round_trips_depth_chart_fields(db):
    payload = {
        "10": _sleeper_player(
            "10",
            full_name="Starter",
            team="KC",
            depth_chart_position="QB",
            depth_chart_order=1,
        ),
    }
    run_sync(db, fetch=lambda url: payload)
    chart = db.depth_chart("KC")
    assert chart[0]["full_name"] == "Starter"
    assert chart[0]["depth_chart_position"] == "QB"
    assert chart[0]["depth_chart_order"] == 1


def test_run_sync_serializes_fantasy_positions_as_json(db):
    payload = {"1": _sleeper_player("1", fantasy_positions=["QB", "RB"])}
    run_sync(db, fetch=lambda url: payload)
    stored = db.get_player("1")
    assert json.loads(stored["fantasy_positions"]) == ["QB", "RB"]


def test_run_sync_records_error_and_reraises(db):
    def boom(url: str):
        raise SleeperFetchError("network down")

    with pytest.raises(SleeperFetchError):
        run_sync(db, fetch=boom)

    last = db.last_sync()
    assert last["status"] == "error"
    assert "network down" in last["error"]
    assert last["players_written"] == 0


def test_run_sync_idempotent(db):
    payload = {"1": _sleeper_player("1"), "2": _sleeper_player("2")}
    run_sync(db, fetch=lambda url: payload)
    run_sync(db, fetch=lambda url: payload)
    assert {p["player_id"] for p in db.list_players()} == {"1", "2"}
    history = db.conn.execute("SELECT status FROM sync_runs ORDER BY id").fetchall()
    assert [r["status"] for r in history] == ["success", "success"]


def test_run_sync_removes_dropped_players_and_cascades_notes(db):
    first = {"1": _sleeper_player("1"), "2": _sleeper_player("2")}
    run_sync(db, fetch=lambda url: first)
    db.add_note("1", "keep")
    db.add_note("2", "drop")

    second = {"1": _sleeper_player("1")}
    run_sync(db, fetch=lambda url: second)

    assert {p["player_id"] for p in db.list_players()} == {"1"}
    assert db.list_notes("1")[0]["body"] == "keep"
    # player 2 is gone, so listing notes raises NotFoundError via list_notes guard.
    from ffpresnap.db import NotFoundError

    with pytest.raises(NotFoundError):
        db.list_notes("2")
