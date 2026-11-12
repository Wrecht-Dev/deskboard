"""Notifications / activity feed.

Owner: shell.  Rows link to the actor's blog page; opening the page fires the
one best-effort mark-activity-read request so Tumblr's badge clears.  The list
uses the same recycling pattern as the post feed.
"""
from __future__ import annotations

import html
import threading
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..util import humanize_time

_SCROLL_THRESHOLD = 500
_ACTION_TEXT = {
    "like": "liked your post",
    "reblog": "reblogged your post",
    "reblog_naked": "reblogged your post",
    "reblog_with_content": "reblogged your post",
    "follower": "followed you",
    "reply": "replied to your post",
    "mention": "mentioned you",
    "answer": "answered your question",
    "ask": "sent you an ask",
    "post_attribution": "reblogged your post",
    "post_mentioned": "mentioned you",
    "poll_end": "your poll ended",
}


def _describe_error(error: BaseException) -> str:
    message = " ".join(str(error).split()) or type(error).__name__
    if len(message) > 240:
        message = message[:237] + "…"
    return message


def _before_from_url(url) -> str | None:
    """Extract Tumblr's ``?before=`` pagination cursor from a next URL."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        values = urllib.parse.parse_qs(parsed.query).get("before")
        if values:
            return str(values[0])
    except Exception:  # noqa: BLE001
        pass
    if "before=" in url:
        tail = url.split("before=", 1)[1].split("&", 1)[0]
        return tail or None
    return None


def _avatar_url(item: dict) -> str:
    try:
        value = item.get("fromTumblelogAvatars")
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            best = ""
            best_width = -1
            for entry in value:
                if not isinstance(entry, dict) or not entry.get("url"):
                    continue
                try:
                    width = int(entry.get("width") or 0)
                except (TypeError, ValueError):
                    width = 0
                if width >= best_width:
                    best = str(entry["url"])
                    best_width = width
            return best
    except Exception:  # noqa: BLE001
        pass
    return ""


class NotificationItem(GObject.Object):
    __gtype_name__ = "DeskboardNotificationItem"

    data = GObject.Property(type=object)

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data


class NotificationsPage(Gtk.Box):
    __gtype_name__ = "DeskboardNotificationsPage"

    def __init__(self, ctx) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.on_back = True
        self._generation = 0
        self._loading = False
        self._done = False
        self._infinite_error = ""
        self._next_before: str | None = None
        self._seen: set[str] = set()
        self._vadj_handler = 0
        self._blog = ""
        self._blog_waiting = False
        self._marked_read = False
        self._follow_back_inflight: set[str] = set()
        self._followed_back: set[str] = set()
        self._resolve_blog()

        self.store = Gio.ListStore(item_type=NotificationItem)
        self.listview = Gtk.ListView(model=Gtk.NoSelection(model=self.store))
        self.listview.set_margin_start(16)
        self.listview.set_margin_end(16)
        self.listview.set_margin_top(10)
        self.listview.set_margin_bottom(24)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_item)
        factory.connect("bind", self._bind_item)
        factory.connect("unbind", self._unbind_item)
        factory.connect("teardown", self._teardown_item)
        self.listview.set_factory(factory)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.listview)
        self.scroller.set_vexpand(True)
        adjustment = self.scroller.get_vadjustment()
        if adjustment is not None:
            self._vadj_handler = adjustment.connect("value-changed", self._on_scroll)

        self._footer = self._build_footer()
        overlay = Gtk.Overlay()
        overlay.set_child(self.scroller)
        overlay.add_overlay(self._footer)
        overlay.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_error_page(), "error")
        self.stack.add_named(overlay, "content")
        self.stack.set_visible_child_name("loading")
        self.append(self.stack)

        try:
            self.connect("destroy", lambda *_args: self.cleanup())
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    # -- blog identity ----------------------------------------------------- #
    def _resolve_blog(self) -> str:
        blog = str(getattr(self.ctx, "user_blog", "") or "")
        if not blog and getattr(self.ctx, "user_blogs", None):
            blog = str(self.ctx.user_blogs[0])
        if not blog:
            blog = str(getattr(self.ctx, "user_name", "") or "")
        self._blog = blog
        return blog

    def _ensure_blog(self) -> bool:
        """The account may still be loading when this page is first opened."""
        if self._blog:
            return True
        if self._resolve_blog():
            return True
        if self._blog_waiting:
            return False
        signed_in = bool(getattr(self.ctx, "demo", False))
        try:
            signed_in = signed_in or bool(self.ctx.is_signed_in)
        except Exception:  # noqa: BLE001
            signed_in = False
        if not signed_in:
            return False
        self._blog_waiting = True
        self._show_state("loading")
        try:
            self.ctx.load_user(self._on_user_loaded)
        except Exception:  # noqa: BLE001
            self._blog_waiting = False
        return False

    def _on_user_loaded(self, ok: bool) -> None:
        self._blog_waiting = False
        try:
            if self._resolve_blog():
                self.refresh()
            else:
                self._show_empty("Sign in to see your notifications.")
        except Exception:  # noqa: BLE001
            pass

    # -- public API -------------------------------------------------------- #
    def refresh(self) -> None:
        self._generation += 1
        self._loading = False
        self._done = False
        self._infinite_error = ""
        self._next_before = None
        self._seen.clear()
        self.store.remove_all()
        self._set_footer(None)
        self._show_state("loading")
        self._load_page(None)
        self._mark_activity_read()

    def _mark_activity_read(self) -> None:
        """Clear the activity badge once per page open; failures are ignored."""
        if self._marked_read:
            return
        if bool(getattr(self.ctx, "demo", False)):
            self._marked_read = True
            return
        if not self._blog:
            return
        self._marked_read = True
        client = self.ctx.client
        blog = self._blog

        def work() -> None:
            try:
                client.mark_activity_read(blog)
            except Exception:  # noqa: BLE001 - badge clearing is best-effort
                pass

        try:
            threading.Thread(target=work, daemon=True, name="activity-read").start()
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        self._generation += 1
        self._loading = False
        if self._vadj_handler:
            try:
                self.scroller.get_vadjustment().disconnect(self._vadj_handler)
            except Exception:  # noqa: BLE001
                pass
            self._vadj_handler = 0

    # -- state pages ------------------------------------------------------- #
    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        spinner = Adw.Spinner()
        spinner.set_size_request(32, 32)
        spinner.set_halign(Gtk.Align.CENTER)
        box.append(spinner)
        label = Gtk.Label(label="Loading activity…")
        label.add_css_class("dim-label")
        box.append(label)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name("notification-symbolic")
        page.set_title("No activity yet")
        page.set_description("Likes, reblogs, replies and new followers will show up here.")
        page.set_vexpand(True)
        button = Gtk.Button(label="Refresh")
        button.add_css_class("pill")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_args: self.refresh())
        page.set_child(button)
        self._empty_page = page
        return page

    def _build_error_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name("network-error-symbolic")
        page.set_title("Couldn't load activity")
        page.set_description("Tumblr did not answer.")
        page.set_vexpand(True)
        button = Gtk.Button(label="Retry")
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_args: self.refresh())
        page.set_child(button)
        self._error_page = page
        return page

    def _show_state(self, name: str) -> None:
        try:
            if self.stack.get_visible_child_name() != name:
                self.stack.set_visible_child_name(name)
        except Exception:  # noqa: BLE001
            pass

    def _show_empty(self, description: str | None = None) -> None:
        try:
            if description:
                self._empty_page.set_description(description)
            self._show_state("empty")
            self._set_footer(None)
        except Exception:  # noqa: BLE001
            pass

    def _show_error(self, message: str) -> None:
        try:
            self._error_page.set_description(message)
            self._show_state("error")
            self._set_footer(None)
        except Exception:  # noqa: BLE001
            pass

    # -- footer ------------------------------------------------------------ #
    def _build_footer(self) -> Gtk.Widget:
        self._footer_label = Gtk.Label(label="Loading more…")
        self._footer_label.add_css_class("caption")
        self._footer_spinner = Adw.Spinner()
        self._footer_spinner.set_size_request(18, 18)
        self._footer_retry = Gtk.Button(label="Retry")
        self._footer_retry.add_css_class("flat")
        self._footer_retry.add_css_class("caption")
        self._footer_retry.set_visible(False)
        self._footer_retry.connect("clicked", lambda *_args: self._retry_more())
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        pill.add_css_class("loading-pill")
        pill.append(self._footer_spinner)
        pill.append(self._footer_label)
        pill.append(self._footer_retry)
        pill.set_halign(Gtk.Align.CENTER)
        pill.set_valign(Gtk.Align.END)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        revealer.set_transition_duration(150)
        revealer.set_child(pill)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.END)
        revealer.set_margin_bottom(16)
        revealer.set_reveal_child(False)
        return revealer

    def _set_footer(self, mode: str | None) -> None:
        try:
            if mode is None:
                self._footer.set_reveal_child(False)
                return
            if mode == "error":
                self._footer_spinner.set_visible(False)
                self._footer_label.set_text("Couldn't load more")
                self._footer_retry.set_visible(True)
            else:
                self._footer_spinner.set_visible(True)
                self._footer_label.set_text("Loading more…")
                self._footer_retry.set_visible(False)
            self._footer.set_reveal_child(True)
        except Exception:  # noqa: BLE001
            pass

    def _retry_more(self) -> None:
        self._infinite_error = ""
        self._set_footer("loading")
        self._load_page(self._next_before)

    # -- loading ----------------------------------------------------------- #
    def _load_page(self, before: str | None) -> None:
        if self._loading or self._done:
            return
        if not self._ensure_blog():
            return
        client = self.ctx.client
        getter = getattr(client, "get_notifications", None)
        if not callable(getter):
            if getattr(self.ctx, "demo", False):
                self._show_empty("Notifications are not available in demo mode.")
            else:
                self._show_error("Notifications are unavailable.")
            return
        self._loading = True
        if before:
            self._set_footer("loading")
        generation = self._generation
        blog = self._blog

        def work() -> None:
            items: list = []
            next_before: str | None = None
            error = ""
            try:
                page = getter(blog, limit=30, before=before)
                if page is not None:
                    items = list(getattr(page, "items", None) or [])
                    next_before = _before_from_url(getattr(page, "next_url", None))
            except Exception as exc:  # noqa: BLE001
                error = _describe_error(exc)
            try:
                GLib.idle_add(self._finish_page, generation, items, next_before, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="notifications").start()
        except Exception as exc:  # noqa: BLE001
            self._loading = False
            self._finish_page(generation, [], None, _describe_error(exc))

    def _finish_page(self, generation: int, items: list, next_before, error: str) -> bool:
        try:
            if generation != self._generation:
                return False
            self._loading = False
            if error:
                self._infinite_error = error
                if self.store.get_n_items() == 0:
                    self._show_error(error)
                else:
                    self._set_footer("error")
                return False
            added = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id") or "")
                if ident and ident in self._seen:
                    continue
                if ident:
                    self._seen.add(ident)
                self.store.append(NotificationItem(item))
                added += 1
            self._infinite_error = ""
            self._next_before = next_before
            self._done = not next_before or added == 0
            if self.store.get_n_items() == 0:
                self._show_empty()
            else:
                self._show_state("content")
                self._set_footer(None)
                self._schedule_fill_check()
            return False
        except Exception:  # noqa: BLE001
            try:
                self._loading = False
                self._show_error(_describe_error(Exception("activity failed")))
            except Exception:  # noqa: BLE001
                pass
            return False

    def _schedule_fill_check(self) -> None:
        if self._loading or self._done or self._infinite_error:
            return
        try:
            GLib.timeout_add(180, self._check_fill)
        except Exception:  # noqa: BLE001
            pass

    def _check_fill(self) -> bool:
        """Ask for the next page when the first one leaves blank space."""
        try:
            if self._loading or self._done or self._infinite_error:
                return False
            adjustment = self.scroller.get_vadjustment()
            if adjustment is None:
                return False
            if adjustment.get_upper() <= adjustment.get_page_size() + 6:
                self._set_footer("loading")
                self._load_page(self._next_before)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_scroll(self, adjustment) -> None:
        try:
            if self._loading or self._done:
                return
            upper = adjustment.get_upper()
            page = adjustment.get_page_size()
            value = adjustment.get_value()
            if upper - (value + page) >= _SCROLL_THRESHOLD:
                return
            if self._infinite_error:
                self._set_footer("error")
                return
            self._set_footer("loading")
            self._load_page(self._next_before)
        except Exception:  # noqa: BLE001
            pass

    # -- list factory ------------------------------------------------------ #
    def _setup_item(self, _factory, item) -> None:
        try:
            clamp = Adw.Clamp()
            clamp.set_maximum_size(760)
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.add_css_class("notification-row")
            clamp.set_child(holder)
            item.set_child(clamp)
        except Exception:  # noqa: BLE001
            item.set_child(Gtk.Box())

    def _bind_item(self, _factory, item) -> None:
        try:
            clamp = item.get_child()
            holder = clamp.get_child() if clamp is not None else None
            if holder is None:
                return
            self._clear_holder(holder)
            data = item.get_item()
            notification = getattr(data, "data", None) if data is not None else None
            if not isinstance(notification, dict):
                return
            holder._notif_id = str(notification.get("id") or "")  # type: ignore[attr-defined]
            if notification.get("unread"):
                holder.add_css_class("notification-unread")
            else:
                holder.remove_css_class("notification-unread")
            row = self._build_row(notification)
            holder.append(row)
            self._load_avatar(notification, holder, row)
        except Exception:  # noqa: BLE001
            pass

    def _unbind_item(self, _factory, item) -> None:
        try:
            clamp = item.get_child()
            holder = clamp.get_child() if clamp is not None else None
            if holder is not None:
                self._clear_holder(holder)
                holder.remove_css_class("notification-unread")
        except Exception:  # noqa: BLE001
            pass

    def _teardown_item(self, _factory, item) -> None:
        try:
            item.set_child(None)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _clear_holder(holder: Gtk.Widget) -> None:
        try:
            while True:
                child = holder.get_first_child()
                if child is None:
                    break
                holder.remove(child)
        except Exception:  # noqa: BLE001
            pass

    def _build_row(self, notification: dict) -> Gtk.Widget:
        actor = str(notification.get("fromTumblelogName") or "").strip() or "Someone"
        kind = str(notification.get("type") or "")
        action = _ACTION_TEXT.get(kind) or kind.replace("_", " ").strip() or "sent you a notification"
        timestamp = notification.get("timestamp")
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError):
            timestamp = 0

        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("notification-button")
        button.set_hexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        avatar = Adw.Avatar()
        avatar.set_size(40)
        avatar.set_show_initials(True)
        avatar.set_text(actor)
        avatar.set_valign(Gtk.Align.START)
        box.append(avatar)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        label = Gtk.Label()
        label.set_use_markup(True)
        label.set_markup(
            f"<b>{html.escape(actor)}</b> {html.escape(action)}"
        )
        label.set_wrap(True)
        label.set_wrap_mode(2)
        label.set_xalign(0)
        text.append(label)
        if notification.get("mutuals"):
            badge = Gtk.Label(label="mutuals")
            badge.add_css_class("mutual-badge")
            badge.set_halign(Gtk.Align.START)
            text.append(badge)
        box.append(text)

        if timestamp:
            time_label = Gtk.Label(label=humanize_time(timestamp))
            time_label.add_css_class("caption")
            time_label.add_css_class("dim-label")
            time_label.set_valign(Gtk.Align.START)
            box.append(time_label)

        button.set_child(box)
        if actor:
            button.connect("clicked", lambda *_args: self._open_blog(actor))

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.set_hexpand(True)
        outer.append(button)
        if self._can_follow_back(notification, actor):
            follow = Gtk.Button(label="Follow back")
            follow.add_css_class("pill")
            follow.add_css_class("notification-follow")
            follow.set_valign(Gtk.Align.CENTER)
            follow.set_margin_end(10)
            if actor in self._followed_back:
                follow.set_visible(False)
            elif actor in self._follow_back_inflight:
                follow.set_sensitive(False)
                follow.set_label("Following…")
            follow.connect(
                "clicked",
                lambda _button, name=actor, widget=follow: self._follow_back(name, widget),
            )
            outer.append(follow)
        return outer

    def _load_avatar(self, notification: dict, holder: Gtk.Widget, row: Gtk.Widget) -> None:
        url = _avatar_url(notification)
        if not url:
            return
        notif_id = str(notification.get("id") or "")
        avatar = None

        def find_avatar(widget):
            if isinstance(widget, Adw.Avatar):
                return widget
            try:
                child = widget.get_first_child()
            except Exception:  # noqa: BLE001
                child = None
            while child is not None:
                found = find_avatar(child)
                if found is not None:
                    return found
                child = child.get_next_sibling()
            return None

        try:
            avatar = find_avatar(row)
        except Exception:  # noqa: BLE001
            avatar = None
        if avatar is None:
            return

        def apply(texture) -> None:
            try:
                if texture is None or avatar is None:
                    return
                if getattr(holder, "_notif_id", None) != notif_id:
                    return
                if avatar.get_root() is None:
                    return
                avatar.set_custom_image(texture)
            except Exception:  # noqa: BLE001
                pass

        try:
            self.ctx.images.load(url, 64, 64, apply)
        except Exception:  # noqa: BLE001
            pass

    def _can_follow_back(self, notification: dict, actor: str) -> bool:
        if not actor:
            return False
        if bool(getattr(self.ctx, "demo", False)):
            return False
        try:
            if not self.ctx.is_signed_in:
                return False
        except Exception:  # noqa: BLE001
            return False
        if actor in self._followed_back:
            return False
        own = {str(getattr(self.ctx, "user_name", "") or "").lower()}
        own.add(str(getattr(self.ctx, "user_blog", "") or "").lower())
        for name in getattr(self.ctx, "user_blogs", None) or []:
            own.add(str(name or "").lower())
        if actor.lower() in own:
            return False
        kind = str(notification.get("type") or "").lower()
        following_you = bool(notification.get("followingYou"))
        if kind not in ("follower", "follow") and not following_you:
            return False
        if notification.get("followed") is True:
            return False
        if notification.get("mutuals"):
            return False
        return True

    def _follow_back(self, actor: str, button: Gtk.Button) -> None:
        if not actor or actor in self._follow_back_inflight:
            return
        try:
            if not self.ctx.is_signed_in:
                return
        except Exception:  # noqa: BLE001
            return
        client = self.ctx.client
        follow = getattr(client, "follow", None)
        if not callable(follow):
            self.ctx.toast("Following isn't available with this client.")
            return
        self._follow_back_inflight.add(actor)
        try:
            button.set_sensitive(False)
            button.set_label("Following…")
        except Exception:  # noqa: BLE001
            pass

        def work() -> None:
            error = ""
            try:
                follow(actor)
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or "Tumblr refused the request."
            try:
                GLib.idle_add(self._finish_follow_back, actor, button, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="activity-follow").start()
        except Exception as exc:  # noqa: BLE001
            self._finish_follow_back(actor, button, str(exc))

    def _finish_follow_back(self, actor: str, button: Gtk.Button, error: str) -> bool:
        self._follow_back_inflight.discard(actor)
        try:
            if error:
                button.set_label("Follow back")
                button.set_sensitive(True)
                self.ctx.toast(f"Couldn't follow @{actor}: {error}")
            else:
                self._followed_back.add(actor)
                button.set_label("Following")
                button.set_sensitive(False)
                self.ctx.toast(f"Following @{actor}")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _open_blog(self, name: str) -> None:
        try:
            self.ctx.publish("open-blog", name)
        except Exception:  # noqa: BLE001
            pass
