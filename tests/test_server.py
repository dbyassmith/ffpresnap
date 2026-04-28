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


def test_sync_players_explicit_sleeper_source(db, monkeypatch):
    monkeypatch.setattr(
        sleeper_module, "fetch_players", lambda url: {"1": _sleeper_player("1")}
    )
    summary = handle_tool_call(db, "sync_players", {"source": "sleeper"})
    assert summary["status"] == "success"
    assert summary["source"] == "sleeper"


def test_sync_players_ourlads_source_runs_in_background(db, monkeypatch):
    """Ourlads sync starts a background thread and returns a run_id. With
    fixture-backed fetcher, the run completes successfully in <2s."""
    from pathlib import Path

    from ffpresnap import ourlads as ourlads_module

    fixtures = Path(__file__).parent / "fixtures" / "ourlads"
    roster_html = (fixtures / "roster_ATL.html").read_bytes()
    chart_html = (fixtures / "all_chart.html").read_bytes()

    def fake_fetch(url: str) -> bytes:
        if url == ourlads_module.OURLADS_ALL_CHART_URL:
            return chart_html
        return roster_html

    monkeypatch.setattr(ourlads_module, "_default_fetch", fake_fetch)
    monkeypatch.setattr(ourlads_module, "DEFAULT_DELAY_SECONDS", 0.0)

    summary = handle_tool_call(db, "sync_players", {"source": "ourlads"})
    assert summary["source"] == "ourlads"
    # Status may be 'running' or 'success' depending on timing.
    assert summary["status"] in ("running", "success")
    run_id = summary["run_id"]
    assert run_id > 0

    # Wait up to 5s for the worker thread to finish.
    import time
    deadline = time.monotonic() + 5.0
    final = None
    while time.monotonic() < deadline:
        final = handle_tool_call(db, "get_sync_status", {"run_id": run_id})
        if final and final["status"] != "running":
            break
        time.sleep(0.05)
    assert final is not None
    assert final["status"] == "success"
    assert (final["players_written"] or 0) > 0


def test_get_sync_status_returns_none_for_unknown_run(db):
    assert handle_tool_call(db, "get_sync_status", {"run_id": 999999}) is None


def test_last_sync_filters_by_source_via_mcp(db, monkeypatch):
    monkeypatch.setattr(
        sleeper_module, "fetch_players", lambda url: {"1": _sleeper_player("1")}
    )
    handle_tool_call(db, "sync_players", {"source": "sleeper"})
    sleeper_run = handle_tool_call(db, "last_sync", {"source": "sleeper"})
    assert sleeper_run is not None
    assert sleeper_run["source"] == "sleeper"
    # Without filter, returns the same row.
    assert handle_tool_call(db, "last_sync", {})["id"] == sleeper_run["id"]


def test_concurrent_sync_raises_tool_error(db, monkeypatch):
    """Running an Ourlads sync while a Sleeper sync is already in flight
    should surface ConcurrentSyncError as a ToolError. Construct this state
    by inserting a 'running' row directly."""
    db.conn.execute(
        "INSERT INTO sync_runs (started_at, source_url, status, source) "
        "VALUES (?, 'https://ongoing', 'running', 'sleeper')",
        (
            __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .isoformat(timespec="seconds"),
        ),
    )
    db.conn.commit()
    with pytest.raises(ToolError) as exc:
        handle_tool_call(db, "sync_players", {"source": "ourlads"})
    assert "already running" in str(exc.value)


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
    results = handle_tool_call(synced_db, "find_player", {"query": ""})
    assert len(results) <= 10


def test_get_player_returns_player_notes_and_mentions(synced_db):
    handle_tool_call(
        synced_db,
        "add_note",
        {"target_type": "player", "target_id": "1", "body": "MVP"},
    )
    out = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    assert out["player"]["full_name"] == "Patrick Mahomes"
    assert out["notes"][0]["body"] == "MVP"
    assert out["mentions"] == []


def test_get_player_unknown_raises(synced_db):
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(synced_db, "get_player", {"player_id": "nope"})


def test_list_players_filters(synced_db):
    kc = handle_tool_call(synced_db, "list_players", {"team": "KC"})
    assert {p["player_id"] for p in kc} == {"1", "2", "3"}
    qbs = handle_tool_call(synced_db, "list_players", {"position": "QB"})
    assert {p["player_id"] for p in qbs} == {"1", "4"}


# --- unified add_note / list_notes ---


def test_add_note_player(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {"target_type": "player", "target_id": "1", "body": "first"},
    )
    out = handle_tool_call(
        synced_db, "list_notes", {"scope": "player", "target_id": "1"}
    )
    assert [x["id"] for x in out["notes"]] == [n["id"]]


def test_add_note_team_accepts_nickname(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {"target_type": "team", "target_id": "Chiefs", "body": "team note"},
    )
    out = handle_tool_call(
        synced_db, "list_notes", {"scope": "team", "target_id": "KC"}
    )
    assert [x["id"] for x in out["notes"]] == [n["id"]]


