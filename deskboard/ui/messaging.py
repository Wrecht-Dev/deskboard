"""Messaging: the conversation list, message requests and one thread.

Owner: messaging.

This module is deliberately defensive about Tumblr's DM payloads: the web API
hands back nested objects whose key casing has drifted over the years
(``participants``/``messages`` are camelCase today, but ``participant`` may be a
name string or an object, media may be a list of dictionaries or a single URL).
Nothing here assumes a single privileged shape.

Writes (sending a message, marking a thread read, accepting a request) run on
worker threads exactly like reads.  The offline demo client implements none of
them, so ``--demo`` stays inert; only a user-visible action can surface an
inline error (a failed read receipt is swallowed).

Threading follows the house rule: every network call runs on a worker thread and
its result is dropped unless the generation it was started for is still current.
Every callback is wrapped because a recycled ``Gtk.ListView`` row can disappear
between issuing a request and receiving the answer.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..api.client import TumblrError
from ..util import absolute_time, humanize_time

# --------------------------------------------------------------------------- #
# private stylesheet
# --------------------------------------------------------------------------- #

_CSS_INSTALLED = False


def install_css() -> None:
    """Attach ``messaging.css`` once (tolerates being called before GTK starts)."""
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    try:
        path = Path(__file__).with_name("messaging.css")
        if not path.is_file():
            return
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(path))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _CSS_INSTALLED = True
    except Exception:  # noqa: BLE001 - a missing stylesheet must never break a page
        return


# --------------------------------------------------------------------------- #
# small shared helpers (library.py reuses describe_error)
# --------------------------------------------------------------------------- #


def describe_error(error: BaseException) -> str:
    """A short, user-safe description of a failed load (never a traceback)."""
    if isinstance(error, TumblrError):
        message = str(error) or "Tumblr could not be reached."
    else:
        message = str(error) or type(error).__name__
    message = " ".join(str(message).split())
    if len(message) > 300:
        message = message[:297].rstrip() + "…"
    return message


_PAGE_ITEM_KEYS = ("conversations", "requests", "messages", "items", "results")


def items_from_page(page: Any) -> list[dict]:
    """Read items from an ``ItemPage`` even if the route nests them differently."""
    raw_items = getattr(page, "items", None)
    clean = (
        [entry for entry in raw_items if isinstance(entry, dict)]
        if isinstance(raw_items, list)
        else []
    )
    if clean:
        return clean
    raw = getattr(page, "raw", None)
    if isinstance(raw, dict):
        for key in _PAGE_ITEM_KEYS:
            value = raw.get(key)
            if isinstance(value, list):
                found = [entry for entry in value if isinstance(entry, dict)]
                if found:
                    return found
    return clean


def before_from_url(url: Any) -> str | None:
    """Pull the ``before`` cursor out of a ``links.next`` URL."""
    if not isinstance(url, str) or not url:
        return None
    try:
        query = urllib.parse.urlparse(url).query
        values = urllib.parse.parse_qs(query)
        found = values.get("before") or values.get("beforeTimestamp")
        if found:
            text = str(found[0]).strip()
            return text or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _first_str(mapping: Any, *keys: str) -> str:
    if not isinstance(mapping, dict):
        return ""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def as_bool(value: Any) -> bool:
    """Tumblr occasionally sends booleans as strings; do not treat "false" as true."""
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> int:
    """Normalise seconds, milliseconds and digit strings to epoch seconds."""
    number = _as_float(value)
    if number is None:
        if isinstance(value, str) and value.strip().isdigit():
            number = float(value.strip())
        else:
            return 0
    if number <= 0:
        return 0
    if number > 100_000_000_000:  # milliseconds (1e11 s is year 5138)
        number /= 1000.0
    try:
        return int(number)
    except (OverflowError, ValueError):
        return 0


def _collapse(text: Any, limit: int = 0) -> str:
    value = " ".join(str(text or "").split())
    if limit and len(value) > limit:
        value = value[: max(0, limit - 1)].rstrip() + "…"
    return value


def _avatar_url(value: Any) -> str:
    """Extract one URL from Tumblr's several avatar encodings."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        direct = _first_str(value, "url", "src", "href")
        if direct:
            return direct
        for key in ("avatar", "image", "images"):
            nested = _avatar_url(value.get(key))
            if nested:
                return nested
        return ""
    if isinstance(value, list):
        best = ""
        best_width = -1
        for entry in value:
            url = _avatar_url(entry)
            if not url:
                continue
            width = 0
            if isinstance(entry, dict):
                width = _as_int(entry.get("width")) or 0
            if width >= best_width:
                best, best_width = url, width
        return best
    return ""


# -- participants ----------------------------------------------------------- #

# A conversation participant is a whole blog object on the wire: ``name`` is
# the handle, ``title`` the display name, and ``avatar`` a list of sizes.
_PERSON_DISPLAY_KEYS = (
    "displayName",
    "display_name",
    "title",
    "name",
    "blogName",
    "blog_name",
    "handle",
)
_PERSON_NAME_KEYS = ("name", "blogName", "blog_name", "handle", "title")
_PERSON_TITLE_KEYS = ("title",)
_PERSON_BLOG_KEYS = ("blogName", "blog_name", "name", "handle", "blog")


def _person_display(person: Any) -> str:
    if isinstance(person, str):
        return person.strip()
    if not isinstance(person, dict):
        return ""
    name = _first_str(person, *_PERSON_DISPLAY_KEYS)
    if name:
        return name
    blog = _first_str(person, *_PERSON_BLOG_KEYS)
    return blog


def _person_name(person: Any) -> str:
    if isinstance(person, str):
        return person.strip()
    if isinstance(person, dict):
        return _first_str(person, *_PERSON_NAME_KEYS)
    return ""


def _person_aliases(person: Any) -> set[str]:
    """Every label a message may use for this participant (casefolded)."""
    aliases: set[str] = set()
    for value in (
        _person_display(person),
        _person_name(person),
        _first_str(person, *_PERSON_BLOG_KEYS) if isinstance(person, dict) else "",
    ):
        text = _collapse(value).casefold()
        if text:
            aliases.add(text)
    return aliases


def _person_blog(person: Any) -> str:
    if isinstance(person, str):
        return person.strip()
    if isinstance(person, dict):
        return _first_str(person, *_PERSON_BLOG_KEYS)
    return ""


def _person_avatar(person: Any) -> str:
    if not isinstance(person, dict):
        return ""
    return _avatar_url(
        person.get("avatar")
        or person.get("avatarUrl")
        or person.get("avatar_url")
        or person.get("profilePicture")
    )


def _person_initials(person: Any) -> str:
    name = _person_display(person) or "?"
    letters = [part[0] for part in name.replace("-", " ").split() if part]
    if not letters:
        return name[:2]
    if len(letters) == 1:
        return letters[0].upper()
    return (letters[0] + letters[-1]).upper()


def _conversation_people(conversation: Any, ctx: Any = None) -> list[Any]:
    """Participants other than the signed-in account, in payload order."""
    if not isinstance(conversation, dict):
        return []
    raw = conversation.get("participants")
    if raw is None:
        raw = conversation.get("users") or conversation.get("blogs")
    people: list[Any] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, (dict, str)):
                people.append(entry)
    elif isinstance(raw, dict):
        for key, entry in raw.items():
            if isinstance(entry, dict):
                merged = dict(entry)
                merged.setdefault("name", str(key))
                people.append(merged)
            elif isinstance(entry, str):
                people.append({"name": entry})
            else:
                people.append({"name": str(key)})
    elif isinstance(raw, str) and raw.strip():
        people.append({"name": raw.strip()})
    me: set[str] = set()
    if ctx is not None:
        for attribute in ("user_name", "user_blog"):
            value = _collapse(getattr(ctx, attribute, "") or "").casefold()
            if value:
                me.add(value)
    if me:
        others = [person for person in people if not (_person_aliases(person) & me)]
        if others:
            return others
    # The shell loads the account identity in the background, so a page may be
    # built before ``user_name`` exists.  Tumblr flags the owner's blog with
    # ``primary`` in every conversation, which is a safe fallback marker.
    if len(people) > 1:
        others = [
            person
            for person in people
            if not (isinstance(person, dict) and bool(person.get("primary")))
        ]
        if others and len(others) < len(people):
            return others
    return people


def _conversation_title(conversation: Any, ctx: Any = None) -> str:
    names: list[str] = []
    for person in _conversation_people(conversation, ctx):
        name = _person_display(person)
        if name and name not in names:
            names.append(name)
    if not names:
        fallback = ""
        if isinstance(conversation, dict):
            fallback = _first_str(conversation, "title", "name")
        return fallback or "Conversation"
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:2]) + f" +{len(names) - 2}"


def _conversation_handle(conversation: Any, ctx: Any = None) -> str:
    handles: list[str] = []
    for person in _conversation_people(conversation, ctx):
        blog = _person_blog(person)
        if blog:
            text = blog if blog.startswith("@") else "@" + blog
            if text not in handles:
                handles.append(text)
    return ", ".join(handles[:4])


