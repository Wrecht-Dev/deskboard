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

    def csrf_token(self) -> str | None:
        """Fetch the CSRF token required by write endpoints."""
        with self._lock:
            if self._csrf:
                return self._csrf
        try:
            request = urllib.request.Request(
                config.ORIGIN + "/",
                headers=self._headers({"Accept": "text/html"}),
            )
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl) as response:
                page = response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - write actions will simply fail loudly
            return None
        match = re.search(r'"csrfToken":"([^"]+)"', page)
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

    def get_notes(self, post_id: str, blog: str = "") -> Page:
        params = {"limit": 50}
        if blog:
            params["tumblelog"] = blog
        return self._to_page(self.request(f"/v2/notes/{urllib.parse.quote(post_id)}", params))

    # -- write endpoints --------------------------------------------------- #
    def like(self, post: Post) -> None:
        self.request("/v2/user/like", method="POST", body={"id": post.id, "reblog_key": post.reblog_key})

    def unlike(self, post: Post) -> None:
        self.request("/v2/user/unlike", method="POST", body={"id": post.id, "reblog_key": post.reblog_key})

    def reblog(self, post: Post, blog: str, comment: str = "") -> None:
        self.request(
            f"/v2/blog/{urllib.parse.quote(blog)}/post/reblog",
            method="POST",
            body={"id": post.id, "reblog_key": post.reblog_key, "comment": comment},
        )

    def follow(self, blog: str) -> None:
        self.request("/v2/user/follow", method="POST", body={"url": blog})

    def unfollow(self, blog: str) -> None:
        self.request("/v2/user/unfollow", method="POST", body={"url": blog})


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
