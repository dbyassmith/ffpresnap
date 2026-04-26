from __future__ import annotations

import pytest

from ffpresnap import cli, sleeper as sleeper_module
from ffpresnap.db import Database
from ffpresnap.sleeper import SleeperFetchError


def _sleeper_player(pid: str) -> dict:
    return {
        "player_id": pid,
        "full_name": f"Player {pid}",
        "team": "KC",
        "position": "QB",
        "fantasy_positions": ["QB"],
    }


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    path = tmp_path / "notes.db"
    monkeypatch.setenv("FFPRESNAP_DB", str(path))
    return path


def test_cli_main_happy_path(env_db, capsys, monkeypatch):
    monkeypatch.setattr(
        sleeper_module,
        "fetch_players",
        lambda url: {"1": _sleeper_player("1"), "2": _sleeper_player("2")},
    )
    rc = cli.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "synced 2 players" in out

    # And the sync was recorded.
    db = Database.open(env_db)
    try:
        last = db.last_sync()
        assert last["status"] == "success"
        assert last["players_written"] == 2
    finally:
        db.close()


def test_cli_main_failure_returns_nonzero_and_records_error(
    env_db, capsys, monkeypatch
):
    def boom(url):
        raise SleeperFetchError("fetch failed")

    monkeypatch.setattr(sleeper_module, "fetch_players", boom)
    rc = cli.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "sync failed" in err and "fetch failed" in err

    db = Database.open(env_db)
    try:
        last = db.last_sync()
        assert last["status"] == "error"
        assert "fetch failed" in last["error"]
    finally:
        db.close()
