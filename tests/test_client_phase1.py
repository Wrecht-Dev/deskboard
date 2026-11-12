"""Phase 1 read-only client surface: notifications, DMs, notes, discovery, upload.

Every request is monkeypatched; this module never opens a socket.  Payloads are
representative captured shapes (wrapped ``response`` objects, bare lists and
``links.next.href`` cursors).
"""
from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message

import pytest

from deskboard.api.client import ItemPage, Page, TumblrClient, TumblrError


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, payload: bytes = b'{"response": {}}') -> None:
        self._payload = payload
        self.headers = Message()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(status: int = 500, detail: bytes = b"server error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://www.tumblr.com/api/v2/media/image",
        status,
        "Server Error",
        Message(),
        io.BytesIO(detail),
    )


def patch_urlopen(monkeypatch, responses: list):
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None, context=None):  # noqa: ARG001
        seen.append(request)
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def item_payload(key: str, items: list[dict], next_url: str | None = None) -> dict:
    response: dict = {key: items}
    if next_url:
        response["links"] = {"next": {"href": next_url}}
    return {"response": response}


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

def test_get_notifications_parses_notifications_and_next_link(monkeypatch):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []
    payload = item_payload(
        "notifications",
        [{"id": "n1"}, {"id": "n2"}, "not-a-dict"],
        next_url="https://www.tumblr.com/api/v2/blog/staff/notifications?before=123",
    )

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return payload

    monkeypatch.setattr(client, "request", fake_request)
    page = client.get_notifications("staff", limit=10, before="123")

    assert isinstance(page, ItemPage)
    assert page.items == [{"id": "n1"}, {"id": "n2"}]
    assert page.next_url == "https://www.tumblr.com/api/v2/blog/staff/notifications?before=123"
    assert calls == [
        ("/v2/blog/staff/notifications", {"limit": 10, "before": "123"})
    ]
    assert bool(page) is True


def test_get_notifications_accepts_a_bare_response_list(monkeypatch):
    client = TumblrClient()
    monkeypatch.setattr(
        client, "request", lambda *args, **kwargs: {"response": [{"id": "n1"}, {"id": "n2"}]}
    )

    page = client.get_notifications("staff")

    assert page.items == [{"id": "n1"}, {"id": "n2"}]
    assert page.next_url is None


def test_get_notifications_accepts_a_top_level_bare_list(monkeypatch):
    """The same shape /v2/tagged uses: the whole response is a JSON array."""
    client = TumblrClient()
    monkeypatch.setattr(
        client, "request", lambda *args, **kwargs: [{"id": "n1"}, {"id": "n2"}]
    )

    page = client.get_notifications("staff")

    assert page.items == [{"id": "n1"}, {"id": "n2"}]


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #

def test_get_conversations_parses_and_uses_the_right_paths(monkeypatch):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return item_payload("conversations", [{"id": "c1"}],
                            "https://www.tumblr.com/api/v2/conversations?before=9")

    monkeypatch.setattr(client, "request", fake_request)

    page = client.get_conversations(limit=5, before="9")
    assert page.items == [{"id": "c1"}]
    assert page.next_url.endswith("before=9")

    requests_page = client.get_conversations(limit=5, before="9", requests=True)
    assert requests_page.items == [{"id": "c1"}]
    assert calls == [
        ("/v2/conversations", {"limit": 5, "before": "9"}),
        ("/v2/conversations/requests", {"limit": 5, "before": "9"}),
    ]


def test_get_conversation_uses_query_selector_not_a_path_segment(monkeypatch):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return item_payload("messages", [{"id": "m1"}])

    monkeypatch.setattr(client, "request", fake_request)
    page = client.get_conversation("12345", limit=25, before="99")

    assert page.items == [{"id": "m1"}]
    assert calls == [
        ("/v2/conversations/messages",
         {"conversation_id": "12345", "limit": 25, "before": "99"})
    ]
    # The old bug used /v2/conversations/12345/messages; pin the exact route.
    assert "/12345/" not in calls[0][0]


# --------------------------------------------------------------------------- #
# inbox, drafts, queue
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("method_name", "args", "expected_path"),
    [
        ("get_inbox", (), "/v2/user/inbox"),
        ("get_drafts", ("staff",), "/v2/blog/staff/posts/draft"),
        ("get_queue", ("staff",), "/v2/blog/staff/posts/queue"),
    ],
)
def test_post_list_routes_return_pages(monkeypatch, method_name, args, expected_path):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return {
            "response": {
                "posts": [
                    {"type": "text", "id": "1", "blogName": "staff", "body": "hello"}
                ]
            }
        }

    monkeypatch.setattr(client, "request", fake_request)
    page = getattr(client, method_name)(*args, limit=10)

    assert isinstance(page, Page)
    assert [post.id for post in page.posts] == ["1"]
    assert calls[0][0] == expected_path
    assert calls[0][1] == {"limit": 10}


# --------------------------------------------------------------------------- #
# discovery and settings
# --------------------------------------------------------------------------- #

