"""Render NPF content blocks into GTK widgets.

Owner: cards.  The public entry point is :func:`render_blocks`; everything else
here is private.  Widget trees are deliberately flat and cheap because a
``Gtk.ListView`` creates one card per visible row and throws it away again.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gtk, Pango

from ..models import Block, Media
from .html import escape_markup, to_pango

# --------------------------------------------------------------------------- #
# Small widget helpers
# --------------------------------------------------------------------------- #


def _label(markup: str = "", *, wrap: bool = True, css: Iterable[str] = (),
           xalign: float = 0.0, selectable: bool = False) -> Gtk.Label:
    label = Gtk.Label()
    label.set_use_markup(True)
    label.set_wrap(wrap)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_lines(-1)
    label.set_xalign(xalign)
    label.set_halign(Gtk.Align.FILL)
    label.set_hexpand(True)
    if selectable:
        label.set_selectable(True)
    if markup:
        label.set_markup(markup)
    for name in css:
        label.add_css_class(name)
    return label


def _connect_links(label: Gtk.Label, ctx: Any) -> None:
    """Open inline <a href> links through the app context, not via GTK."""
    def activate(_label: Gtk.Label, uri: str) -> bool:
        try:
            ctx.open_url(uri)
        except Exception:  # noqa: BLE001 - a dead widget must not crash the UI
            pass
        return True

    try:
        label.connect("activate-link", activate)
    except Exception:  # noqa: BLE001
        pass


def _box(*, orientation: Gtk.Orientation = Gtk.Orientation.VERTICAL,
         spacing: int = 0) -> Gtk.Box:
    return Gtk.Box(orientation=orientation, spacing=spacing)


def _icon(name: str, size: int = 16) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(name)
    image.set_pixel_size(size)
    image.set_valign(Gtk.Align.START)
    return image


class VerticalRule(Gtk.Widget):
    """A theme-aware hairline used for quotes and the reblog trail.

    ``Gtk.Separator`` is drawn with the border colour and disappears on some
    themes; this paints a translucent current-colour bar instead.
    """

    __gtype_name__ = "DeskboardVerticalRule"

    def __init__(self, width: int = 2, alpha: float = 0.24) -> None:
        super().__init__()
        self._alpha = max(0.0, min(1.0, alpha))
        self.set_size_request(width, -1)
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.FILL)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot) -> None:  # noqa: N802 - GTK vfunc name
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return
        try:
            color = self.get_color()
        except Exception:  # noqa: BLE001 - very old GTK
            color = Gdk.RGBA()
            color.parse("#9a9a9a")
        color.alpha = color.alpha * self._alpha
        rect = Graphene.Rect()
        rect.init(0.0, 0.0, float(width), float(height))
        snapshot.append_color(color, rect)


def _ptr(widget: Gtk.Widget) -> None:
    try:
        widget.set_cursor_from_name("pointer")
    except Exception:  # noqa: BLE001 - cursor themes can be unavailable
        pass


def _make_blocks(value: Any) -> list[Block]:
    """Accept a block, a list of raw blocks/dicts, or a plain string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [Block.from_api({"type": "text", "text": value})] if value.strip() else []
    if isinstance(value, dict):
        block = Block.from_api(value)
        return [block] if block else []
    if isinstance(value, (list, tuple)):
        out: list[Block] = []
        for item in value:
            out.extend(_make_blocks(item))
        return out
    return []


def _media_from_data(raw: Any, kind: str) -> list[Media]:
    """Build media from a video/audio payload (``data['media']`` is a dict)."""
    items: list[Any]
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    media: list[Media] = []
    for item in items:
        parsed = Media.from_api(item, kind=kind)
        if parsed and parsed.url:
            parsed.kind = kind
            media.append(parsed)
    return media


# --------------------------------------------------------------------------- #
# Block types
# --------------------------------------------------------------------------- #

