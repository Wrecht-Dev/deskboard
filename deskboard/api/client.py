"""Tumblr internal API client.

The public website does not use the documented ``api.tumblr.com`` OAuth API; it
talks to ``https://www.tumblr.com/api/…`` ("kraken") with a bearer token that is
served inside the page bootstrap plus the visitor's own session cookies.

We do the same, which means:

* with no cookies you get everything a logged-out visitor can see
  (any blog's posts, tag pages, search);
* with the user's session cookies you also get the dashboard, likes, follows
  and the write actions.

All requests are blocking; call them from a worker thread.
"""
from __future__ import annotations

import gzip
import json
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .. import config
from ..models import Blog, Post, get, parse_posts


class TumblrError(Exception):
    """Any failure talking to Tumblr, with a message that is safe to show."""

    def __init__(self, message: str, status: int = 0, url: str = "", body: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)


@dataclass
class ItemPage:
    """A page of arbitrary API objects (notifications, DMs, drafts...)."""

    items: list[dict] = field(default_factory=list)
    next_url: str | None = None
    total: int | None = None
    raw: dict | list = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.items)


@dataclass
class Page:
    """One page of a paginated collection."""

    posts: list[Post] = field(default_factory=list)
    next_url: str | None = None
    total: int | None = None
    raw: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.posts)


# Tumblr validates ``fields[…]`` strictly and 400s on unknown attributes, so we
# ask for whole objects instead of risking schema drift.


_TOKEN_RE = re.compile(r'API_TOKEN\\?"\s*:\s*\\?"([A-Za-z0-9]{20,80})')


