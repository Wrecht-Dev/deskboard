"""Inbox, drafts and queue: three read-only post lists.

Owner: messaging.

All three are the same widget with a different loader: a recycled
``Gtk.ListView`` of :class:`~deskboard.ui.postcard.PostCard` rows, a small count
strip, pagination through ``Page.next_url`` and an inline retry state.  Nothing
here edits, publishes or deletes anything — that is Phase 3 — so the only
mutation a page can cause is fetching its own next page.
"""
from __future__ import annotations

import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from .. import config
from .messaging import as_bool, describe_error, install_css
from .postcard import PostCard


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _post_key(post) -> str:
    identifier = str(getattr(post, "id", "") or "")
    if identifier:
        return identifier
    blog = str(getattr(post, "blog_name", "") or "")
    stamp = str(getattr(post, "timestamp", "") or "")
    url = str(getattr(post, "post_url", "") or "")
    if blog or stamp or url:
        return f"{blog}|{stamp}|{url}"
    return f"object:{id(post)}"


def _strip_open_tooltips(widget: Gtk.Widget) -> None:
    """Cards get a hover tooltip; in a list it only covers the neighbours."""
    try:
        if widget.get_has_tooltip():
            text = widget.get_tooltip_text() or ""
            if text.startswith("Open "):
                widget.set_has_tooltip(False)
        child = widget.get_first_child()
        while child is not None:
            _strip_open_tooltips(child)
            child = child.get_next_sibling()
    except Exception:  # noqa: BLE001
        pass


def _clear_children(widget: Gtk.Widget) -> None:
    try:
        while True:
            child = widget.get_first_child()
            if child is None:
                break
            if isinstance(widget, Gtk.Box):
                widget.remove(child)
            else:
                child.unparent()
    except Exception:  # noqa: BLE001
        pass


def _queue_paused(raw) -> bool:
    """``state.paused`` lives in the raw payload, in more than one shape."""
    if not isinstance(raw, dict):
        return False
    if as_bool(raw.get("paused")):
        return True
    state = raw.get("state")
    if isinstance(state, dict):
        return as_bool(state.get("paused"))
    if isinstance(state, str):
        return state.strip().lower() == "paused"
    return False


class _PostItem(GObject.Object):
    __gtype_name__ = "DeskboardLibraryPostItem"

    post = GObject.Property(type=object)

    def __init__(self, post) -> None:
        super().__init__()
        self.post = post


# --------------------------------------------------------------------------- #
# shared post-list page
# --------------------------------------------------------------------------- #


