"""Feed-ingestion package.

Importing ``ffpresnap.feeds`` triggers registration of every concrete
adapter listed below — that's the design contract for the registry. The
CLI and the unified ``sync`` MCP tool both import this package on startup
so ``adapter_names()`` reflects every registered source without explicit
plumbing.
"""

from __future__ import annotations

from ._base import FeedAdapter, FeedFetchError, FeedItem, Fetcher, IsSeen
from ._registry import (
    adapter_names,
    get_adapter,
    register_adapter,
)

# Concrete adapters — importing each module registers it. Adding a new feed
# source = adding another import line here. The 32beatwriters adapter is
# imported below.
from . import _32beatwriters  # noqa: F401,E402  (import for side effect)  # type: ignore[unused-import]


__all__ = [
    "FeedAdapter",
    "FeedFetchError",
    "FeedItem",
    "Fetcher",
    "IsSeen",
    "adapter_names",
    "get_adapter",
    "register_adapter",
]
