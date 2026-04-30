"""Module-level adapter registry.

Concrete adapters call ``register_adapter(adapter)`` at import time. The
``feeds/__init__.py`` module imports each concrete adapter so registration
happens whenever ``ffpresnap.feeds`` is imported.
"""

from __future__ import annotations

from typing import Iterable

from ._base import FeedAdapter


_REGISTRY: dict[str, FeedAdapter] = {}


def register_adapter(adapter: FeedAdapter) -> None:
    """Register a feed adapter. Called once per adapter at import time.

    Raises ``ValueError`` if an adapter with the same name is already
    registered (catches accidental duplicate imports / shadowing).
    """
    if adapter.name in _REGISTRY:
        raise ValueError(f"feed adapter already registered: {adapter.name!r}")
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: str) -> FeedAdapter:
    """Return the adapter registered under ``name``. Raises ``KeyError``
    if no adapter is registered under that name.
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"no feed adapter registered for {name!r}") from exc


def adapter_names() -> tuple[str, ...]:
    """Return the sorted tuple of registered adapter names. Used by the
    CLI and the unified ``sync`` MCP tool to compute the dynamic
    ``--source`` enum.
    """
    return tuple(sorted(_REGISTRY))


def _reset_registry_for_tests() -> None:
    """Clear the registry. Test-only helper — production code never calls
    this. Tests use it to assert ``register_adapter`` raises on duplicate.
    """
    _REGISTRY.clear()
