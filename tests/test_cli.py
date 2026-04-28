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
    rc = cli.main([])
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
    rc = cli.main([])
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


def test_cli_explicit_sleeper_source(env_db, capsys, monkeypatch):
    monkeypatch.setattr(
        sleeper_module,
        "fetch_players",
        lambda url: {"1": _sleeper_player("1")},
    )
    rc = cli.main(["--source", "sleeper"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "synced 1 players" in out
    assert "source=sleeper" in out


def test_cli_ourlads_source_runs_against_fake_fetcher(env_db, capsys, monkeypatch):
    """Ourlads source fetches via the Fetcher seam. With a fake fetcher
    returning one team's roster + the all-teams chart fixture, the CLI
    completes successfully."""
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
    # Also drop the politeness sleep to keep the test fast.
    monkeypatch.setattr(ourlads_module, "DEFAULT_DELAY_SECONDS", 0.0)

    rc = cli.main(["--source", "ourlads"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "source=ourlads" in out


def test_cli_invalid_source_exits_nonzero(env_db, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--source", "bogus"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "invalid choice" in err or "bogus" in err
