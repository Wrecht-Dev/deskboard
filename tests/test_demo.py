"""Offline demo client tests — the demo must exercise the same model surface."""
from __future__ import annotations

import json

import pytest

from deskboard.api.client import Page, TumblrError
from deskboard.demo import DemoClient
from deskboard.models import Blog, Post, parse_posts


def _all_demo_posts(client: DemoClient) -> list[Post]:
    return [post for page in client._pages for post in page]


def test_demo_client_loads_every_fixture_post(fixture_payloads):
    expected = 0
    for payload in fixture_payloads.values():
        expected += len(parse_posts(payload.get("response", payload)))

    client = DemoClient()
    posts = _all_demo_posts(client)
    assert len(posts) == expected
    assert posts
    assert all(post.id for post in posts)


def test_demo_pages_are_chunked_and_have_cursors():
    client = DemoClient()
    pages = client._pages
    assert pages
    assert all(len(page) <= 10 for page in pages)
    assert all(len(page) == 10 for page in pages[:-1])

    first = client.get_dashboard()
    assert isinstance(first, Page)
    assert first.posts == pages[0]
    assert first.total == sum(len(page) for page in pages)
    if len(pages) > 1:
        assert first.next_url == "demo://page/1"
    else:
        assert first.next_url is None


def test_demo_pagination_follows_every_page():
    client = DemoClient()
    expected = sum(len(page) for page in client._pages)

    page = client.get_dashboard()
    ids: list[str] = []
    seen_pages = 0
    while True:
        ids.extend(post.id for post in page.posts)
        seen_pages += 1
        assert seen_pages < 1000, "pagination did not terminate"
        if not page.next_url:
            break
        page = client.fetch_page(page.next_url)

    assert len(ids) == expected
    assert page.next_url is None


def test_demo_fetch_page_falls_back_to_the_first_page():
    client = DemoClient()
    first = client.get_dashboard()
    fallback = client.fetch_page("not a cursor")
    assert [post.id for post in fallback.posts] == [post.id for post in first.posts]


def test_demo_fetch_page_past_the_end_is_empty():
    client = DemoClient()
    page = client.fetch_page("demo://page/9999")
    assert page.posts == []
    assert page.next_url is None


def test_demo_empty_fixture_directory(tmp_path):
    empty = tmp_path / "fixtures"
    empty.mkdir()
    client = DemoClient(fixture_dir=empty)
    page = client.get_dashboard()
    assert page.posts == []
    assert page.next_url is None
    assert client.get_blog_posts("staff").posts == []


def test_demo_blog_helpers():
    client = DemoClient()
    posts = client.get_blog_posts("staff", limit=3)
    assert len(posts.posts) <= 3
    assert posts.total is not None

    known = next(iter(client._blogs))
    blog = client.get_blog_info(known)
    assert isinstance(blog, Blog)
    assert blog.name == known

    unknown = client.get_blog_info("definitely-not-a-real-blog")
    assert unknown.name == "definitely-not-a-real-blog"
    assert unknown.url == "https://definitely-not-a-real-blog.tumblr.com"

    following = client.get_following()
    assert following and all(isinstance(blog, Blog) for blog in following)
    assert client.get_user_info()["name"] == "demo"
    assert client.get_followed_tags()


def test_demo_reads_return_pages_and_writes_are_local():
    client = DemoClient()
    for page in (
        client.get_likes(),
        client.get_tagged("art"),
        client.search("art"),
        client.get_notes("123"),
    ):
        assert isinstance(page, Page)

    post = _all_demo_posts(client)[0]
    original = post.is_liked
    client.like(post)
    assert post.is_liked is True
    client.unlike(post)
    assert post.is_liked is False
    assert post.is_liked == original

    with pytest.raises(TumblrError):
        client.reblog(post, "demo", "hello")


def test_every_fixture_post_is_renderable_in_the_demo():
    client = DemoClient()
    for post in _all_demo_posts(client):
        blocks = list(post.content)
        for item in post.trail:
            blocks.extend(item.content)
        assert blocks, f"post {post.id} has no blocks at all"
        assert any(
            block.media
            or block.text.strip()
            or block.title.strip()
            or block.url.strip()
            or block.description.strip()
            or block.type in ("poll", "answer", "link")
            for block in blocks
        ), f"post {post.id} has no renderable block"


def test_fixture_files_are_valid_json(fixture_dir):
    paths = sorted(fixture_dir.glob("*.json"))
    assert paths
    for path in paths:
        payload = json.loads(path.read_text("utf-8"))
        assert isinstance(payload, dict)
