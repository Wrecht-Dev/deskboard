"""The thing you actually look at: one post card.

Cards are built by a recycled ``Gtk.ListView``; ``__init__`` does no I/O and
every async callback is guarded so a texture that arrives after the row was
recycled can never raise.
"""
from __future__ import annotations

import math
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, GLib, Gtk, Pango

from ..util import humanize_time, short_number
from .blocks import render_blocks
from .html import escape_markup, plain_text

_REBLOG_ICON = "media-playlist-repeat-symbolic"
_LIKE_ICON = "emote-love-symbolic"
_SHARE_ICON = "edit-copy-symbolic"


# --------------------------------------------------------------------------- #
# height estimation for bound-but-not-yet-mapped rows
# --------------------------------------------------------------------------- #

_HEIGHT_HINTS: dict[str, int] = {}
_MAX_HEIGHT_HINTS = 240
_ESTIMATE_WIDTH = 700.0


def _post_key(post: Any) -> str:
    value = getattr(post, "id", "") or ""
    return str(value) if value else str(id(post))


def _remember_height(post: Any, height: int) -> None:
    if height <= 0:
        return
    key = _post_key(post)
    _HEIGHT_HINTS.pop(key, None)
    _HEIGHT_HINTS[key] = int(height)
    while len(_HEIGHT_HINTS) > _MAX_HEIGHT_HINTS:
        _HEIGHT_HINTS.pop(next(iter(_HEIGHT_HINTS)))


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _estimate_text_height(block: Any) -> float:
    text = getattr(block, "text", "") or ""
    if not text.strip():
        return 0.0
    chars_per_line = 62.0
    lines = max(1, int(math.ceil(len(text) / chars_per_line)))
    line_height = 22.0
    subtype = str(getattr(block, "subtype", "") or "")
    extra = 0.0
    if subtype == "heading1":
        line_height = 34.0
        extra = 6.0
    elif subtype == "heading2":
        line_height = 28.0
        extra = 4.0
    elif subtype == "quote":
        extra = 6.0
    return lines * line_height + extra + 8.0


def _estimate_media_height(block: Any) -> float:
    medias = list(getattr(block, "media", None) or [])
    kind = str(getattr(block, "type", "") or "")
    if kind == "audio":
        return 112.0
    if kind == "video":
        return 390.0
    if not medias:
        return 240.0 if kind == "image" else 0.0
    aspect = 1.0
    for media in medias:
        try:
            if media.width > 0 and media.height > 0:
                aspect = media.width / media.height
                break
        except Exception:  # noqa: BLE001
            pass
    aspect = _clampf(aspect, 0.2, 6.0)
    count = len(medias)
    if count <= 1:
        return _clampf(_ESTIMATE_WIDTH / aspect, 120.0, 640.0)
    if count == 2:
        return _clampf((_ESTIMATE_WIDTH - 6.0) / 2.0 / aspect, 100.0, 560.0)
    if count == 3:
        return _clampf(_ESTIMATE_WIDTH / 2.0 / aspect + 6.0, 160.0, 760.0)
    return _clampf((_ESTIMATE_WIDTH - 6.0) / 2.0 / aspect + 6.0, 160.0, 620.0)


def _estimate_block_height(block: Any) -> float:
    kind = str(getattr(block, "type", "") or "text")
    if kind == "text":
        return _estimate_text_height(block)
    if kind in ("image", "video", "audio"):
        return _estimate_media_height(block) + 14.0
    if kind == "link":
        return 104.0
    if kind == "poll":
        answers = len(getattr(block, "answers", None) or [])
        return 74.0 + answers * 42.0
    if kind == "answer":
        return 168.0
    return _estimate_text_height(block)


def _estimate_blocks_height(blocks: Any) -> float:
    total = 0.0
    for block in blocks or []:
        try:
            total += _estimate_block_height(block)
        except Exception:  # noqa: BLE001
            total += 30.0
    return total


def _estimate_card_height(post: Any) -> int:
    key = _post_key(post)
    hint = _HEIGHT_HINTS.get(key)
    if hint:
        return hint
    height = 116.0  # header + card padding/margins + footer base
    trail_for_display = getattr(post, "trail_for_display", None)
    if callable(trail_for_display):
        try:
            trail, hidden = trail_for_display(3)
        except Exception:  # noqa: BLE001
            trail, hidden = list(getattr(post, "trail", None) or []), 0
    else:
        trail, hidden = list(getattr(post, "trail", None) or []), 0
    if hidden:
        height += 24.0
    for item in trail:
        height += 26.0 + _estimate_blocks_height(getattr(item, "content", None) or [])
    height += _estimate_blocks_height(getattr(post, "content", None) or [])
    if getattr(post, "tags", None):
        height += 26.0
    height += 20.0  # separator + footer spacing
    # Estimates run slightly high in practice; bias down so a row that builds
    # on scroll shrinks rather than jumps open.
    return int(_clampf(height * 0.87, 120.0, 2400.0))