class _PostListPage(Gtk.Box):
    """A paginated list of posts with a count strip and retry states."""

    def __init__(
        self,
        ctx,
        *,
        icon: str,
        empty_title: str,
        empty_description: str,
        loader,
        method_name: str,
        singular: str,
        plural: str,
        demo_description: str,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        install_css()
        self.ctx = ctx
        self._icon = icon
        self._empty_title = empty_title
        self._empty_description = empty_description
        self._demo_description = demo_description
        self._loader = loader
        self._method_name = method_name
        self._singular = singular
        self._plural = plural

        self._generation = 0
        self._inflight = False
        self._finished = True
        self._next_url: str | None = None
        self._seen_urls: set[str] = set()
        self._added: set[str] = set()
        self._loaded = False
        self._error = ""
        self._total: int | None = None
        self._fill_source_id = 0
        self._fill_attempts = 0
        self._vadj_handler = 0
        self._destroyed = False

        self.store = Gio.ListStore(item_type=_PostItem)
        self.selection = Gtk.NoSelection(model=self.store)
        self.listview = Gtk.ListView(model=self.selection)
        self.listview.add_css_class("db-msg-post-list")
        self.listview.set_margin_start(12)
        self.listview.set_margin_end(12)
        self.listview.set_margin_top(4)
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
        self.scroller.set_hexpand(True)
        try:
            adjustment = self.scroller.get_vadjustment()
            if adjustment is not None:
                self._vadj_handler = adjustment.connect("value-changed", self._on_scroll)
        except Exception:  # noqa: BLE001
            self._vadj_handler = 0

        self._footer = self._build_footer()
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.scroller)
        self.overlay.add_overlay(self._footer)
        self.overlay.set_vexpand(True)
        self.overlay.set_hexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_error_page(), "error")
        self.stack.add_named(self.overlay, "content")
        self.stack.set_visible_child_name("loading")

        self.append(self._build_pagebar())
        self.append(self._build_notice())
        self.append(self.stack)

        try:
            self.connect("map", self._on_map)
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    # -- public API -------------------------------------------------------- #
    def refresh(self) -> None:
        """Reload from the first page."""
        self._generation += 1
        self._loaded = True
        self._inflight = False
        self._finished = False
        self._next_url = None
        self._seen_urls.clear()
        self._added.clear()
        self._total = None
        self._error = ""
        self._fill_attempts = 0
        self._cancel_fill()
        try:
            self.store.remove_all()
        except Exception:  # noqa: BLE001
            pass
        self._set_count(0)
        self._set_notice_visible(False)
        self._set_footer(None)
        self._show_state("loading")
        self._request(None)

    def cleanup(self) -> None:
        self._destroyed = True
        self._generation += 1
        self._cancel_fill()
        if self._vadj_handler:
            try:
                self.scroller.get_vadjustment().disconnect(self._vadj_handler)
            except Exception:  # noqa: BLE001
                pass
            self._vadj_handler = 0

    def post_count(self) -> int:
        try:
            return self.store.get_n_items()
        except Exception:  # noqa: BLE001
            return 0

    # -- construction ------------------------------------------------------ #
    def _build_pagebar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("db-msg-pagebar")
        self.count_label = Gtk.Label(label="", xalign=0.0)
        self.count_label.add_css_class("db-msg-count")
        self.count_label.set_hexpand(True)
        self.count_label.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.count_label)
        # Refresh lives in the shell header (Ctrl+R calls page.refresh()).
        return bar

    def _build_notice(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.add_css_class("db-msg-notice")
        icon = Gtk.Image.new_from_icon_name("media-playback-pause-symbolic")
        icon.set_valign(Gtk.Align.START)
        box.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)
        self._notice_title = Gtk.Label(label="", xalign=0.0)
        self._notice_title.add_css_class("db-msg-notice-title")
        self._notice_body = Gtk.Label(label="", xalign=0.0)
        self._notice_body.add_css_class("caption")
        self._notice_body.add_css_class("dim-label")
        self._notice_body.set_wrap(True)
        text.append(self._notice_title)
        text.append(self._notice_body)
        box.append(text)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(160)
        revealer.set_child(box)
        revealer.set_reveal_child(False)
        self._notice = revealer
        return revealer

    def _set_notice_visible(self, visible: bool) -> None:
        try:
            self._notice.set_reveal_child(bool(visible))
        except Exception:  # noqa: BLE001
            pass

    def _set_notice_text(self, title: str, body: str) -> None:
        try:
            self._notice_title.set_text(title)
            self._notice_body.set_text(body)
        except Exception:  # noqa: BLE001
            pass

    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.add_css_class("db-msg-state")
        spinner = Adw.Spinner()
        spinner.set_size_request(32, 32)
        box.append(spinner)
        label = Gtk.Label(label="Loading…")
        label.add_css_class("dim-label")
        label.add_css_class("title-4")
        box.append(label)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name(self._icon)
        page.set_title(self._empty_title)
        page.set_description(self._empty_description)
        page.set_vexpand(True)
        page.set_hexpand(True)
        button = Gtk.Button(label="Refresh")
        button.add_css_class("pill")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_args: self.refresh())
        page.set_child(button)
        return page

    def _build_error_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name("network-error-symbolic")
        page.set_title("Couldn't load this page")
        page.set_description("Tumblr did not answer. Check your connection and try again.")
        page.set_vexpand(True)
        page.set_hexpand(True)
        button = Gtk.Button(label="Retry")
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_args: self.refresh())
        page.set_child(button)
        return page

    def _build_footer(self) -> Gtk.Widget:
        self._footer_spinner = Adw.Spinner()
        self._footer_spinner.set_size_request(18, 18)
        self._footer_label = Gtk.Label(label="Loading more…")
        self._footer_label.add_css_class("caption")
        self._footer_retry = Gtk.Button(label="Retry")
        self._footer_retry.add_css_class("flat")
        self._footer_retry.add_css_class("caption")
        self._footer_retry.set_visible(False)
        self._footer_retry.connect("clicked", lambda *_args: self._retry_more())
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        pill.add_css_class("db-msg-pill")
        pill.append(self._footer_spinner)
        pill.append(self._footer_label)
        pill.append(self._footer_retry)
        pill.set_halign(Gtk.Align.CENTER)
        pill.set_valign(Gtk.Align.END)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        revealer.set_transition_duration(160)
        revealer.set_child(pill)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.END)
        revealer.set_margin_bottom(16)
        revealer.set_reveal_child(False)
        return revealer

    def _show_state(self, name: str) -> None:
        try:
            if self.stack.get_visible_child_name() != name:
                self.stack.set_visible_child_name(name)
        except Exception:  # noqa: BLE001
            pass

    # -- list view --------------------------------------------------------- #
    def _setup_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            clamp = Adw.Clamp()
            clamp.set_maximum_size(config.CONTENT_MAX_WIDTH)
            clamp.set_halign(Gtk.Align.FILL)
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.add_css_class("db-msg-post-slot")
            clamp.set_child(holder)
            item.set_child(clamp)
        except Exception:  # noqa: BLE001
            item.set_child(Gtk.Box())

    @staticmethod
    def _holder_for_item(item: Gtk.ListItem):
        try:
            clamp = item.get_child()
            if clamp is None:
                return None
            return clamp.get_child()
        except Exception:  # noqa: BLE001
            return None

    def _bind_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = self._holder_for_item(item)
            if holder is None:
                return
            _clear_children(holder)
            data = item.get_item()
            post = getattr(data, "post", None) if data is not None else None
            if post is None:
                return
            try:
                card = PostCard(post, self.ctx)
            except Exception as exc:  # noqa: BLE001 - one bad post, not a bad page
                sys.stderr.write(f"Deskboard: could not render a post: {exc}\n")
                card = Gtk.Label(label="This post could not be displayed.")
                card.add_css_class("dim-label")
                card.set_margin_top(12)
                card.set_margin_bottom(12)
                card.set_margin_start(16)
                card.set_margin_end(16)
            _strip_open_tooltips(card)
            holder.append(card)
        except Exception:  # noqa: BLE001
            pass

    def _unbind_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = self._holder_for_item(item)
            if holder is not None:
                _clear_children(holder)
        except Exception:  # noqa: BLE001
            pass

    def _teardown_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            item.set_child(None)
        except Exception:  # noqa: BLE001
            pass

    # -- loading ----------------------------------------------------------- #
    def _supported(self) -> bool:
        client = getattr(self.ctx, "client", None)
        return callable(getattr(client, self._method_name, None))

    def _request(self, url: str | None) -> bool:
        if self._inflight or self._destroyed:
            return False
        if url is None and not self._supported():
            self._loaded = True
            self._show_unsupported()
            return False
        if url is not None:
            if url in self._seen_urls:
                self._finished = True
                return False
            self._seen_urls.add(url)
        self._inflight = True
        self._error = ""
        generation = self._generation
        client = getattr(self.ctx, "client", None)
        loader = self._loader

        def work() -> None:
            posts: list = []
            next_url: str | None = None
            total: int | None = None
            raw: dict = {}
            error = ""
            try:
                if url is None:
                    page = loader()
                else:
                    fetch = getattr(client, "fetch_page", None)
                    if not callable(fetch):
                        raise RuntimeError("This Tumblr client cannot paginate.")
                    page = fetch(url)
                if page is not None:
                    posts = list(getattr(page, "posts", None) or [])
                    candidate = getattr(page, "next_url", None)
                    next_url = candidate if isinstance(candidate, str) and candidate else None
                    try:
                        raw_total = getattr(page, "total", None)
                        total = int(raw_total) if raw_total is not None else None
                    except (TypeError, ValueError):
                        total = None
                    page_raw = getattr(page, "raw", None)
                    raw = page_raw if isinstance(page_raw, dict) else {}
            except Exception as exc:  # noqa: BLE001 - never let a worker die loudly
                error = describe_error(exc)
            try:
                GLib.idle_add(
                    self._finish, generation, url, posts, next_url, total, raw, error
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(
                target=work, daemon=True, name=f"library-{self._method_name}-{generation}"
            ).start()
            return True
        except Exception as exc:  # noqa: BLE001
            self._inflight = False
            self._error = describe_error(exc)
            if self.store.get_n_items() == 0:
                self._show_error(self._error)
            else:
                self._set_footer("error")
            return False

    def _finish(
        self,
        generation: int,
        url: str | None,
        posts: list,
        next_url: str | None,
        total: int | None,
        raw: dict,
        error: str,
    ) -> bool:
        if generation != self._generation or self._destroyed:
            return False
        try:
            self._inflight = False
            if error:
                self._error = error
                self._seen_urls.discard(url or "")
                if self.store.get_n_items() == 0:
                    self._show_error(error)
                else:
                    self._set_footer("error")
                return False

            self._error = ""
            self._append(posts)
            if total is not None:
                self._total = total
            if next_url and next_url not in self._seen_urls:
                self._next_url = next_url
                self._finished = False
            else:
                self._next_url = None
                self._finished = True

            self._apply_notice(raw)
            self._set_count(self._visible_count())

            if self.store.get_n_items() == 0:
                self._show_state("empty")
                self._set_footer(None)
            else:
                self._show_state("content")
                self._sync_footer()
                self._schedule_fill()
            return False
        except Exception as exc:  # noqa: BLE001
            try:
                self._error = describe_error(exc)
                if self.store.get_n_items() == 0:
                    self._show_error(self._error)
            except Exception:  # noqa: BLE001
                pass
            return False

    def _append(self, posts: list) -> int:
        added = 0
        for post in posts or []:
            if post is None:
                continue
            key = _post_key(post)
            if key in self._added:
                continue
            self._added.add(key)
            try:
                self.store.append(_PostItem(post))
                added += 1
            except Exception:  # noqa: BLE001
                continue
        return added

    def _visible_count(self) -> int:
        count = self.post_count()
        if self._total is not None and self._total > count:
            return int(self._total)
        return count

    def _set_count(self, count: int) -> None:
        try:
            number = max(0, int(count))
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            text = ""
        elif number == 1:
            text = f"1 {self._singular}"
        else:
            text = f"{number} {self._plural}"
        try:
            self.count_label.set_text(text)
        except Exception:  # noqa: BLE001
            pass

    def _apply_notice(self, raw: dict) -> None:
        """Hook: the queue reveals its paused state, the other pages stay quiet."""
        self._set_notice_visible(False)

    def _show_unsupported(self) -> None:
        demo = bool(getattr(self.ctx, "demo", False))
        signed_in = bool(getattr(self.ctx, "is_signed_in", False))
        if demo:
            description = self._demo_description
        elif not signed_in:
            description = "Sign in to your Tumblr account to see this."
        else:
            description = "Tumblr did not offer this collection for your account."
        try:
            page = self.stack.get_child_by_name("empty")
            if isinstance(page, Adw.StatusPage):
                page.set_description(description)
        except Exception:  # noqa: BLE001
            pass
        self._set_count(0)
        self._show_state("empty")
        self._set_footer(None)

    def _show_error(self, message: str) -> None:
        try:
            page = self.stack.get_child_by_name("error")
            if isinstance(page, Adw.StatusPage):
                page.set_description(message)
            self._show_state("error")
            self._set_footer(None)
        except Exception:  # noqa: BLE001
            pass

    # -- pagination / scroll ----------------------------------------------- #
    def _adjustment(self):
        try:
            return self.scroller.get_vadjustment()
        except Exception:  # noqa: BLE001
            return None

    def _near_bottom(self) -> bool:
        adjustment = self._adjustment()
        if adjustment is None:
            return False
        try:
            return (
                adjustment.get_upper()
                - (adjustment.get_value() + adjustment.get_page_size())
                < 420
            )
        except Exception:  # noqa: BLE001
            return False

    def _underfilled(self) -> bool:
        adjustment = self._adjustment()
        if adjustment is None:
            return False
        try:
            return adjustment.get_upper() <= adjustment.get_page_size() + 6
        except Exception:  # noqa: BLE001
            return False

    def _set_footer(self, mode: str | None) -> None:
        try:
            if mode is None:
                self._footer.set_reveal_child(False)
                return
            if mode == "loading":
                self._footer_spinner.set_visible(True)
                self._footer_label.set_text("Loading more…")
                self._footer_retry.set_visible(False)
            else:
                self._footer_spinner.set_visible(False)
                self._footer_label.set_text("Couldn't load more posts")
                self._footer_retry.set_visible(True)
            self._footer.set_reveal_child(True)
        except Exception:  # noqa: BLE001
            pass

    def _sync_footer(self) -> None:
        try:
            if self.store.get_n_items() == 0 or not self._near_bottom():
                self._set_footer(None)
            elif self._error:
                self._set_footer("error")
            elif self._inflight and not self._finished:
                self._set_footer("loading")
            else:
                self._set_footer(None)
        except Exception:  # noqa: BLE001
            pass

    def _load_more(self) -> None:
        if self._inflight or self._finished or self._next_url is None:
            return
        target = self._next_url
        self._set_footer("loading")
        if not self._request(target):
            self._sync_footer()

    def _retry_more(self) -> None:
        self._error = ""
        target = self._next_url
        if target is None:
            self._sync_footer()
            return
        self._seen_urls.discard(target)
        self._set_footer("loading")
        if not self._request(target):
            self._sync_footer()

    def _on_scroll(self, _adjustment) -> None:
        try:
            if self.store.get_n_items() == 0:
                return
            if not self._near_bottom():
                self._sync_footer()
                return
            if self._error:
                self._set_footer("error")
                return
            if self._inflight:
                self._set_footer("loading")
                return
            if not self._finished:
                self._load_more()
            else:
                self._set_footer(None)
        except Exception:  # noqa: BLE001
            pass

    def _schedule_fill(self) -> None:
        if self._fill_source_id or self._destroyed:
            return
        if self._finished or self._inflight or not self._underfilled():
            return
        if self._fill_attempts >= 8:
            return
        try:
            self._fill_source_id = GLib.timeout_add(140, self._check_fill)
        except Exception:  # noqa: BLE001
            self._fill_source_id = 0

    def _check_fill(self) -> bool:
        self._fill_source_id = 0
        try:
            if self._destroyed or self._finished or self._inflight:
                return False
            if not self._underfilled():
                return False
            self._fill_attempts += 1
            self._load_more()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _cancel_fill(self) -> None:
        if not self._fill_source_id:
            return
        try:
            GLib.source_remove(self._fill_source_id)
        except Exception:  # noqa: BLE001
            pass
        self._fill_source_id = 0

    # -- lifecycle --------------------------------------------------------- #
    def _on_map(self, *_args) -> None:
        if self._loaded:
            return
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args) -> None:
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# public pages
# --------------------------------------------------------------------------- #


