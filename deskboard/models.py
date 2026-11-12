"""Tumblr data model.

Tumblr's internal API speaks two dialects:

* ``camelCase`` when the request sends ``Accept: application/json;format=camelcase``
  (what the website itself asks for), and
* the older ``snake_case`` otherwise.

Modern posts are delivered in NPF ("Neue Post Format") form: ``type == "blocks"``
with a ``content`` list of typed blocks.  Legacy post types (photo/text/quote/...)
still show up in older parts of a blog, so both shapes are normalised here.

Everything in this module is pure Python with no GTK imports so it can be unit
tested and reused by the CLI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_ACRONYM = re.compile(r"(?<=[a-z0-9])([A-Z])")


def snake_case(key: str) -> str:
    return _ACRONYM.sub(r"_\1", key).lower()


def get(mapping: Any, key: str, default: Any = None) -> Any:
    """Look up ``key`` accepting either camelCase or snake_case storage."""
    if not isinstance(mapping, dict):
        return default
    if key in mapping:
        return mapping[key]
    alt = snake_case(key)
    if alt in mapping:
        return mapping[alt]
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return bool(value)


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #

# Tumblr image URLs carry a size segment like ``/s1280x1920/`` or ``/s500x750u/``.
# We can request a bigger rendition of the very same asset by rewriting it.
_SIZE_SEGMENT = re.compile(r"/s(\d+)x(\d+)([a-z]?)/")


def upscale_image_url(url: str, width: int = 1280) -> str:
    """Ask Tumblr's CDN for a larger rendition of the same image."""
    if not url or "media.tumblr.com" not in url:
        return url
    match = _SIZE_SEGMENT.search(url)
    if not match:
        return url
    current = _int(match.group(1))
    if current >= width:
        return url
    return f"{url[: match.start()]}/s{width}x{int(width * 1.5)}{match.group(3)}/{url[match.end():]}"


@dataclass(slots=True)
class Media:
    """A single media object attached to a post block."""

    url: str
    width: int = 0
    height: int = 0
    mime: str = ""
    kind: str = "image"  # image | video | audio
    poster: str | None = None
    alt: str = ""

    @property
    def is_gif(self) -> bool:
        return self.mime.endswith("gif") or self.url.split("?")[0].lower().endswith(".gif")

    @property
    def aspect(self) -> float:
        if self.height:
            return self.width / self.height
        return 1.0

    @classmethod
    def from_api(cls, data: Any, kind: str = "image") -> "Media | None":
        if isinstance(data, str):
            return cls(url=data, kind=kind)
        if not isinstance(data, dict):
            return None
        url = get(data, "url") or get(data, "source") or ""
        if not url:
            return None
        return cls(
            url=url,
            width=_int(get(data, "width")),
            height=_int(get(data, "height")),
            mime=str(get(data, "type", "") or ""),
            kind=kind,
        )


def _best_media(items: Iterable[Any]) -> list[Media]:
    """Pick the largest rendition for each media key / url family."""
    if isinstance(items, dict):
        items = [items]
    chosen: dict[str, Media] = {}
    order: list[str] = []
    for raw in items or []:
        media = Media.from_api(raw)
        if not media:
            continue
        key = get(raw, "mediaKey") or (media.url.split("/s")[0] if "/s" in media.url else media.url)
        key = str(key)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = media
            order.append(key)
        elif media.width * media.height > existing.width * existing.height:
            chosen[key] = media
    return [chosen[k] for k in order]


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #

TEXT_SUBTYPES = {
    "heading1",
    "heading2",
    "quote",
    "indented",
    "chat",
    "ordered-list-item",
    "unordered-list-item",
    "ask",
}

# Block types that carry prose we can flatten to plain text.
_FLATTEN_TYPES = {"text", "quote", "chat", "link", "poll", "answer"}


@dataclass(slots=True)
class Formatting:
    """One inline formatting range inside a text block."""

    start: int
    end: int
    type: str  # bold | italic | strikethrough | link | mention | color
    url: str = ""
    blog: str = ""
    hex: str = ""


