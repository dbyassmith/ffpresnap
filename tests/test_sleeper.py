from __future__ import annotations

import json

import pytest

from ffpresnap.sleeper import SleeperFetchError, fetch_players


def test_fetch_players_happy_path():
    payload = {"4034": {"player_id": "4034", "full_name": "Patrick Mahomes"}}
    body = json.dumps(payload).encode("utf-8")
    result = fetch_players("https://example.test/x", fetcher=lambda url: body)
    assert result == payload


def test_fetch_players_empty_body_raises():
    with pytest.raises(SleeperFetchError):
        fetch_players("https://example.test/x", fetcher=lambda url: b"")


def test_fetch_players_malformed_json_raises():
    with pytest.raises(SleeperFetchError):
        fetch_players("https://example.test/x", fetcher=lambda url: b"not json")


def test_fetch_players_non_object_raises():
    with pytest.raises(SleeperFetchError):
        fetch_players("https://example.test/x", fetcher=lambda url: b"[1, 2, 3]")


def test_fetch_players_fetcher_failure_propagates():
    def boom(url: str) -> bytes:
        raise SleeperFetchError("simulated network error")

    with pytest.raises(SleeperFetchError, match="simulated"):
        fetch_players("https://example.test/x", fetcher=boom)