class InboxPage(_PostListPage):
    """Asks and submissions addressed to the signed-in account."""

    __gtype_name__ = "DeskboardInboxPage"

    def __init__(self, ctx) -> None:
        super().__init__(
            ctx,
            icon="mail-unread-symbolic",
            empty_title="No asks or submissions",
            empty_description="Asks and submissions you receive will show up here.",
            loader=lambda: ctx.client.get_inbox(limit=20),
            method_name="get_inbox",
            singular="item",
            plural="items",
            demo_description="The inbox is not available in the offline demo.",
        )


class DraftsPage(_PostListPage):
    """The blog's saved drafts (read-only; no editing in this phase)."""

    __gtype_name__ = "DeskboardDraftsPage"

    def __init__(self, ctx, blog) -> None:
        self.blog = str(blog or "").strip()
        target = self.blog
        if target:
            loader = lambda: ctx.client.get_drafts(target, limit=20)  # noqa: E731
        else:
            loader = lambda: None  # noqa: E731
        super().__init__(
            ctx,
            icon="document-save-symbolic",
            empty_title="No drafts",
            empty_description="Posts you save as drafts will show up here.",
            loader=loader,
            method_name="get_drafts",
            singular="draft",
            plural="drafts",
            demo_description="Drafts are not available in the offline demo.",
        )

    def _supported(self) -> bool:
        return bool(self.blog) and super()._supported()

    def _show_unsupported(self) -> None:
        if not self.blog:
            self._set_count(0)
            try:
                page = self.stack.get_child_by_name("empty")
                if isinstance(page, Adw.StatusPage):
                    page.set_description("No blog is selected for drafts.")
            except Exception:  # noqa: BLE001
                pass
            self._show_state("empty")
            self._set_footer(None)
            return
        super()._show_unsupported()


