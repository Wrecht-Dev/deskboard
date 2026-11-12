"""Extended write surface: post management, moderation, queue and settings.

Every call is captured from a monkeypatched ``client.request``; this module never
opens a socket and never touches an account.
"""
from __future__ import annotations

import json

import pytest

from deskboard.api.client import TumblrClient
from deskboard.models import Post


def capture_request(monkeypatch, client: TumblrClient, response=None):
    calls: list[dict] = []

    def fake_request(path, params=None, method="GET", body=None, **kwargs):  # noqa: ARG001
        calls.append({"path": path, "params": params, "method": method, "body": body})
        return response if response is not None else {}

    monkeypatch.setattr(client, "request", fake_request)
    return calls


def post(post_id: str = "42", blog: str = "myblog") -> Post:
    return Post(id=post_id, blog_name=blog, reblog_key="reblog-key")


# --------------------------------------------------------------------------- #
# post management
# --------------------------------------------------------------------------- #

def test_post_management_routes_and_bodies(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)
    item = post()

    client.delete_post(item)
    client.set_post_state(item, state="draft")
    client.pin_post(item)
    client.unpin_post(item)

    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/v2/blog/myblog/post/delete"),
        ("POST", "/v2/blog/myblog/post/edit"),
        ("POST", "/v2/blog/myblog/posts/42/pin"),
        ("DELETE", "/v2/blog/myblog/posts/42/pin"),
    ]
    assert calls[0]["body"] == {"id": "42"}
    assert calls[1]["body"] == {"id": "42", "state": "draft"}
    assert calls[2]["body"] is None
    assert calls[3]["body"] is None


def test_edit_post_is_a_put_with_json_content_and_tags(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)
    item = post()
    content = [{"type": "text", "text": "edited"}]

    client.edit_post(item, content, state="queue", tags=["art", "photo"])

    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == "/v2/blog/myblog/posts/42"
    assert json.loads(call["body"]["content"]) == content
    assert call["body"]["state"] == "queue"
    assert call["body"]["tags"] == "art,photo"


# --------------------------------------------------------------------------- #
# blocking
# --------------------------------------------------------------------------- #

