"""Feed page: a recycled :class:`Gtk.ListView` with paginated loading.

Owner: shell.

The feed is the hot path of the whole application, so everything here is built
around two rules:

* the list model holds lightweight :class:`PostItem` wrappers, and widgets are
  created only when :class:`Gtk.ListView` binds a visible row (and thrown away
  again when it recycles that row), and
* every network call happens on a worker thread and is validated against a
  monotonically increasing generation counter before it may touch the UI.

A page can be replaced at any moment (the user clicks another sidebar entry),
so a result that belongs to an old generation is dropped on the floor.
"""
from __future__ import annotations

import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .. import config
from ..api.client import TumblrError
from .postcard import PostCard

# Start fetching the next page when the viewport is this close to the bottom.
_SCROLL_THRESHOLD = 700
# Never auto-fill more than a handful of pages to cover a very tall window.
_AUTO_FILL_LIMIT = 8


def _describe_error(error: BaseException) -> str:
    """A short, user-safe description of a failed load."""
    if isinstance(error, TumblrError):
        message = str(error) or "Tumblr could not be reached."
    else:
        message = str(error) or type(error).__name__
    message = " ".join(str(message).split())
    if len(message) > 300:
        message = message[:297].rstrip() + "…"
    return message


def _post_key(post) -> str:
    """Stable identity used to drop duplicate posts across page boundaries."""
    post_id = str(getattr(post, "id", "") or "")
    if post_id:
        return post_id
    blog = str(getattr(post, "blog_name", "") or "")
    stamp = str(getattr(post, "timestamp", "") or "")
    if blog or stamp:
        return f"{blog}|{stamp}|{str(getattr(post, 'post_url', '') or '')}"
    return f"object|{id(post)}"


class PostItem(GObject.Object):
    """GObject wrapper so posts can live in a :class:`Gio.ListStore`."""

    __gtype_name__ = "DeskboardPostItem"

    post = GObject.Property(type=object)

    def __init__(self, post) -> None:
        super().__init__()
        self.post = post


