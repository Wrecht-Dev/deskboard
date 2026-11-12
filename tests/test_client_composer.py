"""Composer write tests: NPF post creation and multipart media upload.

All HTTP is mocked.  These tests pin the request body shapes and the error
surfacing the composer UI depends on.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from deskboard.api.client import TumblrClient, TumblrError


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


def http_error(status: int = 400, detail: bytes = b'{"code":8001,"message":"bad post"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://www.tumblr.com/api/v2/blog/myblog/posts",
        status,
        "Bad Request",
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


def capture_request(monkeypatch, client: TumblrClient, response=None):
    calls: list[dict] = []

    def fake_request(path, params=None, method="GET", body=None, **kwargs):  # noqa: ARG001
        calls.append({"path": path, "params": params, "method": method, "body": body})
        return response if response is not None else {"response": {"id": "post-1"}}

    monkeypatch.setattr(client, "request", fake_request)
    return calls


# --------------------------------------------------------------------------- #
# create_post
# --------------------------------------------------------------------------- #

def test_create_post_encodes_content_layout_tags_and_publish_on(monkeypatch):
    client = TumblrClient()
    content = [
        {"type": "text", "text": "hello"},
        {"type": "image", "media": [{"url": "https://example.test/a.jpg"}]},
    ]
    layout = [{"type": "rows"}, {"type": "blocks"}]
    calls = capture_request(
        monkeypatch,
        client,
        response={"response": {"id": "post-1", "state": "queue"}},
    )

    result = client.create_post(
        "myblog",
        content,
        state="queue",
        tags=["art", "photography"],
        layout=layout,
        publish_on="2026-11-11T20:00:00Z",
    )

    assert result == {"id": "post-1", "state": "queue"}
    assert len(calls) == 1
    call = calls[0]
    assert call["path"] == "/v2/blog/myblog/posts"
    assert call["method"] == "POST"
    body = call["body"]
    assert body["state"] == "queue"
    assert json.loads(body["content"]) == content
    assert json.loads(body["layout"]) == layout
    assert body["tags"] == "art,photography"
    assert body["publish_on"] == "2026-11-11T20:00:00Z"


def test_create_post_tags_accept_a_string(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)

    client.create_post("myblog", [{"type": "text", "text": "x"}], tags="one,two")

    assert calls[0]["body"]["tags"] == "one,two"


def test_create_post_default_layout_and_no_optional_publish_on(monkeypatch):
    client = TumblrClient()
    calls = capture_request(monkeypatch, client)
    content = [{"type": "text", "text": "minimal"}]

    client.create_post("myblog", content)

    body = calls[0]["body"]
    assert body["state"] == "published"
    assert json.loads(body["layout"]) == [{"type": "rows"}]
    assert json.loads(body["content"]) == content
    assert "publish_on" not in body
    assert "tags" not in body


def test_create_post_rejects_invalid_state_before_any_request(monkeypatch):
    client = TumblrClient()
    calls: list[object] = []

    def fail_request(*args, **kwargs):  # noqa: ARG001
        calls.append(True)
        raise AssertionError("request must not be sent for an invalid state")

    monkeypatch.setattr(client, "request", fail_request)

    with pytest.raises(ValueError, match="unknown post state"):
        client.create_post("myblog", [{"type": "text", "text": "x"}], state="published?")

    assert calls == []


def test_create_post_400_code_8001_surfaces_as_tumblr_error(monkeypatch):
    client = TumblrClient()
    client._csrf = "csrf-token"  # the POST body needs a CSRF header first
    patch_urlopen(
        monkeypatch,
        [http_error(400, b'{"code":8001,"message":"Your post is too long"}')],
    )

    with pytest.raises(TumblrError) as raised:
        client.create_post("myblog", [{"type": "text", "text": "x"}])

    assert raised.value.status == 400
    assert "8001" in raised.value.body
    assert "Your post is too long" in raised.value.body
    assert str(raised.value)


# --------------------------------------------------------------------------- #
# upload_image (composer path)
# --------------------------------------------------------------------------- #

def test_upload_image_carries_csrf_and_preserves_spaces_in_filename(monkeypatch, tmp_path):
    client = TumblrClient()
    client._csrf = "csrf-token"
    image = tmp_path / "my holiday photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nholiday")
    seen = patch_urlopen(
        monkeypatch, [FakeResponse(b'{"response": {"id": "media-1"}}')]
    )

    result = client.upload_image(str(image))

    assert result == {"response": {"id": "media-1"}}
    assert len(seen) == 1
    request = seen[0]
    assert request.full_url == "https://www.tumblr.com/api/v2/media/image"
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert headers["x-csrf"] == "csrf-token"
    content_type = headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]
    assert b'filename="my holiday photo.png"' in request.data
    assert image.read_bytes() in request.data
    assert request.data.endswith(f"\r\n--{boundary}--\r\n".encode())


def test_upload_image_http_error_surfaces_as_tumblr_error(monkeypatch, tmp_path):
    client = TumblrClient()
    client._csrf = "csrf-token"
    image = tmp_path / "photo.png"
    image.write_bytes(b"bytes")
    patch_urlopen(monkeypatch, [http_error(500, b"upload failed")])

    with pytest.raises(TumblrError) as raised:
        client.upload_image(str(image))

    assert raised.value.status == 500
    assert raised.value.url.endswith("/v2/media/image")