def test_block_and_unblock_blog_payloads(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.block_blog("acting", "target", post_id="post-1", force=True)
    client.unblock_blog("acting", "target")

    assert calls == [
        {
            "path": "/v2/blog/acting/blocks",
            "params": None,
            "method": "POST",
            "body": {
                "blocked_tumblelog": "target",
                "force": True,
                "post_id": "post-1",
            },
        },
        {
            "path": "/v2/blog/acting/blocks",
            "params": None,
            "method": "DELETE",
            "body": {"blocked_tumblelog": "target"},
        },
    ]


def test_block_blog_omits_absent_post_id(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.block_blog("acting", "target")

    assert calls[0]["body"] == {"blocked_tumblelog": "target", "force": False}


# --------------------------------------------------------------------------- #
# polls
# --------------------------------------------------------------------------- #

def test_vote_poll_accepts_string_and_list_selections(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.vote_poll("poll-1", "option-a")
    client.vote_poll("poll-2", ["option-a", "option-b"])

    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/v2/polls/poll-1"),
        ("POST", "/v2/polls/poll-2"),
    ]
    assert calls[0]["body"] == {"selected": "option-a"}
    assert calls[1]["body"] == {"selected": ["option-a", "option-b"]}


# --------------------------------------------------------------------------- #
# followed tags
# --------------------------------------------------------------------------- #

def test_follow_tag_adds_source_as_a_query_parameter(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.follow_tag("art", source="tagged")
    client.follow_tag("design")

    assert calls[0]["path"] == "/v2/user/tags/add"
    assert calls[0]["params"] == {"source": "tagged"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {"tag": "art"}
    assert calls[1]["params"] is None
    assert calls[1]["body"] == {"tag": "design"}


def test_unfollow_tag_payload(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.unfollow_tag("art")

    assert calls == [
        {
            "path": "/v2/user/tags/remove",
            "params": None,
            "method": "POST",
            "body": {"tag": "art"},
        }
    ]


# --------------------------------------------------------------------------- #
# subscriptions
# --------------------------------------------------------------------------- #

def test_blog_and_post_subscription_methods(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)
    item = post()

    client.subscribe_blog("staff")
    client.unsubscribe_blog("staff")
    client.subscribe_post(item)
    client.unsubscribe_post(item)

    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/v2/blog/staff/subscription"),
        ("DELETE", "/v2/blog/staff/subscription"),
        ("POST", "/v2/blog/myblog/posts/42/subscription"),
        ("DELETE", "/v2/blog/myblog/posts/42/subscription"),
    ]
    assert all(c["body"] is None for c in calls)


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #

def test_queue_controls(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.set_queue_paused("myblog", True)
    client.shuffle_queue("myblog")
    client.reorder_queue("myblog", "post-2")
    client.reorder_queue("myblog", "post-3", insert_after="post-2")

    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/v2/blog/myblog/posts/queue/state"),
        ("POST", "/v2/blog/myblog/posts/queue/shuffle"),
        ("POST", "/v2/blog/myblog/posts/queue/reorder"),
        ("POST", "/v2/blog/myblog/posts/queue/reorder"),
    ]
    assert calls[0]["body"] == {"paused": True}
    assert calls[1]["body"] is None
    assert calls[2]["body"] == {"post_id": "post-2"}
    assert calls[3]["body"] == {"post_id": "post-3", "insert_after": "post-2"}


# --------------------------------------------------------------------------- #
# inbox / activity
# --------------------------------------------------------------------------- #

def test_inbox_and_activity_deletes(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.delete_submission("myblog")
    client.delete_inbox_submissions()
    client.mark_activity_read("myblog")

    assert [(c["method"], c["path"]) for c in calls] == [
        ("DELETE", "/v2/blog/myblog/posts/submission"),
        ("DELETE", "/v2/user/inbox"),
        ("POST", "/v2/blog/myblog/activity_last_read"),
    ]
    assert all(c["body"] is None for c in calls)


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #

def test_delete_conversation_uses_query_params_and_delete(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.delete_conversation("conv-1", "alice")

    assert calls == [
        {
            "path": "/v2/conversations/messages",
            "params": {"conversation_id": "conv-1", "participant": "alice"},
            "method": "DELETE",
            "body": None,
        }
    ]


def test_flag_conversation_payload(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.flag_conversation("conv-1", "alice")
    client.flag_conversation("conv-2", "bob", type_="harassment", context="profile")

    assert calls[0]["path"] == "/v2/conversations/flag"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {
        "type": "spam",
        "context": "inline",
        "conversation_id": "conv-1",
        "participant": "alice",
    }
    assert calls[1]["body"] == {
        "type": "harassment",
        "context": "profile",
        "conversation_id": "conv-2",
        "participant": "bob",
    }


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

def test_update_blog_settings_drops_none_and_unwraps(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client, response={"response": {"ok": True}})

    result = client.update_blog_settings("myblog", title="New", theme=None, ask=True)

    assert result == {"ok": True}
    assert calls[0]["path"] == "/v2/blog/myblog/settings"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {"title": "New", "ask": True}


def test_update_user_settings_drops_none_and_unwraps(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client, response={"response": {"ok": 1}})

    result = client.update_user_settings(name="me", unused=None)

    assert result == {"ok": 1}
    assert calls[0]["path"] == "/v2/user/settings"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {"name": "me"}


# --------------------------------------------------------------------------- #
# mention suggestions
# --------------------------------------------------------------------------- #

def test_mention_suggestions_strips_and_encodes_the_prefix(monkeypatch):
    client = TumblrClient()
    calls = capture_request(
        monkeypatch,
        client,
        response={"response": {"blogs": [{"name": "artist"}, "not-a-dict"]}},
    )

    assert client.mention_suggestions("a b/c?d") == [{"name": "artist"}]
    assert calls[0]["path"] == "/v2/mention/abcd"
    assert calls[0]["params"] == {}
    assert calls[0]["method"] == "GET"


def test_mention_suggestions_empty_prefix_uses_the_bare_route(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client, response={"response": {"blogs": []}})

    assert client.mention_suggestions("") == []
    assert calls[0]["path"] == "/v2/mention"


def test_mention_suggestions_keeps_hyphens(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client, response={"response": {"blogs": []}})

    client.mention_suggestions("my-blog")

    assert calls[0]["path"] == "/v2/mention/my-blog"