def _conversation_id(conversation: Any) -> str:
    if not isinstance(conversation, dict):
        return ""
    value = conversation.get("id")
    if value is None:
        return ""
    return str(value).strip()


def _conversation_unread(conversation: Any) -> int:
    if not isinstance(conversation, dict):
        return 0
    for key in ("unreadMessagesCount", "unread_messages_count", "unreadCount"):
        count = _as_int(conversation.get(key))
        if count is not None:
            return max(0, count)
    return 0


def _conversation_spam(conversation: Any) -> bool:
    if not isinstance(conversation, dict):
        return False
    for key in ("isPossibleSpam", "is_possible_spam", "possibleSpam", "isSpam"):
        if as_bool(conversation.get(key)):
            return True
    return False


def _conversation_last_message(conversation: Any) -> dict | None:
    if not isinstance(conversation, dict):
        return None
    raw = conversation.get("messages")
    if not isinstance(raw, list):
        return None
    messages = [item for item in raw if isinstance(item, dict)]
    if not messages:
        return None
    stamps = [_message_timestamp(message) for message in messages]
    if any(stamps):
        best_index = max(range(len(stamps)), key=lambda index: stamps[index])
        return messages[best_index]
    return messages[-1]


# -- messages --------------------------------------------------------------- #


def _flatten_text(value: Any, depth: int = 0) -> str:
    """Best-effort plain text from a text/content field of unknown shape."""
    if depth > 5:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "plainText", "plain_text", "body"):
            if isinstance(value.get(key), str):
                return value[key]
        for key in ("content", "blocks", "paragraphs"):
            nested = value.get(key)
            text = _flatten_text(nested, depth + 1)
            if text:
                return text
        return ""
    if isinstance(value, list):
        parts = [_flatten_text(entry, depth + 1) for entry in value]
        return "\n\n".join(part for part in parts if part)
    return ""


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("content", "text", "body", "message", "plaintext"):
        if key not in message:
            continue
        value = message.get(key)
        if isinstance(value, str):
            if value.strip():
                return value
        else:
            text = _flatten_text(value)
            if text.strip():
                return text
    return ""