def test_add_note_study(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "RB Handcuffs"})
    n = handle_tool_call(
        synced_db,
        "add_note",
        {"target_type": "study", "target_id": str(s["id"]), "body": "x"},
    )
    out = handle_tool_call(
        synced_db, "list_notes", {"scope": "study", "target_id": str(s["id"])}
    )
    assert [x["id"] for x in out["notes"]] == [n["id"]]


def test_add_note_with_mentions(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {
            "target_type": "player",
            "target_id": "1",
            "body": "vs Allen, BUF",
            "mentions": {"player_ids": ["4"], "team_abbrs": ["BUF"]},
        },
    )
    assert {p["player_id"] for p in n["mentions"]["players"]} == {"4"}
    assert [t["abbr"] for t in n["mentions"]["teams"]] == ["BUF"]


def test_add_note_unknown_target_type_raises(synced_db):
    with pytest.raises(ToolError, match="Unknown target_type"):
        handle_tool_call(
            synced_db,
            "add_note",
            {"target_type": "league", "target_id": "1", "body": "x"},
        )


def test_add_note_study_with_non_integer_id_raises(synced_db):
    with pytest.raises(ToolError, match="must be an integer"):
        handle_tool_call(
            synced_db,
            "add_note",
            {"target_type": "study", "target_id": "abc", "body": "x"},
        )


def test_list_notes_recent_returns_feed(synced_db):
    handle_tool_call(
        synced_db, "add_note", {"target_type": "player", "target_id": "1", "body": "p"}
    )
    handle_tool_call(
        synced_db, "add_note", {"target_type": "team", "target_id": "KC", "body": "t"}
    )
    feed = handle_tool_call(synced_db, "list_notes", {"scope": "recent"})
    assert [n["body"] for n in feed] == ["t", "p"]
    assert {n["subject"]["type"] for n in feed} == {"player", "team"}


def test_list_notes_recent_limit(synced_db):
    for i in range(5):
        handle_tool_call(
            synced_db,
            "add_note",
            {"target_type": "player", "target_id": "1", "body": f"n{i}"},
        )
    feed = handle_tool_call(synced_db, "list_notes", {"scope": "recent", "limit": 2})
    assert len(feed) == 2


def test_list_notes_player_scope_requires_target(synced_db):
    with pytest.raises(ToolError, match="target_id is required"):
        handle_tool_call(synced_db, "list_notes", {"scope": "player"})


def test_list_notes_unknown_scope_raises(synced_db):
    with pytest.raises(ToolError, match="Unknown scope"):
        handle_tool_call(
            synced_db, "list_notes", {"scope": "bogus", "target_id": "1"}
        )


# --- mention validation surfaced ---


def test_add_note_unknown_player_mention_raises(synced_db):
    with pytest.raises(ToolError, match="player_id"):
        handle_tool_call(
            synced_db,
            "add_note",
            {
                "target_type": "player",
                "target_id": "1",
                "body": "x",
                "mentions": {"player_ids": ["nope"]},
            },
        )


def test_add_note_ambiguous_team_mention_raises(synced_db):
    with pytest.raises(ToolError, match="ambiguous"):
        handle_tool_call(
            synced_db,
            "add_note",
            {
                "target_type": "player",
                "target_id": "1",
                "body": "x",
                "mentions": {"team_abbrs": ["New York"]},
            },
        )


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


def test_set_study_status(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "x"})
    handle_tool_call(
        synced_db, "set_study_status", {"study_id": s["id"], "status": "archived"}
    )
    open_only = handle_tool_call(synced_db, "list_studies", {})
    assert all(x["id"] != s["id"] for x in open_only)
    archived = handle_tool_call(synced_db, "list_studies", {"status": "archived"})
    assert any(x["id"] == s["id"] for x in archived)
    handle_tool_call(
        synced_db, "set_study_status", {"study_id": s["id"], "status": "open"}
    )
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
    handle_tool_call(
        synced_db,
        "add_note",
        {"target_type": "study", "target_id": str(s["id"]), "body": "x"},
    )
    handle_tool_call(synced_db, "delete_study", {"study_id": s["id"]})
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(synced_db, "get_study", {"study_id": s["id"]})


def test_get_study_unknown_raises(synced_db):
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(synced_db, "get_study", {"study_id": 9999})


# --- get_team ---


def test_get_team_returns_notes_and_mentions(synced_db):
    handle_tool_call(
        synced_db, "add_note", {"target_type": "team", "target_id": "KC", "body": "primary"}
    )
    handle_tool_call(
        synced_db,
        "add_note",
        {
            "target_type": "player",
            "target_id": "4",
            "body": "mentions KC",
            "mentions": {"team_abbrs": ["KC"]},
        },
    )
    out = handle_tool_call(synced_db, "get_team", {"team": "Chiefs"})
    assert out["team"]["abbr"] == "KC"
    assert [n["body"] for n in out["notes"]] == ["primary"]
    assert [n["body"] for n in out["mentions"]] == ["mentions KC"]


