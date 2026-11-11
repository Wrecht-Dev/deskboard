"""API client tests — no network access is used anywhere in this module."""
from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from deskboard import config
from deskboard.api.client import (
    Page,
    TumblrClient,
    TumblrError,
    _as_int,
    _explain,
    _next_href,
)
from deskboard.models import Post, parse_posts


# --------------------------------------------------------------------------- #
# pagination helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"links": {"next": {"href": "https://www.tumblr.com/next"}}},
         "https://www.tumblr.com/next"),
        ({"links": {"next": "https://www.tumblr.com/next"}}, "https://www.tumblr.com/next"),
        ({"nextUrl": "https://www.tumblr.com/next-url"}, "https://www.tumblr.com/next-url"),
        ({"next_url": "https://www.tumblr.com/next-snake"}, "https://www.tumblr.com/next-snake"),
        ({"nextCursor": "cursor"}, "cursor"),
        ({"next_cursor": "snake-cursor"}, "snake-cursor"),
        ({"links": {"prev": {"href": "https://www.tumblr.com/prev"}}}, None),
        ({"links": {}}, None),
        ({}, None),
        (None, None),
        ("not-a-dict", None),
    ],
)
def test_next_href_shapes(payload, expected):
    assert _next_href(payload) == expected


def test_page_truthiness():
    assert bool(Page(posts=[Post(id="1")])) is True
    assert bool(Page()) is False
    # A cursor without posts is still not a usable page.
    assert bool(Page(next_url="https://www.tumblr.com/next")) is False


def test_as_int():
    assert _as_int("42") == 42
    assert _as_int(0) == 0
    assert _as_int(None) is None
    assert _as_int("not a number") is None


# --------------------------------------------------------------------------- #
# error explanations
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("status", "detail", "needles"),
    [
        (401, "Unauthorized", ("expired", "Sign in")),
        (401, "", ("401",)),
        (403, "", ("403", "signed-in")),
        (404, "", ("Not found",)),
        (429, "", ("Rate limited",)),
        (500, "", ("server error", "500")),
        (503, "", ("server error", "503")),
        (418, "", ("unexpected status", "418")),
    ],
)
def test_explain_messages_are_actionable(status, detail, needles):
    message = _explain(status, detail)
    for needle in needles:
        assert needle.lower() in message.lower(), message


def test_tumblr_error_metadata():
    error = TumblrError("nope", status=401, url="https://www.tumblr.com/")
    assert error.is_auth_error is True
    assert error.status == 401
    assert str(error) == "nope"
    assert TumblrError("nope", status=500).is_auth_error is False


# --------------------------------------------------------------------------- #
# Page construction from captured payloads (request is monkeypatched)
# --------------------------------------------------------------------------- #

def _first_payload_with_posts(fixture_payloads: dict[str, dict]) -> dict:
    for payload in fixture_payloads.values():
        response = payload.get("response", payload)
        if isinstance(response, dict) and response.get("posts"):
            return payload
    pytest.skip("no fixture with a response.posts payload")


def test_to_page_on_captured_payload(fixture_payloads, monkeypatch):
    payload = _first_payload_with_posts(fixture_payloads)
    response = payload.get("response", payload)
    expected_posts = parse_posts(response)

    client = TumblrClient()
    calls: list[str] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append(path)
        return payload

    monkeypatch.setattr(client, "request", fake_request)
    page = client.get_blog_posts("staff")

    assert calls == ["/v2/blog/staff/posts"]
    assert isinstance(page, Page)
    assert len(page.posts) == len(expected_posts)
    assert [post.id for post in page.posts] == [post.id for post in expected_posts]
    assert page.next_url == _next_href(response)
    total = response.get("totalPosts")
    assert page.total == (int(total) if total is not None else None)
    assert page.raw == response