def test_search_blogs_returns_the_response_dict(monkeypatch):
    client = TumblrClient()
    expected = {
        "blogs": [{"name": "example"}],
        "tags": ["art"],
        "communities": [{"name": "community"}],
    }
    monkeypatch.setattr(client, "request", lambda *a, **k: {"response": expected})

    assert client.search_blogs("example", limit=5) == expected


def test_get_gif_popular_parses_the_next_offset(monkeypatch):
    client = TumblrClient()
    payload = {
        "response": {
            "gifs": [{"id": "g1"}, {"id": "g2"}, "not-a-dict"],
            "links": {
                "next": {
                    "href": "https://www.tumblr.com/api/v2/gif/popular?offset=40&limit=20"
                }
            },
        }
    }
    monkeypatch.setattr(client, "request", lambda *a, **k: payload)

    gifs, next_offset = client.get_gif_popular(offset=20)

    assert gifs == [{"id": "g1"}, {"id": "g2"}]
    assert next_offset == 40


def test_get_gif_popular_without_a_next_link(monkeypatch):
    client = TumblrClient()
    monkeypatch.setattr(client, "request", lambda *a, **k: {"response": {"gifs": []}})

    assert client.get_gif_popular() == ([], None)


def test_get_gif_popular_next_link_without_offset_returns_none(monkeypatch):
    client = TumblrClient()
    payload = {
        "response": {
            "gifs": [{"id": "g1"}],
            "links": {"next": {"href": "https://www.tumblr.com/api/v2/gif/popular?limit=20"}},
        }
    }
    monkeypatch.setattr(client, "request", lambda *a, **k: payload)

    gifs, next_offset = client.get_gif_popular()

    assert gifs == [{"id": "g1"}]
    assert next_offset is None


def test_settings_return_the_response_dict(monkeypatch):
    client = TumblrClient()
    monkeypatch.setattr(
        client, "request", lambda *a, **k: {"response": {"ask": True, "theme": "dark"}}
    )

    assert client.get_blog_settings("staff") == {"ask": True, "theme": "dark"}
    assert client.get_user_settings() == {"ask": True, "theme": "dark"}


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #

def test_get_notes_uses_blog_route_and_query_parameters(monkeypatch):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return item_payload("notes", [{"id": "note-1"}])

    monkeypatch.setattr(client, "request", fake_request)
    page = client.get_notes("staff", "98765", mode="reblogs", before="1234")

    assert page.items == [{"id": "note-1"}]
    assert calls == [
        (
            "/v2/blog/staff/notes",
            {"id": "98765", "mode": "reblogs", "beforeTimestamp": "1234"},
        )
    ]


def test_get_notes_defaults_to_mode_all(monkeypatch):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return item_payload("notes", [])

    monkeypatch.setattr(client, "request", fake_request)
    client.get_notes("staff", "1")

    assert calls == [("/v2/blog/staff/notes", {"id": "1", "mode": "all"})]


# --------------------------------------------------------------------------- #
# bare-list Page handling (the old /v2/tagged bug)
# --------------------------------------------------------------------------- #

def test_to_page_handles_a_top_level_bare_list(monkeypatch):
    client = TumblrClient()
    payload = [
        {"type": "text", "id": "tag-1", "blogName": "staff", "body": "tagged"},
        {"type": "text", "id": "tag-2", "blogName": "staff", "body": "tagged too"},
    ]
    monkeypatch.setattr(client, "request", lambda *a, **k: payload)

    page = client.get_tagged("art")

    assert isinstance(page, Page)
    assert [post.id for post in page.posts] == ["tag-1", "tag-2"]
    assert page.next_url is None


# --------------------------------------------------------------------------- #
# image upload (multipart)
# --------------------------------------------------------------------------- #

def test_upload_image_builds_multipart_body_and_posts(monkeypatch, tmp_path):
    client = TumblrClient()
    client._csrf = "csrf-token"  # avoid the separate homepage CSRF fetch
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    seen = patch_urlopen(
        monkeypatch, [FakeResponse(b'{"response": {"id": "media-1"}}')]
    )

    result = client.upload_image(str(image))

    assert result == {"response": {"id": "media-1"}}
    assert len(seen) == 1
    request = seen[0]
    assert request.full_url == "https://www.tumblr.com/api/v2/media/image"
    assert request.get_method() == "POST"

    headers = {key.lower(): value for key, value in request.headers.items()}
    content_type = headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]

    body = request.data
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'Content-Disposition: form-data; name="image"; filename="pixel.png"' in body
    assert b"Content-Type: image/png" in body
    assert image.read_bytes() in body
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())


def test_upload_image_raises_tumblr_error_on_http_error(monkeypatch, tmp_path):
    client = TumblrClient()
    client._csrf = "csrf-token"
    image = tmp_path / "broken.png"
    image.write_bytes(b"not-really-a-png")
    patch_urlopen(monkeypatch, [http_error(500, b"upload failed")])

    with pytest.raises(TumblrError) as raised:
        client.upload_image(str(image))

    assert raised.value.status == 500
    assert raised.value.url.endswith("/v2/media/image")
    assert "server error" in str(raised.value).lower()
