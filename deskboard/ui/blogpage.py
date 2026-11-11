"""Blog page: a blog's identity header over its post feed.

Owner: shell.  Everything here is read-only; Follow is intentionally disabled
until the write-auth phase lands.
"""
from __future__ import annotations

import html
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .. import config
from ..api.sources import FeedSource
from ..util import short_number
from .feed import FeedPage

_FOLLOW_TOOLTIP = "needs the write-auth update"


class BlogPage(Gtk.Box):
    __gtype_name__ = "DeskboardBlogPage"

    def __init__(self, ctx, blog_name: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.blog_name = str(blog_name or "").strip()
        self.on_back = True
        self._generation = 0
        self._blog = None
        self._open_url = ""

        self.header = self._build_header()
        self.append(self.header)

        self.feed = FeedPage(
            self.ctx,
            FeedSource("blog", value=self.blog_name, title=self.blog_name or "Blog"),
        )
        self.feed.set_vexpand(True)
        self.append(self.feed)
        try:
            self.feed.load_first()
        except Exception:  # noqa: BLE001
            pass
        self._load_info()

    # -- public API -------------------------------------------------------- #
    def refresh(self) -> None:
        self._load_info()
        try:
            self.feed.refresh()
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        self._generation += 1
        try:
            self.feed.cleanup()
        except Exception:  # noqa: BLE001
            pass

    # -- header ------------------------------------------------------------ #
    def _build_header(self) -> Gtk.Widget:
        outer = Adw.Clamp()
        outer.set_maximum_size(760)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_margin_top(14)
        outer.set_margin_bottom(2)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("blog-header-card")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.avatar = Adw.Avatar()
        self.avatar.set_size(64)
        self.avatar.set_show_initials(True)
        self.avatar.set_text(html.escape(self.blog_name or "?"))
        self.avatar.set_valign(Gtk.Align.START)
        top.append(self.avatar)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        text_box.set_hexpand(True)
        self.title_label = Gtk.Label(label=self.blog_name or "Blog", xalign=0)
        self.title_label.add_css_class("title-2")
        self.title_label.set_ellipsize(3)
        text_box.append(self.title_label)
        self.handle_label = Gtk.Label(label=f"@{self.blog_name}.tumblr.com", xalign=0)
        self.handle_label.add_css_class("dim-label")
        self.handle_label.set_ellipsize(3)
        text_box.append(self.handle_label)
        self.counts_label = Gtk.Label(label="", xalign=0)
        self.counts_label.add_css_class("caption")
        self.counts_label.add_css_class("dim-label")
        self.counts_label.set_visible(False)
        text_box.append(self.counts_label)
        top.append(text_box)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_valign(Gtk.Align.START)
        self.follow_button = Gtk.Button(label="Follow")
        self.follow_button.add_css_class("pill")
        self.follow_button.set_sensitive(False)
        self.follow_button.set_tooltip_text(_FOLLOW_TOOLTIP)
        actions.append(self.follow_button)

        self.open_button = Gtk.Button.new_from_icon_name("web-browser-symbolic")
        self.open_button.set_tooltip_text("Open in browser")
        self.open_button.set_valign(Gtk.Align.CENTER)
        self.open_button.connect("clicked", lambda *_args: self._open_in_browser())
        actions.append(self.open_button)
        top.append(actions)
        card.append(top)

        self.description_label = Gtk.Label(label="")
        self.description_label.set_wrap(True)
        self.description_label.set_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR
        self.description_label.set_xalign(0)
        self.description_label.set_lines(3)
        self.description_label.set_ellipsize(3)
        self.description_label.set_visible(False)
        self.description_label.add_css_class("dim-label")
        card.append(self.description_label)

        self.info_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.info_row.set_visible(False)
        self.info_label = Gtk.Label(label="")
        self.info_label.set_wrap(True)
        self.info_label.set_xalign(0)
        self.info_label.set_hexpand(True)
        self.info_label.add_css_class("caption")
        self.info_label.add_css_class("dim-label")
        self.info_row.append(self.info_label)
        self.info_retry = Gtk.Button(label="Retry")
        self.info_retry.add_css_class("flat")
        self.info_retry.add_css_class("caption")
        self.info_retry.connect("clicked", lambda *_args: self._load_info())
        self.info_row.append(self.info_retry)
        card.append(self.info_row)

        outer.set_child(card)
        return outer

    # -- info -------------------------------------------------------------- #
    def _load_info(self) -> None:
        self._generation += 1
        generation = self._generation
        client = self.ctx.client
        blog_name = self.blog_name

        def work() -> None:
            blog = None
            error = ""
            try:
                getter = getattr(client, "get_blog_info", None)
                if not callable(getter):
                    raise RuntimeError("blog details are unavailable")
                blog = getter(blog_name)
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or "Couldn't load blog details."
            try:
                GLib.idle_add(self._apply_info, generation, blog, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="blog-info").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_info(generation, None, str(exc))

    def _apply_info(self, generation: int, blog, error: str) -> bool:
        try:
            if generation != self._generation:
                return False
            if error or blog is None:
                self.info_label.set_text(error or "Couldn't load blog details.")
                self.info_row.set_visible(True)
                return False
            self._blog = blog
            name = str(getattr(blog, "name", "") or self.blog_name)
            display = str(getattr(blog, "display_name", "") or name)
            handle = str(getattr(blog, "handle", "") or f"{name}.tumblr.com")
            self.title_label.set_text(display)
            self.handle_label.set_text(f"@{handle}")
            description = str(getattr(blog, "description", "") or "").strip()
            if description:
                self.description_label.set_text(description)
                self.description_label.set_visible(True)
            followers = int(getattr(blog, "followers", 0) or 0)
            posts = int(getattr(blog, "posts", 0) or 0)
            parts: list[str] = []
            if followers > 0:
                parts.append(f"{short_number(followers)} followers")
            if posts > 0:
                parts.append(f"{short_number(posts)} posts")
            if parts:
                self.counts_label.set_text(" · ".join(parts))
                self.counts_label.set_visible(True)
            self.avatar.set_text(html.escape(display or name or "?"))
            avatar_url = str(getattr(blog, "avatar", "") or "")
            if avatar_url:
                self._load_avatar(avatar_url)
            self._open_url = str(getattr(blog, "web_url", "") or "")
            self.info_row.set_visible(False)
            # Keep the window title in sync once the real blog title is known.
            window = getattr(self.ctx, "window", None)
            try:
                if window is not None and getattr(window, "page_widget", None) is self:
                    window.window_title.set_title(display or name)
                    window.content_page.set_title(display or name)
                    window.set_title(f"{display or name} — {config.APP_NAME}")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        return False

    def _load_avatar(self, url: str) -> None:
        avatar = self.avatar

        def apply(texture) -> None:
            try:
                if texture is not None and avatar.get_root() is not None:
                    avatar.set_custom_image(texture)
            except Exception:  # noqa: BLE001
                pass

        try:
            self.ctx.images.load(url, 128, 128, apply)
        except Exception:  # noqa: BLE001
            pass

    def _open_in_browser(self) -> None:
        url = self._open_url or (
            f"https://{self.blog_name}.tumblr.com" if self.blog_name else ""
        )
        if url:
            self.ctx.open_url(url)
