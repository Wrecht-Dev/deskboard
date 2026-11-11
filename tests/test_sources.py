"""Feed sources: the small objects that turn sidebar entries into pages."""
from __future__ import annotations

from deskboard.api.client import Page
from deskboard.api.sources import FeedSource
from deskboard.models import Post


def test_home_source_label_and_session_requirement():
    source = FeedSource(kind="home")

    assert source.label == "Home"
    assert source.key == "home"
    assert source.requires_session() is True
    assert source.as_dict() == {"kind": "home", "value": ""}


def test_home_source_load_calls_get_home():
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0
            self.cursor_calls: list[str] = []

        def get_home(self, limit: int = 20) -> Page:  # noqa: ARG002
            self.calls += 1
            return Page(posts=[Post(id="home-1")], next_url="https://www.tumblr.com/next")

        def fetch_page(self, cursor: str) -> Page:
            self.cursor_calls.append(cursor)
            return Page(posts=[])

    client = FakeClient()
    page = FeedSource(kind="home").load(client)

    assert client.calls == 1
    assert client.cursor_calls == []
    assert [post.id for post in page.posts] == ["home-1"]
    assert page.next_url == "https://www.tumblr.com/next"


def test_home_source_from_dict_round_trips():
    source = FeedSource.from_dict({"kind": "home"})

    assert source.kind == "home"
    assert source.label == "Home"
    assert source.requires_session() is True
    round_tripped = FeedSource.from_dict(source.as_dict())
    assert round_tripped.as_dict() == source.as_dict() == {"kind": "home", "value": ""}
    assert round_tripped.kind == "home"
    assert round_tripped.label == "Home"