@dataclass(slots=True)
class Block:
    type: str = "text"
    data: dict = field(default_factory=dict)
    media: list[Media] = field(default_factory=list)
    formatting: list[Formatting] = field(default_factory=list)

    # -- text ------------------------------------------------------------- #
    @property
    def text(self) -> str:
        return str(self.data.get("text") or "")

    @property
    def subtype(self) -> str | None:
        sub = self.data.get("subtype")
        if not sub and self.type in TEXT_SUBTYPES:
            return self.type
        return sub

    @property
    def is_heading(self) -> bool:
        return self.subtype in ("heading1", "heading2")

    @property
    def indent_level(self) -> int:
        return _int(self.data.get("indent_level"), 1) or 1

    # -- media / embeds ---------------------------------------------------- #
    @property
    def url(self) -> str:
        return str(self.data.get("url") or self.data.get("embed_url") or "")

    @property
    def title(self) -> str:
        return str(self.data.get("title") or "")

    @property
    def description(self) -> str:
        return str(self.data.get("description") or "")

    @property
    def provider(self) -> str:
        return str(self.data.get("provider") or "")

    @property
    def poster_url(self) -> str:
        poster = self.data.get("poster")
        if isinstance(poster, list) and poster:
            return str(poster[-1].get("url") or "")
        if isinstance(poster, str):
            return poster
        return ""

    @property
    def is_video_block(self) -> bool:
        return self.type in ("video", "audio")

    # -- link blocks ------------------------------------------------------- #
    @property
    def display_url(self) -> str:
        return str(self.data.get("display_url") or self.data.get("displayUrl") or self.url)

    @property
    def site_name(self) -> str:
        return str(self.data.get("site_name") or self.data.get("siteName") or "")

    # -- audio blocks ------------------------------------------------------ #
    @property
    def artist(self) -> str:
        return str(self.data.get("artist") or "")

    @property
    def album(self) -> str:
        return str(self.data.get("album") or "")

    # -- poll blocks ------------------------------------------------------- #
    @property
    def question(self) -> str:
        return str(self.data.get("question") or "")

    @property
    def answers(self) -> list[tuple[str, int]]:
        """Poll answers as ``(text, vote_count)``; counts are 0 when hidden."""
        out: list[tuple[str, int]] = []
        for answer in self.data.get("answers") or []:
            if isinstance(answer, str):
                out.append((answer, 0))
            elif isinstance(answer, dict):
                text = answer.get("answer_text") or answer.get("answerText") or ""
                if text:
                    out.append((str(text), _int(answer.get("answer_count") or answer.get("answerCount"))))
        return out

    @property
    def poster_media(self) -> list[Media]:
        poster = self.data.get("poster")
        if isinstance(poster, dict):
            poster = [poster]
        return _best_media(poster or [])

    @classmethod
    def from_api(cls, data: Any) -> "Block | None":
        if isinstance(data, str):
            return cls(type="text", data={"text": data})
        if not isinstance(data, dict):
            return None
        block_type = str(data.get("type") or "text")
        block = cls(type=block_type, data=dict(data))

        # ``media`` is a list of renditions for images, but a single object for
        # video/audio blocks — normalise before selecting.
        raw_media = data.get("media")
        if isinstance(raw_media, dict):
            raw_media = [raw_media]
        if block_type == "image":
            block.media = _best_media(raw_media or [])
        elif block_type == "video":
            block.media = _best_media(raw_media or [])
            if not block.media:
                poster = cls._poster_media(data)
                if poster:
                    block.media = [poster]
        elif block_type == "audio":
            block.media = _best_media(raw_media or [])
            if not block.media and data.get("url"):
                block.media = [Media(url=str(data["url"]), kind="audio", mime="audio/mpeg")]

        for item in data.get("formatting") or []:
            if not isinstance(item, dict):
                continue
            block.formatting.append(
                Formatting(
                    start=_int(get(item, "start")),
                    end=_int(get(item, "end")),
                    type=str(get(item, "type") or ""),
                    url=str(get(item, "url") or ""),
                    blog=str(get(item, "blog") or ""),
                    hex=str(get(item, "hex") or ""),
                )
            )
        block.formatting.sort(key=lambda f: (f.start, -f.end))
        return block

    @staticmethod
    def _poster_media(data: dict) -> Media | None:
        poster = data.get("poster")
        if isinstance(poster, list) and poster:
            media = Media.from_api(poster[-1])
            if media:
                media.kind = "image"
            return media
        return None


