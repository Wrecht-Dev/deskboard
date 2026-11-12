"""The thing you actually look at: one post card.

Cards are built by a recycled ``Gtk.ListView``; ``__init__`` does no I/O and
every async callback is guarded so a texture that arrives after the row was
recycled can never raise.
"""
from __future__ import annotations

import math
import threading
import weakref
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, GLib, Gtk, Pango

from ..util import humanize_time, short_number
from .blocks import render_blocks
from .html import escape_markup, plain_text

_REBLOG_ICON = "media-playlist-repeat-symbolic"
_LIKE_ICON = "emote-love-symbolic"
_SHARE_ICON = "edit-copy-symbolic"

# Post-level write state that should survive card recycling (mute/pin/subscribe
# flags are not exposed by the model, and a rebuilt card must not forget a
# toggle the user just made).
_ACTION_STATES: dict[tuple[str, str, str], bool] = {}

# Followed tags are loaded once per session, on a worker thread, the first time
# a card with tags is built.  Widgets subscribe weakly so recycling cannot leak.
_FOLLOWED_TAGS: set[str] = set()
_FOLLOWED_TAGS_KNOWN = False
_FOLLOWED_TAGS_LOADING = False
_FOLLOW_WATCHERS: list[weakref.ReferenceType] = []

_REPORT_REASONS = (
    ("Spam or unwanted content", "spam"),
    ("Harassment or bullying", "harassment"),
    ("Hate speech", "hate_speech"),
    ("Misinformation", "misinformation"),
    ("Sexual or NSFW content", "nsfw"),
    ("Something else", "other"),
)


def _describe_error(exc: Exception) -> str:
    text = " ".join(str(exc or "").split())
    return text or exc.__class__.__name__


def _normalise_tag(tag: str) -> str:
    return str(tag or "").strip().lstrip("#").strip().lower()


def _raw_flag(post: Any, *names: str, default: bool = False) -> bool:
    raw = getattr(post, "raw", None)
    if isinstance(raw, dict):
        for name in names:
            if name in raw:
                return bool(raw[name])
    for name in names:
        value = getattr(post, name, None)
        if value is not None:
            return bool(value)
    return default


def _ensure_followed_tags(ctx: Any) -> None:
    global _FOLLOWED_TAGS_KNOWN, _FOLLOWED_TAGS_LOADING
    if _FOLLOWED_TAGS_KNOWN or _FOLLOWED_TAGS_LOADING:
        return
    if getattr(ctx, "demo", False):
        return
    client = getattr(ctx, "client", None)
    getter = getattr(client, "get_followed_tags", None)
    if not callable(getter):
        _FOLLOWED_TAGS_KNOWN = True
        return
    _FOLLOWED_TAGS_LOADING = True

    def work() -> None:
        tags = None
        try:
            tags = getter()
        except Exception:  # noqa: BLE001 - "unknown" is fine, toggles stay +
            tags = None
        try:
            GLib.idle_add(_apply_followed_tags, tags)
        except Exception:  # noqa: BLE001
            pass

    try:
        threading.Thread(target=work, daemon=True, name="followed-tags").start()
    except Exception:  # noqa: BLE001 - resource exhaustion: stay unknown
        _FOLLOWED_TAGS_LOADING = False


def _apply_followed_tags(tags: Any) -> bool:
    global _FOLLOWED_TAGS_KNOWN, _FOLLOWED_TAGS_LOADING
    _FOLLOWED_TAGS_LOADING = False
    _FOLLOWED_TAGS_KNOWN = True
    if isinstance(tags, (list, tuple, set)):
        _FOLLOWED_TAGS.clear()
        for tag in tags:
            key = _normalise_tag(str(tag))
            if key:
                _FOLLOWED_TAGS.add(key)
    _notify_follow_watchers()
    return False


def _notify_follow_watchers() -> None:
    alive: list[weakref.ReferenceType] = []
    snapshot = set(_FOLLOWED_TAGS)
    for ref in list(_FOLLOW_WATCHERS):
        callback = ref()
        if callback is None:
            continue
        alive.append(ref)
        try:
            callback(snapshot)
        except Exception:  # noqa: BLE001 - a dead card must not break others
            pass
    _FOLLOW_WATCHERS[:] = alive


def _watch_followed(callback: Callable[[set[str]], None]) -> None:
    _FOLLOW_WATCHERS.append(weakref.ref(callback))
    try:
        callback(set(_FOLLOWED_TAGS))
    except Exception:  # noqa: BLE001
        pass


def _set_followed_tag(key: str, followed: bool) -> None:
    if followed:
        _FOLLOWED_TAGS.add(key)
    else:
        _FOLLOWED_TAGS.discard(key)
    _notify_follow_watchers()


# --------------------------------------------------------------------------- #
# height estimation for bound-but-not-yet-mapped rows
# --------------------------------------------------------------------------- #

_HEIGHT_HINTS: dict[str, int] = {}
_MAX_HEIGHT_HINTS = 240
_ESTIMATE_WIDTH = 700.0


def _post_key(post: Any) -> str:
    value = getattr(post, "id", "") or ""
    return str(value) if value else str(id(post))


