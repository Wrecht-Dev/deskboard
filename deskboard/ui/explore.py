"""Explore: trending tag chips above the recommended feed.

Owner: shell.  The paginated post list is deliberately a :class:`FeedPage`
over ``FeedSource("explore")`` — pagination, buffering and error handling live
in exactly one place.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..api.sources import FeedSource
from .feed import FeedPage


class ExplorePage(Gtk.Box):
    __gtype_name__ = "DeskboardExplorePage"

    def __init__(self, ctx) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.on_back = True
        self._generation = 0
        self._chips_visible = False

        self.chips_box = Gtk.FlowBox()
        self.chips_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.chips_box.set_column_spacing(8)
        self.chips_box.set_row_spacing(6)
        self.chips_box.set_max_children_per_line(20)
        self.chips_box.set_homogeneous(False)
        self.chips_box.set_valign(Gtk.Align.START)
        self.chips_box.set_margin_start(14)
        self.chips_box.set_margin_end(14)
        self.chips_box.set_margin_top(8)
        self.chips_box.set_margin_bottom(8)
        self.chips_box.set_visible(False)
        self.append(self.chips_box)

        self.feed = FeedPage(
            self.ctx, FeedSource("explore", title="Explore", subtitle="Recommended")
        )
        self.feed.set_vexpand(True)
        self.append(self.feed)

        try:
            self.feed.load_first()
        except Exception:  # noqa: BLE001
            pass
        self._load_trending()

    # -- public API -------------------------------------------------------- #
    def refresh(self) -> None:
        try:
            self.feed.refresh()
        except Exception:  # noqa: BLE001
            pass
        self._load_trending()

    def focus_posts(self) -> None:
        self.feed.focus_list()

    def cleanup(self) -> None:
        self._generation += 1
        try:
            self.feed.cleanup()
        except Exception:  # noqa: BLE001
            pass

    # -- trending chips ---------------------------------------------------- #
    def _load_trending(self) -> None:
        self._generation += 1
        generation = self._generation
        client = self.ctx.client
        getter = getattr(client, "get_trending_tags", None)
        if not callable(getter):
            return

        def work() -> None:
            tags: list[str] = []
            try:
                raw = getter(limit=20) or []
                for item in raw:
                    name = ""
                    if isinstance(item, str):
                        name = item
                    elif isinstance(item, dict):
                        name = str(item.get("name") or item.get("tag") or "")
                    name = name.strip().lstrip("#")
                    if name:
                        tags.append(name)
            except Exception:  # noqa: BLE001 - chips are optional
                tags = []
            try:
                GLib.idle_add(self._apply_trending, generation, tags)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="explore-trending").start()
        except Exception:  # noqa: BLE001
            pass

    def _apply_trending(self, generation: int, tags: list[str]) -> bool:
        try:
            if generation != self._generation:
                return False
            child = self.chips_box.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                self.chips_box.remove(child)
                child = following
            seen = set()
            added = 0
            for tag in tags:
                key = tag.lower()
                if key in seen:
                    continue
                seen.add(key)
                button = Gtk.Button(label=f"#{tag}")
                button.add_css_class("pill")
                button.add_css_class("tag-chip")
                button.set_valign(Gtk.Align.CENTER)
                button.connect("clicked", lambda _b, value=tag: self._open_tag(value))
                self.chips_box.append(button)
                added += 1
                if added >= 20:
                    break
            visible = added > 0
            self.chips_box.set_visible(visible)
            self._chips_visible = visible
        except Exception:  # noqa: BLE001
            pass
        return False

    def _open_tag(self, tag: str) -> None:
        try:
            self.ctx.publish("open-tag", tag)
        except Exception:  # noqa: BLE001
            pass
