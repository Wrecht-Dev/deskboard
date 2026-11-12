"""Write auth and write payload tests (no sockets, no real account)."""
from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message

import pytest

from deskboard import config
from deskboard.api.client import Page, TumblrClient, TumblrError
from deskboard.models import Post


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


def http_error(status: int = 401, detail: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://www.tumblr.com/api/v2/user/like",
        status,
        "Unauthorized",
        Message(),
        io.BytesIO(detail),
    )


def csrf_page(token: str) -> bytes:
    # Plain form; the escaped JSON.parse form is covered separately.
    return f'<html><script>{{"csrfToken":"{token}"}}</script></html>'.encode()


def patch_urlopen(monkeypatch, queue: list):
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None, context=None):  # noqa: ARG001
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


# --------------------------------------------------------------------------- #
# CSRF token extraction / caching
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("token", "page"),
    [
        (
            "abc123.1794442238",
            b'<script>JSON.parse("{\\"csrfToken\\":\\"abc123.1794442238\\"}")</script>',
        ),
        (
            "plain.12345678",
            b'<script>{"csrfToken":"plain.12345678"}</script>',
        ),
    ],
)
def test_csrf_token_extracts_escaped_and_plain_forms(monkeypatch, token, page):
    client = TumblrClient()
    seen = patch_urlopen(monkeypatch, [FakeResponse(page)])

    assert client.csrf_token() == token
    assert len(seen) == 1
    assert seen[0].full_url == config.ORIGIN + "/dashboard"


def test_csrf_token_is_cached(monkeypatch):
    client = TumblrClient()
    seen = patch_urlopen(monkeypatch, [FakeResponse(csrf_page("cached.token.1"))])

    assert client.csrf_token() == "cached.token.1"
    assert len(seen) == 1

    # A second call must be answered from the cache; no URL opener at all.
    def unexpected_fetch(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("csrf_token() refetched a cached token")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_fetch)
    assert client.csrf_token() == "cached.token.1"


def test_csrf_token_refresh_refetches(monkeypatch):
    client = TumblrClient()
    seen = patch_urlopen(
        monkeypatch,
        [FakeResponse(csrf_page("old.token.1")), FakeResponse(csrf_page("new.token.2"))],
    )

    assert client.csrf_token() == "old.token.1"
    assert client.csrf_token(refresh=True) == "new.token.2"
    assert len(seen) == 2


def test_csrf_token_returns_none_when_no_page_has_a_token(monkeypatch):
    client = TumblrClient()
    # The method tries /dashboard and /, then gives up.
    seen = patch_urlopen(
        monkeypatch, [FakeResponse(b"<html>no token</html>"), FakeResponse(b"<html>still none</html>")]
    )

    assert client.csrf_token() is None
    assert [request.full_url for request in seen] == [
        config.ORIGIN + "/dashboard",
        config.ORIGIN + "/",
    ]


# --------------------------------------------------------------------------- #
# 401 code 1022 retry
# --------------------------------------------------------------------------- #

def test_1022_refreshes_csrf_and_retries_with_the_new_header(monkeypatch):
    client = TumblrClient()
    client._csrf = "stale-token"
    queue = [
        http_error(401, b'{"code":1022,"detail":"Unable to authorize"}'),
        FakeResponse(csrf_page("fresh.token.1")),
        FakeResponse(b'{"response": {"ok": true}}'),
    ]
    seen = patch_urlopen(monkeypatch, queue)

    result = client.request("/v2/user/like", method="POST", body={"id": "1"})

    assert result == {"response": {"ok": True}}
    assert len(seen) == 3
    assert seen[0].full_url == config.ORIGIN + "/api/v2/user/like"
    assert seen[0].get_header("X-csrf") == "stale-token"
    assert seen[1].full_url == config.ORIGIN + "/dashboard"
    assert seen[2].full_url == config.ORIGIN + "/api/v2/user/like"
    assert seen[2].get_header("X-csrf") == "fresh.token.1"
    assert client._csrf == "fresh.token.1"
    assert not queue


def test_1022_retry_that_401s_again_raises_without_looping(monkeypatch):
    client = TumblrClient()
    client._csrf = "stale-token"
    queue = [
        http_error(401, b'{"code":1022,"detail":"Unable to authorize"}'),
        FakeResponse(csrf_page("fresh.token.1")),
        http_error(401, b'{"code":1022,"detail":"Unable to authorize"}'),
    ]
    seen = patch_urlopen(monkeypatch, queue)

    with pytest.raises(TumblrError) as raised:
        client.request("/v2/user/like", method="POST", body={"id": "1"})

    assert raised.value.status == 401
    assert len(seen) == 3  # initial POST + CSRF fetch + one retry, no loop
    assert not queue


def test_get_401_does_not_take_the_csrf_retry_path(monkeypatch):
    client = TumblrClient(cookies={"sid": "session"})
    csrf_calls: list[bool] = []

    def csrf_spy(*args, **kwargs):  # noqa: ARG001
        csrf_calls.append(True)
        return "should-not-be-used"

    monkeypatch.setattr(client, "csrf_token", csrf_spy)
    seen = patch_urlopen(monkeypatch, [http_error(401, b"Unauthorized")])

    with pytest.raises(TumblrError) as raised:
        client.request("/v2/timeline/dashboard")

    assert raised.value.status == 401
    assert csrf_calls == []
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# write payloads
# --------------------------------------------------------------------------- #

def _capture_writes(monkeypatch, client: TumblrClient) -> list[dict]:
    calls: list[dict] = []

    def fake_request(path, params=None, method="GET", body=None, **kwargs):  # noqa: ARG001
        calls.append({"path": path, "params": params, "method": method, "body": body})
        return {}

    monkeypatch.setattr(client, "request", fake_request)
    return calls


