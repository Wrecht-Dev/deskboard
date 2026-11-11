"""Data model tests: NPF blocks, media renditions, trails and legacy posts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskboard.models import (
    Block,
    Blog,
    Media,
    Post,
    TrailItem,
    get,
    parse_posts,
    snake_case,
    upscale_image_url,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def renderable_blocks(post: Post) -> list[Block]:
    """Every block a card could actually draw for a post."""
    blocks = list(post.content)
    for item in post.trail:
        blocks.extend(item.content)
    return [
        block
        for block in blocks
        if block.media
        or block.text.strip()
        or block.title.strip()
        or block.url.strip()
        or block.description.strip()
        or block.type in ("poll", "answer", "link")
    ]


def test_fixture_directory_is_not_empty(fixture_dir):
    assert fixture_dir.is_dir(), f"missing fixtures: {fixture_dir}"
    assert list(fixture_dir.glob("*.json")), "data/fixtures has no captures"


@pytest.mark.parametrize("path", sorted((Path(__file__).resolve().parents[1] / "data/fixtures").glob("*.json")),
                         ids=lambda p: p.name)
def test_every_fixture_post_parses_into_renderable_post(path):
    payload = json.loads(path.read_text("utf-8"))
    response = payload.get("response", payload)
    posts = parse_posts(response)
    assert posts, f"{path.name}: no posts parsed"
    for post in posts:
        assert post.id, f"{path.name}: post without id"
        assert post.blog_name, f"{path.name}: post without blog name"
        assert renderable_blocks(post), f"{path.name}: post {post.id} has no renderable block"


# --------------------------------------------------------------------------- #
# camelCase / snake_case access
# --------------------------------------------------------------------------- #

def test_get_accepts_both_key_styles():
    assert get({"noteCount": 12}, "noteCount") == 12
    assert get({"note_count": 12}, "noteCount") == 12
    assert get({}, "missing", "fallback") == "fallback"
    assert get(None, "missing", 7) == 7


def test_snake_case_helper():
    assert snake_case("blogName") == "blog_name"
    assert snake_case("tagsV2") == "tags_v2"
    assert snake_case("already_snake") == "already_snake"


def test_post_from_api_accepts_snake_case_keys():
    post = Post.from_api(
        {
            "id_string": "42",
            "blog_name": "lowercase",
            "note_count": 7,
            "like_count": 3,
            "reblog_key": "abc",
            "post_url": "https://example.tumblr.com/post/42",
            "can_like": False,
            "is_nsfw": True,
            "timestamp": 123,
            "type": "text",
            "body": "hello",
        }
    )
    assert post.id == "42"
    assert post.blog_name == "lowercase"
    assert post.note_count == 7
    assert post.like_count == 3
    assert post.reblog_key == "abc"
    assert post.post_url.endswith("/post/42")
    assert post.can_like is False
    assert post.is_nsfw is True
    assert post.timestamp == 123
    assert [block.text for block in post.content] == ["hello"]


def test_post_from_api_accepts_camel_case_keys():
    post = Post.from_api(
        {
            "idString": "42",
            "blogName": "camelcase",
            "noteCount": 7,
            "likeCount": 3,
            "reblogKey": "abc",
            "postUrl": "https://example.tumblr.com/post/42",
            "canLike": False,
            "isNsfw": True,
            "timestamp": 123,
            "type": "text",
            "body": "hello",
        }
    )
    assert (post.id, post.blog_name, post.note_count, post.like_count) == ("42", "camelcase", 7, 3)
    assert post.reblog_key == "abc"
    assert post.is_nsfw is True and post.can_like is False


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #

def test_media_rendition_choice_uses_largest_area():
    block = Block.from_api(
        {
            "type": "image",
            "media": [
                {"mediaKey": "key", "url": "https://64.media.tumblr.com/a/s500x750/one.jpg",
                 "width": 500, "height": 750, "type": "image/jpeg"},
                {"mediaKey": "key", "url": "https://64.media.tumblr.com/a/s1280x1920/one.jpg",
                 "width": 1080, "height": 1350, "type": "image/jpeg"},
                {"mediaKey": "key", "url": "https://64.media.tumblr.com/a/s640x960/one.jpg",
                 "width": 640, "height": 800, "type": "image/jpeg"},
            ],
        }
    )
    assert len(block.media) == 1
    media = block.media[0]
    assert (media.width, media.height) == (1080, 1350)
    assert "/s1280x1920/" in media.url
    assert media.mime == "image/jpeg"


def test_media_rendition_choice_keeps_distinct_keys():
    block = Block.from_api(
        {
            "type": "image",
            "media": [
                {"mediaKey": "a", "url": "https://64.media.tumblr.com/a/s500x750/a.jpg",
                 "width": 500, "height": 750},
                {"mediaKey": "b", "url": "https://64.media.tumblr.com/b/s500x750/b.jpg",
                 "width": 500, "height": 750},
            ],
        }
    )
    assert len(block.media) == 2


def test_media_helpers():
    media = Media(url="https://64.media.tumblr.com/a/s500x750/x.gif", width=500, height=750,
                  mime="image/gif")
    assert media.is_gif is True
    assert media.aspect == pytest.approx(500 / 750)
    assert Media.from_api("https://example.test/a.jpg").url == "https://example.test/a.jpg"
    assert Media.from_api({"source": "https://example.test/a.jpg"}) is not None
    assert Media.from_api({}) is None


@pytest.mark.parametrize(
    ("url", "width", "expected"),
    [
        ("https://64.media.tumblr.com/abc/def/s500x750/photo.jpg", 1280,
         "https://64.media.tumblr.com/abc/def/s1280x1920/photo.jpg"),
        ("https://64.media.tumblr.com/abc/def/s500x750u/photo.jpg", 1280,
         "https://64.media.tumblr.com/abc/def/s1280x1920u/photo.jpg"),
        ("https://64.media.tumblr.com/abc/def/s500x750/photo.jpg", 640,
         "https://64.media.tumblr.com/abc/def/s640x960/photo.jpg"),
        ("https://64.media.tumblr.com/abc/def/s1280x1920/photo.jpg", 1280,
         "https://64.media.tumblr.com/abc/def/s1280x1920/photo.jpg"),
        ("https://example.com/abc/s500x750/photo.jpg", 1280,
         "https://example.com/abc/s500x750/photo.jpg"),
        ("https://64.media.tumblr.com/abc/photo.jpg", 1280,
         "https://64.media.tumblr.com/abc/photo.jpg"),
        ("", 1280, ""),
    ],
)
def test_upscale_image_url(url, width, expected):
    assert upscale_image_url(url, width) == expected


# --------------------------------------------------------------------------- #
# blocks and formatting
# --------------------------------------------------------------------------- #

def test_block_text_properties():
    heading = Block.from_api({"type": "heading1", "text": "Title"})
    assert heading.subtype == "heading1"
    assert heading.is_heading is True
    assert heading.text == "Title"

    quote = Block.from_api({"type": "text", "subtype": "quote", "text": "Words"})
    assert quote.subtype == "quote"
    assert quote.is_heading is False
    assert quote.indent_level == 1

    indented = Block.from_api({"type": "text", "subtype": "indented", "indent_level": 2})
    assert indented.indent_level == 2

    # A bare shorthand block still becomes readable text.
    shorthand = Block.from_api("just text")
    assert shorthand is not None
    assert shorthand.text == "just text"


def test_block_media_and_embed_properties():
    poster = Block.from_api(
        {
            "type": "video",
            "provider": "youtube",
            "url": "https://example.test/embed",
            "poster": [
                {"url": "https://64.media.tumblr.com/a/s500x750/p.jpg", "width": 500, "height": 750}
            ],
        }
    )
    assert poster is not None
    assert poster.is_video_block is True
    assert poster.provider == "youtube"
    assert poster.poster_url.endswith("/s500x750/p.jpg")
    assert len(poster.media) == 1
    assert poster.media[0].kind == "image"


def test_block_formatting_is_sorted():
    block = Block.from_api(
        {
            "type": "text",
            "text": "bold and italic",
            "formatting": [
                {"start": 9, "end": 15, "type": "italic"},
                {"start": 0, "end": 4, "type": "bold"},
                {"start": 0, "end": 4, "type": "bold", "url": "https://example.test"},
            ],
        }
    )
    assert [(f.start, f.end, f.type) for f in block.formatting] == [
        (0, 4, "bold"),
        (0, 4, "bold"),
        (9, 15, "italic"),
    ]
    assert block.formatting[0].url == ""
    assert block.formatting[1].url == "https://example.test"


def test_legacy_npf_content_wins_over_legacy_conversion():
    post = Post.from_api(
        {
            "type": "photo",
            "content": [{"type": "text", "text": "npf wins"}],
            "caption": "legacy caption",
            "photos": [{"original_size": {"url": "https://64.media.tumblr.com/a/s500x750/a.jpg",
                                         "width": 500, "height": 750}}],
        }
    )
    assert [block.text for block in post.content] == ["npf wins"]


# --------------------------------------------------------------------------- #
# trail
# --------------------------------------------------------------------------- #

def _trail_payload(count: int, *, with_reblogger: bool = False) -> dict:
    payload = {
        "type": "blocks",
        "id": "1",
        "content": [],
        "trail": [
            {
                "blogName": f"blog{i}",
                "blogTitle": f"Blog {i}",
                "blogUrl": f"https://blog{i}.tumblr.com/",
                "content": [{"type": "text", "text": f"step {i}"}],
            }
            for i in range(count)
        ],
    }
    if with_reblogger:
        payload["rebloggedFromName"] = "blog4"
    return payload


def test_trail_parsing_and_order():
    post = Post.from_api(_trail_payload(5))
    assert len(post.trail) == 5
    assert [item.blog_name for item in post.trail] == [f"blog{i}" for i in range(5)]
    assert post.trail[0].content[0].text == "step 0"
    assert post.is_reblog is True


def test_trail_for_display_collapses_from_the_root():
    post = Post.from_api(_trail_payload(5))
    items, hidden = post.trail_for_display(limit=3)
    assert hidden == 2
    assert [item.blog_name for item in items] == ["blog2", "blog3", "blog4"]

    items, hidden = post.trail_for_display(limit=5)
    assert (len(items), hidden) == (5, 0)

    items, hidden = post.trail_for_display(limit=10)
    assert (len(items), hidden) == (5, 0)


def test_trail_item_display_name_and_legacy_fallback():
    item = TrailItem.from_api(
        {
            "blogName": "name-only",
            "post": {"type": "quote", "text": "old quote", "source": "source"},
        }
    )
    assert item is not None
    assert item.display_name == "name-only"
    assert item.content and item.content[0].type == "text"
    assert item.content[0].subtype == "quote"


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #

def test_tags_from_tags_and_tags_v2_are_merged_and_trimmed():
    post = Post.from_api(
        {
            "type": "text",
            "body": "x",
            "tags": [" art ", "", "design"],
            "tagsV2": [{"name": "photography"}, {"name": "art"}, {}, "nope"],
        }
    )
    assert post.tags == ["art", "design", "photography"]


# --------------------------------------------------------------------------- #
# legacy post types
# --------------------------------------------------------------------------- #

def test_legacy_photo_conversion():
    post = Post.from_api(
        {
            "type": "photo",
            "photos": [
                {
                    "original_size": {
                        "url": "https://64.media.tumblr.com/abc/s1280x1920/photo.jpg",
                        "width": 1280,
                        "height": 1920,
                    }
                }
            ],
            "caption": "a caption",
        }
    )
    assert [block.type for block in post.content] == ["image", "text"]
    assert post.content[0].media[0].width == 1280
    assert post.content[1].text == "a caption"


def test_legacy_quote_conversion():
    post = Post.from_api({"type": "quote", "text": "stay hungry", "source": "someone"})
    assert post.content[0].subtype == "quote"
    assert post.content[0].text == "stay hungry"
    assert post.content[1].text == "— someone"


def test_legacy_link_conversion():
    post = Post.from_api(
        {"type": "link", "title": "Example", "link_url": "https://example.test", "description": "d"}
    )
    assert len(post.content) == 1
    block = post.content[0]
    assert block.type == "link"
    assert block.title == "Example"
    assert block.url == "https://example.test"
    assert block.description == "d"


def test_legacy_chat_conversion():
    post = Post.from_api(
        {
            "type": "chat",
            "chat": [{"label": "A", "phrase": "hi"}, {"phrase": "hello"}],
        }
    )
    assert post.content[0].subtype == "chat"
    assert post.content[0].text == "A: hi\nhello"


def test_legacy_audio_conversion():
    post = Post.from_api(
        {"type": "audio", "audio_url": "https://example.test/song.mp3", "caption": "listen"}
    )
    assert post.content[0].type == "audio"
    assert post.content[0].media[0].kind == "audio"
    assert post.content[1].text == "listen"


def test_legacy_answer_conversion():
    post = Post.from_api({"type": "answer", "question": "Q?", "answer": "A."})
    assert post.content[0].type == "answer"
    assert post.content[0].data["question"][0]["text"] == "Q?"
    assert post.content[1].text == "A."


def test_legacy_text_conversion():
    post = Post.from_api({"type": "text", "title": "Head", "body": "Body"})
    assert [(block.text, block.subtype) for block in post.content] == [
        ("Head", "heading2"),
        ("Body", None),
    ]


# --------------------------------------------------------------------------- #
# parse_posts shapes
# --------------------------------------------------------------------------- #

def test_parse_posts_from_blog_feed():
    posts = parse_posts(
        {
            "posts": [
                {"type": "text", "id": "1", "blogName": "a", "body": "one"},
                {"type": "text", "id": "2", "blogName": "b", "body": "two"},
            ]
        }
    )
    assert [post.id for post in posts] == ["1", "2"]


def test_parse_posts_from_wrapped_timeline_skips_non_posts():
    posts = parse_posts(
        {
            "timeline": {
                "elements": [
                    {"objectType": "post", "type": "text", "id": "1", "blogName": "a",
                     "content": [{"type": "text", "text": "one"}]},
                    {"objectType": "title", "title": "Recommended"},
                    {"objectType": "carousel", "id": "promo"},
                    {"type": "text", "id": "2", "blogName": "b",
                     "content": [{"type": "text", "text": "two"}]},
                ]
            }
        }
    )
    assert [post.id for post in posts] == ["1", "2"]


def test_parse_posts_rejects_unusable_payloads():
    assert parse_posts(None) == []
    assert parse_posts("nope") == []
    assert parse_posts({"posts": None}) == []
    assert parse_posts({"posts": "not-a-list"}) == []
    assert parse_posts({"timeline": []}) == []


def test_post_layout_is_flattened():
    post = Post.from_api(
        {
            "type": "blocks",
            "layout": [{"type": "rows"}, "Ask", {"type": "condensed"}],
        }
    )
    assert post.layout == ["rows", "Ask", "condensed"]


# --------------------------------------------------------------------------- #
# blog objects and post helpers
# --------------------------------------------------------------------------- #

def test_blog_from_api_chooses_largest_avatar():
    blog = Blog.from_api(
        {
            "name": "staff",
            "title": "Staff",
            "url": "https://staff.tumblr.com/",
            "followers": 12,
            "posts": 34,
            "avatar": [
                {"url": "https://example.test/64.png", "width": 64},
                {"url": "https://example.test/512.png", "width": 512},
            ],
        }
    )
    assert blog.display_name == "Staff"
    assert blog.handle == "staff.tumblr.com"
    assert blog.avatar.endswith("512.png")
    assert (blog.followers, blog.posts) == (12, 34)


def test_post_media_helpers_and_labels():
    post = Post.from_api(
        {
            "type": "blocks",
            "id": "1",
            "blogName": "a",
            "rebloggedFromName": "origin",
            "content": [
                {"type": "text", "text": "hello"},
                {
                    "type": "image",
                    "media": [{"url": "https://64.media.tumblr.com/a/s500x750/a.jpg",
                               "width": 500, "height": 750}],
                },
            ],
            "trail": [
                {"blogName": "origin", "content": [{"type": "text", "text": "root"}]}
            ],
        }
    )
    assert [block.type for block in post.media_blocks] == ["image"]
    assert post.first_image is not None
    assert post.first_image.width == 500
    assert post.is_reblog is True
    assert post.kind_label == "reblogged origin"