class QueuePage(_PostListPage):
    """The blog's queue, with its paused state surfaced as an inline notice."""

    __gtype_name__ = "DeskboardQueuePage"

    def __init__(self, ctx, blog) -> None:
        self.blog = str(blog or "").strip()
        target = self.blog
        if target:
            loader = lambda: ctx.client.get_queue(target, limit=20)  # noqa: E731
        else:
            loader = lambda: None  # noqa: E731
        super().__init__(
            ctx,
            icon="appointment-soon-symbolic",
            empty_title="Nothing queued",
            empty_description="Posts you add to the queue will show up here.",
            loader=loader,
            method_name="get_queue",
            singular="queued post",
            plural="queued posts",
            demo_description="The queue is not available in the offline demo.",
        )
        self._set_notice_text(
            "This queue is paused",
            "Queued posts are not publishing on the usual schedule. "
            "Unpause from the Tumblr website to resume.",
        )

    def _supported(self) -> bool:
        return bool(self.blog) and super()._supported()

    def _apply_notice(self, raw: dict) -> None:
        self._set_notice_visible(_queue_paused(raw))

    def _show_unsupported(self) -> None:
        if not self.blog:
            self._set_count(0)
            try:
                page = self.stack.get_child_by_name("empty")
                if isinstance(page, Adw.StatusPage):
                    page.set_description("No blog is selected for the queue.")
            except Exception:  # noqa: BLE001
                pass
            self._show_state("empty")
            self._set_footer(None)
            return
        super()._show_unsupported()