def test_fetch_page_uses_the_given_url_without_network(fixture_payloads, monkeypatch):
    payload = _first_payload_with_posts(fixture_payloads)
    client = TumblrClient()
    seen: list[str] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        seen.append(path)
        return payload

    monkeypatch.setattr(client, "request", fake_request)
    page = client.fetch_page("https://www.tumblr.com/api/v2/timeline/dashboard?page=2")

    assert seen == ["https://www.tumblr.com/api/v2/timeline/dashboard?page=2"]
    assert page.posts


def test_to_page_wrapped_timeline_with_next_link(monkeypatch):
    payload = {
        "response": {
            "timeline": {
                "elements": [
                    {"objectType": "post", "type": "blocks", "id": "1", "blogName": "a",
                     "content": [{"type": "text", "text": "one"}]},
                    {"objectType": "title", "title": "Recommended"},
                    {"objectType": "carousel", "id": "promo"},
                    {"objectType": "post", "type": "blocks", "id": "2", "blogName": "b",
                     "content": [{"type": "text", "text": "two"}]},
                ],
                "links": {"next": {"href": "https://www.tumblr.com/api/v2/timeline/dashboard?page=2"}},
            },
            "totalPosts": 42,
        }
    }
    client = TumblrClient()
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: payload)

    page = client.get_dashboard()

    assert [post.id for post in page.posts] == ["1", "2"]
    assert page.next_url == "https://www.tumblr.com/api/v2/timeline/dashboard?page=2"
    assert page.total == 42


def test_to_page_handles_empty_payload_and_nested_response():
    client = TumblrClient()
    page = client._to_page({})
    assert page.posts == [] and page.next_url is None and page.total is None

    nested = client._to_page(
        {"response": {"response": {"posts": [{"type": "text", "id": "1", "body": "x"}],
                                   "totalPosts": "3"}}}
    )
    assert [post.id for post in nested.posts] == ["1"]
    assert nested.total == 3


# --------------------------------------------------------------------------- #
# small client behaviour without touching the network
# --------------------------------------------------------------------------- #

def test_client_repr_never_leaks_cookie_values():
    client = TumblrClient(cookies={"sid": "very-secret-value"})
    text = repr(client)
    assert "very-secret-value" not in text
    assert "yes" in text
    assert repr(TumblrClient()).endswith("session=no>")


def test_client_cookie_and_url_helpers():
    client = TumblrClient(cookies={"a": "1", "b": "2"})
    assert client.has_session is True
    headers = client._headers()
    assert headers["Cookie"] == "a=1; b=2"
    assert headers["Accept"].startswith("application/json")
    assert "Cookie" not in TumblrClient()._headers()

    assert client._absolute("https://example.test/x") == "https://example.test/x"
    assert client._absolute("/v2/user/info") == config.API_BASE + "/v2/user/info"
    assert client._absolute("/api/v2/user/info") == config.ORIGIN + "/api/v2/user/info"
    assert client._absolute("/svc/foo") == config.ORIGIN + "/svc/foo"

    client.set_cookies({})
    assert client.has_session is False


# --------------------------------------------------------------------------- #
# Home ("For you") tab
# --------------------------------------------------------------------------- #

def _home_payload() -> dict:
    return {
        "response": {
            "timeline": {
                "elements": [
                    {"objectType": "post", "type": "blocks", "id": "101", "blogName": "one",
                     "content": [{"type": "text", "text": "first"}]},
                    {"objectType": "client_side_ad_waterfall", "id": "ad-1"},
                    {"objectType": "title", "title": "For you"},
                    {"objectType": "carousel", "id": "promo-1"},
                    {"objectType": "post", "type": "blocks", "id": "102", "blogName": "two",
                     "content": [{"type": "text", "text": "second"}]},
                    {"type": "blocks", "id": "103", "blogName": "three",
                     "content": [{"type": "text", "text": "third"}]},
                ],
                "links": {
                    "next": {
                        "href": "https://www.tumblr.com/api/v2/tabs/for_you?cursor=next-page"
                    }
                },
            }
        }
    }