def _remember_height(post: Any, height: int) -> None:
    if height <= 0:
        return
    key = _post_key(post)
    _HEIGHT_HINTS.pop(key, None)
    _HEIGHT_HINTS[key] = int(height)
    while len(_HEIGHT_HINTS) > _MAX_HEIGHT_HINTS:
        _HEIGHT_HINTS.pop(next(iter(_HEIGHT_HINTS)))


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _estimate_text_height(block: Any) -> float:
    text = getattr(block, "text", "") or ""
    if not text.strip():
        return 0.0
    chars_per_line = 62.0
    lines = max(1, int(math.ceil(len(text) / chars_per_line)))
    line_height = 22.0
    subtype = str(getattr(block, "subtype", "") or "")
    extra = 0.0
    if subtype == "heading1":
        line_height = 34.0
        extra = 6.0
    elif subtype == "heading2":
        line_height = 28.0
        extra = 4.0
    elif subtype == "quote":
        extra = 6.0
    return lines * line_height + extra + 8.0


def _estimate_media_height(block: Any) -> float:
    medias = list(getattr(block, "media", None) or [])
    kind = str(getattr(block, "type", "") or "")
    if kind == "audio":
        return 112.0
    if kind == "video":
        return 390.0
    if not medias:
        return 240.0 if kind == "image" else 0.0
    aspect = 1.0
    for media in medias:
        try:
            if media.width > 0 and media.height > 0:
                aspect = media.width / media.height
                break
        except Exception:  # noqa: BLE001
            pass
    aspect = _clampf(aspect, 0.2, 6.0)
    count = len(medias)
    if count <= 1:
        return _clampf(_ESTIMATE_WIDTH / aspect, 120.0, 640.0)
    if count == 2:
        return _clampf((_ESTIMATE_WIDTH - 6.0) / 2.0 / aspect, 100.0, 560.0)
    if count == 3:
        return _clampf(_ESTIMATE_WIDTH / 2.0 / aspect + 6.0, 160.0, 760.0)
    return _clampf((_ESTIMATE_WIDTH - 6.0) / 2.0 / aspect + 6.0, 160.0, 620.0)


def _estimate_block_height(block: Any) -> float:
    kind = str(getattr(block, "type", "") or "text")
    if kind == "text":
        return _estimate_text_height(block)
    if kind in ("image", "video", "audio"):
        return _estimate_media_height(block) + 14.0
    if kind == "link":
        return 104.0
    if kind == "poll":
        answers = len(getattr(block, "answers", None) or [])
        return 74.0 + answers * 42.0
    if kind == "answer":
        return 168.0
    return _estimate_text_height(block)


def _estimate_blocks_height(blocks: Any) -> float:
    total = 0.0
    for block in blocks or []:
        try:
            total += _estimate_block_height(block)
        except Exception:  # noqa: BLE001
            total += 30.0
    return total


def _estimate_card_height(post: Any) -> int:
    key = _post_key(post)
    hint = _HEIGHT_HINTS.get(key)
    if hint:
        return hint
    height = 116.0  # header + card padding/margins + footer base
    trail_for_display = getattr(post, "trail_for_display", None)
    if callable(trail_for_display):
        try:
            trail, hidden = trail_for_display(3)
        except Exception:  # noqa: BLE001
            trail, hidden = list(getattr(post, "trail", None) or []), 0
    else:
        trail, hidden = list(getattr(post, "trail", None) or []), 0
    if hidden:
        height += 24.0
    for item in trail:
        height += 26.0 + _estimate_blocks_height(getattr(item, "content", None) or [])
    height += _estimate_blocks_height(getattr(post, "content", None) or [])
    if getattr(post, "tags", None):
        height += 26.0
    height += 20.0  # separator + footer spacing
    # Estimates run slightly high in practice; bias down so a row that builds
    # on scroll shrinks rather than jumps open.
    return int(_clampf(height * 0.87, 120.0, 2400.0))


# --------------------------------------------------------------------------- #
# tiny builders
# --------------------------------------------------------------------------- #

def _label(markup: str = "", *, css: tuple[str, ...] = (), wrap: bool = False,
           selectable: bool = False) -> Gtk.Label:
    label = Gtk.Label()
    label.set_use_markup(True)
    label.set_wrap(wrap)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_xalign(0.0)
    if selectable:
        label.set_selectable(True)
    if markup:
        label.set_markup(markup)
    for name in css:
        label.add_css_class(name)
    return label


def _box(*, orientation: Gtk.Orientation = Gtk.Orientation.VERTICAL,
         spacing: int = 0) -> Gtk.Box:
    return Gtk.Box(orientation=orientation, spacing=spacing)


def _dim(label: Gtk.Label) -> None:
    label.add_css_class("caption")
    label.add_css_class("dim-label")


# --------------------------------------------------------------------------- #
# PostCard
# --------------------------------------------------------------------------- #

