"""Post detail: one post, its permalink and the notes people left on it.

This is the page a card opens into.  It deliberately reuses :class:`PostCard`
for the post body so the NSFW gate, media widgets, trail rendering and action
buttons all behave exactly like they do in the feed — there is no second post
renderer to keep in sync.

Layout::

    ┌──────────────────────────────────────────────────────────┐
    │ (←)  Post                        [ Post │ Notes ]        │
    ├──────────────────────────────────────────────────────────┤
    │  ┌────────────────────────────────────────────────────┐  │
    │  │ PostCard(post)                                     │  │
    │  └────────────────────────────────────────────────────┘  │
    │  🔗 https://….tumblr.com/post/…        [Open] [Copy]    │
    └──────────────────────────────────────────────────────────┘

Notes live on a second view of the same :class:`Adw.ViewStack`; they are only
requested the first time that view is shown, so opening a post costs exactly one
rendered card and no extra API call.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .. import config
from ..util import humanize_time, short_number
from .html import to_pango
from .notes import (
    describe_error,
    load_notes_page,
    note_avatar_url,
    note_blog_name,
    note_display_name,
    note_formatting,
    note_key,
    note_reply_text,
    note_timestamp,
    note_verb,
)
from .postcard import PostCard

_CSS_INSTALLED = False


# --------------------------------------------------------------------------- #
# stylesheet (own file, prefixed classes)
# --------------------------------------------------------------------------- #

def _install_css() -> None:
    """Attach ``postdetail.css`` once; a missing/broken sheet is harmless."""
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    _CSS_INSTALLED = True
    try:
        path = Path(__file__).with_name("postdetail.css")
        if not path.is_file():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except Exception:  # noqa: BLE001 - styling must never break a page
        return


# --------------------------------------------------------------------------- #
# tiny builders
# --------------------------------------------------------------------------- #

def _box(*, orientation: Gtk.Orientation = Gtk.Orientation.VERTICAL,
         spacing: int = 0) -> Gtk.Box:
    return Gtk.Box(orientation=orientation, spacing=spacing)


def _dim(label: Gtk.Label) -> None:
    label.add_css_class("caption")
    label.add_css_class("dim-label")


def _clear_box(box: Any) -> None:
    """Drop every child of a recycled row holder."""
    if box is None:
        return
    try:
        while True:
            child = box.get_first_child()
            if child is None:
                return
            if isinstance(box, Gtk.Box):
                box.remove(child)
            else:  # pragma: no cover - defensive for non-Box holders
                child.unparent()
    except Exception:  # noqa: BLE001
        pass


def _post_url(post: Any) -> str:
    """The canonical web permalink for ``post`` (may be empty)."""
    url = str(getattr(post, "post_url", "") or "").strip()
    if url:
        return url
    blog = str(getattr(post, "blog_name", "") or "").strip()
    post_id = str(getattr(post, "id", "") or "").strip()
    if blog and post_id:
        return f"https://{blog}.tumblr.com/post/{post_id}"
    return ""


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #

class NoteItem(GObject.Object):
    """GObject wrapper so notes can live in a :class:`Gio.ListStore`."""

    __gtype_name__ = "DeskboardNoteItem"

    note = GObject.Property(type=object)

    def __init__(self, note: dict[str, Any]) -> None:
        super().__init__()
        self.note = note


class _NoteRow(Gtk.Box):
    """One note: avatar, clickable actor, verb, relative time, reply body."""

    def __init__(self, note: dict[str, Any], ctx: Any) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("db-detail-note")
        self.set_margin_start(18)
        self.set_margin_end(18)
        self.set_margin_top(5)
        self.set_margin_bottom(5)

        self._ctx = ctx
        self._avatar_url = note_avatar_url(note, 64)
        self._avatar_started = False

        display_name = note_display_name(note) or note_blog_name(note) or "Someone"
        avatar = Adw.Avatar(size=32, text=display_name or "?", show_initials=True)
        avatar.set_valign(Gtk.Align.START)
        avatar.add_css_class("db-detail-note-avatar")
        self._avatar = avatar
        self.append(avatar)

        body = _box(spacing=2)
        body.set_hexpand(True)
        body.set_valign(Gtk.Align.CENTER)

        line = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        line.set_halign(Gtk.Align.FILL)

        blog_name = note_blog_name(note)
        author = Gtk.Button()
        author.add_css_class("flat")
        author.add_css_class("db-detail-note-author")
        author.set_halign(Gtk.Align.START)
        author.set_valign(Gtk.Align.CENTER)
        name_label = Gtk.Label(label=display_name)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_max_width_chars(30)
        name_label.set_xalign(0.0)
        author.set_child(name_label)
        if blog_name:
            author.set_tooltip_text(f"Open @{blog_name}")
            author.connect("clicked", lambda _button: self._open_blog(blog_name))
        else:
            author.set_sensitive(False)
        line.append(author)

        verb = Gtk.Label(label=note_verb(note))
        verb.add_css_class("caption")
        verb.add_css_class("db-detail-note-verb")
        verb.set_valign(Gtk.Align.CENTER)
        line.append(verb)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        line.append(spacer)

        when = humanize_time(note_timestamp(note))
        if when:
            time_label = Gtk.Label(label=when)
            _dim(time_label)
            time_label.set_valign(Gtk.Align.CENTER)
            line.append(time_label)

        body.append(line)

        reply = note_reply_text(note)
        if reply:
            reply_label = Gtk.Label()
            reply_label.set_use_markup(True)
            reply_label.set_markup(to_pango(reply, note_formatting(note)))
            reply_label.set_wrap(True)
            reply_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            reply_label.set_xalign(0.0)
            reply_label.set_halign(Gtk.Align.FILL)
            # Replies are quotes, not the main content: keep them bounded.
            reply_label.set_lines(4)
            reply_label.set_ellipsize(Pango.EllipsizeMode.END)
            reply_label.add_css_class("db-detail-note-reply")
            body.append(reply_label)

        self.append(body)
        self.connect("map", self._on_map)

    # -- avatar ------------------------------------------------------------- #
    def _on_map(self, *_args) -> None:
        """Fetch the avatar only once the row is actually on screen."""
        if self._avatar_started or not self._avatar_url:
            return
        self._avatar_started = True
        url = self._avatar_url
        try:
            texture = self._ctx.images.cached(url, 64)
        except Exception:  # noqa: BLE001
            texture = None
        if texture is not None:
            self._apply_avatar(texture)
            return

        def deliver(texture: Any) -> None:
            if texture is not None:
                self._apply_avatar(texture)

        try:
            self._ctx.images.load(url, 64, 64, deliver)
        except Exception:  # noqa: BLE001 - avatars are optional
            pass

    def _apply_avatar(self, texture: Any) -> None:
        try:
            self._avatar.set_custom_image(texture)
        except Exception:  # noqa: BLE001 - the row may already be recycled
            pass

    # -- actions ------------------------------------------------------------ #
    def _open_blog(self, name: str) -> None:
        try:
            self._ctx.publish("open-blog", name)
        except Exception:  # noqa: BLE001
            pass


class NotesList(Gtk.Box):
    """A recycling list of post notes with ``beforeTimestamp`` pagination.

    ``load()`` fetches the first page; ``load_more()`` follows
    ``ItemPage.next_url``.  Network work happens on a daemon thread and every
    result is validated against a generation counter before it touches widgets.
    """

    __gtype_name__ = "DeskboardNotesList"

    def __init__(self, ctx: Any, blog: str, post_id: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _install_css()
        self.ctx = ctx
        self.blog = str(blog or "").strip()
        self.post_id = str(post_id or "").strip()
        self.add_css_class("db-detail-notes")

        self._generation = 0
        self._started = False
        self._loading = False
        self._next_url: str | None = None
        self._total: int | None = None
        self._error = ""
        self._footer_error = ""
        self._seen: set[str] = set()
        self._destroyed = False

        self.store = Gio.ListStore(item_type=NoteItem)
        self.selection = Gtk.SingleSelection(
            model=self.store, autoselect=False, can_unselect=True
        )
        self.listview = Gtk.ListView(model=self.selection)
        self.listview.add_css_class("db-detail-notes-list")
        self.listview.set_single_click_activate(False)
        self.listview.set_margin_top(4)
        self.listview.set_margin_bottom(8)
        self._factory = self._build_factory()
        self.listview.set_factory(self._factory)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.listview)
        self.scroller.set_vexpand(True)
        self.scroller.set_hexpand(True)

        content = _box(spacing=0)
        content.add_css_class("db-detail-notes-content")
        self._header = self._build_header()
        content.append(self._header)
        content.append(self.scroller)
        content.append(self._build_footer())

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_error_page(), "error")
        self.stack.add_named(content, "content")
        self.stack.set_visible_child_name("loading")
        self.append(self.stack)

        self.connect("destroy", self._on_destroy)

    # -- public API --------------------------------------------------------- #
    def load(self) -> None:
        """Fetch the first page of notes (safe to call more than once)."""
        if self._destroyed:
            return
        if self._started and not self._error:
            return
        self.refresh()

    def refresh(self) -> None:
        """Discard what we have and reload from the newest note."""
        if self._destroyed:
            return
        self._generation += 1
        self._started = False
        self._loading = False
        self._next_url = None
        self._total = None
        self._error = ""
        self._footer_error = ""
        self._seen.clear()
        self._clear_rows()
        self._update_header()
        self._show_state("loading")
        self._sync_footer()
        self._request_page(None)

    def load_more(self) -> None:
        """Follow ``next_url`` for the next page (no-op while busy/finished)."""
        if self._destroyed or self._loading:
            return
        if self._footer_error:
            self._footer_error = ""
            self._sync_footer()
        if not self._next_url:
            return
        self._loading = True
        self._footer_error = ""
        self._sync_footer()
        self._request_page(self._next_url)

    def cleanup(self) -> None:
        """Invalidate any in-flight worker; called when the page is replaced."""
        self._generation += 1
        self._loading = False

    # -- building ----------------------------------------------------------- #
    def _build_header(self) -> Gtk.Widget:
        inner = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.add_css_class("db-detail-notes-header")
        inner.set_margin_start(18)
        inner.set_margin_end(18)
        inner.set_margin_top(10)
        inner.set_margin_bottom(2)

        self._header_label = Gtk.Label()
        self._header_label.add_css_class("db-detail-notes-heading")
        self._header_label.set_xalign(0.0)
        inner.append(self._header_label)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        inner.append(spacer)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(config.CONTENT_MAX_WIDTH)
        clamp.set_halign(Gtk.Align.FILL)
        clamp.set_child(inner)
        return clamp

    def _build_footer(self) -> Gtk.Widget:
        footer = _box(spacing=6)
        footer.add_css_class("db-detail-notes-footer")
        footer.set_halign(Gtk.Align.CENTER)
        footer.set_margin_top(8)
        footer.set_margin_bottom(18)
        footer.set_visible(False)

        status_row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_row.set_halign(Gtk.Align.CENTER)
        self._footer_spinner = Adw.Spinner()
        self._footer_spinner.set_size_request(18, 18)
        self._footer_label = Gtk.Label()
        _dim(self._footer_label)
        self._footer_retry = Gtk.Button(label="Retry")
        self._footer_retry.add_css_class("flat")
        self._footer_retry.add_css_class("caption")
        self._footer_retry.connect("clicked", lambda *_args: self._retry_footer())
        status_row.append(self._footer_spinner)
        status_row.append(self._footer_label)
        status_row.append(self._footer_retry)

        self._load_more = Gtk.Button(label="Load more notes")
        self._load_more.add_css_class("pill")
        self._load_more.set_halign(Gtk.Align.CENTER)
        self._load_more.connect("clicked", lambda *_args: self.load_more())

        footer.append(status_row)
        footer.append(self._load_more)
        self._footer_status_row = status_row
        self._footer = footer
        return footer

    def _build_loading_page(self) -> Gtk.Widget:
        box = _box(spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.add_css_class("db-detail-notes-state")
        spinner = Adw.Spinner()
        spinner.set_size_request(30, 30)
        spinner.set_halign(Gtk.Align.CENTER)
        box.append(spinner)
        label = Gtk.Label(label="Loading notes…")
        label.add_css_class("dim-label")
        box.append(label)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name("emote-love-symbolic")
        page.set_title("No notes yet")
        page.set_description("Nobody has liked, reblogged or replied to this post yet.")
        page.set_vexpand(True)
        self._empty_page = page
        return page

    def _build_error_page(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name("network-error-symbolic")
        page.set_title("Couldn't load notes")
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

    def _build_factory(self) -> Gtk.SignalListItemFactory:
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_item)
        factory.connect("bind", self._bind_item)
        factory.connect("unbind", self._unbind_item)
        factory.connect("teardown", self._teardown_item)
        return factory

    # -- list view factory -------------------------------------------------- #
    def _setup_item(self, _factory: Any, item: Any) -> None:
        try:
            clamp = Adw.Clamp()
            clamp.set_maximum_size(config.CONTENT_MAX_WIDTH)
            clamp.set_halign(Gtk.Align.FILL)
            holder = _box(spacing=0)
            holder.add_css_class("db-detail-note-holder")
            clamp.set_child(holder)
            item.set_child(clamp)
        except Exception:  # noqa: BLE001
            item.set_child(Gtk.Box())

    @staticmethod
    def _holder_for_item(item: Any) -> Any:
        try:
            clamp = item.get_child()
            return clamp.get_child() if clamp is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _bind_item(self, _factory: Any, item: Any) -> None:
        holder = self._holder_for_item(item)
        try:
            _clear_box(holder)
            data = item.get_item()
            note = getattr(data, "note", None) if data is not None else None
            if holder is None or not isinstance(note, dict):
                return
            holder.append(_NoteRow(note, self.ctx))
        except Exception:  # noqa: BLE001 - one broken note must not break the list
            try:
                if holder is not None:
                    _clear_box(holder)
                    fallback = Gtk.Label(label="This note could not be displayed.")
                    _dim(fallback)
                    holder.append(fallback)
            except Exception:  # noqa: BLE001
                pass

    def _unbind_item(self, _factory: Any, item: Any) -> None:
        try:
            _clear_box(self._holder_for_item(item))
        except Exception:  # noqa: BLE001
            pass

    def _teardown_item(self, _factory: Any, item: Any) -> None:
        try:
            _clear_box(self._holder_for_item(item))
            item.set_child(None)
        except Exception:  # noqa: BLE001
            pass

    # -- loading ------------------------------------------------------------ #
    def _request_page(self, next_url: str | None) -> None:
        generation = self._generation
        blog, post_id = self.blog, self.post_id
        client = self.ctx.client
        self._loading = True
        self._error = ""

        def work() -> None:
            page = None
            error = ""
            try:
                page = load_notes_page(client, blog, post_id, next_url)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                error = describe_error(exc)
            try:
                GLib.idle_add(self._finish_page, generation, next_url, page, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(
                target=work, daemon=True, name=f"notes-{blog or 'post'}-{generation}"
            ).start()
        except Exception as exc:  # noqa: BLE001 - e.g. resource exhaustion
            self._finish_page(generation, next_url, None, describe_error(exc))

    def _finish_page(
        self,
        generation: int,
        next_url: str | None,
        page: Any,
        error: str,
    ) -> bool:
        try:
            if generation != self._generation or self._destroyed:
                return False  # a newer load already won
            self._loading = False
            if error:
                if not self._started:
                    self._show_error(error)
                else:
                    self._footer_error = error
                    self._sync_footer()
                return False

            first_page = next_url is None
            if first_page:
                self._started = True
            if page is not None:
                if getattr(page, "total", None) is not None:
                    try:
                        self._total = int(page.total)
                    except (TypeError, ValueError):
                        self._total = None
                self._append_items(getattr(page, "items", None) or [])
                candidate = getattr(page, "next_url", None)
                self._next_url = candidate if isinstance(candidate, str) and candidate else None
            else:
                self._next_url = None

            if self.store.get_n_items() == 0 and not self._next_url:
                self._show_state("empty")
            else:
                self._show_state("content")
            self._update_header()
            self._sync_footer()
            return False
        except Exception as exc:  # noqa: BLE001 - never break the page
            try:
                self._loading = False
                if not self._started:
                    self._show_error(describe_error(exc))
                else:
                    self._footer_error = describe_error(exc)
                    self._sync_footer()
            except Exception:  # noqa: BLE001
                pass
            return False

    def _append_items(self, items: Any) -> int:
        added = 0
        for note in items:
            if not isinstance(note, dict):
                continue
            key = note_key(note)
            if key in self._seen:
                continue
            self._seen.add(key)
            self.store.append(NoteItem(note))
            added += 1
        return added

    # -- state -------------------------------------------------------------- #
    def _show_state(self, name: str) -> None:
        try:
            if self.stack.get_visible_child_name() != name:
                self.stack.set_visible_child_name(name)
        except Exception:  # noqa: BLE001
            pass

    def _show_error(self, message: str) -> None:
        try:
            self._error = message
            self._error_page.set_description(message)
            self._show_state("error")
            self._sync_footer()
        except Exception:  # noqa: BLE001
            pass

    def _update_header(self) -> None:
        try:
            total = self._total if self._total is not None else self.store.get_n_items()
            if total <= 0:
                text = "Notes"
            elif total == 1:
                text = "1 note"
            else:
                text = f"{short_number(total)} notes"
            self._header_label.set_text(text)
        except Exception:  # noqa: BLE001
            pass

    def _sync_footer(self) -> None:
        try:
            if not self._started:
                self._footer.set_visible(False)
                return
            if self._loading:
                self._footer.set_visible(True)
                self._footer_spinner.set_visible(True)
                self._footer_label.set_text("Loading more notes…")
                self._footer_retry.set_visible(False)
                self._footer_status_row.set_visible(True)
                self._load_more.set_visible(False)
                return
            if self._footer_error:
                self._footer.set_visible(True)
                self._footer_spinner.set_visible(False)
                self._footer_label.set_text("Couldn't load more notes")
                self._footer_retry.set_visible(True)
                self._footer_status_row.set_visible(True)
                self._load_more.set_visible(False)
                return
            if self._next_url:
                self._footer.set_visible(True)
                self._footer_status_row.set_visible(False)
                self._footer_retry.set_visible(False)
                self._load_more.set_visible(True)
                return
            self._footer.set_visible(False)
        except Exception:  # noqa: BLE001
            pass

    def _retry_footer(self) -> None:
        self._footer_error = ""
        self._sync_footer()
        if self._next_url:
            self._loading = True
            self._sync_footer()
            self._request_page(self._next_url)

    def _clear_rows(self) -> None:
        try:
            self.store.remove_all()
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args: Any) -> None:
        self._destroyed = True
        self.cleanup()


# --------------------------------------------------------------------------- #
# post detail
# --------------------------------------------------------------------------- #

class PostDetailPage(Gtk.Box):
    """One post plus its notes, with a back action and permalink actions."""

    __gtype_name__ = "DeskboardPostDetailPage"

    def __init__(self, ctx: Any, post: Any, on_back: Any = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _install_css()
        self.ctx = ctx
        self.post = post
        self.on_back = on_back
        self.add_css_class("db-detail-page")

        self._stack = Adw.ViewStack()
        self._stack.set_vexpand(True)
        post_page = self._stack.add_titled(self._build_post_page(), "post", "Post")
        # Without an explicit icon Adw.ViewSwitcher reserves an icon slot and
        # falls back to ``image-missing``; give both pages a real symbolic icon.
        try:
            post_page.set_icon_name("text-x-generic-symbolic")
        except Exception:  # noqa: BLE001 - older libadwaita
            pass

        blog_name = str(getattr(post, "blog_name", "") or "").strip()
        post_id = str(getattr(post, "id", "") or "").strip()
        self.notes = NotesList(ctx, blog_name, post_id)
        notes_page = self._stack.add_titled(self.notes, "notes", "Notes")
        try:
            notes_page.set_icon_name("view-list-symbolic")
        except Exception:  # noqa: BLE001
            pass

        self.append(self._build_top_bar())
        self.append(self._stack)

        try:
            self._stack.connect("notify::visible-child-name", self._on_view_changed)
        except Exception:  # noqa: BLE001
            pass

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        # The shell already offers a back arrow in the window header for hosted
        # pages; keep our own only when nobody else provides one (standalone
        # embedding, harnesses, future page hosts).
        try:
            self.connect("map", self._on_map_visibility)
        except Exception:  # noqa: BLE001
            pass

        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    # -- public API --------------------------------------------------------- #
    def show_notes(self) -> None:
        """Switch to the notes view (loads it on first use)."""
        try:
            self._stack.set_visible_child_name("notes")
        except Exception:  # noqa: BLE001
            pass

    def show_post(self) -> None:
        try:
            self._stack.set_visible_child_name("post")
        except Exception:  # noqa: BLE001
            pass

    def load_notes(self) -> None:
        try:
            self.notes.load()
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        try:
            self.notes.cleanup()
        except Exception:  # noqa: BLE001
            pass

    def refresh(self) -> None:
        """Reload the notes (the shell's Ctrl+R target for hosted pages)."""
        try:
            self.notes.refresh()
        except Exception:  # noqa: BLE001
            pass

    # -- top bar ------------------------------------------------------------ #
    def _build_top_bar(self) -> Gtk.Widget:
        bar = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("db-detail-bar")
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(8)
        bar.set_margin_bottom(4)

        back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back.add_css_class("flat")
        back.set_tooltip_text("Back (Esc)")
        back.set_valign(Gtk.Align.CENTER)
        back.set_sensitive(callable(self.on_back))
        back.connect("clicked", lambda *_args: self._go_back())
        self._back_button = back
        bar.append(back)

        title = Gtk.Label(label="Post")
        title.add_css_class("db-detail-title")
        title.set_xalign(0.0)
        title.set_valign(Gtk.Align.CENTER)
        bar.append(title)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        try:
            switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        except Exception:  # noqa: BLE001 - older libadwaita
            pass
        switcher.set_valign(Gtk.Align.CENTER)
        bar.append(switcher)
        return bar

    # -- post view ---------------------------------------------------------- #
    def _build_post_page(self) -> Gtk.Widget:
        body = _box(spacing=12)
        body.add_css_class("db-detail-post")
        body.set_margin_top(10)
        body.set_margin_bottom(26)
        body.set_margin_start(8)
        body.set_margin_end(8)

        try:
            card = PostCard(self.post, self.ctx)
        except Exception as exc:  # noqa: BLE001 - one bad post, not a broken page
            card = Gtk.Label(label=f"This post could not be displayed.\n{exc}")
            card.add_css_class("dim-label")
            card.set_wrap(True)
            card.set_margin_top(12)
            card.set_margin_bottom(12)
            card.set_margin_start(16)
            card.set_margin_end(16)
        self._card = card
        body.append(card)

        url = _post_url(self.post)
        if url:
            body.append(self._build_permalink_row(url))
            body.append(self._build_action_row(url))

        clamp = Adw.Clamp()
        clamp.set_maximum_size(config.CONTENT_MAX_WIDTH)
        clamp.set_halign(Gtk.Align.FILL)
        clamp.set_child(body)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(clamp)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        return scroller

    def _build_permalink_row(self, url: str) -> Gtk.Widget:
        row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("db-detail-permalink-row")
        row.set_margin_start(18)
        row.set_margin_end(18)

        icon = Gtk.Image.new_from_icon_name("insert-link-symbolic")
        icon.add_css_class("dim-label")
        icon.set_valign(Gtk.Align.START)
        row.append(icon)

        label = Gtk.Label(label=url)
        label.set_selectable(True)
        label.set_xalign(0.0)
        label.set_halign(Gtk.Align.FILL)
        label.set_hexpand(True)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.add_css_class("caption")
        label.add_css_class("db-detail-permalink")
        label.set_tooltip_text(url)
        row.append(label)
        return row

    def _build_action_row(self, url: str) -> Gtk.Widget:
        row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("db-detail-actions")
        row.set_margin_start(18)
        row.set_margin_end(18)

        open_button = Gtk.Button()
        open_button.add_css_class("db-detail-action")
        open_button.set_tooltip_text("Open this post on tumblr.com")
        open_button.set_child(
            Adw.ButtonContent(icon_name="web-browser-symbolic",
                              label="Open in browser")
        )
        open_button.connect("clicked", lambda *_args: self._open_url(url))
        row.append(open_button)

        copy_button = Gtk.Button()
        copy_button.add_css_class("db-detail-action")
        copy_button.set_tooltip_text("Copy this post's permalink")
        copy_button.set_child(
            Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copy link")
        )
        copy_button.connect("clicked", lambda *_args: self._copy_url(url))
        row.append(copy_button)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        return row

    # -- actions ------------------------------------------------------------ #
    def _go_back(self) -> None:
        if not callable(self.on_back):
            return
        try:
            self.on_back()
        except Exception:  # noqa: BLE001
            pass

    def _open_url(self, url: str) -> None:
        if not url:
            return
        try:
            self.ctx.open_url(url)
        except Exception:  # noqa: BLE001
            pass

    def _copy_url(self, url: str) -> None:
        if not url:
            return
        try:
            self.ctx.copy_text(url)
        except Exception:  # noqa: BLE001
            pass

    # -- signals ------------------------------------------------------------ #
    def _on_map_visibility(self, *_args: Any) -> None:
        # Mapping can happen while the host is still deciding whether to show
        # its header back arrow; re-check once the current iteration settles.
        self._sync_back_visibility()
        try:
            GLib.idle_add(self._sync_back_visibility)
        except Exception:  # noqa: BLE001
            pass

    def _sync_back_visibility(self, *_args: Any) -> None:
        """Avoid a second back arrow when the window header already has one."""
        button = getattr(self, "_back_button", None)
        if button is None:
            return
        host = getattr(self.ctx, "window", None)
        host_back = getattr(host, "back_button", None)
        if host_back is not None:
            try:
                if host_back.get_visible():
                    button.set_visible(False)
                    return
            except Exception:  # noqa: BLE001
                pass
        button.set_visible(callable(self.on_back))

    def _on_view_changed(self, *_args: Any) -> None:
        try:
            if self._stack.get_visible_child_name() == "notes":
                self.notes.load()
        except Exception:  # noqa: BLE001
            pass

    def _on_key_pressed(self, _controller: Any, keyval: int,
                        _keycode: int, _state: Any) -> bool:
        if keyval == Gdk.KEY_Escape and callable(self.on_back):
            self._go_back()
            return True
        return False

    def _on_destroy(self, *_args: Any) -> None:
        self.cleanup()