def test_reblog_payload_joins_tags_and_uses_post_route(monkeypatch):
    client = TumblrClient()
    calls = _capture_writes(monkeypatch, client)
    post = Post(id="42", reblog_key="reblog-key")

    client.reblog(post, "myblog", "nice post", tags=["art", "photography"])
    client.reblog(post, "myblog", "no tags")
    client.reblog(post, "myblog", "string tags", tags="one,two")

    assert [call["path"] for call in calls] == [
        "/v2/blog/myblog/post/reblog",
        "/v2/blog/myblog/post/reblog",
        "/v2/blog/myblog/post/reblog",
    ]
    assert all(call["method"] == "POST" for call in calls)
    assert calls[0]["body"] == {
        "id": "42",
        "reblog_key": "reblog-key",
        "comment": "nice post",
        "tags": "art,photography",
    }
    assert "tags" not in calls[1]["body"]
    assert calls[2]["body"]["tags"] == "one,two"


def test_reply_payload(monkeypatch):
    client = TumblrClient()
    calls = _capture_writes(monkeypatch, client)
    post = Post(id="42", reblog_key="reblog-key")

    client.reply(post, "myblog", "hello there", reply_as="otherblog")

    assert calls == [
        {
            "path": "/v2/user/post/reply",
            "params": None,
            "method": "POST",
            "body": {
                "post_id": "42",
                "reply_text": "hello there",
                "reblog_key": "reblog-key",
                "tumblelog": "myblog",
                "reply_as": "otherblog",
            },
        }
    ]


def test_reply_defaults_reply_as_to_the_blog(monkeypatch):
    client = TumblrClient()
    calls = _capture_writes(monkeypatch, client)

    client.reply(Post(id="1", reblog_key="k"), "myblog", "hi")

    assert calls[0]["body"]["reply_as"] == "myblog"


def test_send_message_payload_and_participants(monkeypatch):
    client = TumblrClient()
    calls = _capture_writes(monkeypatch, client)

    client.send_message("conv-1", "alice", "hello")
    client.send_message("conv-2", "alice", "group", participants=["alice", "bob"])

    assert [call["path"] for call in calls] == [
        "/v2/conversations/messages",
        "/v2/conversations/messages",
    ]
    assert calls[0]["body"] == {
        "conversation_id": "conv-1",
        "participant": "alice",
        "participants": ["alice"],
        "message": "hello",
        "type": "text",
    }
    assert calls[1]["body"]["participants"] == ["alice", "bob"]
    assert all(call["method"] == "POST" for call in calls)


def test_accept_conversation_payload(monkeypatch):
    client = TumblrClient()
    calls = _capture_writes(monkeypatch, client)

    client.accept_conversation("conv-1", "alice")

    assert calls[0]["path"] == "/v2/conversations/accept"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {"conversation_id": "conv-1", "participant": "alice"}


def test_like_and_unlike_payloads(monkeypatch):
    client = TumblrClient()
    calls = _capture_writes(monkeypatch, client)
    post = Post(id="42", reblog_key="reblog-key")

    client.like(post)
    client.like(post, placement_id="placement-1")
    client.unlike(post)

    assert [call["path"] for call in calls] == [
        "/v2/user/like",
        "/v2/user/like",
        "/v2/user/unlike",
    ]
    assert calls[0]["body"] == {"id": "42", "reblog_key": "reblog-key"}
    assert calls[1]["body"] == {
        "id": "42",
        "reblog_key": "reblog-key",
        "placement_id": "placement-1",
    }
    assert calls[2]["body"] == {"id": "42", "reblog_key": "reblog-key"}
    assert all(call["method"] == "POST" for call in calls)


# --------------------------------------------------------------------------- #
# read paths must never call writes
# --------------------------------------------------------------------------- #

def test_read_paths_never_call_write_methods(monkeypatch):
    client = TumblrClient()

    def fake_request(path, params=None, method="GET", body=None, **kwargs):  # noqa: ARG001
        assert method == "GET", f"read path {path} used {method}"
        assert body is None, f"read path {path} sent a body"
        return {"response": {}}

    monkeypatch.setattr(client, "request", fake_request)

    def write_guard(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("a read path called a write method")

    for name in (
        "like",
        "unlike",
        "reblog",
        "reply",
        "send_message",
        "accept_conversation",
        "follow",
        "unfollow",
        "mark_conversation_read",
        "upload_image",
        "upload_audio",
    ):
        monkeypatch.setattr(client, name, write_guard)

    # Every read method in the current client surface.
    assert client.get_dashboard().posts == []
    assert client.get_home().posts == []
    assert client.get_blog_posts("staff").posts == []
    assert client.get_blog_info("staff").name == ""
    assert client.get_likes().posts == []
    assert client.get_following() == []
    assert client.get_followed_tags() == []
    assert client.get_user_info() == {}
    assert client.search("art").posts == []
    assert client.get_tagged("art").posts == []
    assert client.fetch_page("https://www.tumblr.com/api/v2/timeline/dashboard").posts == []
    assert client.get_notes("staff", "1").items == []
    assert client.get_notifications("staff").items == []
    assert client.get_conversations().items == []
    assert client.get_conversations(requests=True).items == []
    assert client.get_conversation("conv-1").items == []
    assert client.get_inbox().posts == []
    assert client.get_drafts("staff").posts == []
    assert client.get_queue("staff").posts == []
    assert client.search_blogs("art") == {}
    assert client.get_gif_popular() == ([], None)
    assert client.get_blog_settings("staff") == {}
    assert client.get_user_settings() == {}