def _blocks_from_api(items: Iterable[Any]) -> list[Block]:
    out: list[Block] = []
    for raw in items or []:
        block = Block.from_api(raw)
        if block:
            out.append(block)
    return out


# --------------------------------------------------------------------------- #
# Legacy post conversion
# --------------------------------------------------------------------------- #

def _text_block(text: str, subtype: str | None = None) -> Block:
    data: dict[str, Any] = {"type": "text", "text": text}
    if subtype:
        data["subtype"] = subtype
    return Block(type="text", data=data)


def _legacy_to_blocks(post: dict) -> list[Block]:
    """Best-effort conversion of a pre-NPF post payload into blocks."""
    blocks: list[Block] = []
    kind = str(post.get("type") or "")

    if kind == "photo":
        for photo in post.get("photos") or []:
            original = get(photo, "original_size") or {}
            media = Media.from_api(original)
            if media:
                block = Block(type="image", data={"type": "image"})
                block.media = [media]
                blocks.append(block)
        caption = str(post.get("caption") or "")
        if caption:
            blocks.append(_text_block(caption))

    elif kind == "quote":
        text = str(post.get("text") or "")
        if text:
            blocks.append(_text_block(text, "quote"))
        source = str(post.get("source") or "")
        if source:
            blocks.append(_text_block("— " + source))

    elif kind == "link":
        title = str(post.get("title") or "")
        url = str(get(post, "link_url") or get(post, "url") or "")
        if title or url:
            blocks.append(Block(type="link", data={"type": "link", "title": title, "url": url,
                                                   "description": str(post.get("description") or "")}))

    elif kind == "chat":
        lines = []
        for entry in post.get("chat") or []:
            label = str(get(entry, "label") or "")
            phrase = str(get(entry, "phrase") or "")
            lines.append(f"{label}: {phrase}" if label else phrase)
        if lines:
            blocks.append(_text_block("\n".join(lines), "chat"))

    elif kind == "audio":
        url = str(get(post, "audio_url") or "")
        if url:
            block = Block(type="audio", data={"type": "audio"})
            block.media = [Media(url=url, kind="audio", mime="audio/mpeg")]
            blocks.append(block)
        caption = str(post.get("caption") or "")
        if caption:
            blocks.append(_text_block(caption))

    elif kind == "video":
        player = post.get("player")
        if isinstance(player, list) and player:
            embed = get(player[0], "embed_code") or ""
            blocks.append(Block(type="video", data={"type": "video", "provider": "tumblr",
                                                    "url": str(post.get("permalink_url") or ""),
                                                    "embedHtml": embed}))
        caption = str(post.get("caption") or "")
        if caption:
            blocks.append(_text_block(caption))

    elif kind == "answer":
        question = str(post.get("question") or "")
        answer = str(post.get("answer") or "")
        blocks.append(Block(type="answer", data={"type": "answer",
                                                 "question": [{"type": "text", "text": question}],
                                                 "answer": answer}))
        # flatten for text rendering
        blocks.append(_text_block(answer))

    else:  # text and unknown
        title = str(post.get("title") or "")
        body = str(post.get("body") or post.get("text") or "")
        if title:
            blocks.append(_text_block(title, "heading2"))
        if body:
            blocks.append(_text_block(body))

    return blocks