class PostCard(Gtk.Box):
    """A calm, information-dense rendering of a single Tumblr post."""

    def __init__(self, post: Any, ctx: Any) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.post = post
        self.ctx = ctx
        self.add_css_class("post-card")
        self.set_hexpand(True)

        # Avatars are queued here and fetched only once this card is mapped.
        # A Gtk.ListView binds far more rows than it maps, so eager fetching
        # here would queue hundreds of images for rows nobody ever sees.
        self._deferred_images: dict[tuple[str, int, int], list[Any]] = {}
        self._requested_images: set[tuple[str, int]] = set()
        self._menu_popover: Gtk.Popover | None = None
        self._built = False
        self._build_scheduled = False
        self._inner: Gtk.Box | None = None
        self._alive = True
        self._pending_actions: set[str] = set()
        self._dialog: Adw.AlertDialog | None = None
        self._tag_toggles: dict[str, Gtk.ToggleButton] = {}
        self._tag_busy: set[str] = set()
        self._tag_syncing = False
        self._follow_watch: Callable[[set[str]], None] | None = None
        self._skeleton: Gtk.Box | None = _box(spacing=0)
        self._skeleton.set_hexpand(True)
        self._skeleton.set_size_request(-1, _estimate_card_height(post))
        self.append(self._skeleton)
        self.connect("map", self._on_map)
        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args) -> None:
        self._alive = False
        self._follow_watch = None

    # -- lazy construction -------------------------------------------------- #
    def _on_map(self, *_args) -> None:
        if self._built:
            self._pump_images()
            return
        if self._build_scheduled:
            return
        # Gtk.ListView maps a row briefly while binding it even when the row is
        # outside the viewport.  Wait one idle turn and build only if the row is
        # still mapped; otherwise the skeleton stays until the user scrolls to
        # it and it is mapped for real.
        self._build_scheduled = True
        GLib.idle_add(self._build_if_visible)

    def _build_if_visible(self) -> bool:
        self._build_scheduled = False
        if self._built:
            return False
        try:
            if not self.get_mapped():
                return False
        except Exception:  # noqa: BLE001
            pass
        self._built = True
        self._build_now()
        self._pump_images()
        return False

    def _build_now(self) -> None:
        """Build the real card once the row is mapped."""
        try:
            if self._skeleton is not None:
                self.remove(self._skeleton)
                self._skeleton = None
        except Exception:  # noqa: BLE001
            pass
        inner = _box(spacing=0)
        inner.set_hexpand(True)
        self._inner = inner
        self.append(inner)
        try:
            self._build_header(inner)
            self._build_content(inner)
            self._build_tags(inner)
            self._build_footer(inner)
        except Exception:  # noqa: BLE001 - never let one post break the feed
            try:
                self.remove(inner)
            except Exception:  # noqa: BLE001
                pass
            fallback = Gtk.Label(label="This post could not be displayed.")
            fallback.add_css_class("dim-label")
            fallback.set_margin_top(12)
            fallback.set_margin_bottom(12)
            fallback.set_margin_start(16)
            fallback.set_margin_end(16)
            self.append(fallback)
            self._inner = None
            return
        GLib.idle_add(self._remember_own_height)

    def _remember_own_height(self) -> bool:
        try:
            _remember_height(self.post, self.get_height())
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- header ------------------------------------------------------------- #
    def _build_header(self, parent: Gtk.Box) -> None:
        post = self.post
        blog_name = (getattr(post, "blog_name", "") or "").strip()
        display_name = (getattr(post, "display_name", "") or blog_name or "Untitled").strip()
        header = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_bottom(6)

        author = Gtk.Button()
        author.add_css_class("flat")
        author.add_css_class("post-author")
        author.set_halign(Gtk.Align.FILL)
        author.set_hexpand(True)
        author.connect("clicked", self._on_open_blog)

        row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        avatar = Adw.Avatar(size=40, text=display_name or "?", show_initials=True)
        avatar.set_valign(Gtk.Align.START)
        self._defer_avatar(avatar, getattr(post, "blog_avatar", "") or "", 40)
        row.append(avatar)

        text = _box(spacing=1)
        text.set_hexpand(True)
        name_row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        name = _label(escape_markup(display_name), css=("heading",))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(32)
        name.set_hexpand(False)
        name_row.set_halign(Gtk.Align.START)
        name_row.append(name)

        if getattr(post, "is_reblog", False):
            from_name = (getattr(post, "reblogged_from_name", "") or "").strip()
            if not from_name:
                trail = getattr(post, "trail", None) or []
                if trail:
                    from_name = (getattr(trail[-1], "blog_name", "") or "").strip()
            if from_name:
                handle_name = from_name if from_name.startswith("@") else "@" + from_name
                chip_text = f"reblogged from {handle_name}"
            else:
                chip_text = "reblogged"
            chip = _label(escape_markup(chip_text), css=("caption", "pill", "dim-label"))
            chip.set_ellipsize(Pango.EllipsizeMode.END)
            chip.set_max_width_chars(34)
            chip.set_valign(Gtk.Align.CENTER)
            name_row.append(chip)
        text.append(name_row)

        meta_parts = []
        handle = (getattr(post, "handle", "") or
                  (f"{blog_name}.tumblr.com" if blog_name else ""))
        if handle:
            meta_parts.append(escape_markup(handle))
        when = humanize_time(getattr(post, "timestamp", 0))
        if when:
            meta_parts.append(escape_markup(when))
        if getattr(post, "is_nsfw", False):
            meta_parts.append('<span weight="bold">NSFW</span>')
        if meta_parts:
            meta = _label("  ·  ".join(meta_parts))
            _dim(meta)
            meta.set_ellipsize(Pango.EllipsizeMode.END)
            text.append(meta)

        row.append(text)
        author.set_child(row)
        header.append(author)

        menu = Gtk.Button()
        menu.set_icon_name("view-more-symbolic")
        menu.add_css_class("flat")
        menu.set_tooltip_text("More actions")
        menu.set_valign(Gtk.Align.START)
        menu.connect("clicked", self._show_menu)
        header.append(menu)

        parent.append(header)

    # -- deferred images ---------------------------------------------------- #
    def _defer_avatar(self, avatar: Adw.Avatar, url: str, size: int) -> None:
        if not url:
            return
        width = height = size * 2

        def apply(texture: Any) -> None:
            try:
                if texture is not None:
                    avatar.set_custom_image(texture)
            except Exception:  # noqa: BLE001 - the row may be gone already
                pass

        self._deferred_images.setdefault((url, width, height), []).append(apply)

    def _pump_images(self) -> None:
        if not self._deferred_images:
            return
        for (url, width, height), callbacks in list(self._deferred_images.items()):
            try:
                texture = self.ctx.images.cached(url, width)
            except Exception:  # noqa: BLE001
                texture = None
            if texture is not None:
                for callback in callbacks:
                    callback(texture)
                continue
            if (url, width) in self._requested_images:
                continue
            self._requested_images.add((url, width))

            def deliver(texture: Any, callbacks=callbacks) -> None:
                if texture is None:
                    return
                for callback in callbacks:
                    callback(texture)

            try:
                self.ctx.images.load(url, width, height, deliver)
            except Exception:  # noqa: BLE001 - avatars are optional
                pass

    def _show_menu(self, button: Gtk.Button) -> None:
        # Rebuild on every open: mute/pin/subscribe labels reflect the current
        # optimistic state and a recycled card must not show a stale menu.
        previous = self._menu_popover
        if previous is not None:
            try:
                previous.popdown()
                previous.unparent()
            except Exception:  # noqa: BLE001
                pass
        popover = self._build_menu()
        self._menu_popover = popover
        try:
            popover.set_parent(button)
            popover.popup()
        except Exception:  # noqa: BLE001
            self._menu_popover = None

    def _build_menu(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = _box(spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        def separator() -> None:
            line = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            line.set_margin_top(4)
            line.set_margin_bottom(4)
            box.append(line)

        def add(icon: str, text: str, callback, destructive: bool = False) -> None:
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            content = Adw.ButtonContent(icon_name=icon, label=text)
            content.set_halign(Gtk.Align.START)
            button.set_child(content)
            if destructive:
                button.add_css_class("destructive-action")

            def activate(_button: Gtk.Button) -> None:
                popover.popdown()
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass

            button.connect("clicked", activate)
            box.append(button)

        def caption(text: str) -> None:
            label = Gtk.Label(label=text)
            label.set_xalign(0.0)
            label.set_wrap(True)
            label.set_max_width_chars(42)
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            label.set_margin_top(4)
            label.set_margin_bottom(2)
            label.set_margin_start(8)
            label.set_margin_end(8)
            box.append(label)

        add("web-browser-symbolic", "Open post", self._open_post)
        add("user-home-symbolic", "Open blog", self._open_blog)
        add("edit-copy-symbolic", "Copy link", self._copy_link)
        add("edit-copy-symbolic", "Copy text", self._copy_text)

        writable = self._can_write()
        if not writable:
            separator()
            if getattr(self.ctx, "demo", False):
                caption("Demo mode — actions are disabled.")
            else:
                caption("Sign in to mute, report or manage this post.")
            popover.set_child(box)
            return popover

        client = self._client()
        own = self._own_post()
        target_blog = (getattr(self.post, "blog_name", "") or "").strip()
        separator()

        if callable(getattr(client, "mute_post", None)) and callable(
                getattr(client, "unmute_post", None)):
            muted = self._action_state("mute", ("isMuted", "muted"))
            add("view-conceal-symbolic", "Unmute post" if muted else "Mute post",
                self._toggle_mute)

        if (not own and target_blog and self._acting_blog()
                and callable(getattr(client, "block_blog", None))):
            add("action-unavailable-symbolic", f"Block @{target_blog}",
                self._confirm_block)

        if (not own and target_blog
                and callable(getattr(client, "report", None))):
            add("dialog-warning-symbolic", "Report post", self._confirm_report)

        if own:
            if callable(getattr(client, "pin_post", None)) and callable(
                    getattr(client, "unpin_post", None)):
                pinned = self._action_state("pin", ("isPinned", "pinned"))
                add("view-pin-symbolic", "Unpin post" if pinned else "Pin post",
                    self._toggle_pin)
            can_delete = _raw_flag(self.post, "canDelete", default=True)
            if can_delete and callable(getattr(client, "delete_post", None)):
                add("user-trash-symbolic", "Delete post", self._confirm_delete,
                    destructive=True)

        if callable(getattr(client, "subscribe_post", None)) and callable(
                getattr(client, "unsubscribe_post", None)):
            subscribed = self._action_state(
                "subscribe", ("isSubscribed", "subscribed", "isSubscribedToPost"))
            add("mail-send-receive-symbolic",
                "Unsubscribe from notifications" if subscribed
                else "Subscribe to notifications",
                self._toggle_subscribe)

        popover.set_child(box)
        return popover

    # -- trail + content ---------------------------------------------------- #
    def _build_content(self, parent: Gtk.Box) -> None:
        post = self.post
        trail_for_display = getattr(post, "trail_for_display", None)
        if callable(trail_for_display):
            trail, hidden = trail_for_display(3)
        else:
            trail, hidden = list(getattr(post, "trail", None) or []), 0
        content = list(getattr(post, "content", None) or [])

        if trail:
            trail_box = _box(spacing=0)
            trail_box.add_css_class("post-trail")
            if hidden:
                earlier = _label(f"{hidden} earlier reblog{'s' if hidden != 1 else ''}",
                                css=("caption", "dim-label"))
                earlier.set_margin_start(10)
                earlier.set_margin_bottom(4)
                trail_box.append(earlier)
            for item in trail:
                trail_box.append(self._build_trail_step(item))
            trail_box.set_margin_bottom(4 if not content else 0)
            parent.append(trail_box)

        body = _box(spacing=0)
        body.add_css_class("post-body")
        if content:
            render_blocks(body, content, self.ctx, post)
        elif not trail:
            # Extremely rare: no blocks at all. Use the summary rather than
            # leaving an empty card that looks like a rendering bug.
            summary = (getattr(post, "summary", "") or "").strip()
            if summary:
                label = _label(escape_markup(summary), wrap=True, selectable=True)
                label.add_css_class("body")
                label.set_margin_bottom(8)
                body.append(label)
            else:
                empty = _label("This post has no content.", css=("dim-label",))
                empty.set_margin_bottom(8)
                body.append(empty)
        parent.append(body)

    def _build_trail_step(self, item: Any) -> Gtk.Widget:
        outer = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.add_css_class("trail-step")
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)

        content = _box(spacing=0)
        content.add_css_class("trail-content")
        content.set_hexpand(True)
        content.set_margin_start(2)

        author_row = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        author_row.set_margin_bottom(4)
        author_row.set_margin_start(2)

        avatar_url = getattr(item, "avatar", "") or ""
        if avatar_url and bool(_pref(self.ctx, "show_avatars", True)):
            avatar = Adw.Avatar(size=20, text=item.display_name or item.blog_name or "?",
                                show_initials=True)
            avatar.set_valign(Gtk.Align.CENTER)
            self._defer_avatar(avatar, avatar_url, 20)
            author_row.append(avatar)

        name = _label(escape_markup(item.display_name or item.blog_name or ""),
                      css=("caption-heading",))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(24)
        author_row.append(name)
        handle = f"{item.blog_name}.tumblr.com" if getattr(item, "blog_name", "") else ""
        if handle:
            author_row.append(_label("·", wrap=False, css=("caption", "dim-label")))
            handle_label = _label(escape_markup(handle), css=("caption", "dim-label"))
            handle_label.set_ellipsize(Pango.EllipsizeMode.END)
            handle_label.set_max_width_chars(34)
            handle_label.set_hexpand(True)
            author_row.append(handle_label)
        content.append(author_row)

        render_blocks(content, item.content, self.ctx, self.post)
        outer.append(content)
        return outer

    # -- tags --------------------------------------------------------------- #
    def _build_tags(self, parent: Gtk.Box) -> None:
        tags: list[str] = []
        for tag in getattr(self.post, "tags", None) or []:
            text = str(tag).strip()
            if not text:
                continue
            key = _normalise_tag(text)
            if key and key not in {_normalise_tag(existing) for existing in tags}:
                tags.append(text)
        if not tags:
            return

        wrap = Adw.WrapBox()
        wrap.set_child_spacing(6)
        wrap.set_line_spacing(4)
        wrap.set_margin_top(6)
        wrap.set_margin_bottom(2)
        limit = 12
        can_follow = self._can_write() and callable(
            getattr(self._client(), "follow_tag", None)) and callable(
            getattr(self._client(), "unfollow_tag", None))
        if can_follow:
            _ensure_followed_tags(self.ctx)

        for tag in tags[:limit]:
            wrap.append(self._build_tag_chip(tag, can_follow))
        if len(tags) > limit:
            more = _label("…", wrap=False, css=("caption", "dim-label"))
            more.set_valign(Gtk.Align.CENTER)
            wrap.append(more)

        if can_follow:
            self._follow_watch = self._sync_tag_toggles
            _watch_followed(self._follow_watch)
        parent.append(wrap)

    def _build_tag_chip(self, tag: str, can_follow: bool) -> Gtk.Widget:
        chip = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        chip.add_css_class("post-tag")
        text = tag if tag.startswith("#") else "#" + tag
        button = Gtk.Button(label=text)
        button.add_css_class("flat")
        button.add_css_class("caption")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(f"Open {text}")
        button.connect("clicked", lambda _button, value=text: self._open_tag(value))
        chip.append(button)

        if not can_follow:
            return chip
        key = _normalise_tag(tag)
        toggle = Gtk.ToggleButton()
        toggle.add_css_class("flat")
        toggle.add_css_class("post-tag-follow")
        toggle.set_valign(Gtk.Align.CENTER)
        toggle.set_child(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        toggle.set_tooltip_text(f"Follow {text}")
        toggle.connect("toggled", self._on_tag_toggled, tag, key)
        self._tag_toggles[key] = toggle
        chip.append(toggle)
        self._sync_tag_toggle(toggle, key in _FOLLOWED_TAGS, text)
        return chip

    def _open_tag(self, tag: str) -> None:
        try:
            self.ctx.publish("open-tag", tag.lstrip("#"))
        except Exception:  # noqa: BLE001
            pass

    def _sync_tag_toggle(self, toggle: Gtk.ToggleButton, followed: bool,
                         text: str | None = None) -> None:
        image = toggle.get_child()
        if isinstance(image, Gtk.Image):
            try:
                image.set_from_icon_name(
                    "object-select-symbolic" if followed else "list-add-symbolic")
            except Exception:  # noqa: BLE001
                pass
        label = text or "#"
        try:
            toggle.set_tooltip_text(
                f"Unfollow {label}" if followed else f"Follow {label}")
        except Exception:  # noqa: BLE001
            pass

    def _sync_tag_toggles(self, tags: set[str]) -> None:
        self._tag_syncing = True
        try:
            for key, toggle in list(self._tag_toggles.items()):
                followed = key in tags
                try:
                    toggle.set_active(followed)
                except Exception:  # noqa: BLE001
                    continue
                self._sync_tag_toggle(toggle, followed, "#" + key)
        finally:
            self._tag_syncing = False

    def _on_tag_toggled(self, toggle: Gtk.ToggleButton, tag: str, key: str) -> None:
        if self._tag_syncing or key in self._tag_busy:
            return
        target = bool(toggle.get_active())
        self._tag_busy.add(key)
        _set_followed_tag(key, target)
        client = self._client()

        def work() -> None:
            error = ""
            try:
                if target:
                    client.follow_tag(tag.lstrip("#"))
                else:
                    client.unfollow_tag(tag.lstrip("#"))
            except Exception as exc:  # noqa: BLE001 - surfaced as a toast
                error = _describe_error(exc)

            def finish() -> bool:
                self._tag_busy.discard(key)
                if not self._alive:
                    return False
                if error:
                    _set_followed_tag(key, not target)
                    self._toast(
                        f"Couldn't {'follow' if target else 'unfollow'} "
                        f"#{key}: {error}")
                else:
                    self._toast(f"Following #{tag.lstrip('#')}" if target
                                else f"Unfollowed #{tag.lstrip('#')}")
                return False

            try:
                GLib.idle_add(finish)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True,
                             name="tag-follow").start()
        except Exception as exc:  # noqa: BLE001
            self._tag_busy.discard(key)
            _set_followed_tag(key, not target)
            self._toast(f"Couldn't follow tag: {_describe_error(exc)}")

    # -- footer ------------------------------------------------------------- #
    def _build_footer(self, parent: Gtk.Box) -> None:
        post = self.post
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("dim-label")
        separator.set_margin_top(6)
        separator.set_margin_bottom(4)
        parent.append(separator)

        footer = _box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)

        self._like_button = Gtk.Button()
        self._like_button.add_css_class("flat")
        self._like_button.set_valign(Gtk.Align.CENTER)
        self._like_content = Adw.ButtonContent()
        self._like_button.set_child(self._like_content)
        self._like_button.connect("clicked", self._on_like)
        footer.append(self._like_button)

        self._reblog_button = self._action_button(
            _REBLOG_ICON, short_number(post.reblog_count) if post.reblog_count else "",
            "Reblog", self._on_reblog)
        footer.append(self._reblog_button)

        share = self._action_button(_SHARE_ICON, "", "Copy link", self._copy_link)
        footer.append(share)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        footer.append(spacer)

        notes = short_number(post.note_count)
        notes_label = Gtk.Label(label=f"{notes} notes" if post.note_count != 1 else "1 note")
        _dim(notes_label)
        notes_label.set_valign(Gtk.Align.CENTER)
        notes_label.set_xalign(1.0)
        notes_label.set_tooltip_text("Notes")
        footer.append(notes_label)

        parent.append(footer)
        self._sync_like()

    def _action_button(self, icon: str, label: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        button.set_child(Adw.ButtonContent(icon_name=icon, label=label))
        button.connect("clicked", lambda _button: callback())
        return button

    def _sync_like(self) -> None:
        try:
            liked = bool(self.post.is_liked)
            self._like_content.set_icon_name(_LIKE_ICON)
            count = int(getattr(self.post, "like_count", 0) or 0)
            self._like_content.set_label(short_number(count) if count else "")
            if liked:
                self._like_button.add_css_class("accent")
                self._like_button.remove_css_class("dim-label")
            else:
                self._like_button.remove_css_class("accent")
                self._like_button.add_css_class("dim-label")
            self._like_button.set_tooltip_text("Unlike" if liked else "Like")
        except Exception:  # noqa: BLE001
            pass

    # -- ownership / capability checks ------------------------------------- #
    def _client(self) -> Any:
        return getattr(self.ctx, "client", None)

    def _can_write(self) -> bool:
        if getattr(self.ctx, "demo", False):
            return False
        try:
            return bool(getattr(self.ctx, "is_signed_in", False))
        except Exception:  # noqa: BLE001
            return False

    def _own_blogs(self) -> set[str]:
        names: set[str] = set()
        try:
            primary = str(getattr(self.ctx, "user_blog", "") or "").strip()
            if primary:
                names.add(primary.lower())
            for value in getattr(self.ctx, "user_blogs", None) or []:
                name = str(value or "").strip()
                if name:
                    names.add(name.lower())
        except Exception:  # noqa: BLE001
            pass
        try:
            prefs = getattr(self.ctx, "prefs", None)
            posting = str(prefs.get("posting_blog", "") or "").strip() if prefs else ""
            if posting:
                names.add(posting.lower())
        except Exception:  # noqa: BLE001
            pass
        return names

    def _own_post(self) -> bool:
        blog = (getattr(self.post, "blog_name", "") or "").strip().lower()
        return bool(blog) and blog in self._own_blogs()

    def _acting_blog(self) -> str:
        primary = str(getattr(self.ctx, "user_blog", "") or "").strip()
        if primary:
            return primary
        for value in getattr(self.ctx, "user_blogs", None) or []:
            name = str(value or "").strip()
            if name:
                return name
        try:
            prefs = getattr(self.ctx, "prefs", None)
            posting = str(prefs.get("posting_blog", "") or "").strip() if prefs else ""
            if posting:
                return posting
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _action_key(self, kind: str) -> tuple[str, str, str]:
        return (kind, str(getattr(self.post, "blog_name", "") or ""),
                str(getattr(self.post, "id", "") or ""))

    def _action_state(self, kind: str, raw_names: tuple[str, ...] = (),
                      default: bool = False) -> bool:
        key = self._action_key(kind)
        if key in _ACTION_STATES:
            return _ACTION_STATES[key]
        return _raw_flag(self.post, *raw_names, default=default)

    def _set_action_state(self, kind: str, value: bool) -> None:
        _ACTION_STATES[self._action_key(kind)] = bool(value)

    # -- async write plumbing ---------------------------------------------- #
    def _run_action(self, action: str, work: Callable[[], None],
                    done: Callable[[str], None]) -> None:
        if action in self._pending_actions:
            return
        self._pending_actions.add(action)

        def worker() -> None:
            error = ""
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - surfaced as a toast
                error = _describe_error(exc)

            def finish() -> bool:
                self._pending_actions.discard(action)
                if not self._alive:
                    return False
                try:
                    done(error)
                except Exception:  # noqa: BLE001
                    pass
                return False

            try:
                GLib.idle_add(finish)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=worker, daemon=True,
                             name=f"postcard-{action}").start()
        except Exception as exc:  # noqa: BLE001
            self._pending_actions.discard(action)
            done(_describe_error(exc))

    def _toast(self, message: str) -> None:
        try:
            self.ctx.toast(message)
        except Exception:  # noqa: BLE001
            pass

    # -- post write actions ------------------------------------------------- #
    def _toggle_mute(self) -> None:
        client = self._client()
        post = self.post
        target = not self._action_state("mute", ("isMuted", "muted"))
        self._set_action_state("mute", target)
        call = (client.mute_post if target else client.unmute_post)

        def finish(error: str) -> None:
            if error:
                self._set_action_state("mute", not target)
                verb = "mute" if target else "unmute"
                self._toast(f"Couldn't {verb} post: {error}")
            else:
                self._toast("Post muted" if target else "Post unmuted")

        self._run_action("mute", lambda: call(post), finish)

    def _toggle_pin(self) -> None:
        client = self._client()
        post = self.post
        target = not self._action_state("pin", ("isPinned", "pinned"))
        self._set_action_state("pin", target)
        call = (client.pin_post if target else client.unpin_post)

        def finish(error: str) -> None:
            if error:
                self._set_action_state("pin", not target)
                self._toast(f"Couldn't {'pin' if target else 'unpin'} post: {error}")
            else:
                self._toast("Post pinned" if target else "Post unpinned")

        self._run_action("pin", lambda: call(post), finish)

    def _toggle_subscribe(self) -> None:
        client = self._client()
        post = self.post
        raw_names = ("isSubscribed", "subscribed", "isSubscribedToPost")
        target = not self._action_state("subscribe", raw_names)
        self._set_action_state("subscribe", target)
        call = (client.subscribe_post if target else client.unsubscribe_post)

        def finish(error: str) -> None:
            if error:
                self._set_action_state("subscribe", not target)
                self._toast(f"Couldn't update notifications: {error}")
            else:
                self._toast("Post notifications on" if target
                            else "Post notifications off")

        self._run_action("subscribe", lambda: call(post), finish)

    def _confirm_block(self) -> None:
        blog = (getattr(self.post, "blog_name", "") or "").strip()
        acting = self._acting_blog()
        if not blog or not acting:
            return
        if self._dialog is not None:
            return
        try:
            dialog = Adw.AlertDialog()
            dialog.set_heading(f"Block @{blog}?")
            dialog.set_body("You won't see this blog again. You can unblock it later.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("block", "Block")
            dialog.set_response_appearance("block", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
                self._dialog = None
                if response == "block":
                    self._block_blog(acting, blog)

            dialog.connect("response", on_response)
            self._dialog = dialog
            self._present_dialog(dialog)
        except Exception as exc:  # noqa: BLE001
            self._dialog = None
            self._toast(f"Couldn't open the block dialog: {_describe_error(exc)}")

    def _block_blog(self, acting_blog: str, target_blog: str) -> None:
        client = self._client()
        post_id = str(getattr(self.post, "id", "") or "")
        self._run_action(
            "block",
            lambda: client.block_blog(acting_blog, target_blog, post_id),
            lambda error: self._toast(
                f"Couldn't block @{target_blog}: {error}" if error
                else f"Blocked @{target_blog}"),
        )

    def _confirm_report(self) -> None:
        if self._dialog is not None:
            return
        blog = (getattr(self.post, "blog_name", "") or "").strip()
        if not blog:
            return
        try:
            dialog = Adw.AlertDialog()
            dialog.set_heading("Report this post?")
            dialog.set_body(
                f"Report @{blog}'s post to Tumblr? The report is sent with the "
                "reason you choose below."
            )
            labels = [label for label, _value in _REPORT_REASONS]
            dropdown = Gtk.DropDown.new_from_strings(labels)
            dropdown.set_selected(0)
            selector = _box(spacing=6)
            selector.set_margin_top(6)
            reason_label = Gtk.Label(label="Reason")
            reason_label.set_xalign(0.0)
            reason_label.add_css_class("caption")
            reason_label.add_css_class("dim-label")
            selector.append(reason_label)
            selector.append(dropdown)
            dialog.set_extra_child(selector)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("report", "Report")
            dialog.set_response_appearance("report", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
                self._dialog = None
                if response != "report":
                    return
                index = 0
                try:
                    index = int(dropdown.get_selected())
                except Exception:  # noqa: BLE001
                    index = 0
                index = max(0, min(index, len(_REPORT_REASONS) - 1))
                self._report_post(blog, _REPORT_REASONS[index][1])

            dialog.connect("response", on_response)
            self._dialog = dialog
            self._present_dialog(dialog)
        except Exception as exc:  # noqa: BLE001
            self._dialog = None
            self._toast(f"Couldn't open the report dialog: {_describe_error(exc)}")

    def _report_post(self, blog_name: str, reason: str) -> None:
        client = self._client()
        post_id = str(getattr(self.post, "id", "") or "")
        if not post_id or not blog_name:
            return
        self._run_action(
            "report",
            lambda: client.report(post_id=post_id, blog_name=blog_name, reason=reason),
            lambda error: self._toast(
                f"Couldn't report post: {error}" if error else "Post reported"),
        )

    def _confirm_delete(self) -> None:
        if self._dialog is not None:
            return
        try:
            dialog = Adw.AlertDialog()
            dialog.set_heading("Delete this post?")
            dialog.set_body("This deletes the post permanently. This can't be undone.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("delete", "Delete")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
                self._dialog = None
                if response == "delete":
                    self._delete_post()

            dialog.connect("response", on_response)
            self._dialog = dialog
            self._present_dialog(dialog)
        except Exception as exc:  # noqa: BLE001
            self._dialog = None
            self._toast(f"Couldn't open the delete dialog: {_describe_error(exc)}")

    def _delete_post(self) -> None:
        client = self._client()
        post = self.post

        def finish(error: str) -> None:
            if error:
                self._toast(f"Couldn't delete post: {error}")
                return
            self._toast("Post deleted")
            try:
                self.ctx.publish("post-deleted", post)
            except Exception:  # noqa: BLE001
                pass

        self._run_action("delete", lambda: client.delete_post(post), finish)

    def _present_dialog(self, dialog: Adw.AlertDialog) -> None:
        parent = self.get_root()
        if not isinstance(parent, Gtk.Window):
            parent = getattr(self.ctx, "window", None)
        try:
            dialog.present(parent if isinstance(parent, Gtk.Window) else None)
        except Exception:  # noqa: BLE001
            try:
                dialog.present(None)
            except Exception:
                pass


        post = self.post
        url = (getattr(post, "post_url", "") or "").strip()
        if url:
            return url
        blog = (getattr(post, "blog_name", "") or "").strip()
        post_id = str(getattr(post, "id", "") or "").strip()
        if blog and post_id:
            return f"https://{blog}.tumblr.com/post/{post_id}"
        return ""

    def _post_url(self) -> str:
        post = self.post
        url = (getattr(post, "post_url", "") or "").strip()
        if url:
            return url
        blog = (getattr(post, "blog_name", "") or "").strip()
        post_id = str(getattr(post, "id", "") or "").strip()
        if blog and post_id:
            return f"https://{blog}.tumblr.com/post/{post_id}"
        return ""

    def _on_open_blog(self, _button: Gtk.Button) -> None:
        self._open_blog()

    def _open_blog(self) -> None:
        post = self.post
        name = (getattr(post, "blog_name", "") or "").strip()
        if not name:
            return
        # The shell turns this into an in-app blog feed; opening the browser as
        # well would do the same job twice.
        try:
            self.ctx.publish("open-blog", name)
        except Exception:  # noqa: BLE001
            pass

    def _open_post(self) -> None:
        self._safe_open(self._post_url())

    def _safe_open(self, url: str) -> None:
        if not url:
            return
        try:
            self.ctx.open_url(url)
        except Exception:  # noqa: BLE001
            pass

    def _copy_link(self) -> None:
        url = self._post_url()
        if url:
            try:
                self.ctx.copy_text(url)
            except Exception:  # noqa: BLE001
                pass

    def _copy_text(self) -> None:
        try:
            text = self._post_text()
        except Exception:  # noqa: BLE001
            return
        if text:
            try:
                self.ctx.copy_text(text)
            except Exception:  # noqa: BLE001
                pass

    def _post_text(self) -> str:
        post = self.post
        text = plain_text(getattr(post, "content", None) or [])
        if text:
            return text
        parts = []
        for item in getattr(post, "trail", None) or []:
            chunk = plain_text(getattr(item, "content", None) or [])
            if chunk:
                parts.append(chunk)
        if parts:
            return "\n\n—\n\n".join(parts)
        return (getattr(post, "summary", "") or "").strip()

    def _on_like(self, _button: Gtk.Button) -> None:
        try:
            self.post.is_liked = not bool(self.post.is_liked)
            delta = 1 if self.post.is_liked else -1
            self.post.like_count = max(0, int(getattr(self.post, "like_count", 0) or 0) + delta)
        except Exception:  # noqa: BLE001
            pass
        self._sync_like()
        try:
            self.ctx.publish("like", self.post)
        except Exception:  # noqa: BLE001
            pass

    def _on_reblog(self) -> None:
        try:
            self.ctx.publish("reblog", self.post)
        except Exception:  # noqa: BLE001
            pass


def _pref(ctx: Any, key: str, default: Any = None) -> Any:
    try:
        return ctx.prefs.get(key, default)
    except Exception:  # noqa: BLE001
        return default