def test_get_home_parses_wrapped_timeline_and_next_link(monkeypatch):
    client = TumblrClient()
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: _home_payload())

    page = client.get_home()

    assert isinstance(page, Page)
    assert [post.id for post in page.posts] == ["101", "102", "103"]
    assert page.next_url == "https://www.tumblr.com/api/v2/tabs/for_you?cursor=next-page"
    assert bool(page) is True


def test_get_home_sends_the_expected_path_and_params(monkeypatch):
    client = TumblrClient()
    calls: list[tuple[str, dict]] = []

    def fake_request(path, params=None, **kwargs):  # noqa: ARG001
        calls.append((path, params or {}))
        return _home_payload()

    monkeypatch.setattr(client, "request", fake_request)
    client.get_home(limit=7)

    assert calls == [
        ("/v2/tabs/for_you", {"notes_info": "false", "reblog_info": "true", "limit": 7})
    ]


def test_get_home_without_a_timeline_returns_an_empty_page(monkeypatch):
    client = TumblrClient()
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: {"response": {}})

    page = client.get_home()

    assert page.posts == []
    assert page.next_url is None
    assert page.total is None
    assert bool(page) is False


# --------------------------------------------------------------------------- #
# 401 auth-refresh retry
# --------------------------------------------------------------------------- #

def _http_error(status: int = 401, detail: bytes = b"Unauthorized") -> urllib.error.HTTPError:
    from email.message import Message

    return urllib.error.HTTPError(
        "https://www.tumblr.com/api/v2/user/info",
        status,
        "Unauthorized",
        Message(),
        io.BytesIO(detail),
    )


class _FakeHTTPResponse:
    def __init__(self, payload: bytes = b'{"response": {"ok": true}}') -> None:
        from email.message import Message

        self._payload = payload
        self.headers = Message()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(monkeypatch, responses: list):
    """Drive ``urllib.request.urlopen`` from a queue of responses/exceptions."""
    urls: list[str] = []
    cookie_headers: list[str | None] = []

    def fake_urlopen(request, timeout=None, context=None):  # noqa: ARG001
        urls.append(request.full_url)
        cookie_headers.append(request.get_header("Cookie"))
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return urls, cookie_headers


def test_auth_refresh_adopts_cookies_and_retries_once(monkeypatch):
    refreshed: list[int] = []

    def on_auth_failed():
        refreshed.append(1)
        return {"sid": "fresh"}

    client = TumblrClient(cookies={"sid": "stale"}, on_auth_failed=on_auth_failed)
    urls, cookie_headers = _patch_urlopen(
        monkeypatch, [_http_error(401), _FakeHTTPResponse()]
    )

    result = client.request("/v2/user/info")

    assert result == {"response": {"ok": True}}
    assert len(refreshed) == 1
    assert len(urls) == 2
    assert "sid=stale" in (cookie_headers[0] or "")
    assert "sid=fresh" in (cookie_headers[1] or "")
    assert client.cookies == {"sid": "fresh"}


def test_auth_refresh_retry_that_also_401s_raises_without_looping(monkeypatch):
    refreshed: list[int] = []

    def on_auth_failed():
        refreshed.append(1)
        return {"sid": "fresh"}

    client = TumblrClient(cookies={"sid": "stale"}, on_auth_failed=on_auth_failed)
    urls, _ = _patch_urlopen(monkeypatch, [_http_error(401), _http_error(401)])

    with pytest.raises(TumblrError) as raised:
        client.request("/v2/user/info")

    assert raised.value.status == 401
    assert len(refreshed) == 1
    assert len(urls) == 2


def test_auth_refresh_returning_none_surfaces_the_original_error(monkeypatch):
    refreshed: list[int] = []

    def on_auth_failed():
        refreshed.append(1)
        return None

    client = TumblrClient(cookies={"sid": "stale"}, on_auth_failed=on_auth_failed)
    urls, _ = _patch_urlopen(monkeypatch, [_http_error(401)])

    with pytest.raises(TumblrError) as raised:
        client.request("/v2/user/info")

    assert raised.value.status == 401
    assert "expired" in str(raised.value).lower()
    assert len(refreshed) == 1
    assert len(urls) == 1
