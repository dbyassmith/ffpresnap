from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
from typing import Any, Callable


PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
DEFAULT_TIMEOUT = 30.0


class SleeperFetchError(Exception):
    pass


# A fetcher is a callable that takes a URL and returns raw bytes.
Fetcher = Callable[[str], bytes]


def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        raise SleeperFetchError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise SleeperFetchError(f"Network error fetching {url}: {e.reason}") from e


def fetch_players(
    url: str = PLAYERS_URL, *, fetcher: Fetcher | None = None
) -> dict[str, dict[str, Any]]:
    """Fetch the NFL player payload from Sleeper and return a dict keyed by player_id.

    The optional ``fetcher`` parameter is the test seam — pass a callable that takes
    a URL and returns bytes. Production code uses :func:`_default_fetch`.
    """
    fn: Fetcher = fetcher if fetcher is not None else _default_fetch
    raw = fn(url)
    if not raw:
        raise SleeperFetchError(f"Empty response from {url}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SleeperFetchError(f"Malformed JSON from {url}: {e}") from e
    if not isinstance(payload, dict):
        raise SleeperFetchError(
            f"Expected JSON object from {url}, got {type(payload).__name__}"
        )
    return payload
