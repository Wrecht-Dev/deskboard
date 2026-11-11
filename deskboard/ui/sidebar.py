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

    def __init__(self, ctx, on_select=None, on_search=None, on_sign_in=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.on_select = on_select
        self.on_search = on_search
        self.on_sign_in = on_sign_in

        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._selected_key = ""
        self._syncing = False
        self._tags: list[str] = []
        self._tags_loading = False
        self._tags_pending = False
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

    # -- public API -------------------------------------------------------- #
    def select(self, key: str) -> None:
        """Highlight the row for ``key`` without emitting a selection."""
        self._selected_key = key or ""
        row = self._rows.get(self._selected_key)
        self._syncing = True
        try:
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
            if signed_in:
                self._add_heading("Following")
                self._add_row(FeedSource("dashboard", title="Following", icon="user-home-symbolic"))
                self._add_row(FeedSource("likes", title="Likes", icon="emote-love-symbolic"))

                if self._tags:
                    self._add_heading("Tags")
                    for tag in self._tags:
                        self._add_row(
                            FeedSource("tagged", value=tag, title=f"#{tag}"),
                            marker="#",
                        )

                blogs = [str(name) for name in (getattr(self.ctx, "user_blogs", None) or []) if name]
                if blogs:
                    self._add_heading("Blogs")
                    for name in blogs:
                        self._add_row(
                            FeedSource("blog", value=name, title=name, icon="web-browser-symbolic")
                        )

            self._add_heading("Discover")
            self._add_row(
                FeedSource("blog", value="staff", title="Staff picks", icon="system-users-symbolic")
            )
        finally:
            self._syncing = False

        # Re-apply the current selection (rows were recreated).
        self.select(self._selected_key)

    def _add_heading(self, text: str) -> None:
        try:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            row.set_can_focus(False)
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("sidebar-heading")
            row.set_child(label)
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
        source = getattr(row, "source", None)
        if source is None:
            return
        self._selected_key = source.key
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
                self._tags = cleaned[:24]
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