# --------------------------------------------------------------------------- #
# tiny builders
# --------------------------------------------------------------------------- #

def _label(markup: str = "", *, css: tuple[str, ...] = (), wrap: bool = False,
           selectable: bool = False) -> Gtk.Label:
    label = Gtk.Label()
    label.set_use_markup(True)
    label.set_wrap(wrap)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_xalign(0.0)
    if selectable:
        label.set_selectable(True)
    if markup:
        label.set_markup(markup)
    for name in css:
        label.add_css_class(name)
    return label


def _box(*, orientation: Gtk.Orientation = Gtk.Orientation.VERTICAL,
         spacing: int = 0) -> Gtk.Box:
    return Gtk.Box(orientation=orientation, spacing=spacing)


def _dim(label: Gtk.Label) -> None:
    label.add_css_class("caption")
    label.add_css_class("dim-label")


# --------------------------------------------------------------------------- #
# PostCard
# --------------------------------------------------------------------------- #

class PostCard(Gtk.Box):
    """A calm, information-dense rendering of a single Tumblr post."""

    def __init__(self, post: Any, ctx: Any) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.post = post
        self.ctx = ctx
        self.add_css_class("post-card")
        self.set_hexpand(True)

        # Avatars are queued here and fetched only once this card is mapped.
        # A Gtk.ListView binds far more rows than it maps, so eager fetching
        # here would queue hundreds of images for rows nobody ever sees.
        self._deferred_images: dict[tuple[str, int, int], list[Any]] = {}
        self._requested_images: set[tuple[str, int]] = set()
        self._menu_popover: Gtk.Popover | None = None
        self._built = False
        self._build_scheduled = False
        self._inner: Gtk.Box | None = None
        self._skeleton: Gtk.Box | None = _box(spacing=0)
        self._skeleton.set_hexpand(True)
        self._skeleton.set_size_request(-1, _estimate_card_height(post))
        self.append(self._skeleton)
        self.connect("map", self._on_map)

    # -- lazy construction -------------------------------------------------- #
    def _on_map(self, *_args) -> None:
        if self._built:
            self._pump_images()
            return
        if self._build_scheduled:
            return
        # Gtk.ListView maps a row briefly while binding it even when the row is
        # outside the viewport.  Wait one idle turn and build only if the row is
        # still mapped; otherwise the skeleton stays until the user scrolls to
        # it and it is mapped for real.
        self._build_scheduled = True
        GLib.idle_add(self._build_if_visible)

    def _build_if_visible(self) -> bool:
        self._build_scheduled = False
        if self._built:
            return False
        try:
            if not self.get_mapped():
                return False
        except Exception:  # noqa: BLE001
            pass
        self._built = True
        self._build_now()
        self._pump_images()
        return False

    def _build_now(self) -> None:
        """Build the real card once the row is mapped."""
        try:
            if self._skeleton is not None:
                self.remove(self._skeleton)
                self._skeleton = None
        except Exception:  # noqa: BLE001
            pass
        inner = _box(spacing=0)
        inner.set_hexpand(True)
        self._inner = inner
        self.append(inner)
        try:
            self._build_header(inner)
            self._build_content(inner)
            self._build_tags(inner)
            self._build_footer(inner)
        except Exception:  # noqa: BLE001 - never let one post break the feed
            try:
                self.remove(inner)
            except Exception:  # noqa: BLE001
                pass
            fallback = Gtk.Label(label="This post could not be displayed.")
            fallback.add_css_class("dim-label")
            fallback.set_margin_top(12)
            fallback.set_margin_bottom(12)
            fallback.set_margin_start(16)
            fallback.set_margin_end(16)
            self.append(fallback)
            self._inner = None
            return
        GLib.idle_add(self._remember_own_height)

    def _remember_own_height(self) -> bool:
        try:
            _remember_height(self.post, self.get_height())
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- header ------------------------------------------------------------- #
    def _build_header(self, parent: Gtk.Box) -> None:
        post = self.post
        blog_name = (getattr(post, "blog_name", "") or "").strip()
        display_name = (getattr(post, "display_name", "") or blog_name or "Untitled").strip()
        header = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_bottom(6)

        author = Gtk.Button()
        author.add_css_class("flat")
        author.add_css_class("post-author")
        author.set_halign(Gtk.Align.FILL)
        author.set_hexpand(True)
        author.connect("clicked", self._on_open_blog)

        row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        avatar = Adw.Avatar(size=40, text=display_name or "?", show_initials=True)
        avatar.set_valign(Gtk.Align.START)
        self._defer_avatar(avatar, getattr(post, "blog_avatar", "") or "", 40)
        row.append(avatar)

        text = _box(spacing=1)
        text.set_hexpand(True)
        name_row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        name = _label(escape_markup(display_name), css=("heading",))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(32)
        name.set_hexpand(False)
        name_row.set_halign(Gtk.Align.START)
        name_row.append(name)

        if getattr(post, "is_reblog", False):
            from_name = (getattr(post, "reblogged_from_name", "") or "").strip()
            if not from_name:
                trail = getattr(post, "trail", None) or []
                if trail:
                    from_name = (getattr(trail[-1], "blog_name", "") or "").strip()
            if from_name:
                handle_name = from_name if from_name.startswith("@") else "@" + from_name
                chip_text = f"reblogged from {handle_name}"
            else:
                chip_text = "reblogged"
            chip = _label(escape_markup(chip_text), css=("caption", "pill", "dim-label"))
            chip.set_ellipsize(Pango.EllipsizeMode.END)
            chip.set_max_width_chars(34)
            chip.set_valign(Gtk.Align.CENTER)
            name_row.append(chip)
        text.append(name_row)

        meta_parts = []
        handle = (getattr(post, "handle", "") or
                  (f"{blog_name}.tumblr.com" if blog_name else ""))
        if handle:
            meta_parts.append(escape_markup(handle))
        when = humanize_time(getattr(post, "timestamp", 0))
        if when:
            meta_parts.append(escape_markup(when))
        if getattr(post, "is_nsfw", False):
            meta_parts.append('<span weight="bold">NSFW</span>')
        if meta_parts:
            meta = _label("  ·  ".join(meta_parts))
            _dim(meta)
            meta.set_ellipsize(Pango.EllipsizeMode.END)
            text.append(meta)

        row.append(text)
        author.set_child(row)
        header.append(author)

        menu = Gtk.Button()
        menu.set_icon_name("view-more-symbolic")
        menu.add_css_class("flat")
        menu.set_tooltip_text("More actions")
        menu.set_valign(Gtk.Align.START)
        menu.connect("clicked", self._show_menu)
        header.append(menu)

        parent.append(header)

    # -- deferred images ---------------------------------------------------- #
    def _defer_avatar(self, avatar: Adw.Avatar, url: str, size: int) -> None:
        if not url:
            return
        width = height = size * 2

        def apply(texture: Any) -> None:
            try:
                if texture is not None:
                    avatar.set_custom_image(texture)
            except Exception:  # noqa: BLE001 - the row may be gone already
                pass

        self._deferred_images.setdefault((url, width, height), []).append(apply)

    def _pump_images(self) -> None:
        if not self._deferred_images:
            return
        for (url, width, height), callbacks in list(self._deferred_images.items()):
            try:
                texture = self.ctx.images.cached(url, width)
            except Exception:  # noqa: BLE001
                texture = None
            if texture is not None:
                for callback in callbacks:
                    callback(texture)
                continue
            if (url, width) in self._requested_images:
                continue
            self._requested_images.add((url, width))

            def deliver(texture: Any, callbacks=callbacks) -> None:
                if texture is None:
                    return
                for callback in callbacks:
                    callback(texture)

            try:
                self.ctx.images.load(url, width, height, deliver)
            except Exception:  # noqa: BLE001 - avatars are optional
                pass

    def _show_menu(self, button: Gtk.Button) -> None:
        popover = self._menu_popover
        if popover is None:
            popover = self._build_menu()
            self._menu_popover = popover
            try:
                popover.set_parent(button)
            except Exception:  # noqa: BLE001
                return
        try:
            popover.popup()
        except Exception:  # noqa: BLE001
            pass

    def _build_menu(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = _box(spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        def add(icon: str, text: str, callback) -> None:
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            content = Adw.ButtonContent(icon_name=icon, label=text)
            content.set_halign(Gtk.Align.START)
            button.set_child(content)

            def activate(_button: Gtk.Button) -> None:
                popover.popdown()
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass

            button.connect("clicked", activate)
            box.append(button)

        add("web-browser-symbolic", "Open post", self._open_post)
        add("user-home-symbolic", "Open blog", self._open_blog)
        add("edit-copy-symbolic", "Copy link", self._copy_link)
        add("edit-copy-symbolic", "Copy text", self._copy_text)
        popover.set_child(box)
        return popover

    # -- trail + content ---------------------------------------------------- #
    def _build_content(self, parent: Gtk.Box) -> None:
        post = self.post
        trail_for_display = getattr(post, "trail_for_display", None)
        if callable(trail_for_display):
            trail, hidden = trail_for_display(3)
        else:
            trail, hidden = list(getattr(post, "trail", None) or []), 0
        content = list(getattr(post, "content", None) or [])

        if trail:
            trail_box = _box(spacing=0)
            trail_box.add_css_class("post-trail")
            if hidden:
                earlier = _label(f"{hidden} earlier reblog{'s' if hidden != 1 else ''}",
                                css=("caption", "dim-label"))
                earlier.set_margin_start(10)
                earlier.set_margin_bottom(4)
                trail_box.append(earlier)
            for item in trail:
                trail_box.append(self._build_trail_step(item))
            trail_box.set_margin_bottom(4 if not content else 0)
            parent.append(trail_box)

        body = _box(spacing=0)
        body.add_css_class("post-body")
        if content:
            render_blocks(body, content, self.ctx, post)
        elif not trail:
            # Extremely rare: no blocks at all. Use the summary rather than
            # leaving an empty card that looks like a rendering bug.
            summary = (getattr(post, "summary", "") or "").strip()
            if summary:
                label = _label(escape_markup(summary), wrap=True, selectable=True)
                label.add_css_class("body")
                label.set_margin_bottom(8)
                body.append(label)
            else:
                empty = _label("This post has no content.", css=("dim-label",))
                empty.set_margin_bottom(8)
                body.append(empty)
        parent.append(body)

    def _build_trail_step(self, item: Any) -> Gtk.Widget:
        outer = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.add_css_class("trail-step")
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)

        content = _box(spacing=0)
        content.add_css_class("trail-content")
        content.set_hexpand(True)
        content.set_margin_start(2)

        author_row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        author_row.set_margin_bottom(4)
        author_row.set_margin_start(2)

        avatar_url = getattr(item, "avatar", "") or ""
        if avatar_url and bool(_pref(self.ctx, "show_avatars", True)):
            avatar = Adw.Avatar(size=20, text=item.display_name or item.blog_name or "?",
                                show_initials=True)
            avatar.set_valign(Gtk.Align.CENTER)
            self._defer_avatar(avatar, avatar_url, 20)
            author_row.append(avatar)

        name = _label(escape_markup(item.display_name or item.blog_name or ""),
                      css=("caption-heading",))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(24)
        author_row.append(name)
        handle = f"{item.blog_name}.tumblr.com" if getattr(item, "blog_name", "") else ""
        if handle:
            author_row.append(_label("·", wrap=False, css=("caption", "dim-label")))
            handle_label = _label(escape_markup(handle), css=("caption", "dim-label"))
            handle_label.set_ellipsize(Pango.EllipsizeMode.END)
            handle_label.set_max_width_chars(34)
            handle_label.set_hexpand(True)
            author_row.append(handle_label)
        content.append(author_row)

        render_blocks(content, item.content, self.ctx, self.post)
        outer.append(content)
        return outer

    # -- tags --------------------------------------------------------------- #
    def _build_tags(self, parent: Gtk.Box) -> None:
        tags = [str(tag).strip() for tag in (getattr(self.post, "tags", None) or []) if str(tag).strip()]
        if not tags:
            return
        limit = 14
        seen: list[str] = []
        for tag in tags:
            text = tag if tag.startswith("#") else "#" + tag
            if text not in seen:
                seen.append(text)
            if len(seen) >= limit:
                break
        if len(tags) > limit:
            seen.append("…")
        label = Gtk.Label(label="  ".join(seen))
        label.set_use_markup(False)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.FILL)
        label.set_hexpand(True)
        _dim(label)
        label.set_margin_top(6)
        label.set_margin_bottom(2)
        parent.append(label)

    # -- footer ------------------------------------------------------------- #
    def _build_footer(self, parent: Gtk.Box) -> None:
        post = self.post
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("dim-label")
        separator.set_margin_top(6)
        separator.set_margin_bottom(4)
        parent.append(separator)

        footer = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)

        self._like_button = Gtk.Button()
        self._like_button.add_css_class("flat")
        self._like_button.set_valign(Gtk.Align.CENTER)
        self._like_content = Adw.ButtonContent()
        self._like_button.set_child(self._like_content)
        self._like_button.connect("clicked", self._on_like)
        footer.append(self._like_button)

        self._reblog_button = self._action_button(
            _REBLOG_ICON, short_number(post.reblog_count) if post.reblog_count else "",
            "Reblog", self._on_reblog)
        footer.append(self._reblog_button)

        share = self._action_button(_SHARE_ICON, "", "Copy link", self._copy_link)
        footer.append(share)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        footer.append(spacer)

        notes = short_number(post.note_count)
        notes_label = Gtk.Label(label=f"{notes} notes" if post.note_count != 1 else "1 note")
        _dim(notes_label)
        notes_label.set_valign(Gtk.Align.CENTER)
        notes_label.set_xalign(1.0)
        notes_label.set_tooltip_text("Notes")
        footer.append(notes_label)

        parent.append(footer)
        self._sync_like()

    def _action_button(self, icon: str, label: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        button.set_child(Adw.ButtonContent(icon_name=icon, label=label))
        button.connect("clicked", lambda _button: callback())
        return button

    def _sync_like(self) -> None:
        try:
            liked = bool(self.post.is_liked)
            self._like_content.set_icon_name(_LIKE_ICON)
            count = int(getattr(self.post, "like_count", 0) or 0)
            self._like_content.set_label(short_number(count) if count else "")
            if liked:
                self._like_button.add_css_class("accent")
                self._like_button.remove_css_class("dim-label")
            else:
                self._like_button.remove_css_class("accent")
                self._like_button.add_css_class("dim-label")
            self._like_button.set_tooltip_text("Unlike" if liked else "Like")
        except Exception:  # noqa: BLE001
            pass

    # -- actions ------------------------------------------------------------ #
    def _post_url(self) -> str:
        post = self.post
        url = (getattr(post, "post_url", "") or "").strip()
        if url:
            return url
        blog = (getattr(post, "blog_name", "") or "").strip()
        post_id = str(getattr(post, "id", "") or "").strip()
        if blog and post_id:
            return f"https://{blog}.tumblr.com/post/{post_id}"
        return ""

    def _on_open_blog(self, _button: Gtk.Button) -> None:
        self._open_blog()

    def _open_blog(self) -> None:
        post = self.post
        name = (getattr(post, "blog_name", "") or "").strip()
        if not name:
            return
        # The shell turns this into an in-app blog feed; opening the browser as
        # well would do the same job twice.
        try:
            self.ctx.publish("open-blog", name)
        except Exception:  # noqa: BLE001
            pass

    def _open_post(self) -> None:
        self._safe_open(self._post_url())

    def _safe_open(self, url: str) -> None:
        if not url:
            return
        try:
            self.ctx.open_url(url)
        except Exception:  # noqa: BLE001
            pass

    def _copy_link(self) -> None:
        url = self._post_url()
        if url:
            try:
                self.ctx.copy_text(url)
            except Exception:  # noqa: BLE001
                pass

    def _copy_text(self) -> None:
        try:
            text = self._post_text()
        except Exception:  # noqa: BLE001
            return
        if text:
            try:
                self.ctx.copy_text(text)
            except Exception:  # noqa: BLE001
                pass

    def _post_text(self) -> str:
        post = self.post
        text = plain_text(getattr(post, "content", None) or [])
        if text:
            return text
        parts = []
        for item in getattr(post, "trail", None) or []:
            chunk = plain_text(getattr(item, "content", None) or [])
            if chunk:
                parts.append(chunk)
        if parts:
            return "\n\n—\n\n".join(parts)
        return (getattr(post, "summary", "") or "").strip()

    def _on_like(self, _button: Gtk.Button) -> None:
        try:
            self.post.is_liked = not bool(self.post.is_liked)
            delta = 1 if self.post.is_liked else -1
            self.post.like_count = max(0, int(getattr(self.post, "like_count", 0) or 0) + delta)
        except Exception:  # noqa: BLE001
            pass
        self._sync_like()
        try:
            self.ctx.publish("like", self.post)
        except Exception:  # noqa: BLE001
            pass

    def _on_reblog(self) -> None:
        try:
            self.ctx.publish("reblog", self.post)
        except Exception:  # noqa: BLE001
            pass


def _pref(ctx: Any, key: str, default: Any = None) -> Any:
    try:
        return ctx.prefs.get(key, default)
    except Exception:  # noqa: BLE001
        return default
