"""Feed sources: the small objects that turn a sidebar entry into pages of posts.

A source is deliberately dumb — it knows how to ask the client for the first
page and how to follow a cursor.  All pagination state lives in the widget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import Page, TumblrClient

# Sidebar kinds
HOME = "home"
EXPLORE = "explore"
DASHBOARD = "dashboard"
LIKES = "likes"
BLOG = "blog"
TAGGED = "tagged"
MYTAGS = "mytags"
SEARCH = "search"
FOLLOWING = "following"


@dataclass
class FeedSource:
    kind: str = DASHBOARD
    value: str = ""
    title: str = ""
    subtitle: str = ""
    icon: str = "user-home-symbolic"
    sort: str = "latest"  # tagged feeds: "latest" | "top"

    @property
    def key(self) -> str:
        """Stable identity used for sidebar selection and caching."""
        suffix = f":{self.sort}" if self.kind == TAGGED and self.sort != "latest" else ""
        return (f"{self.kind}:{self.value}" if self.value else self.kind) + suffix

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        if self.kind == HOME:
            return "Home"
        if self.kind == EXPLORE:
            return "Explore"
        if self.kind == DASHBOARD:
            return "Following"
        if self.kind == LIKES:
            return "Likes"
        if self.kind == BLOG:
            return self.value or "Blog"
        if self.kind == TAGGED:
            return f"#{self.value}"
        if self.kind == SEARCH:
            return f"Search: {self.value}"
        return self.kind.title()

    def load(self, client: TumblrClient, cursor: str | None = None) -> Page:
        """Fetch one page. Raise ``TumblrError`` on failure."""
        if cursor:
            return client.fetch_page(cursor)
        if self.kind == HOME:
            return client.get_home()
        if self.kind == EXPLORE:
            return client.get_explore()
        if self.kind == DASHBOARD:
            return client.get_dashboard()
        if self.kind == LIKES:
            return client.get_likes()
        if self.kind == BLOG:
            return client.get_blog_posts(self.value)
        if self.kind == TAGGED:
            try:
                return client.get_tagged(self.value, sort=self.sort)
            except TypeError:
                # Older clients and the offline demo predate the sort argument.
                return client.get_tagged(self.value)
        if self.kind == SEARCH:
            return client.search(self.value)
        if self.kind == FOLLOWING:
            return client.get_dashboard()
        raise ValueError(f"unknown feed kind: {self.kind}")

    def requires_session(self) -> bool:
        return self.kind in (HOME, DASHBOARD, LIKES, FOLLOWING, SEARCH)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "value": self.value}
        if self.kind == TAGGED:
            data["sort"] = self.sort
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedSource":
        kind = str(data.get("kind") or DASHBOARD)
        value = str(data.get("value") or "")
        sort = str(data.get("sort") or "latest")
        return cls(kind=kind, value=value, sort=sort)


class FollowedTagsSource(FeedSource):
    """A feed built from *all* the tags this account follows.

    Tumblr has no single "posts from my tags" endpoint, so each page pulls the
    first page of several followed tags concurrently, merges, de-duplicates and
    orders them the same way a tag page does (newest first for ``latest``, most
    notes first for ``top``). The cursor is the index of the next tag batch.
    """

    batch: int = 6

    def __init__(self, kind: str = MYTAGS, sort: str = "latest", title: str = "Your tags", **kwargs) -> None:
        super().__init__(kind=kind, sort=sort, title=title, icon="tag-symbolic", **kwargs)
        self._tags: list[str] | None = None
        self._tags_error: str = ""

    # -- helper ------------------------------------------------------------ #
    def _followed(self, client) -> list[str]:
        if self._tags is None:
            try:
                self._tags = list(client.get_followed_tags())
            except Exception as exc:  # noqa: BLE001 - surfaced through the feed
                self._tags_error = str(exc) or "Could not load your tags."
                self._tags = []
        return self._tags

    # -- FeedSource -------------------------------------------------------- #
    def load(self, client, cursor: str | None = None):
        from concurrent.futures import ThreadPoolExecutor

        from .client import Page

        tags = self._followed(client)
        if not tags and self._tags_error:
            raise RuntimeError(self._tags_error)
        offset = 0
        if cursor:
            import json

            try:
                offset = int(json.loads(cursor).get("offset", 0))
            except (TypeError, ValueError):
                offset = 0
        if offset >= len(tags):
            return Page(posts=[], next_url=None)
        chunk = tags[offset:offset + self.batch]

        def fetch(tag: str):
            try:
                return client.get_tagged(tag, limit=14, sort=self.sort).posts
            except Exception:  # noqa: BLE001 - one bad tag must not kill the feed
                return []

        posts = []
        with ThreadPoolExecutor(max_workers=min(4, len(chunk)) or 1) as pool:
            for batch in pool.map(fetch, chunk):
                posts.extend(batch)

        seen: set[str] = set()
        unique = []
        for post in posts:
            if post.id and post.id not in seen:
                seen.add(post.id)
                unique.append(post)
        if self.sort == "top":
            unique.sort(key=lambda p: p.note_count, reverse=True)
        else:
            unique.sort(key=lambda p: p.timestamp, reverse=True)

        import json as _json

        next_offset = offset + self.batch
        next_url = _json.dumps({"offset": next_offset}) if next_offset < len(tags) else None
        return Page(posts=unique, next_url=next_url, total=None)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "sort": self.sort}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FollowedTagsSource":
        return cls(sort=str(data.get("sort") or "latest"))
