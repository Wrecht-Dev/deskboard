"""The thing you actually look at: one post card.

Cards are built by a recycled ``Gtk.ListView``; ``__init__`` does no I/O and
every async callback is guarded so a texture that arrives after the row was
recycled can never raise.
"""
from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gtk, Pango

from .. import config
from ..util import humanize_time, short_number
from .blocks import render_blocks
from .html import escape_markup, plain_text

_REBLOG_ICON = "media-playlist-repeat-symbolic"
_LIKE_ICON = "emote-love-symbolic"
_SHARE_ICON = "edit-copy-symbolic"


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


def _load_avatar(avatar: Adw.Avatar, url: str, size: int, ctx: Any) -> None:
    if not url:
        return

    def apply(texture: Any) -> None:
        try:
            if texture is not None:
                avatar.set_custom_image(texture)
        except Exception:  # noqa: BLE001 - the row may be gone already
            pass

    try:
        ctx.images.load(url, size * 2, size * 2, apply)
    except Exception:  # noqa: BLE001 - avatars are strictly optional
        pass


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

        clamp = Adw.Clamp()
        clamp.set_maximum_size(int(getattr(config, "CONTENT_MAX_WIDTH", 760) or 760))
        clamp.set_tightening_threshold(560)
        clamp.set_halign(Gtk.Align.FILL)
        clamp.set_hexpand(True)
        self.append(clamp)

        inner = _box(spacing=0)
        clamp.set_child(inner)

        self._build_header(inner)
        self._build_content(inner)
        self._build_tags(inner)
        self._build_footer(inner)

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
        _load_avatar(avatar, post.blog_avatar, 40, self.ctx)
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

        menu = Gtk.MenuButton()
        menu.set_icon_name("view-more-symbolic")
        menu.add_css_class("flat")
        menu.set_tooltip_text("More actions")
        menu.set_valign(Gtk.Align.START)
        menu.set_popover(self._build_menu())
        header.append(menu)

        parent.append(header)

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
            _load_avatar(avatar, avatar_url, 20, self.ctx)
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
