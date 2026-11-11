"""Search: posts, blogs, tags and communities in one view.

Owner: shell.  The Posts tab is a :class:`FeedPage`; the other three tabs are
plain result lists from ``client.search_blogs`` (one request feeds all three).
"""
from __future__ import annotations

import html
import threading
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..api.sources import FeedSource
from ..util import short_number
from .feed import FeedPage

_RESULT_TABS = (
    ("blogs", "Blogs"),
    ("tags", "Tags"),
    ("communities", "Communities"),
)


class SearchPage(Gtk.Box):
    __gtype_name__ = "DeskboardSearchPage"

    def __init__(self, ctx, query: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.query = str(query or "").strip()
        self.on_back = True
        self._generation = 0

        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.switcher = Adw.ViewSwitcher()
        self.switcher.set_stack(self.stack)
        try:
            self.switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        except Exception:  # noqa: BLE001
            pass
        switcher_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        switcher_box.set_halign(Gtk.Align.CENTER)
        switcher_box.set_margin_top(10)
        switcher_box.set_margin_bottom(6)
        switcher_box.set_margin_start(12)
        switcher_box.set_margin_end(12)
        switcher_box.append(self.switcher)
        # A BreakpointBin lets the switcher collapse to icons on narrow windows
        # without clipping the tab labels.
        self.switcher_bin = Adw.BreakpointBin()
        self.switcher_bin.set_hexpand(True)
        # BreakpointBin wants an explicit minimum size to evaluate breakpoints.
        self.switcher_bin.set_size_request(1, 1)
        self.switcher_bin.set_child(switcher_box)
        try:
            condition = Adw.BreakpointCondition.parse("max-width: 720px")
            breakpoint = Adw.Breakpoint.new(condition)
            breakpoint.add_setter(self.switcher, "policy", Adw.ViewSwitcherPolicy.NARROW)
            self.switcher_bin.add_breakpoint(breakpoint)
            self._breakpoint = breakpoint
        except Exception:  # noqa: BLE001 - keep labels if breakpoints are unsupported
            self._breakpoint = None
        self.append(self.switcher_bin)
        self.append(self.stack)

        self._result_stacks: dict[str, Gtk.Stack] = {}
        self._result_lists: dict[str, Gtk.ListBox] = {}
        self._result_empty: dict[str, Adw.StatusPage] = {}
        self._result_error_pages: dict[str, Adw.StatusPage] = {}

        # Posts is first, matching the web search tabs.
        self.feed = FeedPage(
            self.ctx, FeedSource("search", value=self.query, title=f"Search: {self.query}")
        )
        self.feed.set_vexpand(True)
        try:
            posts_page = self.stack.add_titled(self.feed, "posts", "Posts")
            posts_page.set_icon_name("view-list-symbolic")
        except Exception:  # noqa: BLE001
            self.stack.add_named(self.feed, "posts")
        icon_names = {
            "blogs": "web-browser-symbolic",
            "tags": "view-grid-symbolic",
            "communities": "system-users-symbolic",
        }
        for kind, title in _RESULT_TABS:
            child = self._build_result_page(kind)
            try:
                page = self.stack.add_titled(child, kind, title)
                page.set_icon_name(icon_names.get(kind, "view-grid-symbolic"))
            except Exception:  # noqa: BLE001
                self.stack.add_named(child, kind)
        try:
            self.stack.set_visible_child_name("posts")
        except Exception:  # noqa: BLE001
            pass

        try:
            self.feed.load_first()
        except Exception:  # noqa: BLE001
            pass
        self._load_results()

    # -- public API -------------------------------------------------------- #
    def focus_posts(self) -> None:
        self.feed.focus_list()

    def refresh(self) -> None:
        try:
            self.feed.refresh()
        except Exception:  # noqa: BLE001
            pass
        self._load_results()

    def cleanup(self) -> None:
        self._generation += 1
        try:
            self.feed.cleanup()
        except Exception:  # noqa: BLE001
            pass

    # -- result tabs ------------------------------------------------------- #
    def _build_result_page(self, kind: str) -> Gtk.Widget:
        stack = Gtk.Stack()
        stack.set_vexpand(True)
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(120)

        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        loading.set_halign(Gtk.Align.CENTER)
        loading.set_valign(Gtk.Align.CENTER)
        spinner = Adw.Spinner()
        spinner.set_size_request(32, 32)
        loading.append(spinner)
        label = Gtk.Label(label="Searching…")
        label.add_css_class("dim-label")
        loading.append(label)
        stack.add_named(loading, "loading")

        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        listbox.add_css_class("search-results")
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.set_activate_on_single_click(True)
        listbox.set_margin_start(18)
        listbox.set_margin_end(18)
        listbox.set_margin_top(10)
        listbox.set_margin_bottom(24)
        listbox.connect("row-activated", self._on_result_activated)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(760)
        clamp.set_child(listbox)
        scroller.set_child(clamp)
        stack.add_named(scroller, "results")

        empty = Adw.StatusPage()
        empty.set_icon_name("folder-symbolic")
        empty.set_title("Nothing found")
        empty.set_description(f"No {kind} matched “{self.query}”.")
        empty.set_vexpand(True)
        stack.add_named(empty, "empty")

        error = Adw.StatusPage()
        error.set_icon_name("network-error-symbolic")
        error.set_title("Couldn't search")
        error.set_description("Tumblr did not answer.")
        error.set_vexpand(True)
        retry = Gtk.Button(label="Retry")
        retry.add_css_class("pill")
        retry.set_halign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_args: self._load_results())
        error.set_child(retry)
        stack.add_named(error, "error")

        stack.set_visible_child_name("loading")
        self._result_stacks[kind] = stack
        self._result_lists[kind] = listbox
        self._result_empty[kind] = empty
        self._result_error_pages[kind] = error
        return stack

    def _load_results(self) -> None:
        self._generation += 1
        generation = self._generation
        for kind, _title in _RESULT_TABS:
            try:
                self._result_stacks[kind].set_visible_child_name("loading")
            except Exception:  # noqa: BLE001
                pass

        client = self.ctx.client
        getter = getattr(client, "search_blogs", None)
        if not callable(getter):
            describe = (
                "Blog, tag and community search is not available in demo mode."
                if getattr(self.ctx, "demo", False)
                else "Searching blogs, tags and communities is not available here."
            )
            for kind, _title in _RESULT_TABS:
                self._show_empty(kind, describe)
            return

        query = self.query

        def work() -> None:
            data: dict = {}
            error = ""
            try:
                result = getter(query, limit=20)
                data = result if isinstance(result, dict) else {}
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or "Tumblr did not answer."
                if len(error) > 240:
                    error = error[:237] + "…"
            try:
                GLib.idle_add(self._apply_results, generation, data, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="search-results").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_results(generation, {}, str(exc))

    def _apply_results(self, generation: int, data: dict, error: str) -> bool:
        try:
            if generation != self._generation:
                return False
            if error:
                for kind, _title in _RESULT_TABS:
                    self._show_error(kind, error)
                return False
            for kind, _title in _RESULT_TABS:
                items = data.get(kind)
                if not isinstance(items, list):
                    items = []
                self._populate(kind, items)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _show_empty(self, kind: str, description: str) -> None:
        try:
            empty = self._result_empty[kind]
            empty.set_description(description)
            self._result_stacks[kind].set_visible_child_name("empty")
        except Exception:  # noqa: BLE001
            pass

    def _show_error(self, kind: str, message: str) -> None:
        try:
            page = self._result_error_pages[kind]
            page.set_description(message)
            self._result_stacks[kind].set_visible_child_name("error")
        except Exception:  # noqa: BLE001
            pass

    def _populate(self, kind: str, items: list) -> None:
        listbox = self._result_lists.get(kind)
        if listbox is None:
            return
        child = listbox.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            listbox.remove(child)
            child = following
        if not items:
            self._show_empty(kind, f"No {kind} matched “{self.query}”.")
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            row = self._make_row(kind, item)
            if row is not None:
                listbox.append(row)
        self._result_stacks[kind].set_visible_child_name("results")

    # -- rows -------------------------------------------------------------- #
    def _make_row(self, kind: str, item: dict):
        try:
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(False)
            row.add_css_class("search-row")

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.set_hexpand(True)

            if kind == "blogs":
                name = str(item.get("name") or "").strip()
                title = str(item.get("title") or name or "Blog").strip()
                avatar = self._make_avatar(str(title or name), self._avatar_url(item.get("avatar")))
                box.append(avatar)
                title_label = Gtk.Label(label=title, xalign=0)
                title_label.add_css_class("heading")
                title_label.set_ellipsize(3)
                text.append(title_label)
                subtitle = f"@{name}" if name else ""
                count = self._count_label(item)
                if count:
                    subtitle = f"{subtitle} · {count}" if subtitle else count
                if subtitle:
                    sub = Gtk.Label(label=subtitle, xalign=0)
                    sub.add_css_class("caption")
                    sub.add_css_class("dim-label")
                    sub.set_ellipsize(3)
                    text.append(sub)
                row.handler = lambda value=name: self._open_blog(value)  # type: ignore[attr-defined]
            elif kind == "tags":
                tag = str(item.get("tag") or item.get("name") or "").strip().lstrip("#")
                glyph = Gtk.Label(label="#", xalign=0.5)
                glyph.add_css_class("search-glyph")
                glyph.set_size_request(38, -1)
                box.append(glyph)
                title_label = Gtk.Label(label=f"#{tag}", xalign=0)
                title_label.add_css_class("heading")
                title_label.set_ellipsize(3)
                text.append(title_label)
                subtitle = self._count_label(item)
                if subtitle:
                    sub = Gtk.Label(label=subtitle, xalign=0)
                    sub.add_css_class("caption")
                    sub.add_css_class("dim-label")
                    sub.set_ellipsize(3)
                    text.append(sub)
                row.handler = lambda value=tag: self._open_tag(value)  # type: ignore[attr-defined]
            else:  # communities
                name = str(item.get("name") or "").strip()
                title = str(item.get("title") or name or "Community").strip()
                avatar = self._make_avatar(
                    str(title or name), self._avatar_url(item.get("avatarImage"))
                )
                box.append(avatar)
                title_label = Gtk.Label(label=title, xalign=0)
                title_label.add_css_class("heading")
                title_label.set_ellipsize(3)
                text.append(title_label)
                subtitle = str(item.get("subtext") or item.get("name") or "").strip()
                if subtitle:
                    sub = Gtk.Label(label=subtitle, xalign=0)
                    sub.add_css_class("caption")
                    sub.add_css_class("dim-label")
                    sub.set_ellipsize(3)
                    text.append(sub)
                url = str(item.get("url") or "")
                row.handler = lambda value=url: self._open_community(value)  # type: ignore[attr-defined]

            box.append(text)
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.add_css_class("dim-label")
            chevron.set_valign(Gtk.Align.CENTER)
            box.append(chevron)
            row.set_child(box)
            return row
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _avatar_url(value) -> str:
        try:
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                best = ""
                best_width = -1
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    width = int(item.get("width") or 0)
                    if width >= best_width and item.get("url"):
                        best = str(item["url"])
                        best_width = width
                return best
            if isinstance(value, dict):
                return str(value.get("url") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _make_avatar(self, text: str, url: str) -> Gtk.Widget:
        avatar = Adw.Avatar(size=38, text=text or "?", show_initials=True)
        avatar.set_valign(Gtk.Align.CENTER)
        if url:
            def apply(texture) -> None:
                try:
                    if texture is not None and avatar.get_root() is not None:
                        avatar.set_custom_image(texture)
                except Exception:  # noqa: BLE001
                    pass

            try:
                self.ctx.images.load(url, 76, 76, apply)
            except Exception:  # noqa: BLE001
                pass
        return avatar

    @staticmethod
    def _count_label(item: dict) -> str:
        parts: list[str] = []
        try:
            followers = item.get("followers")
            if isinstance(followers, int) and followers > 0:
                parts.append(f"{short_number(followers)} followers")
            posts = item.get("totalPosts", item.get("posts"))
            if isinstance(posts, int) and posts > 0:
                parts.append(f"{short_number(posts)} posts")
            recent = item.get("recentPosts")
            if isinstance(recent, int) and recent > 0 and not parts:
                parts.append(f"{short_number(recent)} recent posts")
        except Exception:  # noqa: BLE001
            pass
        return " · ".join(parts)

    def _on_result_activated(self, _listbox, row) -> None:
        try:
            handler = getattr(row, "handler", None)
            if callable(handler):
                handler()
        except Exception:  # noqa: BLE001
            pass

    # -- navigation -------------------------------------------------------- #
    def _open_blog(self, name: str) -> None:
        if name:
            self.ctx.publish("open-blog", name)

    def _open_tag(self, tag: str) -> None:
        tag = str(tag or "").lstrip("#")
        if tag:
            self.ctx.publish("open-tag", tag)

    def _open_community(self, url: str) -> None:
        if url:
            self.ctx.open_url(url)
