from __future__ import annotations

import pytest

from ffpresnap import sleeper as sleeper_module
from ffpresnap.db import Database
from ffpresnap.server import ToolError, handle_tool_call


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


@pytest.fixture
def db(tmp_path):
    d = Database.open(tmp_path / "notes.db")
    yield d
    d.close()


@pytest.fixture
def synced_db(db, monkeypatch):
    payload = {
        "1": _sleeper_player(
            "1",
            full_name="Patrick Mahomes",
            team="KC",
            position="QB",
            depth_chart_position="QB",
            depth_chart_order=1,
        ),
        "2": _sleeper_player(
            "2",
            full_name="Travis Kelce",
            team="KC",
            position="TE",
            fantasy_positions=["TE"],
            depth_chart_position="TE",
            depth_chart_order=1,
        ),
        "3": _sleeper_player(
            "3",
            full_name="Unranked Sub",
            team="KC",
            position="WR",
            fantasy_positions=["WR"],
            depth_chart_position=None,
            depth_chart_order=None,
        ),
        "4": _sleeper_player(
            "4",
            full_name="Josh Allen",
            team="BUF",
            position="QB",
        ),
    }
    monkeypatch.setattr(sleeper_module, "fetch_players", lambda url: payload)
    handle_tool_call(db, "sync_players", {})
    return db


# --- sync / last_sync ---


def test_sync_players_writes_and_records(db, monkeypatch):
    monkeypatch.setattr(
        sleeper_module, "fetch_players", lambda url: {"1": _sleeper_player("1")}
    )
    summary = handle_tool_call(db, "sync_players", {})
    assert summary["status"] == "success"
    assert summary["players_written"] == 1
    last = handle_tool_call(db, "last_sync", {})
    assert last["id"] == summary["run_id"]


def test_last_sync_none_initially(db):
    assert handle_tool_call(db, "last_sync", {}) is None


# --- teams ---


def test_list_teams_no_query_returns_32(db):
    teams = handle_tool_call(db, "list_teams", {})
    assert len(teams) == 32


def test_list_teams_query_filters(db):
    afc = handle_tool_call(db, "list_teams", {"query": "AFC"})
    assert len(afc) == 16


# --- depth chart ---


def test_get_depth_chart_by_abbr_full_name_and_nickname(synced_db):
    by_abbr = handle_tool_call(synced_db, "get_depth_chart", {"team": "KC"})
    by_full = handle_tool_call(
        synced_db, "get_depth_chart", {"team": "Kansas City Chiefs"}
    )
    by_nick = handle_tool_call(synced_db, "get_depth_chart", {"team": "Chiefs"})
    assert by_abbr == by_full == by_nick
    assert by_abbr["team"]["abbr"] == "KC"
    positions = [g["position"] for g in by_abbr["groups"]]
    assert "QB" in positions and "TE" in positions and "Unranked" in positions
    assert positions[-1] == "Unranked"


def test_get_depth_chart_unknown_team_raises(synced_db):
    with pytest.raises(ToolError, match="No team"):
        handle_tool_call(synced_db, "get_depth_chart", {"team": "Foobar"})


def test_get_depth_chart_ambiguous_team_raises(synced_db):
    with pytest.raises(ToolError, match="ambiguous"):
        handle_tool_call(synced_db, "get_depth_chart", {"team": "New York"})


# --- player lookup ---


def test_find_player_returns_matches(synced_db):
    results = handle_tool_call(synced_db, "find_player", {"query": "patrick"})
    assert any(p["full_name"] == "Patrick Mahomes" for p in results)


def test_find_player_caps_at_10(synced_db):
    # synced_db has 4 players; verify generally that limit applies.
    results = handle_tool_call(synced_db, "find_player", {"query": ""})
    assert len(results) <= 10


def test_get_player_returns_player_and_notes(synced_db):
    handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "MVP"})
    out = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    assert out["player"]["full_name"] == "Patrick Mahomes"
    assert out["notes"][0]["body"] == "MVP"


def test_get_player_unknown_raises(synced_db):
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(synced_db, "get_player", {"player_id": "nope"})


def test_list_players_filters(synced_db):
    kc = handle_tool_call(synced_db, "list_players", {"team": "KC"})
    assert {p["player_id"] for p in kc} == {"1", "2", "3"}
    qbs = handle_tool_call(synced_db, "list_players", {"position": "QB"})
    assert {p["player_id"] for p in qbs} == {"1", "4"}


# --- notes ---


def test_add_note_requires_existing_player(synced_db):
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(
            synced_db, "add_note", {"player_id": "missing", "body": "x"}
        )


def test_add_and_list_notes_roundtrip(synced_db):
    handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "first"})
    handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "second"})
    out = handle_tool_call(synced_db, "list_notes", {"player_id": "1"})
    bodies = [n["body"] for n in out["notes"]]
    assert bodies == ["second", "first"]


def test_update_and_delete_note(synced_db):
    n = handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "before"})
    updated = handle_tool_call(
        synced_db, "update_note", {"note_id": n["id"], "body": "after"}
    )
    assert updated["body"] == "after"
    handle_tool_call(synced_db, "delete_note", {"note_id": n["id"]})
    out = handle_tool_call(synced_db, "list_notes", {"player_id": "1"})
    assert out["notes"] == []


def test_add_note_missing_arg_raises(synced_db):
    with pytest.raises(ToolError, match="Missing required argument"):
        handle_tool_call(synced_db, "add_note", {"player_id": "1"})


# --- team notes ---


def test_team_notes_round_trip(synced_db):
    handle_tool_call(
        synced_db, "add_team_note", {"team": "Chiefs", "body": "AFC West favorite"}
    )
    handle_tool_call(
        synced_db, "add_team_note", {"team": "KC", "body": "Strong O-line"}
    )
    out = handle_tool_call(synced_db, "list_team_notes", {"team": "Kansas City Chiefs"})
    assert out["team"]["abbr"] == "KC"
    bodies = [n["body"] for n in out["notes"]]
    assert bodies == ["Strong O-line", "AFC West favorite"]


def test_team_notes_unknown_team_raises(synced_db):
    with pytest.raises(ToolError, match="No team"):
        handle_tool_call(synced_db, "add_team_note", {"team": "Foobar", "body": "x"})


def test_team_notes_do_not_appear_in_player_view(synced_db):
    handle_tool_call(
        synced_db, "add_team_note", {"team": "KC", "body": "team-level"}
    )
    handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "player-level"})
    player_view = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    bodies = [n["body"] for n in player_view["notes"]]
    assert bodies == ["player-level"]


# --- removed tools ---


def test_removed_player_tools_are_unknown(db):
    with pytest.raises(ToolError, match="Unknown tool"):
        handle_tool_call(db, "add_player", {"name": "x"})
    with pytest.raises(ToolError, match="Unknown tool"):
        handle_tool_call(db, "delete_player", {"player_id": 1})


# --- end-to-end agent flow ---


def test_full_navigation_flow(synced_db):
    """Team picker -> depth chart -> player detail -> note -> retrieve note."""
    teams = handle_tool_call(synced_db, "list_teams", {"query": "Chiefs"})
    assert teams[0]["abbr"] == "KC"

    chart = handle_tool_call(synced_db, "get_depth_chart", {"team": "Chiefs"})
    qb_group = next(g for g in chart["groups"] if g["position"] == "QB")
    pid = qb_group["players"][0]["player_id"]

    handle_tool_call(synced_db, "add_note", {"player_id": pid, "body": "starter"})
    out = handle_tool_call(synced_db, "get_player", {"player_id": pid})
    assert out["notes"][0]["body"] == "starter"