def _render_text(container: Gtk.Box, block: Block, ctx: Any, post: Any,
                 ordered_index: int | None = None) -> bool:
    text = block.text
    if not text.strip():
        return False

    subtype = (block.subtype or "").lower()
    markup = to_pango(text, block.formatting)

    if subtype == "heading1":
        label = _label(f'<span size="xx-large" weight="bold">{markup}</span>',
                       selectable=True)
        _connect_links(label, ctx)
        label.set_margin_top(10)
        label.set_margin_bottom(4)
        container.append(label)
        return True

    if subtype == "heading2":
        label = _label(f'<span size="x-large" weight="bold">{markup}</span>',
                       selectable=True)
        _connect_links(label, ctx)
        label.set_margin_top(8)
        label.set_margin_bottom(3)
        container.append(label)
        return True

    if subtype == "quote":
        rule = VerticalRule(3, 0.30)
        rule.set_margin_end(12)
        row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.add_css_class("post-quote")
        row.append(rule)
        label = _label(f"<i>{markup}</i>", selectable=True)
        _connect_links(label, ctx)
        row.append(label)
        row.set_margin_top(6)
        row.set_margin_bottom(10)
        row.set_margin_start(2)
        container.append(row)
        return True

    if subtype in ("unordered-list-item", "ordered-list-item"):
        level = max(1, getattr(block, "indent_level", 1) or 1)
        if subtype == "ordered-list-item":
            prefix = f"{ordered_index or 1}. "
        else:
            prefix = "• "
        label = _label(prefix + markup, selectable=True)
        _connect_links(label, ctx)
        label.set_margin_start(6 + 18 * (level - 1))
        label.set_margin_bottom(3)
        container.append(label)
        return True

    if subtype == "chat":
        _render_chat(container, text, ctx)
        return True

    if subtype == "indented":
        level = max(1, getattr(block, "indent_level", 1) or 1)
        label = _label(markup, selectable=True)
        _connect_links(label, ctx)
        label.set_margin_start(24 * level)
        label.set_margin_bottom(8)
        container.append(label)
        return True

    label = _label(markup, selectable=True)
    _connect_links(label, ctx)
    label.set_margin_bottom(8)
    container.append(label)
    return True


def _render_chat(container: Gtk.Box, text: str, ctx: Any = None) -> None:
    for line in text.splitlines():
        if not line.strip():
            continue
        name, separator, message = line.partition(":")
        row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_margin_start(4)
        row.set_margin_bottom(2)
        if separator and name.strip() and len(name) < 80:
            speaker = _label(f"<b>{escape_markup(name.strip())}:</b>", wrap=False)
            speaker.set_hexpand(False)
            speaker.set_valign(Gtk.Align.START)
            speaker.add_css_class("heading")
            row.append(speaker)
            row.set_spacing(8)
            body = _label(to_pango(message.lstrip(), None), selectable=True)
        else:
            body = _label(to_pango(line, None), selectable=True)
        if ctx is not None:
            _connect_links(body, ctx)
        body.set_valign(Gtk.Align.START)
        row.append(body)
        container.append(row)


def _thumbnail(ctx: Any, url: str, size: int = 64) -> Gtk.Picture:
    picture = Gtk.Picture()
    picture.set_size_request(size, size)
    picture.set_content_fit(Gtk.ContentFit.COVER)
    picture.set_valign(Gtk.Align.START)
    picture.set_can_shrink(True)
    picture.add_css_class("card")

    def apply(texture: Any) -> None:
        try:
            if texture is not None:
                picture.set_paintable(texture)
        except Exception:  # noqa: BLE001 - card may have been recycled
            pass

    try:
        ctx.images.load(url, size * 2, size * 2, apply)
    except Exception:  # noqa: BLE001 - images are optional
        pass
    return picture


