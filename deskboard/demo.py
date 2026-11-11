"""Offline demo client.

``python3 -m deskboard --demo`` runs the whole interface against captured
payloads so the UI can be developed, screenshotted and tested without a session
(or a network).  The fixtures are ordinary public Tumblr responses.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .api.client import Page, TumblrError
from .models import Blog, Post, get, parse_posts

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"


class DemoClient:
    """Implements the read surface of :class:`TumblrClient` from local files."""

    has_session = True

    def __init__(self, fixture_dir: Path | None = None) -> None:
        import os

        self.directory = fixture_dir or FIXTURE_DIR
        self._pages: list[list[Post]] = []
        self._blogs: dict[str, Blog] = {}
        # ``DESKBOARD_DEMO_INFINITE=1`` loops the fixtures with synthetic ids for
        # scroll-performance testing (a few hundred cards).
        self.infinite = os.environ.get("DESKBOARD_DEMO_INFINITE", "") not in ("", "0")
        self._load()

    # -- loading ----------------------------------------------------------- #
    def _load(self) -> None:
        if not self.directory.exists():
            return
        posts: list[Post] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            response = payload.get("response", payload)
            posts.extend(parse_posts(response))
            blog = response.get("blog") if isinstance(response, dict) else None
            if isinstance(blog, dict) and blog.get("name"):
                self._blogs[str(blog["name"])] = Blog.from_api(blog)
        # chunk into pages of 10 so scrolling exercises the pager
        self._pages = [posts[i:i + 10] for i in range(0, len(posts), 10)] or [[]]

    # -- client surface ---------------------------------------------------- #
    def set_cookies(self, cookies) -> None:  # pragma: no cover - demo is fixed
        pass

    def _page(self, index: int) -> Page:
        if self.infinite and self._pages:
            index = index % len(self._pages)
            total = None
        elif index >= len(self._pages):
            return Page(posts=[], next_url=None)
        if self.infinite and self._pages:
            next_url = f"demo://page/{index + 1}"
        else:
            next_url = f"demo://page/{index + 1}" if index + 1 < len(self._pages) else None
        return Page(posts=self._pages[index], next_url=next_url, total=sum(len(p) for p in self._pages))

    def fetch_page(self, url: str) -> Page:
        try:
            index = int(str(url).rsplit("/", 1)[-1])
        except (TypeError, ValueError):
            index = 0
        return self._page(index)

    def get_dashboard(self, limit: int = 20) -> Page:
        return self._page(0)

    def get_home(self, limit: int = 20) -> Page:
        """The "For you" tab: start one page in so demo shows distinct content."""
        return self._page(1 if len(self._pages) > 1 else 0)

    def get_explore(self, limit: int = 20) -> Page:
        return self._page(2 if len(self._pages) > 2 else 0)

    def get_trending_tags(self, limit: int = 20) -> list[str]:
        return ["art", "photography", "design", "typography", "museum"]

    def get_blog_posts(self, blog: str, limit: int = 20, tag: str | None = None) -> Page:
        posts = [p for page in self._pages for p in page]
        if blog:
            matching = [p for p in posts if p.blog_name.lower() == blog.lower()]
            posts = matching or posts
        return Page(posts=posts[: limit or 10], next_url=None, total=len(posts))

    def get_blog_info(self, blog: str) -> Blog:
        blog = blog or ""
        if blog in self._blogs:
            return self._blogs[blog]
        return Blog(name=blog, title=blog, url=f"https://{blog}.tumblr.com")

    def get_user_info(self) -> dict:
        return {
            "name": "demo",
            "blogs": [{"name": "demo", "title": "Demo blog"}],
        }

    def get_likes(self, limit: int = 20) -> Page:
        return self._page(0)

    def get_following(self, limit: int = 50) -> list[Blog]:
        return list(self._blogs.values())

    def get_followed_tags(self) -> list[str]:
        return ["art", "photography", "design"]

    def search(self, query: str, limit: int = 20) -> Page:
        return self._page(0)

    def get_tagged(self, tag: str, limit: int = 20) -> Page:
        return self._page(0)

    def get_notes(self, post_id: str, blog: str = "") -> Page:
        return Page(posts=[], next_url=None)

    # -- writes are inert in demo mode ------------------------------------- #
    def like(self, post: Post) -> None:
        post.is_liked = True

    def unlike(self, post: Post) -> None:
        post.is_liked = False

    def reblog(self, post: Post, blog: str, comment: str = "") -> None:
        raise TumblrError("Demo mode: posting is disabled.")

    def follow(self, blog: str) -> None:
        pass

    def unfollow(self, blog: str) -> None:
        pass
