"""Sidebar: navigation sections, search and the account footer.

Owner: shell.

``Sidebar`` is a dumb widget: it displays the feeds it is told about, emits a
:class:`~deskboard.api.sources.FeedSource` through ``on_select`` and lets the
window decide what to do with it.  The followed-tag list is the only thing it
loads itself, and that happens on a worker thread.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ..api.sources import FeedSource


class Sidebar(Gtk.Box):
    __gtype_name__ = "DeskboardSidebar"

    def __init__(self, ctx, on_select=None, on_search=None, on_sign_in=None, on_page=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.on_select = on_select
        self.on_search = on_search
        self.on_sign_in = on_sign_in
        self.on_page = on_page

        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._tag_rows: dict[str, Gtk.ListBoxRow] = {}
        self._tags_listbox = None
        self._tags_scroller = None
        self._selected_key = ""
        self._syncing = False
        self._tags: list[str] = []
        self._tags_loading = False
        self._tags_pending = False
        self._trending: list[str] = []
        self._trending_loading = False
        self._trending_loaded = False
        self._identity_gen = 0

        self.set_size_request(268, -1)
        self.add_css_class("deskboard-sidebar")

        # -- search -------------------------------------------------------- #
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        search_box.set_margin_top(10)
        search_box.set_margin_bottom(4)
        search_box.set_margin_start(12)
        search_box.set_margin_end(12)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search Tumblr")
        self.search_entry.set_hexpand(True)
        self.search_entry.set_tooltip_text("Search Tumblr (Ctrl+F or /)")
        self.search_entry.connect("activate", self._on_search_activated)
        search_box.append(self.search_entry)
        self.append(search_box)

        # -- sections ------------------------------------------------------ #
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_show_separators(False)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.connect("row-selected", self._on_row_selected)
        self.scroller.set_child(self.listbox)
        self.append(self.scroller)

        # -- account footer ------------------------------------------------ #
        self.footer = self._build_footer()
        self.append(self.footer)

        self._rebuild()
        self._load_trending()

    # -- public API -------------------------------------------------------- #
    def select(self, key: str) -> None:
        """Highlight the row for ``key`` without emitting a selection."""
        self._selected_key = key or ""
        row = self._rows.get(self._selected_key)
        tag_row = self._tag_rows.get(self._selected_key)
        self._syncing = True
        try:
            if self._tags_listbox is not None:
                if tag_row is not None:
                    self._tags_listbox.select_row(tag_row)
                else:
                    self._tags_listbox.unselect_all()
            if row is not None:
                self.listbox.select_row(row)
            else:
                self.listbox.unselect_all()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._syncing = False

    def focus_search(self) -> None:
        try:
            self.search_entry.grab_focus()
            self.search_entry.select_region(0, -1)
        except Exception:  # noqa: BLE001
            pass

    def search_text(self) -> str:
        try:
            return self.search_entry.get_text().strip()
        except Exception:  # noqa: BLE001
            return ""

    def refresh_identity(self, reload_tags: bool = True) -> None:
        """Rebuild rows after a session change (tags, blogs, account footer)."""
        self._identity_gen += 1
        generation = self._identity_gen
        if not self._signed_in():
            self._tags = []
        self._rebuild()
        self._update_footer()
        if reload_tags and self._signed_in():
            self._load_tags(generation)

    def set_selected_source(self, source) -> None:
        key = getattr(source, "key", "") if source is not None else ""
        self.select(key)

    # -- internals --------------------------------------------------------- #
    def _signed_in(self) -> bool:
        if getattr(self.ctx, "demo", False):
            return True
        try:
            return bool(self.ctx.is_signed_in)
        except Exception:  # noqa: BLE001
            return False

    def _rebuild(self) -> None:
        self._syncing = True
        try:
            child = self.listbox.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                self.listbox.remove(child)
                child = following
            self._rows.clear()

            signed_in = self._signed_in()
            self._tag_rows.clear()
            self._tags_listbox = None
            self._tags_scroller = None
            if signed_in:
                # Home first: the user's "not just following" dashboard tab.
                self._add_row(FeedSource("home", title="Home", icon="go-home-symbolic"))
                self._add_row(
                    FeedSource("dashboard", title="Following", icon="system-users-symbolic")
                )
                self._add_row(FeedSource("likes", title="Likes", icon="emote-love-symbolic"))

            # Explore is public, so it is available signed out too.
            self._add_row(
                FeedSource("explore", title="Explore", icon="view-grid-symbolic")
            )
            self._add_trending_group()

            if signed_in:
                self._add_your_tags_group()

                blogs = [str(name) for name in (getattr(self.ctx, "user_blogs", None) or []) if name]
                if blogs:
                    self._add_heading("Blogs")
                    for name in blogs:
                        self._add_row(
                            FeedSource("blog", value=name, title=name, icon="web-browser-symbolic")
                        )

                # Keep these ungrouped so the Library rows stay reachable without
                # an extra three section headings between them and the footer.
                self._add_action_row("messages", "Messages", "mail-message-new-symbolic")
                self._add_action_row("notifications", "Notifications", "notification-symbolic")
                self._add_heading("Library")
                self._add_action_row("inbox", "Inbox", "mail-unread-symbolic")
                self._add_action_row("drafts", "Drafts", "document-edit-symbolic")
                self._add_action_row("queue", "Queue", "alarm-symbolic")

            self._add_heading("Discover")
            self._add_row(
                FeedSource("blog", value="staff", title="Staff picks", icon="system-users-symbolic")
            )
        finally:
            self._syncing = False

        # Re-apply the current selection (rows were recreated).
        self.select(self._selected_key)

    def _add_heading(self, text: str, trailing: str = "") -> None:
        try:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            row.set_can_focus(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.add_css_class("sidebar-heading-box")
            label = Gtk.Label(label=text, xalign=0)
            label.set_hexpand(True)
            label.add_css_class("sidebar-heading")
            box.append(label)
            if trailing:
                count = Gtk.Label(label=trailing, xalign=1)
                count.add_css_class("sidebar-heading-count")
                box.append(count)
            row.set_child(box)
            self.listbox.append(row)
        except Exception:  # noqa: BLE001
            pass

    def _add_row(self, source: FeedSource, marker: str | None = None) -> None:
        key = source.key
        if key in self._rows:
            return
        try:
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.add_css_class("sidebar-row")
            row.source = source  # type: ignore[attr-defined]

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.set_margin_top(4)
            box.set_margin_bottom(4)

            if marker:
                glyph = Gtk.Label(label=marker, xalign=0.5)
                glyph.set_size_request(16, -1)
                glyph.add_css_class("sidebar-glyph")
                box.append(glyph)
            else:
                icon = Gtk.Image.new_from_icon_name(source.icon or "view-grid-symbolic")
                icon.set_pixel_size(16)
                icon.add_css_class("sidebar-icon")
                box.append(icon)

            label = Gtk.Label(label=source.label, xalign=0)
            label.set_ellipsize(3)  # Pango.EllipsizeMode.END
            label.set_hexpand(True)
            box.append(label)

            row.set_child(box)
            self.listbox.append(row)
            self._rows[key] = row
        except Exception:  # noqa: BLE001
            pass

    def _add_action_row(self, key: str, label: str, icon_name: str) -> None:
        """A non-feed sidebar row that asks the window to build a page."""
        if key in self._rows:
            return
        try:
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.add_css_class("sidebar-row")
            row.page_key = key  # type: ignore[attr-defined]
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(16)
            icon.add_css_class("sidebar-icon")
            box.append(icon)
            label_widget = Gtk.Label(label=label, xalign=0)
            label_widget.set_ellipsize(3)
            label_widget.set_hexpand(True)
            box.append(label_widget)
            row.set_child(box)
            self.listbox.append(row)
            self._rows[key] = row
        except Exception:  # noqa: BLE001
            pass

    def _add_trending_group(self) -> None:
        if not self._trending:
            return
        followed = {tag.lower() for tag in self._tags}
        shown = 0
        self._add_heading("Trending tags")
        for tag in self._trending:
            if tag.lower() in followed:
                continue
            self._add_row(
                FeedSource("tagged", value=tag, title=f"#{tag}"),
                marker="#",
            )
            shown += 1
            if shown >= 6:
                break

    # -- footer ------------------------------------------------------------ #
    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        footer.add_css_class("account-footer")
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        footer.set_margin_top(8)
        footer.set_margin_bottom(10)

        self.footer_avatar = Adw.Avatar()
        self.footer_avatar.set_size(34)
        self.footer_avatar.set_show_initials(True)
        footer.append(self.footer_avatar)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        self.footer_name = Gtk.Label(label="Guest", xalign=0)
        self.footer_name.set_ellipsize(3)
        self.footer_name.add_css_class("heading")
        self.footer_detail = Gtk.Label(label="Browsing publicly", xalign=0)
        self.footer_detail.set_ellipsize(3)
        self.footer_detail.add_css_class("caption")
        self.footer_detail.add_css_class("dim-label")
        text_box.append(self.footer_name)
        text_box.append(self.footer_detail)
        footer.append(text_box)

        menu = Gio.Menu()
        menu.append("Open my blog", "win.open-my-blog")
        menu.append("Preferences", "win.preferences")
        menu.append("Sign out", "win.sign-out")
        self.footer_menu = Gtk.MenuButton()
        self.footer_menu.set_icon_name("open-menu-symbolic")
        self.footer_menu.set_tooltip_text("Account menu")
        self.footer_menu.set_menu_model(menu)
        self.footer_menu.add_css_class("flat")
        footer.append(self.footer_menu)

        self.sign_in_button = Gtk.Button(label="Sign in")
        self.sign_in_button.add_css_class("pill")
        self.sign_in_button.connect("clicked", self._on_sign_in_clicked)
        footer.append(self.sign_in_button)
        return footer

    def _update_footer(self) -> None:
        try:
            signed_in = self._signed_in()
            demo = bool(getattr(self.ctx, "demo", False))
            name = str(getattr(self.ctx, "user_name", "") or "")
            if demo and not name:
                name = "demo"
            if signed_in:
                display = name or "Tumblr"
                self.footer_avatar.set_text(display)
                self.footer_name.set_text(display)
                detail = "Demo mode" if demo else (getattr(self.ctx.session, "source", "") or "Signed in")
                self.footer_detail.set_text(str(detail))
            else:
                self.footer_avatar.set_text("?")
                self.footer_name.set_text("Guest")
                self.footer_detail.set_text("Public feeds only")
            self.footer_avatar.set_visible(signed_in)
            self.footer_menu.set_visible(signed_in)
            self.sign_in_button.set_visible(not signed_in)
        except Exception:  # noqa: BLE001
            pass

    def _on_sign_in_clicked(self, *_args) -> None:
        if callable(self.on_sign_in):
            try:
                self.on_sign_in()
            except Exception:  # noqa: BLE001
                pass

    # -- search / selection ------------------------------------------------ #
    def _on_search_activated(self, _entry) -> None:
        query = self.search_text()
        if not query:
            return
        if callable(self.on_search):
            try:
                self.on_search(query)
            except Exception:  # noqa: BLE001
                pass

    def _on_row_selected(self, _listbox, row) -> None:
        if self._syncing or row is None:
            return
        page_key = getattr(row, "page_key", None)
        source = getattr(row, "source", None)
        if page_key is not None:
            self._selected_key = str(page_key)
            try:
                if self._tags_listbox is not None:
                    self._syncing = True
                    self._tags_listbox.unselect_all()
                    self._syncing = False
            except Exception:  # noqa: BLE001
                pass
            if callable(self.on_page):
                try:
                    self.on_page(str(page_key))
                except Exception:  # noqa: BLE001
                    pass
            return
        if source is None:
            return
        self._selected_key = source.key
        try:
            if self._tags_listbox is not None:
                self._syncing = True
                self._tags_listbox.unselect_all()
                self._syncing = False
        except Exception:  # noqa: BLE001
            pass
        if callable(self.on_select):
            try:
                self.on_select(source)
            except Exception:  # noqa: BLE001
                pass

    # -- "Your tags" ------------------------------------------------------- #
    def _add_your_tags_group(self) -> None:
        """A capped, scrollable list of every tag the account follows."""
        if not self._tags:
            return
        try:
            self._add_heading("Your tags", trailing=f"{len(self._tags)} tags")
            inner = Gtk.ListBox()
            inner.add_css_class("navigation-sidebar")
            inner.add_css_class("tags-list")
            inner.set_selection_mode(Gtk.SelectionMode.SINGLE)
            inner.set_show_separators(False)
            inner.set_activate_on_single_click(True)
            inner.connect("row-selected", self._on_tag_row_selected)

            for tag in self._tags:
                row = Gtk.ListBoxRow()
                row.set_activatable(True)
                row.add_css_class("sidebar-row")
                source = FeedSource("tagged", value=tag, title=f"#{tag}")
                row.source = source  # type: ignore[attr-defined]
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                box.set_margin_start(10)
                box.set_margin_end(10)
                box.set_margin_top(3)
                box.set_margin_bottom(3)
                glyph = Gtk.Label(label="#", xalign=0.5)
                glyph.set_size_request(16, -1)
                glyph.add_css_class("sidebar-glyph")
                box.append(glyph)
                label = Gtk.Label(label=tag, xalign=0)
                label.set_ellipsize(3)
                label.set_hexpand(True)
                box.append(label)
                box.append(self._build_tag_options(tag))
                row.set_child(box)
                inner.append(row)
                self._tag_rows[source.key] = row
                self._tag_rows[
                    FeedSource("tagged", value=tag, title=f"#{tag}", sort="top").key
                ] = row

            scroller = Gtk.ScrolledWindow()
            scroller.add_css_class("tags-scroller")
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_propagate_natural_height(True)
            scroller.set_max_content_height(220)
            try:
                # Overlay scrollbars are invisible until hover; a classic
                # scrollbar makes the capped tag list discoverable.
                scroller.set_overlay_scrolling(False)
                scroller.set_has_frame(False)
            except Exception:  # noqa: BLE001
                pass
            scroller.set_child(inner)

            outer = Gtk.ListBoxRow()
            outer.set_selectable(False)
            outer.set_activatable(False)
            outer.set_can_focus(False)
            outer.set_child(scroller)
            self.listbox.append(outer)
            self._tags_listbox = inner
            self._tags_scroller = scroller
        except Exception:  # noqa: BLE001
            self._tags_listbox = None
            self._tags_scroller = None

    def _build_tag_options(self, tag: str) -> Gtk.MenuButton:
        button = Gtk.MenuButton()
        button.set_icon_name("view-more-symbolic")
        button.add_css_class("flat")
        button.add_css_class("tag-options")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("Tag options")
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        latest = Gtk.Button(label="Open Latest")
        latest.add_css_class("flat")
        latest.set_halign(Gtk.Align.FILL)
        top = Gtk.Button(label="Open Top")
        top.add_css_class("flat")
        top.set_halign(Gtk.Align.FILL)

        def choose(sort: str) -> None:
            try:
                popover.popdown()
            except Exception:  # noqa: BLE001
                pass
            self._open_tag_sort(tag, sort)

        latest.connect("clicked", lambda *_args: choose("latest"))
        top.connect("clicked", lambda *_args: choose("top"))
        box.append(latest)
        box.append(top)
        popover.set_child(box)
        button.set_popover(popover)
        return button

    def _open_tag_sort(self, tag: str, sort: str) -> None:
        source = FeedSource("tagged", value=tag, title=f"#{tag}", sort=sort)
        self._selected_key = source.key
        try:
            self._syncing = True
            self.listbox.unselect_all()
            if self._tags_listbox is not None:
                self._tags_listbox.unselect_all()
            self._syncing = False
        except Exception:  # noqa: BLE001
            self._syncing = False
        if callable(self.on_select):
            try:
                self.on_select(source)
            except Exception:  # noqa: BLE001
                pass

    def _on_tag_row_selected(self, _listbox, row) -> None:
        if self._syncing or row is None:
            return
        source = getattr(row, "source", None)
        if source is None:
            return
        self._selected_key = source.key
        try:
            self._syncing = True
            self.listbox.unselect_all()
            self._syncing = False
        except Exception:  # noqa: BLE001
            pass
        if callable(self.on_select):
            try:
                self.on_select(source)
            except Exception:  # noqa: BLE001
                pass

    # -- followed tags ----------------------------------------------------- #
    def _load_tags(self, generation: int) -> None:
        if self._tags_loading:
            # A second identity refresh arrived while the request was in flight;
            # rerun it once the current result has been consumed.
            self._tags_pending = True
            return
        self._tags_loading = True
        client = self.ctx.client

        def work() -> None:
            tags: list[str] = []
            try:
                try:
                    raw = client.get_followed_tags(limit=200) or []
                except TypeError:
                    raw = client.get_followed_tags() or []
                for item in raw:
                    if isinstance(item, str) and item.strip():
                        tags.append(item.strip())
                    elif isinstance(item, dict) and item.get("name"):
                        tags.append(str(item["name"]).strip())
            except Exception:  # noqa: BLE001 - tags are a nice-to-have
                tags = []
            try:
                GLib.idle_add(self._apply_tags, generation, tags)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="sidebar-tags").start()
        except Exception:  # noqa: BLE001
            self._tags_loading = False

    # -- trending tags ----------------------------------------------------- #
    def _load_trending(self) -> None:
        if self._trending_loading or self._trending_loaded:
            return
        client = self.ctx.client
        getter = getattr(client, "get_trending_tags", None)
        if not callable(getter):
            self._trending_loaded = True
            return
        self._trending_loading = True

        def work() -> None:
            tags: list[str] = []
            try:
                raw = getter(limit=20) or []
                for item in raw:
                    if isinstance(item, str) and item.strip():
                        tags.append(item.strip())
                    elif isinstance(item, dict) and item.get("name"):
                        tags.append(str(item["name"]).strip())
            except Exception:  # noqa: BLE001 - the sub-list is a nice-to-have
                tags = []
            try:
                GLib.idle_add(self._apply_trending, tags)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="sidebar-trending").start()
        except Exception:  # noqa: BLE001
            self._trending_loading = False

    def _apply_trending(self, tags: list[str]) -> bool:
        self._trending_loading = False
        self._trending_loaded = True
        try:
            cleaned: list[str] = []
            seen = set()
            for tag in tags:
                key = tag.lower()
                if tag and key not in seen:
                    seen.add(key)
                    cleaned.append(tag)
            self._trending = cleaned[:20]
            self._rebuild()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _apply_tags(self, generation: int, tags: list[str]) -> bool:
        self._tags_loading = False
        try:
            if generation == self._identity_gen:
                cleaned: list[str] = []
                seen = set()
                for tag in tags:
                    key = tag.lower()
                    if tag and key not in seen:
                        seen.add(key)
                        cleaned.append(tag)
                self._tags = cleaned
                self._rebuild()
            else:
                self._tags_pending = True
        except Exception:  # noqa: BLE001
            pass
        if self._tags_pending:
            self._tags_pending = False
            if self._signed_in():
                self._load_tags(self._identity_gen)
        return False