def _render_link(container: Gtk.Box, block: Block, ctx: Any) -> bool:
    url = block.url
    display_url = block.display_url or url
    title = block.title.strip()
    description = block.description.strip()
    if not (url or title or description):
        return False

    domain = ""
    if display_url:
        try:
            parsed = urlparse(display_url if "//" in display_url else "https://" + display_url)
            domain = (parsed.netloc or "").lower()
        except ValueError:
            domain = ""
    if domain.startswith("www."):
        domain = domain[4:]

    body = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    body.set_margin_top(10)
    body.set_margin_bottom(10)
    body.set_margin_start(12)
    body.set_margin_end(12)

    poster = ""
    poster_media = getattr(block, "poster_media", None) or []
    if poster_media:
        poster = poster_media[0].url
    if poster:
        body.append(_thumbnail(ctx, poster, 64))
    else:
        body.append(_icon("web-browser-symbolic", 24))

    text = _box(spacing=2)
    text.set_hexpand(True)
    headline = _label(f"<b>{escape_markup(title or domain or display_url)}</b>",
                      css=("body",), selectable=False)
    headline.set_ellipsize(Pango.EllipsizeMode.END)
    headline.set_lines(2)
    text.append(headline)
    source = block.site_name.strip()
    caption_parts: list[str] = []
    for part in (source, domain):
        if part and part.lower() not in {seen.lower() for seen in caption_parts}:
            caption_parts.append(part)
    caption_text = " · ".join(caption_parts)
    if caption_text:
        caption = _label(escape_markup(caption_text), css=("caption", "dim-label"))
        caption.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(caption)
    if description:
        description_label = _label(escape_markup(description), css=("caption", "dim-label"))
        description_label.set_lines(3)
        description_label.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(description_label)
    body.append(text)
    if url:
        body.append(_icon("adw-external-link-symbolic", 14))

    card: Gtk.Widget
    if url:
        card = Gtk.Button()
        card.add_css_class("card")
        card.set_child(body)
        card.set_tooltip_text(display_url)
        _ptr(card)
        card.connect("clicked", lambda _button: _open(ctx, url))
    else:
        card = Gtk.Box()
        card.add_css_class("card")
        card.append(body)
    card.set_margin_top(2)
    card.set_margin_bottom(10)
    container.append(card)
    return True


def _render_poll(container: Gtk.Box, block: Block) -> bool:
    data = block.data if isinstance(block.data, dict) else {}
    question = block.question.strip()
    answers = block.answers
    if not question and not answers:
        return False

    card = _box(spacing=0)
    card.add_css_class("card")
    card.set_margin_top(2)
    card.set_margin_bottom(10)

    header = _box(spacing=2)
    header.set_margin_top(12)
    header.set_margin_start(14)
    header.set_margin_end(14)
    if question:
        header.append(_label(escape_markup(question), css=("heading",)))
    header.append(_label("Poll", css=("caption", "dim-label")))
    card.append(header)

    body = _box(spacing=10)
    body.set_margin_top(10)
    body.set_margin_bottom(12)
    body.set_margin_start(14)
    body.set_margin_end(14)

    raw_answers = data.get("answers") if isinstance(data.get("answers"), list) else []
    total_votes = sum(int(votes) for _text, votes in answers)
    for index, (answer_text, votes) in enumerate(answers):
        if not answer_text.strip():
            continue
        raw = raw_answers[index] if index < len(raw_answers) else None
        percent = None
        if isinstance(raw, dict):
            for key in ("percentage", "percent", "percentageOfVotes"):
                if isinstance(raw.get(key), (int, float)):
                    percent = float(raw[key])
                    break
        if percent is None and total_votes > 0:
            percent = votes * 100.0 / total_votes
        fraction = max(0.0, min(1.0, (percent or 0.0) / 100.0))

        row = _box(spacing=4)
        label_row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label_row.append(_label(escape_markup(answer_text), selectable=True))
        detail = ""
        if percent is not None:
            detail = f"{percent:.0f}%"
        if votes:
            detail = f"{detail} · {votes}" if detail else str(votes)
        if detail:
            count = _label(escape_markup(detail), wrap=False, css=("caption", "dim-label"))
            count.set_hexpand(False)
            label_row.append(count)
        row.append(label_row)

        bar = Gtk.ProgressBar()
        bar.set_fraction(fraction)
        bar.set_show_text(False)
        bar.set_size_request(-1, 6)
        row.append(bar)
        body.append(row)
    card.append(body)

    if total_votes:
        footer = _label(f"{total_votes} votes", css=("caption", "dim-label"))
        footer.set_margin_start(14)
        footer.set_margin_end(14)
        footer.set_margin_bottom(10)
        card.append(footer)
    container.append(card)
    return True


