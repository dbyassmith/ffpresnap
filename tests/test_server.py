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


def test_list_recent_notes_mixes_players_and_teams(synced_db):
    handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "p1"})
    handle_tool_call(synced_db, "add_team_note", {"team": "KC", "body": "t1"})
    handle_tool_call(synced_db, "add_note", {"player_id": "4", "body": "p2"})

    feed = handle_tool_call(synced_db, "list_recent_notes", {})
    bodies = [n["body"] for n in feed]
    assert bodies == ["p2", "t1", "p1"]
    types = {n["subject"]["type"] for n in feed}
    assert types == {"player", "team"}


def test_list_recent_notes_limit_clamped(synced_db):
    for i in range(5):
        handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": f"n{i}"})
    feed = handle_tool_call(synced_db, "list_recent_notes", {"limit": 2})
    assert len(feed) == 2


# --- studies ---


def test_create_list_get_study(synced_db):
    s = handle_tool_call(
        synced_db, "create_study", {"title": "RB Handcuffs", "description": "draft late"}
    )
    assert s["status"] == "open"
    listed = handle_tool_call(synced_db, "list_studies", {})
    assert any(x["id"] == s["id"] for x in listed)

    out = handle_tool_call(synced_db, "get_study", {"study_id": s["id"]})
    assert out["study"]["title"] == "RB Handcuffs"
    assert out["notes"] == []
    assert out["mentions"] == []


def test_archive_unarchive_study(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "x"})
    handle_tool_call(synced_db, "archive_study", {"study_id": s["id"]})
    open_only = handle_tool_call(synced_db, "list_studies", {})
    assert all(x["id"] != s["id"] for x in open_only)
    archived = handle_tool_call(synced_db, "list_studies", {"status": "archived"})
    assert any(x["id"] == s["id"] for x in archived)
    all_studies = handle_tool_call(synced_db, "list_studies", {"status": "all"})
    assert any(x["id"] == s["id"] for x in all_studies)
    handle_tool_call(synced_db, "unarchive_study", {"study_id": s["id"]})
    open_again = handle_tool_call(synced_db, "list_studies", {})
    assert any(x["id"] == s["id"] for x in open_again)


def test_update_study(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "old"})
    updated = handle_tool_call(
        synced_db, "update_study", {"study_id": s["id"], "title": "new"}
    )
    assert updated["title"] == "new"


def test_delete_study_cascades(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "doomed"})
    handle_tool_call(synced_db, "add_study_note", {"study_id": s["id"], "body": "x"})
    handle_tool_call(synced_db, "delete_study", {"study_id": s["id"]})
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(synced_db, "get_study", {"study_id": s["id"]})


def test_get_study_unknown_raises(synced_db):
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(synced_db, "get_study", {"study_id": 9999})


def test_add_study_note_with_mentions(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "RB Handcuffs"})
    note = handle_tool_call(
        synced_db,
        "add_study_note",
        {
            "study_id": s["id"],
            "body": "Pacheco vs Hunt, KC backfield",
            "mentions": {"player_ids": ["1", "2"], "team_abbrs": ["KC"]},
        },
    )
    assert {p["player_id"] for p in note["mentions"]["players"]} == {"1", "2"}
    assert [t["abbr"] for t in note["mentions"]["teams"]] == ["KC"]


# --- mentions on existing tools ---


def test_get_player_returns_notes_and_mentions_separately(synced_db):
    handle_tool_call(synced_db, "add_note", {"player_id": "1", "body": "primary"})
    handle_tool_call(
        synced_db,
        "add_note",
        {"player_id": "4", "body": "mentions 1", "mentions": {"player_ids": ["1"]}},
    )
    out = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    primary_bodies = [n["body"] for n in out["notes"]]
    mention_bodies = [n["body"] for n in out["mentions"]]
    assert primary_bodies == ["primary"]
    assert mention_bodies == ["mentions 1"]


def test_get_team_returns_notes_and_mentions(synced_db):
    handle_tool_call(synced_db, "add_team_note", {"team": "KC", "body": "primary"})
    handle_tool_call(
        synced_db,
        "add_note",
        {"player_id": "4", "body": "mentions KC", "mentions": {"team_abbrs": ["KC"]}},
    )
    out = handle_tool_call(synced_db, "get_team", {"team": "Chiefs"})
    assert out["team"]["abbr"] == "KC"
    assert [n["body"] for n in out["notes"]] == ["primary"]
    assert [n["body"] for n in out["mentions"]] == ["mentions KC"]


def test_add_note_with_unknown_mention_raises(synced_db):
    with pytest.raises(ToolError, match="player_id"):
        handle_tool_call(
            synced_db,
            "add_note",
            {"player_id": "1", "body": "x", "mentions": {"player_ids": ["nope"]}},
        )


def test_add_note_with_ambiguous_team_mention_raises(synced_db):
    with pytest.raises(ToolError, match="ambiguous"):
        handle_tool_call(
            synced_db,
            "add_note",
            {"player_id": "1", "body": "x", "mentions": {"team_abbrs": ["New York"]}},
        )


def test_update_note_replaces_mentions(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {"player_id": "1", "body": "v1", "mentions": {"player_ids": ["2"]}},
    )
    updated = handle_tool_call(
        synced_db,
        "update_note",
        {"note_id": n["id"], "body": "v2", "mentions": {"player_ids": ["4"]}},
    )
    assert [p["player_id"] for p in updated["mentions"]["players"]] == ["4"]


def test_update_note_without_mentions_keeps_them(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {"player_id": "1", "body": "v1", "mentions": {"player_ids": ["2"]}},
    )
    updated = handle_tool_call(
        synced_db, "update_note", {"note_id": n["id"], "body": "v2"}
    )
    assert [p["player_id"] for p in updated["mentions"]["players"]] == ["2"]


# --- end-to-end agent flow ---


def test_studies_and_mentions_full_flow(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "RB Handcuffs"})
    handle_tool_call(
        synced_db,
        "add_study_note",
        {
            "study_id": s["id"],
            "body": "watch Pacheco backups",
            "mentions": {"player_ids": ["1"], "team_abbrs": ["KC"]},
        },
    )
    # Player view picks up the cross-reference under mentions.
    pview = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    assert pview["notes"] == []
    assert any("Pacheco" in n["body"] for n in pview["mentions"])

    # Team view picks it up too.
    tview = handle_tool_call(synced_db, "get_team", {"team": "KC"})
    assert any("Pacheco" in n["body"] for n in tview["mentions"])

    # Recent feed includes the study note with mentions resolved.
    feed = handle_tool_call(synced_db, "list_recent_notes", {})
    study_note = feed[0]
    assert study_note["subject"]["type"] == "study"
    assert study_note["subject"]["title"] == "RB Handcuffs"
    assert {p["player_id"] for p in study_note["mentions"]["players"]} == {"1"}

    handle_tool_call(synced_db, "archive_study", {"study_id": s["id"]})
    open_studies = handle_tool_call(synced_db, "list_studies", {})
    assert all(x["id"] != s["id"] for x in open_studies)


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