# --------------------------------------------------------------------------- #
# Blog
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Blog:
    name: str = ""
    title: str = ""
    url: str = ""
    avatar: str = ""
    description: str = ""
    followers: int = 0
    posts: int = 0
    uuid: str = ""
    is_following: bool = False
    can_follow: bool = False
    ask: bool = False            # accepts asks
    ask_anon: bool = False       # allows anonymous asks
    ask_page_title: str = ""
    can_submit: bool = False     # accepts submissions
    can_message: bool = False

    @property
    def display_name(self) -> str:
        return self.title or self.name

    @property
    def handle(self) -> str:
        return f"{self.name}.tumblr.com" if self.name else ""

    @property
    def web_url(self) -> str:
        return self.url or (f"https://{self.name}.tumblr.com" if self.name else "")

    @classmethod
    def from_api(cls, data: Any) -> "Blog":
        if not isinstance(data, dict):
            return cls()
        avatars = data.get("avatar")
        avatar = ""
        if isinstance(avatars, list):
            # smallest sufficient rendition first; last entry is usually the largest
            best = [a for a in avatars if isinstance(a, dict) and a.get("url")]
            if best:
                avatar = str(max(best, key=lambda a: _int(a.get("width"))).get("url"))
        elif isinstance(avatars, str):
            avatar = avatars
        return cls(
            name=str(get(data, "name") or ""),
            title=str(get(data, "title") or ""),
            url=str(get(data, "url") or ""),
            avatar=avatar,
            description=str(get(data, "description") or ""),
            followers=_int(get(data, "followers")),
            posts=_int(get(data, "posts") or get(data, "totalPosts")),
            uuid=str(get(data, "uuid") or ""),
            is_following=_bool(get(data, "isFollowing", False)),
            can_follow=_bool(get(data, "canFollow", True), True),
            ask=_bool(get(data, "ask", False)),
            ask_anon=_bool(get(data, "askAnon", False)),
            ask_page_title=str(get(data, "askPageTitle") or ""),
            can_submit=_bool(get(data, "canSubmit", False)),
            can_message=_bool(get(data, "canMessage", False)),
        )


# --------------------------------------------------------------------------- #
# Trail
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class TrailItem:
    """One reblog step in a post's trail (oldest first in the API)."""

    blog_name: str = ""
    blog_title: str = ""
    blog_url: str = ""
    avatar: str = ""
    content: list[Block] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.blog_title or self.blog_name

    @classmethod
    def from_api(cls, data: Any) -> "TrailItem | None":
        if not isinstance(data, dict):
            return None
        blog = data.get("blog") or {}
        blocks = _blocks_from_api(data.get("content") or [])
        if not blocks and data.get("post"):
            # very old trail entries wrap a legacy post object
            blocks = _legacy_to_blocks(data.get("post") or {})
        return cls(
            blog_name=str(get(data, "blogName") or get(blog, "name") or ""),
            blog_title=str(get(data, "blogTitle") or get(blog, "title") or ""),
            blog_url=str(get(data, "blogUrl") or get(blog, "url") or ""),
            avatar=str(get(data, "avatar") or ""),
            content=blocks,
        )