class FeedPage(Gtk.Box):
    """One feed (dashboard, likes, blog, tag or search) with infinite scroll."""

    __gtype_name__ = "DeskboardFeedPage"

    def __init__(self, ctx, source, on_progress=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.source = source
        self.on_progress = on_progress
        self._total: int | None = None

        # pagination / request state
        self._generation = 0
        self._loading = False
        self._finished = False
        self._cursor: str | None = None
        self._added: set[str] = set()
        self._more_error = False
        self._auto_attempts = 0
        self._fill_source_id = 0
        self._vadj_handler = 0

        # view state
        self._bound: dict[int, Gtk.Widget] = {}
        self._prefs_unsub = None

        self.store = Gio.ListStore(item_type=PostItem)
        self.selection = Gtk.SingleSelection(
            model=self.store, autoselect=False, can_unselect=True
        )
        self.selection.connect("notify::selected", self._on_selection_changed)

        self.listview = Gtk.ListView(model=self.selection)
        self.listview.add_css_class("feed-list")
        self.listview.set_margin_start(16)
        self.listview.set_margin_end(16)
        self.listview.set_margin_top(10)
        self.listview.set_margin_bottom(28)
        self.listview.set_single_click_activate(False)
        self.listview.connect("activate", self._on_activate)
        self._factory = self._build_factory()
        self.listview.set_factory(self._factory)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.listview)
        self.scroller.set_vexpand(True)
        self.scroller.set_hexpand(True)
        adjustment = self.scroller.get_vadjustment()
        if adjustment is not None:
            self._vadj_handler = adjustment.connect("value-changed", self._on_scroll)

        self._footer = self._build_footer()
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.scroller)
        self.overlay.add_overlay(self._footer)
        self.overlay.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_error_page(), "error")
        self.stack.add_named(self.overlay, "content")
        self.stack.set_visible_child_name("loading")
        self.append(self.stack)

        try:
            self._prefs_unsub = ctx.subscribe("prefs-changed", self._on_prefs_changed)
        except Exception:  # noqa: BLE001 - preferences are optional
            self._prefs_unsub = None
        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    # -- public API -------------------------------------------------------- #
    def load_first(self) -> None:
        """Load the first page (called by the window right after construction)."""
        self.refresh()

    def refresh(self) -> None:
        """Reset pagination and reload from the top."""
        self._generation += 1
        self._loading = False
        self._finished = False
        self._cursor = None
        self._total = None
        self._added.clear()
        self._bound.clear()
        self._more_error = False
        self._auto_attempts = 0
        if self._fill_source_id:
            try:
                GLib.source_remove(self._fill_source_id)
            except Exception:  # noqa: BLE001
                pass
            self._fill_source_id = 0
        self.store.remove_all()
        self._set_footer(None)
        self._show_state("loading")
        self._start_load(None)

    def cleanup(self) -> None:
        """Invalidate in-flight work; called when the page is replaced."""
        self._generation += 1
        self._loading = False
        if self._fill_source_id:
            try:
                GLib.source_remove(self._fill_source_id)
            except Exception:  # noqa: BLE001
                pass
            self._fill_source_id = 0
        if self._prefs_unsub is not None:
            try:
                self._prefs_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._prefs_unsub = None
        if self._vadj_handler:
            try:
                self.scroller.get_vadjustment().disconnect(self._vadj_handler)
            except Exception:  # noqa: BLE001
                pass
            self._vadj_handler = 0
        self._bound.clear()

    def focus_list(self) -> None:
        try:
            self.listview.grab_focus()
        except Exception:  # noqa: BLE001
            pass

    def post_count(self) -> int:
        return self.store.get_n_items()

    def selected_post(self):
        try:
            position = self.selection.get_selected()
            if position == Gtk.INVALID_LIST_POSITION or position >= self.store.get_n_items():
                return None
            item = self.store.get_item(position)
            return getattr(item, "post", None) if item is not None else None
        except Exception:  # noqa: BLE001
            return None

    def move_selection(self, delta: int) -> bool:
        """Move the highlighted row (j / k) and keep it visible."""
        count = self.store.get_n_items()
        if count <= 0:
            return False
        try:
            position = self.selection.get_selected()
            if position == Gtk.INVALID_LIST_POSITION:
                target = 0 if delta >= 0 else count - 1
            else:
                target = max(0, min(count - 1, position + int(delta)))
            self.selection.set_selected(target)
            self.listview.scroll_to(
                target, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def open_selected(self) -> bool:
        post = self.selected_post()
        if post is None:
            return False
        url = str(getattr(post, "post_url", "") or getattr(post, "blog_url", "") or "")
        if not url:
            return False
        self.ctx.open_url(url)
        return True

    def like_selected(self) -> bool:
        post = self.selected_post()
        if post is None:
            return False
        # Cards toggle optimistically before publishing; mirror that here so the
        # window handler always sees the *desired* state rather than a blind flip.
        try:
            post.is_liked = not bool(getattr(post, "is_liked", False))
            delta = 1 if post.is_liked else -1
            post.like_count = max(0, int(getattr(post, "like_count", 0) or 0) + delta)
        except Exception:  # noqa: BLE001
            pass
        self.ctx.publish("like", post)
        return True

    def reblog_selected(self) -> bool:
        post = self.selected_post()
        if post is None:
            return False
        self.ctx.publish("reblog", post)
        return True

    # -- list view factory ------------------------------------------------- #
    def _build_factory(self) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_item)
        factory.connect("bind", self._bind_item)
        factory.connect("unbind", self._unbind_item)
        factory.connect("teardown", self._teardown_item)
        return factory

    def _setup_item(self, _factory, item) -> None:
        try:
            clamp = Adw.Clamp()
            clamp.set_maximum_size(config.CONTENT_MAX_WIDTH)
            clamp.set_halign(Gtk.Align.FILL)
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.add_css_class("post-row")
            clamp.set_child(holder)
            item.set_child(clamp)
        except Exception:  # noqa: BLE001
            item.set_child(Gtk.Box())

    def _bind_item(self, _factory, item) -> None:
        try:
            holder = self._holder_for_item(item)
            if holder is None:
                return
            self._clear_holder(holder)
            data = item.get_item()
            post = getattr(data, "post", None) if data is not None else None
            if post is not None:
                try:
                    card = PostCard(post, self.ctx)
                except Exception as exc:  # noqa: BLE001 - one broken post, not a broken feed
                    sys.stderr.write(f"Deskboard: could not render a post: {exc}\n")
                    card = Gtk.Label(label="This post could not be displayed.")
                    card.add_css_class("dim-label")
                    card.set_margin_top(12)
                    card.set_margin_bottom(12)
                    card.set_margin_start(16)
                    card.set_margin_end(16)
                self._strip_open_tooltips(card)
                holder.append(card)
            position = item.get_position()
            if position != Gtk.INVALID_LIST_POSITION:
                self._bound[position] = holder
                if self.selection.get_selected() == position:
                    holder.add_css_class("selected-post")
        except Exception:  # noqa: BLE001
            pass

    def _unbind_item(self, _factory, item) -> None:
        try:
            holder = self._holder_for_item(item)
            self._forget_holder(holder)
            if holder is not None:
                self._clear_holder(holder)
                holder.remove_css_class("selected-post")
        except Exception:  # noqa: BLE001
            pass

    def _teardown_item(self, _factory, item) -> None:
        try:
            holder = self._holder_for_item(item)
            self._forget_holder(holder)
            if holder is not None:
                self._clear_holder(holder)
            item.set_child(None)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _holder_for_item(item):
        """Return the single content holder inside a (recycled) list row."""
        try:
            clamp = item.get_child()
            if clamp is None:
                return None
            return clamp.get_child()
        except Exception:  # noqa: BLE001
            return None

    def _forget_holder(self, holder) -> None:
        """Drop bookkeeping for a recycled holder, even if its position moved."""
        if holder is None:
            return
        try:
            stale = [position for position, bound in self._bound.items() if bound is holder]
            for position in stale:
                self._bound.pop(position, None)
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def _strip_open_tooltips(cls, widget) -> None:
        """Remove navigation tooltips from cards; they cover the feed on hover."""
        try:
            if widget.get_has_tooltip():
                text = (widget.get_tooltip_text() or widget.get_tooltip_markup() or "")
                if text.startswith("Open "):
                    widget.set_has_tooltip(False)
            child = widget.get_first_child()
            while child is not None:
                cls._strip_open_tooltips(child)
                child = child.get_next_sibling()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _clear_holder(holder: Gtk.Widget) -> None:
        """Drop every recycled card so its textures can be collected promptly.

        Re-reading the first child each round (instead of walking a sibling
        chain after detaching) is deliberate: a detached widget's sibling chain
        is no longer valid, and the classic walk leaves every second child
        behind — which would leak cards (and their textures) on every rebind.
        """
        try:
            while True:
                child = holder.get_first_child()
                if child is None:
                    break
                if isinstance(holder, Gtk.Box):
                    holder.remove(child)
                else:
                    child.unparent()
        except Exception:  # noqa: BLE001
            pass

    def _on_selection_changed(self, *_args) -> None:
        try:
            selected = self.selection.get_selected()
            for position, holder in list(self._bound.items()):
                if position == selected:
                    holder.add_css_class("selected-post")
                else:
                    holder.remove_css_class("selected-post")
        except Exception:  # noqa: BLE001
            pass

    def _on_activate(self, _listview, position: int) -> None:
        try:
            item = self.store.get_item(position)
            post = getattr(item, "post", None) if item is not None else None
            if post is not None:
                url = str(getattr(post, "post_url", "") or getattr(post, "blog_url", "") or "")
                if url:
                    self.ctx.open_url(url)
        except Exception:  # noqa: BLE001
            pass

    # -- state pages ------------------------------------------------------- #
    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.add_css_class("feed-state")

        spinner = Adw.Spinner()
        spinner.set_size_request(34, 34)
        spinner.set_halign(Gtk.Align.CENTER)
        box.append(spinner)

        label = Gtk.Label(label="Loading…")
        label.add_css_class("dim-label")
        label.add_css_class("title-4")
        box.append(label)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name("folder-symbolic")
        page.set_title("Nothing here yet")
        page.set_description(f"No posts in {self.source.label} right now.")
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
        page.set_title("Couldn't load this feed")
        page.set_description("Tumblr did not answer. Check your connection and try again.")
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

    # -- loading footer ---------------------------------------------------- #
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
        revealer.set_transition_duration(180)
        revealer.set_child(pill)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.END)
        revealer.set_margin_bottom(18)
        revealer.set_reveal_child(False)
        return revealer

    def _set_footer(self, mode: str | None) -> None:
        try:
            if mode is None:
                self._footer.set_reveal_child(False)
                return
            if mode == "loading":
                self._footer_spinner.set_visible(True)
                self._footer_label.set_text("Loading more…")
                self._footer_retry.set_visible(False)
            else:  # error
                self._footer_spinner.set_visible(False)
                self._footer_label.set_text("Couldn't load more posts")
                self._footer_retry.set_visible(True)
            self._footer.set_reveal_child(True)
        except Exception:  # noqa: BLE001
            pass

    def _retry_more(self) -> None:
        self._more_error = False
        self._set_footer("loading")
        self._start_load(self._cursor)

    # -- pagination -------------------------------------------------------- #
    def _start_load(self, cursor: str | None) -> None:
        if self._loading:
            return
        if cursor is not None and (self._finished or self._more_error):
            return
        generation = self._generation
        self._loading = True
        if cursor is not None:
            self._set_footer("loading")

        client = self.ctx.client
        source = self.source

        def work() -> None:
            posts: list = []
            next_url: str | None = None
            total: int | None = None
            error = ""
            try:
                page = source.load(client, cursor)
                if page is not None:
                    posts = list(getattr(page, "posts", None) or [])
                    candidate = getattr(page, "next_url", None)
                    next_url = candidate if isinstance(candidate, str) and candidate else None
                    raw_total = getattr(page, "total", None)
                    try:
                        total = int(raw_total) if raw_total is not None else None
                    except (TypeError, ValueError):
                        total = None
            except TumblrError as exc:
                error = _describe_error(exc)
            except Exception as exc:  # noqa: BLE001 - never let a worker die loudly
                error = _describe_error(exc)
            try:
                GLib.idle_add(self._finish_load, generation, posts, next_url, error, total)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(
                target=work, daemon=True, name=f"feed-{source.kind}-{generation}"
            ).start()
        except Exception as exc:  # noqa: BLE001 - e.g. resource exhaustion
            self._loading = False
            self._show_error(_describe_error(exc))

    def _finish_load(
        self,
        generation: int,
        posts: list,
        next_url: str | None,
        error: str,
        total: int | None = None,
    ) -> bool:
        try:
            if generation != self._generation:
                return False  # stale: the feed changed while we were fetching
            initial = self._cursor is None
            self._loading = False
            if error:
                self._more_error = not initial and self.store.get_n_items() > 0
                if self._more_error:
                    self._set_footer("error")
                    self.ctx.toast("Couldn't load more posts")
                else:
                    self._show_error(error)
                return False

            added = 0
            for post in posts:
                if post is None:
                    continue
                key = _post_key(post)
                if key in self._added:
                    continue
                self._added.add(key)
                self.store.append(PostItem(post))
                added += 1

            if total is not None:
                self._total = total
            self._cursor = next_url
            self._finished = next_url is None
            if not initial and added == 0:
                # An empty or all-duplicates page: stop rather than loop forever.
                self._finished = True
            if self.store.get_n_items() == 0:
                self._finished = True
                self._show_state("empty")
                self._set_footer(None)
                return False
            self._more_error = False
            self._set_footer(None)
            self._show_state("content")
            self._notify_progress()
            self._schedule_fill_check()
            return False
        except Exception as exc:  # noqa: BLE001
            try:
                self._loading = False
                self._show_error(_describe_error(exc))
            except Exception:  # noqa: BLE001
                pass
            return False

    def _notify_progress(self) -> None:
        if not callable(self.on_progress):
            return
        try:
            self.on_progress(self.store.get_n_items(), self._total)
        except Exception:  # noqa: BLE001
            pass

    def _show_error(self, message: str) -> None:
        try:
            self._error_page.set_description(message)
            self._show_state("error")
            self._set_footer(None)
        except Exception:  # noqa: BLE001
            pass

    def _schedule_fill_check(self) -> None:
        if self._fill_source_id or self._finished or self._loading:
            return
        try:
            self._fill_source_id = GLib.timeout_add(140, self._check_fill)
        except Exception:  # noqa: BLE001
            self._fill_source_id = 0

    def _check_fill(self) -> bool:
        """Load another page when the first one does not fill the window."""
        self._fill_source_id = 0
        try:
            if self._loading or self._finished or self._more_error:
                return False
            if self._auto_attempts >= _AUTO_FILL_LIMIT:
                return False
            adjustment = self.scroller.get_vadjustment()
            if adjustment is None:
                return False
            if adjustment.get_upper() <= adjustment.get_page_size() + 6:
                self._auto_attempts += 1
                if self._cursor is not None:
                    self._start_load(self._cursor)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_scroll(self, adjustment) -> None:
        try:
            if self._loading or self._finished or self._more_error:
                return
            if self._cursor is None:
                return
            upper = adjustment.get_upper()
            page = adjustment.get_page_size()
            value = adjustment.get_value()
            if upper - (value + page) < _SCROLL_THRESHOLD:
                self._load_more()
        except Exception:  # noqa: BLE001
            pass

    def _load_more(self) -> None:
        if self._loading or self._finished or self._more_error:
            return
        if self._cursor is None:
            return
        self._start_load(self._cursor)

    # -- misc -------------------------------------------------------------- #
    def _on_prefs_changed(self, key: str = "") -> None:
        """Recycle every visible row when a card-affecting preference changes."""
        if key not in ("show_avatars", "compact", "nsfw_blur", "autoplay_video"):
            return
        try:
            factory = self._build_factory()
            self.listview.set_factory(factory)
            self._factory = factory
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args) -> None:
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            pass