# --- update_note ---


def test_update_note_replaces_mentions(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {
            "target_type": "player",
            "target_id": "1",
            "body": "v1",
            "mentions": {"player_ids": ["2"]},
        },
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
        {
            "target_type": "player",
            "target_id": "1",
            "body": "v1",
            "mentions": {"player_ids": ["2"]},
        },
    )
    updated = handle_tool_call(
        synced_db, "update_note", {"note_id": n["id"], "body": "v2"}
    )
    assert [p["player_id"] for p in updated["mentions"]["players"]] == ["2"]


def test_delete_note(synced_db):
    n = handle_tool_call(
        synced_db,
        "add_note",
        {"target_type": "player", "target_id": "1", "body": "x"},
    )
    handle_tool_call(synced_db, "delete_note", {"note_id": n["id"]})
    out = handle_tool_call(
        synced_db, "list_notes", {"scope": "player", "target_id": "1"}
    )
    assert out["notes"] == []


# --- removed legacy tools ---


# --- watchlist ---


def test_update_player_toggles_watchlist(synced_db):
    p = handle_tool_call(
        synced_db, "update_player", {"player_id": "1", "watchlist": True}
    )
    assert p["watchlist"] is True
    p2 = handle_tool_call(
        synced_db, "update_player", {"player_id": "1", "watchlist": False}
    )
    assert p2["watchlist"] is False


def test_update_player_unknown_raises(synced_db):
    with pytest.raises(ToolError, match="not found"):
        handle_tool_call(
            synced_db, "update_player", {"player_id": "nope", "watchlist": True}
        )


def test_list_players_watchlist_filter(synced_db):
    handle_tool_call(synced_db, "update_player", {"player_id": "1", "watchlist": True})
    handle_tool_call(synced_db, "update_player", {"player_id": "4", "watchlist": True})
    on = handle_tool_call(synced_db, "list_players", {"watchlist": True})
    assert {p["player_id"] for p in on} == {"1", "4"}


def test_get_player_includes_watchlist(synced_db):
    out = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    assert "watchlist" in out["player"]
    assert out["player"]["watchlist"] is False


# --- prompt library ---


def test_list_prompts_tool_returns_seeded_prompts(synced_db):
    synced_db._seed_prompts(
        loader=lambda: [
            {
                "slug": "show-prompt-library",
                "title": "Show Library",
                "description": "Re-open the library.",
                "body": "Call list_prompts and render an artifact.",
            },
            {
                "slug": "study-browser",
                "title": "Study Browser",
                "description": "Browse studies.",
                "body": "Call list_studies and render cards.",
            },
        ]
    )
    result = handle_tool_call(synced_db, "list_prompts", {})
    assert [p["slug"] for p in result] == ["show-prompt-library", "study-browser"]
    assert all({"slug", "title", "description", "body"} <= set(p) for p in result)


def test_list_prompts_tool_empty(db):
    db._seed_prompts(loader=lambda: [])
    assert handle_tool_call(db, "list_prompts", {}) == []


def test_legacy_tools_are_unknown(db):
    for name in (
        "add_team_note",
        "add_study_note",
        "list_team_notes",
        "list_study_notes",
        "list_recent_notes",
        "archive_study",
        "unarchive_study",
        "add_player",
        "delete_player",
    ):
        with pytest.raises(ToolError, match="Unknown tool"):
            handle_tool_call(db, name, {})


# --- end-to-end agent flow ---


def test_studies_and_mentions_full_flow(synced_db):
    s = handle_tool_call(synced_db, "create_study", {"title": "RB Handcuffs"})
    handle_tool_call(
        synced_db,
        "add_note",
        {
            "target_type": "study",
            "target_id": str(s["id"]),
            "body": "watch Pacheco backups",
            "mentions": {"player_ids": ["1"], "team_abbrs": ["KC"]},
        },
    )
    pview = handle_tool_call(synced_db, "get_player", {"player_id": "1"})
    assert pview["notes"] == []
    assert any("Pacheco" in n["body"] for n in pview["mentions"])

    tview = handle_tool_call(synced_db, "get_team", {"team": "KC"})
    assert any("Pacheco" in n["body"] for n in tview["mentions"])

    feed = handle_tool_call(synced_db, "list_notes", {"scope": "recent"})
    study_note = feed[0]
    assert study_note["subject"]["type"] == "study"
    assert study_note["subject"]["title"] == "RB Handcuffs"
    assert {p["player_id"] for p in study_note["mentions"]["players"]} == {"1"}

    handle_tool_call(
        synced_db, "set_study_status", {"study_id": s["id"], "status": "archived"}
    )
    open_studies = handle_tool_call(synced_db, "list_studies", {})
    assert all(x["id"] != s["id"] for x in open_studies)