# --------------------------------------------------------------------------- #
# Post
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Post:
    id: str = ""
    blog_name: str = ""
    blog_title: str = ""
    blog_url: str = ""
    blog_avatar: str = ""
    post_url: str = ""
    timestamp: int = 0
    tags: list[str] = field(default_factory=list)
    note_count: int = 0
    like_count: int = 0
    reblog_count: int = 0
    reply_count: int = 0
    reblog_key: str = ""
    can_like: bool = True
    can_reblog: bool = True
    can_reply: bool = True
    is_liked: bool = False
    is_nsfw: bool = False
    summary: str = ""
    post_type: str = "text"
    content: list[Block] = field(default_factory=list)
    trail: list[TrailItem] = field(default_factory=list)
    reblogged_from_name: str = ""
    reblogged_from_url: str = ""
    reblogged_root_name: str = ""
    served_id: str = ""
    layout: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    # -- derived ----------------------------------------------------------- #
    @property
    def display_name(self) -> str:
        return self.blog_title or self.blog_name

    @property
    def handle(self) -> str:
        return f"{self.blog_name}.tumblr.com"

    @property
    def is_reblog(self) -> bool:
        return bool(self.trail) or bool(self.reblogged_from_name)

    @property
    def kind_label(self) -> str:
        """Short human label used in the card header chip."""
        if not self.trail:
            return ""
        return f"reblogged {self.reblogged_from_name}" if self.reblogged_from_name else "reblogged"

    def trail_for_display(self, limit: int = 3) -> tuple[list[TrailItem], int]:
        """Return the trail to render plus the number of hidden earlier steps.

        Long reblog chains are collapsed from the *root* end, which is what every
        Tumblr client does: keep the most recent steps, note how many were cut.
        """
        if len(self.trail) <= limit:
            return list(self.trail), 0
        return list(self.trail[-limit:]), len(self.trail) - limit

    @property
    def media_blocks(self) -> list[Block]:
        return [b for b in self.content if b.type in ("image", "video", "audio")]

    @property
    def first_image(self) -> Media | None:
        for block in self.content:
            if block.media:
                return block.media[0]
        return None

    @classmethod
    def from_api(cls, data: Any) -> "Post":
        if not isinstance(data, dict):
            return cls()
        blog = data.get("blog") or {}
        blog_name = str(get(data, "blogName") or get(blog, "name") or "")
        post_type = str(get(data, "type") or "text")

        if post_type == "blocks":
            content = _blocks_from_api(data.get("content") or [])
        else:
            content = _blocks_from_api(data.get("content") or []) or _legacy_to_blocks(data)

        trail: list[TrailItem] = []
        for entry in data.get("trail") or []:
            item = TrailItem.from_api(entry)
            if item and item.content:
                trail.append(item)

        avatars = blog.get("avatar")
        avatar = ""
        if isinstance(avatars, list) and avatars:
            best = [a for a in avatars if isinstance(a, dict) and a.get("url")]
            if best:
                avatar = str(max(best, key=lambda a: _int(a.get("width"))).get("url"))

        tags: list[str] = []
        for tag in data.get("tags") or []:
            if isinstance(tag, str) and tag.strip():
                tags.append(tag.strip())
        for tag in data.get("tagsV2") or []:
            if isinstance(tag, dict):
                name = str(tag.get("name") or "").strip()
                if name and name not in tags:
                    tags.append(name)

        post = cls(
            id=str(get(data, "idString") or get(data, "id") or ""),
            blog_name=blog_name,
            blog_title=str(get(blog, "title") or ""),
            blog_url=str(get(blog, "url") or ""),
            blog_avatar=avatar,
            post_url=str(get(data, "postUrl") or ""),
            timestamp=_int(get(data, "timestamp")),
            tags=tags,
            note_count=_int(get(data, "noteCount")),
            like_count=_int(get(data, "likeCount")),
            reblog_count=_int(get(data, "reblogCount")),
            reply_count=_int(get(data, "replyCount")),
            reblog_key=str(get(data, "reblogKey") or ""),
            can_like=_bool(get(data, "canLike"), True),
            can_reblog=_bool(get(data, "canReblog"), True),
            can_reply=_bool(get(data, "canReply"), True),
            is_liked=_bool(get(data, "liked", False)),
            is_nsfw=_bool(get(data, "isNsfw", False)),
            summary=str(get(data, "summary") or ""),
            post_type=post_type,
            content=content,
            trail=trail,
            reblogged_from_name=str(get(data, "rebloggedFromName") or ""),
            reblogged_from_url=str(get(data, "rebloggedFromUrl") or ""),
            reblogged_root_name=str(get(data, "rebloggedRootName") or ""),
            served_id=str(get(data, "serveId") or ""),
            raw=data,
        )

        layout = []
        for item in data.get("layout") or []:
            if isinstance(item, dict):
                layout.append(str(item.get("type") or ""))
            elif isinstance(item, str):
                layout.append(item)
        post.layout = layout
        return post


def parse_posts(payload: Any) -> list[Post]:
    """Accept any of the shapes Tumblr uses for a page of posts.

    Blog feeds return ``response.posts``; the dashboard returns a "wrapped
    timeline" whose ``response.timeline.elements`` mixes posts with promo
    titles, carousels and ads — only the post elements are kept.
    """
    if not isinstance(payload, dict):
        return []
    candidates = (
        payload.get("posts")
        or payload.get("likedPosts")
        or payload.get("likes")
    )
    if candidates is None:
        timeline = payload.get("timeline")
        if isinstance(timeline, dict):
            candidates = (
                timeline.get("elements")
                or timeline.get("objects")
                or timeline.get("posts")
            )
        if candidates is None:
            candidates = payload.get("elements") or payload.get("objects")
    if not isinstance(candidates, list):
        return []
    posts: list[Post] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("objectType") not in (None, "post"):
            continue
        posts.append(Post.from_api(item))
    return posts
