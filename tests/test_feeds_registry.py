from __future__ import annotations

import pytest

from ffpresnap.feeds._base import FeedItem
from ffpresnap.feeds._registry import (
    _reset_registry_for_tests,
    adapter_names,
    get_adapter,
    register_adapter,
)


class _FakeAdapter:
    name: str
    source_url = "https://example.com"

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    def fetch(self, *, full=False, fetch=None, is_seen=None):
        return iter(())

    def map_team(self, external_team: str) -> str | None:
        return None


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test starts with an empty registry to avoid cross-talk with
    the package-level imports of concrete adapters.
    """
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


def test_register_and_get_roundtrip():
    a = _FakeAdapter("alpha")
    register_adapter(a)
    assert get_adapter("alpha") is a


def test_register_duplicate_name_raises():
    register_adapter(_FakeAdapter("dup"))
    with pytest.raises(ValueError, match="already registered"):
        register_adapter(_FakeAdapter("dup"))


def test_get_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_adapter("nope")


def test_adapter_names_sorted_tuple():
    register_adapter(_FakeAdapter("zulu"))
    register_adapter(_FakeAdapter("alpha"))
    register_adapter(_FakeAdapter("mike"))
    assert adapter_names() == ("alpha", "mike", "zulu")


def test_feeditem_roundtrips_to_dict():
    item = FeedItem(
        external_id="32bw:1",
        external_player_id="42",
        external_player_name="Test Player",
        external_team="Team Foo",
        external_position="WR",
        source_url="https://example.com/x",
        source_author="Reporter",
        raw_html="<p>x</p>",
        cleaned_text="x",
        created_at="2026-04-29T00:00:00Z",
    )
    d = item.to_dict()
    assert d["external_id"] == "32bw:1"
    assert d["cleaned_text"] == "x"
    assert d["external_player_name"] == "Test Player"
