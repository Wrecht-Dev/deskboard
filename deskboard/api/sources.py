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
            return client.get_tagged(self.value, sort=self.sort)
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
        return cls(kind=kind, value=value)
