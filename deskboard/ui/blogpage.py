"""Blog page: a blog's identity header over its post feed.

Owner: shell.  Follow is a user-triggered write on a worker thread; nothing
here fires automatically.
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

_ASK_LIMIT = 500

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
        self._following = False
        self._follow_inflight = False
        self._ask_dialog = None

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
        self.follow_button = Gtk.Button()
        self.follow_button.add_css_class("pill")
        self.follow_button.set_sensitive(False)
        follow_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        follow_box.set_halign(Gtk.Align.CENTER)
        self.follow_spinner = Adw.Spinner()
        self.follow_spinner.set_size_request(14, 14)
        self.follow_spinner.set_visible(False)
        follow_box.append(self.follow_spinner)
        self.follow_label = Gtk.Label(label="Follow")
        follow_box.append(self.follow_label)
        self.follow_button.set_child(follow_box)
        self.follow_button.connect("clicked", lambda *_args: self._on_follow_clicked())
        actions.append(self.follow_button)

        self.ask_button = Gtk.Button(label="Ask")
        self.ask_button.add_css_class("pill")
        self.ask_button.set_valign(Gtk.Align.CENTER)
        self.ask_button.set_visible(False)
        self.ask_button.connect("clicked", lambda *_args: self._on_ask_clicked())
        actions.append(self.ask_button)

        self.message_button = Gtk.Button(label="Message")
        self.message_button.add_css_class("pill")
        self.message_button.set_valign(Gtk.Align.CENTER)
        self.message_button.set_visible(False)
        self.message_button.connect("clicked", lambda *_args: self._on_message_clicked())
        actions.append(self.message_button)

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
                self.follow_button.set_sensitive(bool(self.blog_name))
                self.ask_button.set_visible(False)
                self.message_button.set_visible(False)
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
            self._following = bool(getattr(blog, "is_following", False))
            self.follow_button.set_sensitive(True)
            self._sync_follow_button()
            demo = bool(getattr(self.ctx, "demo", False))
            ask = bool(getattr(blog, "ask", False))
            self.ask_button.set_visible(ask)
            self.ask_button.set_sensitive(ask and not demo)
            self.ask_button.set_tooltip_text(
                "Asks aren't available in the demo."
                if demo and ask
                else ("Ask this blog a question" if ask else None)
            )
            can_message = bool(getattr(blog, "can_message", False))
            self.message_button.set_visible(can_message)
            self.message_button.set_sensitive(can_message and not demo)
            self.message_button.set_tooltip_text(
                "Messaging isn't available in the demo."
                if demo and can_message
                else ("Message this blog" if can_message else None)
            )
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

    # -- ask / message ----------------------------------------------------- #
    def _on_ask_clicked(self) -> None:
        if bool(getattr(self.ctx, "demo", False)):
            self.ctx.toast("Asks aren't available in the demo.")
            return
        blog = self._blog
        if blog is None or not bool(getattr(blog, "ask", False)):
            return
        if self._ask_dialog is not None:
            try:
                self._ask_dialog.present(self)
            except Exception:  # noqa: BLE001
                pass
            return
        name = str(getattr(blog, "name", "") or self.blog_name)
        try:
            dialog = Adw.Dialog()
            dialog.set_title(f"Ask @{name}")
            try:
                dialog.set_content_width(540)
                dialog.set_content_height(440)
            except Exception:  # noqa: BLE001
                pass
            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            header.set_title_widget(Adw.WindowTitle(title=f"Ask @{name}", subtitle=""))
            cancel = Gtk.Button(label="Cancel")
            cancel.add_css_class("flat")
            cancel.connect("clicked", lambda *_args: dialog.close())
            header.pack_start(cancel)
            send = Gtk.Button(label="Send")
            send.add_css_class("suggested-action")
            send.set_sensitive(False)
            header.pack_end(send)
            toolbar.add_top_bar(header)

            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            body.set_margin_start(16)
            body.set_margin_end(16)
            body.set_margin_top(14)
            body.set_margin_bottom(10)
            caption = Gtk.Label(label="Your question", xalign=0)
            caption.add_css_class("caption")
            body.append(caption)
            view = Gtk.TextView()
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.set_top_margin(10)
            view.set_bottom_margin(10)
            view.set_left_margin(10)
            view.set_right_margin(10)
            view.set_hexpand(True)
            view.set_vexpand(True)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(view)
            scroller.set_min_content_height(180)
            scroller.set_vexpand(True)
            body.append(scroller)

            counter = Gtk.Label(label=f"0/{_ASK_LIMIT}", xalign=1)
            counter.add_css_class("caption")
            counter.add_css_class("dim-label")
            body.append(counter)

            anonymous = None
            if bool(getattr(blog, "ask_anon", False)):
                anonymous = Gtk.CheckButton(label="Ask anonymously")
                anonymous.set_active(False)
                body.append(anonymous)

            error_label = Gtk.Label(label="")
            error_label.set_wrap(True)
            error_label.set_xalign(0)
            error_label.add_css_class("composer-error")
            error_label.set_visible(False)
            body.append(error_label)
            toolbar.set_content(body)
            dialog.set_child(toolbar)

            def text() -> str:
                buffer = view.get_buffer()
                return buffer.get_text(
                    buffer.get_start_iter(), buffer.get_end_iter(), True
                ).strip()

            def changed(_buffer=None) -> None:
                value = text()
                try:
                    counter.set_text(f"{len(value)}/{_ASK_LIMIT}")
                    over = len(value) > _ASK_LIMIT
                    if over:
                        counter.add_css_class("composer-error")
                    else:
                        counter.remove_css_class("composer-error")
                    send.set_sensitive(bool(value) and not over)
                except Exception:  # noqa: BLE001
                    pass

            view.get_buffer().connect("changed", changed)

            def submit() -> None:
                question = text()
                if not question:
                    return
                if len(question) > _ASK_LIMIT:
                    error_label.set_text(f"Questions are limited to {_ASK_LIMIT} characters.")
                    error_label.set_visible(True)
                    return
                sender = getattr(self.ctx.client, "send_ask", None)
                if not callable(sender):
                    error_label.set_text("Asks aren't available with this Tumblr client.")
                    error_label.set_visible(True)
                    return
                error_label.set_visible(False)
                send.set_sensitive(False)
                send.set_label("Sending…")
                anon = bool(anonymous.get_active()) if anonymous is not None else False
                client = self.ctx.client

                def work() -> None:
                    error = ""
                    try:
                        sender(name, question, anon)
                    except Exception as exc:  # noqa: BLE001
                        error = " ".join(str(exc).split()) or "Tumblr refused the request."
                    try:
                        GLib.idle_add(finish, error)
                    except Exception:  # noqa: BLE001
                        pass

                def finish(error: str) -> bool:
                    try:
                        if error:
                            send.set_sensitive(True)
                            send.set_label("Send")
                            error_label.set_text(f"Couldn't send your ask: {error}")
                            error_label.set_visible(True)
                        else:
                            dialog.close()
                            self.ctx.toast("Ask sent")
                    except Exception:  # noqa: BLE001
                        pass
                    return False

                try:
                    threading.Thread(target=work, daemon=True, name="blog-ask").start()
                except Exception as exc:  # noqa: BLE001
                    finish(str(exc))

            send.connect("clicked", lambda *_args: submit())

            def closed(_dialog) -> None:
                self._ask_dialog = None

            dialog.connect("closed", closed)
            self._ask_dialog = dialog
            dialog.present(self)
        except Exception as exc:  # noqa: BLE001
            self._ask_dialog = None
            self.ctx.toast(f"Couldn't open the ask dialog: {exc}")

    def _on_message_clicked(self) -> None:
        if bool(getattr(self.ctx, "demo", False)):
            self.ctx.toast("Messaging isn't available in the demo.")
            return
        name = str(getattr(self._blog, "name", "") or self.blog_name)
        if not name:
            return
        window = getattr(self.ctx, "window", None)
        try:
            opener = getattr(window, "open_messages_with", None) if window else None
            if callable(opener):
                opener(name)
            else:
                self.ctx.publish("open-dm", name)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(f"Couldn't open messages: {exc}")

    # -- follow (user-triggered write) ------------------------------------- #
    def _sync_follow_button(self, busy: bool = False) -> None:
        try:
            self.follow_label.set_text("Following" if self._following else "Follow")
            self.follow_spinner.set_visible(bool(busy))
            self.follow_button.set_sensitive(not busy and not self._follow_inflight)
            if self._following:
                self.follow_button.remove_css_class("suggested-action")
            else:
                self.follow_button.add_css_class("suggested-action")
        except Exception:  # noqa: BLE001
            pass

    def _on_follow_clicked(self) -> None:
        if self._follow_inflight:
            return
        name = ""
        try:
            name = str(getattr(self._blog, "name", "") or self.blog_name)
        except Exception:  # noqa: BLE001
            name = self.blog_name
        if not name:
            self.ctx.toast("This blog cannot be followed right now.")
            return
        demo = bool(getattr(self.ctx, "demo", False))
        try:
            signed_in = demo or bool(self.ctx.is_signed_in)
        except Exception:  # noqa: BLE001
            signed_in = demo
        if not signed_in:
            self.ctx.toast("Sign in to follow blogs.")
            return

        target = not self._following
        self._following = target
        self._follow_inflight = True
        self._sync_follow_button(busy=True)
        client = self.ctx.client

        def work() -> None:
            error = ""
            try:
                if target:
                    client.follow(name)
                else:
                    client.unfollow(name)
            except Exception as exc:  # noqa: BLE001 - surface to the user
                error = " ".join(str(exc).split()) or "Tumblr refused the request."
            try:
                GLib.idle_add(self._finish_follow, name, target, error, demo)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="blog-follow").start()
        except Exception as exc:  # noqa: BLE001
            self._finish_follow(name, target, str(exc), demo)

    def _finish_follow(self, name: str, target: bool, error: str, demo: bool) -> bool:
        self._follow_inflight = False
        self._ask_dialog = None
        try:
            if self.get_root() is None:
                return False
            if error:
                self._following = not target
                self._sync_follow_button()
                action = "follow" if target else "unfollow"
                self.ctx.toast(f"Couldn't {action} @{name}: {error}")
                return False
            try:
                if self._blog is not None:
                    self._blog.is_following = target
            except Exception:  # noqa: BLE001
                pass
            self._sync_follow_button()
            if demo:
                self.ctx.toast("Demo mode: follow state is not saved.")
            elif target:
                self.ctx.toast(f"Following @{name}")
            else:
                self.ctx.toast(f"Unfollowed @{name}")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _open_in_browser(self) -> None:
        url = self._open_url or (
            f"https://{self.blog_name}.tumblr.com" if self.blog_name else ""
        )
        if url:
            self.ctx.open_url(url)