def _render_answer(container: Gtk.Box, block: Block, ctx: Any, post: Any,
                   depth: int) -> bool:
    data = block.data if isinstance(block.data, dict) else {}
    question = _make_blocks(data.get("question") or data.get("questionBlocks"))
    answer = _make_blocks(data.get("answer") or data.get("answerBlocks"))
    if not question and not answer:
        return False

    if question:
        box = _box(spacing=4)
        box.add_css_class("card")
        box.set_margin_top(2)
        box.set_margin_bottom(8)
        inner = _box(spacing=2)
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        asker = str(data.get("askingName") or data.get("asking_name") or "").strip()
        prompt = f"{asker} asked" if asker else "Anonymous asked"
        inner.append(_label(escape_markup(prompt), css=("caption", "dim-label")))
        box.append(inner)
        _render(box, question, ctx, post, depth + 1)
        container.append(box)

    if answer:
        _render(container, answer, ctx, post, depth + 1)
    return True


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #

_REVEALED: set[str] = set()
_GATES: dict[str, list[Callable[[], None]]] = {}


def _post_key(post: Any) -> str:
    for attr in ("id", "served_id"):
        value = getattr(post, attr, "") or ""
        if value:
            return str(value)
    return str(id(post))


def _nsfw_gate(factory: Callable[[], Gtk.Widget], post: Any, ctx: Any) -> Gtk.Widget:
    key = _post_key(post)
    if key in _REVEALED:
        return factory()

    holder = _box(spacing=0)
    holder.add_css_class("card")
    holder.add_css_class("nsfw-cover")
    holder.set_margin_top(4)
    holder.set_margin_bottom(10)

    button = Gtk.Button()
    button.add_css_class("flat")
    content = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    content.set_margin_top(14)
    content.set_margin_bottom(14)
    content.set_margin_start(14)
    content.set_margin_end(14)
    content.append(_icon("view-conceal-symbolic", 24))
    message = _box(spacing=2)
    message.set_hexpand(True)
    message.append(_label("Sensitive content — click to view", css=("heading",)))
    message.append(_label("This post is marked as mature.", css=("caption", "dim-label")))
    content.append(message)
    button.set_child(content)
    _ptr(button)
    holder.append(button)

    done = False

    def reveal(_button=None) -> None:
        nonlocal done
        if done:
            return
        done = True
        try:
            widget = factory()
            widget.set_margin_top(2)
            widget.set_margin_bottom(8)
            holder.remove(button)
            holder.append(widget)
        except Exception:  # noqa: BLE001 - card may have been recycled
            return
        _REVEALED.add(key)
        for callback in _GATES.pop(key, []):
            if callback is not reveal:
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass

    _GATES.setdefault(key, []).append(reveal)
    button.connect("clicked", reveal)
    return holder


def _image_grid(medias: list[Media], block: Block, ctx: Any, post: Any) -> Gtk.Widget:
    from .mediawidgets import ImageGrid

    colors = block.data.get("colors") if isinstance(block.data, dict) else None
    try:
        return ImageGrid(medias, ctx, colors=colors, post=post)
    except TypeError:
        # The media module may still expose the two-argument signature.
        return ImageGrid(medias, ctx)


def _video_block(block: Block, ctx: Any, post: Any) -> Gtk.Widget:
    from .mediawidgets import VideoBlock

    try:
        return VideoBlock(block, ctx, post=post)
    except TypeError:
        return VideoBlock(block, ctx)


def _audio_block(block: Block, ctx: Any, post: Any) -> Gtk.Widget:
    from .mediawidgets import AudioBlock

    try:
        return AudioBlock(block, ctx, post=post)
    except TypeError:
        return AudioBlock(block, ctx)


def _render_media_run(container: Gtk.Box, block: Block, medias: list[Media],
                      ctx: Any, post: Any, kind: str) -> None:
    if not medias:
        return
    if kind == "image":
        factory = lambda: _image_grid(medias, block, ctx, post)  # noqa: E731
    elif kind == "video":
        factory = lambda: _video_block(block, ctx, post)  # noqa: E731
    else:
        factory = lambda: _audio_block(block, ctx, post)  # noqa: E731

    nsfw = bool(getattr(post, "is_nsfw", False)) and _pref_on(ctx, "nsfw_blur", True)
    if nsfw:
        widget = _nsfw_gate(factory, post, ctx)
    else:
        widget = factory()
        widget.set_margin_top(4)
        widget.set_margin_bottom(10)
    container.append(widget)


