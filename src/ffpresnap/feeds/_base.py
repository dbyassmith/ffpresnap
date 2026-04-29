"""Base contracts for feed adapters.

A feed adapter pulls paginated, player-tagged content from an external source
(beat-reporter publications, injury wires, etc.) and yields ``FeedItem``
records. The orchestrator in ``ffpresnap.sync._run_feed_sync`` handles
identity matching, auto-note creation, and idempotent persistence; adapters
only produce items.

To add a new feed source:
  1. Create a module in this package implementing the ``FeedAdapter`` protocol.
  2. Call ``register_adapter(YourAdapter())`` at module import time.
  3. Add an entry for it in ``Database._seed_feed_sources`` so the
     ``feed_sources`` row exists with a stable id.
  4. Import the new module from ``feeds/__init__.py`` so registration fires
     when the package loads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Protocol


Fetcher = Callable[[str], bytes]
"""HTTP fetch callable. Takes a URL, returns raw response bytes. Used as
the test seam — adapters accept an injected ``Fetcher`` instead of calling
``urllib`` directly so tests can replay saved fixtures."""


IsSeen = Callable[[str], bool]
"""Predicate the orchestrator hands the adapter so the adapter can short-
circuit pagination once it sees a page where every item is already in
the DB. ``is_seen(external_id) -> bool``."""


class FeedFetchError(Exception):
    """Raised by an adapter when it cannot fetch or parse a page after
    retries. The orchestrator catches this, records the run as ``'error'``,
    and preserves any items already yielded.
    """


@dataclass(frozen=True)
class FeedItem:
    """A single piece of feed content tagged to one external player.

    ``external_id`` is the adapter-stable id used to dedupe across re-syncs
    (combined with the adapter's name to form the idempotency key in
    ``feed_items``). The remaining ``external_*`` fields capture the
    adapter's view of the player; identity matching against ``players``
    happens later in the orchestrator using ``team_abbr`` (which the
    adapter translates from ``external_team`` via ``map_team``).
    """

    external_id: str
    external_player_id: str | None
    external_player_name: str
    external_team: str
    external_position: str
    source_url: str | None
    source_author: str | None
    raw_html: str
    cleaned_text: str
    created_at: str  # ISO8601 string from the adapter

    def to_dict(self) -> dict[str, object]:
        return {
            "external_id": self.external_id,
            "external_player_id": self.external_player_id,
            "external_player_name": self.external_player_name,
            "external_team": self.external_team,
            "external_position": self.external_position,
            "source_url": self.source_url,
            "source_author": self.source_author,
            "raw_html": self.raw_html,
            "cleaned_text": self.cleaned_text,
            "created_at": self.created_at,
        }


class FeedAdapter(Protocol):
    """Contract every feed adapter implements.

    Adapters are stateless singletons registered at import time. The
    orchestrator handles persistence, transactions, and counters; the
    adapter only produces ``FeedItem`` instances.
    """

    name: str
    """Stable adapter name used as the ``--source`` value (e.g.
    ``'32beatwriters'``). Must match a row in ``feed_sources``."""

    source_url: str
    """Canonical homepage URL for the source. Stamped into
    ``sync_runs.source_url`` for each run."""

    def fetch(
        self,
        *,
        full: bool,
        fetch: Fetcher | None = None,
        is_seen: IsSeen | None = None,
    ) -> Iterable[FeedItem]:
        """Yield feed items newest-first.

        ``full=True`` walks the entire feed (used for first-run backfill or
        explicit reconciliation against API drift). ``full=False`` walks
        from newest until the adapter sees a page where ``is_seen()`` is
        True for every item, then stops — the orchestrator's incremental
        mode.

        ``fetch`` is the HTTP fetcher; the adapter falls back to its own
        default fetcher when ``None``. ``is_seen`` is the orchestrator's
        dedup predicate; adapters may pass ``None`` for ``full`` runs.

        Raises ``FeedFetchError`` if the adapter cannot complete the run
        (network errors, malformed responses).
        """
        ...

    def map_team(self, external_team: str) -> str | None:
        """Translate the adapter's team label to an NFL abbr matching
        ``players.team`` (e.g. ``'Minnesota Vikings'`` -> ``'MIN'``).
        Returns ``None`` for unmappable strings (e.g. ``'Prospect'``,
        ``''``, college affiliations); the orchestrator skips identity
        match for those rows.
        """
        ...