class TumblrClient:
    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        token: str | None = None,
        timeout: float = 25.0,
        on_auth_failed: "Callable[[], dict[str, str] | None] | None" = None,
    ) -> None:
        self.cookies: dict[str, str] = dict(cookies or {})
        # Called once on a 401/403: return fresh cookies to retry with, or None.
        self.on_auth_failed = on_auth_failed
        self._auth_refreshed = False
        self.token = token or config.DEFAULT_API_TOKEN
        self.timeout = timeout
        self._lock = threading.Lock()
        self._csrf: str | None = None
        self._token_refreshed = False
        self._ssl = ssl.create_default_context()

    # -- identity ---------------------------------------------------------- #
    @property
    def has_session(self) -> bool:
        return bool(self.cookies)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        self.cookies = dict(cookies)

    def __repr__(self) -> str:  # never leak cookie values
        return f"<TumblrClient session={'yes' if self.has_session else 'no'}>"

    # -- low level --------------------------------------------------------- #
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json;format=camelcase",
            "Accept-Encoding": "gzip",
            "User-Agent": config.USER_AGENT,
            "Referer": config.ORIGIN + "/",
            "X-Version": "1",
            "Origin": config.ORIGIN,
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if extra:
            headers.update(extra)
        return headers

    def _absolute(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if not url.startswith("/"):
            url = "/" + url
        # ``/v2/...`` paths are relative to the API root, ``/svc`` etc. to the site.
        if url.startswith("/v2/") or url.startswith("/api/"):
            if url.startswith("/api/"):
                return config.ORIGIN + url
            return config.API_BASE + url
        return config.ORIGIN + url

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict:
        url = self._absolute(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)

        payload: bytes | None = None
        headers = self._headers()
        if body is not None:
            payload = urllib.parse.urlencode(body, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["X-CSRF"] = self.csrf_token() or ""

        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read()
                if exc.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                detail = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - best effort only
                pass
            status = exc.code
            if status == 401 and method != "GET" and retry_auth and "1022" in detail:
                # stale/absent CSRF token: refresh it and retry exactly once
                with self._lock:
                    had_token = bool(self._csrf)
                    self._csrf = None
                if self.csrf_token(refresh=True):
                    return self.request(path, params, method=method, body=body, retry_auth=False)
                if had_token:
                    raise TumblrError(_explain(status, detail), status=status, url=url, body=detail) from exc
            if status in (401, 403) and retry_auth and self.cookies and not self._auth_refreshed:
                self._auth_refreshed = True
                fresh = None
                try:
                    fresh = self.on_auth_failed() if self.on_auth_failed else None
                except Exception:  # noqa: BLE001 - refresh is best effort
                    fresh = None
                if fresh:
                    self.cookies = dict(fresh)
                    return self.request(path, params, method=method, body=body, retry_auth=False)
            if status in (401, 403) and retry_auth and not self.cookies:
                # The public token rotates every few months; re-read it once.
                if self.refresh_token():
                    return self.request(path, params, method=method, body=body, retry_auth=False)
            raise TumblrError(_explain(status, detail), status=status, url=url, body=detail) from exc
        except urllib.error.URLError as exc:
            raise TumblrError(f"Network unavailable: {exc.reason}", url=url) from exc
        except TimeoutError as exc:
            raise TumblrError("Tumblr took too long to respond.", url=url) from exc
        except json.JSONDecodeError as exc:
            raise TumblrError("Tumblr sent an unreadable response.", url=url) from exc

    def refresh_token(self) -> bool:
        """Re-read the bearer token from the website bootstrap."""
        with self._lock:
            if self._token_refreshed:
                return False
            self._token_refreshed = True
        try:
            request = urllib.request.Request(
                config.ORIGIN + "/",
                headers={"User-Agent": config.USER_AGENT, "Accept": "text/html"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl) as response:
                page = response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - token refresh is best effort
            return False
        match = _TOKEN_RE.search(page)
        if not match:
            return False
        candidate = match.group(1)
        if candidate == self.token:
            return False
        self.token = candidate
        return True

    def csrf_token(self, refresh: bool = False) -> str | None:
        """The per-session CSRF token required by every write endpoint.

        The website embeds it in the dashboard bootstrap as
        ``"csrfToken":"<value>"`` — sometimes JSON-escaped inside a
        ``JSON.parse("…")`` payload, which is why the pattern tolerates
        backslashes. Cached for the session; call with ``refresh=True`` after a
        401 to pick up a rotated token.
        """
        with self._lock:
            if self._csrf and not refresh:
                return self._csrf
        import gzip as _gzip

        headers = self._headers({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        })
        headers.pop("Accept-Encoding", None)
        for path in ("/dashboard", "/"):
            try:
                request = urllib.request.Request(config.ORIGIN + path, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = _gzip.decompress(raw)
                    page = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - writes will fail with a clear message
                continue
            match = re.search(r'csrfToken\\{0,2}"\s*:\s*\\{0,2}"([^"\\]{8,80})', page)
            if match:
                with self._lock:
                    self._csrf = match.group(1)
                return self._csrf
        return None

    # -- pagination -------------------------------------------------------- #
    def fetch_page(self, url: str) -> Page:
        data = self.request(url)
        return self._to_page(data)

    def _to_page(self, data: dict) -> Page:
        if isinstance(data, list):
            # some routes (e.g. /v2/tagged) answer with a bare array
            return Page(posts=parse_posts({"posts": data}), next_url=None, total=None, raw=data)
        response = data.get("response", data) if isinstance(data, dict) else {}
        if isinstance(response, dict) and "response" in response and not response.get("posts"):
            response = response["response"]
        posts = parse_posts(response)
        next_url = _next_href(response)
        if next_url is None and isinstance(response, dict):
            timeline = response.get("timeline")
            if isinstance(timeline, dict):
                next_url = _next_href(timeline)
        return Page(
            posts=posts,
            next_url=next_url,
            total=_as_int(get(response, "totalPosts")),
            raw=response if isinstance(response, dict) else {},
        )

    # -- read endpoints ---------------------------------------------------- #
    def get_dashboard(self, limit: int = config.PAGE_SIZE) -> Page:
        return self._to_page(
            self.request(
                "/v2/timeline/dashboard",
                {"limit": limit, "reblog_info": "true", "wrapped-timeline": "true"},
            )
        )

    def get_home(self, limit: int = config.PAGE_SIZE) -> Page:
        """The web client's **Home** tab ("For you").

        ``/v2/tabs`` advertises it as ``stuff_for_you`` with the timeline URI
        ``/v2/tabs/for_you``.  Tumblr caps a page at a handful of posts no
        matter what ``limit`` says, so callers should pipeline requests.
        """
        return self._to_page(
            self.request(
                "/v2/tabs/for_you",
                {"notes_info": "false", "reblog_info": "true", "limit": limit},
            )
        )

    def get_explore(self, limit: int = config.PAGE_SIZE) -> Page:
        """The web client's Explore timeline (recommended posts)."""
        return self._to_page(
            self.request(
                "/v2/explore/recommended",
                {"reblog_info": "true", "limit": limit},
            )
        )

    def get_trending_tags(self, limit: int = 20) -> list[str]:
        data = self.request("/v2/explore/trending/tags", {"limit": limit})
        response = data.get("response", {})
        timeline = response.get("timeline") if isinstance(response, dict) else None
        elements = (timeline or {}).get("elements") or []
        tags: list[str] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            name = str(element.get("tagName") or element.get("name") or "")
            for resource in element.get("resources") or []:
                if name:
                    break
                if isinstance(resource, dict) and resource.get("tag"):
                    name = str(resource["tag"])
            if not name:
                href = ((element.get("links") or {}).get("destination") or {}).get("href") or ""
                if "/tagged/" in href:
                    name = href.split("/tagged/")[-1].split("?")[0]
            if name and name not in tags:
                tags.append(name)
        return tags

    def get_blog_posts(self, blog: str, limit: int = config.PAGE_SIZE, tag: str | None = None) -> Page:
        params: dict[str, Any] = {"limit": limit, "reblog_info": "true"}
        if tag:
            params["tag"] = tag
        return self._to_page(self.request(f"/v2/blog/{urllib.parse.quote(blog)}/posts", params))

    def get_blog_info(self, blog: str) -> Blog:
        data = self.request(f"/v2/blog/{urllib.parse.quote(blog)}/info")
        response = data.get("response", {})
        return Blog.from_api(response.get("blog") if isinstance(response, dict) else {})

    def get_likes(self, limit: int = config.PAGE_SIZE) -> Page:
        return self._to_page(self.request("/v2/user/likes", {"limit": limit}))

    def get_following(self, limit: int = 50) -> list[Blog]:
        data = self.request("/v2/user/following", {"limit": limit})
        response = data.get("response", {})
        blogs = response.get("blogs") if isinstance(response, dict) else None
        return [Blog.from_api(item) for item in (blogs or []) if isinstance(item, dict)]

    def get_followed_tags(self, limit: int = 50) -> list[str]:
        """Tags this account follows.

        Tumblr's ``/v2/user/tags`` returns ``{count, tags: [{id, name, …}]}``.
        (``/v2/user/tags/following`` exists in the web bundle but answers 400,
        so it is only kept as a fallback for future API reshuffles.)
        """
        try:
            data = self.request("/v2/user/tags", {"limit": limit})
        except TumblrError as exc:
            if exc.status != 400:
                raise
            data = self.request("/v2/user/tags/following")
        response = data.get("response", {})
        tags = response.get("tags") if isinstance(response, dict) else None
        out: list[str] = []
        for item in tags or []:
            name = ""
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                name = str(item.get("name") or "")
            if name and name not in out:
                out.append(name)
        return out

    def get_user_info(self) -> dict:
        data = self.request("/v2/user/info")
        return data.get("response", {}).get("user", {}) or {}

    def search(self, query: str, limit: int = config.PAGE_SIZE) -> Page:
        return self._to_page(self.request("/v2/timeline/search", {"query": query, "limit": limit}))

    def get_tagged(self, tag: str, limit: int = config.PAGE_SIZE) -> Page:
        """Tag pages still live on the older ``/v2/tagged`` route."""
        return self._to_page(self.request("/v2/tagged", {"tag": tag, "limit": limit}))

    def _to_items(self, data: dict, keys: tuple[str, ...], before_key: str = "before") -> ItemPage:
        if isinstance(data, list):
            # some routes answer with a bare top-level array
            return ItemPage(items=[i for i in data if isinstance(i, dict)],
                            next_url=None, total=None, raw={"items": data})
        response = data.get("response", data) if isinstance(data, dict) else {}
        items: list = []
        raw: dict = {}
        if isinstance(response, dict):
            raw = response
            for key in keys:
                value = response.get(key)
                if isinstance(value, list):
                    items = value
                    break
        elif isinstance(response, list):
            items = response
            raw = {"items": response}
        links = response.get("links") if isinstance(response, dict) else None
        next_url = None
        if isinstance(links, dict):
            nxt = links.get("next")
            if isinstance(nxt, dict):
                next_url = nxt.get("href")
        return ItemPage(items=[i for i in items if isinstance(i, dict)], next_url=next_url,
                        total=_as_int(get(response, "totalConversations")) if isinstance(response, dict) else None,
                        raw=raw)

    # -- notifications / activity ------------------------------------------ #
    def get_notifications(self, blog: str, limit: int = 30, before: str | None = None) -> ItemPage:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        return self._to_items(
            self.request(f"/v2/blog/{urllib.parse.quote(blog)}/notifications", params),
            ("notifications", "items"),
        )

    # -- messaging --------------------------------------------------------- #
    def get_conversations(self, limit: int = 30, before: str | None = None, requests: bool = False) -> ItemPage:
        path = "/v2/conversations/requests" if requests else "/v2/conversations"
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        return self._to_items(self.request(path, params), ("conversations",))

    def get_conversation(self, conversation_id: str, limit: int = 50, before: str | None = None) -> ItemPage:
        """Messages in one conversation.

        The route is shared with sending: ``GET /v2/conversations/messages``
        with ``conversation_id`` (or two ``participant`` names) as the selector.
        """
        params: dict[str, Any] = {"conversation_id": conversation_id, "limit": limit}
        if before:
            params["before"] = before
        return self._to_items(
            self.request("/v2/conversations/messages", params), ("messages", "items")
        )

    def mark_conversation_read(self, conversation_id: str, participant: str = "") -> None:
        self.request(
            "/v2/conversations/mark_as_read",
            method="POST", body={"conversation_id": conversation_id, "participant": participant},
        )

    # -- inbox (asks / submissions) ---------------------------------------- #
    def get_inbox(self, limit: int = 20) -> Page:
        return self._to_page(self.request("/v2/user/inbox", {"limit": limit}))

    # -- drafts / queue ---------------------------------------------------- #
    def get_drafts(self, blog: str, limit: int = 20) -> Page:
        return self._to_page(
            self.request(f"/v2/blog/{urllib.parse.quote(blog)}/posts/draft", {"limit": limit})
        )

    def get_queue(self, blog: str, limit: int = 20) -> Page:
        return self._to_page(
            self.request(f"/v2/blog/{urllib.parse.quote(blog)}/posts/queue", {"limit": limit})
        )

    # -- notes ------------------------------------------------------------- #
    def get_notes(self, blog: str, post_id: str, mode: str = "all", before: str | None = None) -> ItemPage:
        """Notes on a post: ``GET /v2/blog/{blog}/notes?id=<post id>``."""
        params: dict[str, Any] = {"id": post_id, "mode": mode}
        if before:
            params["beforeTimestamp"] = before
        return self._to_items(
            self.request(f"/v2/blog/{urllib.parse.quote(blog)}/notes", params),
            ("notes", "items"),
        )

    # -- discovery --------------------------------------------------------- #
    def search_blogs(self, query: str, limit: int = 20) -> dict:
        data = self.request("/v2/search/blogs", {"query": query, "limit": limit})
        response = data.get("response", {})
        return response if isinstance(response, dict) else {}

    def get_gif_popular(self, offset: int = 0) -> tuple[list[dict], int | None]:
        data = self.request("/v2/gif/popular", {"offset": offset})
        response = data.get("response", {}) or {}
        gifs = [g for g in (response.get("gifs") or []) if isinstance(g, dict)]
        links = response.get("links") or {}
        nxt = (links.get("next") or {}).get("href") if isinstance(links, dict) else None
        next_offset = None
        if isinstance(nxt, str) and "offset=" in nxt:
            try:
                next_offset = int(nxt.split("offset=")[-1].split("&")[0])
            except ValueError:
                next_offset = None
        return gifs, next_offset

    # -- settings ---------------------------------------------------------- #
    def get_blog_settings(self, blog: str) -> dict:
        data = self.request(f"/v2/blog/{urllib.parse.quote(blog)}/settings")
        response = data.get("response", {})
        return response if isinstance(response, dict) else {}

    def get_user_settings(self) -> dict:
        data = self.request("/v2/user/settings")
        response = data.get("response", {})
        return response if isinstance(response, dict) else {}

    # -- composer ---------------------------------------------------------- #
    POST_STATES = ("published", "draft", "queue", "submission")

    def create_post(self, blog: str, content: list[dict], state: str = "published",
                    tags: str | list[str] | None = None, layout: list[dict] | None = None,
                    publish_on: str | None = None) -> dict:
        """Create a post from NPF content blocks.

        Mirrors the web composer: ``POST /v2/blog/{blog}/posts`` with
        ``content`` (NPF blocks), ``layout``, comma-joined ``tags`` and a
        ``state`` of published/draft/queue/submission.
        """
        if state not in self.POST_STATES:
            raise ValueError(f"unknown post state: {state}")
        body: dict[str, Any] = {
            "content": json.dumps(content, separators=(",", ":")),
            "state": state,
            "layout": json.dumps(layout or [{"type": "rows"}], separators=(",", ":")),
        }
        if tags:
            body["tags"] = ",".join(tags) if isinstance(tags, (list, tuple)) else str(tags)
        if publish_on:
            body["publish_on"] = publish_on
        data = self.request(f"/v2/blog/{urllib.parse.quote(blog)}/posts", method="POST", body=body)
        return data.get("response", {}) if isinstance(data, dict) else {}

    # -- notes moderation -------------------------------------------------- #
    def delete_note(self, blog: str, note_tumblelog: str, note_created_time: str | int,
                    post_id: str, scope: str = "") -> None:
        """Delete a note (e.g. your own reply) from a post."""
        body = {"note_tumblelog": note_tumblelog, "note_created_time": note_created_time,
                "post_id": post_id}
        if scope:
            body["scope"] = scope
        self.request(f"/v2/blog/{urllib.parse.quote(blog)}/notes/delete", method="POST", body=body)

    def flag_note(self, blog: str, flagged_blog: str, note_created_time: str | int = "") -> None:
        """Report a note on one of your posts."""
        body: dict[str, Any] = {"flagged_note_tumblelog_name": flagged_blog}
        if note_created_time:
            body["flagged_note_created_time"] = note_created_time
        self.request(f"/v2/blog/{urllib.parse.quote(blog)}/notes/flag", method="POST", body=body)

    def delete_reply(self, blog: str, post_id: str, reply_id: str) -> None:
        """Delete a reply from a post (your own reply)."""
        self.request(
            f"/v2/blog/{urllib.parse.quote(blog)}/post/{urllib.parse.quote(post_id)}"
            f"/replies/{urllib.parse.quote(reply_id)}",
            method="DELETE",
        )

    # -- post muting (in-feed) --------------------------------------------- #
    def mute_post(self, post: Post, seconds: int | None = None) -> None:
        body = {"mute_length_seconds": seconds} if seconds else None
        self.request(
            f"/v2/blog/{urllib.parse.quote(post.blog_name)}/posts/{urllib.parse.quote(post.id)}/mute",
            method="POST", body=body,
        )

    def unmute_post(self, post: Post) -> None:
        self.request(
            f"/v2/blog/{urllib.parse.quote(post.blog_name)}/posts/{urllib.parse.quote(post.id)}/mute",
            method="DELETE",
        )

    # -- media upload (composer) ------------------------------------------- #
    def upload_image(self, path: str) -> dict:
        """Upload a local image and return the Tumblr **media object**.

        The server wraps it in ``{"response": {...}}``; callers get the media
        object directly (ready for ``{"type": "image", "media": [obj]}``).
        """
        return self._media_object("/v2/media/image", path, "image")

    def upload_audio(self, path: str) -> dict:
        return self._media_object("/v2/media/audio", path, "audio")

    def _media_object(self, path: str, file_path: str, field: str) -> dict:
        data = self._upload(path, file_path, field)
        if isinstance(data, dict):
            media = data.get("response", data)
            if isinstance(media, dict):
                # some responses nest again under "media"
                inner = media.get("media")
                if isinstance(inner, dict):
                    return inner
                return media
        return {}

    def _upload(self, path: str, file_path: str, field: str) -> dict:
        import mimetypes
        import uuid
        from pathlib import Path as _Path

        boundary = "----deskboard" + uuid.uuid4().hex
        file_path = str(_Path(file_path).expanduser())
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as handle:
            payload = handle.read()
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{_Path(file_path).name}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),
            payload,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        url = self._absolute(path)
        request = urllib.request.Request(
            url, data=b"".join(parts), method="POST",
            headers=self._headers({
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-CSRF": self.csrf_token() or "",
            }),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout * 3, context=self._ssl) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise TumblrError(_explain(exc.code, detail), status=exc.code, url=url, body=detail) from exc
        except urllib.error.URLError as exc:
            raise TumblrError(f"Upload failed: {exc.reason}", url=url) from exc

    # -- write endpoints --------------------------------------------------- #
    def like(self, post: Post, placement_id: str = "") -> None:
        body = {"id": post.id, "reblog_key": post.reblog_key}
        if placement_id:
            body["placement_id"] = placement_id
        self.request("/v2/user/like", method="POST", body=body)

    def unlike(self, post: Post) -> None:
        self.request("/v2/user/unlike", method="POST", body={"id": post.id, "reblog_key": post.reblog_key})

    def reblog(self, post: Post, blog: str, comment: str = "", tags: str | list[str] | None = None,
               publish_on: str | None = None) -> None:
        """Reblog ``post`` to ``blog``.

        Covers the three actions Tumblr's own dialog offers:

        * **Reblog (with text)** — pass ``comment``;
        * **Reblog now** — leave ``comment=""`` so only the post is reblogged;
        * **Schedule reblog** — pass ``publish_on`` (ISO-8601 UTC) and Tumblr
          queues it for that time.

        ``publish_on`` is the only field not confirmed against a live write
        (scheduling is a public action); the rest is verified.
        """
        body: dict[str, Any] = {"id": post.id, "reblog_key": post.reblog_key, "comment": comment or ""}
        if tags:
            body["tags"] = ",".join(tags) if isinstance(tags, (list, tuple)) else str(tags)
        if publish_on:
            body["publish_on"] = publish_on
        self.request(f"/v2/blog/{urllib.parse.quote(post_blog := blog)}/post/reblog", method="POST", body=body)

    def reply(self, post: Post, blog: str, text: str, reply_as: str | None = None) -> None:
        """Reply to a post (the note visible on the post page)."""
        self.request(
            "/v2/user/post/reply",
            method="POST",
            body={
                "post_id": post.id,
                "reply_text": text,
                "reblog_key": post.reblog_key,
                "tumblelog": blog,
                "reply_as": reply_as or blog,
            },
        )

    def follow(self, blog: str) -> None:
        self.request("/v2/user/follow", method="POST", body={"url": blog})

    def unfollow(self, blog: str) -> None:
        self.request("/v2/user/unfollow", method="POST", body={"url": blog})

    # -- messaging (writes) ------------------------------------------------ #
    def send_message(self, conversation_id: str, participant: str, text: str,
                     participants: list[str] | None = None) -> None:
        self.request(
            "/v2/conversations/messages",
            method="POST",
            body={
                "conversation_id": conversation_id,
                "participant": participant,
                "participants": participants or [participant],
                "message": text,
                "type": "text",
            },
        )

    def accept_conversation(self, conversation_id: str, participant: str) -> None:
        self.request(
            "/v2/conversations/accept",
            method="POST",
            body={"conversation_id": conversation_id, "participant": participant},
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _next_href(response: Any) -> str | None:
    """Extract the next-page URL from any of Tumblr's pagination shapes."""
    if not isinstance(response, dict):
        return None
    links = response.get("links")
    if isinstance(links, dict):
        nxt = links.get("next")
        if isinstance(nxt, dict):
            href = nxt.get("href")
            if isinstance(href, str) and href:
                return href
        if isinstance(nxt, str) and nxt:
            return nxt
    for key in ("nextUrl", "next_url"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    cursor = get(response, "nextCursor") or get(response, "next_cursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _explain(status: int, detail: str) -> str:
    """Turn an API failure into something a person can act on."""
    if status == 401:
        if "Unauthorized" in detail:
            return "Tumblr refused the request: your session has expired or is missing. Sign in again."
        return "Tumblr refused the request (401)."
    if status == 403:
        return "Tumblr blocked that request (403). It may require a signed-in session."
    if status == 404:
        return "Not found — the blog or post may have been deleted."
    if status == 429:
        return "Rate limited by Tumblr. Give it a minute and retry."
    if status >= 500:
        return f"Tumblr had a server error ({status}). Try again shortly."
    return f"Tumblr returned an unexpected status ({status})."