def _pref_on(ctx: Any, key: str, default: bool = True) -> bool:
    try:
        return bool(ctx.prefs.get(key))
    except Exception:  # noqa: BLE001 - prefs are optional in tests
        return default


def _open(ctx: Any, url: str) -> None:
    try:
        ctx.open_url(url)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def render_blocks(container: Gtk.Box, blocks: Iterable[Block], ctx: Any,
                  post: Any = None) -> None:
    """Append rendered widgets for ``blocks`` to ``container``."""
    _render(container, blocks, ctx, post, 0)


def _render(container: Gtk.Box, blocks: Iterable[Block], ctx: Any, post: Any,
            depth: int) -> None:
    if depth > 3:
        return
    items = list(blocks or [])
    index = 0
    ordered_counts: dict[int, int] = {}
    while index < len(items):
        block = items[index]
        try:
            block_type = str(getattr(block, "type", "") or "")
        except Exception:  # noqa: BLE001
            index += 1
            continue

        if block_type == "image" and getattr(block, "media", None):
            # Consecutive image blocks are one photo set: one grid looks far
            # better than three stacked full-width pictures.
            medias: list[Media] = []
            seen: set[str] = set()
            colors_block = block
            while (index < len(items) and str(getattr(items[index], "type", "")) == "image"
                   and getattr(items[index], "media", None)):
                for media in items[index].media:
                    if media and media.url not in seen:
                        seen.add(media.url)
                        medias.append(media)
                if not (getattr(colors_block, "data", {}) or {}).get("colors"):
                    colors_block = items[index]
                index += 1
            _render_media_run(container, colors_block, medias, ctx, post, "image")
            ordered_counts.clear()
            continue

        if block_type == "video":
            data = getattr(block, "data", {}) or {}
            medias = _media_from_data(data.get("media"), "video")
            if medias:
                block = replace(block, media=medias)
            _render_media_run(container, block, medias or list(getattr(block, "media", []) or []),
                              ctx, post, "video")
            ordered_counts.clear()
            index += 1
            continue

        if block_type == "audio":
            data = getattr(block, "data", {}) or {}
            medias = _media_from_data(data.get("media") or data.get("url"), "audio")
            if medias:
                block = replace(block, media=medias)
            _render_media_run(container, block, medias or list(getattr(block, "media", []) or []),
                              ctx, post, "audio")
            ordered_counts.clear()
            index += 1
            continue

        if block_type == "link":
            if _render_link(container, block, ctx):
                ordered_counts.clear()
                index += 1
                continue

        if block_type == "poll":
            if _render_poll(container, block):
                ordered_counts.clear()
                index += 1
                continue

        if block_type == "answer":
            if _render_answer(container, block, ctx, post, depth):
                # Legacy payloads repeat the answer as the next text block.
                data = getattr(block, "data", {}) or {}
                answer = data.get("answer")
                if isinstance(answer, str) and answer.strip():
                    while index + 1 < len(items):
                        nxt = items[index + 1]
                        if (str(getattr(nxt, "type", "")) == "text"
                                and getattr(nxt, "text", "").strip() == answer.strip()):
                            index += 1
                        else:
                            break
                ordered_counts.clear()
                index += 1
                continue

        # Text blocks — including legacy ``quote``/``chat`` types stored as text.
        subtype = str(getattr(block, "subtype", "") or "").lower()
        if subtype == "ordered-list-item":
            level = max(1, getattr(block, "indent_level", 1) or 1)
            ordered_counts[level] = ordered_counts.get(level, 0) + 1
            _render_text(container, block, ctx, post, ordered_counts[level])
        else:
            if subtype not in ("unordered-list-item",):
                ordered_counts.clear()
            if not _render_text(container, block, ctx, post):
                # Unknown block carrying only media data (defensive fallback).
                data = getattr(block, "data", {}) or {}
                for kind in ("image", "video", "audio"):
                    medias = _media_from_data(data.get("media"), kind)
                    if medias:
                        _render_media_run(container, block, medias, ctx, post, kind)
                        break
        index += 1