def _message_sender(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    sender = message.get("participant")
    if sender is None:
        sender = message.get("sender") or message.get("blogName") or message.get("blog_name")
    if isinstance(sender, dict):
        return _person_display(sender)
    if isinstance(sender, str):
        return sender.strip()
    return ""


def _message_timestamp(message: Any) -> int:
    if not isinstance(message, dict):
        return 0
    for key in ("timestamp", "ts", "createdAt", "created_at", "sentAt", "sent_at", "time"):
        if key in message:
            stamp = _timestamp(message.get(key))
            if stamp:
                return stamp
    return 0


def _message_kind(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    kind = _first_str(message, "type", "messageType", "kind").upper()
    if kind:
        return kind
    if _message_media(message):
        return "IMAGE"
    if isinstance(message.get("post"), dict):
        return "POST"
    return ""


def _message_media(message: Any) -> list[dict]:
    """Normalise the several media encodings to dictionaries with ``url``."""
    if not isinstance(message, dict):
        return []
    raw = message.get("media") or message.get("attachments") or message.get("files")
    if raw is None:
        single = message.get("mediaUrl") or message.get("media_url")
        raw = [{"url": single}] if isinstance(single, str) and single else []
    entries: list[dict] = []
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                url = _avatar_url(item)
                if not url:
                    continue
                kind = _first_str(item, "type", "kind", "mediaType").lower()
                if not kind:
                    mime = _first_str(item, "mimeType", "mime_type", "mime")
                    if mime.startswith("image/"):
                        kind = "image"
                    elif mime.startswith("video/"):
                        kind = "video"
                    elif mime.startswith("audio/"):
                        kind = "audio"
                entries.append(
                    {
                        "url": url,
                        "kind": kind or "image",
                        "width": _as_int(item.get("width")) or 0,
                        "height": _as_int(item.get("height")) or 0,
                    }
                )
            elif isinstance(item, str) and item.strip():
                entries.append({"url": item.strip(), "kind": "image", "width": 0, "height": 0})
    return entries


def _message_is_mine(ctx: Any, conversation: Any, message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    for key in ("isCurrentUser", "is_current_user", "isMine", "mine", "isOwn"):
        if key in message:
            return as_bool(message.get(key))
    sender = _message_sender(message).casefold()
    if not sender:
        return False
    me = _collapse(getattr(ctx, "user_name", "") if ctx is not None else "").casefold()
    if me and sender == me:
        return True
    for person in _conversation_people(conversation, ctx):
        if sender in _person_aliases(person):
            return False
    # A sender that is not one of the (other) participants is the account
    # owner; conversations only list the people on the other side.
    return True


def _message_key(message: Any, fallback: int = 0) -> str:
    if not isinstance(message, dict):
        return f"object:{id(message)}:{fallback}"
    identifier = message.get("id")
    if identifier not in (None, ""):
        return f"id:{identifier}"
    stamp = _message_timestamp(message)
    sender = _message_sender(message)
    text = _message_text(message)
    return f"raw:{stamp}|{sender}|{text[:64]}"


def _message_snippet(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    text = _collapse(_message_text(message), 140)
    if text:
        return text
    kind = _message_kind(message)
    if kind in ("IMAGE", "PHOTO"):
        return "Photo"
    if kind == "VIDEO":
        return "Video"
    if kind == "AUDIO":
        return "Audio"
    if kind == "POST":
        return "Shared a post"
    if _message_media(message):
        return "Attachment"
    return "Message"


def _conversation_snippet(conversation: Any, ctx: Any = None) -> str:
    message = _conversation_last_message(conversation)
    if message is None:
        return ""
    base = _message_snippet(message)
    if not base:
        return ""
    people = _conversation_people(conversation, ctx)
    if len(people) > 1:
        if _message_is_mine(ctx, conversation, message):
            return f"You: {base}"
        sender = _collapse(_message_sender(message), 28)
        for person in people:
            if sender and sender.casefold() in _person_aliases(person):
                sender = _person_display(person) or sender
                break
        if sender:
            return f"{sender}: {base}"
    return base


def _conversation_unread_text(conversation: Any) -> str:
    count = _conversation_unread(conversation)
    if count <= 0:
        return ""
    return "99+" if count > 99 else str(count)


def _conversation_pending(conversation: Any) -> bool:
    if not isinstance(conversation, dict):
        return False
    for key in ("pending", "isPending", "is_pending"):
        if key in conversation:
            return as_bool(conversation.get(key))
    return False


def _conversation_can_send(conversation: Any) -> bool:
    """``canSend == False`` means the thread is read-only (blocked/deleted)."""
    if not isinstance(conversation, dict):
        return False
    for key in ("canSend", "can_send"):
        if key in conversation:
            return as_bool(conversation.get(key))
    status = _first_str(conversation, "status").casefold()
    return status not in ("deleted", "blocked", "inactive", "closed")


def _conversation_recipients(conversation: Any, ctx: Any = None) -> list[str]:
    """Blog names to address a DM to: everyone on the other side, in order."""
    names: list[str] = []
    for person in _conversation_people(conversation, ctx):
        name = _person_blog(person) or _person_display(person)
        if name and name not in names:
            names.append(name)
    if not names and isinstance(conversation, dict):
        fallback = _first_str(conversation, "participant", "blogName", "blog_name")
        if fallback:
            names.append(fallback)
    return names


def _conversation_recipient(conversation: Any, ctx: Any = None) -> str:
    names = _conversation_recipients(conversation, ctx)
    return names[0] if names else ""


# --------------------------------------------------------------------------- #
# tiny widget helpers
# --------------------------------------------------------------------------- #


def _label(
    text: str = "",
    *,
    css: tuple[str, ...] = (),
    xalign: float = 0.0,
    wrap: bool = False,
    selectable: bool = False,
) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign)
    label.set_wrap(wrap)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if selectable:
        label.set_selectable(True)
    for name in css:
        label.add_css_class(name)
    return label


def _spinner(size: int = 18) -> Adw.Spinner:
    spinner = Adw.Spinner()
    spinner.set_size_request(size, size)
    return spinner


def _status_page(icon: str, title: str, description: str) -> Adw.StatusPage:
    page = Adw.StatusPage()
    page.set_icon_name(icon)
    page.set_title(title)
    page.set_description(description)
    page.set_vexpand(True)
    page.set_hexpand(True)
    return page


def _state_button(page: Gtk.Widget, text: str, callback, *, suggested: bool = False) -> Gtk.Button:
    button = Gtk.Button(label=text)
    button.add_css_class("pill")
    if suggested:
        button.add_css_class("suggested-action")
    button.set_halign(Gtk.Align.CENTER)
    button.connect("clicked", lambda *_args: callback())
    page.set_child(button)
    return button


def _message_title_for_error(requests: bool) -> str:
    return "Couldn't load message requests" if requests else "Couldn't load messages"


# --------------------------------------------------------------------------- #
# avatar cluster
# --------------------------------------------------------------------------- #


class _AvatarCluster(Gtk.Overlay):
    """One or two overlapping avatars, plus a ``+N`` counter for groups."""

    __gtype_name__ = "DeskboardMsgAvatarCluster"

    def __init__(self, people: list[Any], ctx: Any, size: int = 44) -> None:
        super().__init__()
        self.set_valign(Gtk.Align.CENTER)
        self._ctx = ctx
        first = people[0] if people else {}
        self._main = Adw.Avatar(size=size, text=_person_initials(first), show_initials=True)
        self._main.add_css_class("db-msg-avatar")
        self.set_child(self._main)
        _load_avatar(self._ctx, self._main, _person_avatar(first), size * 2)

        extra = people[1:]
        if len(extra) == 1:
            second = Adw.Avatar(
                size=max(18, size // 2), text=_person_initials(extra[0]), show_initials=True
            )
            second.add_css_class("db-msg-avatar-secondary")
            second.add_css_class("db-msg-avatar")
            second.set_halign(Gtk.Align.END)
            second.set_valign(Gtk.Align.END)
            second.set_tooltip_text(_person_display(extra[0]) or "")
            try:
                self.add_overlay(second)
            except Exception:  # noqa: BLE001
                pass
            _load_avatar(self._ctx, second, _person_avatar(extra[0]), size)
        elif len(extra) > 1:
            more = Gtk.Label(label=f"+{len(extra)}")
            more.add_css_class("db-msg-avatar-more")
            more.set_halign(Gtk.Align.END)
            more.set_valign(Gtk.Align.END)
            try:
                self.add_overlay(more)
            except Exception:  # noqa: BLE001
                pass


def _load_avatar(ctx: Any, avatar: Adw.Avatar, url: str, size: int) -> None:
    if not url:
        return
    images = getattr(ctx, "images", None)
    if images is None:
        return
    cached = None
    try:
        cached = images.cached(url, size)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        try:
            avatar.set_custom_image(cached)
        except Exception:  # noqa: BLE001
            pass
        return

    def deliver(texture: Any) -> None:
        try:
            if texture is None:
                return
            if avatar.get_parent() is None:  # recycled / destroyed row
                return
            avatar.set_custom_image(texture)
        except Exception:  # noqa: BLE001 - avatars are decorative
            pass

    try:
        images.load(url, size, size, deliver)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# conversation list
# --------------------------------------------------------------------------- #


class _ConversationItem(GObject.Object):
    __gtype_name__ = "DeskboardConversationItem"

    conversation = GObject.Property(type=object)

    def __init__(self, conversation: dict) -> None:
        super().__init__()
        self.conversation = conversation


class _ConversationTab(Gtk.Box):
    """One list (Inbox or Requests) with its own lazy pagination state."""

    def __init__(self, owner: "MessagingPage", ctx: Any, requests: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.owner = owner
        self.ctx = ctx
        self.requests = bool(requests)

        self._generation = 0
        self._inflight = False
        self._finished = False
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._added: set[str] = set()
        self._filled_attempts = 0
        self._fill_source_id = 0
        self._vadj_handler = 0
        self._loaded = False
        self._error = ""
        self._destroyed = False

        self.store = Gio.ListStore(item_type=_ConversationItem)
        self.selection = Gtk.SingleSelection(
            model=self.store, autoselect=False, can_unselect=True
        )
        self.listview = Gtk.ListView(model=self.selection)
        self.listview.add_css_class("db-msg-list")
        self.listview.set_margin_start(10)
        self.listview.set_margin_end(10)
        self.listview.set_margin_top(6)
        self.listview.set_margin_bottom(22)
        try:
            self.listview.set_single_click_activate(True)
        except Exception:  # noqa: BLE001
            pass
        self.listview.connect("activate", self._on_activate)
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
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_error_page(), "error")
        self.stack.add_named(self.overlay, "content")
        self.stack.set_visible_child_name("loading")
        self.append(self.stack)

        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    # -- public ------------------------------------------------------------ #
    def ensure_loaded(self) -> None:
        """Load once, when the tab becomes visible for the first time."""
        if self._loaded or self._inflight:
            return
        self.refresh()

    def refresh(self) -> None:
        self._generation += 1
        self._loaded = True
        self._inflight = False
        self._finished = False
        self._cursor = None
        self._seen_cursors.clear()
        self._added.clear()
        self._error = ""
        self._filled_attempts = 0
        self._cancel_fill()
        try:
            self.store.remove_all()
        except Exception:  # noqa: BLE001
            pass
        self._set_footer(None)
        self._show_state("loading")
        self._request(None)

    def cleanup(self) -> None:
        self._generation += 1
        self._destroyed = True
        self._cancel_fill()
        if self._vadj_handler:
            try:
                self.scroller.get_vadjustment().disconnect(self._vadj_handler)
            except Exception:  # noqa: BLE001
                pass
            self._vadj_handler = 0

    def add_conversation(self, conversation: dict, at: int = 0) -> bool:
        """Insert (or move in) one conversation without a network round-trip."""
        if self._destroyed or not isinstance(conversation, dict):
            return False
        key = _conversation_id(conversation) or f"object:{id(conversation)}"
        if key in self._added:
            return False
        try:
            position = max(0, min(int(at or 0), self.store.get_n_items()))
            self.store.insert(position, _ConversationItem(conversation))
        except Exception:  # noqa: BLE001
            return False
        self._added.add(key)
        if self._loaded:
            try:
                self._show_state("content")
                self._sync_footer()
            except Exception:  # noqa: BLE001
                pass
        return True

    def remove_conversation(self, conversation: dict) -> int | None:
        """Remove one conversation, returning its old row index when found."""
        if not isinstance(conversation, dict):
            return None
        wanted = _conversation_id(conversation)
        try:
            for index in range(self.store.get_n_items()):
                item = self.store.get_item(index)
                data = getattr(item, "conversation", None)
                if not isinstance(data, dict):
                    continue
                if data is conversation:
                    matches = True
                else:
                    other = _conversation_id(data)
                    matches = bool(wanted and other and wanted == other)
                if not matches:
                    continue
                self.store.remove(index)  # GListStore takes a position, not an item
                self._added.discard(wanted or f"object:{id(conversation)}")
                if self._loaded and self.store.get_n_items() == 0:
                    self._show_state("empty")
                self._sync_footer()
                return index
        except Exception:  # noqa: BLE001
            return None
        return None

    def refresh_conversation(self, conversation: dict) -> bool:
        """Rebuild one row after its dict was mutated (e.g. a read receipt)."""
        if self._destroyed or not isinstance(conversation, dict):
            return False
        wanted = _conversation_id(conversation)
        try:
            for index in range(self.store.get_n_items()):
                data = getattr(self.store.get_item(index), "conversation", None)
                if not isinstance(data, dict):
                    continue
                matches = data is conversation
                if not matches:
                    other = _conversation_id(data)
                    matches = bool(wanted and other and wanted == other)
                if matches:
                    # Replacing the item (not items_changed) is what makes
                    # Gtk.ListView rebind the row.
                    self.store.remove(index)
                    self.store.insert(index, _ConversationItem(conversation))
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    # -- construction ------------------------------------------------------ #
    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.add_css_class("db-msg-state")
        box.append(_spinner(32))
        label = _label("Loading conversations…", css=("dim-label", "title-4"))
        label.set_halign(Gtk.Align.CENTER)
        box.append(label)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        if self.requests:
            icon = "user-available-symbolic"
            title = "No message requests"
            description = "Messages from people you don't follow will show up here."
        else:
            icon = "mail-unread-symbolic"
            title = "No conversations"
            description = "Direct messages with people you follow will show up here."
        page = _status_page(icon, title, description)
        _state_button(page, "Refresh", self.refresh)
        return page

    def _build_error_page(self) -> Gtk.Widget:
        page = _status_page(
            "network-error-symbolic",
            _message_title_for_error(self.requests),
            "Tumblr did not answer. Check your connection and try again.",
        )
        _state_button(page, "Retry", self.refresh, suggested=True)
        return page

    def _build_footer(self) -> Gtk.Widget:
        self._footer_spinner = _spinner(18)
        self._footer_label = _label("Loading more…", css=("caption",))
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
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.add_css_class("db-msg-row-slot")
            item.set_child(holder)
        except Exception:  # noqa: BLE001
            item.set_child(Gtk.Box())

    def _bind_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = item.get_child()
            if holder is None:
                return
            _clear_children(holder)
            data = item.get_item()
            conversation = getattr(data, "conversation", None) if data is not None else None
            if isinstance(conversation, dict):
                holder.append(self._build_row(conversation))
        except Exception:  # noqa: BLE001
            pass

    def _unbind_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = item.get_child()
            if holder is not None:
                _clear_children(holder)
        except Exception:  # noqa: BLE001
            pass

    def _teardown_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            item.set_child(None)
        except Exception:  # noqa: BLE001
            pass

    def _build_row(self, conversation: dict) -> Gtk.Widget:
        spam = _conversation_spam(conversation)
        pending = _conversation_pending(conversation)
        can_accept = self.requests and (pending or spam)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("db-msg-row")
        if spam:
            outer.add_css_class("db-msg-spam")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_hexpand(True)
        outer.append(row)

        people = _conversation_people(conversation, self.ctx)
        try:
            row.append(_AvatarCluster(people, self.ctx, 44))
        except Exception:  # noqa: BLE001
            row.append(Adw.Avatar(size=44, text="?", show_initials=True))

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        row.append(text)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = _label(_conversation_title(conversation, self.ctx), css=("heading", "db-msg-name"))
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(60)
        name_row.append(title)
        if spam or pending:
            chip = _label(
                "Message request" if spam else "Request",
                css=("caption", "db-msg-chip"),
            )
            chip.set_valign(Gtk.Align.CENTER)
            name_row.append(chip)
        filler = Gtk.Box()
        filler.set_hexpand(True)
        name_row.append(filler)
        text.append(name_row)

        snippet = _conversation_snippet(conversation, self.ctx)
        if snippet:
            subtitle = _label(snippet, css=("caption", "dim-label", "db-msg-snippet"))
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_max_width_chars(64)
            text.append(subtitle)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_valign(Gtk.Align.CENTER)
        row.append(side)

        stamp = _timestamp(
            (conversation.get("lastModifiedTs") or conversation.get("last_modified_ts"))
        )
        when = humanize_time(stamp) if stamp else ""
        if when:
            time_label = _label(when, css=("caption", "dim-label", "db-msg-time"), xalign=1.0)
            time_label.set_halign(Gtk.Align.END)
            time_label.set_tooltip_text(absolute_time(stamp))
            side.append(time_label)

        badge_text = _conversation_unread_text(conversation)
        if badge_text:
            badge = _label(badge_text, css=("db-msg-badge",), xalign=0.5)
            badge.set_halign(Gtk.Align.END)
            badge.set_tooltip_text(
                "1 unread message"
                if badge_text == "1"
                else f"{badge_text} unread messages"
            )
            side.append(badge)

        hint_text = ""
        if spam and (pending or self.requests):
            hint_text = "Message request — hidden until you reply"
        elif pending:
            hint_text = "Waiting for you to accept"
        if hint_text or can_accept:
            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            footer.set_margin_top(2)
            if hint_text:
                hint = _label(
                    hint_text,
                    css=(
                        ("caption", "dim-label", "db-msg-spam-hint")
                        if spam
                        else ("caption", "dim-label")
                    ),
                )
                hint.set_ellipsize(Pango.EllipsizeMode.END)
                hint.set_hexpand(True)
                footer.append(hint)
            if can_accept:
                accept = Gtk.Button(label="Accept")
                accept.add_css_class("pill")
                accept.add_css_class("suggested-action")
                accept.add_css_class("db-msg-accept")
                accept.set_valign(Gtk.Align.CENTER)
                try:
                    accept.set_sensitive(not self.owner.is_accepting(conversation))
                except Exception:  # noqa: BLE001
                    pass
                accept.connect(
                    "clicked",
                    lambda _button, conv=conversation: self._on_accept_clicked(conv),
                )
                footer.append(accept)
            text.append(footer)
        return outer

    def _on_accept_clicked(self, conversation: dict) -> None:
        try:
            self.owner.accept_conversation(conversation)
        except Exception:  # noqa: BLE001
            pass

    # -- loading ----------------------------------------------------------- #
    def _request(self, cursor: str | None) -> bool:
        if self._inflight or self._destroyed:
            return False
        client = getattr(self.ctx, "client", None)
        if not callable(getattr(client, "get_conversations", None)):
            self._loaded = True
            self._show_unsupported()
            return False
        key = cursor or ""
        if cursor is not None and key in self._seen_cursors:
            self._finished = True
            return False
        self._inflight = True
        self._error = ""
        self._seen_cursors.add(key)
        generation = self._generation
        requests = self.requests

        def work() -> None:
            items: list = []
            next_url: str | None = None
            total: int | None = None
            error = ""
            try:
                page = client.get_conversations(
                    limit=30, before=cursor if cursor else None, requests=requests
                )
                if page is not None:
                    items = items_from_page(page)
                    candidate = getattr(page, "next_url", None)
                    next_url = candidate if isinstance(candidate, str) and candidate else None
                    total = _as_int(getattr(page, "total", None))
            except Exception as exc:  # noqa: BLE001 - never let a worker die loudly
                error = describe_error(exc)
            try:
                GLib.idle_add(self._finish, generation, cursor, items, next_url, total, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(
                target=work,
                daemon=True,
                name=f"msg-list-{'requests' if requests else 'inbox'}-{generation}",
            ).start()
            return True
        except Exception as exc:  # noqa: BLE001 - e.g. thread limits
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
        cursor: str | None,
        items: list,
        next_url: str | None,
        total: int | None,
        error: str,
    ) -> bool:
        if generation != self._generation or self._destroyed:
            return False
        try:
            self._inflight = False
            if error:
                self._error = error
                self._seen_cursors.discard(cursor or "")
                if self.store.get_n_items() == 0:
                    self._show_error(error)
                else:
                    self._set_footer("error")
                return False

            self._error = ""
            self._append(items)
            next_cursor = before_from_url(next_url) if next_url else None
            if next_cursor and next_cursor not in self._seen_cursors:
                self._cursor = next_cursor
                self._finished = False
            else:
                self._cursor = None
                self._finished = True

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

    def _append(self, items: list) -> int:
        added = 0
        for entry in items or []:
            identifier = _conversation_id(entry)
            key = identifier or f"object:{id(entry)}"
            if key in self._added:
                continue
            self._added.add(key)
            try:
                self.store.append(_ConversationItem(entry))
                added += 1
            except Exception:  # noqa: BLE001
                continue
        return added

    def _show_unsupported(self) -> None:
        demo = bool(getattr(self.ctx, "demo", False))
        description = (
            "Conversations are not available in the offline demo."
            if demo
            else "Sign in to your Tumblr account to see direct messages."
        )
        try:
            page = self.stack.get_child_by_name("empty")
            if isinstance(page, Adw.StatusPage):
                page.set_description(description)
        except Exception:  # noqa: BLE001
            pass
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

    # -- pagination / scroll ------------------------------------------------ #
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
                < 320
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
                self._footer_label.set_text("Couldn't load more conversations")
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
        if self._inflight or self._finished or self._cursor is None:
            return
        self._set_footer("loading")
        if not self._request(self._cursor):
            self._sync_footer()

    def _retry_more(self) -> None:
        self._error = ""
        cursor = self._cursor
        if cursor is None:
            self._sync_footer()
            return
        self._set_footer("loading")
        if not self._request(cursor):
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
        if self._filled_attempts >= 8:
            return
        try:
            self._fill_source_id = GLib.timeout_add(120, self._check_fill)
        except Exception:  # noqa: BLE001
            self._fill_source_id = 0

    def _check_fill(self) -> bool:
        self._fill_source_id = 0
        try:
            if self._destroyed or self._finished or self._inflight:
                return False
            if not self._underfilled():
                return False
            self._filled_attempts += 1
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

    # -- activation / teardown ---------------------------------------------- #
    def _on_activate(self, _listview, position: int) -> None:
        try:
            item = self.store.get_item(position)
            conversation = getattr(item, "conversation", None) if item is not None else None
            if isinstance(conversation, dict):
                self.owner.open_conversation(conversation)
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args) -> None:
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            pass


def _clear_children(widget: Gtk.Widget) -> None:
    """Detach every child so a recycled row releases its widgets immediately."""
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


# --------------------------------------------------------------------------- #
# one conversation
# --------------------------------------------------------------------------- #


class _MessageItem(GObject.Object):
    __gtype_name__ = "DeskboardMessageItem"

    message = GObject.Property(type=object)

    def __init__(self, message: dict) -> None:
        super().__init__()
        self.message = message


class ConversationPage(Gtk.Box):
    """One conversation thread: bubbles, an optimistic composer, older pages."""

    __gtype_name__ = "DeskboardConversationPage"

    def __init__(self, ctx, conversation, on_back=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        install_css()
        self.ctx = ctx
        self.conversation = conversation if isinstance(conversation, dict) else {}
        self.on_back = on_back
        self._destroyed = False

        self._generation = 0
        self._inflight = False
        self._finished = False
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._keys: set[str] = set()
        self._started = False
        self._loaded = False
        self._error = ""
        self._fill_source_id = 0
        self._fill_attempts = 0
        self._vadj_handler = 0

        self._conversation_id = _conversation_id(self.conversation)
        self._people = _conversation_people(self.conversation, ctx)
        self._group = len(self._people) > 1
        self._recipients = _conversation_recipients(self.conversation, ctx)
        self._recipient = self._recipients[0] if self._recipients else ""
        self._can_send = _conversation_can_send(self.conversation)

        # composer / write state
        self._send_inflight = False
        self._pending_item: _MessageItem | None = None
        self._pending_message: dict | None = None
        self._read_id = 0
        self.on_read = None

        self.store = Gio.ListStore(item_type=_MessageItem)
        self.selection = Gtk.NoSelection(model=self.store)
        self.listview = Gtk.ListView(model=self.selection)
        self.listview.add_css_class("db-msg-thread")
        self.listview.set_margin_start(10)
        self.listview.set_margin_end(10)
        self.listview.set_margin_top(8)
        self.listview.set_margin_bottom(10)
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

        self._top_pill = self._build_top_pill()
        overlay = Gtk.Overlay()
        overlay.set_child(self.scroller)
        overlay.add_overlay(self._top_pill)
        overlay.set_vexpand(True)
        overlay.set_hexpand(True)
        self._content = overlay

        self.message_stack = Gtk.Stack()
        self.message_stack.set_vexpand(True)
        self.message_stack.set_hexpand(True)
        self.message_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.message_stack.set_transition_duration(120)
        self.message_stack.add_named(self._build_loading_page(), "loading")
        self.message_stack.add_named(self._build_empty_page(), "empty")
        self.message_stack.add_named(self._build_error_page(), "error")
        self.message_stack.add_named(self._content, "content")
        self.message_stack.set_visible_child_name("loading")

        self.append(self._build_header())
        self.append(self.message_stack)
        self.append(self._build_composer())

        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

        self._auto_load_id = GLib.idle_add(self._auto_load)
        # Opening a thread acknowledges its unread messages.  Fire-and-forget:
        # a failed receipt must never interrupt the conversation.
        self._read_id = GLib.idle_add(self._mark_read_once)

    # -- public API -------------------------------------------------------- #
    def refresh(self) -> None:
        """Reload the newest page and reset pagination."""
        self._generation += 1
        self._loaded = True
        self._inflight = False
        self._finished = False
        self._started = False
        self._cursor = None
        self._seen_cursors.clear()
        self._keys.clear()
        self._error = ""
        self._fill_attempts = 0
        self._cancel_fill()
        try:
            self.store.remove_all()
        except Exception:  # noqa: BLE001
            pass
        self._set_top_pill(None)
        self._show_state("loading")
        self._request(None, initial=True)

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

    def message_count(self) -> int:
        try:
            return self.store.get_n_items()
        except Exception:  # noqa: BLE001
            return 0

    # -- construction ------------------------------------------------------ #
    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("db-msg-convheader")

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.add_css_class("flat")
        self.back_button.set_tooltip_text("Back to conversations")
        self.back_button.set_valign(Gtk.Align.CENTER)
        self.back_button.connect("clicked", self._on_back_clicked)
        self.back_button.set_visible(callable(self.on_back))
        header.append(self.back_button)

        try:
            header.append(_AvatarCluster(self._people, self.ctx, 34))
        except Exception:  # noqa: BLE001
            pass

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        titles.set_hexpand(True)
        titles.set_valign(Gtk.Align.CENTER)
        header.append(titles)

        self.title_label = _label(
            _conversation_title(self.conversation, self.ctx),
            css=("heading", "db-msg-convtitle"),
        )
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(48)
        titles.append(self.title_label)

        handle = _conversation_handle(self.conversation, self.ctx)
        self.handle_label = _label(handle, css=("caption", "dim-label"))
        self.handle_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.handle_label.set_visible(bool(handle))
        titles.append(self.handle_label)

        return header

    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.add_css_class("db-msg-state")
        box.append(_spinner(30))
        label = _label("Loading messages…", css=("dim-label", "title-4"))
        label.set_halign(Gtk.Align.CENTER)
        box.append(label)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        return _status_page(
            "mail-unread-symbolic",
            "No messages yet",
            "Messages in this conversation will appear here.",
        )

    def _build_error_page(self) -> Gtk.Widget:
        page = _status_page(
            "network-error-symbolic",
            "Couldn't load this conversation",
            "Tumblr did not answer. Check your connection and try again.",
        )
        _state_button(page, "Retry", self.refresh, suggested=True)
        return page

    def _build_top_pill(self) -> Gtk.Widget:
        self._top_spinner = _spinner(16)
        self._top_label = _label("Loading earlier messages…", css=("caption",))
        self._top_retry = Gtk.Button(label="Retry")
        self._top_retry.add_css_class("flat")
        self._top_retry.add_css_class("caption")
        self._top_retry.set_visible(False)
        self._top_retry.connect("clicked", lambda *_args: self._retry_older())
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        pill.add_css_class("db-msg-pill")
        pill.append(self._top_spinner)
        pill.append(self._top_label)
        pill.append(self._top_retry)
        pill.set_halign(Gtk.Align.CENTER)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(160)
        revealer.set_child(pill)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.START)
        revealer.set_margin_top(10)
        revealer.set_reveal_child(False)
        return revealer

    def _build_composer(self) -> Gtk.Widget:
        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        composer.add_css_class("db-msg-composer")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        composer.append(row)

        # Multi-line on purpose: Enter sends, Shift+Enter adds a line.
        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_accepts_tab(False)
        self.text_view.set_top_margin(7)
        self.text_view.set_bottom_margin(7)
        self.text_view.set_left_margin(9)
        self.text_view.set_right_margin(9)
        self.buffer = self.text_view.get_buffer()
        self.buffer.connect("changed", self._on_composer_changed)

        self.composer_scroll = Gtk.ScrolledWindow()
        self.composer_scroll.add_css_class("db-msg-composer-scroll")
        self.composer_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.composer_scroll.set_propagate_natural_height(True)
        self.composer_scroll.set_max_content_height(132)
        self.composer_scroll.set_min_content_height(42)
        self.composer_scroll.set_hexpand(True)
        self.composer_scroll.set_child(self.text_view)
        row.append(self.composer_scroll)

        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("pill")
        self.send_button.add_css_class("suggested-action")
        self.send_button.set_valign(Gtk.Align.CENTER)
        self.send_button.connect("clicked", lambda *_args: self._send())
        row.append(self.send_button)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_composer_key)
        self.text_view.add_controller(key_controller)

        self.send_error = _label("", css=("caption", "db-msg-senderror"))
        self.send_error.set_wrap(True)
        self.send_error.set_visible(False)
        composer.append(self.send_error)

        self.composer_hint = _label("", css=("caption", "dim-label", "db-msg-sendhint"))
        self.composer_hint.set_wrap(True)
        composer.append(self.composer_hint)

        self._update_composer()
        return composer

    # -- composer state ---------------------------------------------------- #
    def _composer_reason(self) -> str:
        """Why the composer is unavailable, or ``""`` when it can send."""
        client = getattr(self.ctx, "client", None)
        sender = getattr(client, "send_message", None)
        demo = bool(getattr(self.ctx, "demo", False))
        if not callable(sender):
            if demo:
                return "Sending isn't available in the offline demo."
            return "Sending isn't available with this Tumblr client."
        if not self._conversation_id:
            return "This conversation can't be messaged."
        if not self._recipient:
            return "Can't reply in this conversation."
        if not self._can_send:
            return "This conversation is read-only."
        return ""

    def _buffer_text(self) -> str:
        try:
            start, end = self.buffer.get_bounds()
            return self.buffer.get_text(start, end, True)
        except Exception:  # noqa: BLE001
            return ""

    def _set_buffer_text(self, text: str) -> None:
        try:
            self.buffer.set_text(text or "")
        except Exception:  # noqa: BLE001
            pass

    def _update_composer(self) -> None:
        try:
            reason = self._composer_reason()
            has_text = bool(self._buffer_text().strip())
            sending = self._send_inflight
            self.text_view.set_sensitive(not reason)
            self.send_button.set_sensitive(not reason and not sending and has_text)
            if sending:
                hint = "Sending…"
            elif reason:
                hint = reason
            else:
                hint = ""
            self.composer_hint.set_text(hint)
            self.composer_hint.set_visible(bool(hint))
            if reason:
                self.text_view.set_tooltip_text(reason)
                self.send_button.set_tooltip_text(reason)
            elif sending:
                self.text_view.set_tooltip_text("Sending your message…")
                self.send_button.set_tooltip_text("Sending…")
            else:
                self.text_view.set_tooltip_text("Enter sends, Shift+Enter adds a line")
                self.send_button.set_tooltip_text("Send message")
        except Exception:  # noqa: BLE001
            pass

    def _on_composer_changed(self, *_args) -> None:
        try:
            self._hide_send_error()
        except Exception:  # noqa: BLE001
            pass
        self._update_composer()

    def _on_composer_key(self, _controller, keyval, _keycode, state) -> bool:
        try:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if state & Gdk.ModifierType.SHIFT_MASK:
                    return False  # let the view insert a newline
                self._send()
                return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _show_send_error(self, message: str) -> None:
        try:
            text = _collapse(message, 280) or "Tumblr refused the message."
            self.send_error.set_text(f"Couldn't send: {text}")
            self.send_error.set_visible(True)
        except Exception:  # noqa: BLE001
            pass

    def _hide_send_error(self) -> None:
        try:
            self.send_error.set_visible(False)
            self.send_error.set_text("")
        except Exception:  # noqa: BLE001
            pass

    # -- sending ----------------------------------------------------------- #
    def _send(self) -> None:
        """Optimistically append a bubble, then POST the message off-thread."""
        if self._send_inflight or self._destroyed:
            return
        reason = self._composer_reason()
        if reason:
            self._update_composer()
            return
        text = self._buffer_text().strip()
        if not text:
            return
        client = getattr(self.ctx, "client", None)
        sender = getattr(client, "send_message", None)
        if not callable(sender):
            self._update_composer()
            return

        conversation_id = self._conversation_id
        participant = self._recipient
        participants = list(self._recipients)
        self._send_inflight = True
        self._hide_send_error()

        message = {
            "id": f"local-{time.time_ns()}",
            "participant": (
                str(getattr(self.ctx, "user_name", "") or getattr(self.ctx, "user_blog", ""))
                or "me"
            ),
            "content": text,
            "type": "TEXT",
            "timestamp": int(time.time()),
            "isCurrentUser": True,
            "_pending": True,
            "_local": True,
        }
        item = _MessageItem(message)
        self._pending_item = item
        self._pending_message = message
        try:
            self.store.append(item)
            self._show_state("content")
            self._schedule_scroll_bottom()
        except Exception:  # noqa: BLE001
            self._pending_item = None
            self._pending_message = None
            self._send_inflight = False
            self._show_send_error("The message could not be displayed.")
            self._update_composer()
            return
        self._update_composer()

        def work() -> None:
            error = ""
            try:
                sender(conversation_id, participant, text, participants=participants)
            except Exception as exc:  # noqa: BLE001 - report on the main loop
                error = describe_error(exc)
            try:
                GLib.idle_add(self._finish_send, item, message, text, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="msg-send").start()
        except Exception as exc:  # noqa: BLE001 - e.g. thread limits
            self._finish_send(item, message, text, describe_error(exc))

    def _position_of(self, item: GObject.Object) -> int:
        try:
            for index in range(self.store.get_n_items()):
                if self.store.get_item(index) is item:
                    return index
        except Exception:  # noqa: BLE001
            return -1
        return -1

    def _finish_send(self, item, message: dict, sent_text: str, error: str) -> bool:
        self._send_inflight = False
        try:
            if self._destroyed:
                return False
            position = self._position_of(item)
            if error:
                if position >= 0:
                    try:
                        self.store.remove(position)
                    except Exception:  # noqa: BLE001
                        pass
                if self.store.get_n_items() == 0:
                    self._show_state("empty")
                self._pending_item = None
                self._pending_message = None
                self._show_send_error(error)
                # The typed text was never removed; restore it only if the
                # user cleared the box while the request was running.
                if not self._buffer_text().strip():
                    self._set_buffer_text(sent_text)
                self._update_composer()
                return False

            message.pop("_pending", None)
            message.pop("_local", None)
            if position >= 0:
                try:
                    # Replace the row so the dimmed "sending" bubble becomes a
                    # normal sent bubble without touching the network state.
                    self.store.remove(position)
                    self.store.insert(position, _MessageItem(message))
                except Exception:  # noqa: BLE001
                    pass
            self._pending_item = None
            self._pending_message = None
            self._hide_send_error()
            if self._buffer_text().strip() == sent_text:
                self._set_buffer_text("")
            self._update_composer()
            self._schedule_scroll_bottom()
            return False
        except Exception:  # noqa: BLE001
            return False

    # -- read receipts ----------------------------------------------------- #
    def _mark_read_once(self) -> bool:
        self._read_id = 0
        try:
            if self._destroyed:
                return False
            client = getattr(self.ctx, "client", None)
            mark = getattr(client, "mark_conversation_read", None)
            if not callable(mark) or not self._conversation_id:
                return False
            conversation_id = self._conversation_id
            participant = self._recipient

            def work() -> None:
                try:
                    mark(conversation_id, participant)
                except Exception:  # noqa: BLE001 - a receipt is best effort
                    return
                try:
                    GLib.idle_add(self._on_read_marked)
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=work, daemon=True, name="msg-mark-read").start()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_read_marked(self) -> bool:
        try:
            if not self._destroyed and isinstance(self.conversation, dict):
                self.conversation["unreadMessagesCount"] = 0
                callback = self.on_read
                if callable(callback):
                    callback()
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- list factory ------------------------------------------------------- #
    def _setup_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.set_hexpand(True)
            holder.add_css_class("db-msg-message-slot")
            item.set_child(holder)
        except Exception:  # noqa: BLE001
            item.set_child(Gtk.Box())

    def _bind_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = item.get_child()
            if holder is None:
                return
            _clear_children(holder)
            data = item.get_item()
            message = getattr(data, "message", None) if data is not None else None
            if isinstance(message, dict):
                holder.append(self._build_message_row(message))
        except Exception:  # noqa: BLE001 - one broken message must not kill the thread
            pass

    def _unbind_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            holder = item.get_child()
            if holder is not None:
                _clear_children(holder)
        except Exception:  # noqa: BLE001
            pass

    def _teardown_item(self, _factory, item: Gtk.ListItem) -> None:
        try:
            item.set_child(None)
        except Exception:  # noqa: BLE001
            pass

    def _build_message_row(self, message: dict) -> Gtk.Widget:
        mine = _message_is_mine(self.ctx, self.conversation, message)
        pending = as_bool(message.get("_pending"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.set_hexpand(True)
        row.add_css_class("db-msg-message-row")
        row.add_css_class("db-msg-message-mine" if mine else "db-msg-message-theirs")

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("db-msg-bubble")
        if mine:
            bubble.add_css_class("db-msg-bubble-mine")
        if pending:
            bubble.add_css_class("db-msg-pending")

        if not mine and self._group:
            raw_sender = _message_sender(message).casefold()
            sender = _person_display(
                next(
                    (
                        person
                        for person in self._people
                        if raw_sender and raw_sender in _person_aliases(person)
                    ),
                    None,
                )
            ) or _message_sender(message)
            if sender:
                name = _label(
                    _collapse(sender, 40), css=("caption", "db-msg-sender"), wrap=True
                )
                bubble.append(name)

        body_built = False
        text = _message_text(message)
        if text.strip():
            label = _label(text, css=("db-msg-text",), wrap=True, selectable=True)
            label.set_max_width_chars(60)
            label.set_xalign(0.0)
            bubble.append(label)
            body_built = True

        for media in _message_media(message)[:4]:
            widget = self._build_media(media)
            if widget is not None:
                bubble.append(widget)
                body_built = True

        post = message.get("post")
        if isinstance(post, dict):
            widget = self._build_shared_post(post)
            if widget is not None:
                bubble.append(widget)
                body_built = True

        if not body_built:
            bubble.append(
                _label("Unsupported message", css=("caption", "dim-label", "db-msg-text"))
            )

        stamp = _message_timestamp(message)
        when = "Sending…" if pending else (humanize_time(stamp) if stamp else "")
        if when:
            time_label = _label(
                when,
                css=("caption", "db-msg-time"),
                xalign=1.0 if mine else 0.0,
            )
            time_label.set_halign(Gtk.Align.END if mine else Gtk.Align.START)
            if stamp and not pending:
                time_label.set_tooltip_text(absolute_time(stamp))
            bubble.append(time_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        if mine:
            row.append(spacer)
            row.append(bubble)
        else:
            row.append(bubble)
            row.append(spacer)
        return row

    def _build_media(self, media: dict) -> Gtk.Widget | None:
        url = str(media.get("url") or "")
        if not url:
            return None
        kind = str(media.get("kind") or "image").lower()
        if kind in ("video", "audio", "gif"):
            icon = "video-x-generic-symbolic" if kind == "video" else "audio-x-generic-symbolic"
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            chip.add_css_class("db-msg-media-chip")
            chip.append(Gtk.Image.new_from_icon_name(icon))
            chip.append(_label(kind.capitalize(), css=("caption",)))
            return chip
        picture = Gtk.Picture()
        picture.add_css_class("db-msg-media")
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        width = _as_int(media.get("width")) or 0
        height = _as_int(media.get("height")) or 0
        box_width, box_height = 260, 180
        if width > 0 and height > 0:
            box_width = min(260, width)
            box_height = max(90, min(300, int(box_width * (height / width))))
        picture.set_size_request(box_width, box_height)

        def deliver(texture: Any) -> None:
            try:
                if texture is None or picture.get_parent() is None:
                    return
                picture.set_paintable(texture)
            except Exception:  # noqa: BLE001
                pass

        images = getattr(self.ctx, "images", None)
        if images is not None:
            try:
                images.load(url, 520, 520, deliver)
            except Exception:  # noqa: BLE001
                pass
        return picture

    def _build_shared_post(self, post: dict) -> Gtk.Widget | None:
        blog = _first_str(post, "blog_name", "blogName", "blog", "blogTitle")
        summary = _first_str(post, "summary", "excerpt", "caption", "title")
        if not summary:
            summary = _flatten_text(post.get("content") or post.get("body"))
        url = _first_str(post, "post_url", "postUrl", "url")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("db-msg-postcard")
        heading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        heading_row.append(Gtk.Image.new_from_icon_name("text-x-generic-symbolic"))
        heading = "Shared a post" + (f" from {blog}" if blog else "")
        heading_label = _label(_collapse(heading, 60), css=("caption", "db-msg-postheading"))
        heading_label.set_ellipsize(Pango.EllipsizeMode.END)
        heading_row.append(heading_label)
        card.append(heading_row)
        if summary:
            excerpt = _label(_collapse(summary, 220), css=("caption", "db-msg-postexcerpt"))
            excerpt.set_wrap(True)
            excerpt.set_max_width_chars(54)
            excerpt.set_lines(3)
            excerpt.set_ellipsize(Pango.EllipsizeMode.END)
            card.append(excerpt)
        if url:
            button = Gtk.Button(label="Open post")
            button.add_css_class("flat")
            button.add_css_class("caption")
            button.set_halign(Gtk.Align.START)
            button.connect("clicked", lambda *_args: self._open_url(url))
            card.append(button)
        return card

    def _open_url(self, url: str) -> None:
        try:
            self.ctx.open_url(url)
        except Exception:  # noqa: BLE001
            pass

    # -- loading ------------------------------------------------------------ #
    def _auto_load(self) -> bool:
        self._auto_load_id = 0
        try:
            if self._destroyed or self._loaded or self._inflight:
                return False
            if not self._conversation_id:
                self._show_state("empty")
                return False
            self.refresh()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _request(self, cursor: str | None, initial: bool) -> bool:
        if self._inflight or self._destroyed:
            return False
        if not self._conversation_id:
            self._show_state("empty")
            return False
        client = getattr(self.ctx, "client", None)
        if not callable(getattr(client, "get_conversation", None)):
            self._show_state("empty")
            return False
        key = cursor or ""
        if cursor is not None and key in self._seen_cursors:
            self._finished = True
            return False
        self._inflight = True
        self._error = ""
        self._seen_cursors.add(key)
        generation = self._generation
        conversation_id = self._conversation_id

        def work() -> None:
            items: list = []
            next_url: str | None = None
            error = ""
            try:
                page = client.get_conversation(
                    conversation_id, limit=50, before=cursor if cursor else None
                )
                if page is not None:
                    items = items_from_page(page)
                    candidate = getattr(page, "next_url", None)
                    next_url = candidate if isinstance(candidate, str) and candidate else None
            except Exception as exc:  # noqa: BLE001
                error = describe_error(exc)
            try:
                GLib.idle_add(
                    self._finish, generation, cursor, items, next_url, error, initial
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(
                target=work, daemon=True, name=f"msg-thread-{generation}"
            ).start()
            return True
        except Exception as exc:  # noqa: BLE001
            self._inflight = False
            self._error = describe_error(exc)
            if self.store.get_n_items() == 0:
                self._show_error(self._error)
            else:
                self._set_top_pill("error")
            return False

    def _finish(
        self,
        generation: int,
        cursor: str | None,
        items: list,
        next_url: str | None,
        error: str,
        initial: bool,
    ) -> bool:
        if generation != self._generation or self._destroyed:
            return False
        try:
            self._inflight = False
            if error:
                self._error = error
                self._seen_cursors.discard(cursor or "")
                if self.store.get_n_items() == 0:
                    self._show_error(error)
                else:
                    self._set_top_pill("error")
                return False

            self._error = ""
            ordered = _chronological(items)
            # When everything already fit in the viewport, the user is looking
            # at the bottom (newest) and backfilling must keep it there; a
            # real "scroll up for older" keeps the current message pinned.
            was_underfilled = self._underfilled()
            anchor = None if initial else self._capture_anchor()
            if initial:
                self._append_initial(ordered)
            else:
                self._append_older(ordered)

            next_cursor = before_from_url(next_url) if next_url else None
            if next_cursor and next_cursor not in self._seen_cursors:
                self._cursor = next_cursor
                self._finished = False
            else:
                self._cursor = None
                self._finished = True

            if self.store.get_n_items() == 0:
                self._show_state("empty")
                self._set_top_pill(None)
            else:
                self._started = True
                self._show_state("content")
                self._set_top_pill(None)
                if initial or was_underfilled:
                    self._schedule_scroll_bottom()
                elif anchor is not None:
                    GLib.idle_add(self._restore_anchor, anchor)
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

    def _append_initial(self, messages: list) -> int:
        added = 0
        for message in messages:
            key = _message_key(message, added)
            if key in self._keys:
                continue
            self._keys.add(key)
            try:
                self.store.append(_MessageItem(message))
                added += 1
            except Exception:  # noqa: BLE001
                continue
        return added

    def _append_older(self, messages: list) -> int:
        position = 0
        added = 0
        for message in messages:
            key = _message_key(message, added)
            if key in self._keys:
                continue
            self._keys.add(key)
            try:
                self.store.insert(position, _MessageItem(message))
                position += 1
                added += 1
            except Exception:  # noqa: BLE001
                continue
        return added

    def _show_state(self, name: str) -> None:
        try:
            if self.message_stack.get_visible_child_name() != name:
                self.message_stack.set_visible_child_name(name)
        except Exception:  # noqa: BLE001
            pass

    def _show_error(self, message: str) -> None:
        try:
            page = self.message_stack.get_child_by_name("error")
            if isinstance(page, Adw.StatusPage):
                page.set_description(message)
            self._show_state("error")
            self._set_top_pill(None)
        except Exception:  # noqa: BLE001
            pass

    # -- scrolling / pagination --------------------------------------------- #
    def _adjustment(self):
        try:
            return self.scroller.get_vadjustment()
        except Exception:  # noqa: BLE001
            return None

    def _capture_anchor(self):
        adjustment = self._adjustment()
        if adjustment is None:
            return None
        try:
            return (adjustment.get_value(), adjustment.get_upper(), adjustment.get_page_size())
        except Exception:  # noqa: BLE001
            return None

    def _restore_anchor(self, anchor) -> bool:
        adjustment = self._adjustment()
        if adjustment is None or not anchor:
            return False
        try:
            value, upper, _page = anchor
            delta = max(0.0, adjustment.get_upper() - upper)
            adjustment.set_value(max(0.0, value + delta))
        except Exception:  # noqa: BLE001
            pass
        return False

    def _scroll_to_bottom(self) -> bool:
        try:
            count = self.store.get_n_items()
            if count > 0:
                self.listview.scroll_to(count - 1, Gtk.ListScrollFlags.NONE, None)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _scroll_to_bottom_soon(self) -> bool:
        if not self._destroyed:
            self._scroll_to_bottom()
        return False

    def _schedule_scroll_bottom(self) -> None:
        """Scroll now and once more after the new row has been measured."""
        try:
            GLib.idle_add(self._scroll_to_bottom)
        except Exception:  # noqa: BLE001
            pass
        try:
            GLib.timeout_add(90, self._scroll_to_bottom_soon)
        except Exception:  # noqa: BLE001
            pass

    def _near_top(self) -> bool:
        adjustment = self._adjustment()
        if adjustment is None:
            return False
        try:
            return adjustment.get_value() < 90
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

    def _set_top_pill(self, mode: str | None) -> None:
        try:
            if mode is None:
                self._top_pill.set_reveal_child(False)
                return
            if mode == "loading":
                self._top_spinner.set_visible(True)
                self._top_label.set_text("Loading earlier messages…")
                self._top_retry.set_visible(False)
            else:
                self._top_spinner.set_visible(False)
                self._top_label.set_text("Couldn't load earlier messages")
                self._top_retry.set_visible(True)
            self._top_pill.set_reveal_child(True)
        except Exception:  # noqa: BLE001
            pass

    def _load_older(self) -> None:
        if self._inflight or self._finished or self._cursor is None:
            return
        self._set_top_pill("loading")
        if not self._request(self._cursor, initial=False):
            self._set_top_pill(None)

    def _retry_older(self) -> None:
        self._error = ""
        self._load_older()

    def _on_scroll(self, _adjustment) -> None:
        try:
            if self.store.get_n_items() == 0 or not self._started:
                return
            if not self._near_top():
                return
            if self._error:
                self._set_top_pill("error")
                return
            if self._inflight:
                self._set_top_pill("loading")
                return
            self._load_older()
        except Exception:  # noqa: BLE001
            pass

    def _schedule_fill(self) -> None:
        if self._fill_source_id or self._destroyed:
            return
        if self._finished or self._inflight or not self._underfilled():
            return
        if self._fill_attempts >= 10:
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
            self._load_older()
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

    # -- misc --------------------------------------------------------------- #
    def _on_back_clicked(self, *_args) -> None:
        if not callable(self.on_back):
            return
        try:
            self.on_back()
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args) -> None:
        try:
            if self._auto_load_id:
                GLib.source_remove(self._auto_load_id)
                self._auto_load_id = 0
        except Exception:  # noqa: BLE001
            pass
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            pass


def _chronological(messages: list) -> list:
    """Order one API page oldest → newest without trusting its direction."""
    if not messages:
        return []
    stamps = [_message_timestamp(message) for message in messages]
    known = [stamp for stamp in stamps if stamp > 0]
    if known:
        if len(known) == len(stamps):
            return sorted(messages, key=lambda message: _message_timestamp(message))
        # Mixed timestamps: use whichever end of the page looks older as the
        # anchor, but keep the payload order stable.
        if stamps[0] <= stamps[-1]:
            return messages
        return list(reversed(messages))
    # No timestamps at all: Tumblr's `before=` pagination is newest-first.
    return list(reversed(messages))


# --------------------------------------------------------------------------- #
# the messages page (list of conversations)
# --------------------------------------------------------------------------- #


class MessagingPage(Gtk.Box):
    """Inbox / Requests tabs plus an embedded conversation view."""

    __gtype_name__ = "DeskboardMessagingPage"

    def __init__(self, ctx) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        install_css()
        self.ctx = ctx
        self._destroyed = False
        self._accept_inflight: set[str] = set()
        self.conversation_page: ConversationPage | None = None

        # Adw.ViewSwitcher only accepts an Adw.ViewStack, so the tab stack is
        # the libadwaita one (same named-child API as Gtk.Stack).
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)
        try:
            self.view_stack.set_transition_type(Adw.ViewStackTransitionType.CROSSFADE)
            self.view_stack.set_transition_duration(120)
        except Exception:  # noqa: BLE001 - older runtime
            pass

        self.inbox_tab = _ConversationTab(self, ctx, requests=False)
        self.requests_tab = _ConversationTab(self, ctx, requests=True)
        self.view_stack.add_titled(self.inbox_tab, "inbox", "Inbox")
        self.view_stack.add_titled(self.requests_tab, "requests", "Requests")
        try:
            self.view_stack.set_visible_child_name("inbox")
        except Exception:  # noqa: BLE001
            pass
        self.view_stack.connect("notify::visible-child", self._on_tab_changed)

        self.switcher = Adw.ViewSwitcher()
        self.switcher.set_stack(self.view_stack)
        try:
            self.switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        except Exception:  # noqa: BLE001
            pass

        toolrow = Gtk.CenterBox()
        toolrow.add_css_class("db-msg-toolrow")
        toolrow.set_center_widget(self.switcher)

        list_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        list_page.set_vexpand(True)
        list_page.set_hexpand(True)
        list_page.append(toolrow)
        list_page.append(self.view_stack)

        self.root_stack = Gtk.Stack()
        self.root_stack.set_vexpand(True)
        self.root_stack.set_hexpand(True)
        self.root_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.root_stack.set_transition_duration(180)
        self.root_stack.add_named(list_page, "list")
        self.root_stack.set_visible_child_name("list")
        self.append(self.root_stack)

        try:
            self.connect("map", self._on_map)
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    # -- public API -------------------------------------------------------- #
    def refresh(self) -> None:
        """Reload whatever the page is showing."""
        if self.conversation_page is not None:
            try:
                self.conversation_page.refresh()
                return
            except Exception:  # noqa: BLE001
                pass
        tab = self.active_tab()
        if tab is not None:
            try:
                tab.refresh()
            except Exception:  # noqa: BLE001
                pass

    def active_tab(self) -> _ConversationTab | None:
        try:
            name = self.view_stack.get_visible_child_name()
            if name == "requests":
                return self.requests_tab
            return self.inbox_tab
        except Exception:  # noqa: BLE001
            return None

    def open_conversation(self, conversation) -> None:
        """Show one conversation in-place (the shell does not need to route)."""
        if not isinstance(conversation, dict) or self._destroyed:
            return
        self._close_conversation(silent=True)
        try:
            page = ConversationPage(self.ctx, conversation, on_back=self._close_conversation)
        except Exception as exc:  # noqa: BLE001
            try:
                self.ctx.toast(f"Couldn't open that conversation: {describe_error(exc)}")
            except Exception:  # noqa: BLE001
                pass
            return
        self.conversation_page = page
        try:
            page.on_read = lambda conv=conversation: self._refresh_conversation(conv)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root_stack.add_named(page, "conversation")
            self.root_stack.set_visible_child_name("conversation")
        except Exception:  # noqa: BLE001
            self.conversation_page = None
            try:
                page.unparent()
            except Exception:  # noqa: BLE001
                pass

    def show_list(self) -> None:
        self._close_conversation()

    def is_accepting(self, conversation) -> bool:
        if not isinstance(conversation, dict):
            return False
        key = _conversation_id(conversation) or f"object:{id(conversation)}"
        return key in self._accept_inflight

    def accept_conversation(self, conversation) -> None:
        """Accept a message request: optimistic move, then confirm off-thread."""
        if not isinstance(conversation, dict) or self._destroyed:
            return
        key = _conversation_id(conversation) or f"object:{id(conversation)}"
        if key in self._accept_inflight:
            return
        client = getattr(self.ctx, "client", None)
        method = getattr(client, "accept_conversation", None)
        if not callable(method):
            demo = bool(getattr(self.ctx, "demo", False))
            self._toast(
                "Accepting message requests isn't available in the offline demo."
                if demo
                else "Accepting message requests isn't available with this Tumblr client."
            )
            return
        conversation_id = _conversation_id(conversation)
        participant = _conversation_recipient(conversation, self.ctx)
        if not conversation_id or not participant:
            self._toast("Couldn't accept that request: missing conversation details.")
            return

        self._accept_inflight.add(key)
        original_pending = conversation.get("pending")
        original_index = self.requests_tab.remove_conversation(conversation)
        conversation["pending"] = False
        moved = False
        if self.inbox_tab._loaded:
            moved = self.inbox_tab.add_conversation(conversation, at=0)
        self.requests_tab.refresh_conversation(conversation)

        def work() -> None:
            error = ""
            try:
                method(conversation_id, participant)
            except Exception as exc:  # noqa: BLE001 - report on the main loop
                error = describe_error(exc)
            try:
                GLib.idle_add(
                    self._finish_accept,
                    conversation,
                    original_pending,
                    original_index,
                    moved,
                    error,
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="msg-accept").start()
        except Exception as exc:  # noqa: BLE001
            self._finish_accept(
                conversation, original_pending, original_index, moved, describe_error(exc)
            )

    def _finish_accept(
        self,
        conversation,
        original_pending,
        original_index,
        moved: bool,
        error: str,
    ) -> bool:
        if not isinstance(conversation, dict):
            return False
        key = _conversation_id(conversation) or f"object:{id(conversation)}"
        self._accept_inflight.discard(key)
        try:
            if self._destroyed:
                return False
            if error:
                conversation["pending"] = original_pending
                if moved:
                    self.inbox_tab.remove_conversation(conversation)
                if original_index is not None and self.requests_tab._loaded:
                    self.requests_tab.add_conversation(conversation, at=original_index)
                self.requests_tab.refresh_conversation(conversation)
                self.inbox_tab.refresh_conversation(conversation)
                self._toast(f"Couldn't accept that request: {error}")
            else:
                self.inbox_tab.refresh_conversation(conversation)
                self._toast("Message request accepted")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _toast(self, message: str) -> None:
        try:
            self.ctx.toast(message)
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        self._destroyed = True
        self._close_conversation(silent=True)
        for tab in (self.inbox_tab, self.requests_tab):
            try:
                tab.cleanup()
            except Exception:  # noqa: BLE001
                pass

    # -- internals --------------------------------------------------------- #
    def _close_conversation(self, silent: bool = False) -> None:
        page = self.conversation_page
        self.conversation_page = None
        if page is None:
            return
        try:
            page.cleanup()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root_stack.set_visible_child_name("list")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root_stack.remove(page)  # Gtk.Stack.remove unparents in GTK4
        except Exception:  # noqa: BLE001
            pass
        if not silent:
            tab = self.active_tab()
            if tab is not None:
                try:
                    tab.listview.grab_focus()
                except Exception:  # noqa: BLE001
                    pass
        # The conversation dict may have been mutated (read receipt), so
        # rebuild just that row when we are back on the list.
        for tab in (self.inbox_tab, self.requests_tab):
            try:
                tab.refresh_conversation(page.conversation)
            except Exception:  # noqa: BLE001
                pass

    def _refresh_conversation(self, conversation) -> None:
        for tab in (self.inbox_tab, self.requests_tab):
            try:
                tab.refresh_conversation(conversation)
            except Exception:  # noqa: BLE001
                pass

    def _on_tab_changed(self, *_args) -> None:
        tab = self.active_tab()
        if tab is not None:
            try:
                tab.ensure_loaded()
            except Exception:  # noqa: BLE001
                pass

    def _on_map(self, *_args) -> None:
        if self.conversation_page is not None:
            return
        tab = self.active_tab()
        if tab is not None:
            try:
                tab.ensure_loaded()
            except Exception:  # noqa: BLE001
                pass

    def _on_destroy(self, *_args) -> None:
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            pass
